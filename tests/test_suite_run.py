"""``hotato suite run`` (hotato.suite_run): run a suite.v1 of conversation-tests
through the deterministic simulator, record the 8-entity registry rows, and emit
a per-dimension + reliability report -- never a blended score.
"""

from __future__ import annotations

import json
import os

import pytest

from hotato import suite_run as SR
from hotato.fleet.registry import Registry


# =========================================================================
# tiny inline fixtures (one PASSing job, one job with a genuine outcome DEFECT)
# =========================================================================

def _pass_scenario():
    return {
        "kind": "hotato.scenario", "version": 1, "id": "refund-ok",
        "goal": {"type": "get_refund", "target": "order A-1"},
        "facts": {"order_id": "A-1"},
        "caller": {"script": [{"say": "order A-1 arrived damaged"},
                              {"say": "i want a refund"}],
                   "behavior": {"backchannels": {"probability": 0.0}}},
        "variation_matrix": {"speaking_rate": [0.9, 1.0, 1.1], "noise": ["clean", "cafe"],
                             "repetitions": 1},
        "agent_mock": {
            "tools": [{"name": "lookup_order", "arguments": {"order_id": "A-1"},
                       "result": {"found": True}, "latency_ms": 300},
                      {"name": "issue_refund", "arguments": {"order_id": "A-1"},
                       "result": {"status": "refunded"}, "latency_ms": 500}],
            "state": {"orders": [{"order_id": "A-1", "refund_status": "refunded"}]},
        },
    }


def _defect_scenario():
    # The agent claims a refund but never calls issue_refund (outcome DEFECT).
    return {
        "kind": "hotato.scenario", "version": 1, "id": "refund-broken",
        "goal": {"type": "get_refund", "target": "order A-2"},
        "facts": {"order_id": "A-2"},
        "caller": {"script": [{"say": "order A-2 never arrived"},
                              {"say": "please refund order A-2"}],
                   "behavior": {"backchannels": {"probability": 0.0}}},
        "variation_matrix": {"speaking_rate": [0.9, 1.0, 1.1], "noise": ["clean", "cafe"],
                             "repetitions": 1},
        "agent_mock": {
            "tools": [{"name": "lookup_order", "arguments": {"order_id": "A-2"},
                       "result": {"found": True}, "latency_ms": 300}],
            "state": {"orders": [{"order_id": "A-2", "refund_status": "none"}]},
        },
    }


def _test_for(scn_id, expect_pass):
    det = [
        {"id": "asked-refund", "kind": "phrase", "regex": "refund", "role": "caller",
         "dimension": "conversation"},
        {"id": "refund-tool", "kind": "tool_result", "name": "issue_refund",
         "result_subset": {"status": "refunded"}, "dimension": "outcome"},
        {"id": "refund-state", "kind": "state", "resource": "orders",
         "filters": {"order_id": "A-1" if expect_pass else "A-2"},
         "expect": {"refund_status": "refunded"}, "dimension": "outcome"},
    ]
    return {
        "kind": "hotato.conversation-test", "version": 1,
        "id": f"{scn_id}-test", "agent": "agent-under-test",
        "scenario": f"{scn_id}.scenario.json",
        "assertions": {"deterministic": det},
        "success": {"required": ["all_deterministic_assertions_pass"]},
    }


def _write(path, obj):
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(obj, fh)


def _build_suite(tmp_path, *, policy="fail", include_defect=True):
    _write(os.path.join(tmp_path, "refund-ok.scenario.json"), _pass_scenario())
    _write(os.path.join(tmp_path, "refund-ok.test.json"), _test_for("refund-ok", True))
    tests = ["refund-ok.test.json"]
    if include_defect:
        _write(os.path.join(tmp_path, "refund-broken.scenario.json"), _defect_scenario())
        _write(os.path.join(tmp_path, "refund-broken.test.json"),
               _test_for("refund-broken", False))
        tests.append("refund-broken.test.json")
    suite = {"kind": "hotato.suite", "version": 1, "suite_id": "smoke",
             "name": "smoke", "required_for_release": True,
             "inconclusive_policy": policy, "tests": tests}
    p = os.path.join(tmp_path, "smoke.suite.json")
    _write(p, suite)
    return p


