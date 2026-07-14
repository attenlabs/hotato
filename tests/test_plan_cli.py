"""`hotato plan` CLI reconciliation: the positional result argument, the
always-present read-only blocks (kind, platform_mutation, evidence, risks,
next_commands), not-scorable events as input issues, the Twilio rule (never
agent-config advice), and the input rejection contract (non-envelopes, frame
dumps, benchmark results all exit 2)."""

import json
from importlib import resources

import pytest

from hotato import cli
from hotato.core import run_suite
from hotato.diagnose import diagnose_envelope
from hotato.fixplan import build_plan

# --- minimal envelope builders (same shapes as test_fixplan.py) ---------------

def _event(event_id, *, expected_yield, did_yield, passed, reasons=(),
           seconds_to_yield=None, talk_over_sec=0.0):
    return {
        "event_id": event_id,
        "scenario_id": event_id,
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
PASS_HOLD = dict(expected_yield=False, did_yield=False, passed=True)


def _write(tmp_path, name, payload):
    p = tmp_path / name
    p.write_text(json.dumps(payload), encoding="utf-8")
    return str(p)


def _plan_cli(tmp_path, run_payload, *extra, capsys=None):
    run_path = _write(tmp_path, "run.json", run_payload)
    out_path = tmp_path / "plan.json"
    rc = cli.main(["plan", run_path, "--out", str(out_path), *extra])
    plan = (json.loads(out_path.read_text(encoding="utf-8"))
            if out_path.exists() else None)
    return rc, plan


# --- positional argument -------------------------------------------------------

def test_positional_result_json_is_accepted(tmp_path):
    rc, plan = _plan_cli(tmp_path,
                         _envelope([_event("a", **MISSED),
                                    _event("b", **PASS_HOLD)]))
    assert rc == 0
    assert plan["decision"] == "propose_one_step"


def test_two_different_result_files_is_a_usage_error(tmp_path):
    a = _write(tmp_path, "a.json", _envelope([_event("a", **MISSED)]))
    b = _write(tmp_path, "b.json", _envelope([_event("a", **MISSED)]))
    assert cli.main(["plan", a, "--run", b]) == 2


def test_no_result_file_is_a_usage_error():
    assert cli.main(["plan"]) == 2


# --- input rejection: only run envelopes are accepted ---------------------------

def test_rejects_non_envelope_frame_dump_and_benchmark_results(tmp_path):
    junk = _write(tmp_path, "junk.json", {"nope": 1})
    assert cli.main(["plan", junk]) == 2

    dump = _write(tmp_path, "dump.json",
                  {"tool": "hotato", "kind": "frame-dump", "frames": [],
                   "events": []})
    assert cli.main(["plan", dump]) == 2

    bench = _write(tmp_path, "bench.json",
                   {"tool": "hotato", "kind": "stack-benchmark",
                    "events": [], "scenarios": {}})
    assert cli.main(["plan", bench]) == 2

    cmp_res = _write(tmp_path, "cmp.json",
                     {"tool": "hotato", "kind": "compare", "events": []})
    assert cli.main(["plan", cmp_res]) == 2


# --- the always-present read-only blocks ----------------------------------------

def test_plans_carry_kind_and_platform_mutation_false(tmp_path):
    shapes = [
        _envelope([_event("a", **MISSED), _event("b", **PASS_HOLD)]),
        _envelope([_event("b", **PASS_HOLD)]),
    ]
    for payload in shapes:
        rc, plan = _plan_cli(tmp_path, payload)
        assert rc == 0
        assert plan["kind"] == "fix-plan"
        assert plan["platform_mutation"] == {
            "performed": False, "reason": "hotato plan is read-only"}


def test_propose_one_step_carries_evidence_risks_and_next_commands(tmp_path,
                                                                   capsys):
    rc, plan = _plan_cli(tmp_path,
                         _envelope([_event("a", **MISSED),
                                    _event("b", **PASS_HOLD)]),
                         "--stack", "vapi")
    assert rc == 0
    assert plan["decision"] == "propose_one_step"
    assert plan["config_only_safe"] is True
    assert plan["evidence"], "the measured evidence backs the plan"
    assert plan["evidence"][0]["event_id"] == "a"
    assert plan["evidence"][0]["measured"]["talk_over_sec"] == 2.5
    assert plan["risks"] == [c["risk"] for c in plan["changes"]]
    assert any("hotato compare" in cmd for cmd in plan["next_commands"])
    out = capsys.readouterr().out
    assert "platform mutation: performed=false" in out
    assert "next:" in out


def test_funnel_refusal_still_carries_the_read_only_blocks(tmp_path):
    root = resources.files("hotato").joinpath("data", "demo", "failing")
    env = run_suite(scenarios_dir=str(root.joinpath("scenarios")),
                    audio_dir=str(root.joinpath("audio")))
    rc, plan = _plan_cli(tmp_path, env)
    assert rc == 0
    assert plan["decision"] == "do_not_tune_single_threshold"
    assert plan["platform_mutation"]["performed"] is False
    assert plan["risks"] == [
        "any single-threshold change trades the two failing axes against "
        "each other"]
    assert any("hotato compare" in cmd for cmd in plan["next_commands"])


def test_no_failures_reads_no_fix_needed(tmp_path, capsys):
    rc, plan = _plan_cli(tmp_path, _envelope([_event("b", **PASS_HOLD)]))
    assert rc == 0
    assert plan["decision"] == "no_change"
    assert plan["changes"] == []
    assert "No fix needed" in capsys.readouterr().out


# --- not scorable = input issue, never a fix -------------------------------------

def test_not_scorable_events_are_input_issues_never_fixed(tmp_path, capsys):
    bad = _event("silent-agent", **MISSED)
    bad["scorable"] = False
    bad["not_scorable_reason"] = (
        "the agent was not talking at the caller onset")
    rc, plan = _plan_cli(tmp_path,
                         _envelope([bad, _event("a", **MISSED),
                                    _event("b", **PASS_HOLD)]))
    assert rc == 0
    (issue,) = plan["input_issues"]
    assert issue["event_id"] == "silent-agent"
    assert "never an agent failure" in issue["reason"]
    assert all(e["event_id"] != "silent-agent" for e in plan["evidence"])
    assert plan["decision"] == "propose_one_step"   # driven by "a" alone
    assert "input issue (not an agent failure): silent-agent" in (
        capsys.readouterr().out)


# --- the Twilio rule --------------------------------------------------------------

def test_twilio_never_yields_agent_config_advice(tmp_path, capsys):
    rc, plan = _plan_cli(tmp_path,
                         _envelope([_event("a", **MISSED),
                                    _event("b", **PASS_HOLD)]),
                         "--stack", "twilio")
    assert rc == 0
    assert plan["target"]["stack"] == "twilio"
    assert plan["decision"] == "diagnostic_checklist"
    assert plan["changes"] == []
    joined = " ".join(plan["checklist"]).lower()
    assert "channel assignment" in joined
    assert "upstream voice-agent stack" in joined
    assert "channel assignment" in capsys.readouterr().out.lower()


def test_twilio_rule_applies_from_the_envelope_stack_too(tmp_path):
    rc, plan = _plan_cli(tmp_path,
                         _envelope([_event("a", **MISSED),
                                    _event("b", **PASS_HOLD)],
                                   stack="twilio"))
    assert rc == 0
    assert plan["decision"] == "diagnostic_checklist"
    assert plan["changes"] == []


def test_twilio_with_a_target_flag_is_a_usage_error(tmp_path):
    run_path = _write(tmp_path, "run.json",
                      _envelope([_event("a", **MISSED)]))
    assert cli.main(["plan", run_path, "--stack", "twilio",
                     "--assistant-id", "x"]) == 2


# --- schema: the new keys validate against the shipped schema ---------------------

def test_new_blocks_validate_against_the_shipped_schema(tmp_path):
    jsonschema = pytest.importorskip("jsonschema")
    schema = json.loads(
        resources.files("hotato").joinpath("schema", "fixplan.v1.json")
        .read_text(encoding="utf-8"))
    plans = [
        build_plan(diagnosis=diagnose_envelope(
            _envelope([_event("a", **MISSED), _event("b", **PASS_HOLD)]))),
        build_plan(diagnosis=diagnose_envelope(
            _envelope([_event("a", **MISSED)])), stack="twilio"),
    ]
    for plan in plans:
        jsonschema.validate(plan, schema)
        mutated = json.loads(json.dumps(plan))
        mutated["platform_mutation"]["performed"] = True
        with pytest.raises(jsonschema.ValidationError):
            jsonschema.validate(mutated, schema)
