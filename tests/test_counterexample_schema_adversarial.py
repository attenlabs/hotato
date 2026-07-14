"""Adversarial runtime/schema and capsule-integrity regression probes."""

from __future__ import annotations

import copy
import json
import shutil
import sys
from pathlib import Path
from typing import Optional

import pytest
from jsonschema import Draft7Validator

from hotato.counterexample import (
    CounterexampleRefusal,
    compile_counterexample,
    export_counterexample,
    inspect_counterexample,
    predicate_counterexample,
    reproduce_counterexample,
    verify_counterexample,
)
from hotato.counterexample.model import (
    canonical_json,
    inventory_files,
    prefixed_digest,
    sha256_bytes,
)
from hotato.counterexample.oracle import failure_identity_digest

ROOT = Path(__file__).resolve().parents[1]
FIXTURES = Path(__file__).parent / "fixtures" / "counterexample"
SCHEMAS = ROOT / "src" / "hotato" / "schema"


def _read_json(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def _write_json(path: Path, value: object) -> None:
    path.write_text(canonical_json(value, pretty=True), encoding="utf-8")


def _capsule_id(capsule: dict) -> str:
    value = copy.deepcopy(capsule)
    value.pop("counterexample_id", None)
    return prefixed_digest(value)


def _seal_manifest(root: Path) -> None:
    manifest = {
        "kind": "hotato.counterexample-manifest.v1",
        "version": 1,
        "algorithm": "sha256",
        "files": inventory_files(str(root), exclude=("MANIFEST.sha256.json",)),
    }
    _write_json(root / "MANIFEST.sha256.json", manifest)


def _seal_capsule(root: Path, capsule: dict) -> dict:
    capsule["counterexample_id"] = _capsule_id(capsule)
    _write_json(root / "capsule.json", capsule)
    _seal_manifest(root)
    return capsule


def _refresh_private_bindings(root: Path, capsule: Optional[dict] = None) -> dict:
    capsule = _read_json(root / "capsule.json") if capsule is None else capsule
    artifacts = capsule["artifacts"]
    byte_members = {
        "source_scenario_file",
        "source_conversation_test_file",
        "journal",
        "report_markdown",
        "report_html",
        "share_card",
        "reproduce_script",
        "predicate_script",
    }
    for name, relative in artifacts.items():
        path = root / relative
        if name in byte_members:
            capsule["artifact_digests"][name] = "sha256:" + sha256_bytes(
                path.read_bytes()
            )
        else:
            capsule["artifact_digests"][name] = prefixed_digest(_read_json(path))
    capsule["oracle"]["digest"] = prefixed_digest(_read_json(root / "oracle.json"))
    return _seal_capsule(root, capsule)


@pytest.fixture(scope="module")
def validators() -> dict[str, Draft7Validator]:
    return {
        name: Draft7Validator(_read_json(SCHEMAS / name))
        for name in (
            "counterexample.v1.json",
            "counterexample-oracle.v1.json",
            "reduction-certificate.v1.json",
        )
    }


@pytest.fixture(scope="module")
def templates(tmp_path_factory) -> dict[str, Path]:
    root = tmp_path_factory.mktemp("counterexample-adversarial-templates")
    private = root / "private"
    compile_counterexample(
        str(FIXTURES / "pii.scenario.json"),
        str(FIXTURES / "pii.test.json"),
        target="pii-email",
        out_dir=str(private),
        workspace=str(FIXTURES),
    )
    state = root / "state"
    compile_counterexample(
        str(FIXTURES / "state.scenario.json"),
        str(FIXTURES / "state.test.json"),
        target="refund-posted",
        out_dir=str(state),
        workspace=str(FIXTURES),
    )
    share = root / "share"
    export_counterexample(str(private), out_dir=str(share))
    return {"private": private, "state": state, "share": share}


def _clone(template: Path, tmp_path: Path, name: str = "case") -> Path:
    destination = tmp_path / name
    shutil.copytree(template, destination)
    return destination


COMPILER_SCHEMA_EDGE_CASES = (
    "negative-count-bound",
    "empty-entity-key",
    "empty-state-path",
    "empty-additive-scenario-key",
)


def _edge_case_documents(case: str) -> tuple[dict, dict, str]:
    scenario = {
        "kind": "hotato.scenario",
        "version": 1,
        "id": case,
        "goal": {"type": "test", "target": "schema-contract"},
        "caller": {"script": [{"say": "hello"}]},
    }
    if case == "negative-count-bound":
        assertion = {
            "id": "target",
            "kind": "count",
            "dimension": "conversation",
            "phrase": "never-present",
            "count": -1,
        }
    elif case == "empty-entity-key":
        scenario["agent_mock"] = {
            "tools": [{"name": "lookup", "arguments": {"id": "A"}, "result": {}}]
        }
        assertion = {
            "id": "target",
            "kind": "entity_accuracy",
            "dimension": "outcome",
            "reference": {"": "expected"},
        }
    elif case == "empty-state-path":
        scenario["agent_mock"] = {"state": {"orders": [{"id": "A"}]}}
        assertion = {
            "id": "target",
            "kind": "state",
            "dimension": "outcome",
            "resource": "orders",
            "filters": {"id": "A"},
            "expect": {"": "expected"},
        }
    elif case == "empty-additive-scenario-key":
        scenario[""] = "metadata"
        assertion = {
            "id": "target",
            "kind": "phrase",
            "dimension": "policy",
            "regex": "never-present",
        }
    else:  # pragma: no cover - parametrization is closed above
        raise AssertionError(case)
    test = {
        "kind": "hotato.conversation-test",
        "version": 1,
        "id": f"{case}-test",
        "agent": "fixture-agent",
        "assertions": {"deterministic": [assertion], "rubric": []},
        "inconclusive_policy": "refuse",
    }
    return scenario, test, "target"


@pytest.mark.parametrize("case", COMPILER_SCHEMA_EDGE_CASES)
def test_compiler_refuses_or_emits_schema_valid_artifacts(validators, tmp_path, case):
    scenario, test, target = _edge_case_documents(case)
    scenario_path = tmp_path / "scenario.json"
    test_path = tmp_path / "test.json"
    _write_json(scenario_path, scenario)
    _write_json(test_path, test)
    output = tmp_path / "output"
    try:
        compile_counterexample(
            str(scenario_path),
            str(test_path),
            target=target,
            out_dir=str(output),
            workspace=str(tmp_path),
        )
    except (CounterexampleRefusal, ValueError):
        return

    assert validators["counterexample.v1.json"].is_valid(
        _read_json(output / "capsule.json")
    )
    assert validators["counterexample-oracle.v1.json"].is_valid(
        _read_json(output / "oracle.json")
    )
    assert validators["reduction-certificate.v1.json"].is_valid(
        _read_json(output / "certificate.json")
    )


@pytest.mark.parametrize("relative", ["../outside.json", "/etc/passwd"])
def test_runnable_artifact_path_escape_is_refused(templates, tmp_path, relative):
    root = _clone(templates["private"], tmp_path)
    capsule = _read_json(root / "capsule.json")
    capsule["artifacts"]["scenario"] = relative
    _seal_capsule(root, capsule)

    with pytest.raises(CounterexampleRefusal) as raised:
        reproduce_counterexample(str(root))
    assert raised.value.code == "unsafe_member"


@pytest.mark.skipif(
    sys.platform == "win32",
    reason="planting the symlink this refusal is exercised against needs the "
           "SeCreateSymbolicLink privilege on Windows (absent by default); the "
           "symlink-rejection logic itself is POSIX-exercised here",
)
def test_symlinked_artifact_is_refused_before_use(templates, tmp_path):
    root = _clone(templates["private"], tmp_path)
    artifact = root / "input" / "scenario.json"
    artifact.unlink()
    artifact.symlink_to(root / "source" / "scenario.json")

    with pytest.raises(CounterexampleRefusal) as raised:
        reproduce_counterexample(str(root))
    assert raised.value.code == "symlink_refused"


@pytest.mark.skipif(
    sys.platform == "win32",
    reason="creating the directory symlink this refusal is exercised against "
           "needs the SeCreateSymbolicLink privilege on Windows (absent by "
           "default); the symlink-rejection logic itself is POSIX-exercised here",
)
def test_symlinked_capsule_root_is_refused(templates, tmp_path):
    root = _clone(templates["private"], tmp_path, "source")
    alias = tmp_path / "alias"
    alias.symlink_to(root, target_is_directory=True)

    with pytest.raises(CounterexampleRefusal) as raised:
        reproduce_counterexample(str(alias))
    assert raised.value.code == "symlink_refused"


def test_undeclared_file_is_refused_by_manifest_inventory(templates, tmp_path):
    root = _clone(templates["private"], tmp_path)
    (root / "undeclared.txt").write_text("hidden", encoding="utf-8")

    with pytest.raises(CounterexampleRefusal) as raised:
        reproduce_counterexample(str(root))
    assert raised.value.code == "manifest_inventory"


@pytest.mark.parametrize("profile", ["private", "share"])
def test_rebound_manifest_cannot_extend_closed_profile_inventory(
    templates, tmp_path, profile
):
    root = _clone(templates[profile], tmp_path)
    (root / "credentials.txt").write_text("declared-but-forbidden", encoding="utf-8")
    _seal_manifest(root)

    with pytest.raises(CounterexampleRefusal) as raised:
        inspect_counterexample(str(root))
    assert raised.value.code == "profile_inventory"


@pytest.mark.parametrize(
    "relative",
    ["report.md", "report.html", "card.svg", "README.md"],
)
def test_rebound_share_human_artifact_must_be_canonical(
    templates, tmp_path, relative
):
    root = _clone(templates["share"], tmp_path)
    (root / relative).write_text("attacker-controlled projection", encoding="utf-8")
    _seal_manifest(root)

    with pytest.raises(CounterexampleRefusal) as raised:
        inspect_counterexample(str(root))
    assert raised.value.code == "derived_artifact_mismatch"


@pytest.mark.parametrize(
    "relative",
    ["input/scenario.json", "report.md", "oracle.json"],
)
def test_private_inspect_binds_every_claimed_artifact(
    templates, tmp_path, relative
):
    root = _clone(templates["private"], tmp_path)
    (root / relative).write_text("{}\n", encoding="utf-8")
    _seal_manifest(root)

    with pytest.raises(CounterexampleRefusal):
        inspect_counterexample(str(root))


def test_manifest_member_traversal_is_refused(templates, tmp_path):
    root = _clone(templates["private"], tmp_path)
    manifest = _read_json(root / "MANIFEST.sha256.json")
    manifest["files"][0]["path"] = "../outside"
    _write_json(root / "MANIFEST.sha256.json", manifest)

    with pytest.raises(CounterexampleRefusal) as raised:
        reproduce_counterexample(str(root))
    assert raised.value.code == "unsafe_member"


SHARE_SCHEMA_DIVERGENCES = (
    "unknown-kind",
    "included-private-content",
    "empty-omission-claim",
    "empty-version",
    "oversized-budget",
    "zero-caller-turns",
    "empty-minimality-claim",
    "unknown-frozen-component",
    "malformed-minimality-check",
)


def _mutate_schema_invalid_share(capsule: dict, case: str) -> None:
    if case == "unknown-kind":
        capsule["target"]["kind"] = "invented_assertion"
    elif case == "non-string-dimension":
        capsule["target"]["dimension"] = {"blended": True}
    elif case == "included-private-content":
        capsule["privacy"]["content_included"] = ["scenario"]
    elif case == "empty-omission-claim":
        capsule["privacy"]["omitted"] = []
    elif case == "empty-version":
        capsule["provenance"]["hotato_version"] = ""
    elif case == "oversized-budget":
        capsule["reduction"]["budget"] = 100001
    elif case == "zero-caller-turns":
        capsule["reduction"]["initial"]["turns"] = 0
    elif case == "empty-minimality-claim":
        capsule["minimality"]["claim"] = ""
    elif case == "unknown-frozen-component":
        capsule["minimality"]["frozen_components"] = ["speaker_id"]
    elif case == "malformed-minimality-check":
        capsule["minimality"]["check_summary"] = {"count": -1, "outcomes": {}}
    else:  # pragma: no cover - parametrization is closed above
        raise AssertionError(case)


@pytest.mark.parametrize("case", SHARE_SCHEMA_DIVERGENCES)
def test_inspect_rejects_schema_invalid_share_capsule(
    validators, templates, tmp_path, case
):
    root = _clone(templates["share"], tmp_path)
    capsule = _read_json(root / "capsule.json")
    _mutate_schema_invalid_share(capsule, case)
    assert not validators["counterexample.v1.json"].is_valid(capsule)
    _seal_capsule(root, capsule)

    with pytest.raises(CounterexampleRefusal):
        inspect_counterexample(str(root))


def test_inspect_rejects_non_string_share_dimension(validators, templates, tmp_path):
    root = _clone(templates["share"], tmp_path)
    capsule = _read_json(root / "capsule.json")
    _mutate_schema_invalid_share(capsule, "non-string-dimension")
    assert not validators["counterexample.v1.json"].is_valid(capsule)
    _seal_capsule(root, capsule)

    with pytest.raises(CounterexampleRefusal):
        inspect_counterexample(str(root))


CAPSULE_SCALAR_DIVERGENCES = (
    "private-version-bool",
    "share-version-bool",
    "private-hotato-version",
    "share-hotato-version",
    "private-selection-seed-bool",
    "private-selection-applied-int",
)


@pytest.mark.parametrize("case", CAPSULE_SCALAR_DIVERGENCES)
def test_inspect_rejects_schema_invalid_scalar_semantics(
    validators, templates, tmp_path, case
):
    profile = "share" if case.startswith("share-") else "private"
    root = _clone(templates[profile], tmp_path)
    capsule = _read_json(root / "capsule.json")
    if case.endswith("version-bool"):
        capsule["version"] = True
    elif case.endswith("hotato-version"):
        capsule["provenance"]["hotato_version"] = "unversioned"
    elif case == "private-selection-seed-bool":
        capsule["provenance"]["scenario_selection"]["seed"] = False
    elif case == "private-selection-applied-int":
        capsule["provenance"]["scenario_selection"]["variation_matrix_applied"] = 0
    else:  # pragma: no cover - parametrization is closed above
        raise AssertionError(case)
    assert not validators["counterexample.v1.json"].is_valid(capsule)
    _seal_capsule(root, capsule)

    with pytest.raises(CounterexampleRefusal):
        inspect_counterexample(str(root))


@pytest.mark.parametrize("profile", ["private", "share"])
def test_inspect_fails_cleanly_on_unhashable_target_kind(templates, tmp_path, profile):
    root = _clone(templates[profile], tmp_path)
    capsule = _read_json(root / "capsule.json")
    capsule["target"]["kind"] = []
    if profile == "private":
        identity = copy.deepcopy(capsule["target"])
        identity.pop("fingerprint")
        capsule["target"]["fingerprint"] = prefixed_digest(identity)
    _seal_capsule(root, capsule)

    with pytest.raises(CounterexampleRefusal):
        inspect_counterexample(str(root))


PRIVATE_NESTED_DIVERGENCES = (
    "empty-artifacts",
    "malformed-oracle-reference",
    "empty-artifact-digests",
    "untyped-failure-atom",
    "false-content-inventory",
)


def _mutate_schema_invalid_private(capsule: dict, case: str) -> None:
    if case == "empty-artifacts":
        capsule["artifacts"] = {}
    elif case == "malformed-oracle-reference":
        capsule["oracle"] = {"path": 7, "digest": "not-a-digest"}
    elif case == "empty-artifact-digests":
        capsule["artifact_digests"] = {}
    elif case == "untyped-failure-atom":
        capsule["target"]["failure_atom"] = {"code": "unknown-failure"}
        capsule["target"]["source_failure_atoms"] = [
            {"code": "unknown-failure"}
        ]
    elif case == "false-content-inventory":
        capsule["privacy"]["content_included"] = []
    else:  # pragma: no cover - parametrization is closed above
        raise AssertionError(case)


@pytest.mark.parametrize("case", PRIVATE_NESTED_DIVERGENCES)
def test_inspect_rejects_schema_invalid_private_capsule(
    validators, templates, tmp_path, case
):
    root = _clone(templates["private"], tmp_path)
    capsule = _read_json(root / "capsule.json")
    _mutate_schema_invalid_private(capsule, case)
    assert not validators["counterexample.v1.json"].is_valid(capsule)
    _seal_capsule(root, capsule)

    with pytest.raises(CounterexampleRefusal):
        inspect_counterexample(str(root))


@pytest.mark.parametrize(
    "atom",
    [
        {"code": []},
        {"code": "sequence-step-absent", "index": True},
        {"code": "pii-detected", "detector": {}},
        {"code": "policy-violation", "rule": [], "type": "banned"},
        {"code": "state-field-value-mismatch", "field": []},
        {"code": "entity-value-mismatch", "key": []},
        {"code": "forbidden-match", "reason": "payload"},
    ],
)
def test_inspect_fails_cleanly_on_malformed_failure_atom(
    validators, templates, tmp_path, atom
):
    root = _clone(templates["private"], tmp_path)
    capsule = _read_json(root / "capsule.json")
    capsule["target"]["failure_atom"] = atom
    capsule["target"]["source_failure_atoms"] = [copy.deepcopy(atom)]
    assert not validators["counterexample.v1.json"].is_valid(capsule)
    _seal_capsule(root, capsule)

    with pytest.raises(CounterexampleRefusal):
        inspect_counterexample(str(root))


def test_reproduce_rejects_schema_invalid_oracle(validators, templates, tmp_path):
    root = _clone(templates["private"], tmp_path)
    oracle = _read_json(root / "oracle.json")
    oracle["authority"] = "model-judged"
    oracle["ci_gate_eligible"] = False
    oracle["observation_scope"]["rule"] = "any failure is sufficient"
    _write_json(root / "oracle.json", oracle)
    assert not validators["counterexample-oracle.v1.json"].is_valid(oracle)
    _refresh_private_bindings(root)

    with pytest.raises(CounterexampleRefusal):
        reproduce_counterexample(str(root))


def test_reproduce_binds_target_kind_to_embedded_source_assertion(tmp_path):
    scenario = {
        "kind": "hotato.scenario",
        "version": 1,
        "id": "cross-kind-source",
        "goal": {"type": "support", "target": "account"},
        "caller": {"script": [{"say": "help"}]},
    }
    test_doc = {
        "kind": "hotato.conversation-test",
        "version": 1,
        "id": "cross-kind-test",
        "agent": "fixture-agent",
        "assertions": {
            "deterministic": [
                {
                    "id": "result",
                    "kind": "tool_result",
                    "name": "issue_refund",
                    "result_subset": {"status": "posted"},
                }
            ],
            "rubric": [],
        },
    }
    scenario_path = tmp_path / "scenario.json"
    test_path = tmp_path / "test.json"
    _write_json(scenario_path, scenario)
    _write_json(test_path, test_doc)
    root = tmp_path / "private"
    compile_counterexample(
        str(scenario_path),
        str(test_path),
        target="result",
        out_dir=str(root),
        workspace=str(tmp_path),
    )

    capsule = _read_json(root / "capsule.json")
    capsule["target"]["kind"] = "tool_call"
    capsule["target"]["fingerprint"] = failure_identity_digest(
        capsule["target"]
    )
    oracle = _read_json(root / "oracle.json")
    oracle["target"] = copy.deepcopy(capsule["target"])
    _write_json(root / "oracle.json", oracle)
    certificate = _read_json(root / "certificate.json")
    certificate["failure_fingerprint"] = capsule["target"]["fingerprint"]
    _write_json(root / "certificate.json", certificate)
    _refresh_private_bindings(root, capsule)

    with pytest.raises(CounterexampleRefusal) as raised:
        reproduce_counterexample(str(root))
    assert raised.value.code == "target_binding_mismatch"


def test_reproduce_fails_cleanly_on_unhashable_oracle_freeze(templates, tmp_path):
    root = _clone(templates["private"], tmp_path)
    oracle = _read_json(root / "oracle.json")
    oracle["observation_scope"]["frozen_components"] = [{}]
    _write_json(root / "oracle.json", oracle)
    _refresh_private_bindings(root)

    with pytest.raises(CounterexampleRefusal):
        reproduce_counterexample(str(root))


def test_reproduce_rejects_boolean_oracle_version(validators, templates, tmp_path):
    root = _clone(templates["private"], tmp_path)
    oracle = _read_json(root / "oracle.json")
    oracle["version"] = True
    assert not validators["counterexample-oracle.v1.json"].is_valid(oracle)
    _write_json(root / "oracle.json", oracle)
    _refresh_private_bindings(root)

    with pytest.raises(CounterexampleRefusal):
        reproduce_counterexample(str(root))


def test_reproduce_rejects_boolean_minimum_caller_turns(templates, tmp_path):
    root = _clone(templates["private"], tmp_path)
    oracle = _read_json(root / "oracle.json")
    oracle["observation_scope"]["minimum_caller_turns"] = True
    _write_json(root / "oracle.json", oracle)
    _refresh_private_bindings(root)

    with pytest.raises(CounterexampleRefusal):
        reproduce_counterexample(str(root))


def test_verify_rejects_schema_invalid_certificate_top_level(
    validators, templates, tmp_path
):
    root = _clone(templates["private"], tmp_path)
    certificate = _read_json(root / "certificate.json")
    certificate["unverified_claim"] = "trusted"
    _write_json(root / "certificate.json", certificate)
    assert not validators["reduction-certificate.v1.json"].is_valid(certificate)
    _refresh_private_bindings(root)

    with pytest.raises(CounterexampleRefusal):
        verify_counterexample(str(root))


@pytest.mark.parametrize("kind", [[], {}])
def test_verify_fails_cleanly_on_unhashable_certificate_operation_kind(
    templates, tmp_path, kind
):
    root = _clone(templates["private"], tmp_path)
    certificate = _read_json(root / "certificate.json")
    certificate["accepted_steps"][0]["operation"]["kind"] = kind
    _write_json(root / "certificate.json", certificate)
    _refresh_private_bindings(root)

    with pytest.raises(CounterexampleRefusal) as raised:
        verify_counterexample(str(root))
    assert raised.value.code == "certificate_schema"


def test_verify_fails_cleanly_on_unhashable_journal_operation_kind(
    templates, tmp_path
):
    root = _clone(templates["private"], tmp_path)
    journal_path = root / "reduction.jsonl"
    rows = [
        json.loads(line)
        for line in journal_path.read_text(encoding="utf-8").splitlines()
    ]
    rows[0]["operation"]["kind"] = []
    journal = "".join(canonical_json(row) for row in rows).encode("utf-8")
    journal_path.write_bytes(journal)
    certificate = _read_json(root / "certificate.json")
    certificate["journal_sha256"] = "sha256:" + sha256_bytes(journal)
    _write_json(root / "certificate.json", certificate)
    _refresh_private_bindings(root)

    with pytest.raises(CounterexampleRefusal) as raised:
        verify_counterexample(str(root))
    assert raised.value.code == "certificate_schema"


def test_verify_binds_claimed_operation_to_replayed_transform(
    validators, templates, tmp_path
):
    root = _clone(templates["private"], tmp_path)
    certificate = _read_json(root / "certificate.json")
    step = certificate["accepted_steps"][0]
    step["operation"] = {
        "kind": "remove-field",
        "phase": "empty-environment",
        "path": "environment",
    }
    assert validators["reduction-certificate.v1.json"].is_valid(certificate)
    _write_json(root / "certificate.json", certificate)
    _refresh_private_bindings(root)

    with pytest.raises(CounterexampleRefusal):
        verify_counterexample(str(root))


def test_verify_binds_removed_paths_to_replayed_transform(
    validators, templates, tmp_path
):
    root = _clone(templates["private"], tmp_path)
    certificate = _read_json(root / "certificate.json")
    step = next(
        row
        for row in certificate["accepted_steps"]
        if row["operation"]["kind"] == "remove-path-set"
    )
    original_operation = copy.deepcopy(step["operation"])
    forged_operation = copy.deepcopy(original_operation)
    forged_operation["paths"] = [f"attacker.path[{index}]" for index in range(len(original_operation["paths"]))]
    step["operation"] = forged_operation

    journal_path = root / "reduction.jsonl"
    rows = [json.loads(line) for line in journal_path.read_text(encoding="utf-8").splitlines()]
    matching = [
        row
        for row in rows
        if row["status"] == "PRESERVED"
        and row["candidate_digest"] == step["child_digest"]
        and row["operation"] == original_operation
    ]
    assert len(matching) == 1
    matching[0]["operation"] = forged_operation
    journal = "".join(canonical_json(row) for row in rows).encode("utf-8")
    journal_path.write_bytes(journal)
    certificate["journal_sha256"] = "sha256:" + sha256_bytes(journal)
    _write_json(root / "certificate.json", certificate)
    assert validators["reduction-certificate.v1.json"].is_valid(certificate)
    _refresh_private_bindings(root)

    with pytest.raises(CounterexampleRefusal):
        verify_counterexample(str(root))


def test_reproduce_rejects_malformed_accepted_step(validators, templates, tmp_path):
    root = _clone(templates["private"], tmp_path)
    certificate = _read_json(root / "certificate.json")
    certificate["accepted_steps"][0]["operation"] = "forged-operation"
    certificate["accepted_steps"][0]["oracle_result_digest"] = None
    _write_json(root / "certificate.json", certificate)
    assert not validators["reduction-certificate.v1.json"].is_valid(certificate)
    _refresh_private_bindings(root)

    with pytest.raises(CounterexampleRefusal):
        reproduce_counterexample(str(root))


def test_reproduce_rejects_boolean_certificate_version(
    validators, templates, tmp_path
):
    root = _clone(templates["private"], tmp_path)
    certificate = _read_json(root / "certificate.json")
    certificate["version"] = True
    assert not validators["reduction-certificate.v1.json"].is_valid(certificate)
    _write_json(root / "certificate.json", certificate)
    _refresh_private_bindings(root)

    with pytest.raises(CounterexampleRefusal):
        reproduce_counterexample(str(root))


def test_verify_rejects_boolean_accepted_step_number(templates, tmp_path):
    root = _clone(templates["private"], tmp_path)
    certificate = _read_json(root / "certificate.json")
    certificate["accepted_steps"][0]["step"] = True
    _write_json(root / "certificate.json", certificate)
    _refresh_private_bindings(root)

    with pytest.raises(CounterexampleRefusal):
        verify_counterexample(str(root))


def test_verify_rejects_boolean_journal_attempt(templates, tmp_path):
    root = _clone(templates["private"], tmp_path)
    journal_path = root / "reduction.jsonl"
    rows = [json.loads(line) for line in journal_path.read_text(encoding="utf-8").splitlines()]
    rows[0]["attempt"] = True
    journal = "".join(canonical_json(row) for row in rows).encode("utf-8")
    journal_path.write_bytes(journal)
    certificate = _read_json(root / "certificate.json")
    certificate["journal_sha256"] = "sha256:" + sha256_bytes(journal)
    _write_json(root / "certificate.json", certificate)
    _refresh_private_bindings(root)

    with pytest.raises(CounterexampleRefusal):
        verify_counterexample(str(root))


def test_verify_rejects_unknown_single_unit_phase(templates, tmp_path):
    root = _clone(templates["private"], tmp_path)
    certificate = _read_json(root / "certificate.json")
    step = next(
        row
        for row in certificate["accepted_steps"]
        if row["operation"]["kind"] == "remove-single-unit"
    )
    original = copy.deepcopy(step["operation"])
    step["operation"]["phase"] = "bogus-phase"
    journal_path = root / "reduction.jsonl"
    rows = [json.loads(line) for line in journal_path.read_text(encoding="utf-8").splitlines()]
    journal_row = next(
        row
        for row in rows
        if row["status"] == "PRESERVED" and row["operation"] == original
    )
    journal_row["operation"]["phase"] = "bogus-phase"
    journal = "".join(canonical_json(row) for row in rows).encode("utf-8")
    journal_path.write_bytes(journal)
    certificate["journal_sha256"] = "sha256:" + sha256_bytes(journal)
    _write_json(root / "certificate.json", certificate)
    _refresh_private_bindings(root)

    with pytest.raises(CounterexampleRefusal):
        verify_counterexample(str(root))


def test_reproduce_fails_cleanly_on_unhashable_removed_paths(templates, tmp_path):
    root = _clone(templates["private"], tmp_path)
    certificate = _read_json(root / "certificate.json")
    step = next(
        row
        for row in certificate["accepted_steps"]
        if row["operation"]["kind"] == "remove-path-set"
    )
    step["operation"]["paths"] = [{}]
    _write_json(root / "certificate.json", certificate)
    _refresh_private_bindings(root)

    with pytest.raises(CounterexampleRefusal):
        reproduce_counterexample(str(root))


def test_verify_rejects_semantically_forged_reduction_journal(templates, tmp_path):
    root = _clone(templates["private"], tmp_path)
    forged = b'{"attempt":1,"status":"PRESERVED","operation":"fabricated"}\n'
    (root / "reduction.jsonl").write_bytes(forged)
    certificate = _read_json(root / "certificate.json")
    certificate["journal_sha256"] = "sha256:" + sha256_bytes(forged)
    _write_json(root / "certificate.json", certificate)
    _refresh_private_bindings(root)

    with pytest.raises(CounterexampleRefusal):
        verify_counterexample(str(root))


def test_verify_rejects_unaccepted_preserved_journal_row(templates, tmp_path):
    root = _clone(templates["private"], tmp_path)
    certificate = _read_json(root / "certificate.json")
    child_digests = {step["child_digest"] for step in certificate["accepted_steps"]}
    journal_path = root / "reduction.jsonl"
    rows = [json.loads(line) for line in journal_path.read_text(encoding="utf-8").splitlines()]
    row = next(
        item
        for item in rows
        if item["status"] != "PRESERVED"
        and item["candidate_digest"] not in child_digests
        and item.get("code") != "budget_exhausted"
    )
    row["status"] = "PRESERVED"
    row["code"] = "target_failed"
    row["failure_atom_digest"] = certificate["accepted_steps"][0][
        "failure_atom_digest"
    ]
    journal = "".join(canonical_json(item) for item in rows).encode("utf-8")
    journal_path.write_bytes(journal)
    certificate["journal_sha256"] = "sha256:" + sha256_bytes(journal)
    _write_json(root / "certificate.json", certificate)
    _refresh_private_bindings(root)

    with pytest.raises(CounterexampleRefusal):
        verify_counterexample(str(root))


def test_verify_rejects_extra_preserved_row_reusing_accepted_child(templates, tmp_path):
    root = _clone(templates["private"], tmp_path)
    certificate = _read_json(root / "certificate.json")
    accepted = certificate["accepted_steps"][0]
    journal_path = root / "reduction.jsonl"
    rows = [json.loads(line) for line in journal_path.read_text(encoding="utf-8").splitlines()]
    row = next(item for item in rows if item["status"] != "PRESERVED")
    row.update({
        "status": "PRESERVED",
        "code": "target_failed",
        "candidate_digest": accepted["child_digest"],
        "failure_atom_digest": accepted["failure_atom_digest"],
    })
    journal = "".join(canonical_json(item) for item in rows).encode("utf-8")
    journal_path.write_bytes(journal)
    certificate["journal_sha256"] = "sha256:" + sha256_bytes(journal)
    _write_json(root / "certificate.json", certificate)
    _refresh_private_bindings(root)

    with pytest.raises(CounterexampleRefusal):
        verify_counterexample(str(root))


def test_verify_rejects_forged_reduction_statistics(validators, templates, tmp_path):
    root = _clone(templates["private"], tmp_path)
    capsule = _read_json(root / "capsule.json")
    capsule["reduction"]["attempts"] = 0
    capsule["reduction"]["candidate_evaluations"] = 0
    capsule["reduction"]["cache_hits"] = 999
    capsule["reduction"]["total_evaluations"] = 4
    capsule["reduction"]["initial"]["bytes"] = 1
    capsule["reduction"]["final"]["bytes"] = 1
    certificate = _read_json(root / "certificate.json")
    certificate["candidate_evaluations"] = 0
    certificate["cache_hits"] = 999
    _write_json(root / "certificate.json", certificate)
    assert validators["counterexample.v1.json"].is_valid(capsule)
    assert validators["reduction-certificate.v1.json"].is_valid(certificate)
    _refresh_private_bindings(root, capsule)

    with pytest.raises(CounterexampleRefusal):
        verify_counterexample(str(root))


def test_verify_rejects_erased_minimality_evidence(validators, templates, tmp_path):
    root = _clone(templates["state"], tmp_path)
    capsule = _read_json(root / "capsule.json")
    assert capsule["minimality"]["remaining_unit_checks"]
    capsule["minimality"]["remaining_unit_checks"] = []
    _write_json(root / "minimality.json", capsule["minimality"])
    assert validators["counterexample.v1.json"].is_valid(capsule)
    _refresh_private_bindings(root, capsule)

    with pytest.raises(CounterexampleRefusal):
        verify_counterexample(str(root))


def test_verify_rejects_minimality_scope_mismatch(validators, templates, tmp_path):
    root = _clone(templates["private"], tmp_path)
    capsule = _read_json(root / "capsule.json")
    capsule["minimality"]["frozen_components"] = ["tools"]
    _write_json(root / "minimality.json", capsule["minimality"])
    assert validators["counterexample.v1.json"].is_valid(capsule)
    _refresh_private_bindings(root, capsule)

    with pytest.raises(CounterexampleRefusal):
        verify_counterexample(str(root))


def test_runtime_enforces_total_evaluation_cross_field(validators, templates, tmp_path):
    root = _clone(templates["share"], tmp_path)
    capsule = _read_json(root / "capsule.json")
    capsule["reduction"]["total_evaluations"] += 1
    assert validators["counterexample.v1.json"].is_valid(capsule)
    _seal_capsule(root, capsule)

    with pytest.raises(CounterexampleRefusal) as raised:
        inspect_counterexample(str(root))
    assert raised.value.code == "capsule_schema"


def test_runtime_enforces_private_seed_selection_binding(validators, templates, tmp_path):
    root = _clone(templates["private"], tmp_path)
    capsule = _read_json(root / "capsule.json")
    capsule["provenance"]["scenario_selection"]["seed"] += 1
    assert validators["counterexample.v1.json"].is_valid(capsule)
    _seal_capsule(root, capsule)

    with pytest.raises(CounterexampleRefusal) as raised:
        inspect_counterexample(str(root))
    assert raised.value.code == "capsule_schema"


def test_runtime_enforces_target_fingerprint_binding(validators, templates, tmp_path):
    root = _clone(templates["private"], tmp_path)
    capsule = _read_json(root / "capsule.json")
    capsule["target"]["fingerprint"] = "sha256:" + "0" * 64
    assert validators["counterexample.v1.json"].is_valid(capsule)
    _seal_capsule(root, capsule)

    with pytest.raises(CounterexampleRefusal) as raised:
        inspect_counterexample(str(root))
    assert raised.value.code == "capsule_schema"


def test_inspect_fails_cleanly_on_unhashable_frozen_component(templates, tmp_path):
    root = _clone(templates["share"], tmp_path)
    capsule = _read_json(root / "capsule.json")
    capsule["minimality"]["frozen_components"] = [{}]
    _seal_capsule(root, capsule)

    with pytest.raises(CounterexampleRefusal):
        inspect_counterexample(str(root))


def test_verify_fails_cleanly_on_non_object_oracle(templates, tmp_path):
    root = _clone(templates["private"], tmp_path)
    _write_json(root / "oracle.json", [])
    _refresh_private_bindings(root)

    with pytest.raises(CounterexampleRefusal):
        verify_counterexample(str(root))


def test_verify_fails_cleanly_on_non_object_certificate(templates, tmp_path):
    root = _clone(templates["private"], tmp_path)
    _write_json(root / "certificate.json", [])
    _refresh_private_bindings(root)

    with pytest.raises(CounterexampleRefusal):
        verify_counterexample(str(root))


def test_verify_fails_cleanly_on_non_string_step_digest(templates, tmp_path):
    root = _clone(templates["private"], tmp_path)
    certificate = _read_json(root / "certificate.json")
    certificate["accepted_steps"][0]["parent_digest"] = 7
    _write_json(root / "certificate.json", certificate)
    _refresh_private_bindings(root)

    with pytest.raises(CounterexampleRefusal):
        verify_counterexample(str(root))


def test_predicate_skips_malformed_certificate_instead_of_crashing(templates, tmp_path):
    root = _clone(templates["private"], tmp_path)
    _write_json(root / "certificate.json", [])
    _refresh_private_bindings(root)

    assert predicate_counterexample(str(root)) == 125


def test_predicate_skips_unhashable_operation_kind(templates, tmp_path):
    root = _clone(templates["private"], tmp_path)
    certificate = _read_json(root / "certificate.json")
    certificate["accepted_steps"][0]["operation"]["kind"] = []
    _write_json(root / "certificate.json", certificate)
    _refresh_private_bindings(root)

    assert predicate_counterexample(str(root)) == 125


def test_evaluator_digest_binds_transitive_synth_dependency(tmp_path, monkeypatch):
    import hotato.counterexample.bundle as bundle
    import hotato.synth as synth

    original = bundle._evaluator_digest()
    replacement = tmp_path / "synth.py"
    replacement.write_bytes(Path(synth.__file__).read_bytes() + b"\n# changed dependency\n")
    monkeypatch.setattr(synth, "__file__", str(replacement))

    assert bundle._evaluator_digest() != original


def test_verify_rejects_replaced_executable_under_same_counterexample_id(
    templates, tmp_path
):
    root = _clone(templates["private"], tmp_path)
    capsule_id = _read_json(root / "capsule.json")["counterexample_id"]
    (root / "reproduce.sh").write_text(
        "#!/bin/sh\necho substituted-helper >&2\nexit 0\n", encoding="utf-8"
    )
    _seal_manifest(root)
    assert _read_json(root / "capsule.json")["counterexample_id"] == capsule_id

    with pytest.raises(CounterexampleRefusal):
        verify_counterexample(str(root))


def test_verify_rejects_rebound_noncanonical_executable(templates, tmp_path):
    root = _clone(templates["private"], tmp_path)
    (root / "reproduce.sh").write_text(
        "#!/bin/sh\necho attacker-controlled >&2\nexit 0\n", encoding="utf-8"
    )
    _refresh_private_bindings(root)

    with pytest.raises(CounterexampleRefusal) as raised:
        verify_counterexample(str(root))
    assert raised.value.code == "derived_artifact_mismatch"


def test_verify_rejects_duplicate_json_object_names(templates, tmp_path):
    root = _clone(templates["private"], tmp_path)
    capsule_path = root / "capsule.json"
    text = capsule_path.read_text(encoding="utf-8")
    capsule_path.write_text(
        text.replace("{\n", '{\n  "kind": "attacker-controlled",\n', 1),
        encoding="utf-8",
    )
    _seal_manifest(root)

    with pytest.raises(CounterexampleRefusal):
        verify_counterexample(str(root))


def test_inspect_rejects_boolean_manifest_version(templates, tmp_path):
    root = _clone(templates["share"], tmp_path)
    manifest = _read_json(root / "MANIFEST.sha256.json")
    manifest["version"] = True
    _write_json(root / "MANIFEST.sha256.json", manifest)

    with pytest.raises(CounterexampleRefusal):
        inspect_counterexample(str(root))


def test_manifest_row_limit_is_preflighted_before_member_io(
    templates, tmp_path, monkeypatch
):
    import hotato.counterexample.bundle as bundle

    root = _clone(templates["private"], tmp_path)
    data = (root / "capsule.json").read_bytes()
    row = {
        "path": "capsule.json",
        "sha256": sha256_bytes(data),
        "bytes": len(data),
    }
    manifest = {
        "kind": "hotato.counterexample-manifest.v1",
        "version": 1,
        "algorithm": "sha256",
        "files": [copy.deepcopy(row) for _ in range(1025)],
    }
    _write_json(root / "MANIFEST.sha256.json", manifest)
    calls = 0
    original = bundle.read_regular_bytes

    def counted_read(*args, **kwargs):
        nonlocal calls
        calls += 1
        return original(*args, **kwargs)

    monkeypatch.setattr(bundle, "read_regular_bytes", counted_read)
    with pytest.raises(CounterexampleRefusal) as raised:
        verify_counterexample(str(root))
    assert raised.value.code == "capsule_too_many_files"
    assert calls < 10


def test_verify_rejects_unmanifested_empty_directory(templates, tmp_path):
    root = _clone(templates["private"], tmp_path)
    (root / "unmanifested-empty-directory").mkdir()

    with pytest.raises(CounterexampleRefusal):
        verify_counterexample(str(root))


def test_verify_caps_empty_directory_count_before_unbounded_walk(
    templates, tmp_path, monkeypatch
):
    import hotato.counterexample.model as model

    root = _clone(templates["private"], tmp_path)
    (root / "extra-one").mkdir()
    (root / "extra-two").mkdir()
    monkeypatch.setattr(model, "MAX_CAPSULE_DIRECTORIES", 2)

    with pytest.raises(CounterexampleRefusal) as raised:
        verify_counterexample(str(root))
    assert raised.value.code == "capsule_too_many_directories"


def test_verify_caps_empty_directory_depth(templates, tmp_path, monkeypatch):
    import hotato.counterexample.model as model

    root = _clone(templates["private"], tmp_path)
    (root / "extra" / "nested").mkdir(parents=True)
    monkeypatch.setattr(model, "MAX_CAPSULE_DEPTH", 1)

    with pytest.raises(CounterexampleRefusal) as raised:
        verify_counterexample(str(root))
    assert raised.value.code == "capsule_too_deep"


@pytest.mark.skipif(
    sys.platform == "win32",
    reason="creating the directory symlink this refusal is exercised against "
           "needs the SeCreateSymbolicLink privilege on Windows (absent by "
           "default); the symlink-rejection logic itself is POSIX-exercised here",
)
def test_inspect_rejects_symlinked_capsule_root(templates, tmp_path):
    root = _clone(templates["share"], tmp_path, "source")
    alias = tmp_path / "alias"
    alias.symlink_to(root, target_is_directory=True)

    with pytest.raises(CounterexampleRefusal):
        inspect_counterexample(str(alias))


@pytest.mark.skipif(
    sys.platform == "win32",
    reason="the mid-read TOCTOU substitution swaps in a directory symlink, which "
           "needs the SeCreateSymbolicLink privilege on Windows (absent by "
           "default); the substitution-rejection logic itself is POSIX-exercised here",
)
def test_bundle_member_refuses_ancestor_symlink_substitution(templates, tmp_path, monkeypatch):
    import hotato.counterexample.bundle as bundle

    root = _clone(templates["private"], tmp_path)
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "scenario.json").write_bytes((root / "input" / "scenario.json").read_bytes())
    original_read = bundle.read_regular_bytes
    swapped = False
    victim = root / "input" / "scenario.json"

    def swapping_read(path, *args, **kwargs):
        nonlocal swapped
        if Path(path) == victim and not swapped:
            swapped = True
            (root / "input").rename(root / "input-before-race")
            (root / "input").symlink_to(outside, target_is_directory=True)
        return original_read(path, *args, **kwargs)

    monkeypatch.setattr(bundle, "read_regular_bytes", swapping_read)
    with pytest.raises(CounterexampleRefusal):
        bundle._bundle_member(str(root), "input/scenario.json")


