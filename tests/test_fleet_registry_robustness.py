"""Regression tests for three confirmed registry robustness defects:

#7  INSERT OR REPLACE (DELETE-then-INSERT) semantics nulled every column the
    caller did not mention, and discover() had no idempotency gate so a
    rescan silently reset an already-reviewed/labeled/dismissed candidate's
    status back to 'new'.
#10 Registry() opened sqlite3 with the implicit 5.0s connect-timeout default
    and nothing translated sqlite3.OperationalError ("database is locked")
    into the shared errors.HANDLED contract, so concurrent multi-process
    writers (an in-spec shape: JobQueue.claim's BEGIN IMMEDIATE, every
    CLI/MCP call opening its own Registry) could crash with a raw traceback.
#11 Registry.__init__ read meta.schema_version but never compared it to the
    module SCHEMA_VERSION constant, so an old client opening a store written
    by a newer/incompatible hotato would proceed silently instead of failing
    closed.
"""
from __future__ import annotations

import os
import sqlite3
import threading
import time

import pytest

from hotato.fleet.api import FleetAPI
from hotato.fleet.registry import SCHEMA_VERSION, TABLE_PK, Registry, RegistrySchemaVersionError
from tests import _trial_audio as ta

# --- #7: real UPSERT preserves unset columns -------------------------------

def test_insert_replace_upsert_preserves_columns_caller_did_not_mention(tmp_path):
    """The add_recording PII-wipe landmine: a second add_recording() call on an
    existing recording_id must not revert retention_policy_json/pii_class to
    NULL just because that call's dict did not mention them (the old
    INSERT OR REPLACE did DELETE-then-INSERT, which nulled everything absent
    from the new row)."""
    reg = Registry(home=str(tmp_path))
    reg.add_recording("wsA", "rec1", call_id="call1", raw_sha256="raw1",
                      pcm_sha256="pcm1", channel_layout="stereo")
    reg.set_recording_privacy("wsA", "rec1", retention_policy_json='{"days":30}',
                              pii_class="phi")
    row = reg._one("SELECT * FROM recordings WHERE workspace_id=? AND recording_id=?",
                   ("wsA", "rec1"))
    assert row["pii_class"] == "phi" and row["retention_policy_json"] == '{"days":30}'

    # A second add_recording (e.g. a retried/duplicated ingest) does not
    # mention retention_policy_json/pii_class at all.
    reg.add_recording("wsA", "rec1", call_id="call1", raw_sha256="raw1",
                      pcm_sha256="pcm1", channel_layout="stereo")
    row = reg._one("SELECT * FROM recordings WHERE workspace_id=? AND recording_id=?",
                   ("wsA", "rec1"))
    assert row["pii_class"] == "phi", "pii_class was wiped by a re-add_recording"
    assert row["retention_policy_json"] == '{"days":30}', (
        "retention_policy_json was wiped by a re-add_recording")
    # columns the second call DID mention still take the new value
    reg.add_recording("wsA", "rec1", call_id="call1", raw_sha256="raw2",
                      pcm_sha256="pcm1", channel_layout="stereo")
    row = reg._one("SELECT * FROM recordings WHERE workspace_id=? AND recording_id=?",
                   ("wsA", "rec1"))
    assert row["raw_sha256"] == "raw2"
    assert row["pii_class"] == "phi"  # still preserved
    reg.close()


def test_table_pk_map_covers_every_replace_true_table(tmp_path):
    """_insert(replace=True) raises a clean ValueError (not a raw KeyError) for
    any table missing from TABLE_PK, so the map staying in sync with the
    schema is enforced rather than silently mis-upserting."""
    reg = Registry(home=str(tmp_path))
    assert "recordings" in TABLE_PK and "candidates" in TABLE_PK
    with pytest.raises(ValueError):
        reg._insert("not_a_real_table", {"workspace_id": "wsA", "x": 1}, replace=True)
    reg.close()


# --- #7: discover() idempotency gate ---------------------------------------

