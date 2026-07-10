"""Agent-native MCP fleet tools: read / verify / propose, no production mutation.

Tested via the standalone functions (no MCP transport / mcp extra required)."""
import os

from hotato import mcp_server as m
from hotato.fleet.api import FleetAPI
from tests import _trial_audio as ta


def _populate(home, tmp_path):
    api = FleetAPI(home=home)
    api.init_workspace("default")
    api.agent_add("default", "bot", stack="vapi")
    wav = str(tmp_path / "c.wav"); ta.talkover_call(wav)
    ing = api.ingest_recording("default", "bot", wav)
    api.discover("default", "bot", wav, recording_id=ing["recording_id"])
    api.close()


def test_fleet_status_and_candidate_list_are_read_only(tmp_path):
    home = str(tmp_path / "home")
    _populate(home, tmp_path)
    status = m.mcp_fleet_status(home, "default")
    assert status["counts"]["agents"] == 1
    cands = m.mcp_candidate_list(home, "default", limit=5)
    assert cands["count"] >= 1
    assert "human must label" in cands["note"]


def test_experiment_propose_is_read_only_and_gated():
    out = m.mcp_experiment_propose(agent_id="bot", contract_id="c1")
    assert len(out["variants"]) == 3
    assert out["pending_irreversible_action"] is None       # a proposal only
    assert "human-gated" in out["note"]


def test_trial_explain_reports_pending_human_gate(tmp_path):
    home = str(tmp_path / "home")
    api = FleetAPI(home=home)
    api.init_workspace("default")
    api.registry.add_trial("default", "t1", agent_id="bot", verdict="improved",
                           evidence_tier=3)
    api.registry.add_decision("default", "d1", trial_id="t1",
                              recommendation="approval required", approved=0)
    api.close()
    out = m.mcp_trial_explain(home, "default", "t1")
    assert out["found"] and out["verdict"] == "improved"
    assert out["pending_irreversible_action"] is not None    # deployment stays gated


def test_artifact_verify_flags_unsigned(tmp_path):
    from hotato import contract as _contract
    wav = str(tmp_path / "c.wav"); ta.talkover_call(wav)
    outdir = str(tmp_path / "contracts")
    _contract.create_contract(stereo=wav, expect="yield", out_dir=outdir,
                              onset_sec=2.0, contract_id="c-001",
                              max_time_to_yield_sec=1.0, max_talk_over_sec=1.0)
    res = m.mcp_artifact_verify(outdir)
    assert res["ok"]
    assert res["authenticity"] == "unsigned"        # created without a signing key
    assert res["authenticated"] is False


def test_experiment_run_action_is_offline_and_gates_deploy(tmp_path):
    import json, os
    from hotato import core
    from hotato.fleet.api import FleetAPI
    scen = tmp_path / "scen"; bdir = tmp_path / "before"; adir = tmp_path / "after"
    for d in (scen, bdir, adir):
        d.mkdir()
    json.dump({"id": "f1-yield", "caller_onset_sec": 2.0,
               "expected": {"yield": True, "max_time_to_yield_sec": 1.0, "max_talk_over_sec": 1.0}},
              open(scen / "f1-yield.json", "w"))
    ta.talkover_call(str(bdir / "f1-yield.example.wav"))
    ta.yielding_call(str(adir / "f1-yield.example.wav"))
    b = core.run_suite(scenarios_dir=str(scen), audio_dir=str(bdir), suffix=".example.wav")
    a = core.run_suite(scenarios_dir=str(scen), audio_dir=str(adir), suffix=".example.wav")
    json.dump(b, open(bdir / "run.json", "w")); json.dump(a, open(adir / "run.json", "w"))
    home = str(tmp_path / "home")
    api = FleetAPI(home=home); api.init_workspace("default"); api.agent_add("default", "bot", stack="mock"); api.close()
    res = m.mcp_experiment_run(home, "default", "bot", "t1", str(bdir), str(bdir), str(adir), 1)
    assert res["ok"] and res["verdict"] == "improved"
    assert res["pending_irreversible_action"] is not None    # deploy stays human-gated
    # cleanup targets only a staging clone
    assert m.mcp_clone_cleanup("mock", "c1", home)["ok"]