def test_verify_uses_the_same_root_snapshot_as_its_manifest(tmp_path, monkeypatch):
    import hotato.counterexample.bundle as bundle

    first = tmp_path / "first"
    first_result = compile_counterexample(
        str(FIXTURES / "pii.scenario.json"),
        str(FIXTURES / "pii.test.json"),
        target="pii-email",
        out_dir=str(first),
        workspace=str(FIXTURES),
    )
    replacement = tmp_path / "replacement"
    compile_counterexample(
        str(FIXTURES / "policy.scenario.json"),
        str(FIXTURES / "policy.test.json"),
        target="guarantee-language",
        out_dir=str(replacement),
        workspace=str(FIXTURES),
    )
    original_verify_manifest = bundle._verify_manifest
    swapped = False

    def verify_then_swap(root):
        nonlocal swapped
        result = original_verify_manifest(root)
        if not swapped:
            swapped = True
            first.rename(tmp_path / "manifest-verified-snapshot")
            replacement.rename(first)
        return result

    monkeypatch.setattr(bundle, "_verify_manifest", verify_then_swap)
    try:
        result = verify_counterexample(str(first))
    except CounterexampleRefusal:
        return
    assert result["counterexample_id"] == first_result["counterexample_id"]


def test_compile_parses_the_same_input_bytes_it_hashes(tmp_path, monkeypatch):
    import hotato.counterexample.bundle as bundle

    scenario_path = tmp_path / "scenario.json"
    test_path = tmp_path / "test.json"
    shutil.copyfile(FIXTURES / "pii.scenario.json", scenario_path)
    shutil.copyfile(FIXTURES / "pii.test.json", test_path)
    changed = _read_json(scenario_path)
    changed["caller"]["script"][2]["say"] = "a-different-address@example.net"
    original_loader = bundle.SC.load_scenario_file
    swapped = False

    def swapping_loader(path):
        nonlocal swapped
        if not swapped:
            swapped = True
            _write_json(Path(path), changed)
        return original_loader(path)

    monkeypatch.setattr(bundle.SC, "load_scenario_file", swapping_loader)
    output = tmp_path / "output"
    compile_counterexample(
        str(scenario_path),
        str(test_path),
        target="pii-email",
        out_dir=str(output),
        workspace=str(tmp_path),
    )

    assert verify_counterexample(str(output))["ok"] is True


