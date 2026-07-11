"""Fleet registry: the persisted §7.1 entities added in this change.

Exercises each new table's add/get/list, asserts workspace isolation (a row in
one workspace is never returned for another), asserts the additive migration
path (an OLD-shaped DB missing the new columns is upgraded on open), and
validates a deployment_receipt against its shipped JSON Schema.
"""
import json
import sqlite3
from importlib import resources

import pytest

from hotato.fleet import canary
from hotato.fleet.registry import Registry


def test_contract_set_immutable_ordered_and_scoped(tmp_path):
    reg = Registry(home=str(tmp_path))
    reg.add_contract_set("wsA", "set1", ["h3", "h1", "h2"])   # order preserved
    row = reg.get_contract_set("wsA", "set1")
    assert json.loads(row["member_contract_hashes"]) == ["h3", "h1", "h2"]
    # a string membership is accepted verbatim
    reg.add_contract_set("wsA", "set2", '["a","b"]')
    assert json.loads(reg.get_contract_set("wsA", "set2")["member_contract_hashes"]) == ["a", "b"]
    # immutable: re-inserting the same (workspace, set_id) raises
    with pytest.raises(sqlite3.IntegrityError):
        reg.add_contract_set("wsA", "set1", ["x"])
    # workspace isolation
    assert reg.get_contract_set("wsB", "set1") is None
    reg.close()


def test_deployment_receipts_add_list_and_scoped(tmp_path):
    reg = Registry(home=str(tmp_path))
    reg.add_deployment_receipt("wsA", "dr1", agent_id="bot-a", kind="canary",
                               variant_id="v1", config_hash="cfg", prior_revision=4,
                               detail_json='{"plan":"5pct"}', receipt_digest="deadbeef")
    reg.add_deployment_receipt("wsA", "dr2", agent_id="bot-a", kind="rollback",
                               prior_revision=3, receipt_digest="feedface")
    reg.add_deployment_receipt("wsB", "dr9", agent_id="bot-z", kind="clone",
                               receipt_digest="00")
    assert len(reg.list_deployment_receipts("wsA")) == 2
    assert len(reg.list_deployment_receipts("wsA", kind="canary")) == 1
    assert reg.list_deployment_receipts("wsA", agent_id="bot-a")[0]["receipt_digest"] in {"deadbeef", "feedface"}
    # workspace isolation: wsB's clone is never visible to wsA
    assert all(r["agent_id"] != "bot-z" for r in reg.list_deployment_receipts("wsA"))
    assert len(reg.list_deployment_receipts("wsB")) == 1
    reg.close()


def test_attestations_add_list_and_scoped(tmp_path):
    reg = Registry(home=str(tmp_path))
    reg.add_attestation("wsA", "att1", subject_kind="contract", subject_id="c1",
                        signer="alice", subject_digest="sd1", statement="passes",
                        algorithm="ed25519")
    reg.add_attestation("wsB", "att2", subject_kind="contract", subject_id="c1",
                        signer="mallory", subject_digest="sd2", statement="x",
                        algorithm="hmac-sha256")
    got = reg.list_attestations("wsA")
    assert len(got) == 1 and got[0]["signer"] == "alice"
    assert reg.list_attestations("wsA", subject_kind="contract", subject_id="c1")[0]["subject_digest"] == "sd1"
    # workspace isolation
    assert [a["signer"] for a in reg.list_attestations("wsB")] == ["mallory"]
    reg.close()


def test_variants_add_list_ranked_and_scoped(tmp_path):
    reg = Registry(home=str(tmp_path))
    reg.add_variant("wsA", "v2", trial_id="t1", agent_id="bot-a", rank=2,
                    config_delta_json="{}", expected_json='{"e":1}', observed_json='{"o":1}',
                    eligible=1)
    reg.add_variant("wsA", "v1", trial_id="t1", agent_id="bot-a", rank=1)
    reg.add_variant("wsA", "v9", trial_id="t2", agent_id="bot-a")
    reg.add_variant("wsB", "vz", trial_id="t1", agent_id="bot-z")
    # ordered by rank within the trial
    ranked = reg.list_variants("wsA", trial_id="t1")
    assert [v["variant_id"] for v in ranked] == ["v1", "v2"]
    assert ranked[0]["agent_id"] == "bot-a"
    assert json.loads(ranked[1]["expected_json"]) == {"e": 1}
    # trial filter + workspace isolation
    assert len(reg.list_variants("wsA")) == 3
    assert [v["variant_id"] for v in reg.list_variants("wsA", trial_id="t2")] == ["v9"]
    assert [v["variant_id"] for v in reg.list_variants("wsB")] == ["vz"]
    reg.close()


def test_watermarks_get_set_and_scoped(tmp_path):
    reg = Registry(home=str(tmp_path))
    assert reg.get_watermark("wsA", "bot-a", "vapi") is None
    reg.set_watermark("wsA", "bot-a", "vapi", 100.0)
    reg.set_watermark("wsA", "bot-a", "retell", 5.0)          # distinct source
    reg.set_watermark("wsA", "bot-a", "vapi", 200.0)          # advances
    reg.set_watermark("wsB", "bot-a", "vapi", 999.0)          # other workspace
    assert reg.get_watermark("wsA", "bot-a", "vapi") == 200.0
    assert reg.get_watermark("wsA", "bot-a", "retell") == 5.0
    # workspace isolation: same agent+source key, different workspace
    assert reg.get_watermark("wsB", "bot-a", "vapi") == 999.0
    assert reg.get_watermark("wsA", "bot-b", "vapi") is None
    reg.close()


