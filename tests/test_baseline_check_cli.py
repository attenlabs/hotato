"""``hotato baseline check`` (hotato.baseline): the per-dimension timing drift
gate between two saved run envelopes.

Pinned here: within-tolerance exit 0 and beyond-tolerance exit 1 for both
percent ("+10%") and absolute ("+0.05") tolerances; the one-sided gate (an
improvement never drifts); the missing-dimension REFUSAL (exit 2, never a
silent pass); the unknown-dimension / malformed-file / wrong-envelope usage
errors; the stable JSON envelope; and the JUnit render (one testcase per
dimension, a failure child on drift).
"""

from __future__ import annotations

import json
import os

import pytest

from hotato import baseline as B
from hotato import cli


def _envelope(events):
    """A minimal saved run envelope in the `hotato run --format json` shape
    (hotato.core._envelope): tool + schema_version + an events list."""
    return {
        "tool": "hotato",
        "schema_version": "1",
        "mode": "single",
        "stack": "generic",
        "offline": True,
        "summary": {"events": len(events), "passed": len(events),
                    "failed": 0, "regression": False},
        "events": events,
        "exit_code": 0,
    }


def _event(event_id, *, seconds_to_yield=None, talk_over_sec=None,
           response_gap_sec=None, scorable=None):
    e = {
        "event_id": event_id,
        "scenario_id": None,
        "expected_yield": True,
        "verdict": {
            "passed": True,
            "did_yield": seconds_to_yield is not None,
            "seconds_to_yield": seconds_to_yield,
            "talk_over_sec": talk_over_sec,
            "reasons": [],
        },
        "signals": {
            "latency": {
                "response_gap_sec": response_gap_sec,
                "premature_start_sec": None,
            },
        },
    }
    if scorable is not None:
        e["scorable"] = scorable
    return e


def _write(tmp_path, name, obj_or_text):
    p = os.path.join(tmp_path, name)
    with open(p, "w", encoding="utf-8") as fh:
        if isinstance(obj_or_text, str):
            fh.write(obj_or_text)
        else:
            json.dump(obj_or_text, fh)
    return p


def _paths(tmp_path, tolerances_text, baseline_events, candidate_events):
    return (
        _write(tmp_path, "tolerances.yaml", tolerances_text),
        _write(tmp_path, "baseline.json", _envelope(baseline_events)),
        _write(tmp_path, "candidate.json", _envelope(candidate_events)),
    )


# --- within / beyond, absolute tolerance -------------------------------------

def test_within_absolute_tolerance_exits_0(tmp_path, capsys):
    tol, base, cand = _paths(
        tmp_path, 'seconds_to_yield: "+0.05"',
        [_event("a", seconds_to_yield=0.40)],
        [_event("a", seconds_to_yield=0.44)],
    )
    assert cli.main(["baseline", "check", tol, base, cand]) == 0
    out = capsys.readouterr().out
    assert "within" in out
    assert "DRIFT" not in out


def test_beyond_absolute_tolerance_exits_1(tmp_path, capsys):
    tol, base, cand = _paths(
        tmp_path, 'seconds_to_yield: "+0.05"',
        [_event("a", seconds_to_yield=0.40)],
        [_event("a", seconds_to_yield=0.46)],
    )
    assert cli.main(["baseline", "check", tol, base, cand]) == 1
    out = capsys.readouterr().out
    assert "DRIFT" in out
    assert "drift beyond tolerance: seconds_to_yield" in out


def test_exactly_at_the_absolute_bound_is_within(tmp_path):
    tol, base, cand = _paths(
        tmp_path, 'seconds_to_yield: "+0.05"',
        [_event("a", seconds_to_yield=0.40)],
        [_event("a", seconds_to_yield=0.45)],
    )
    assert cli.main(["baseline", "check", tol, base, cand]) == 0


# --- within / beyond, percent tolerance --------------------------------------

def test_within_percent_tolerance_exits_0(tmp_path):
    tol, base, cand = _paths(
        tmp_path, 'response_gap_sec: "+10%"',
        [_event("a", response_gap_sec=0.50)],
        [_event("a", response_gap_sec=0.55)],   # exactly +10% of the baseline
    )
    assert cli.main(["baseline", "check", tol, base, cand]) == 0


def test_beyond_percent_tolerance_exits_1(tmp_path):
    tol, base, cand = _paths(
        tmp_path, 'response_gap_sec: "+10%"',
        [_event("a", response_gap_sec=0.50)],
        [_event("a", response_gap_sec=0.56)],
    )
    assert cli.main(["baseline", "check", tol, base, cand]) == 1