def _run(tmp_path, *, registry=None, release_id="rc", policy="fail",
         include_defect=True, out_dir=None, workspace="default"):
    suite_path = _build_suite(tmp_path, policy=policy, include_defect=include_defect)
    suite_doc, base_dir = SR.load_suite_file(suite_path)
    return SR.run_suite(suite_doc, base_dir, agent_id="agent-under-test",
                        release_id=release_id, registry=registry, out_dir=out_dir,
                        workspace=workspace)


# =========================================================================
# resolution + validation
# =========================================================================

def test_resolve_test_refs_missing_ref_raises(tmp_path):
    with pytest.raises(ValueError):
        SR.resolve_test_refs(["does-not-exist.json"], str(tmp_path))


def test_load_suite_file_rejects_bad_kind(tmp_path):
    p = os.path.join(tmp_path, "bad.json")
    _write(p, {"kind": "nope", "version": 1, "suite_id": "x", "name": "x", "tests": []})
    with pytest.raises(ValueError):
        SR.load_suite_file(p)


# =========================================================================
# per-dimension + reliability, never a blended score
# =========================================================================

def test_run_suite_reports_per_dimension_and_reliability(tmp_path):
    res = _run(tmp_path)
    assert res["counts"]["tests"] == 2
    # 2 scenarios x 3 rates x 2 noise = 12 runs total.
    assert res["counts"]["runs"] == 12
    assert res["counts"]["valid"] == 12
    assert res["counts"]["simulator_invalid"] == 0
    assert res["counts"]["passed_tests"] == 1
    assert res["counts"]["failed_tests"] == 1
    # per-dimension grouping present; outcome has both a pass (ok test) and a fail
    # (defect test), never merged into one number.
    assert res["dimensions"]["outcome"]["pass"] >= 1
    assert res["dimensions"]["outcome"]["fail"] >= 1
    # reliability over all 12 valid runs (6 pass, 6 fail) -> pass@1 == 0.5.
    assert res["reliability"]["n"] == 12
    assert res["reliability"]["pass_at_1"] == pytest.approx(0.5)
    # worst test outcome gates the suite.
    assert res["exit_code"] == 1
    assert res["origin"] == "simulated"


def test_no_overall_score_anywhere(tmp_path):
    res = _run(tmp_path)

    def _has(obj):
        if isinstance(obj, dict):
            return "overall_score" in obj or any(_has(v) for v in obj.values())
        if isinstance(obj, list):
            return any(_has(v) for v in obj)
        return False

    assert not _has(res)


def test_all_pass_suite_exits_zero(tmp_path):
    res = _run(tmp_path, include_defect=False)
    assert res["counts"]["failed_tests"] == 0
    assert res["exit_code"] == 0
    assert res["reliability"]["pass_at_1"] == pytest.approx(1.0)


# =========================================================================
# suite inconclusive_policy is authoritative (invariant 3)
# =========================================================================

def test_static_test_inconclusive_can_fail_under_suite_policy(tmp_path):
    # A test with NO scenario + an assertion needing a trace -> INCONCLUSIVE.
    static = {
        "kind": "hotato.conversation-test", "version": 1, "id": "needs-trace",
        "agent": "agent-under-test",
        "assertions": {"deterministic": [
            {"id": "must-call", "kind": "tool_result", "name": "do_thing",
             "result_subset": {"ok": True}, "dimension": "outcome"}]},
        "success": {"required": ["all_deterministic_assertions_pass"]},
    }
    _write(os.path.join(tmp_path, "needs-trace.test.json"), static)
    suite = {"kind": "hotato.suite", "version": 1, "suite_id": "ci",
             "name": "ci", "required_for_release": True,
             "inconclusive_policy": "fail", "tests": ["needs-trace.test.json"]}
    p = os.path.join(tmp_path, "ci.suite.json")
    _write(p, suite)
    doc, base = SR.load_suite_file(p)
    res = SR.run_suite(doc, base, agent_id="agent-under-test", release_id="rc")
    # Under the suite's fail policy, the INCONCLUSIVE (absent trace) gates.
    assert res["exit_code"] != 0
    t = res["tests"][0]
    assert t["kind"] == "static"
    assert t["dimensions"]["outcome"] == "INCONCLUSIVE"


