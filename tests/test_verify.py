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


def test_claim_never_calls_a_regression_an_improvement(tmp_path):
    """Regression (honesty): when the battery got strictly worse (n==0 now pass,
    every fixture regressed) the headline claim must NOT say 'This improvement'.
    Before the fix the same fixed template unconditionally called any
    min-n-supported outcome an 'improvement', even a pure regression."""
    before = _write(tmp_path, "b.json",
                    [_ev(f"f{i}", True, False, 0.1) for i in range(3)])
    after = _write(tmp_path, "a.json",
                   [_ev(f"f{i}", True, False, 0.9) for i in range(3)])
    v = _verify.verify_sides(before, after, min_n=3)
    assert v["results"]["worse"] == 3
    assert v["regression_axis"]["now_pass"] == 0
    assert v["regressions"] == ["f0", "f1", "f2"]
    stmt = v["claim"]["statement"].lower()
    # the misleading headline is "this improvement COINCIDES..."; it must be gone
    assert "this improvement" not in stmt, stmt
    assert "regress" in stmt
    assert "caused by" not in stmt


def test_claim_mixed_result_is_not_called_an_improvement(tmp_path):
    """When some fixtures newly pass but others regress, the outcome is 'mixed',
    not an unqualified 'improvement'."""
    before = _write(tmp_path, "b.json", [
        _ev("f0", True, False, 1.2), _ev("f1", True, False, 0.1),
        _ev("f2", True, False, 0.1),
    ])
    after = _write(tmp_path, "a.json", [
        _ev("f0", True, True, 0.3, 0.4),  # fixed
        _ev("f1", True, False, 0.9),       # worse
        _ev("f2", True, False, 0.9),       # worse
    ])
    v = _verify.verify_sides(before, after, min_n=3)
    assert v["regression_axis"]["now_pass"] == 1
    assert len(v["regressions"]) == 2
    stmt = v["claim"]["statement"].lower()
    assert "mixed" in stmt
    assert "this improvement" not in stmt


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


# --- hotato.verify.yaml policy gate -----------------------------------------
#
# verify --policy turns the measured rollup into a PASS/FAIL gate: target.improve
# success criteria AND hard guardrails. The anti-bandaid law: it passes only when
# every guardrail holds AND every target is met, so a one-axis fix that regresses
# (or never tests) the other axis cannot pass.

_FULL_POLICY = """\
target:
  improve:
    talk_over_sec_p95: -0.5   # must drop by at least half a second
    failed_count: decrease
guardrails:
  max_new_false_yields: 0
  max_not_scorable: 0
  require_hold_fixture: true
  require_yield_fixture: true
"""


def _policy(tmp_path, text, name="hotato.verify.yaml"):
    p = tmp_path / name
    p.write_text(text, encoding="utf-8")
    return str(p)


def _good_before(tmp_path):
    return _write(tmp_path, "b.json", [
        _ev("f1", True, False, 1.2), _ev("f2", True, False, 1.3),
        _ev("f3", True, False, 1.5), _ev("f4", True, False, 1.1),
        _ev("h1", False, True, 0.0),
    ])


def test_policy_parses_the_documented_subset(tmp_path):
    """The stdlib parser coerces the exact scalar types the canon schema uses
    (signed float, keyword string, int cap, bool), with comments stripped."""
    raw = _verify._parse_verify_policy(_FULL_POLICY)
    assert raw["target"]["improve"]["talk_over_sec_p95"] == -0.5
    assert raw["target"]["improve"]["failed_count"] == "decrease"
    assert raw["guardrails"]["max_new_false_yields"] == 0
    assert raw["guardrails"]["require_hold_fixture"] is True
    pol = _verify.load_policy(_policy(tmp_path, _FULL_POLICY))
    assert pol["source"].endswith("hotato.verify.yaml")
    assert pol["target"]["improve"]["talk_over_sec_p95"] == -0.5
    assert set(pol["guardrails"]) == {
        "max_new_false_yields", "max_not_scorable",
        "require_hold_fixture", "require_yield_fixture"}


