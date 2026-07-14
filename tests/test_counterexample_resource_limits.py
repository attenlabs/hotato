"""Fail-closed resource and malformed-input probes for proof capsules."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from hotato import assert_ as assertions
from hotato.counterexample import (
    CounterexampleRefusal,
    bundle,
    compile_counterexample,
    verify_counterexample,
)
from hotato.counterexample.model import (
    MAX_ACCEPTED_STEPS,
    MAX_CAPSULE_MEMBER_BYTES,
    MAX_MINIMALITY_UNITS,
    MAX_TRANSFORM_OPERATIONS,
    canonical_json,
    load_json,
)
from hotato.counterexample.oracle import target_assertion
from hotato.counterexample.reducers import verify_single_units
from hotato.counterexample.search import SearchState


def _write(path: Path, value: object) -> None:
    path.write_text(canonical_json(value, pretty=True), encoding="utf-8")


def _scenario(text: str = "help") -> dict:
    return {
        "kind": "hotato.scenario",
        "version": 1,
        "id": "resource-source",
        "goal": {"type": "support", "target": "account"},
        "caller": {"script": [{"say": text}]},
    }


def _test(assertion: dict) -> dict:
    return {
        "kind": "hotato.conversation-test",
        "version": 1,
        "id": "resource-test",
        "agent": "fixture-agent",
        "assertions": {
            "deterministic": [{"id": "target", **assertion}],
            "rubric": [],
        },
    }


def _compile(tmp_path: Path, scenario: dict, test_doc: dict) -> Path:
    scenario_path = tmp_path / "scenario.json"
    test_path = tmp_path / "test.json"
    output = tmp_path / "capsule"
    _write(scenario_path, scenario)
    _write(test_path, test_doc)
    compile_counterexample(
        str(scenario_path),
        str(test_path),
        target="target",
        out_dir=str(output),
        workspace=str(tmp_path),
    )
    return output


def test_proof_lane_refuses_backtracking_regex_profile(tmp_path: Path) -> None:
    with pytest.raises(CounterexampleRefusal) as raised:
        _compile(
            tmp_path,
            _scenario("a" * 200 + "!"),
            _test({"kind": "phrase", "regex": "(a+)+$"}),
        )
    assert raised.value.code == "unsupported_target_regex"


@pytest.mark.parametrize(
    "pattern",
    ["a*b", "[ab]*c", ".*Z", "a+Z", "a?Z", "a{3}Z", "a{0,999999}Z"],
)
def test_proof_lane_refuses_every_variable_quantifier(
    tmp_path: Path, pattern: str
) -> None:
    with pytest.raises(CounterexampleRefusal) as raised:
        _compile(
            tmp_path,
            _scenario("a" * 20_000),
            _test({"kind": "phrase", "regex": pattern}),
        )
    assert raised.value.code == "unsupported_target_regex"


def test_oversized_proof_regex_is_refused_before_python_compiles_it(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = 0

    def forbidden_compile(*_args, **_kwargs):
        nonlocal calls
        calls += 1
        raise AssertionError("oversized proof regex reached re.compile")

    monkeypatch.setattr(assertions.re, "compile", forbidden_compile)
    with pytest.raises(CounterexampleRefusal) as raised:
        target_assertion(
            _test({"kind": "phrase", "regex": "x" * 1_025}),
            "target",
        )
    assert raised.value.code == "unsupported_target_regex"
    assert calls == 0


def test_every_outcome_lane_is_regex_preflighted_before_generic_validation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = 0

    def forbidden_compile(*_args, **_kwargs):
        nonlocal calls
        calls += 1
        raise AssertionError("unpreflighted outcome regex reached re.compile")

    monkeypatch.setattr(assertions.re, "compile", forbidden_compile)
    with pytest.raises(CounterexampleRefusal) as raised:
        target_assertion(
            _test(
                {
                    "kind": "outcome",
                    "all_of": [{"phrase": "safe"}],
                    "any_of": [{"phrase": "x" * 1_025}],
                }
            ),
            "target",
        )
    assert raised.value.code == "unsupported_target_regex"
    assert calls == 0


@pytest.mark.parametrize(
    "assertion",
    [
        {
            "kind": "tool_call",
            "name": "refund",
            "args_subset": {"": "A"},
        },
        {
            "kind": "tool_result",
            "name": "refund",
            "result_subset": {"": "posted"},
        },
        {
            "kind": "entity_accuracy",
            "reference": {"order_id": None},
        },
        {
            "kind": "outcome",
            "all_of": [{"field_present": "timing.latency_ms"}],
        },
        {"kind": "dtmf", "digits": "99"},
        {"kind": "latency", "span_type": "caller_audio_active", "max_ms": 100},
    ],
)
def test_unrepresentable_scripted_targets_are_refused_before_source_replay(
    tmp_path: Path, assertion: dict
) -> None:
    with pytest.raises(CounterexampleRefusal) as raised:
        _compile(tmp_path, _scenario(), _test(assertion))
    assert raised.value.code == "unsupported_target"
    assert not (tmp_path / "capsule").exists()


def test_minimality_unit_limit_refuses_before_candidate_replay() -> None:
    scenario = _scenario("missing")
    scenario.update(
        {
            f"optional_{index:04d}": index
            for index in range(MAX_MINIMALITY_UNITS + 1)
        }
    )
    calls = 0

    def evaluator(_candidate: dict) -> dict:
        nonlocal calls
        calls += 1
        return {"status": "ABSENT"}

    state = SearchState(MAX_MINIMALITY_UNITS + 1, evaluator)
    with pytest.raises(CounterexampleRefusal) as raised:
        verify_single_units(scenario, state, set())
    assert raised.value.code == "minimality_work_limit"
    assert calls == 0


def test_search_refuses_accepted_chain_limit_before_oracle_call() -> None:
    calls = 0

    def evaluator(_candidate: dict) -> dict:
        nonlocal calls
        calls += 1
        return {"status": "PRESERVED"}

    state = SearchState(MAX_ACCEPTED_STEPS + 1, evaluator)
    state.accepted = MAX_ACCEPTED_STEPS
    with pytest.raises(CounterexampleRefusal) as raised:
        state.try_accept(
            {"keep": 1, "remove": 2},
            {"keep": 1},
            {"kind": "remove-field", "phase": "test", "path": "remove"},
        )
    assert raised.value.code == "accepted_step_limit"
    assert calls == 0


def test_search_refuses_oversized_transform_before_oracle_call() -> None:
    calls = 0

    def evaluator(_candidate: dict) -> dict:
        nonlocal calls
        calls += 1
        return {"status": "PRESERVED"}

    current = {f"key_{index:05d}": index for index in range(MAX_TRANSFORM_OPERATIONS + 1)}
    state = SearchState(1, evaluator)
    with pytest.raises(CounterexampleRefusal) as raised:
        state.try_accept(
            current,
            {},
            {"kind": "remove-path-set", "phase": "test", "paths": ["pending"]},
        )
    assert raised.value.code == "transform_work_limit"
    assert calls == 0


def test_operation_shape_refuses_oversized_path_set_before_sorting(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    paths = [f"environment.key_{index:05d}" for index in range(MAX_TRANSFORM_OPERATIONS + 1)]
    sorted_called = False

    def forbidden_sorted(*_args, **_kwargs):
        nonlocal sorted_called
        sorted_called = True
        raise AssertionError("oversized path set reached sorting")

    monkeypatch.setattr(bundle, "sorted", forbidden_sorted, raising=False)
    with pytest.raises(CounterexampleRefusal) as raised:
        bundle._operation_shape(
            {"kind": "remove-path-set", "phase": "test", "paths": paths}
        )
    assert raised.value.code == "certificate_schema"
    assert sorted_called is False


def test_large_group_reduction_emits_verifiable_bounded_transforms(
    tmp_path: Path,
) -> None:
    scenario = _scenario("person@example.com")
    scenario["environment"] = {
        f"optional_{index:05d}": index
        for index in range(MAX_TRANSFORM_OPERATIONS * 2 + 2)
    }
    output = _compile(
        tmp_path,
        scenario,
        _test(
            {
                "kind": "pii",
                "detectors": ["email"],
                "mode": "must_not_leak",
            }
        ),
    )
    certificate = json.loads((output / "certificate.json").read_text(encoding="utf-8"))
    assert certificate["accepted_steps"]
    assert all(
        len(step["transform"]["operations"]) <= MAX_TRANSFORM_OPERATIONS
        for step in certificate["accepted_steps"]
    )
    verified = verify_counterexample(str(output))
    assert verified["ok"] is True


def test_certificate_step_limit_is_preflighted(tmp_path: Path) -> None:
    output = _compile(
        tmp_path,
        _scenario("person@example.com"),
        _test(
            {
                "kind": "pii",
                "detectors": ["email"],
                "mode": "must_not_leak",
            }
        ),
    )
    certificate = json.loads((output / "certificate.json").read_text(encoding="utf-8"))
    certificate["accepted_steps"] = [
        certificate["accepted_steps"][0]
        for _ in range(MAX_ACCEPTED_STEPS + 1)
    ]
    certificate["candidate_evaluations"] = MAX_ACCEPTED_STEPS + 1
    with pytest.raises(CounterexampleRefusal) as raised:
        bundle._validate_certificate_document(certificate)
    assert raised.value.code == "certificate_schema"


def test_cached_preserved_journal_row_is_refused(tmp_path: Path) -> None:
    output = _compile(
        tmp_path,
        _scenario("person@example.com"),
        _test(
            {
                "kind": "pii",
                "detectors": ["email"],
                "mode": "must_not_leak",
            }
        ),
    )
    certificate = json.loads((output / "certificate.json").read_text(encoding="utf-8"))
    capsule = json.loads((output / "capsule.json").read_text(encoding="utf-8"))
    rows = [
        json.loads(line)
        for line in (output / "reduction.jsonl").read_text(encoding="utf-8").splitlines()
    ]
    preserved = next(row for row in rows if row["status"] == "PRESERVED")
    preserved["cached"] = True
    journal = "".join(canonical_json(row) for row in rows).encode("utf-8")
    with pytest.raises(CounterexampleRefusal) as raised:
        bundle._validate_journal(journal, certificate, capsule["reduction"])
    assert raised.value.code == "journal_chain_mismatch"


def test_outcome_predicate_wrong_scalar_type_never_reaches_regex_engine(
    tmp_path: Path,
) -> None:
    with pytest.raises(ValueError, match="non-empty string"):
        _compile(
            tmp_path,
            _scenario(),
            _test({"kind": "outcome", "all_of": [{"phrase": []}]}),
        )


def test_mock_tool_at_ms_wrong_type_is_rejected_before_render(tmp_path: Path) -> None:
    scenario = _scenario()
    scenario["agent_mock"] = {
        "tools": [{"name": "lookup", "at_ms": {}}]
    }
    with pytest.raises(ValueError, match="at_ms"):
        _compile(
            tmp_path,
            scenario,
            _test({"kind": "tool_call", "name": "refund"}),
        )


def test_huge_json_integer_becomes_coded_refusal(tmp_path: Path) -> None:
    path = tmp_path / "huge-int.json"
    path.write_text('{"value":' + "9" * 5_000 + "}\n", encoding="utf-8")
    with pytest.raises(CounterexampleRefusal) as raised:
        load_json(str(path))
    assert raised.value.code == "invalid_json"


def test_oracle_scenario_byte_limit_is_fail_closed(tmp_path: Path) -> None:
    with pytest.raises(CounterexampleRefusal) as raised:
        _compile(
            tmp_path,
            _scenario("x" * (2 * 1024 * 1024)),
            _test({"kind": "phrase", "regex": "missing"}),
        )
    assert raised.value.code == "source_unresolved"
    assert "resource_limit_exceeded" in str(raised.value)


def test_oracle_evidence_row_limit_is_fail_closed(tmp_path: Path) -> None:
    text = " ".join(f"a{index}@b.co" for index in range(10_100))
    with pytest.raises(CounterexampleRefusal) as raised:
        _compile(
            tmp_path,
            _scenario(text),
            _test(
                {
                    "kind": "pii",
                    "detectors": ["email"],
                    "mode": "must_not_leak",
                }
            ),
        )
    assert raised.value.code == "source_unresolved"
    assert "resource_limit_exceeded" in str(raised.value)


def test_manifest_declared_member_limit_is_checked_before_member_io(
    tmp_path: Path,
) -> None:
    output = _compile(
        tmp_path,
        _scenario("person@example.com"),
        _test(
            {
                "kind": "pii",
                "detectors": ["email"],
                "mode": "must_not_leak",
            }
        ),
    )
    manifest_path = output / "MANIFEST.sha256.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["files"][0]["bytes"] = MAX_CAPSULE_MEMBER_BYTES + 1
    _write(manifest_path, manifest)

    with pytest.raises(CounterexampleRefusal) as raised:
        verify_counterexample(str(output))
    assert raised.value.code == "capsule_member_too_large"


def test_journal_row_limit_is_fail_closed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    output = _compile(
        tmp_path,
        _scenario("person@example.com"),
        _test(
            {
                "kind": "pii",
                "detectors": ["email"],
                "mode": "must_not_leak",
            }
        ),
    )
    assert len((output / "reduction.jsonl").read_bytes().splitlines()) > 1
    monkeypatch.setattr(bundle, "_MAX_JOURNAL_ROWS", 1)
    with pytest.raises(CounterexampleRefusal) as raised:
        verify_counterexample(str(output))
    assert raised.value.code == "journal_too_many_rows"
