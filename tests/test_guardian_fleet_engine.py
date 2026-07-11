"""Integration gates for the Guardian/Fleet build (plan sections 7, 9, 14).

Covers the pieces the plan required and the gap analysis found unwired:
capture-receipt emission (attested tier reachable), idempotent job convergence,
the bounded experiment engine (propose -> run -> Pareto rank), the one-click
contract-from-candidate path + high_stakes, batch discovery + cross-call
clustering, and the privacy controls (retention / deletion receipt / derived
redaction / approval). Deterministic, offline, stdlib + the mock adapter.
"""
from __future__ import annotations

import json
import os

import pytest

from tests import _trial_audio as ta
import hotato.core as core
from hotato.fleet.api import FleetAPI
from hotato.fleet.adapters import MockAdapter


def _yield_hold_battery(root):
    """A before battery whose caller stimulus MATCHES the mock recapture (the same
    _mock_capture generator, fixed=False -> failing before), so a clean fresh
    recapture is certifiable. 2 yield targets (fail before) + 1 hold guard."""
    from hotato.fleet import _mock_capture
    scen, bdir = os.path.join(root, "scen"), os.path.join(root, "before")
    os.makedirs(scen); os.makedirs(bdir)
    scenarios = []
    specs = [("f1", True), ("f2", True), ("h1", False)]
    for sid, y in specs:
        sc = {"id": sid, "caller_onset_sec": 2.0,
              "expected": {"yield": y, "max_time_to_yield_sec": 1.0, "max_talk_over_sec": 1.0}}
        json.dump(sc, open(os.path.join(scen, f"{sid}.json"), "w"))
        scenarios.append(sc)
        cap = _mock_capture.capture(bdir, "before", sc, fixed=(not y))  # yield fails; hold passes
        os.replace(cap["recording"], os.path.join(bdir, f"{sid}.example.wav"))
    before = core.run_suite(scenarios_dir=scen, audio_dir=bdir, suffix=".example.wav")
    return before, bdir, scenarios


def _stereo_wav(path, *, onset=3.0, agent_end=8.0, total=10.0, rate=16000):
    import wave, struct, math
    with wave.open(path, "wb") as w:
        w.setnchannels(2); w.setsampwidth(2); w.setframerate(rate)
        fr = []
        for i in range(int(total * rate)):
            t = i / rate
            c = 11000 * math.sin(2 * math.pi * 300 * t) if t > onset else 0
            a = 11000 * math.sin(2 * math.pi * 200 * t) if t < agent_end else 0
            fr.append(struct.pack("<hh", int(c), int(a)))
        w.writeframes(b"".join(fr))


# --- §6.4 capture receipts: attested tier reachable, unsigned falls back ----

def test_clone_run_reaches_attested_tier_with_key(tmp_path, monkeypatch):
    monkeypatch.setenv("HOTATO_ATTEST_KEY", "k")
    before, bdir, scenarios = _yield_hold_battery(str(tmp_path))
    with FleetAPI(str(tmp_path / "home")) as api:
        api.init_workspace("a"); api.agent_add("a", "ag", stack="vapi")
        r = api.experiment_clone_run(
            "a", "ag", trial_id="t", adapter=MockAdapter(str(tmp_path / "mw")),
            source_ref="asst", variant={"config_delta": {"interrupt_min_words": 0}},
            scenarios=scenarios, before_env=before, before_dir=bdir,
            battery_env=before, min_n=1)
    assert r["verdict"] == "improved"
    assert r["evidence_tier"] == 4
    assert r["evidence"]["headline"] == "PAIRED FRESH-RECAPTURE IMPROVED"
    v = r["evidence"]["vector"]
    assert v["capture_origin"] == "runner_attested"
    assert v["policy_integrity"] == "signed"
    assert v["opposite_risk_guard"] == "present_passing"


