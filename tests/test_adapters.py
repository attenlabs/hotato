"""Stack adapter capability contract + the full offline experiment loop."""
import json
import os

import pytest

from hotato import core
from hotato import evidence as ev
from hotato import manifest as m
from hotato import recompute as rc
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


def test_live_adapter_does_not_declare_unwired_mutations(tmp_path):
    # HONEST capabilities: delete_clone is now implemented + verified against the
    # live Vapi API, so Vapi advertises it. rollback (a PRODUCTION revert) is still
    # not wired for a hosted provider, so it stays unavailable. The mock does all.
    v = adapters.get_adapter("vapi")
    assert v.supports("delete_clone")            # wired + verified live
    assert "rollback" not in v.capabilities()
    assert not v.supports("rollback")
    mock = adapters.get_adapter("mock", work_dir=str(tmp_path))
    assert mock.supports("rollback") and mock.supports("delete_clone")


def test_source_id_path_injection_is_refused():
    # a source id smuggling an extra path segment is refused before any URL is
    # built or any network call is made -- on both the write (apply_variant) and
    # read (inspect_config) paths.
    v = adapters.get_adapter("vapi", api_key="sk-test-key")
    with pytest.raises(ValueError):
        v.apply_variant({"source_id": "asst/../x", "name": "staging"},
                        {"config_delta": {"firstMessage": "x"}})
    with pytest.raises(ValueError):
        v.inspect_config("asst/../x")


# --- honest capability discovery (regression: adapters must not over-declare) --

def _invoke_op(adapter, cap):
    """Invoke an operation-backed capability with representative args, staging a
    fresh clone first when the op needs one. Returns the result; the caller
    asserts on any exception. (No network: live ops without creds refuse before
    any request is made.)"""
    clone_ref = "staging-ref"
    try:
        if adapter.supports("clone_agent"):
            clone_ref = adapter.clone_agent("asst_1", name="staging")
    except adapters.CapabilityError:
        clone_ref = "staging-ref"  # live adapter without creds refuses; fine
    scenario = {"id": "s1", "caller_onset_sec": 2.0}
    dispatch = {
        "snapshot_config": lambda: adapter.snapshot_config({"turn_taking": {"x": 1}}),
        "inspect_config": lambda: adapter.inspect_config("asst_1"),
        "clone_agent": lambda: adapter.clone_agent("asst_1", name="staging"),
        "apply_variant": lambda: adapter.apply_variant(
            clone_ref, {"config_delta": {"interrupt_min_words": 1}}),
        "run_scenario": lambda: adapter.run_scenario(clone_ref, scenario),
        "capture_result": lambda: adapter.capture_result(clone_ref, scenario),
        "rollback": lambda: adapter.rollback("asst_1", "rev-1"),
        "delete_clone": lambda: adapter.delete_clone(clone_ref),
    }
    return dispatch[cap]()


def _all_adapters(tmp_path):
    return {
        "vapi": adapters.get_adapter("vapi"),        # no creds
        "retell": adapters.get_adapter("retell"),    # no creds
        "livekit": adapters.get_adapter("livekit"),
        "pipecat": adapters.get_adapter("pipecat"),
        "mock": adapters.get_adapter("mock", work_dir=str(tmp_path / "work")),
    }


def test_no_adapter_advertises_a_capability_that_raises_notimplemented(tmp_path):
    # THE regression: every ADVERTISED operation must actually be implemented --
    # invoking it returns a result or refuses for missing credentials
    # (CapabilityError), but NEVER raises NotImplementedError. Covers every
    # adapter and every operation-backed capability it advertises.
    for stack, adapter in _all_adapters(tmp_path).items():
        for cap in sorted(adapter.capabilities()):
            if cap not in adapters._OPERATION_METHODS:
                continue  # feature capability: no adapter method to invoke
            try:
                result = _invoke_op(adapter, cap)
            except adapters.CapabilityError:
                pass  # implemented-but-needs-credentials surfaced as a refusal
            except NotImplementedError as exc:  # pragma: no cover - the bug
                pytest.fail(
                    f"{stack} advertises {cap!r} but invoking it raises "
                    f"NotImplementedError: {exc}")
            else:
                assert result is not None


