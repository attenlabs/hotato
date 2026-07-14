"""Adversarial coverage for exact counterexample failure-branch identity.

Each pair below has the same assertion status but a different deterministic
failure mechanism.  The compiler must preserve the source branch instead of
shrinking a concrete defect into a coarser missing-evidence defect.
"""

from __future__ import annotations

import json
from typing import Any

from hotato import assert_ as A
from hotato.counterexample import compile_counterexample, verify_counterexample
from hotato.counterexample.model import canonical_json
from hotato.counterexample.oracle import (
    FAILURE_ATOM_FIELDS,
    FailureOracle,
    failure_atoms,
    target_assertion,
)
from hotato.state_adapter import MockStateAdapter


def _tool(name: str, **fields: Any) -> dict[str, Any]:
    span: dict[str, Any] = {"type": "tool_call", "name": name}
    span.update(fields)
    return span


def _atom(assertion: dict[str, Any], context: A.Context) -> dict[str, Any]:
    A.validate_assertions_doc({"version": 1, "assertions": [assertion]})
    result = A.evaluate_assertion(assertion, context)
    assert result["status"] == "FAIL", result
    atoms = failure_atoms(assertion, result, context)
    assert len(atoms) == 1, atoms
    return atoms[0]


def _compile(
    tmp_path,
    *,
    assertion: dict[str, Any],
    agent_mock: dict[str, Any],
    stem: str,
):
    scenario = {
        "kind": "hotato.scenario",
        "version": 1,
        "id": f"{stem}-scenario",
        "goal": {"type": "branch-soundness", "target": stem},
        "caller": {"script": [{"say": "Exercise the declared test path."}]},
        "agent_mock": agent_mock,
    }
    test_doc = {
        "kind": "hotato.conversation-test",
        "version": 1,
        "id": f"{stem}-test",
        "agent": "fixture-agent",
        "assertions": {"deterministic": [assertion], "rubric": []},
    }
    scenario_path = tmp_path / f"{stem}.scenario.json"
    test_path = tmp_path / f"{stem}.test.json"
    out = tmp_path / f"{stem}.hotato-repro"
    scenario_path.write_text(canonical_json(scenario, pretty=True), encoding="utf-8")
    test_path.write_text(canonical_json(test_doc, pretty=True), encoding="utf-8")

    result = compile_counterexample(
        str(scenario_path),
        str(test_path),
        target=assertion["id"],
        out_dir=str(out),
        workspace=str(tmp_path),
    )
    assert result["exit_code"] == 0, result
    assert verify_counterexample(str(out))["ok"] is True
    reduced = json.loads((out / "input" / "scenario.json").read_text("utf-8"))
    return result, reduced


def test_state_atoms_separate_record_field_and_value_absence():
    assertion = {
        "id": "state-target",
        "kind": "state",
        "resource": "orders",
        "filters": {"id": "A-1"},
        "expect": {"status": "posted"},
    }
    record_missing = A.build_context(
        state_adapter=MockStateAdapter({"orders": []})
    )
    field_missing = A.build_context(
        state_adapter=MockStateAdapter({"orders": [{"id": "A-1"}]})
    )
    wrong_value = A.build_context(
        state_adapter=MockStateAdapter(
            {"orders": [{"id": "A-1", "status": "pending"}]}
        )
    )

    assert _atom(assertion, record_missing) == {"code": "state-record-missing"}
    assert _atom(assertion, field_missing) == {
        "code": "state-field-missing",
        "field": "status",
    }
    assert _atom(assertion, wrong_value) == {
        "code": "state-field-value-mismatch",
        "field": "status",
    }


def test_state_wrong_value_compile_retains_wrong_value_evidence(tmp_path):
    assertion = {
        "id": "state-target",
        "kind": "state",
        "resource": "orders",
        "filters": {"id": "A-1"},
        "expect": {"status": "posted"},
    }
    result, reduced = _compile(
        tmp_path,
        assertion=assertion,
        agent_mock={
            "state": {
                "orders": [
                    {"id": "A-1", "status": "pending", "irrelevant": True},
                    {"id": "A-2", "status": "posted"},
                ]
            }
        },
        stem="state-wrong-value",
    )

    assert result["target"]["failure_atom"] == {
        "code": "state-field-value-mismatch",
        "field": "status",
    }
    rows = reduced["agent_mock"]["state"]["orders"]
    assert rows == [{"id": "A-1", "status": "pending"}]


