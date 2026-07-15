"""Shared synthetic SOURCE results for the Failure Record tests.

These build the already-evaluated envelopes the projection consumes (a
test-run result, a suite-run result, a contract-verify envelope) with
controllable verdicts. Synthetic fixtures for structure only: they establish
schema, authority, privacy, and renderer behavior, never agent performance.
"""

import copy

RELIABILITY_AGGREGATE = {
    "pass_at_1": 0.4,
    "pass_at_k": 1.0,
    "pass_caret_k": 0.0,
    "n": 5,
    "k": 5,
    "passes": 2,
    "ci": {"low": 0.117621, "high": 0.76928, "method": "wilson", "z": 1.96},
    "note": "synthetic fixture aggregate",
}


def det_row(row_id, kind, status, dimension=None, reason=None, **extra):
    row = {"id": row_id, "kind": kind, "deterministic": True, "status": status}
    if dimension is not None:
        row["dimension"] = dimension
    if reason is not None:
        row["reason"] = reason
    row.update(extra)
    return row


DEFAULT_ROWS = [
    det_row("refund-issued", "tool_call", "FAIL", dimension="outcome",
            reason="expected a refund.create tool call; none was found in "
                   "the trace"),
    det_row("disclosure-present", "policy", "PASS", dimension="policy"),
    det_row("yield-latency", "latency", "PASS", dimension="conversation"),
]


def make_test_run(rows=None, *, rubric_results=(), rubric_gated=False,
                  reliability=None, exit_code=None, conversation=None,
                  origin="real", test_id="refund-claimed-not-issued"):
    rows = copy.deepcopy(DEFAULT_ROWS) if rows is None else list(rows)
    statuses = [r["status"] for r in rows]
    if exit_code is None:
        exit_code = 1 if "FAIL" in statuses else 0
    doc = {
        "kind": "hotato.test-run",
        "version": 1,
        "test_id": test_id,
        "agent": "support-agent",
        "inconclusive_policy": "report",
        "exit_code": exit_code,
        "success": {
            "required": ["all_deterministic_assertions_pass"],
            "conditions": {"all_deterministic_assertions_pass":
                           all(s == "PASS" for s in statuses)},
            "passed": all(s == "PASS" for s in statuses),
            "rubric_gated": rubric_gated,
        },
        "assertions": {
            "schema": "assert.v1",
            "exit_code": exit_code,
            "inconclusive_policy": "report",
            "results": rows,
            "summary": {
                "deterministic": {
                    "pass": statuses.count("PASS"),
                    "fail": statuses.count("FAIL"),
                    "inconclusive": statuses.count("INCONCLUSIVE"),
                },
                "judge": {"pass": 0, "fail": 0},
                "note": "synthetic",
            },
        },
        "rubric": {
            "schema": "rubric.v1",
            "exit_code": 1 if (rubric_gated and any(
                r.get("status") in ("FAIL", "ERROR") for r in rubric_results
            )) else 0,
            "advisory": not rubric_gated,
            "gated": rubric_gated,
            "results": list(rubric_results),
            "summary": {},
        },
        "dimensions": {},
        "reliability": {
            "aggregate": (copy.deepcopy(RELIABILITY_AGGREGATE)
                          if reliability is None else reliability),
            "origin": origin,
            "runs": 5,
            "basis": "agent_deterministic_replay",
            "note": "synthetic",
            "per_run": [],
        },
        "repetitions": {"runs": 5, "per_run": []},
    }
    if conversation is not None:
        doc["conversation"] = conversation
    return doc


def make_suite_run(tests):
    return {
        "kind": "hotato.suite-run",
        "version": 1,
        "suite_id": "support-regression",
        "name": "support regression",
        "agent": "support-agent",
        "release_id": "rc-2",
        "workspace": "default",
        "inconclusive_policy": "report",
        "required_for_release": True,
        "origin": "simulated",
        "counts": {"tests": len(tests)},
        "dimensions": {},
        "reliability": {},
        "tests": list(tests),
        "simulator_invalid": [],
        "exit_code": max((t.get("exit_code", 0) for t in tests), default=0),
    }


def make_suite_test(test_id, *, exit_code, dim_counts=None, dim_reason=None,
                    dim_public_reason=None, dim_failure_kind=None,
                    simulator_invalid=(), valid_runs=1,
                    rubric_summary=None, rubric_gated=False):
    return {
        "test_id": test_id,
        "scenario_id": f"{test_id}-scenario",
        "agent": "support-agent",
        "kind": "scenario",
        "inconclusive_policy": "report",
        "dimensions": {},
        "dim_counts": dim_counts or {},
        "dim_reason": dim_reason or {},
        "dim_public_reason": dim_public_reason or {},
        "dim_failure_kind": dim_failure_kind or {},
        "ungrouped": None,
        "success": {"required": [], "conditions": {},
                    "passed": exit_code == 0, "rubric_gated": rubric_gated},
        "rubric_summary": rubric_summary,
        "reliability": copy.deepcopy(RELIABILITY_AGGREGATE),
        "reliability_basis": "simulated_matrix",
        "counts": {"runs": valid_runs + len(simulator_invalid),
                   "valid": valid_runs,
                   "simulator_invalid": len(simulator_invalid), "scored": valid_runs},
        "variation_cells": [],
        "simulator_invalid": list(simulator_invalid),
        "runs": [],
        "exit_code": exit_code,
        "status": {0: "pass", 1: "fail", 2: "refuse"}.get(exit_code, "fail"),
        "origin": "simulated",
    }


def make_contract_verify(results):
    failed = [r for r in results if not r.get("passed", False)]
    return {
        "tool": "hotato",
        "kind": "contract-verify",
        "schema_version": "1",
        "offline": True,
        "dir": "bundles",
        "count": len(results),
        "results": list(results),
        "summary": {"passed": len(results) - len(failed), "failed": len(failed)},
        "tampered": 0,
        "refused": 0,
        "assertions_failed": 0,
        "exit_code": 1 if failed else 0,
    }


def make_contract_result(contract_id, *, passed, not_scorable_reason=None,
                         expect=None, did_yield=None, seconds_to_yield=0.42,
                         talk_over_sec=None):
    if did_yield is None:
        did_yield = passed
    return {
        "id": contract_id,
        "expect": expect,
        "passed": passed,
        "not_scorable_reason": not_scorable_reason,
        "verdict_eligible": not_scorable_reason is None,
        "measurement": {"did_yield": did_yield,
                        "seconds_to_yield": seconds_to_yield,
                        "talk_over_sec": talk_over_sec,
                        "scorable": not_scorable_reason is None},
    }
