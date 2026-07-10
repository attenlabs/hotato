"""``hotato explain`` (S3, reliability loop): root-cause-by-layer attribution
composed from ``diagnose`` + the same policy gate ``plan`` enforces -- no new
scoring engine. Pinned here: attribution on clear cases (a mapped knob with a
passing opposite-risk fixture, the threshold funnel, a contract's own
policy-bound comparison), REFUSAL on genuinely ambiguous or insufficient
evidence (echo, an ambiguous slow yield, a not-scorable event, an unlabeled
sweep candidate, a contract false-stop with no disambiguating candidate_kind),
and explicit unknowns whenever no voice trace is attached."""

import json
from importlib import resources

import pytest

from hotato import cli
from hotato import explain as ex
from hotato.core import run_suite

HARD = str(resources.files("hotato").joinpath(
    "data", "audio", "01-hard-interruption.example.wav"))          # yields at 2.40
BACKCHANNEL = str(resources.files("hotato").joinpath(
    "data", "audio", "02-backchannel-mhm.example.wav"))            # holds at 2.10


# --- run-envelope helpers (same shapes as test_diagnose.py) -----------------

def _event(event_id, *, expected_yield, did_yield, passed, reasons=(),
           seconds_to_yield=None, talk_over_sec=0.0, scenario_id=None,
           scorable=None, not_scorable_reason=None, response_gap_sec=None,
           premature_start_sec=None):
    ev = {
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
        "measurements": {
            "caller_onset_sec": 2.0,
            "agent_talking_at_onset": True,
            "hop_sec": 0.01,
            "notes": "",
        },
        "signals": {
            "barge_in": {"did_yield": did_yield,
                         "time_to_yield_sec": seconds_to_yield,
                         "talk_over_sec": talk_over_sec},
            "latency": {"response_gap_sec": response_gap_sec,
                        "premature_start_sec": premature_start_sec},
        },
        "fix": None,
    }
    if scorable is False:
        ev["scorable"] = False
        ev["not_scorable_reason"] = not_scorable_reason or "input problem"
    return ev


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
TALK_OVER = dict(expected_yield=True, did_yield=True, passed=False,
                 reasons=["talked over the caller for 1.20s, more than the "
                          "0.50s bound"],
                 seconds_to_yield=1.2, talk_over_sec=1.2)
PASS_HOLD = dict(expected_yield=False, did_yield=False, passed=True)
PASS_YIELD = dict(expected_yield=True, did_yield=True, passed=True,
                  seconds_to_yield=0.3, talk_over_sec=0.3)


def _write(tmp_path, name, payload):
    p = tmp_path / name
    p.write_text(json.dumps(payload), encoding="utf-8")
    return str(p)


def _attr(explanation, type_):
    matches = [a for a in explanation["attributions"] if a["type"] == type_]
    assert len(matches) == 1, f"expected one {type_} attribution"
    return matches[0]


# --- attribution on clear cases ----------------------------------------------

def test_missed_real_interruption_with_coverage_is_safe_to_patch(tmp_path):
    path = _write(tmp_path, "r.json",
                  _envelope([_event("a", **MISSED), _event("b", **PASS_HOLD)]))
    result = ex.explain(path)
    a = _attr(result, "missed_real_interruption")
    assert a["failure_layer"] == "turn_taking"
    assert a["turn_taking_layer"] == "interruption_detection"
    assert a["confidence"] == "high"
    assert a["fixability"] == "safe_to_patch"
    assert a["opposite_risk"]
    assert a["evidence_for"]
    assert result["refusals"] == []
    assert "hotato plan" in result["safe_next_action"]


def test_excess_talk_over_with_coverage_is_safe_to_patch(tmp_path):
    path = _write(tmp_path, "r.json",
                  _envelope([_event("a", **TALK_OVER), _event("b", **PASS_HOLD)]))
    result = ex.explain(path)
    a = _attr(result, "excess_talk_over")
    assert a["fixability"] == "safe_to_patch"
    assert a["turn_taking_layer"] == "interruption_detection"