def test_tool_result_atoms_separate_tool_result_and_subset_absence():
    assertion = {
        "id": "result-target",
        "kind": "tool_result",
        "name": "issue_refund",
        "result_subset": {"status": "posted"},
    }

    assert _atom(assertion, A.build_context(spans=[])) == {
        "code": "tool-missing"
    }
    assert _atom(
        assertion,
        A.build_context(spans=[_tool("issue_refund")]),
    ) == {"code": "result-missing"}
    assert _atom(
        assertion,
        A.build_context(
            spans=[_tool("issue_refund", result={"status": "pending"})]
        ),
    ) == {"code": "result-subset-mismatch"}


def test_tool_result_subset_mismatch_compile_retains_tool(tmp_path):
    assertion = {
        "id": "result-target",
        "kind": "tool_result",
        "name": "issue_refund",
        "result_subset": {"status": "posted"},
    }
    result, reduced = _compile(
        tmp_path,
        assertion=assertion,
        agent_mock={
            "tools": [
                {
                    "name": "issue_refund",
                    "arguments": {"id": "A-1"},
                    "result": {"status": "pending", "irrelevant": True},
                },
                {"name": "unrelated", "result": {"ok": True}},
            ]
        },
        stem="result-subset-mismatch",
    )

    assert result["target"]["failure_atom"] == {
        "code": "result-subset-mismatch"
    }
    assert [tool["name"] for tool in reduced["agent_mock"]["tools"]] == [
        "issue_refund"
    ]


def test_entity_atoms_separate_missing_from_wrong_values():
    assertion = {
        "id": "entity-target",
        "kind": "entity_accuracy",
        "reference": {"order_id": "A-1"},
    }
    missing = A.build_context(
        spans=[_tool("lookup", arguments={"unrelated": "value"})]
    )
    wrong = A.build_context(
        spans=[_tool("lookup", arguments={"order_id": "B-2"})]
    )

    assert _atom(assertion, missing) == {
        "code": "entity-missing",
        "key": "order_id",
    }
    assert _atom(assertion, wrong) == {
        "code": "entity-value-mismatch",
        "key": "order_id",
    }


def test_entity_wrong_value_compile_retains_wrong_argument(tmp_path):
    assertion = {
        "id": "entity-target",
        "kind": "entity_accuracy",
        "reference": {"order_id": "A-1"},
    }
    result, reduced = _compile(
        tmp_path,
        assertion=assertion,
        agent_mock={
            "tools": [
                {
                    "name": "lookup",
                    "arguments": {"order_id": "B-2", "irrelevant": "drop-me"},
                    "result": {"found": True},
                },
                {"name": "unrelated", "arguments": {"noise": "drop-me"}},
            ]
        },
        stem="entity-wrong-value",
    )

    assert result["target"]["failure_atom"] == {
        "code": "entity-value-mismatch",
        "key": "order_id",
    }
    tools = reduced["agent_mock"]["tools"]
    assert len(tools) == 1
    assert tools[0]["arguments"]["order_id"] == "B-2"


def test_exact_count_atoms_separate_above_from_below():
    assertion = {
        "id": "count-target",
        "kind": "count",
        "span_type": "tool_call",
        "count": 1,
    }

    assert _atom(assertion, A.build_context(spans=[])) == {"code": "count-below"}
    assert _atom(
        assertion,
        A.build_context(spans=[_tool("one"), _tool("two")]),
    ) == {"code": "count-above"}


def test_count_above_compile_cannot_cross_to_count_below(tmp_path):
    assertion = {
        "id": "count-target",
        "kind": "count",
        "span_type": "tool_call",
        "count": 1,
    }
    result, reduced = _compile(
        tmp_path,
        assertion=assertion,
        agent_mock={
            "tools": [
                {"name": "one"},
                {"name": "two"},
                {"name": "three"},
            ]
        },
        stem="count-above",
    )

    assert result["target"]["failure_atom"] == {"code": "count-above"}
    assert len(reduced["agent_mock"]["tools"]) == 2


def test_tool_error_atoms_separate_missing_tool_error_and_pattern():
    assertion = {
        "id": "error-target",
        "kind": "tool_error",
        "name": "charge_card",
        "error_matches": "declined",
    }

    assert _atom(assertion, A.build_context(spans=[])) == {
        "code": "tool-missing"
    }
    assert _atom(
        assertion,
        A.build_context(spans=[_tool("charge_card", result={"ok": True})]),
    ) == {"code": "tool-error-missing"}
    assert _atom(
        assertion,
        A.build_context(spans=[_tool("charge_card", error="timeout")]),
    ) == {"code": "tool-error-pattern-mismatch"}