def test_report_policy_does_not_gate_inconclusive(tmp_path):
    # success tolerates INCONCLUSIVE (no_deterministic_fail), so under the report
    # policy an absent-input INCONCLUSIVE gates NOTHING.
    static = {
        "kind": "hotato.conversation-test", "version": 1, "id": "needs-trace",
        "agent": "agent-under-test",
        "assertions": {"deterministic": [
            {"id": "must-call", "kind": "tool_result", "name": "do_thing",
             "result_subset": {"ok": True}, "dimension": "outcome"}]},
        "success": {"required": ["no_deterministic_fail"]},
    }
    _write(os.path.join(tmp_path, "needs-trace.test.json"), static)
    suite = {"kind": "hotato.suite", "version": 1, "suite_id": "obs",
             "name": "obs", "inconclusive_policy": "report",
             "tests": ["needs-trace.test.json"]}
    p = os.path.join(tmp_path, "obs.suite.json")
    _write(p, suite)
    doc, base = SR.load_suite_file(p)
    res = SR.run_suite(doc, base, agent_id="agent-under-test", release_id="rc")
    assert res["exit_code"] == 0  # report policy: INCONCLUSIVE does not gate


# =========================================================================
# registry population (so `hotato serve` renders the views)
# =========================================================================

def test_run_suite_records_8_entity_rows(tmp_path):
    reg = Registry(os.path.join(tmp_path, "reg"))
    try:
        res = _run(tmp_path, registry=reg, release_id="rc1", workspace="w")
        assert reg.get_release("w", "rc1") is not None
        assert reg.get_suite("w", "smoke") is not None
        assert reg.get_scenario("w", "refund-ok-test") is not None
        runs = reg.list_runs("w", release_id="rc1", limit=1000)
        assert len(runs) == res["counts"]["runs"]
        # every completed run has a conversation + at least one evaluation.
        completed = [r for r in runs if r["status"] == "completed"]
        assert completed
        convs = reg.list_conversations("w", run_id=completed[0]["run_id"], limit=10)
        assert convs and convs[0]["origin"] == "simulated"
        evals = reg.list_evaluations("w", conversation_id=convs[0]["conversation_id"],
                                     limit=10)
        assert evals
        assert all(e["status"] in ("PASS", "FAIL", "INCONCLUSIVE") for e in evals)
    finally:
        reg.close()


def test_two_releases_do_not_clobber(tmp_path):
    reg = Registry(os.path.join(tmp_path, "reg"))
    try:
        _run(tmp_path, registry=reg, release_id="rc1", workspace="w")
        _run(tmp_path, registry=reg, release_id="rc2", workspace="w")
        rc1 = reg.list_runs("w", release_id="rc1", limit=1000)
        rc2 = reg.list_runs("w", release_id="rc2", limit=1000)
        assert len(rc1) == 12 and len(rc2) == 12
        # the run ids are release-namespaced, so no row was clobbered.
        assert not (set(r["run_id"] for r in rc1) & set(r["run_id"] for r in rc2))
    finally:
        reg.close()


# =========================================================================
# rendering is deterministic (byte-reproducible)
# =========================================================================

def test_reports_are_deterministic(tmp_path):
    res = _run(tmp_path)
    assert SR.render_report_md(res) == SR.render_report_md(res)
    assert SR.render_report_html(res) == SR.render_report_html(res)
    md = SR.render_report_md(res)
    assert "Per-dimension" in md
    html = SR.render_report_html(res)
    assert "<h1>Suite report" in html