def test_describe_available_implies_not_a_stub(tmp_path):
    # available=True must never be reported for an operation whose method is only
    # an @_unimplemented stub, and capabilities() must equal the available subset.
    for adapter in _all_adapters(tmp_path).values():
        desc = adapter.describe()
        assert adapter.capabilities() == {c for c, r in desc.items() if r["available"]}
        for cap, rec in desc.items():
            method = adapters._OPERATION_METHODS.get(cap)
            if method is None:
                continue
            fn = getattr(type(adapter), method)
            is_stub = getattr(fn, "_hotato_unimplemented", False)
            assert rec["available"] is (not is_stub)
            if is_stub:
                assert not adapter.supports(cap)


def test_live_and_source_adapters_do_not_advertise_scenario_ops(tmp_path):
    # DRIVE-A-CALL: run_scenario is now IMPLEMENTED where the provider has a
    # confirmed create-call API -- Vapi (POST /call) and Twilio (Calls.json). Both
    # advertise it (available=True) but, without credentials, describe() reports
    # authorized=False and invoking raises CapabilityError (never places a call).
    for stack in ("vapi", "twilio"):
        adapter = adapters.get_adapter(stack)
        assert adapter.supports("run_scenario")
        assert "run_scenario" in adapter.capabilities()
        assert adapter.describe()["run_scenario"]["available"] is True
        assert adapter.describe()["run_scenario"]["authorized"] is False
    # Retell (no confirmed create-call API) and the capture-in-your-infra source
    # adapters stay honestly unadvertised for run_scenario -- invoking raises.
    for stack in ("retell", "livekit", "pipecat"):
        adapter = adapters.get_adapter(stack)
        assert not adapter.supports("run_scenario")
        assert "run_scenario" not in adapter.capabilities()
        assert adapter.describe()["run_scenario"]["available"] is False
    v = adapters.get_adapter("vapi")
    assert v.supports("capture_result")  # implemented; describe() authorized=False w/o a key
    assert v.describe()["capture_result"]["available"] is True
    for stack in ("retell", "livekit", "pipecat"):
        assert not adapters.get_adapter(stack).supports("capture_result")


def test_describe_distinguishes_credentials_from_unimplemented():
    # HONEST discovery contract: implemented-but-needs-credentials
    # (available=True, authorized=False) is distinct from not-implemented
    # (available=False), and neither answer crashes.
    v = adapters.get_adapter("vapi")  # no creds
    d = v.describe()
    assert d["clone_agent"] == {
        "available": True, "authorized": False,
        "reason": "implemented; requires credentials (connect a stack and supply an API key)"}
    assert d["snapshot_config"] == {"available": True, "authorized": True, "reason": "ready"}
    # run_scenario is now IMPLEMENTED for Vapi (drive-a-call) -- available=True,
    # authorized=False without a key (the credentialed distinction, not unimplemented).
    assert d["run_scenario"]["available"] is True
    assert d["run_scenario"]["authorized"] is False
    # supplying a key flips authorization on the implemented op (still no crash)
    vk = adapters.get_adapter("vapi", api_key="sk-test-key")
    assert vk.describe()["clone_agent"]["authorized"] is True


def test_mock_reports_full_loop_available_and_every_op_returns(tmp_path):
    # The mock DOES implement the whole loop, so it honestly advertises the full
    # capability set and every operation-backed capability returns a real result.
    mock = adapters.get_adapter("mock", work_dir=str(tmp_path / "m"))
    assert mock.capabilities() == set(adapters.CAPABILITIES)
    for cap in sorted(adapters.CAPABILITIES):
        rec = mock.describe()[cap]
        assert rec["available"] is True and rec["authorized"] is True
        if cap in adapters._OPERATION_METHODS:
            assert _invoke_op(mock, cap) is not None
