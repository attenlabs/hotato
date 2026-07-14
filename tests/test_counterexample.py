"""Counterexample compiler correctness, determinism, privacy, and tamper tests."""

from __future__ import annotations

import json
import os
import shutil
import sys

import pytest

from hotato.counterexample import (
    CounterexampleRefusal,
    compile_counterexample,
    export_counterexample,
    inspect_counterexample,
    predicate_counterexample,
    reproduce_counterexample,
    verify_counterexample,
)
from hotato.counterexample.model import canonical_json, prefixed_digest

FIXTURES = os.path.join(os.path.dirname(__file__), "fixtures", "counterexample")
SCENARIO = os.path.join(FIXTURES, "pii.scenario.json")
TEST = os.path.join(FIXTURES, "pii.test.json")


def _compile(tmp_path, name="case.hotato-repro", budget=512):
    out = tmp_path / name
    result = compile_counterexample(
        SCENARIO,
        TEST,
        target="pii-email",
        out_dir=str(out),
        workspace=FIXTURES,
        budget=budget,
    )
    return out, result


def _compile_fixture(tmp_path, stem, target):
    out = tmp_path / f"{stem}.hotato-repro"
    result = compile_counterexample(
        os.path.join(FIXTURES, f"{stem}.scenario.json"),
        os.path.join(FIXTURES, f"{stem}.test.json"),
        target=target,
        out_dir=str(out),
        workspace=FIXTURES,
    )
    return out, result


def _load(path):
    return json.loads(path.read_text(encoding="utf-8"))


def _rewrite_manifest(root):
    import hotato.counterexample.bundle as bundle

    (root / "MANIFEST.sha256.json").write_text(
        canonical_json(bundle._manifest(str(root)), pretty=True), encoding="utf-8",
    )


def test_compile_reduces_and_verifies(tmp_path):
    out, result = _compile(tmp_path)
    assert result["exit_code"] == 0
    assert result["minimality"] == "one_minimal"
    assert result["reduction"]["initial"]["turns"] == 5
    assert result["reduction"]["final"]["turns"] == 1
    assert result["reduction"]["final"]["tools"] == 0
    assert result["reduction"]["final"]["state_leaves"] == 0

    reduced = _load(out / "input" / "scenario.json")
    assert reduced["caller"]["script"] == [
        {"say": "You can reach me at person@example.com."}
    ]
    assert "agent_mock" not in reduced
    assert "variation_matrix" not in reduced

    verified = verify_counterexample(str(out))
    assert verified["ok"] is True
    assert verified["minimality"] == "one_minimal"
    assert verified["source_replays"] == 2
    assert verified["final_replays"] == 2
    assert verified["accepted_steps_replayed"] == result["reduction"]["accepted"]
    assert predicate_counterexample(str(out)) == 1
    reproduced = reproduce_counterexample(str(out))
    assert reproduced["status"] == "failure_reproduced"
    assert reproduced["evaluator_match"] is True


def test_tool_argument_failure_cannot_reduce_to_missing_tool(tmp_path):
    scenario = {
        "kind": "hotato.scenario",
        "version": 1,
        "id": "wrong-refund-arguments",
        "goal": {"type": "refund", "target": "order"},
        "caller": {"script": [{"say": "Please refund my order."}]},
        "agent_mock": {
            "tools": [
                {
                    "name": "issue_refund",
                    "arguments": {"id": "B"},
                    "result": {"accepted": True},
                }
            ]
        },
    }
    test_doc = {
        "kind": "hotato.conversation-test",
        "version": 1,
        "id": "wrong-refund-arguments-test",
        "agent": "fixture-agent",
        "assertions": {
            "deterministic": [
                {
                    "id": "refund-call",
                    "kind": "tool_call",
                    "name": "issue_refund",
                    "args_subset": {"id": "A"},
                }
            ],
            "rubric": [],
        },
    }
    scenario_path = tmp_path / "scenario.json"
    test_path = tmp_path / "test.json"
    scenario_path.write_text(canonical_json(scenario, pretty=True), encoding="utf-8")
    test_path.write_text(canonical_json(test_doc, pretty=True), encoding="utf-8")
    out = tmp_path / "wrong-arguments.hotato-repro"

    result = compile_counterexample(
        str(scenario_path),
        str(test_path),
        target="refund-call",
        out_dir=str(out),
        workspace=str(tmp_path),
    )

    assert result["exit_code"] == 0
    assert result["target"]["failure_atom"] == {
        "code": "tool-argument-value-mismatch",
        "key": "id",
    }
    reduced = _load(out / "input" / "scenario.json")
    assert [tool["name"] for tool in reduced["agent_mock"]["tools"]] == [
        "issue_refund"
    ]
    assert reduced["agent_mock"]["tools"][0]["arguments"] == {"id": "B"}
    assert verify_counterexample(str(out))["ok"] is True


