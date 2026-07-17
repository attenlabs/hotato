"""Typed failure-oracle coverage for every reducers-v1 assertion kind."""

from __future__ import annotations

import copy

import pytest

from hotato import assert_ as A
from hotato.counterexample.oracle import (
    FailureOracle,
    failure_atoms,
    failure_fingerprint,
    target_assertion,
)
from hotato.state_adapter import MockStateAdapter


def _tool(name, **fields):
    row = {"type": "tool_call", "name": name}
    row.update(fields)
    return row


def _cases():
    return {
        "phrase": (
            {"id": "a", "kind": "phrase", "regex": "recorded", "role": "agent"},
            A.build_context(transcript=[{"role": "agent", "text": "hello"}]),
        ),
        "pii": (
            {"id": "a", "kind": "pii", "detectors": ["email"], "mode": "must_not_leak"},
            A.build_context(transcript=[{"role": "caller", "text": "person@example.com"}]),
        ),
        "policy": (
            {"id": "a", "kind": "policy", "rule_ids": ["no-guarantee-language"]},
            A.build_context(transcript=[{"role": "caller", "text": "I guarantee delivery"}]),
        ),
        "tool_call": (
            {"id": "a", "kind": "tool_call", "name": "issue_refund"},
            A.build_context(spans=[_tool("lookup_order")]),
        ),
        "outcome": (
            {
                "id": "a", "kind": "outcome",
                "all_of": [{"tool_called": "issue_refund"}, {"phrase": "confirmed", "role": "caller"}],
            },
            A.build_context(
                spans=[_tool("lookup_order")],
                transcript=[{"role": "caller", "text": "still waiting"}],
            ),
        ),
        "tool_result": (
            {
                "id": "a", "kind": "tool_result", "name": "issue_refund",
                "result_subset": {"status": "posted"},
            },
            A.build_context(spans=[_tool("issue_refund", result={"status": "pending"})]),
        ),
        "tool_error": (
            {"id": "a", "kind": "tool_error", "name": "issue_refund"},
            A.build_context(spans=[_tool("issue_refund", result={"status": "ok"})]),
        ),
        "state": (
            {
                "id": "a", "kind": "state", "resource": "orders",
                "filters": {"id": "A-1"}, "expect": {"status": "posted"},
            },
            A.build_context(state_adapter=MockStateAdapter({
                "orders": [{"id": "A-1", "status": "pending"}],
            })),
        ),
        "state_change": (
            {
                "id": "a", "kind": "state_change", "resource": "account",
                "filters": {"id": "U-1"}, "field": "balance", "changed": True,
            },
            A.build_context(state_adapter=MockStateAdapter({
                "account": {
                    "before": [{"id": "U-1", "balance": 5}],
                    "after": [{"id": "U-1", "balance": 5}],
                }
            })),
        ),
        "handoff": (
            {"id": "a", "kind": "handoff", "to": "billing"},
            A.build_context(spans=[{"type": "handoff", "to": "support"}]),
        ),
        "termination": (
            {"id": "a", "kind": "termination", "reason": "dropped"},
            A.build_context(spans=[{"type": "termination", "reason": "complete"}]),
        ),
        "latency": (
            {"id": "a", "kind": "latency", "tool": "issue_refund", "max_ms": 500},
            A.build_context(spans=[_tool("issue_refund", latency_ms=900)]),
        ),
        "entity_accuracy": (
            {"id": "a", "kind": "entity_accuracy", "reference": {"order_id": "A-1"}},
            A.build_context(spans=[_tool("lookup", arguments={"order_id": "B-2"})]),
        ),
        "sequence": (
            {
                "id": "a", "kind": "sequence",
                "steps": [{"tool": "lookup"}, {"tool": "issue_refund"}],
            },
            A.build_context(spans=[_tool("lookup")]),
        ),
        "count": (
            {"id": "a", "kind": "count", "span_type": "tool_call", "count": 1},
            A.build_context(spans=[_tool("lookup"), _tool("issue_refund")]),
        ),
    }


def test_case_matrix_covers_every_supported_deterministic_kind():
    assert set(_cases()) == set(A.KINDS).difference({
        "timing_contract", "dtmf", "http_result",
    })