def test_endpointing_miss_with_coverage_is_safe_to_patch(tmp_path):
    ev = _event("a", expected_yield=True, did_yield=True, passed=False,
                reasons=["response gap 1.80s exceeded the 1.00s bound"],
                seconds_to_yield=0.3, talk_over_sec=0.3, response_gap_sec=1.8)
    covering = _event("b", expected_yield=True, did_yield=True, passed=True,
                      seconds_to_yield=0.3, talk_over_sec=0.3,
                      response_gap_sec=0.4)
    path = _write(tmp_path, "r.json", _envelope([ev, covering]))
    result = ex.explain(path)
    a = _attr(result, "endpointing_miss")
    assert a["turn_taking_layer"] == "endpointing"
    assert a["fixability"] == "safe_to_patch"


def test_missing_opposite_risk_coverage_is_insufficient_evidence(tmp_path):
    path = _write(tmp_path, "r.json", _envelope([_event("a", **MISSED)]))
    result = ex.explain(path)
    a = _attr(result, "missed_real_interruption")
    assert a["fixability"] == "insufficient_evidence"
    assert a["confidence"] == "medium"
    assert any("no passing" in x for x in a["evidence_against"])


def test_threshold_funnel_is_a_composite_do_not_patch_attribution(tmp_path):
    root = resources.files("hotato").joinpath("data", "demo", "failing")
    env = run_suite(scenarios_dir=str(root.joinpath("scenarios")),
                    audio_dir=str(root.joinpath("audio")))
    path = _write(tmp_path, "r.json", env)
    result = ex.explain(path)
    funnel = _attr(result, "threshold_funnel")
    assert funnel["fixability"] == "do_not_patch"
    assert funnel["event_id"] is None
    # both contributing events are ALSO attributed, do_not_patch, not refused.
    per_event = {a["type"] for a in result["attributions"]
                if a["event_id"] is not None}
    assert per_event == {"missed_real_interruption", "false_stop_on_backchannel"}
    for a in result["attributions"]:
        if a["event_id"] is not None:
            assert a["fixability"] == "do_not_patch"
    assert result["refusals"] == []
    assert "engagement-control" in result["safe_next_action"]
    assert result["battery"]["decision"] == "do_not_tune_single_threshold"


# --- refusal on ambiguous / insufficient evidence -----------------------------

def test_echo_false_stop_is_refused_not_guessed(tmp_path):
    ev = _event("echo-1", scenario_id="07-echo-bleed", **FALSE_STOP)
    path = _write(tmp_path, "r.json", _envelope([ev]))
    result = ex.explain(path)
    assert result["attributions"] == []
    (r,) = result["refusals"]
    assert r["event_id"] == "echo-1"
    assert "indistinguishable" in r["reason"]
    assert "audio" in r["safe_next_action"].lower() or "tts" in r["safe_next_action"].lower()


def test_ambiguous_slow_yield_is_refused(tmp_path):
    ev = _event("slow-1", expected_yield=True, did_yield=True, passed=False,
                reasons=["yielded in 1.40s, slower than the 0.70s bound"],
                seconds_to_yield=1.4, talk_over_sec=1.4)
    path = _write(tmp_path, "r.json", _envelope([ev]))
    result = ex.explain(path)
    assert result["attributions"] == []
    (r,) = result["refusals"]
    assert r["event_id"] == "slow-1"
    assert "indistinguishable" in r["reason"]


def test_not_scorable_event_is_refused_never_an_attribution(tmp_path):
    bad = _event("silent-agent", **MISSED, scorable=False,
                 not_scorable_reason="the agent was not talking at onset")
    path = _write(tmp_path, "r.json", _envelope([bad, _event("b", **PASS_HOLD)]))
    result = ex.explain(path)
    assert all(a["event_id"] != "silent-agent" for a in result["attributions"])
    (r,) = [x for x in result["refusals"] if x["event_id"] == "silent-agent"]
    assert "never an agent failure" in r["reason"] or "input problem" in r["reason"]


