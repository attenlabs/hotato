"""Full counterexample-capsule lifecycle coverage for every reducers-v1 kind."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest

from hotato import assert_ as A
from hotato.counterexample import (
    compile_counterexample,
    export_counterexample,
    inspect_counterexample,
    reproduce_counterexample,
    verify_counterexample,
)
from hotato.counterexample.model import canonical_json, prefixed_digest


@dataclass(frozen=True)
class LifecycleCase:
    assertion: dict[str, Any]
    failure_atom: dict[str, Any]
    caller_text: str = "Please help with my account."
    agent_mock: dict[str, Any] | None = None


CASES: dict[str, LifecycleCase] = {
    "phrase": LifecycleCase(
        assertion={"regex": "recorded for quality", "role": "caller"},
        failure_atom={"code": "required-match-missing"},
    ),
    "pii": LifecycleCase(
        assertion={"detectors": ["email"], "mode": "must_not_leak"},
        failure_atom={"code": "pii-detected", "detector": "email"},
        caller_text="Email me at person@example.com.",
    ),
    "policy": LifecycleCase(
        assertion={"rule_ids": ["no-guarantee-language"]},
        failure_atom={
            "code": "policy-violation",
            "rule": "no-guarantee-language",
            "type": "banned",
        },
        caller_text="I guarantee delivery tomorrow.",
    ),
    "tool_call": LifecycleCase(
        assertion={"name": "issue_refund"},
        failure_atom={"code": "tool-missing"},
    ),
    "outcome": LifecycleCase(
        assertion={"all_of": [{"tool_called": "issue_refund"}]},
        failure_atom={"code": "predicate-unmet", "index": 0},
    ),
    "tool_result": LifecycleCase(
        assertion={
            "name": "issue_refund",
            "result_subset": {"status": "posted"},
        },
        failure_atom={"code": "tool-missing"},
    ),
    "tool_error": LifecycleCase(
        assertion={"name": "issue_refund"},
        failure_atom={"code": "tool-missing"},
    ),
    "state": LifecycleCase(
        assertion={
            "resource": "orders",
            "filters": {"id": "A-1"},
            "expect": {"status": "posted"},
        },
        failure_atom={"code": "state-field-value-mismatch", "field": "status"},
        agent_mock={
            "state": {"orders": [{"id": "A-1", "status": "pending"}]}
        },
    ),
    "state_change": LifecycleCase(
        assertion={
            "resource": "account",
            "filters": {"id": "U-1"},
            "field": "balance",
            "changed": True,
        },
        failure_atom={"code": "state-unchanged", "field": "balance"},
        agent_mock={
            "state": {
                "account": {
                    "before": [{"id": "U-1", "balance": 5}],
                    "after": [{"id": "U-1", "balance": 5}],
                }
            }
        },
    ),
    "handoff": LifecycleCase(
        assertion={"to": "billing"},
        failure_atom={"code": "handoff-missing"},
    ),
    "termination": LifecycleCase(
        assertion={"reason": "dropped"},
        failure_atom={"code": "termination-missing"},
    ),
    "latency": LifecycleCase(
        assertion={"tool": "issue_refund", "max_ms": 500},
        failure_atom={"code": "latency-declared-threshold-exceeded"},
        agent_mock={
            "tools": [
                {
                    "name": "issue_refund",
                    "result": {"status": "posted"},
                    "latency_ms": 900,
                }
            ]
        },
    ),
    "entity_accuracy": LifecycleCase(
        assertion={"reference": {"order_id": "A-1"}},
        failure_atom={"code": "entity-missing", "key": "order_id"},
    ),
    "sequence": LifecycleCase(
        assertion={
            "steps": [{"tool": "lookup_order"}, {"tool": "issue_refund"}]
        },
        failure_atom={"code": "sequence-step-absent", "index": 0},
    ),
    "count": LifecycleCase(
        assertion={"span_type": "tool_call", "count": 1},
        failure_atom={"code": "count-below"},
    ),
}


def _documents(kind: str, case: LifecycleCase) -> tuple[dict[str, Any], dict[str, Any]]:
    scenario: dict[str, Any] = {
        "kind": "hotato.scenario",
        "version": 1,
        "id": f"{kind}-lifecycle-scenario",
        "goal": {"type": "support", "target": "account"},
        "caller": {"script": [{"say": case.caller_text}]},
    }
    if case.agent_mock is not None:
        scenario["agent_mock"] = case.agent_mock

    assertion = {
        "id": f"{kind}-failure",
        "kind": kind,
        **case.assertion,
    }
    test_doc = {
        "kind": "hotato.conversation-test",
        "version": 1,
        "id": f"{kind}-lifecycle-test",
        "agent": "fixture-agent",
        "assertions": {"deterministic": [assertion], "rubric": []},
    }
    return scenario, test_doc


def test_lifecycle_matrix_covers_every_scripted_counterexample_assertion_kind() -> None:
    assert set(CASES) == set(A.KINDS).difference({
        "timing_contract", "dtmf", "http_result", "formula",
    })


@pytest.mark.parametrize("kind", sorted(CASES))
def test_private_and_share_safe_capsule_lifecycle_for_every_kind(
    tmp_path: Path,
    kind: str,
) -> None:
    case = CASES[kind]
    scenario, test_doc = _documents(kind, case)
    target = f"{kind}-failure"
    scenario_path = tmp_path / f"{kind}.scenario.json"
    test_path = tmp_path / f"{kind}.test.json"
    private = tmp_path / f"{kind}.hotato-repro"
    share = tmp_path / f"{kind}.share.hotato-repro"
    scenario_path.write_text(canonical_json(scenario, pretty=True), encoding="utf-8")
    test_path.write_text(canonical_json(test_doc, pretty=True), encoding="utf-8")

    compiled = compile_counterexample(
        str(scenario_path),
        str(test_path),
        target=target,
        out_dir=str(private),
        workspace=str(tmp_path),
    )
    assert compiled["exit_code"] == 0
    assert compiled["minimality"] == "one_minimal"
    assert compiled["target"]["kind"] == kind
    assert compiled["target"]["failure_atom"] == case.failure_atom

    verified = verify_counterexample(str(private))
    assert verified["exit_code"] == 0
    assert verified["ok"] is True
    assert verified["status"] == "verified"
    assert verified["counterexample_id"] == compiled["counterexample_id"]

    reproduced = reproduce_counterexample(str(private))
    assert reproduced["exit_code"] == 0
    assert reproduced["ok"] is True
    assert reproduced["status"] == "failure_reproduced"
    assert reproduced["counterexample_id"] == compiled["counterexample_id"]

    private_summary = inspect_counterexample(str(private))
    assert private_summary["exit_code"] == 0
    assert private_summary["profile"] == "private-runnable-v1"
    assert private_summary["counterexample_id"] == compiled["counterexample_id"]
    assert private_summary["target"]["kind"] == kind
    assert private_summary["target"]["failure_atom"] == case.failure_atom

    exported = export_counterexample(str(private), out_dir=str(share))
    assert exported["exit_code"] == 0
    assert exported["profile"] == "share-safe-v1"
    assert exported["runnable"] is False

    share_summary = inspect_counterexample(str(share))
    assert share_summary["exit_code"] == 0
    assert share_summary["profile"] == "share-safe-v1"
    assert share_summary["counterexample_id"] == exported["counterexample_id"]
    assert share_summary["target"]["kind"] == kind
    assert share_summary["target"]["failure_atom_digest"] == prefixed_digest(
        case.failure_atom
    )
    assert not (share / "input").exists()
