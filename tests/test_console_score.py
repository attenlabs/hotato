"""The console score-on-arrival engine (sidecar + worker + rebuild).

Pinned here: the sidecar schema is versioned and a mismatched version is
refused with a rebuild instruction; the worker scores a completed session
end-to-end from an evidence db built through production.py's own ingest API
(per-dimension observations, ranked candidates, the measured failure-reason
sentence, per-hop latency with authority provenance); refusal is a first-class
NOT_SCORABLE row with its reason (absent path, missing lane, mono recording);
a scorer crash becomes an ERROR row and the loop continues; a persist failure
surfaces as a visible ERROR, never a claimed score; sessions are walked one at
a time (every sessions-table pick is a LIMIT-1 keyset query); and the same
evidence db always rebuilds to a byte-identical canonical dump.
"""

from __future__ import annotations

import shutil
import sqlite3
import wave
from importlib import resources

import pytest

from hotato import __version__, cli
from hotato import console_worker as console_worker_mod
from hotato.console_store import SCHEMA_VERSION, ConsoleStore, ConsoleStoreError
from hotato.console_worker import (
    ConsoleScoreWorker,
    default_console_path,
    rebuild_sidecar,
    run_rebuild,
)
from hotato.production import ProductionStore


def _stereo_fixture() -> str:
    return str(
        resources.files("hotato").joinpath(
            "data", "audio", "01-hard-interruption.example.wav"
        )
    )


def _mono_wav(path) -> str:
    with wave.open(str(path), "wb") as handle:
        handle.setnchannels(1)
        handle.setsampwidth(2)
        handle.setframerate(16000)
        handle.writeframes(b"\x00\x00" * 8000)
    return str(path)


def _event(
    event_id,
    event_type,
    *,
    subject,
    time_value,
    data=None,
    authority="adapter_reported",
    sequence=None,
):
    value = {
        "specversion": "1.0",
        "id": event_id,
        "source": "console-fixture",
        "type": event_type,
        "subject": subject,
        "time": time_value,
        "data": {} if data is None else data,
        "authority": {
            "kind": authority,
            "eligible_for_execution_claim": authority
            in ("measured", "signed_attestation"),
        },
    }
    if sequence is not None:
        value["sequence"] = sequence
    return value


def _ingest_call(
    store,
    subject,
    *,
    audio_path=None,
    with_media_event=True,
    redact_payloads=False,
):
    """One quiesced call session through the production plane's own ingest API:
    lifecycle + audio asset + one model hop + one timed turn."""

    events = [
        _event(f"{subject}-start", "session.started", subject=subject,
               time_value="2026-07-17T12:00:00Z"),
        _event(f"{subject}-turn-a", "turn.started", subject=subject,
               time_value="2026-07-17T12:00:01Z"),
        _event(
            f"{subject}-model",
            "model.operation",
            subject=subject,
            time_value="2026-07-17T12:00:02Z",
            data={"latency_ms": 210.0, "availability": "available"},
        ),
        _event(
            f"{subject}-turn-b",
            "turn.ended",
            subject=subject,
            time_value="2026-07-17T12:00:03.500Z",
            data={"yield_latency_ms": 480.0},
        ),
        _event(f"{subject}-end", "session.ended", subject=subject,
               time_value="2026-07-17T12:00:06Z"),
    ]
    if with_media_event:
        data = {"availability": "available", "channels": 2}
        if audio_path is not None:
            data["path"] = audio_path
        events.insert(
            1,
            _event(
                f"{subject}-audio",
                "media.asset.available",
                subject=subject,
                time_value="2026-07-17T12:00:00.500Z",
                data=data,
                authority="measured",
            ),
        )
    for event in events:
        store.ingest(event, redact_payloads=redact_payloads)


def _evidence_db(tmp_path, name="production.sqlite3"):
    return ProductionStore(str(tmp_path / name), clock=lambda: 1000.0)