def test_improvement_never_drifts(tmp_path):
    # The gate is one-sided: every dimension is lower-is-better timing, so a
    # DECREASE passes even under a zero tolerance.
    tol, base, cand = _paths(
        tmp_path, 'response_gap_sec: "+0"',
        [_event("a", response_gap_sec=0.50)],
        [_event("a", response_gap_sec=0.30)],
    )
    assert cli.main(["baseline", "check", tol, base, cand]) == 0


def test_pooled_mean_across_events_is_the_gated_value(tmp_path):
    # Baseline mean 0.40 over two events; candidate mean 0.50 over two events:
    # +25% drifts a +10% tolerance even though one candidate event improved.
    tol, base, cand = _paths(
        tmp_path, 'seconds_to_yield: "+10%"',
        [_event("a", seconds_to_yield=0.30),
         _event("b", seconds_to_yield=0.50)],
        [_event("a", seconds_to_yield=0.25),
         _event("b", seconds_to_yield=0.75)],
    )
    assert cli.main(["baseline", "check", tol, base, cand]) == 1


def test_not_scorable_events_never_contribute(tmp_path):
    # The not-scorable candidate event carries a wild value; it is an input
    # problem, excluded exactly as the run envelope's own summary excludes it.
    tol, base, cand = _paths(
        tmp_path, 'seconds_to_yield: "+0.05"',
        [_event("a", seconds_to_yield=0.40)],
        [_event("a", seconds_to_yield=0.41),
         _event("junk", seconds_to_yield=9.0, scorable=False)],
    )
    assert cli.main(["baseline", "check", tol, base, cand]) == 0


# --- refusals and usage errors (exit 2) --------------------------------------

def test_missing_dimension_refuses_exit_2(tmp_path, capsys):
    # talk_over_sec has no measurement on either side: REFUSE, never a
    # silent pass.
    tol, base, cand = _paths(
        tmp_path, 'talk_over_sec: "+0.02"',
        [_event("a", seconds_to_yield=0.40)],
        [_event("a", seconds_to_yield=0.40)],
    )
    assert cli.main(["baseline", "check", tol, base, cand]) == 2
    err = capsys.readouterr().err
    assert "talk_over_sec" in err
    assert "refuses" in err


def test_missing_dimension_on_the_candidate_side_refuses(tmp_path, capsys):
    tol, base, cand = _paths(
        tmp_path, 'response_gap_sec: "+10%"',
        [_event("a", response_gap_sec=0.50)],
        [_event("a", seconds_to_yield=0.40)],
    )
    assert cli.main(["baseline", "check", tol, base, cand]) == 2
    assert "candidate" in capsys.readouterr().err


def test_unknown_dimension_is_a_usage_error(tmp_path, capsys):
    tol, base, cand = _paths(
        tmp_path, 'wpm: "+10%"',
        [_event("a", seconds_to_yield=0.40)],
        [_event("a", seconds_to_yield=0.40)],
    )
    assert cli.main(["baseline", "check", tol, base, cand]) == 2
    err = capsys.readouterr().err
    assert "wpm" in err
    assert "seconds_to_yield" in err   # the known-dimension list is named


def test_malformed_tolerance_value_is_a_usage_error(tmp_path):
    tol, base, cand = _paths(
        tmp_path, 'seconds_to_yield: "fast"',
        [_event("a", seconds_to_yield=0.40)],
        [_event("a", seconds_to_yield=0.40)],
    )
    assert cli.main(["baseline", "check", tol, base, cand]) == 2


def test_negative_tolerance_is_a_usage_error(tmp_path):
    tol, base, cand = _paths(
        tmp_path, 'seconds_to_yield: "-0.05"',
        [_event("a", seconds_to_yield=0.40)],
        [_event("a", seconds_to_yield=0.40)],
    )
    assert cli.main(["baseline", "check", tol, base, cand]) == 2


def test_non_run_envelope_is_a_usage_error(tmp_path, capsys):
    tol = _write(tmp_path, "tolerances.yaml", 'seconds_to_yield: "+0.05"')
    base = _write(tmp_path, "baseline.json", {"not": "an envelope"})
    cand = _write(tmp_path, "candidate.json",
                  _envelope([_event("a", seconds_to_yield=0.40)]))
    assert cli.main(["baseline", "check", tol, base, cand]) == 2
    assert "run envelope" in capsys.readouterr().err


def test_release_compare_envelope_is_refused_with_a_pointer(tmp_path, capsys):
    tol = _write(tmp_path, "tolerances.yaml", 'seconds_to_yield: "+0.05"')
    base = _write(tmp_path, "baseline.json",
                  {"kind": "hotato.release-compare", "version": 1})
    cand = _write(tmp_path, "candidate.json",
                  _envelope([_event("a", seconds_to_yield=0.40)]))
    assert cli.main(["baseline", "check", tol, base, cand]) == 2
    assert "release compare" in capsys.readouterr().err