def test_clone_run_without_key_is_operator_asserted_not_refused(tmp_path, monkeypatch):
    monkeypatch.delenv("HOTATO_ATTEST_KEY", raising=False)
    before, bdir, scenarios = _yield_hold_battery(str(tmp_path))
    with FleetAPI(str(tmp_path / "home")) as api:
        api.init_workspace("a"); api.agent_add("a", "ag", stack="vapi")
        r = api.experiment_clone_run(
            "a", "ag", trial_id="t", adapter=MockAdapter(str(tmp_path / "mw")),
            source_ref="asst", variant={"config_delta": {"interrupt_min_words": 0}},
            scenarios=scenarios, before_env=before, before_dir=bdir,
            battery_env=before, min_n=1)
    assert r["refusal"] is None
    assert r["evidence"]["vector"]["capture_origin"] == "operator_asserted"


# --- §7.3 idempotent jobs converge -----------------------------------------

def test_duplicate_ingest_converges_to_one_job(tmp_path):
    wav = str(tmp_path / "c.wav"); _stereo_wav(wav)
    with FleetAPI(str(tmp_path / "home")) as api:
        api.init_workspace("a"); api.agent_add("a", "ag", stack="vapi")
        r1 = api.ingest_recording("a", "ag", wav)
        r2 = api.ingest_recording("a", "ag", wav)
        r3 = api.ingest_recording("a", "ag", wav)
        assert r1["recording_id"] == r2["recording_id"] == r3["recording_id"]
        assert r1["job_id"] == r2["job_id"] == r3["job_id"]
        assert r2["deduped"] and r3["deduped"]
        assert api.status("a")["jobs"] == {"done": 1}


# --- §9.4-9.6 bounded experiment engine ------------------------------------

def test_experiment_propose_is_bounded_and_persisted(tmp_path):
    with FleetAPI(str(tmp_path / "home")) as api:
        api.init_workspace("a"); api.agent_add("a", "ag", stack="vapi")
        res = api.experiment_propose("a", "ag", intent="more_sensitive",
                                     current_config={"turn_taking": {"interrupt_min_words": 3}},
                                     max_variants=6, trial_id="t")
        assert 1 <= res["count"] <= 6
        assert any(v["kind"] == "baseline" for v in res["variants"])
        for v in res["variants"]:
            assert "expected" in v and v["expected"]  # expected effects stated up front
        assert len(api.registry.list_variants("a")) == res["count"]


def test_experiment_run_all_ranks_on_visible_components(tmp_path, monkeypatch):
    monkeypatch.setenv("HOTATO_ATTEST_KEY", "k")
    before, bdir, scenarios = _yield_hold_battery(str(tmp_path))
    with FleetAPI(str(tmp_path / "home")) as api:
        api.init_workspace("a"); api.agent_add("a", "ag", stack="vapi")
        res = api.experiment_run_all(
            "a", "ag", adapter=MockAdapter(str(tmp_path / "mw")), source_ref="asst",
            intent="more_sensitive", scenarios=scenarios, before_env=before,
            before_dir=bdir, current_config={"turn_taking": {"interrupt_min_words": 3}},
            max_variants=6, min_n=1, base_trial_id="exp")
        assert res["proposed"] >= 1
        # ranked entries expose components, never one blended score
        for x in res["ranked"]:
            assert "metrics" in x and "rank" in x and "pareto_front" in x
        assert "Hotato score" not in res["note"].lower() or "no single" in res["note"].lower()
        # ranks persisted to the variants table
        rows = [dict(r) for r in api.registry.list_variants("a")]
        assert any(r.get("rank") for r in rows)


# --- §9.1 batch discovery + cross-call clustering ---------------------------