def test_policy_all_pass_exits_0_and_reports_targets(tmp_path):
    before = _good_before(tmp_path)
    after = _write(tmp_path, "a.json", [
        _ev("f1", True, True, 0.3, 0.4), _ev("f2", True, True, 0.2, 0.5),
        _ev("f3", True, True, 0.4, 0.6), _ev("f4", True, True, 0.3, 0.4),
        _ev("h1", False, True, 0.0),
    ])
    pol = _policy(tmp_path, _FULL_POLICY)
    v = _verify.verify_sides(before, after, min_n=3)
    ev = _verify.evaluate_policy(v, _verify.load_policy(pol))
    assert ev["passed"] is True
    assert ev["targets_met"] is True and ev["guardrails_ok"] is True
    # target.improve is reported: both metrics present with met=True
    metrics = {t["metric"]: t for t in ev["targets"]}
    assert metrics["talk_over_sec_p95"]["met"] is True
    assert metrics["failed_count"]["met"] is True
    assert cli.main(["verify", "--before", before, "--after", after,
                     "--policy", pol]) == 0


def test_policy_guardrail_max_new_false_yields_fires(tmp_path):
    """Anti-bandaid: a fix that clears talk-over but makes a hold guard newly
    yield trips max_new_false_yields -> verify FAILS exit 1, even though the
    talk-over and failing-count TARGETS are met."""
    before = _good_before(tmp_path)
    after = _write(tmp_path, "a.json", [
        _ev("f1", True, True, 0.3, 0.4), _ev("f2", True, True, 0.2, 0.5),
        _ev("f3", True, True, 0.4, 0.6), _ev("f4", True, True, 0.3, 0.4),
        _ev("h1", False, False, 0.0),  # hold guard now false-yields
    ])
    pol = _policy(tmp_path, _FULL_POLICY)
    v = _verify.verify_sides(before, after, min_n=3)
    ev = _verify.evaluate_policy(v, _verify.load_policy(pol))
    # the whole point: targets met, but a guardrail is violated -> not passed
    assert ev["targets_met"] is True
    assert ev["guardrails_ok"] is False
    assert ev["passed"] is False
    g = {x["name"]: x for x in ev["guardrails"]}
    assert g["max_new_false_yields"]["observed"] == 1
    assert g["max_new_false_yields"]["ok"] is False
    assert cli.main(["verify", "--before", before, "--after", after,
                     "--policy", pol]) == 1


def test_policy_require_hold_fixture_fails_when_battery_lacks_one(tmp_path):
    """A battery with no hold fixture cannot certify the opposite-risk axis, so
    require_hold_fixture is a hard fail even though every yield fixture is
    fixed. A threshold bandaid cannot pass by only testing one side."""
    before = _write(tmp_path, "b.json", [
        _ev("f1", True, False, 1.2), _ev("f2", True, False, 1.3),
        _ev("f3", True, False, 1.4),
    ])
    after = _write(tmp_path, "a.json", [
        _ev("f1", True, True, 0.3, 0.4), _ev("f2", True, True, 0.2, 0.5),
        _ev("f3", True, True, 0.3, 0.4),
    ])
    pol = _policy(tmp_path, "guardrails:\n  require_hold_fixture: true\n")
    ev = _verify.evaluate_policy(
        _verify.verify_sides(before, after, min_n=3), _verify.load_policy(pol))
    g = ev["guardrails"][0]
    assert g["name"] == "require_hold_fixture"
    assert g["observed"] == 0 and g["ok"] is False
    assert ev["passed"] is False
    assert cli.main(["verify", "--before", before, "--after", after,
                     "--policy", pol]) == 1


def test_policy_require_yield_fixture_fails_when_battery_lacks_one(tmp_path):
    before = _write(tmp_path, "b.json",
                    [_ev("h1", False, True, 0.0), _ev("h2", False, True, 0.0)])
    after = _write(tmp_path, "a.json",
                   [_ev("h1", False, True, 0.0), _ev("h2", False, True, 0.0)])
    pol = _policy(tmp_path, "guardrails:\n  require_yield_fixture: true\n")
    ev = _verify.evaluate_policy(
        _verify.verify_sides(before, after, min_n=1), _verify.load_policy(pol))
    assert ev["guardrails"][0]["name"] == "require_yield_fixture"
    assert ev["guardrails"][0]["ok"] is False
    assert ev["passed"] is False


def test_policy_max_not_scorable_fires(tmp_path):
    before = _write(tmp_path, "b.json", [_ev("f1", True, False, 1.2)])
    after = _write(tmp_path, "a.json",
                   [_ev("f1", True, True, 0.3, 0.4, scorable=False)])
    pol = _policy(tmp_path, "guardrails:\n  max_not_scorable: 0\n")
    ev = _verify.evaluate_policy(
        _verify.verify_sides(before, after, min_n=1), _verify.load_policy(pol))
    assert ev["guardrails"][0]["observed"] == 1
    assert ev["passed"] is False