def _worker(tmp_path, evidence_path, name="console.sqlite3"):
    sidecar = ConsoleStore(str(tmp_path / name))
    worker = ConsoleScoreWorker(str(evidence_path), sidecar, autostart=False)
    return worker, sidecar


# --- sidecar schema -----------------------------------------------------------

def test_sidecar_schema_version_present_and_mismatch_refused(tmp_path):
    path = tmp_path / "console.sqlite3"
    store = ConsoleStore(str(path))
    assert store.counts() == {"SCORED": 0, "NOT_SCORABLE": 0, "ERROR": 0}
    store.close()

    db = sqlite3.connect(str(path))
    row = db.execute(
        "SELECT value FROM metadata WHERE key='console_schema_version'"
    ).fetchone()
    assert row == (SCHEMA_VERSION,)
    db.execute(
        "UPDATE metadata SET value='999' WHERE key='console_schema_version'"
    )
    db.commit()
    db.close()
    with pytest.raises(ConsoleStoreError, match="--rebuild-scores"):
        ConsoleStore(str(path))


# --- worker scores a completed session end-to-end -----------------------------

def test_worker_scores_completed_session_end_to_end(tmp_path):
    evidence = _evidence_db(tmp_path)
    _ingest_call(evidence, "call-a", audio_path=_stereo_fixture())
    worker, sidecar = _worker(tmp_path, evidence.path)

    result = worker.run_once()
    assert result["scored"] == 1
    assert result["errors"] == 0 and result["persist_failures"] == 0

    record = sidecar.get_score("call-a")
    assert record["state"] == "SCORED"
    assert record["session_state"] == "QUIESCENT"
    # The measured failure-reason sentence is the top candidate's plain-English
    # timing sentence -- built only from measured numbers.
    assert record["reason"].startswith("the caller took the floor")
    assert record["candidates"], "ranked candidate moments recorded"
    assert record["candidates"][0]["kind"] == "overlap_while_agent_talking"
    dims = record["dimensions"]
    assert dims["overlap_while_agent_talking"]["candidate_count"] >= 1
    assert dims["overlap_while_agent_talking"]["worst_sec"] > 0
    assert dims["long_response_gap"]["candidate_count"] == 0

    # Provenance: scorer version + config hash on the record (I5).
    assert record["scorer_version"] == __version__
    assert record["config_sha256"].startswith("sha256:")
    assert record["config"]["scorer"] == "hotato.scan"
    assert record["evidence_sha256"].startswith("sha256:")

    # Timing derives from evidence timestamps only: the model hop keeps its
    # reported latency AND its declared authority; the turn span and the
    # end-to-end figure are labeled as derived from event timestamps.
    hops = {hop["kind"]: hop for hop in record["hops"]}
    assert hops["model.operation"]["latency_ms"] == 210.0
    assert hops["model.operation"]["authority"] == "adapter_reported"
    assert hops["turn"]["latency_ms"] == 2500.0
    assert hops["turn"]["authority"] == "derived:event_timestamps"
    assert record["timing"]["end_to_end_ms"] == 6000.0
    assert record["timing"]["turn_spans"][0]["reported"]["yield_latency_ms"] == 480.0

    # Unchanged evidence is skipped, never re-scored.
    again = worker.run_once()
    assert again["skipped"] == 1 and again["scored"] == 0

    worker.close()
    sidecar.close()
    evidence.close()


# --- refusal is a first-class state ------------------------------------------