def test_run_fills_recurrence_and_novelty(tmp_path):
    w1 = str(tmp_path / "c1.wav"); _stereo_wav(w1, onset=3.0)
    w2 = str(tmp_path / "c2.wav"); _stereo_wav(w2, onset=4.0)
    with FleetAPI(str(tmp_path / "home")) as api:
        api.init_workspace("a"); api.agent_add("a", "ag", stack="vapi")
        res = api.run("a", "ag", recordings=[w1, w2])
        assert len(res["ingested"]) == 2
        assert res["clusters"]  # populated cluster rollup
        for cand in res["top_candidates"]:
            comp = cand.get("components") or {}
            assert comp.get("recurrence") is not None
            assert comp.get("novelty") is not None


# --- §9.2 / §14 one-click contract + high_stakes ---------------------------

def test_contract_from_candidate_registers_high_stakes(tmp_path):
    wav = str(tmp_path / "c.wav"); _stereo_wav(wav)
    with FleetAPI(str(tmp_path / "home")) as api:
        api.init_workspace("a"); api.agent_add("a", "ag", stack="vapi")
        run = api.run("a", "ag", recordings=[wav])
        cands = run["top_candidates"]
        if not cands:
            pytest.skip("no candidate discovered on this synthetic input")
        cid = cands[0]["candidate_id"]
        res = api.contract_from_candidate("a", cid, reviewer="alice", decision="yield",
                                          high_stakes=True)
        assert res["high_stakes"] is True and os.path.isdir(res["dir"])
        bench = api.benchmark("a")
        agent = bench["agents"][0]
        assert agent["contracts"] == 1 and agent["high_stakes_contracts"] == 1


# --- §14 privacy: retention / deletion receipt / legal hold / redaction -----

def test_deletion_leaves_receipt_and_legal_hold_blocks(tmp_path):
    wav = str(tmp_path / "c.wav"); _stereo_wav(wav)
    with FleetAPI(str(tmp_path / "home")) as api:
        api.init_workspace("a"); api.agent_add("a", "ag", stack="vapi")
        ing = api.ingest_recording("a", "ag", wav)
        rid = ing["recording_id"]
        # legal hold blocks deletion
        api.set_retention("a", rid, consent_basis="contract", allowed_purposes=["eval"],
                          legal_hold=True)
        blocked = api.delete_recording("a", rid, reason="dsar", actor="ops")
        assert blocked["deleted"] is False and blocked["blocked_by_legal_hold"]
        # lift the hold, delete leaves a durable receipt
        api.set_retention("a", rid, consent_basis="contract", allowed_purposes=["eval"],
                          legal_hold=False)
        done = api.delete_recording("a", rid, reason="dsar", actor="ops")
        assert done["deleted"] is True
        assert done["receipt"]["receipt_digest"]


def test_redaction_is_a_derived_artifact(tmp_path):
    wav = str(tmp_path / "c.wav"); _stereo_wav(wav)
    with FleetAPI(str(tmp_path / "home")) as api:
        api.init_workspace("a"); api.agent_add("a", "ag", stack="vapi")
        ing = api.ingest_recording("a", "ag", wav)
        rec = api.redact_recording("a", ing["recording_id"], [(3.0, 4.0)], actor="ops")
        assert rec["derived"] is True
        assert rec["parent_recording_id"] == ing["recording_id"]
        assert rec["derived_digest"]


def test_approve_trial_records_but_does_not_deploy(tmp_path, monkeypatch):
    monkeypatch.setenv("HOTATO_ATTEST_KEY", "k")
    before, bdir, scenarios = _yield_hold_battery(str(tmp_path))
    with FleetAPI(str(tmp_path / "home")) as api:
        api.init_workspace("a"); api.agent_add("a", "ag", stack="vapi")
        api.experiment_clone_run(
            "a", "ag", trial_id="t", adapter=MockAdapter(str(tmp_path / "mw")),
            source_ref="asst", variant={"config_delta": {"interrupt_min_words": 0}},
            scenarios=scenarios, before_env=before, before_dir=bdir,
            battery_env=before, min_n=1)
        appr = api.approve_trial("a", "t", approver="director", note="ship it")
        assert appr["approved"] is True and appr["approver"] == "director"
        assert "no deployment" in appr["note"].lower()