def test_tool_error_pattern_mismatch_compile_retains_error(tmp_path):
    assertion = {
        "id": "error-target",
        "kind": "tool_error",
        "name": "charge_card",
        "error_matches": "declined",
    }
    result, reduced = _compile(
        tmp_path,
        assertion=assertion,
        agent_mock={
            "tools": [
                {"name": "charge_card", "error": "timeout"},
                {"name": "unrelated", "result": {"ok": True}},
            ]
        },
        stem="error-pattern-mismatch",
    )

    assert result["target"]["failure_atom"] == {
        "code": "tool-error-pattern-mismatch"
    }
    assert reduced["agent_mock"]["tools"] == [
        {"name": "charge_card", "error": "timeout"}
    ]


def test_handoff_atoms_separate_missing_from_target_mismatch():
    assertion = {
        "id": "handoff-target",
        "kind": "handoff",
        "to": "billing",
    }

    assert _atom(assertion, A.build_context(spans=[])) == {
        "code": "handoff-missing"
    }
    assert _atom(
        assertion,
        A.build_context(spans=[{"type": "handoff", "to": "support"}]),
    ) == {"code": "handoff-target-mismatch"}


def test_handoff_target_mismatch_compile_retains_handoff(tmp_path):
    assertion = {
        "id": "handoff-target",
        "kind": "handoff",
        "to": "billing",
    }
    result, reduced = _compile(
        tmp_path,
        assertion=assertion,
        agent_mock={"handoff": {"to": "support"}},
        stem="handoff-target-mismatch",
    )

    assert result["target"]["failure_atom"] == {
        "code": "handoff-target-mismatch"
    }
    assert reduced["agent_mock"]["handoff"] == {"to": "support"}


def test_termination_atoms_separate_missing_from_attribute_mismatch():
    assertion = {
        "id": "termination-target",
        "kind": "termination",
        "reason": "dropped",
    }

    assert _atom(assertion, A.build_context(spans=[])) == {
        "code": "termination-missing"
    }
    assert _atom(
        assertion,
        A.build_context(
            spans=[{"type": "termination", "reason": "completed"}]
        ),
    ) == {"code": "termination-attribute-mismatch"}


def test_termination_attribute_mismatch_compile_retains_termination(tmp_path):
    assertion = {
        "id": "termination-target",
        "kind": "termination",
        "reason": "dropped",
    }
    result, reduced = _compile(
        tmp_path,
        assertion=assertion,
        agent_mock={"termination": {"reason": "completed"}},
        stem="termination-attribute-mismatch",
    )

    assert result["target"]["failure_atom"] == {
        "code": "termination-attribute-mismatch"
    }
    assert reduced["agent_mock"]["termination"] == {"reason": "completed"}


def test_dtmf_atoms_separate_missing_from_digits_mismatch():
    assertion = {
        "id": "dtmf-target",
        "kind": "dtmf",
        "digits": "99",
    }

    assert _atom(assertion, A.build_context(spans=[])) == {
        "code": "dtmf-missing"
    }
    assert _atom(
        assertion,
        A.build_context(spans=[{"type": "dtmf", "digits": "42"}]),
    ) == {"code": "dtmf-digits-mismatch"}


def test_never_before_atoms_separate_missing_boundary_from_order_violation():
    assertion = {
        "id": "order-target",
        "kind": "tool_call",
        "never_before": {"tool": "issue_refund", "until": "verify_identity"},
    }
    boundary_missing = A.build_context(spans=[_tool("issue_refund")])
    wrong_order = A.build_context(
        spans=[_tool("issue_refund"), _tool("verify_identity")]
    )

    assert _atom(assertion, boundary_missing) == {
        "code": "never-before-boundary-missing"
    }
    assert _atom(assertion, wrong_order) == {
        "code": "never-before-order-violation"
    }