def test_same_inputs_are_byte_identical(tmp_path):
    one, _ = _compile(tmp_path, "one.hotato-repro")
    two, _ = _compile(tmp_path, "two.hotato-repro")
    files_one = sorted(
        str(path.relative_to(one)) for path in one.rglob("*") if path.is_file()
    )
    files_two = sorted(
        str(path.relative_to(two)) for path in two.rglob("*") if path.is_file()
    )
    assert files_one == files_two
    for rel in files_one:
        assert (one / rel).read_bytes() == (two / rel).read_bytes(), rel


def test_budget_exhaustion_never_claims_minimality(tmp_path):
    out, result = _compile(tmp_path, "budget.hotato-repro", budget=1)
    assert result["exit_code"] == 1
    assert result["minimality"] == "budget_exhausted"
    capsule = _load(out / "capsule.json")
    assert capsule["minimality"]["status"] == "budget_exhausted"
    assert "1-minimal" not in capsule["minimality"]["claim"]
    assert verify_counterexample(str(out))["ok"] is True


def test_tamper_is_refused_and_predicate_skips(tmp_path):
    out, _ = _compile(tmp_path)
    scenario = out / "input" / "scenario.json"
    scenario.write_text(scenario.read_text(encoding="utf-8") + " ", encoding="utf-8")
    with pytest.raises(CounterexampleRefusal, match="sha256"):
        verify_counterexample(str(out))
    assert predicate_counterexample(str(out)) == 125


def test_self_rehashed_certificate_forgery_fails_transform_replay(tmp_path):
    out, _ = _compile(tmp_path)
    import hotato.counterexample.bundle as bundle

    certificate_path = out / "certificate.json"
    certificate = _load(certificate_path)
    certificate["accepted_steps"][0]["transform"]["operations"][0]["removed_digest"] = (
        "sha256:" + "0" * 64
    )
    certificate_path.write_text(canonical_json(certificate, pretty=True), encoding="utf-8")
    capsule_path = out / "capsule.json"
    capsule = _load(capsule_path)
    capsule["artifact_digests"]["certificate"] = prefixed_digest(certificate)
    capsule["counterexample_id"] = bundle._capsule_id(capsule)
    capsule_path.write_text(canonical_json(capsule, pretty=True), encoding="utf-8")
    _rewrite_manifest(out)
    with pytest.raises(CounterexampleRefusal, match="transform|deleted value"):
        verify_counterexample(str(out))
    assert predicate_counterexample(str(out)) == 125


@pytest.mark.skipif(
    sys.platform == "win32",
    reason="creating the symlink this refusal is exercised against needs the "
           "SeCreateSymbolicLink privilege on Windows (absent by default); the "
           "symlink-rejection logic itself is POSIX-exercised here",
)
def test_nested_directory_symlink_is_refused_even_when_undeclared(tmp_path):
    out, _ = _compile(tmp_path)
    os.symlink(tmp_path, out / "undeclared-link")
    with pytest.raises(CounterexampleRefusal, match="symlink"):
        inspect_counterexample(str(out))


@pytest.mark.skipif(
    sys.platform == "win32",
    reason="creating the ancestor symlink this refusal is exercised against "
           "needs the SeCreateSymbolicLink privilege on Windows (absent by "
           "default); the symlink-rejection logic itself is POSIX-exercised here",
)
def test_output_symlink_ancestor_refuses_before_creating_outside(tmp_path):
    outside = tmp_path / "outside"
    outside.mkdir()
    link = tmp_path / "linked"
    os.symlink(outside, link)
    requested = link / "must-not-be-created" / "case.hotato-repro"
    with pytest.raises(CounterexampleRefusal, match="symlink"):
        compile_counterexample(
            SCENARIO, TEST, target="pii-email", out_dir=str(requested), workspace=FIXTURES,
        )
    assert not (outside / "must-not-be-created").exists()