def test_not_scorable_rows_carry_their_reason(tmp_path):
    evidence = _evidence_db(tmp_path)
    # Redacted payloads: the media event exists but its path was reduced to a
    # descriptor, so scoring has no recording to read.
    _ingest_call(evidence, "call-redacted", audio_path=_stereo_fixture(),
                 redact_payloads=True)
    # No media event at all: the participant_audio lane never became available.
    _ingest_call(evidence, "call-no-audio", with_media_event=False)
    # A one-channel recording: the scorer's own refusal, verbatim.
    _ingest_call(evidence, "call-mono",
                 audio_path=_mono_wav(tmp_path / "mono.wav"))
    worker, sidecar = _worker(tmp_path, evidence.path)

    result = worker.run_once()
    assert result["not_scorable"] == 3 and result["scored"] == 0

    redacted = sidecar.get_score("call-redacted")
    assert redacted["state"] == "NOT_SCORABLE"
    assert "no local audio path" in redacted["reason"]

    missing = sidecar.get_score("call-no-audio")
    assert missing["state"] == "NOT_SCORABLE"
    assert "participant_audio evidence lane is missing" in missing["reason"]

    mono = sidecar.get_score("call-mono")
    assert mono["state"] == "NOT_SCORABLE"
    assert "one channel" in mono["reason"]
    # Timing still derives from the events even when audio is refused.
    assert mono["timing"]["end_to_end_ms"] == 6000.0

    worker.close()
    sidecar.close()
    evidence.close()


def test_scorer_crash_is_an_error_row_and_the_loop_continues(tmp_path, monkeypatch):
    crashing = str(tmp_path / "crash.wav")
    shutil.copy(_stereo_fixture(), crashing)
    evidence = _evidence_db(tmp_path)
    _ingest_call(evidence, "call-a-crash", audio_path=crashing)
    _ingest_call(evidence, "call-b-fine", audio_path=_stereo_fixture())
    worker, sidecar = _worker(tmp_path, evidence.path)

    real_scan = console_worker_mod.scan_recording

    def crash_on_marked(path, **kwargs):
        if path == crashing:
            raise RuntimeError("boom")
        return real_scan(path, **kwargs)

    monkeypatch.setattr(console_worker_mod, "scan_recording", crash_on_marked)
    result = worker.run_once()
    assert result["errors"] == 1 and result["scored"] == 1

    crashed = sidecar.get_score("call-a-crash")
    assert crashed["state"] == "ERROR"
    assert crashed["reason"] == "RuntimeError: boom"
    assert sidecar.get_score("call-b-fine")["state"] == "SCORED"

    worker.close()
    sidecar.close()
    evidence.close()


def test_persist_failure_surfaces_error_and_never_claims_a_score(
    tmp_path, monkeypatch
):
    evidence = _evidence_db(tmp_path)
    _ingest_call(evidence, "call-a", audio_path=_stereo_fixture())
    worker, sidecar = _worker(tmp_path, evidence.path)

    real_upsert = ConsoleStore.upsert_score

    def failing_upsert(self, record):
        if record["state"] != "ERROR":
            raise sqlite3.OperationalError("disk I/O error")
        return real_upsert(self, record)

    monkeypatch.setattr(ConsoleStore, "upsert_score", failing_upsert)
    result = worker.run_once()
    assert result["persist_failures"] == 1
    assert result["errors"] == 1 and result["scored"] == 0

    surfaced = sidecar.get_score("call-a")
    assert surfaced["state"] == "ERROR"
    assert surfaced["reason"].startswith("persist_failure:")

    # The failure is never settled: once persistence works again, the next
    # cycle re-scores the session.
    monkeypatch.setattr(ConsoleStore, "upsert_score", real_upsert)
    recovered = worker.run_once()
    assert recovered["scored"] == 1
    assert sidecar.get_score("call-a")["state"] == "SCORED"

    worker.close()
    sidecar.close()
    evidence.close()


# --- bounded, one-at-a-time processing ---------------------------------------

def test_sessions_are_walked_one_at_a_time(tmp_path, monkeypatch):
    evidence = _evidence_db(tmp_path)
    for index in range(5):
        _ingest_call(evidence, f"call-{index}", audio_path=_stereo_fixture())
    worker, sidecar = _worker(tmp_path, evidence.path)

    statements = []
    real_open = console_worker_mod._open_evidence_ro

    def traced_open(path):
        db = real_open(path)
        db.set_trace_callback(statements.append)
        return db

    monkeypatch.setattr(console_worker_mod, "_open_evidence_ro", traced_open)
    result = worker.run_once()
    assert result["scored"] == 5

    session_picks = [
        sql for sql in statements
        if "FROM sessions WHERE state IN" in sql
    ]
    assert session_picks, "the worker reads sessions through the keyset query"
    assert all("LIMIT 1" in sql for sql in session_picks), (
        "every sessions pick is a single-row keyset query; no all-sessions "
        "list is materialized"
    )

    worker.close()
    sidecar.close()
    evidence.close()


