"""Typed failure-oracle coverage for every reducers-v1 assertion kind."""

from __future__ import annotations

import copy

import pytest

from hotato import assert_ as A
from hotato.counterexample.model import canonical_json
from hotato.counterexample.oracle import FailureOracle, failure_fingerprint, target_assertion, typed_witness
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
        "dtmf": (
            {"id": "a", "kind": "dtmf", "digits": "99"},
            A.build_context(spans=[{"type": "dtmf", "digits": "42"}]),
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
    assert set(_cases()) == set(A.KINDS).difference({"timing_contract"})


@pytest.mark.parametrize("kind", sorted(_cases()))
def test_typed_witness_is_structured_stable_and_reason_free(kind):
    assertion, context = _cases()[kind]
    A.validate_assertions_doc({"version": 1, "assertions": [assertion]})
    result = A.evaluate_assertion(assertion, context)
    assert result["status"] == "FAIL", (kind, result)
    witness = typed_witness(assertion, result, context)
    assert witness["type"] == f"{kind}-failure"
    assert "reason" not in canonical_json(witness).lower()
    one = failure_fingerprint("oracle-test", assertion, witness)
    two = failure_fingerprint("oracle-test", copy.deepcopy(assertion), copy.deepcopy(witness))
    assert one == two
    assert one["required_status"] == "FAIL"
    assert one["authority"] == "deterministic"


def test_pii_and_outcome_witnesses_reject_same_count_different_failure_lineage():
    pii_assertion, _ = _cases()["pii"]
    first_ctx = A.build_context(transcript=[{"role": "caller", "text": "one@example.com"}])
    second_ctx = A.build_context(transcript=[{"role": "caller", "text": "two@example.com"}])
    first = typed_witness(pii_assertion, A.evaluate_assertion(pii_assertion, first_ctx), first_ctx)
    second = typed_witness(pii_assertion, A.evaluate_assertion(pii_assertion, second_ctx), second_ctx)
    assert first["hits"] == second["hits"] == 1
    assert first != second

    outcome_assertion, _ = _cases()["outcome"]
    no_matches = A.build_context(spans=[_tool("lookup_order")], transcript=[{"role": "caller", "text": "waiting"}])
    phrase_only = A.build_context(spans=[_tool("lookup_order")], transcript=[{"role": "caller", "text": "confirmed"}])
    first_result = A.evaluate_assertion(outcome_assertion, no_matches)
    second_result = A.evaluate_assertion(outcome_assertion, phrase_only)
    assert first_result["status"] == second_result["status"] == "FAIL"
    assert typed_witness(outcome_assertion, first_result, no_matches) != typed_witness(
        outcome_assertion, second_result, phrase_only,
    )


def test_oracle_reports_drifted_instead_of_absent_for_a_different_failure_identity():
    scenario = {
        "kind": "hotato.scenario", "version": 1, "id": "drift-source",
        "goal": {"type": "support", "target": "account"},
        "caller": {"script": [{"say": "one@example.com"}]},
    }
    test_doc = {
        "kind": "hotato.conversation-test", "version": 1,
        "id": "drift-test", "agent": "fixture-agent",
        "assertions": {
            "deterministic": [{
                "id": "pii", "kind": "pii", "detectors": ["email"],
                "mode": "must_not_leak",
            }],
            "rubric": [],
        },
    }
    assertion = target_assertion(test_doc, "pii")
    oracle = FailureOracle(test_doc, assertion, 0)
    oracle.freeze_source(scenario)
    changed = copy.deepcopy(scenario)
    changed["caller"]["script"][0]["say"] = "two@example.com"
    result = oracle.evaluate(changed)
    assert result["status"] == "DRIFTED"
    assert result["code"] == "failure_identity_drift"