def test_compile_freezes_or_refuses_external_policy_pack_dependency(tmp_path):
    policy_path = tmp_path / "policy-pack.json"
    _write_json(
        policy_path,
        {
            "name": "capsule-policy",
            "version": 1,
            "rules": [
                {"id": "blocked-token", "type": "banned", "regex": "blocked-token"}
            ],
        },
    )
    scenario_path = tmp_path / "scenario.json"
    _write_json(
        scenario_path,
        {
            "kind": "hotato.scenario",
            "version": 1,
            "id": "external-policy-dependency",
            "goal": {"type": "test", "target": "policy"},
            "caller": {"script": [{"say": "blocked-token"}]},
        },
    )
    test_path = tmp_path / "test.json"
    _write_json(
        test_path,
        {
            "kind": "hotato.conversation-test",
            "version": 1,
            "id": "external-policy-test",
            "agent": "fixture-agent",
            "assertions": {
                "deterministic": [
                    {
                        "id": "policy-target",
                        "kind": "policy",
                        "dimension": "policy",
                        "pack_path": str(policy_path),
                        "rule_ids": ["blocked-token"],
                    }
                ],
                "rubric": [],
            },
            "inconclusive_policy": "refuse",
        },
    )
    output = tmp_path / "output"
    try:
        compile_counterexample(
            str(scenario_path),
            str(test_path),
            target="policy-target",
            out_dir=str(output),
            workspace=str(tmp_path),
        )
    except CounterexampleRefusal:
        return

    policy_path.unlink()
    assert verify_counterexample(str(output))["ok"] is True


