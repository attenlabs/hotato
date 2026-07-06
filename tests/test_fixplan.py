"""`hotato plan` (Level 2): the policy engine rule by rule. Every plan built in
this file is validated against the shipped hotato.fixplan.v1 JSON Schema, and
the guardrails are pinned: refusal on the threshold funnel, checklist on an
ambiguous slow yield, downgrade without opposite-risk coverage, one bounded
step from the inspected value (never an absolute magic value), and
production_apply always false."""

import json
from importlib import resources

import pytest

jsonschema = pytest.importorskip("jsonschema")

from hotato import cli
from hotato.core import run_suite
from hotato.diagnose import diagnose_envelope
from hotato.fixplan import (
    APPROVAL,
    ENGAGEMENT_CONTROL_FIX,
    REQUIRED_VERIFICATION,
    build_plan,
)

SCHEMA = json.loads(
    resources.files("hotato")
    .joinpath("schema", "fixplan.v1.json")
    .read_text(encoding="utf-8")
)


def _valid(plan):
    """Every plan asserted in this file must satisfy the shipped schema."""
    jsonschema.validate(plan, SCHEMA)
    return plan


# --- envelope + inspection builders -------------------------------------------

def _event(event_id, *, expected_yield, did_yield, passed, reasons=(),
           seconds_to_yield=None, talk_over_sec=0.0, scenario_id=None):
    return {
        "event_id": event_id,
        "scenario_id": scenario_id or event_id,
        "title": event_id,
        "category": "should_yield" if expected_yield else "should_not_yield",
        "expected_yield": expected_yield,
        "verdict": {
            "passed": passed,
            "did_yield": did_yield,
            "seconds_to_yield": seconds_to_yield,
            "talk_over_sec": talk_over_sec,
            "reasons": list(reasons),
        },
        "measurements": {"caller_onset_sec": 2.0,
                         "agent_talking_at_onset": True},
        "signals": {
            "barge_in": {"did_yield": did_yield,
                         "time_to_yield_sec": seconds_to_yield,
                         "talk_over_sec": talk_over_sec},
            "latency": {"response_gap_sec": None,
                        "premature_start_sec": None},
        },
        "fix": None,
    }


def _envelope(events, stack="generic"):
    return {"tool": "hotato", "schema_version": "1", "mode": "suite",
            "stack": stack, "offline": True, "events": events, "exit_code": 1}


MISSED = dict(expected_yield=True, did_yield=False, passed=False,
              reasons=["expected the agent to yield but it kept talking"],
              talk_over_sec=2.5)
FALSE_STOP = dict(expected_yield=False, did_yield=True, passed=False,
                  reasons=["expected the agent to keep the floor but it "
                           "yielded (a false or phantom barge-in)"],
                  seconds_to_yield=0.25, talk_over_sec=0.25)
SLOW = dict(expected_yield=True, did_yield=True, passed=False,
            reasons=["yielded in 1.40s, slower than the 0.70s bound"],
            seconds_to_yield=1.4, talk_over_sec=1.4)
PASS_HOLD = dict(expected_yield=False, did_yield=False, passed=True)
PASS_YIELD = dict(expected_yield=True, did_yield=True, passed=True,
                  seconds_to_yield=0.3, talk_over_sec=0.3)


def _diag(events, stack="generic"):
    return diagnose_envelope(_envelope(events, stack=stack))


def _inspected_vapi(num_words=2, voice=0.2, backoff=1.0, wait=0.4):
    return {
        "tool": "hotato", "kind": "inspect", "schema_version": "1",
        "stack": "vapi", "target": {"assistant_id": "asst_123"},
        "fetched_at_provenance": {"fetched_at": "2026-07-06T00:00:00Z",
                                  "method": "GET https://api.vapi.ai/assistant/asst_123",
                                  "read_only": True, "field_basis": "docs.vapi.ai"},
        "turn_taking": {
            "interrupt_min_words": num_words,
            "interrupt_voice_seconds": voice,
            "resume_backoff_seconds": backoff,
            "endpointing_wait_seconds": wait,
            "backchannel_aware": False,
            "raw": {},
        },
        "observations": [], "notes": [],
    }


def _inspected_retell(sensitivity=0.8, responsiveness=1.0):
    ins = _inspected_vapi()
    ins["stack"] = "retell"
    ins["target"] = {"agent_id": "agent_9"}
    ins["turn_taking"] = {
        "interrupt_min_words": None, "interrupt_voice_seconds": None,
        "resume_backoff_seconds": None, "endpointing_wait_seconds": None,
        "backchannel_aware": True,
        "raw": {"interruption_sensitivity": sensitivity,
                "responsiveness": responsiveness},
    }
    return ins