def test_never_before_order_violation_compile_retains_boundary(tmp_path):
    assertion = {
        "id": "order-target",
        "kind": "tool_call",
        "never_before": {"tool": "issue_refund", "until": "verify_identity"},
    }
    result, reduced = _compile(
        tmp_path,
        assertion=assertion,
        agent_mock={
            "tools": [
                {"name": "issue_refund"},
                {"name": "verify_identity"},
                {"name": "unrelated"},
            ]
        },
        stem="never-before-order-violation",
    )

    assert result["target"]["failure_atom"] == {
        "code": "never-before-order-violation"
    }
    assert [tool["name"] for tool in reduced["agent_mock"]["tools"]] == [
        "issue_refund",
        "verify_identity",
    ]


def test_runtime_branch_matrix_exercises_every_closed_failure_code():
    cases: dict[
        tuple[str, str],
        tuple[dict[str, Any], A.Context, dict[str, Any]],
    ] = {}

    def add(
        assertion: dict[str, Any],
        context: A.Context,
        expected: dict[str, Any],
    ) -> None:
        assertion = {"id": f"branch-{len(cases)}", **assertion}
        key = (assertion["kind"], expected["code"])
        assert key not in cases
        cases[key] = assertion, context, expected

    add(
        {"kind": "phrase", "regex": "blocked", "absent": True},
        A.build_context(transcript=[{"role": "caller", "text": "blocked"}]),
        {"code": "forbidden-match"},
    )
    add(
        {"kind": "phrase", "regex": "recorded", "role": "agent"},
        A.build_context(transcript=[{"role": "caller", "text": "hello"}]),
        {"code": "no-qualifying-turns"},
    )
    add(
        {"kind": "phrase", "regex": "recorded", "role": "agent"},
        A.build_context(transcript=[{"role": "agent", "text": "hello"}]),
        {"code": "required-match-missing"},
    )
    add(
        {"kind": "pii", "detectors": ["email"], "mode": "must_not_leak"},
        A.build_context(
            transcript=[{"role": "caller", "text": "person@example.com"}]
        ),
        {"code": "pii-detected", "detector": "email"},
    )
    add(
        {"kind": "policy", "rule_ids": ["no-guarantee-language"]},
        A.build_context(
            transcript=[{"role": "caller", "text": "I guarantee delivery"}]
        ),
        {
            "code": "policy-violation",
            "rule": "no-guarantee-language",
            "type": "banned",
        },
    )
    add(
        {"kind": "tool_call", "name": "refund"},
        A.build_context(spans=[]),
        {"code": "tool-missing"},
    )
    add(
        {
            "kind": "tool_call",
            "name": "refund",
            "args_subset": {"id": "A"},
        },
        A.build_context(spans=[_tool("refund", arguments={"id": "B"})]),
        {"code": "tool-arguments-mismatch"},
    )
    add(
        {"kind": "tool_call", "name": "refund", "count": 1},
        A.build_context(spans=[]),
        {"code": "tool-count-below"},
    )
    add(
        {"kind": "tool_call", "name": "refund", "count": 1},
        A.build_context(spans=[_tool("refund"), _tool("refund")]),
        {"code": "tool-count-above"},
    )
    add(
        {
            "kind": "tool_call",
            "require_order": ["lookup", "refund"],
        },
        A.build_context(spans=[_tool("lookup")]),
        {"code": "order-step-missing", "index": 1},
    )
    add(
        {
            "kind": "tool_call",
            "never_before": {"tool": "refund", "until": "verify"},
        },
        A.build_context(spans=[_tool("refund")]),
        {"code": "never-before-boundary-missing"},
    )
    add(
        {
            "kind": "tool_call",
            "never_before": {"tool": "refund", "until": "verify"},
        },
        A.build_context(spans=[_tool("refund"), _tool("verify")]),
        {"code": "never-before-order-violation"},
    )
    add(
        {"kind": "outcome", "all_of": [{"tool_called": "refund"}]},
        A.build_context(spans=[]),
        {"code": "predicate-unmet", "index": 0},
    )
    add(
        {"kind": "outcome", "any_of": [{"tool_called": "refund"}]},
        A.build_context(spans=[]),
        {"code": "no-predicate-met"},
    )
    result_assertion = {
        "kind": "tool_result",
        "name": "refund",
        "result_subset": {"status": "posted"},
    }
    add(result_assertion, A.build_context(spans=[]), {"code": "tool-missing"})
    add(
        result_assertion,
        A.build_context(spans=[_tool("refund")]),
        {"code": "result-missing"},
    )
    add(
        result_assertion,
        A.build_context(spans=[_tool("refund", result={"status": "pending"})]),
        {"code": "result-subset-mismatch"},
    )
    error_assertion = {
        "kind": "tool_error",
        "name": "charge",
        "error_matches": "declined",
    }
    add(error_assertion, A.build_context(spans=[]), {"code": "tool-missing"})
    add(
        error_assertion,
        A.build_context(spans=[_tool("charge", result={"ok": True})]),
        {"code": "tool-error-missing"},
    )
    add(
        error_assertion,
        A.build_context(spans=[_tool("charge", error="timeout")]),
        {"code": "tool-error-pattern-mismatch"},
    )
    add(
        {
            "kind": "tool_error",
            "name": "charge",
            "absent": True,
        },
        A.build_context(spans=[_tool("charge", error="declined")]),
        {"code": "unexpected-tool-error"},
    )
    state_assertion = {
        "kind": "state",
        "resource": "orders",
        "filters": {"id": "A"},
        "expect": {"status": "posted"},
    }
    add(
        state_assertion,
        A.build_context(state_adapter=MockStateAdapter({"orders": []})),
        {"code": "state-record-missing"},
    )
    add(
        state_assertion,
        A.build_context(
            state_adapter=MockStateAdapter({"orders": [{"id": "A"}]})
        ),
        {"code": "state-field-missing", "field": "status"},
    )
    add(
        state_assertion,
        A.build_context(
            state_adapter=MockStateAdapter(
                {"orders": [{"id": "A", "status": "pending"}]}
            )
        ),
        {"code": "state-field-value-mismatch", "field": "status"},
    )

    def state_change_context(before: dict, after: dict) -> A.Context:
        return A.build_context(
            state_adapter=MockStateAdapter(
                {
                    "account": {
                        "before": [{"id": "U", **before}],
                        "after": [{"id": "U", **after}],
                    }
                }
            )
        )

    add(
        {
            "kind": "state_change",
            "resource": "account",
            "filters": {"id": "U"},
            "field": "balance",
            "from": 1,
        },
        state_change_context({}, {"balance": 2}),
        {"code": "before-field-missing", "field": "balance"},
    )
    add(
        {
            "kind": "state_change",
            "resource": "account",
            "filters": {"id": "U"},
            "field": "balance",
            "from": 1,
        },
        state_change_context({"balance": 0}, {"balance": 2}),
        {"code": "before-value-mismatch", "field": "balance"},
    )
    add(
        {
            "kind": "state_change",
            "resource": "account",
            "filters": {"id": "U"},
            "field": "balance",
            "to": 2,
        },
        state_change_context({"balance": 1}, {}),
        {"code": "after-field-missing", "field": "balance"},
    )
    add(
        {
            "kind": "state_change",
            "resource": "account",
            "filters": {"id": "U"},
            "field": "balance",
            "to": 2,
        },
        state_change_context({"balance": 1}, {"balance": 3}),
        {"code": "after-value-mismatch", "field": "balance"},
    )
    add(
        {
            "kind": "state_change",
            "resource": "account",
            "filters": {"id": "U"},
            "field": "balance",
            "changed": True,
        },
        state_change_context({"balance": 1}, {"balance": 1}),
        {"code": "state-unchanged", "field": "balance"},
    )
    add(
        {"kind": "handoff", "to": "billing"},
        A.build_context(spans=[]),
        {"code": "handoff-missing"},
    )
    add(
        {"kind": "handoff", "to": "billing"},
        A.build_context(spans=[{"type": "handoff", "to": "support"}]),
        {"code": "handoff-target-mismatch"},
    )
    add(
        {"kind": "handoff", "to": "billing", "absent": True},
        A.build_context(spans=[{"type": "handoff", "to": "billing"}]),
        {"code": "unexpected-handoff"},
    )
    add(
        {"kind": "dtmf", "digits": "99"},
        A.build_context(spans=[]),
        {"code": "dtmf-missing"},
    )
    add(
        {"kind": "dtmf", "digits": "99"},
        A.build_context(spans=[{"type": "dtmf", "digits": "42"}]),
        {"code": "dtmf-digits-mismatch"},
    )
    add(
        {"kind": "dtmf", "digits": "99", "absent": True},
        A.build_context(spans=[{"type": "dtmf", "digits": "99"}]),
        {"code": "unexpected-dtmf"},
    )
    add(
        {"kind": "termination", "reason": "dropped"},
        A.build_context(spans=[]),
        {"code": "termination-missing"},
    )
    add(
        {"kind": "termination", "reason": "dropped"},
        A.build_context(
            spans=[{"type": "termination", "reason": "complete"}]
        ),
        {"code": "termination-attribute-mismatch"},
    )
    add(
        {"kind": "termination", "reason": "dropped", "absent": True},
        A.build_context(
            spans=[{"type": "termination", "reason": "dropped"}]
        ),
        {"code": "unexpected-termination"},
    )
    add(
        {"kind": "latency", "tool": "refund", "max_ms": 100},
        A.build_context(spans=[_tool("refund", latency_ms=200)]),
        {"code": "latency-threshold-exceeded"},
    )
    entity_assertion = {
        "kind": "entity_accuracy",
        "reference": {"order_id": "A"},
    }
    add(
        entity_assertion,
        A.build_context(spans=[_tool("lookup", arguments={})]),
        {"code": "entity-missing", "key": "order_id"},
    )
    add(
        entity_assertion,
        A.build_context(spans=[_tool("lookup", arguments={"order_id": "B"})]),
        {"code": "entity-value-mismatch", "key": "order_id"},
    )
    add(
        {
            "kind": "sequence",
            "steps": [{"tool": "lookup"}, {"tool": "refund"}],
        },
        A.build_context(spans=[_tool("lookup")]),
        {"code": "sequence-step-missing", "index": 1},
    )
    count_assertion = {
        "kind": "count",
        "span_type": "tool_call",
        "count": 1,
    }
    add(count_assertion, A.build_context(spans=[]), {"code": "count-below"})
    add(
        count_assertion,
        A.build_context(spans=[_tool("one"), _tool("two")]),
        {"code": "count-above"},
    )

    runtime_codes = {
        (kind, code)
        for kind, branches in FAILURE_ATOM_FIELDS.items()
        for code in branches
    }
    assert set(cases) == runtime_codes
    for assertion, context, expected in cases.values():
        assert _atom(assertion, context) == expected


