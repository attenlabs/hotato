"""Closed JSON Schema contracts for counterexample proof artifacts."""

from __future__ import annotations

import copy
import json
from pathlib import Path

import pytest
from jsonschema import Draft7Validator

from hotato.counterexample import compile_counterexample, export_counterexample

ROOT = Path(__file__).resolve().parents[1]
SCHEMA_DIR = ROOT / "src" / "hotato" / "schema"
FIXTURES = Path(__file__).parent / "fixtures" / "counterexample"

CAPSULE_SCHEMA = "counterexample.v1.json"
ORACLE_SCHEMA = "counterexample-oracle.v1.json"
CERTIFICATE_SCHEMA = "reduction-certificate.v1.json"


def _load(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


@pytest.fixture(scope="module")
def validators() -> dict[str, Draft7Validator]:
    out = {}
    for name in (CAPSULE_SCHEMA, ORACLE_SCHEMA, CERTIFICATE_SCHEMA):
        schema = _load(SCHEMA_DIR / name)
        Draft7Validator.check_schema(schema)
        out[name] = Draft7Validator(schema)
    return out


@pytest.fixture(scope="module")
def documents(tmp_path_factory) -> dict[str, dict]:
    root = tmp_path_factory.mktemp("counterexample-schema")
    private = root / "private.hotato-repro"
    compile_counterexample(
        str(FIXTURES / "pii.scenario.json"),
        str(FIXTURES / "pii.test.json"),
        target="pii-email",
        out_dir=str(private),
        workspace=str(FIXTURES),
    )
    share = root / "share-safe"
    export_counterexample(str(private), out_dir=str(share))
    return {
        "private": _load(private / "capsule.json"),
        "share": _load(share / "capsule.json"),
        "oracle": _load(private / "oracle.json"),
        "certificate": _load(private / "certificate.json"),
    }


def _assert_valid(validator: Draft7Validator, document: dict) -> None:
    errors = sorted(validator.iter_errors(document), key=lambda error: list(error.absolute_path))
    assert not errors, "\n".join(
        f"{list(error.absolute_path)!r}: {error.message}" for error in errors
    )


def _assert_invalid(validator: Draft7Validator, document: dict) -> None:
    assert not validator.is_valid(document)


def test_emitted_private_share_oracle_and_certificate_validate(validators, documents):
    _assert_valid(validators[CAPSULE_SCHEMA], documents["private"])
    _assert_valid(validators[CAPSULE_SCHEMA], documents["share"])
    _assert_valid(validators[ORACLE_SCHEMA], documents["oracle"])
    _assert_valid(validators[CERTIFICATE_SCHEMA], documents["certificate"])


@pytest.mark.parametrize(
    "stem,target",
    [
        ("policy", "guarantee-language"),
        ("latency", "refund-tool-latency"),
        ("state", "refund-posted"),
    ],
)
def test_other_reference_failure_artifacts_validate(validators, tmp_path, stem, target):
    output = tmp_path / f"{stem}.hotato-repro"
    compile_counterexample(
        str(FIXTURES / f"{stem}.scenario.json"),
        str(FIXTURES / f"{stem}.test.json"),
        target=target,
        out_dir=str(output),
        workspace=str(FIXTURES),
    )
    _assert_valid(validators[CAPSULE_SCHEMA], _load(output / "capsule.json"))
    _assert_valid(validators[ORACLE_SCHEMA], _load(output / "oracle.json"))
    _assert_valid(validators[CERTIFICATE_SCHEMA], _load(output / "certificate.json"))


@pytest.mark.parametrize("field", ["oracle", "artifacts", "artifact_digests"])
def test_private_capsule_requires_every_runnable_proof_reference(
    validators, documents, field
):
    document = copy.deepcopy(documents["private"])
    del document[field]
    _assert_invalid(validators[CAPSULE_SCHEMA], document)


@pytest.mark.parametrize("field", ["oracle", "artifacts", "artifact_digests"])
def test_share_capsule_forbids_runnable_proof_references(validators, documents, field):
    document = copy.deepcopy(documents["share"])
    document[field] = copy.deepcopy(documents["private"][field])
    _assert_invalid(validators[CAPSULE_SCHEMA], document)


def test_profile_branches_are_closed_and_cannot_be_hybridized(validators, documents):
    validator = validators[CAPSULE_SCHEMA]

    private = copy.deepcopy(documents["private"])
    del private["source"]["scenario_file_sha256"]
    _assert_invalid(validator, private)

    share = copy.deepcopy(documents["share"])
    share["source"]["scenario_file_sha256"] = documents["private"]["source"][
        "scenario_file_sha256"
    ]
    _assert_invalid(validator, share)

    share = copy.deepcopy(documents["share"])
    share["target"]["assertion_id"] = "pii-email"
    _assert_invalid(validator, share)

    private = copy.deepcopy(documents["private"])
    private["privacy"]["runnable"] = True
    _assert_invalid(validator, private)


@pytest.mark.parametrize(
    "malformed",
    [
        "0" * 64,
        "sha256:" + "A" * 64,
        "sha256:" + "0" * 63,
        "sha256:" + "0" * 65,
        "sha256:" + "g" * 64,
    ],
)
def test_prefixed_digest_grammar_is_exact(validators, documents, malformed):
    document = copy.deepcopy(documents["private"])
    document["counterexample_id"] = malformed
    _assert_invalid(validators[CAPSULE_SCHEMA], document)


def test_nested_digest_grammar_and_raw_content_hashes_are_distinct(validators, documents):
    validator = validators[CAPSULE_SCHEMA]

    document = copy.deepcopy(documents["private"])
    document["artifact_digests"]["certificate"] = "f" * 64
    _assert_invalid(validator, document)

    document = copy.deepcopy(documents["private"])
    document["preservation"]["source_content_hashes"][0] = "sha256:" + "f" * 64
    _assert_invalid(validator, document)

    document = copy.deepcopy(documents["private"])
    document["preservation"]["source_result_digests"][0] = "f" * 64
    _assert_invalid(validator, document)


def test_reduction_stats_and_replay_evidence_are_typed_and_closed(validators, documents):
    validator = validators[CAPSULE_SCHEMA]

    for mutation in (
        lambda doc: doc["reduction"]["initial"].__setitem__("turns", True),
        lambda doc: doc["reduction"].__setitem__("qualification_evaluations", 3),
        lambda doc: doc["reduction"].__setitem__("candidate_evaluations", 100001),
        lambda doc: doc["reduction"].__setitem__("mystery_counter", 1),
        lambda doc: doc["preservation"].__setitem__("source_executions", 1),
        lambda doc: doc["preservation"].__setitem__("overall_score", 1),
    ):
        document = copy.deepcopy(documents["private"])
        mutation(document)
        _assert_invalid(validator, document)


def test_minimality_status_claim_and_reduction_termination_are_coupled(validators, documents):
    validator = validators[CAPSULE_SCHEMA]

    document = copy.deepcopy(documents["private"])
    document["reduction"]["termination"] = "budget_exhausted"
    _assert_invalid(validator, document)

    document = copy.deepcopy(documents["private"])
    document["minimality"]["claim"] = "minimal enough"
    _assert_invalid(validator, document)

    document = copy.deepcopy(documents["private"])
    document["minimality"]["frozen_components"] = ["speaker_id"]
    _assert_invalid(validator, document)


def test_privacy_and_provenance_are_profile_specific_and_closed(validators, documents):
    validator = validators[CAPSULE_SCHEMA]

    document = copy.deepcopy(documents["share"])
    document["provenance"]["seed"] = 0
    _assert_invalid(validator, document)

    document = copy.deepcopy(documents["private"])
    document["provenance"]["evaluator_digest"] = "unverified"
    _assert_invalid(validator, document)

    document = copy.deepcopy(documents["private"])
    document["provenance"]["scenario_selection"]["variation_matrix_applied"] = True
    _assert_invalid(validator, document)

    document = copy.deepcopy(documents["share"])
    document["privacy"]["content_included"] = ["transcript"]
    _assert_invalid(validator, document)


ATOMS = [
    ("phrase", {"code": "required-match-missing"}),
    ("pii", {"code": "pii-detected", "detector": "email"}),
    (
        "policy",
        {
            "code": "policy-violation",
            "rule": "disclosure",
            "type": "required_disclosure_missing",
        },
    ),
    ("tool_call", {"code": "tool-arguments-mismatch"}),
    ("outcome", {"code": "predicate-unmet", "index": 0}),
    ("tool_result", {"code": "result-subset-mismatch"}),
    ("tool_error", {"code": "tool-error-missing"}),
    ("state", {"code": "state-field-value-mismatch", "field": "refund.status"}),
    ("state_change", {"code": "state-unchanged", "field": "balance"}),
    ("handoff", {"code": "handoff-missing"}),
    ("dtmf", {"code": "dtmf-missing"}),
    ("termination", {"code": "unexpected-termination"}),
    ("latency", {"code": "latency-threshold-exceeded"}),
    ("entity_accuracy", {"code": "entity-value-mismatch", "key": "order_id"}),
    ("sequence", {"code": "sequence-step-missing", "index": 1}),
    ("count", {"code": "count-below"}),
]


@pytest.mark.parametrize("kind,atom", ATOMS, ids=[row[0] for row in ATOMS])
def test_every_supported_failure_atom_validates_in_capsule_and_oracle(
    validators, documents, kind, atom
):
    capsule = copy.deepcopy(documents["private"])
    capsule["target"]["kind"] = kind
    capsule["target"]["failure_atom"] = copy.deepcopy(atom)
    capsule["target"]["source_failure_atoms"] = [copy.deepcopy(atom)]
    _assert_valid(validators[CAPSULE_SCHEMA], capsule)

    oracle = copy.deepcopy(documents["oracle"])
    oracle["target"]["kind"] = kind
    oracle["target"]["failure_atom"] = copy.deepcopy(atom)
    oracle["target"]["source_failure_atoms"] = [copy.deepcopy(atom)]
    _assert_valid(validators[ORACLE_SCHEMA], oracle)


def test_failure_atom_nested_closure_is_enforced(validators, documents):
    for schema_name, key in ((CAPSULE_SCHEMA, "private"), (ORACLE_SCHEMA, "oracle")):
        document = copy.deepcopy(documents[key])
        document["target"]["failure_atom"] = {"code": "unknown-branch"}
        _assert_invalid(validators[schema_name], document)

        document = copy.deepcopy(documents[key])
        document["target"]["failure_atom"]["aggregate_score"] = 1
        _assert_invalid(validators[schema_name], document)


def test_oracle_target_and_observation_scope_are_exact(validators, documents):
    validator = validators[ORACLE_SCHEMA]

    document = copy.deepcopy(documents["oracle"])
    document["target"]["speaker_id"] = "caller-1"
    _assert_invalid(validator, document)

    document = copy.deepcopy(documents["oracle"])
    document["target"]["dimension"] = "overall"
    _assert_invalid(validator, document)

    document = copy.deepcopy(documents["oracle"])
    document["observation_scope"]["rule"] = "same failure"
    _assert_invalid(validator, document)

    document = copy.deepcopy(documents["oracle"])
    document["observation_scope"]["frozen_components"] = ["script", "script"]
    _assert_invalid(validator, document)

    document = copy.deepcopy(documents["oracle"])
    document["observation_scope"]["minimum_caller_turns"] = 0
    _assert_invalid(validator, document)


def _step_with(document: dict, kind: str) -> dict:
    return next(
        step for step in document["accepted_steps"] if step["operation"]["kind"] == kind
    )


def test_certificate_steps_are_closed_and_digest_domains_are_distinct(validators, documents):
    validator = validators[CERTIFICATE_SCHEMA]

    document = copy.deepcopy(documents["certificate"])
    document["accepted_steps"][0]["parent_digest"] = "sha256:" + "0" * 64
    _assert_invalid(validator, document)

    document = copy.deepcopy(documents["certificate"])
    document["accepted_steps"][0]["oracle_result_digest"] = "0" * 64
    _assert_invalid(validator, document)

    document = copy.deepcopy(documents["certificate"])
    document["accepted_steps"][0]["unexpected"] = True
    _assert_invalid(validator, document)

    document = copy.deepcopy(documents["certificate"])
    document["candidate_evaluations"] = True
    _assert_invalid(validator, document)


def test_certificate_accepts_only_replayable_delete_only_transforms(validators, documents):
    validator = validators[CERTIFICATE_SCHEMA]

    document = copy.deepcopy(documents["certificate"])
    document["accepted_steps"][0]["transform"]["kind"] = "hotato.patch.v1"
    _assert_invalid(validator, document)

    document = copy.deepcopy(documents["certificate"])
    document["accepted_steps"][0]["transform"]["operations"] = []
    _assert_invalid(validator, document)

    document = copy.deepcopy(documents["certificate"])
    operation = document["accepted_steps"][0]["transform"]["operations"][0]
    operation["op"] = "replace"
    _assert_invalid(validator, document)

    document = copy.deepcopy(documents["certificate"])
    operations = document["accepted_steps"][0]["transform"]["operations"]
    operations.append(copy.deepcopy(operations[0]))
    _assert_invalid(validator, document)

    document = copy.deepcopy(documents["certificate"])
    document["accepted_steps"][0]["transform"]["operations"][0]["path"] = [True]
    _assert_invalid(validator, document)


def test_certificate_operation_union_rejects_ambiguous_or_invalid_operations(
    validators, documents
):
    validator = validators[CERTIFICATE_SCHEMA]

    document = copy.deepcopy(documents["certificate"])
    _step_with(document, "remove-field")["operation"]["removed_source_indices"] = [0]
    _assert_invalid(validator, document)

    document = copy.deepcopy(documents["certificate"])
    operation = _step_with(document, "remove-path-set")["operation"]
    operation["paths"] = ["caller.script[0]", "caller.script[0]"]
    _assert_invalid(validator, document)

    document = copy.deepcopy(documents["certificate"])
    operation = _step_with(document, "remove-single-unit")["operation"]
    operation["component"] = "speaker-id"
    _assert_invalid(validator, document)

    document = copy.deepcopy(documents["certificate"])
    _step_with(document, "remove-single-unit")["operation"]["phase"] = "arbitrary"
    _assert_invalid(validator, document)