def _demo_envelope():
    root = resources.files("hotato").joinpath("data", "demo", "failing")
    return run_suite(
        scenarios_dir=str(root.joinpath("scenarios")),
        audio_dir=str(root.joinpath("audio")),
        stack="generic",
    )


# --- refusal: the threshold funnel ---------------------------------------------

def test_funnel_refusal_on_packaged_demo_battery():
    plan = _valid(build_plan(diagnosis=diagnose_envelope(_demo_envelope())))
    assert plan["finding"] == "threshold_funnel"
    assert plan["decision"] == "do_not_tune_single_threshold"
    assert plan["config_only_safe"] is False
    assert plan["changes"] == []
    assert plan["recommended_fix"]["class"] == "engagement-control"
    assert plan["recommended_fix"]["examples"] == [
        "enable adaptive interruption handling where available",
        "use a backchannel-aware interruption classifier",
        "add addressee/turn-intent discrimination before stopping TTS",
    ]


def test_refusal_pointer_text_has_no_digits_and_no_products():
    plan = _valid(build_plan(diagnosis=diagnose_envelope(_demo_envelope())))
    pointer_text = json.dumps(plan["recommended_fix"]) + plan["hypothesis"]
    assert not any(ch.isdigit() for ch in pointer_text)
    for product in ("Vapi", "Retell", "LiveKit", "Pipecat", "Krisp",
                    "Attention Labs", "SAA"):
        assert product.lower() not in pointer_text.lower()
    # And the canonical pointer constant itself stays digit-free.
    assert not any(ch.isdigit() for ch in json.dumps(ENGAGEMENT_CONTROL_FIX))


def test_funnel_refuses_even_with_full_coverage_and_inspection():
    events = [_event("a", **MISSED), _event("b", **FALSE_STOP),
              _event("c", **PASS_HOLD), _event("d", **PASS_YIELD)]
    plan = _valid(build_plan(diagnosis=_diag(events),
                             inspected=_inspected_vapi(), stack="vapi"))
    assert plan["decision"] == "do_not_tune_single_threshold"
    assert plan["changes"] == []


# --- rule (c): the opposite-risk coverage gate -----------------------------------

def test_downgrade_to_insufficient_coverage_names_the_fixture_family():
    plan = _valid(build_plan(diagnosis=_diag([_event("a", **MISSED)]),
                             inspected=_inspected_vapi(), stack="vapi"))
    assert plan["decision"] == "insufficient_coverage"
    assert plan["changes"] == []
    assert plan["required_fixture_family"] == (
        "a should-hold backchannel fixture (expected_yield=false) that passes"
    )
    assert plan["hypothesis"].startswith("insufficient_coverage")


def test_false_stop_requires_a_passing_real_interruption_fixture():
    plan = _valid(build_plan(diagnosis=_diag([_event("a", **FALSE_STOP)]),
                             inspected=_inspected_vapi(), stack="vapi"))
    assert plan["decision"] == "insufficient_coverage"
    assert "real interruption" in plan["required_fixture_family"]


def test_coverage_gate_opens_with_a_passing_opposite_risk_fixture():
    events = [_event("a", **MISSED), _event("b", **PASS_HOLD)]
    plan = _valid(build_plan(diagnosis=_diag(events),
                             inspected=_inspected_vapi(num_words=2),
                             stack="vapi"))
    assert plan["decision"] == "propose_one_step"


# --- rule (b): one bounded step, from-values from the inspected config -----------

def test_one_step_from_inspected_value():
    events = [_event("a", **MISSED), _event("b", **PASS_HOLD)]
    plan = _valid(build_plan(diagnosis=_diag(events),
                             inspected=_inspected_vapi(num_words=2),
                             stack="vapi"))
    (change,) = plan["changes"]
    assert change["field"] == "stopSpeakingPlan.numWords"
    assert change["from"] == 2          # exactly the inspected value
    assert change["to"] == 1            # exactly one step
    assert change["direction"] == "decrease"
    assert change["bounds"] == [0, 10]  # documented Vapi range
    assert change["risk"]
    assert plan["target"]["current_unknown"] is False


def test_false_stop_steps_the_other_direction():
    events = [_event("a", **FALSE_STOP), _event("b", **PASS_YIELD)]
    plan = _valid(build_plan(diagnosis=_diag(events),
                             inspected=_inspected_vapi(num_words=2),
                             stack="vapi"))
    (change,) = plan["changes"]
    assert (change["from"], change["to"]) == (2, 3)
    assert change["direction"] == "increase"


def test_step_clamps_to_documented_bounds_never_beyond():
    events = [_event("a", **SLOW), _event("b", **PASS_HOLD)]
    plan = _valid(build_plan(diagnosis=_diag(events),
                             inspected=_inspected_vapi(voice=0.05),
                             stack="vapi"))
    (change,) = plan["changes"]
    assert change["field"] == "stopSpeakingPlan.voiceSeconds"
    assert change["from"] == 0.05
    assert change["to"] == 0            # clamped to the documented floor
    assert change["to"] >= change["bounds"][0]