def _state_oracle(expect: dict[str, Any]) -> tuple[FailureOracle, dict[str, Any]]:
    test_doc = {
        "kind": "hotato.conversation-test",
        "version": 1,
        "id": "multi-branch-test",
        "agent": "fixture-agent",
        "assertions": {
            "deterministic": [
                {
                    "id": "state-target",
                    "kind": "state",
                    "resource": "orders",
                    "filters": {"id": "A"},
                    "expect": expect,
                }
            ],
            "rubric": [],
        },
    }
    assertion = target_assertion(test_doc, "state-target")
    return FailureOracle(test_doc, assertion, 0), test_doc


def test_oracle_preserves_source_anchor_when_candidate_first_atom_differs():
    oracle, _ = _state_oracle({"a": 1, "b": 1})
    source = {
        "kind": "hotato.scenario",
        "version": 1,
        "id": "multi-branch-source",
        "goal": {"type": "support", "target": "account"},
        "caller": {"script": [{"say": "help"}]},
        "agent_mock": {
            "state": {"orders": [{"id": "A", "a": 0, "b": 0}]}
        },
    }
    frozen = oracle.freeze_source(source)
    assert frozen["failure_atom"] == {
        "code": "state-field-value-mismatch",
        "field": "a",
    }

    candidate = json.loads(json.dumps(source))
    del candidate["agent_mock"]["state"]["orders"][0]["b"]
    result = oracle.evaluate(candidate)
    assert result["failure_atoms"][0] == {
        "code": "state-field-missing",
        "field": "b",
    }
    assert result["status"] == "PRESERVED"
    assert result["failure_atom"] == frozen["failure_atom"]


def test_oracle_reports_drift_when_wrong_state_value_becomes_missing():
    oracle, _ = _state_oracle({"status": "posted"})
    source = {
        "kind": "hotato.scenario",
        "version": 1,
        "id": "state-drift-source",
        "goal": {"type": "support", "target": "account"},
        "caller": {"script": [{"say": "help"}]},
        "agent_mock": {
            "state": {
                "orders": [{"id": "A", "status": "pending"}]
            }
        },
    }
    oracle.freeze_source(source)
    candidate = json.loads(json.dumps(source))
    del candidate["agent_mock"]["state"]["orders"][0]["status"]
    result = oracle.evaluate(candidate)
    assert result["status"] == "DRIFTED"
    assert result["code"] == "failure_identity_drift"