def test_compile_does_not_clobber_output_created_during_commit(tmp_path, monkeypatch):
    import hotato.counterexample.bundle as bundle

    output = tmp_path / "output"
    original_commit = bundle._rename_no_replace
    raced = False

    def create_destination_then_commit(source, destination):
        nonlocal raced
        if Path(destination) == output and not raced:
            raced = True
            output.mkdir()
        return original_commit(source, destination)

    monkeypatch.setattr(bundle, "_rename_no_replace", create_destination_then_commit)
    with pytest.raises(CounterexampleRefusal):
        compile_counterexample(
            str(FIXTURES / "pii.scenario.json"),
            str(FIXTURES / "pii.test.json"),
            target="pii-email",
            out_dir=str(output),
            workspace=str(FIXTURES),
        )
    assert raced


def test_export_projects_the_same_capsule_it_verified(tmp_path, monkeypatch):
    import hotato.counterexample.bundle as bundle

    first = tmp_path / "first"
    compile_counterexample(
        str(FIXTURES / "pii.scenario.json"),
        str(FIXTURES / "pii.test.json"),
        target="pii-email",
        out_dir=str(first),
        workspace=str(FIXTURES),
    )
    replacement = tmp_path / "replacement"
    compile_counterexample(
        str(FIXTURES / "policy.scenario.json"),
        str(FIXTURES / "policy.test.json"),
        target="guarantee-language",
        out_dir=str(replacement),
        workspace=str(FIXTURES),
    )
    original_verify = bundle.verify_counterexample

    def verify_then_swap(path):
        result = original_verify(path)
        first.rename(tmp_path / "verified-snapshot")
        replacement.rename(first)
        return result

    monkeypatch.setattr(bundle, "verify_counterexample", verify_then_swap)
    share = tmp_path / "share"
    export_counterexample(str(first), out_dir=str(share))

    assert _read_json(share / "capsule.json")["target"]["kind"] == "pii"