# --- rebuild determinism ------------------------------------------------------

def test_rebuild_is_deterministic_and_matches_the_worker(tmp_path):
    evidence = _evidence_db(tmp_path)
    _ingest_call(evidence, "call-scored", audio_path=_stereo_fixture())
    _ingest_call(evidence, "call-no-audio", with_media_event=False)
    _ingest_call(evidence, "call-mono",
                 audio_path=_mono_wav(tmp_path / "mono.wav"))

    first = ConsoleStore(str(tmp_path / "rebuild-a.sqlite3"))
    second = ConsoleStore(str(tmp_path / "rebuild-b.sqlite3"))
    summary = rebuild_sidecar(evidence.path, first)
    rebuild_sidecar(evidence.path, second)
    assert summary["scored"] == 1 and summary["not_scorable"] == 2

    dump_a = first.canonical_dump()
    assert dump_a == second.canonical_dump()

    # The worker's incremental sidecar converges to the same canonical bytes.
    worker, incremental = _worker(tmp_path, evidence.path, name="worker.sqlite3")
    worker.run_once()
    assert incremental.canonical_dump() == dump_a

    # A rebuild over an already-populated sidecar regenerates from empty and
    # lands on the same bytes again.
    rebuild_sidecar(evidence.path, first)
    assert first.canonical_dump() == dump_a

    worker.close()
    incremental.close()
    first.close()
    second.close()
    evidence.close()


def test_worker_prunes_rows_for_sessions_that_left_the_scorable_set(tmp_path):
    evidence = _evidence_db(tmp_path)
    _ingest_call(evidence, "call-keep", audio_path=_stereo_fixture())
    _ingest_call(evidence, "call-gone", with_media_event=False)
    worker, sidecar = _worker(tmp_path, evidence.path)
    worker.run_once()
    assert sidecar.get_score("call-gone") is not None

    evidence.delete_session("call-gone")
    result = worker.run_once()
    assert result["pruned"] == 1
    assert sidecar.get_score("call-gone") is None
    assert sidecar.get_score("call-keep") is not None

    worker.close()
    sidecar.close()
    evidence.close()


# --- CLI wiring ---------------------------------------------------------------

def test_rebuild_scores_cli_regenerates_the_sidecar_and_exits(tmp_path, capsys):
    evidence = _evidence_db(tmp_path)
    _ingest_call(evidence, "call-a", audio_path=_stereo_fixture())
    evidence.close()
    db_path = str(tmp_path / "production.sqlite3")

    assert cli.main(["serve", "--rebuild-scores", "--production-db", db_path]) == 0
    out = capsys.readouterr().out
    assert "scored: 1" in out

    sidecar_path = default_console_path(db_path)
    sidecar = ConsoleStore(sidecar_path)
    assert sidecar.counts()["SCORED"] == 1
    sidecar.close()

    # Without the evidence source the rebuild is a usage error, not a server.
    assert cli.main(["serve", "--rebuild-scores"]) == 2


def test_run_rebuild_returns_the_summary(tmp_path):
    evidence = _evidence_db(tmp_path)
    _ingest_call(evidence, "call-a", audio_path=_stereo_fixture())
    evidence.close()
    summary = run_rebuild(str(tmp_path / "production.sqlite3"))
    assert summary["scored"] == 1
    assert summary["sidecar"] == default_console_path(
        str(tmp_path / "production.sqlite3")
    )


def test_score_production_requires_the_evidence_db(tmp_path):
    from hotato.serve.app import run_serve

    with pytest.raises(ValueError, match="--production-db"):
        run_serve(
            workspace="default",
            registry=str(tmp_path / "registry"),
            score_production=True,
            open_browser=False,
        )