def test_sweep_candidate_ref_is_always_refused(tmp_path):
    doc = {
        "tool": "hotato", "kind": "analyze", "stack": "generic",
        "candidates": [{
            "source": "calls/call_abc123.wav", "kind": "agent_stop_no_caller",
            "t_sec": 5.0, "salience": 1.0,
            "durations": {"trailing_silence_sec": 0.46,
                         "caller_proximity_sec": 0.5},
            "agent_reaction": {},
        }],
        "total_candidates": 1,
    }
    path = _write(tmp_path, "sweep.json", doc)
    result = ex.explain(f"{path}#1")
    assert result["input_kind"] == "sweep_candidate"
    assert result["attributions"] == []
    (r,) = result["refusals"]
    assert "no human label" in r["reason"]
    assert "fixture promote" in r["safe_next_action"]
    assert "--expect yield" in r["safe_next_action"]
    assert "--expect hold" in r["safe_next_action"]


def test_contract_false_stop_without_candidate_kind_is_refused(tmp_path):
    rc = cli.main([
        "contract", "create", "--stereo", HARD, "--id", "fs-1",
        "--onset", "2.40", "--expect", "hold", "--out", str(tmp_path),
    ])
    assert rc == 0
    result = ex.explain(str(tmp_path / "fs-1.hotato"))
    assert result["attributions"] == []
    (r,) = result["refusals"]
    assert "cannot support one root cause" in r["reason"]


def test_contract_false_stop_with_echo_candidate_kind_is_refused_as_echo(tmp_path):
    rc = cli.main([
        "contract", "create", "--stereo", HARD, "--id", "fs-echo",
        "--onset", "2.40", "--expect", "hold", "--out", str(tmp_path),
    ])
    assert rc == 0
    cpath = tmp_path / "fs-echo.hotato" / "contract.json"
    c = json.loads(cpath.read_text(encoding="utf-8"))
    c["source"]["candidate_kind"] = "echo_correlated_activity"
    cpath.write_text(json.dumps(c), encoding="utf-8")
    result = ex.explain(str(tmp_path / "fs-echo.hotato"))
    (r,) = result["refusals"]
    assert "echo" in r["reason"].lower()


# --- contract-bundle attribution via the SAME policy bound comparison --------

def test_contract_missed_interruption_is_attributed(tmp_path):
    rc = cli.main([
        "contract", "create", "--stereo", BACKCHANNEL, "--id", "missed-1",
        "--onset", "2.10", "--expect", "yield", "--out", str(tmp_path),
    ])
    assert rc == 0
    result = ex.explain(str(tmp_path / "missed-1.hotato"))
    a = _attr(result, "missed_real_interruption")
    assert a["turn_taking_layer"] == "interruption_detection"


def test_contract_slow_yield_from_policy_bound_comparison(tmp_path):
    rc = cli.main([
        "contract", "create", "--stereo", HARD, "--id", "slow-1",
        "--onset", "2.40", "--expect", "yield", "--out", str(tmp_path),
        "--max-time-to-yield", "0.01",
    ])
    assert rc == 0
    result = ex.explain(str(tmp_path / "slow-1.hotato"))
    a = _attr(result, "slow_yield")
    assert "exceeds policy" in a["evidence_for"][0]


def test_contract_passing_has_no_attribution_or_refusal(tmp_path):
    rc = cli.main([
        "contract", "create", "--stereo", HARD, "--id", "pass-1",
        "--onset", "2.40", "--expect", "yield", "--out", str(tmp_path),
    ])
    assert rc == 0
    result = ex.explain(str(tmp_path / "pass-1.hotato"))
    assert result["attributions"] == []
    assert result["refusals"] == []
    assert result["battery"]["decision"] == "no_failures"