def test_policy_target_not_met_fails_even_when_guardrails_hold(tmp_path):
    """The other half of the anti-bandaid gate: guardrails all hold, but the
    talk-over TARGET (-0.5s) is only improved by -0.2s, so verify fails."""
    before = _write(tmp_path, "b.json", [
        _ev("f1", True, False, 1.0), _ev("f2", True, False, 1.0),
        _ev("f3", True, False, 1.0), _ev("h1", False, True, 0.0),
    ])
    after = _write(tmp_path, "a.json", [
        _ev("f1", True, True, 0.8, 0.4), _ev("f2", True, True, 0.8, 0.5),
        _ev("f3", True, True, 0.8, 0.4), _ev("h1", False, True, 0.0),
    ])
    pol = _policy(tmp_path, "target:\n  improve:\n    talk_over_sec_p95: -0.5\n"
                            "guardrails:\n  max_new_false_yields: 0\n")
    ev = _verify.evaluate_policy(
        _verify.verify_sides(before, after, min_n=3), _verify.load_policy(pol))
    assert ev["guardrails_ok"] is True
    assert ev["targets_met"] is False
    assert ev["passed"] is False
    assert cli.main(["verify", "--before", before, "--after", after,
                     "--policy", pol]) == 1


def test_policy_html_renders_the_policy_section(tmp_path):
    before = _good_before(tmp_path)
    after = _write(tmp_path, "a.json", [
        _ev("f1", True, True, 0.3, 0.4), _ev("f2", True, True, 0.2, 0.5),
        _ev("f3", True, True, 0.4, 0.6), _ev("f4", True, True, 0.3, 0.4),
        _ev("h1", False, False, 0.0),  # violates the guardrail
    ])
    v = _verify.verify_sides(before, after, min_n=3)
    v["policy"] = _verify.evaluate_policy(v, _verify.load_policy(
        _policy(tmp_path, _FULL_POLICY)))
    html = _verify.render_html(v)
    assert "Policy check: FAILED" in html
    assert "max_new_false_yields" in html
    assert "talk_over_sec_p95" in html
    assert "violated" in html
    # still honest by construction
    assert "coincidence, not causation" in html.lower()


def test_policy_invalid_keys_are_exit_2_usage_errors(tmp_path):
    before = _write(tmp_path, "b.json", [_ev("f1", True, False, 1.2)])
    after = _write(tmp_path, "a.json", [_ev("f1", True, True, 0.3, 0.4)])
    bad = _policy(tmp_path, "target:\n  improve:\n    bogus_metric: -0.5\n",
                  name="bad.yaml")
    assert cli.main(["verify", "--before", before, "--after", after,
                     "--policy", bad]) == 2
    # an unknown guardrail is also rejected
    with pytest.raises(ValueError):
        _verify.load_policy(_policy(tmp_path,
                                    "guardrails:\n  max_bogus: 1\n",
                                    name="bad2.yaml"))
    # an empty policy is a usage error, not a silent always-pass
    with pytest.raises(ValueError):
        _verify.load_policy(_policy(tmp_path, "# nothing here\n",
                                    name="empty.yaml"))


def test_policy_parser_rejects_tabs_and_lists(tmp_path):
    with pytest.raises(ValueError):
        _verify._parse_verify_policy("target:\n\timprove: 1\n")
    with pytest.raises(ValueError):
        _verify._parse_verify_policy("guardrails:\n  - max_not_scorable\n")


def test_policy_wrong_typed_guardrail_is_rejected(tmp_path):
    # max_* must be a non-negative int, require_* must be a bool
    with pytest.raises(ValueError):
        _verify.load_policy(_policy(tmp_path,
                                    "guardrails:\n  max_not_scorable: -1\n"))
    with pytest.raises(ValueError):
        _verify.load_policy(_policy(tmp_path,
                                    "guardrails:\n  require_hold_fixture: 3\n",
                                    name="r.yaml"))


def test_shipped_example_policy_loads_and_matches_canon():
    """The example we ship is a real, valid policy carrying the canon schema."""
    import os
    here = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    path = os.path.join(here, "examples", "verify-policy", "hotato.verify.yaml")
    pol = _verify.load_policy(path)
    assert pol["target"]["improve"]["talk_over_sec_p95"] == -0.5
    assert pol["target"]["improve"]["failed_count"] == "decrease"
    assert pol["guardrails"]["max_new_false_yields"] == 0
    assert pol["guardrails"]["max_not_scorable"] == 0
    assert pol["guardrails"]["require_hold_fixture"] is True
    assert pol["guardrails"]["require_yield_fixture"] is True