def test_bare_baseline_without_check_is_a_usage_error():
    with pytest.raises(SystemExit) as exc:
        cli.main(["baseline"])
    assert exc.value.code == 2


# --- the JSON envelope --------------------------------------------------------

def test_json_shape_is_stable(tmp_path, capsys):
    tol, base, cand = _paths(
        tmp_path,
        'response_gap_sec: "+10%"\nseconds_to_yield: "+0.05"\n',
        [_event("a", seconds_to_yield=0.40, response_gap_sec=0.50)],
        [_event("a", seconds_to_yield=0.60, response_gap_sec=0.52)],
    )
    assert cli.main(["baseline", "check", tol, base, cand,
                     "--format", "json"]) == 1
    env = json.loads(capsys.readouterr().out)
    assert env["tool"] == "hotato"
    assert env["schema_version"] == "1"
    assert env["kind"] == "hotato.baseline-check"
    assert env["version"] == 1
    assert env["within_tolerance"] is False
    assert env["exit_code"] == 1
    assert env["drifted"] == ["seconds_to_yield"]
    d = env["dimensions"]["seconds_to_yield"]
    assert set(d) == {"tolerance", "tolerance_kind", "allowed_increase_sec",
                      "baseline", "candidate", "delta_sec", "within"}
    assert d["tolerance"] == "+0.05"
    assert d["tolerance_kind"] == "absolute"
    assert d["within"] is False
    assert d["baseline"] == {"mean_sec": 0.4, "n": 1}
    assert d["candidate"] == {"mean_sec": 0.6, "n": 1}
    assert d["delta_sec"] == pytest.approx(0.2)
    rg = env["dimensions"]["response_gap_sec"]
    assert rg["tolerance_kind"] == "percent"
    assert rg["allowed_increase_sec"] == pytest.approx(0.05)
    assert rg["within"] is True


# --- the JUnit render ---------------------------------------------------------

def test_junit_written_with_one_testcase_per_dimension(tmp_path, capsys):
    tol, base, cand = _paths(
        tmp_path,
        'response_gap_sec: "+10%"\nseconds_to_yield: "+0.05"\n',
        [_event("a", seconds_to_yield=0.40, response_gap_sec=0.50)],
        [_event("a", seconds_to_yield=0.60, response_gap_sec=0.52)],
    )
    junit = os.path.join(tmp_path, "drift.xml")
    assert cli.main(["baseline", "check", tol, base, cand,
                     "--junit", junit]) == 1
    with open(junit, encoding="utf-8") as fh:
        xml = fh.read()
    assert xml.startswith('<?xml version="1.0" encoding="UTF-8"?>')
    assert '<testsuite name="hotato baseline check" tests="2" failures="1">' in xml
    assert '<testcase classname="hotato.baseline" name="seconds_to_yield">' in xml
    assert '<testcase classname="hotato.baseline" name="response_gap_sec">' in xml
    assert xml.count("<failure") == 1
    assert "exceeds allowed" in xml


def test_junit_all_within_has_zero_failures(tmp_path):
    tol, base, cand = _paths(
        tmp_path, 'seconds_to_yield: "+0.05"',
        [_event("a", seconds_to_yield=0.40)],
        [_event("a", seconds_to_yield=0.41)],
    )
    junit = os.path.join(tmp_path, "drift.xml")
    assert cli.main(["baseline", "check", tol, base, cand,
                     "--junit", junit]) == 0
    with open(junit, encoding="utf-8") as fh:
        xml = fh.read()
    assert 'failures="0"' in xml
    assert "<failure" not in xml


# --- module-level: tolerance parsing -----------------------------------------

def test_parse_tolerances_accepts_bare_and_signed_forms():
    parsed = B.parse_tolerances(
        'response_gap_sec: "10%"\n'
        "seconds_to_yield: +0.05\n"     # bare: the YAML subset coerces to 0.05
        'talk_over_sec: "+0.02"\n'
    )
    assert parsed["response_gap_sec"] == {"raw": "10%", "kind": "percent",
                                          "amount": 10.0}
    assert parsed["seconds_to_yield"] == {"raw": 0.05, "kind": "absolute",
                                          "amount": 0.05}
    assert parsed["talk_over_sec"] == {"raw": "+0.02", "kind": "absolute",
                                       "amount": 0.02}


def test_parse_tolerances_refuses_an_empty_or_non_mapping_doc():
    with pytest.raises(ValueError):
        B.parse_tolerances("")
    with pytest.raises(ValueError):
        B.parse_tolerances('["seconds_to_yield"]')
