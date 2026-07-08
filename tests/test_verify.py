"""hotato verify: battery-scale before/after proof, reusing compare + aggregate.

Covers the load-bearing behaviours from the spec:

* a synthetic old/new envelope set yields the correct before/after rollup:
  N of M previously-failing fixtures now pass, K of L hold guards still pass;
* the taxonomy per fixture matches compare.classify_pair exactly (reuse, not a
  reimplementation);
* the claim is REFUSED under low n (too few previously-failing fixtures);
* it says "coincides with", never "caused by";
* an unjudgeable side is not_scorable, an unpaired fixture is reported, and
  --fail-on-regression gates on a real regression.
"""

from __future__ import annotations

import json

import pytest

from hotato import cli
from hotato import compare as _compare
from hotato import verify as _verify


def _ev(eid, expected_yield, passed, tov=0.0, tty=None, scorable=True):
    v = {
        "passed": passed,
        "did_yield": expected_yield if passed else (not expected_yield),
        "talk_over_sec": tov,
        "seconds_to_yield": tty,
        "reasons": [] if passed else ["out of bound"],
    }
    e = {"event_id": eid, "scenario_id": eid,
         "expected_yield": expected_yield, "verdict": v}
    if not scorable:
        e["scorable"] = False
    return e


def _env(events):
    passed = sum(1 for e in events if e["verdict"]["passed"])
    return {
        "tool": "hotato", "mode": "suite", "stack": "vapi", "offline": True,
        "events": events,
        "summary": {"events": len(events), "passed": passed,
                    "failed": len(events) - passed},
        "exit_code": 0,
    }


def _write(tmp_path, name, events):
    p = tmp_path / name
    p.write_text(json.dumps(_env(events)), encoding="utf-8")
    return str(p)


# --- the core rollup --------------------------------------------------------

def test_rollup_counts_fixed_and_hold_guards(tmp_path):
    before = _write(tmp_path, "before.json", [
        _ev("f1", True, False, 1.2), _ev("f2", True, False, 0.9, 2.1),
        _ev("f3", True, False, 1.5), _ev("f4", True, False, 0.8, 1.9),
        _ev("h1", False, True, 0.0),
    ])
    after = _write(tmp_path, "after.json", [
        _ev("f1", True, True, 0.3, 0.4), _ev("f2", True, True, 0.2, 0.5),
        _ev("f3", True, True, 0.4, 0.6), _ev("f4", True, True, 0.3, 0.4),
        _ev("h1", False, True, 0.0),
    ])
    v = _verify.verify_sides(before, after, min_n=3)
    assert v["paired"] == 5
    assert v["regression_axis"] == {"used_to_fail": 4, "now_pass": 4, "still_fail": 0}
    assert v["hold_axis"] == {"hold_guards": 1, "still_pass": 1, "regressed": 0}
    assert v["results"]["fixed"] == 4
    assert v["results"]["still_pass"] == 1
    assert v["claim"]["supported"] is True
    assert "4 of 4" in v["claim"]["statement"]


def test_per_fixture_result_matches_compare_taxonomy(tmp_path):
    before_events = [_ev("f1", True, False, 1.2), _ev("h1", False, True, 0.0),
                     _ev("f2", True, False, 1.0, 3.0)]
    after_events = [_ev("f1", True, True, 0.3, 0.4), _ev("h1", False, True, 0.0),
                    _ev("f2", True, False, 1.4, 3.5)]  # both fail, talk-over worse
    before = _write(tmp_path, "b.json", before_events)
    after = _write(tmp_path, "a.json", after_events)
    v = _verify.verify_sides(before, after, min_n=1)
    by = {r["fixture"]: r["result"] for r in v["per_fixture"]}
    # verify must agree with compare.classify_pair on every pair
    for be, ae in zip(before_events, after_events):
        expect_yield = bool(be["expected_yield"])
        assert by[be["event_id"]] == _compare.classify_pair(expect_yield, be, ae)
    assert by["f1"] == "fixed"
    assert by["h1"] == "still_pass"
    assert by["f2"] == "worse"


def test_pooled_distribution_shift_reuses_aggregate_stats(tmp_path):
    before = _write(tmp_path, "b.json",
                    [_ev("f1", True, False, 1.5), _ev("f2", True, False, 1.3)])
    after = _write(tmp_path, "a.json",
                   [_ev("f1", True, True, 0.3, 0.4), _ev("f2", True, True, 0.2, 0.5)])
    v = _verify.verify_sides(before, after, min_n=1)
    assert v["distribution"]["before"]["talk_over_sec"]["n"] == 2
    assert v["distribution"]["after"]["talk_over_sec"]["p95"] < \
        v["distribution"]["before"]["talk_over_sec"]["p95"]