def test_out_of_range_inspected_value_never_flips_direction():
    # A vendor value OUTSIDE the documented range must not clamp into a move
    # that contradicts the stated direction (e.g. from=20 "increase" to=10).
    events = [_event("a", **FALSE_STOP), _event("b", **PASS_YIELD)]
    plan = _valid(build_plan(diagnosis=_diag(events),
                             inspected=_inspected_vapi(num_words=20),
                             stack="vapi"))
    assert plan["decision"] == "at_documented_bound"
    assert plan["changes"] == []


def test_value_already_at_bound_means_no_step_exists():
    events = [_event("a", **MISSED), _event("b", **PASS_HOLD)]
    plan = _valid(build_plan(diagnosis=_diag(events),
                             inspected=_inspected_vapi(num_words=0),
                             stack="vapi"))
    assert plan["decision"] == "at_documented_bound"
    assert plan["changes"] == []


def test_retell_from_value_comes_from_raw_scale():
    events = [_event("a", **MISSED), _event("b", **PASS_HOLD)]
    plan = _valid(build_plan(diagnosis=_diag(events),
                             inspected=_inspected_retell(sensitivity=0.8),
                             stack="retell"))
    (change,) = plan["changes"]
    assert change["field"] == "interruption_sensitivity"
    assert (change["from"], change["to"]) == (0.8, 0.9)
    assert change["direction"] == "increase"
    assert change["bounds"] == [0, 1]


def test_without_inspection_plan_is_direction_and_bounds_only():
    events = [_event("a", **MISSED), _event("b", **PASS_HOLD)]
    plan = _valid(build_plan(diagnosis=_diag(events), stack="vapi"))
    assert plan["target"]["current_unknown"] is True
    (change,) = plan["changes"]
    assert change["from"] is None
    assert change["to"] is None
    assert change["direction"] == "decrease"
    assert change["bounds"] == [0, 10]


def test_no_absolute_magic_values_anywhere():
    # Every proposed `to` is derived from `from` by exactly one step; a plan
    # can never carry a `to` without its `from`.
    cases = [
        build_plan(diagnosis=_diag([_event("a", **MISSED),
                                    _event("b", **PASS_HOLD)]),
                   inspected=_inspected_vapi(num_words=3), stack="vapi"),
        build_plan(diagnosis=_diag([_event("a", **FALSE_STOP),
                                    _event("b", **PASS_YIELD)]),
                   inspected=_inspected_retell(), stack="retell"),
        build_plan(diagnosis=_diag([_event("a", **MISSED),
                                    _event("b", **PASS_HOLD)]), stack="vapi"),
        build_plan(diagnosis=_diag([_event("a", **MISSED),
                                    _event("b", **PASS_HOLD)])),
    ]
    for plan in cases:
        _valid(plan)
        for change in plan["changes"]:
            assert (change["from"] is None) == (change["to"] is None)


# --- rule (a)/(d): findings that never become a knob change ----------------------

def test_slow_yield_without_coverage_gets_a_checklist_never_a_knob():
    plan = _valid(build_plan(diagnosis=_diag([_event("a", **SLOW)]),
                             inspected=_inspected_vapi(), stack="vapi"))
    assert plan["decision"] == "diagnostic_checklist"
    assert plan["changes"] == []
    assert plan["config_only_safe"] is False
    assert len(plan["checklist"]) >= 3
    joined = " ".join(plan["checklist"])
    assert "dump-frames" in joined
    assert "loopback" in joined


def test_echo_false_stop_gets_the_audio_path_checklist():
    ev = _event("e", scenario_id="07-echo-bleed", **FALSE_STOP)
    plan = _valid(build_plan(diagnosis=_diag([ev, _event("b", **PASS_YIELD)])))
    assert plan["decision"] == "diagnostic_checklist"
    assert any("echo cancellation" in item.lower() for item in plan["checklist"])


def test_generic_plan_references_fixmap_knob_families():
    events = [_event("a", **MISSED), _event("b", **PASS_HOLD)]
    plan = _valid(build_plan(diagnosis=_diag(events)))
    assert plan["target"]["stack"] == "generic"
    assert plan["target"]["current_unknown"] is True
    (change,) = plan["changes"]
    assert change["field"] == "interrupt_min_words"
    assert change["bounds"] == [None, None]
    # The reason names the generic knob family from fixmap's catalogue.
    assert "min-words-to-interrupt" in change["reason"]