def test_contract_not_scorable_is_refused(tmp_path):
    bundle = tmp_path / "bad-1.hotato"
    (bundle / "traces").mkdir(parents=True)
    contract = {
        "schema": "hotato.contract.v1", "id": "bad-1",
        "created_at": "2026-01-01T00:00:00Z", "created_by": "test",
        "kind": "voice-turn-taking-contract",
        "label": {"expected_behavior": "yield", "label_source": "human"},
        "source": {"stack": "generic", "recording_type": "stereo",
                   "channels": 2, "source_audio_sha256": "x"},
        "event": {"onset_sec": 1.0, "clipped": True},
        "measurement": {"scorable": False,
                        "not_scorable_reason": "agent not talking at onset",
                        "did_yield": None, "seconds_to_yield": None,
                        "talk_over_sec": None, "passed": None,
                        "indicative_only": False, "diarization": None},
        "trust": {"status": "safe to scan", "scorable": True, "warnings": []},
        "policy": {"pass_conditions": {"yield": True}},
        "replay": {"command": "x", "ci_command": "x"},
        "bundle": {"paths": {}},
    }
    (bundle / "contract.json").write_text(json.dumps(contract), encoding="utf-8")
    result = ex.explain(str(bundle))
    assert result["attributions"] == []
    (r,) = result["refusals"]
    assert "not scorable" in r["reason"]
    assert "agent not talking at onset" in r["reason"]


# --- unknowns populated when traces are absent (or present) ------------------

def test_run_envelope_always_notes_no_trace(tmp_path):
    path = _write(tmp_path, "r.json", _envelope([_event("b", **PASS_HOLD)]))
    result = ex.explain(path)
    assert any("playout trace" in u for u in result["unknowns"])


def test_contract_without_trace_notes_it_in_unknowns(tmp_path):
    rc = cli.main([
        "contract", "create", "--stereo", HARD, "--id", "notrace-1",
        "--onset", "2.40", "--expect", "yield", "--out", str(tmp_path),
    ])
    assert rc == 0
    result = ex.explain(str(tmp_path / "notrace-1.hotato"))
    assert any("no client-side playout trace attached" in u
              for u in result["unknowns"])


def test_sweep_candidate_notes_no_human_label_in_unknowns(tmp_path):
    doc = {
        "tool": "hotato", "kind": "analyze", "stack": "generic",
        "candidates": [{
            "source": "calls/call_x.wav", "kind": "long_response_gap",
            "t_sec": 3.0, "salience": 1.0,
            "durations": {"gap_sec": 2.0}, "agent_reaction": {},
        }],
        "total_candidates": 1,
    }
    path = _write(tmp_path, "sweep.json", doc)
    result = ex.explain(f"{path}#1")
    assert any("no human label" in u for u in result["unknowns"])


# --- no failures / clean run --------------------------------------------------

def test_no_failures_has_nothing_to_explain(tmp_path):
    path = _write(tmp_path, "r.json", _envelope([_event("b", **PASS_HOLD)]))
    result = ex.explain(path)
    assert result["attributions"] == []
    assert result["refusals"] == []
    assert "nothing to fix" in result["safe_next_action"]


# --- input rejection: same envelope-shape contract as diagnose/plan ----------

def test_rejects_non_envelope_json(tmp_path):
    junk = _write(tmp_path, "junk.json", {"nope": 1})
    with pytest.raises(ValueError):
        ex.explain(junk)


def test_missing_file_raises_oserror():
    with pytest.raises(OSError):
        ex.explain("/definitely/does/not/exist.json")


def test_empty_source_is_a_usage_error():
    with pytest.raises(ValueError):
        ex.explain("")


# --- no em/en dashes anywhere in rendered text --------------------------------

def test_render_text_has_no_em_or_en_dashes(tmp_path):
    root = resources.files("hotato").joinpath("data", "demo", "failing")
    env = run_suite(scenarios_dir=str(root.joinpath("scenarios")),
                    audio_dir=str(root.joinpath("audio")))
    path = _write(tmp_path, "r.json", env)
    text = ex.render_text(ex.explain(path))
    assert "–" not in text
    assert "—" not in text


def test_render_html_has_no_em_or_en_dashes(tmp_path):
    root = resources.files("hotato").joinpath("data", "demo", "failing")
    env = run_suite(scenarios_dir=str(root.joinpath("scenarios")),
                    audio_dir=str(root.joinpath("audio")))
    path = _write(tmp_path, "r.json", env)
    html = ex.render_html(ex.explain(path))
    assert "–" not in html
    assert "—" not in html
    assert "<html" in html
