"""The full automatic experiment loop: clone -> apply -> recapture -> recompute.

Runs end to end offline via the mock adapter (plan rank 5 / §22.5). With a live
adapter the networked steps refuse without credentials; nothing here mutates
production and the test clone is cleaned up."""
import json

from hotato import core
from hotato.fleet import adapters
from hotato.fleet.api import FleetAPI
from tests import _trial_audio as ta


def _failing_before(tmp_path):
    scen = tmp_path / "scen"; bdir = tmp_path / "before"
    for d in (scen, bdir):
        d.mkdir()
    json.dump({"id": "f1-yield", "caller_onset_sec": 2.0,
               "expected": {"yield": True, "max_time_to_yield_sec": 1.0, "max_talk_over_sec": 1.0}},
              open(scen / "f1-yield.json", "w"))
    ta.talkover_call(str(bdir / "f1-yield.example.wav"))     # agent talks over -> fails
    before = core.run_suite(scenarios_dir=str(scen), audio_dir=str(bdir), suffix=".example.wav")
    json.dump(before, open(bdir / "run.json", "w"))
    return before, str(bdir)


def test_auto_experiment_loop_reaches_improved_and_cleans_up(tmp_path):
    api = FleetAPI(home=str(tmp_path / "home"))
    api.init_workspace("ws1")
    api.agent_add("ws1", "bot", stack="mock")
    before, bdir = _failing_before(tmp_path)
    assert before["events"][0]["verdict"]["passed"] is False
    adapter = adapters.get_adapter("mock", work_dir=str(tmp_path / "work"))
    res = api.experiment_clone_run(
        "ws1", "bot", trial_id="t1", adapter=adapter, source_ref="mock-src",
        variant={"config_delta": {"interrupt_min_words": 1}},
        scenarios=[{"id": "f1-yield", "caller_onset_sec": 2.0}],
        before_env=before, before_dir=bdir,
        policy={"max_talk_over_sec": 1.0, "max_time_to_yield_sec": 1.0}, min_n=1)
    assert res["verdict"] == "improved"
    assert res["evidence_tier"] >= 3            # PAIRED
    assert res["clone"]["cleaned_up"] is True    # test clone removed
    assert "approval is required" in res["recommendation"]   # never auto-deploys
    # a decision row exists, unapproved
    dec = api.registry._all("SELECT approved FROM decisions WHERE workspace_id='ws1'")
    assert dec and dec[0]["approved"] == 0
    api.close()


def test_live_adapter_clone_refuses_without_credentials():
    v = adapters.get_adapter("vapi")     # no api_key
    try:
        v.clone_agent("asst_1", name="staging")
        assert False, "live clone must refuse without credentials"
    except adapters.CapabilityError as e:
        assert "requires credentials" in str(e)