def test_rescan_does_not_clobber_a_labeled_candidates_status(tmp_path):
    """The live/reachable half of #7: fleet run's discover() regenerates the
    SAME deterministic candidate_id on a rescan of an already-processed
    recording. Before the fix this silently reset an already-labeled
    candidate's status back to 'new' (and nulled severity/measured_json),
    reintroducing an adjudicated candidate into the active review queue."""
    api = FleetAPI(home=str(tmp_path / "home"))
    api.init_workspace("ws1", "Acme")
    api.agent_add("ws1", "support-bot", stack="vapi")
    wav = str(tmp_path / "call.wav")
    ta.talkover_call(wav)
    ing = api.ingest_recording("ws1", "support-bot", wav)
    disc1 = api.discover("ws1", "support-bot", wav, recording_id=ing["recording_id"])
    assert disc1["candidates"]
    cid = disc1["candidates"][0]["candidate_id"]

    # a human reviews and labels the candidate
    lab = api.label("ws1", cid, decision="yield", reviewer="alice")
    assert lab["status"] == "labeled"
    row = api.registry._one(
        "SELECT * FROM candidates WHERE workspace_id=? AND candidate_id=?", ("ws1", cid))
    assert row["status"] == "labeled"
    assert row["severity"] is not None

    # a rescan of the SAME recording (fleet run re-invoked, a retried job)
    # regenerates the identical deterministic candidate_id.
    disc2 = api.discover("ws1", "support-bot", wav, recording_id=ing["recording_id"])
    cid2 = disc2["candidates"][0]["candidate_id"]
    assert cid2 == cid  # deterministic id, confirmed reproduced

    row_after = api.registry._one(
        "SELECT * FROM candidates WHERE workspace_id=? AND candidate_id=?", ("ws1", cid))
    assert row_after["status"] == "labeled", (
        "rescan reset an already-labeled candidate back to 'new'")
    assert row_after["severity"] is not None, "rescan nulled severity on an adjudicated candidate"
    assert row_after["measured_json"] is not None, "rescan nulled measured_json"

    # the labeled candidate must not reappear in the 'new' review queue
    queue_ids = [c["candidate_id"] for c in api.review_queue("ws1")]
    assert cid not in queue_ids
    api.close()


def test_rescan_still_discovers_a_genuinely_new_recording(tmp_path):
    """The idempotency gate must not suppress discovery of a DIFFERENT
    recording; only a rescan of the identical recording_id is a no-op on
    already-known candidate ids."""
    api = FleetAPI(home=str(tmp_path / "home"))
    api.init_workspace("ws1")
    api.agent_add("ws1", "support-bot", stack="vapi")
    wav1 = str(tmp_path / "call1.wav"); ta.talkover_call(wav1)
    wav2 = str(tmp_path / "call2.wav"); ta.talkover_call(wav2, onset=1.0)
    ing1 = api.ingest_recording("ws1", "support-bot", wav1)
    ing2 = api.ingest_recording("ws1", "support-bot", wav2)
    assert ing1["recording_id"] != ing2["recording_id"]
    disc1 = api.discover("ws1", "support-bot", wav1, recording_id=ing1["recording_id"])
    disc2 = api.discover("ws1", "support-bot", wav2, recording_id=ing2["recording_id"])
    assert disc1["candidates"] and disc2["candidates"]
    assert (disc1["candidates"][0]["candidate_id"]
            != disc2["candidates"][0]["candidate_id"])
    api.close()


# --- #10: sqlite busy-timeout ceiling + clean error translation ------------

def test_registry_survives_a_write_lock_held_past_the_old_5s_default(tmp_path):
    """Reproduces the finding directly: a raw connection holds BEGIN IMMEDIATE
    for longer than sqlite3's old implicit 5.0s connect-timeout default. A
    fresh Registry() (which itself writes: schema init + meta bookkeeping)
    must wait it out and succeed instead of raising after ~5s."""
    home = str(tmp_path)
    Registry(home=home).close()  # create the db file / schema first
    db_path = os.path.join(home, "fleet.db")

    started = threading.Event()

    def holder():
        con = sqlite3.connect(db_path, timeout=30)
        con.execute("BEGIN IMMEDIATE")
        started.set()
        time.sleep(6.5)  # longer than the OLD implicit 5.0s connect timeout
        con.execute("COMMIT")
        con.close()

    t = threading.Thread(target=holder)
    t.start()
    assert started.wait(timeout=5)
    time.sleep(0.3)  # let the holder's BEGIN IMMEDIATE actually land

    t0 = time.time()
    waiter = Registry(home=home)          # must NOT raise
    waiter.ensure_workspace("ws1")
    elapsed = time.time() - t0
    t.join()
    assert elapsed > 5.0, "did not actually wait past the old 5s ceiling"
    assert waiter.get_workspace("ws1") is not None
    waiter.close()


