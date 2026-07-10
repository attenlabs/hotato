"""Stack adapter capability contract + the full offline experiment loop."""
import json
import os

from hotato import core, evidence as ev, manifest as m, recompute as rc
from hotato.fleet import adapters
from tests import _trial_audio as ta


def test_capability_declaration_and_gating():
    v = adapters.get_adapter("vapi")
    assert "clone_agent" in v.capabilities() and "snapshot_config" in v.capabilities()
    # networked op without creds refuses (never mutates production silently)
    try:
        v.clone_agent("asst_1", name="staging")
        assert False, "should have refused"
    except adapters.CapabilityError:
        pass
    # offline config hashing works with no creds
    assert len(v.snapshot_config({"turn_taking": {"x": 1}})) == 64
    # source-config target declares no hosted clone
    lk = adapters.get_adapter("livekit")
    assert "clone_agent" not in lk.capabilities()
    assert not lk.supports("clone_agent")


def test_mock_adapter_runs_full_experiment_loop(tmp_path):
    # BEFORE: a failing call (agent talks over the caller)
    scen = tmp_path / "scen"; bdir = tmp_path / "before"
    for d in (scen, bdir):
        d.mkdir()
    json.dump({"id": "f1-yield", "caller_onset_sec": 2.0,
               "expected": {"yield": True, "max_time_to_yield_sec": 1.0, "max_talk_over_sec": 1.0}},
              open(scen / "f1-yield.json", "w"))
    ta.talkover_call(str(bdir / "f1-yield.example.wav"))
    before = core.run_suite(scenarios_dir=str(scen), audio_dir=str(bdir), suffix=".example.wav")
    assert before["events"][0]["verdict"]["passed"] is False

    # CLONE -> APPLY -> RUN SCENARIO -> CAPTURE (all offline via mock)
    adapter = adapters.get_adapter("mock", work_dir=str(tmp_path / "work"))
    clone = adapter.clone_agent("src-asst", name="staging")
    adapter.apply_variant(clone, {"config_delta": {"interrupt_min_words": 1}})
    cap = adapter.run_scenario(clone, {"id": "f1-yield", "caller_onset_sec": 2.0})

    # build the AFTER envelope from the fresh recapture, in a dir named by the id
    adir = tmp_path / "after"; adir.mkdir()
    os.replace(cap["recording"], str(adir / "f1-yield.example.wav"))
    after = core.run_suite(scenarios_dir=str(scen), audio_dir=str(adir), suffix=".example.wav")
    assert after["events"][0]["verdict"]["passed"] is True

    # RECOMPUTE the pair under a pinned manifest -> not refused, PAIRED once enriched
    man = m.build_manifest(before, trial_id="t", nonce="n",
                           policy={"max_talk_over_sec": 1.0, "max_time_to_yield_sec": 1.0}, min_n=1)
    r = rc.recompute_trial(before, str(bdir), after, str(adir), man)
    assert r["refusal"] is None
    vec = dict(r["evidence"]["vector"]); vec["input_health"] = "clean"; vec["channel_mapping"] = "confirmed"
    assert ev.classify(vec)["tier"] >= ev.TIER_PAIRED


def test_delete_clone_cleans_up(tmp_path):
    adapter = adapters.get_adapter("mock", work_dir=str(tmp_path))
    clone = adapter.clone_agent("src", name="s")
    assert adapter.delete_clone(clone)["deleted"] == clone