@pytest.mark.parametrize("kind", sorted(_cases()))
def test_failure_atom_is_structured_stable_and_reason_free(kind):
    assertion, context = _cases()[kind]
    A.validate_assertions_doc({"version": 1, "assertions": [assertion]})
    result = A.evaluate_assertion(assertion, context)
    assert result["status"] == "FAIL", (kind, result)
    atoms = failure_atoms(assertion, result, context)
    assert atoms
    assert all(set(atom).issubset({"code", "detector", "rule", "type", "index", "field", "key"}) for atom in atoms)
    assert all("reason" not in atom for atom in atoms)
    one = failure_fingerprint("oracle-test", assertion, atoms[0], atoms)
    two = failure_fingerprint(
        "oracle-test",
        copy.deepcopy(assertion),
        copy.deepcopy(atoms[0]),
        copy.deepcopy(atoms),
    )
    assert one == two
    assert one["required_status"] == "FAIL"
    assert one["authority"] == "deterministic"


def test_atoms_ignore_payload_changes_and_distinguish_outcome_branches():
    pii_assertion, _ = _cases()["pii"]
    first_ctx = A.build_context(transcript=[{"role": "caller", "text": "one@example.com"}])
    second_ctx = A.build_context(transcript=[{"role": "caller", "text": "two@example.com"}])
    first = failure_atoms(
        pii_assertion, A.evaluate_assertion(pii_assertion, first_ctx), first_ctx
    )
    second = failure_atoms(
        pii_assertion, A.evaluate_assertion(pii_assertion, second_ctx), second_ctx
    )
    assert first == second == [{"code": "pii-detected", "detector": "email"}]

    outcome_assertion, _ = _cases()["outcome"]
    no_matches = A.build_context(spans=[_tool("lookup_order")], transcript=[{"role": "caller", "text": "waiting"}])
    phrase_only = A.build_context(spans=[_tool("lookup_order")], transcript=[{"role": "caller", "text": "confirmed"}])
    first_result = A.evaluate_assertion(outcome_assertion, no_matches)
    second_result = A.evaluate_assertion(outcome_assertion, phrase_only)
    assert first_result["status"] == second_result["status"] == "FAIL"
    assert failure_atoms(outcome_assertion, first_result, no_matches) != failure_atoms(
        outcome_assertion, second_result, phrase_only
    )


def test_tool_call_atom_distinguishes_wrong_arguments_from_missing_tool():
    assertion = {
        "id": "refund-call",
        "kind": "tool_call",
        "name": "issue_refund",
        "args_subset": {"id": "A"},
    }
    wrong = A.build_context(
        spans=[_tool("issue_refund", arguments={"id": "B"})]
    )
    missing = A.build_context(spans=[])
    wrong_result = A.evaluate_assertion(assertion, wrong)
    missing_result = A.evaluate_assertion(assertion, missing)
    assert wrong_result["status"] == missing_result["status"] == "FAIL"
    assert failure_atoms(assertion, wrong_result, wrong) == [
        {"code": "tool-argument-value-mismatch", "key": "id"}
    ]
    assert failure_atoms(assertion, missing_result, missing) == [
        {"code": "tool-missing"}
    ]


def test_phrase_atom_distinguishes_no_role_turns_from_nonmatching_turns():
    assertion = {
        "id": "disclosure",
        "kind": "phrase",
        "regex": "recorded",
        "role": "agent",
    }
    no_agent = A.build_context(
        transcript=[{"role": "caller", "text": "hello"}]
    )
    wrong_agent = A.build_context(
        transcript=[{"role": "agent", "text": "hello"}]
    )
    no_agent_result = A.evaluate_assertion(assertion, no_agent)
    wrong_agent_result = A.evaluate_assertion(assertion, wrong_agent)
    assert no_agent_result["status"] == wrong_agent_result["status"] == "FAIL"
    assert failure_atoms(assertion, no_agent_result, no_agent) == [
        {"code": "no-qualifying-turns"}
    ]
    assert failure_atoms(assertion, wrong_agent_result, wrong_agent) == [
        {"code": "required-match-missing"}
    ]


def test_oracle_reports_drifted_instead_of_absent_for_a_different_failure_identity():
    scenario = {
        "kind": "hotato.scenario", "version": 1, "id": "drift-source",
        "goal": {"type": "support", "target": "account"},
        "caller": {"script": [{"say": "one@example.com and 416-555-1212"}]},
    }
    test_doc = {
        "kind": "hotato.conversation-test", "version": 1,
        "id": "drift-test", "agent": "fixture-agent",
        "assertions": {
            "deterministic": [{
                "id": "pii", "kind": "pii", "detectors": ["email", "phone"],
                "mode": "must_not_leak",
            }],
            "rubric": [],
        },
    }
    assertion = target_assertion(test_doc, "pii")
    oracle = FailureOracle(test_doc, assertion, 0)
    oracle.freeze_source(scenario)
    changed = copy.deepcopy(scenario)
    changed["caller"]["script"][0]["say"] = "416-555-1212"
    result = oracle.evaluate(changed)
    assert result["status"] == "DRIFTED"
    assert result["code"] == "failure_identity_drift"
