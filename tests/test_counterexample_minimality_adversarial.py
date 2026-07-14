"""Adversarial conformance tests for the reducers-v1 minimality inventory."""

from __future__ import annotations

import copy

import pytest

from hotato.counterexample.model import ABSENT, PRESERVED
from hotato.counterexample.oracle import FailureOracle, target_assertion
from hotato.counterexample.reducers import enumerate_units, final_single_unit_pass
from hotato.counterexample.search import SearchState


def _scenario() -> dict:
    return {
        "kind": "hotato.scenario",
        "version": 1,
        "id": "empty-container-proof",
        "goal": {"type": "support", "target": "account"},
        "caller": {
            "script": [{"say": "hello"}],
            "behavior": {
                "backchannels": {"probability": 1.0},
                "interruptions": [],
            },
        },
    }


def _paths(scenario: dict) -> set[tuple]:
    return {tuple(unit["path"]) for unit in enumerate_units(scenario, set())}


def test_empty_interruptions_field_remains_a_behavior_unit() -> None:
    scenario = _scenario()

    assert ("caller", "behavior", "interruptions") in _paths(scenario)


def test_nonempty_interruptions_are_item_units_without_a_whole_list_unit() -> None:
    scenario = _scenario()
    scenario["caller"]["behavior"]["interruptions"] = [
        {"trigger": "greeting", "offset_ms": 10},
        {"trigger": "resolution", "offset_ms": 20},
    ]

    paths = _paths(scenario)
    assert ("caller", "behavior", "interruptions", 0) in paths
    assert ("caller", "behavior", "interruptions", 1) in paths
    assert ("caller", "behavior", "interruptions") not in paths


@pytest.mark.parametrize(
    ("mutation", "expected_path"),
    [
        (lambda value: value.update({"facts": {}}), ("facts",)),
        (lambda value: value.update({"environment": {}}), ("environment",)),
        (lambda value: value.update({"variation_matrix": {}}), ("variation_matrix",)),
        (
            lambda value: value.update({"agent_mock": {"tools": []}}),
            ("agent_mock", "tools"),
        ),
        (
            lambda value: value.update({"agent_mock": {"state": {}}}),
            ("agent_mock", "state"),
        ),
        (
            lambda value: value.update({
                "agent_mock": {"state": {"orders": []}},
            }),
            ("agent_mock", "state", "orders"),
        ),
        (
            lambda value: value.update({
                "agent_mock": {"state": {"orders": {"before": []}}},
            }),
            ("agent_mock", "state", "orders", "before"),
        ),
    ],
)
def test_other_optional_empty_containers_are_already_in_the_closed_inventory(
    mutation, expected_path
) -> None:
    scenario = _scenario()
    mutation(scenario)

    assert expected_path in _paths(scenario)


def test_fixed_point_deletes_empty_interruptions_before_one_minimal_claim() -> None:
    scenario = _scenario()

    def evaluator(candidate: dict) -> dict:
        behavior = (candidate.get("caller") or {}).get("behavior") or {}
        if "backchannels" in behavior:
            return {"status": PRESERVED, "code": "same-count-failure"}
        return {"status": ABSENT, "code": "target-no-longer-fails"}

    state = SearchState(64, evaluator)
    reduced, checks, complete = final_single_unit_pass(
        copy.deepcopy(scenario), state, set()
    )

    assert complete is True
    assert "backchannels" in reduced["caller"]["behavior"]
    assert "interruptions" not in reduced["caller"]["behavior"]
    assert all(row["outcome"] != PRESERVED for row in checks)
    assert ("caller", "behavior", "interruptions") not in _paths(reduced)


def test_count_oracle_reproducer_cannot_certify_surviving_empty_interruptions() -> None:
    scenario = _scenario()
    test_doc = {
        "kind": "hotato.conversation-test",
        "version": 1,
        "id": "empty-container-test",
        "agent": "fixture-agent",
        "assertions": {
            "deterministic": [{
                "id": "zero-backchannels",
                "kind": "count",
                "span_type": "backchannel",
                "count": 0,
            }],
            "rubric": [],
        },
    }
    assertion = target_assertion(test_doc, "zero-backchannels")
    oracle = FailureOracle(test_doc, assertion, 0)
    oracle.freeze_source(scenario)
    state = SearchState(64, oracle.evaluate)

    reduced, checks, complete = final_single_unit_pass(
        copy.deepcopy(scenario), state, oracle.frozen
    )

    assert complete is True
    assert oracle.evaluate(reduced)["status"] == PRESERVED
    assert "backchannels" in reduced["caller"]["behavior"]
    assert "interruptions" not in reduced["caller"]["behavior"]
    assert all(row["outcome"] != PRESERVED for row in checks)