def test_migration_upgrades_old_db_missing_new_columns(tmp_path):
    """An existing DB file predating the privacy/variant columns is upgraded when
    a Registry is constructed over it, and the new methods then work."""
    home = tmp_path / "old"
    home.mkdir()
    con = sqlite3.connect(str(home / "fleet.db"))
    # OLD-shaped recordings: no retention_policy_json / pii_class
    con.execute("""CREATE TABLE recordings (
        workspace_id TEXT NOT NULL, recording_id TEXT NOT NULL, call_id TEXT,
        raw_sha256 TEXT, pcm_sha256 TEXT, artifact_digest TEXT,
        channel_layout TEXT, captured_at REAL,
        PRIMARY KEY (workspace_id, recording_id))""")
    # OLD-shaped variants: no agent_id / expected_json / observed_json / rank
    con.execute("""CREATE TABLE variants (
        workspace_id TEXT NOT NULL, variant_id TEXT NOT NULL, trial_id TEXT,
        config_delta_json TEXT, expected_effect TEXT, observed_effect TEXT,
        eligible INTEGER, created_at REAL,
        PRIMARY KEY (workspace_id, variant_id))""")
    con.commit()
    con.close()

    reg = Registry(home=str(home))
    rcols = {r["name"] for r in reg.conn.execute("PRAGMA table_info(recordings)")}
    assert {"retention_policy_json", "pii_class"} <= rcols
    vcols = {r["name"] for r in reg.conn.execute("PRAGMA table_info(variants)")}
    assert {"agent_id", "expected_json", "observed_json", "rank"} <= vcols

    reg.add_recording("wsA", "rec1")
    reg.set_recording_privacy("wsA", "rec1", retention_policy_json='{"days":30}', pii_class="phi")
    row = reg._one("SELECT * FROM recordings WHERE workspace_id=? AND recording_id=?",
                   ("wsA", "rec1"))
    assert row["retention_policy_json"] == '{"days":30}' and row["pii_class"] == "phi"

    reg.add_variant("wsA", "v1", trial_id="t1", agent_id="bot-a", rank=1)
    assert reg.list_variants("wsA", trial_id="t1")[0]["agent_id"] == "bot-a"
    reg.close()


def test_set_recording_privacy_partial_update(tmp_path):
    reg = Registry(home=str(tmp_path))
    reg.add_recording("wsA", "rec1")
    reg.set_recording_privacy("wsA", "rec1", pii_class="pii")   # only one field
    row = reg._one("SELECT * FROM recordings WHERE workspace_id=? AND recording_id=?",
                   ("wsA", "rec1"))
    assert row["pii_class"] == "pii" and row["retention_policy_json"] is None
    reg.set_recording_privacy("wsA", "rec1", retention_policy_json='{"days":7}')
    row = reg._one("SELECT * FROM recordings WHERE workspace_id=? AND recording_id=?",
                   ("wsA", "rec1"))
    assert row["pii_class"] == "pii" and row["retention_policy_json"] == '{"days":7}'
    reg.close()


def test_deployment_receipt_helper_is_pure_and_deterministic():
    r1 = canary.deployment_receipt("canary", agent_id="bot-a", variant_id="v1",
                                   config_hash="cfg", prior_revision=4, detail={"plan": "5pct"})
    r2 = canary.deployment_receipt("canary", agent_id="bot-a", variant_id="v1",
                                   config_hash="cfg", prior_revision=4, detail={"plan": "5pct"})
    assert r1 == r2                                  # deterministic
    assert len(r1["receipt_digest"]) == 64
    assert r1["kind"] == "canary" and r1["schema_version"] == "1"
    # a different field changes the digest
    r3 = canary.deployment_receipt("rollback", agent_id="bot-a", prior_revision=3)
    assert r3["receipt_digest"] != r1["receipt_digest"]
    with pytest.raises(ValueError):
        canary.deployment_receipt("upsell", agent_id="bot-a")


def test_deployment_receipt_validates_against_schema_and_persists(tmp_path):
    rec = canary.deployment_receipt("canary", agent_id="bot-a", variant_id="v1",
                                    config_hash="cfg", prior_revision=4, detail={"plan": "5pct"})
    # persists uniformly through the registry
    reg = Registry(home=str(tmp_path))
    reg.add_deployment_receipt("wsA", "dr1", agent_id=rec["agent_id"], kind=rec["kind"],
                               variant_id=rec["variant_id"], config_hash=rec["config_hash"],
                               prior_revision=rec["prior_revision"],
                               detail_json=json.dumps(rec["detail"]),
                               receipt_digest=rec["receipt_digest"])
    got = reg.list_deployment_receipts("wsA")
    assert got and got[0]["receipt_digest"] == rec["receipt_digest"]
    reg.close()

    jsonschema = pytest.importorskip("jsonschema")
    schema = json.loads(resources.files("hotato").joinpath(
        "schema", "deployment_receipt.v1.json").read_text(encoding="utf-8"))
    jsonschema.validate(instance=rec, schema=schema)
    bad = dict(rec); bad["kind"] = "upsell"          # not an allowed kind
    with pytest.raises(jsonschema.ValidationError):
        jsonschema.validate(instance=bad, schema=schema)
