"""Failure atoms are closed and coupled to their deterministic assertion kind."""

from __future__ import annotations

import copy
import json
from pathlib import Path

import pytest
from jsonschema import Draft7Validator

ROOT = Path(__file__).resolve().parents[1]
SCHEMA_DIR = ROOT / "src" / "hotato" / "schema"
DIGEST = "sha256:" + "0" * 64


KIND_ATOMS = {
    "phrase": [
        {"code": "forbidden-match"},
        {"code": "no-qualifying-turns"},
        {"code": "required-match-missing"},
    ],
    "pii": [
        {"code": "pii-detected", "detector": detector}
        for detector in ("ssn", "card_luhn", "email", "phone")
    ],
    "policy": [
        {
            "code": "policy-violation",
            "rule": "disclosure",
            "type": violation_type,
        }
        for violation_type in ("banned", "required_disclosure_missing")
    ],
    "tool_call": [
        {"code": code}
        for code in (
            "tool-missing",
            "tool-arguments-mismatch",
            "tool-count-below",
            "tool-count-above",
            "never-before-boundary-missing",
            "never-before-order-violation",
        )
    ]
    + [{"code": "order-step-missing", "index": 0}],
    "outcome": [
        {"code": "predicate-unmet", "index": 0},
        {"code": "no-predicate-met"},
    ],
    "tool_result": [
        {"code": code}
        for code in ("tool-missing", "result-missing", "result-subset-mismatch")
    ],
    "tool_error": [
        {"code": code}
        for code in (
            "tool-missing",
            "unexpected-tool-error",
            "tool-error-missing",
            "tool-error-pattern-mismatch",
        )
    ],
    "state": [
        {"code": "state-record-missing"},
        {"code": "state-field-missing", "field": "refund.status"},
        {"code": "state-field-value-mismatch", "field": "refund.status"},
    ],
    "state_change": [
        {"code": code, "field": "refund.status"}
        for code in (
            "before-field-missing",
            "before-value-mismatch",
            "after-field-missing",
            "after-value-mismatch",
            "state-unchanged",
        )
    ],
    "handoff": [
        {"code": code}
        for code in ("unexpected-handoff", "handoff-missing", "handoff-target-mismatch")
    ],
    "dtmf": [
        {"code": code}
        for code in ("unexpected-dtmf", "dtmf-missing", "dtmf-digits-mismatch")
    ],
    "termination": [
        {"code": code}
        for code in (
            "unexpected-termination",
            "termination-missing",
            "termination-attribute-mismatch",
        )
    ],
    "latency": [{"code": "latency-threshold-exceeded"}],
    "entity_accuracy": [
        {"code": code, "key": "order_id"}
        for code in ("entity-missing", "entity-value-mismatch")
    ],
    "sequence": [{"code": "sequence-step-missing", "index": 0}],
    "count": [{"code": "count-below"}, {"code": "count-above"}],
}

ATOM_CASES = [
    (kind, atom)
    for kind, atoms in KIND_ATOMS.items()
    for atom in atoms
]


def _target_validator(schema_name: str, definition: str) -> Draft7Validator:
    schema = json.loads((SCHEMA_DIR / schema_name).read_text(encoding="utf-8"))
    Draft7Validator.check_schema(schema)
    return Draft7Validator(
        {
            "$schema": "http://json-schema.org/draft-07/schema#",
            "$ref": f"#/definitions/{definition}",
            "definitions": schema["definitions"],
        }
    )


@pytest.fixture(scope="module", params=(
    ("counterexample.v1.json", "private_target"),
    ("counterexample-oracle.v1.json", "target"),
))
def target_validator(request) -> Draft7Validator:
    return _target_validator(*request.param)


@pytest.fixture(scope="module")
def share_target_validator() -> Draft7Validator:
    return _target_validator("counterexample.v1.json", "share_target")


def _target(kind: str, atom: dict) -> dict:
    return {
        "test_id": "failure-atom-contract",
        "assertion_digest": DIGEST,
        "assertion_id": "target",
        "kind": kind,
        "dimension": None,
        "authority": "deterministic",
        "required_status": "FAIL",
        "failure_atom": copy.deepcopy(atom),
        "source_failure_atoms": [copy.deepcopy(atom)],
        "fingerprint": DIGEST,
    }


def _share_target(kind: str, failure_code: str) -> dict:
    return {
        "assertion_ref": DIGEST,
        "kind": kind,
        "dimension": None,
        "authority": "deterministic",
        "required_status": "FAIL",
        "fingerprint": DIGEST,
        "failure_code": failure_code,
        "failure_atom_digest": DIGEST,
    }


@pytest.mark.parametrize(
    "kind,atom",
    ATOM_CASES,
    ids=[f"{kind}:{atom['code']}" for kind, atom in ATOM_CASES],
)
def test_every_closed_failure_branch_is_valid_only_for_its_kind(
    target_validator: Draft7Validator,
    kind: str,
    atom: dict,
) -> None:
    assert target_validator.is_valid(_target(kind, atom))

    wrong_kind = "pii" if kind == "phrase" else "phrase"
    assert not target_validator.is_valid(_target(wrong_kind, atom))


@pytest.mark.parametrize(
    "kind,atom",
    ATOM_CASES,
    ids=[f"share:{kind}:{atom['code']}" for kind, atom in ATOM_CASES],
)
def test_share_failure_code_is_readable_and_kind_coupled(
    share_target_validator: Draft7Validator,
    kind: str,
    atom: dict,
) -> None:
    assert share_target_validator.is_valid(_share_target(kind, atom["code"]))

    wrong_kind = "pii" if kind == "phrase" else "phrase"
    assert not share_target_validator.is_valid(
        _share_target(wrong_kind, atom["code"])
    )


def test_selected_and_complete_source_atom_set_are_both_kind_coupled(
    target_validator: Draft7Validator,
) -> None:
    target = _target("state", {"code": "state-record-missing"})
    target["source_failure_atoms"].append({"code": "tool-missing"})
    assert not target_validator.is_valid(target)


@pytest.mark.parametrize(
    "kind,retired_atom",
    [
        ("tool_call", {"code": "tool-count-not-equal"}),
        ("tool_call", {"code": "never-before-violation"}),
        ("tool_error", {"code": "required-tool-error-missing"}),
        ("state", {"code": "state-field-mismatch", "field": "status"}),
        ("handoff", {"code": "required-handoff-missing"}),
        ("dtmf", {"code": "required-dtmf-missing"}),
        ("termination", {"code": "required-termination-missing"}),
        ("entity_accuracy", {"code": "entity-mismatch", "key": "order_id"}),
        ("count", {"code": "count-not-equal"}),
    ],
    ids=lambda value: value["code"] if isinstance(value, dict) else value,
)
def test_retired_coarse_failure_branches_are_rejected(
    target_validator: Draft7Validator,
    kind: str,
    retired_atom: dict,
) -> None:
    assert not target_validator.is_valid(_target(kind, retired_atom))