# --- honesty: low-n refusal, coincidence-not-causation ----------------------

def test_low_n_refuses_the_battery_scale_claim(tmp_path):
    before = _write(tmp_path, "b.json",
                    [_ev("f1", True, False, 1.2), _ev("f2", True, False, 0.9),
                     _ev("h1", False, True, 0.0)])
    after = _write(tmp_path, "a.json",
                   [_ev("f1", True, True, 0.3, 0.4), _ev("f2", True, True, 0.2, 0.5),
                    _ev("h1", False, True, 0.0)])
    v = _verify.verify_sides(before, after, min_n=3)
    assert v["claim"]["supported"] is False
    assert "min-n 3" in v["claim"]["statement"]


def test_claim_says_coincides_never_caused(tmp_path):
    before = _write(tmp_path, "b.json",
                    [_ev(f"f{i}", True, False, 1.2) for i in range(4)])
    after = _write(tmp_path, "a.json",
                   [_ev(f"f{i}", True, True, 0.3, 0.4) for i in range(4)])
    v = _verify.verify_sides(before, after, min_n=3)
    text = json.dumps(v).lower()
    assert "coincides" in text
    assert "caused by" not in text
    assert v["claim"]["relationship"] == "coincides_with"


# --- edges: not scorable, unpaired, regression gate -------------------------

def test_not_scorable_side_uses_shared_taxonomy(tmp_path):
    before = _write(tmp_path, "b.json", [_ev("f1", True, False, 1.2)])
    after = _write(tmp_path, "a.json", [_ev("f1", True, True, 0.3, 0.4, scorable=False)])
    v = _verify.verify_sides(before, after, min_n=1)
    assert v["per_fixture"][0]["result"] == "not_scorable"


def test_unpaired_fixtures_are_reported_never_silently_dropped(tmp_path):
    before = _write(tmp_path, "b.json",
                    [_ev("f1", True, False, 1.2), _ev("only_before", True, False, 1.0)])
    after = _write(tmp_path, "a.json",
                   [_ev("f1", True, True, 0.3, 0.4), _ev("only_after", True, True, 0.2, 0.3)])
    v = _verify.verify_sides(before, after, min_n=1)
    assert v["unpaired"]["only_before"] == ["only_before"]
    assert v["unpaired"]["only_after"] == ["only_after"]
    assert v["paired"] == 1


def test_no_pairs_is_a_usage_error(tmp_path):
    before = _write(tmp_path, "b.json", [_ev("x", True, False, 1.2)])
    after = _write(tmp_path, "a.json", [_ev("y", True, True, 0.3, 0.4)])
    with pytest.raises(ValueError):
        _verify.verify_sides(before, after)


def test_duplicate_fixture_within_a_side_is_rejected(tmp_path):
    before = _write(tmp_path, "b.json",
                    [_ev("dup", True, False, 1.2), _ev("dup", True, False, 1.0)])
    after = _write(tmp_path, "a.json", [_ev("dup", True, True, 0.3, 0.4)])
    with pytest.raises(ValueError):
        _verify.verify_sides(before, after)


# --- CLI --------------------------------------------------------------------

def test_cli_verify_text_and_json(tmp_path, capsys):
    before = _write(tmp_path, "b.json",
                    [_ev(f"f{i}", True, False, 1.2) for i in range(4)])
    after = _write(tmp_path, "a.json",
                   [_ev(f"f{i}", True, True, 0.3, 0.4) for i in range(4)])
    assert cli.main(["verify", "--before", before, "--after", after]) == 0
    text = capsys.readouterr().out
    assert "used to fail now pass" in text
    assert "coincidence, not causation" in text

    out = tmp_path / "proof.json"
    assert cli.main(["verify", "--before", before, "--after", after,
                     "--format", "json", "--out", str(out)]) == 0
    doc = json.loads(out.read_text(encoding="utf-8"))
    assert doc["kind"] == "verify"