@pytest.mark.skipif(not hasattr(os, "mkfifo"), reason="FIFOs are POSIX-only")
def test_special_file_input_is_refused_without_reading(tmp_path):
    fifo = tmp_path / "scenario.fifo"
    os.mkfifo(fifo)
    test_path = tmp_path / "pii.test.json"
    shutil.copyfile(TEST, test_path)
    with pytest.raises(CounterexampleRefusal, match="regular file"):
        compile_counterexample(
            str(fifo), str(test_path), target="pii-email",
            out_dir=str(tmp_path / "out"), workspace=str(tmp_path),
        )


def test_share_safe_export_contains_no_source_content(tmp_path):
    private, _ = _compile_fixture(tmp_path, "state", "refund-posted")
    public = tmp_path / "public"
    result = export_counterexample(str(private), out_dir=str(public))
    assert result["runnable"] is False
    all_bytes = b"".join(
        path.read_bytes() for path in public.rglob("*") if path.is_file()
    )
    for secret in (
        b"A-1", b"lookup_order", b"fixture-agent", b"refund_status",
        b"agent_mock.state.orders",
        str(tmp_path).encode("utf-8"),
    ):
        assert secret not in all_bytes
    public_capsule = _load(public / "capsule.json")
    assert public_capsule["privacy"]["omitted"][-1] == "deletion_paths"
    summary = public_capsule["minimality"]["check_summary"]
    assert summary["count"] == sum(summary["outcomes"].values())
    assert "remaining_unit_checks" not in public_capsule["minimality"]
    assert not (public / "input").exists()


def test_model_judge_target_is_refused(tmp_path):
    test = _load(__import__("pathlib").Path(TEST))
    test["assertions"]["rubric"] = [
        {"id": "judge", "kind": "judge_rubric", "criteria": "good"}
    ]
    path = tmp_path / "rubric.test.json"
    path.write_text(canonical_json(test, pretty=True), encoding="utf-8")
    scenario = tmp_path / "scenario.json"
    shutil.copyfile(SCENARIO, scenario)
    with pytest.raises(CounterexampleRefusal, match="model-judged"):
        compile_counterexample(
            str(scenario), str(path), target="judge", out_dir=str(tmp_path / "out"),
            workspace=str(tmp_path),
        )


@pytest.mark.parametrize(
    "assertion,message",
    [
        ({"id": "target", "kind": "timing_contract", "bundle": "external.hotato"}, "external bundle"),
        ({"id": "target", "kind": "latency", "field": "verdict.response_gap_sec", "max": 0.2}, "external timing field"),
        ({"id": "target", "kind": "policy", "pack_path": "external-pack.json"}, "external to the capsule"),
    ],
)
def test_external_or_unsupported_deterministic_targets_are_refused(tmp_path, assertion, message):
    test = _load(__import__("pathlib").Path(TEST))
    test["assertions"]["deterministic"] = [assertion]
    test_path = tmp_path / "unsupported.test.json"
    scenario_path = tmp_path / "scenario.json"
    test_path.write_text(canonical_json(test, pretty=True), encoding="utf-8")
    shutil.copyfile(SCENARIO, scenario_path)
    with pytest.raises(CounterexampleRefusal, match=message):
        compile_counterexample(
            str(scenario_path), str(test_path), target="target",
            out_dir=str(tmp_path / "out"), workspace=str(tmp_path),
        )


def test_output_exists_and_workspace_escape_refuse(tmp_path):
    occupied = tmp_path / "occupied"
    occupied.mkdir()
    with pytest.raises(CounterexampleRefusal, match="already exists"):
        compile_counterexample(
            SCENARIO, TEST, target="pii-email", out_dir=str(occupied), workspace=FIXTURES,
        )
    with pytest.raises(CounterexampleRefusal, match="outside"):
        compile_counterexample(
            SCENARIO, TEST, target="pii-email", out_dir=str(tmp_path / "out"),
            workspace=str(tmp_path),
        )