def test_busy_database_raises_clean_oserror_not_a_raw_sqlite_traceback(tmp_path, monkeypatch):
    """When contention genuinely exceeds even the widened busy window, the
    failure must surface as the shared errors.HANDLED OSError (clean,
    actionable, exit-2 structured error) rather than a raw, uncaught
    sqlite3.OperationalError traceback. Re-opening an ALREADY-initialized,
    up-to-date store makes no write of its own (schema exists, schema_version
    already matches), so the write that actually contends for the lock is a
    real one after open -- ensure_workspace(), mirroring the finding's own
    repro shape."""
    home = str(tmp_path)
    Registry(home=home).close()
    db_path = os.path.join(home, "fleet.db")
    monkeypatch.setenv("HOTATO_FLEET_DB_TIMEOUT_SEC", "1")

    started = threading.Event()

    def holder():
        con = sqlite3.connect(db_path, timeout=30)
        con.execute("BEGIN IMMEDIATE")
        started.set()
        time.sleep(3.0)  # longer than the 1s window configured above
        con.execute("COMMIT")
        con.close()

    t = threading.Thread(target=holder)
    t.start()
    assert started.wait(timeout=5)
    time.sleep(0.3)

    waiter = Registry(home=home)  # no write needed here; succeeds even under 1s
    with pytest.raises(OSError) as exc_info:
        waiter.ensure_workspace("ws1")  # a real write; must hit the held lock
    assert not isinstance(exc_info.value, sqlite3.OperationalError)
    assert "busy" in str(exc_info.value).lower()
    assert "retry" in str(exc_info.value).lower()
    t.join()


# --- #11: schema-version enforcement ---------------------------------------

def test_newer_schema_version_refuses_closed_with_clean_error(tmp_path):
    """A store written by a newer hotato (higher schema_version) must refuse
    to open with a clean, actionable RegistrySchemaVersionError instead of
    silently proceeding to run additive migrations / DML against a store
    shape it does not understand."""
    home = tmp_path / "store"
    home.mkdir()
    Registry(home=str(home)).close()
    db_path = home / "fleet.db"
    con = sqlite3.connect(str(db_path))
    con.execute("UPDATE meta SET value=? WHERE key='schema_version'",
               (str(SCHEMA_VERSION + 1),))
    con.commit()
    con.close()

    with pytest.raises(RegistrySchemaVersionError) as exc_info:
        Registry(home=str(home))
    msg = str(exc_info.value)
    assert str(db_path) in msg
    assert str(SCHEMA_VERSION + 1) in msg and str(SCHEMA_VERSION) in msg
    # it is also a ValueError, so it free-rides on errors.HANDLED with no
    # plumbing changes needed in cli.py / mcp_server.py.
    assert isinstance(exc_info.value, ValueError)

    # the connection must not be left open/leaked on the refusal path
    con2 = sqlite3.connect(str(db_path), timeout=1)
    con2.execute("BEGIN IMMEDIATE")   # would fail if the old connection still held a lock
    con2.execute("ROLLBACK")
    con2.close()


def test_garbage_schema_version_value_fails_closed_same_as_newer(tmp_path):
    """A non-numeric/corrupt meta.schema_version value is treated the same as
    'newer/unknown' (fail closed on ambiguity), never silently coerced or
    ignored."""
    home = tmp_path / "store"
    home.mkdir()
    Registry(home=str(home)).close()
    db_path = home / "fleet.db"
    con = sqlite3.connect(str(db_path))
    con.execute("UPDATE meta SET value=? WHERE key='schema_version'", ("not-a-number",))
    con.commit()
    con.close()

    with pytest.raises(RegistrySchemaVersionError):
        Registry(home=str(home))


def test_older_schema_version_upgrades_the_stored_marker(tmp_path):
    """A store from an OLDER schema_version (lower than SCHEMA_VERSION) is
    accepted (the additive migrations already handle the actual upgrade) and
    the stored marker is advanced to the current version rather than staying
    stale forever."""
    home = tmp_path / "store"
    home.mkdir()
    Registry(home=str(home)).close()
    db_path = home / "fleet.db"
    con = sqlite3.connect(str(db_path))
    con.execute("UPDATE meta SET value='0' WHERE key='schema_version'")
    con.commit()
    con.close()

    reg = Registry(home=str(home))
    row = reg._one("SELECT value FROM meta WHERE key='schema_version'")
    assert int(row["value"]) == SCHEMA_VERSION
    reg.close()


def test_same_schema_version_opens_normally(tmp_path):
    """The common case (matching schema_version) must not raise and must not
    rewrite meta unnecessarily."""
    reg1 = Registry(home=str(tmp_path))
    reg1.ensure_workspace("ws1")
    reg1.close()
    reg2 = Registry(home=str(tmp_path))  # re-open: schema_version already matches
    assert reg2.get_workspace("ws1") is not None
    reg2.close()