def test_cli_fail_on_regression_exits_1(tmp_path):
    # a hold guard that regresses (was passing, now fails)
    before = _write(tmp_path, "b.json",
                    [_ev("f1", True, False, 1.2), _ev("h1", False, True, 0.0)])
    after = _write(tmp_path, "a.json",
                   [_ev("f1", True, True, 0.3, 0.4), _ev("h1", False, False, 0.9, 0.2)])
    # default: measures, does not gate
    assert cli.main(["verify", "--before", before, "--after", after]) == 0
    # opt-in gate
    assert cli.main(["verify", "--before", before, "--after", after,
                     "--fail-on-regression"]) == 1


def test_cli_directory_inputs(tmp_path):
    bdir = tmp_path / "before"
    adir = tmp_path / "after"
    bdir.mkdir()
    adir.mkdir()
    (bdir / "run.json").write_text(
        json.dumps(_env([_ev(f"f{i}", True, False, 1.2) for i in range(3)])),
        encoding="utf-8")
    (adir / "run.json").write_text(
        json.dumps(_env([_ev(f"f{i}", True, True, 0.3, 0.4) for i in range(3)])),
        encoding="utf-8")
    assert cli.main(["verify", "--before", str(bdir), "--after", str(adir),
                     "--min-n", "3"]) == 0


# --- defect (round 3): malformed / hand-edited envelope sides never crash ---
#
# verify's contract is a clean exit-2 structured error for a malformed side, the
# same as every other bad-input path -- never a raw AttributeError / TypeError /
# KeyError traceback. Four distinct crash sites, one clean-error contract.

def _write_raw(tmp_path, name, events):
    """An envelope whose events[] is written verbatim (no _ev/_env scoring), so a
    deliberately malformed shape reaches the loader/comparator/stats path."""
    env = {"tool": "hotato", "kind": "run", "schema_version": "1",
           "mode": "suite", "stack": "vapi", "summary": {}, "events": events}
    p = tmp_path / name
    p.write_text(json.dumps(env), encoding="utf-8")
    return str(p)


def test_verify_non_object_events_do_not_crash(tmp_path):
    """events=[1,2,3]: a non-object entry is not a fixture and must be skipped,
    not crash _event_key with AttributeError."""
    b = _write_raw(tmp_path, "b.json", [1, 2, 3])
    a = _write_raw(tmp_path, "a.json", [1, 2, 3])
    # no scalar fixtures pair -> clean exit 2, no traceback
    assert cli.main(["verify", "--before", b, "--after", a]) == 2


def test_verify_unhashable_event_id_is_clean_error(tmp_path):
    """A list event_id is unhashable; it must be a named exit-2 usage error, not a
    raw TypeError: unhashable type at ``key in seen``."""
    ev = [{"event_id": ["a", "b"], "expected_yield": True,
           "verdict": {"passed": True}}]
    b = _write_raw(tmp_path, "b.json", ev)
    a = _write_raw(tmp_path, "a.json", ev)
    assert cli.main(["verify", "--before", b, "--after", a]) == 2


def test_verify_both_fail_missing_metrics_do_not_crash(tmp_path):
    """Both sides scorable and failing, but the verdict omits talk_over_sec /
    seconds_to_yield: compare._both_fail_result must degrade to 'unchanged', not
    raise KeyError."""
    ev = [{"event_id": "e1", "expected_yield": True,
           "verdict": {"passed": False}}]
    b = _write_raw(tmp_path, "b.json", ev)
    a = _write_raw(tmp_path, "a.json", ev)
    v = _verify.verify_sides(b, a)
    assert v["per_fixture"][0]["result"] == "unchanged"
    assert cli.main(["verify", "--before", b, "--after", a]) == 0


def test_verify_non_numeric_metric_does_not_crash_stats(tmp_path):
    """A non-numeric talk_over_sec ('oops') must be excluded from the pooled
    distribution, not raise TypeError in _stats.dist_summary's round()."""
    ev = [{"event_id": "e1", "expected_yield": True,
           "verdict": {"passed": True, "talk_over_sec": "oops"}}]
    b = _write_raw(tmp_path, "b.json", ev)
    a = _write_raw(tmp_path, "a.json", ev)
    v = _verify.verify_sides(b, a)
    # the bogus value never enters the distribution
    assert v["distribution"]["before"]["talk_over_sec"] is None
    assert cli.main(["verify", "--before", b, "--after", a]) == 0


def test_dist_summary_skips_non_numeric():
    from hotato._stats import dist_summary
    assert dist_summary(["oops", None, [1], {"x": 1}]) is None
    assert dist_summary([1.0, "oops", 3.0])["n"] == 2