def test_inspect_checks_integrity_without_execution(tmp_path):
    out, _ = _compile(tmp_path)
    summary = inspect_counterexample(str(out))
    assert summary["exit_code"] == 0
    assert summary["profile"] == "private-runnable-v1"
    assert summary["target"]["assertion_id"] == "pii-email"


def test_verify_pins_proof_engine_but_reproduce_allows_engine_drift(tmp_path, monkeypatch):
    out, _ = _compile(tmp_path)
    import hotato.counterexample.bundle as bundle

    monkeypatch.setattr(bundle, "_evaluator_digest", lambda: "sha256:" + "0" * 64)
    with pytest.raises(CounterexampleRefusal, match="evaluator"):
        verify_counterexample(str(out))
    reproduced = reproduce_counterexample(str(out))
    assert reproduced["exit_code"] == 0
    assert reproduced["evaluator_match"] is False
    assert predicate_counterexample(str(out)) == 1


def test_missing_global_failure_reduces_irrelevant_domain_and_unknown_fields(tmp_path):
    scenario_doc = {
        "kind": "hotato.scenario",
        "version": 1,
        "id": "missing-disclosure-source",
        "goal": {"type": "support", "target": "account"},
        "caller": {
            "script": [
                {"say": "First unrelated turn.", "after": "opening"},
                {"say": "Second unrelated turn."},
                {"say": "Third unrelated turn."},
            ]
        },
        "debug_metadata": {"owner": "must-be-reduced"},
    }
    test_doc = {
        "kind": "hotato.conversation-test",
        "version": 1,
        "id": "missing-disclosure-test",
        "agent": "fixture-agent",
        "assertions": {
            "deterministic": [{
                "id": "recording-disclosure",
                "kind": "phrase",
                "dimension": "policy",
                "regex": "recorded for quality",
                "role": "agent",
            }],
            "rubric": [],
        },
        "inconclusive_policy": "refuse",
    }
    scenario_path = tmp_path / "missing.scenario.json"
    test_path = tmp_path / "missing.test.json"
    scenario_path.write_text(canonical_json(scenario_doc, pretty=True), encoding="utf-8")
    test_path.write_text(canonical_json(test_doc, pretty=True), encoding="utf-8")
    out = tmp_path / "missing.hotato-repro"
    result = compile_counterexample(
        str(scenario_path), str(test_path), target="recording-disclosure",
        out_dir=str(out), workspace=str(tmp_path),
    )
    reduced = _load(out / "input" / "scenario.json")
    assert result["minimality"] == "one_minimal"
    assert len(reduced["caller"]["script"]) == 1
    assert "debug_metadata" not in reduced
    assert set(reduced["caller"]["script"][0]) == {"say"}
    assert verify_counterexample(str(out))["ok"] is True


@pytest.mark.parametrize(
    "stem,target,dimension,kind",
    [
        ("policy", "guarantee-language", "policy", "policy"),
        ("latency", "refund-tool-latency", "speech", "latency"),
        ("state", "refund-posted", "outcome", "state"),
    ],
)
def test_policy_timing_and_outcome_failures_compile_end_to_end(
    tmp_path, stem, target, dimension, kind,
):
    out, compiled = _compile_fixture(tmp_path, stem, target)
    assert compiled["minimality"] == "one_minimal"
    assert compiled["target"]["dimension"] == dimension
    assert compiled["target"]["kind"] == kind
    assert compiled["reduction"]["final"]["turns"] == 1
    assert compiled["reduction"]["final"]["bytes"] < compiled["reduction"]["initial"]["bytes"]
    verified = verify_counterexample(str(out))
    assert verified["ok"] is True
    assert verified["accepted_steps_replayed"] == compiled["reduction"]["accepted"]

    if stem == "latency":
        reduced = _load(out / "input" / "scenario.json")
        assert [row["name"] for row in reduced["agent_mock"]["tools"]] == ["issue_refund"]
    if stem == "state":
        reduced = _load(out / "input" / "scenario.json")
        assert reduced["agent_mock"]["state"]["orders"][0]["refund_status"] == "pending"
        assert set(reduced["agent_mock"]["state"]) == {"orders"}
        assert set(reduced["agent_mock"]["state"]["orders"][0]) == {"id", "refund_status"}