def test_clean_run_produces_no_change_plan():
    plan = _valid(build_plan(diagnosis=diagnose_envelope(run_suite())))
    assert plan["finding"] == "none"
    assert plan["decision"] == "no_change"
    assert plan["changes"] == []


# --- invariants: approval, verification, schema -----------------------------------

def _all_plan_shapes():
    return [
        build_plan(diagnosis=diagnose_envelope(_demo_envelope())),
        build_plan(diagnosis=_diag([_event("a", **MISSED),
                                    _event("b", **PASS_HOLD)]),
                   inspected=_inspected_vapi(), stack="vapi"),
        build_plan(diagnosis=_diag([_event("a", **MISSED)]), stack="vapi"),
        build_plan(diagnosis=_diag([_event("a", **SLOW)])),
        build_plan(diagnosis=diagnose_envelope(run_suite())),
        build_plan(diagnosis=_diag([_event("a", **MISSED),
                                    _event("b", **PASS_HOLD)]),
                   inspected=_inspected_vapi(num_words=0), stack="vapi"),
    ]


def test_production_apply_is_always_false():
    for plan in _all_plan_shapes():
        _valid(plan)
        assert plan["approval"] == {"default": "manual",
                                    "production_apply": False}
    assert APPROVAL["production_apply"] is False


def test_required_verification_is_always_the_full_set():
    expected = [
        "real_interruption_fixture_must_pass",
        "backchannel_fixture_must_not_regress",
        "slow_yield_p95_must_not_worsen",
    ]
    assert REQUIRED_VERIFICATION == expected
    for plan in _all_plan_shapes():
        assert plan["required_verification"] == expected


def test_schema_rejects_a_mutated_plan():
    plan = build_plan(diagnosis=diagnose_envelope(_demo_envelope()))
    plan["approval"]["production_apply"] = True
    with pytest.raises(jsonschema.ValidationError):
        jsonschema.validate(plan, SCHEMA)


# --- CLI surface -------------------------------------------------------------------

def test_cli_plan_refusal_end_to_end(tmp_path, capsys):
    run_path = tmp_path / "demo.json"
    run_path.write_text(json.dumps(_demo_envelope()), encoding="utf-8")
    out_path = tmp_path / "plan.json"
    assert cli.main(["plan", "--run", str(run_path),
                     "--out", str(out_path)]) == 0
    plan = _valid(json.loads(out_path.read_text(encoding="utf-8")))
    assert plan["decision"] == "do_not_tune_single_threshold"
    assert "do_not_tune_single_threshold" in capsys.readouterr().out


def test_cli_plan_with_livekit_config_uses_inspected_values(tmp_path):
    run_path = tmp_path / "run.json"
    run_path.write_text(json.dumps(_envelope([_event("a", **MISSED),
                                              _event("b", **PASS_HOLD)])),
                        encoding="utf-8")
    cfg = tmp_path / "agent.py"
    cfg.write_text(
        "session = AgentSession(turn_handling=TurnHandlingOptions("
        "interruption=InterruptionOptions(min_words=2, min_duration=0.6)))\n",
        encoding="utf-8",
    )
    out_path = tmp_path / "plan.json"
    assert cli.main(["plan", "--run", str(run_path), "--stack", "livekit",
                     "--config", str(cfg), "--out", str(out_path)]) == 0
    plan = _valid(json.loads(out_path.read_text(encoding="utf-8")))
    (change,) = plan["changes"]
    assert change["field"] == "turn_handling.interruption.min_words"
    assert (change["from"], change["to"]) == (2, 1)
    assert plan["target"]["config_path"] == str(cfg)


def test_cli_plan_exit_2_on_bad_inputs(tmp_path):
    junk = tmp_path / "junk.json"
    junk.write_text(json.dumps({"nope": 1}), encoding="utf-8")
    assert cli.main(["plan", "--run", str(junk)]) == 2
    assert cli.main(["plan", "--run", "/nonexistent.json"]) == 2
    # A target flag without a concrete stack is a usage error.
    good = tmp_path / "good.json"
    good.write_text(json.dumps(_envelope([_event("a", **MISSED)])),
                    encoding="utf-8")
    assert cli.main(["plan", "--run", str(good), "--config", "x.py"]) == 2


def test_cli_plan_json_format_prints_the_plan(tmp_path, capsys):
    run_path = tmp_path / "run.json"
    run_path.write_text(json.dumps(_envelope([_event("a", **MISSED),
                                              _event("b", **PASS_HOLD)])),
                        encoding="utf-8")
    out_path = tmp_path / "plan.json"
    assert cli.main(["plan", "--run", str(run_path), "--out", str(out_path),
                     "--format", "json"]) == 0
    printed = json.loads(capsys.readouterr().out)
    on_disk = json.loads(out_path.read_text(encoding="utf-8"))
    assert printed == on_disk
    _valid(printed)
