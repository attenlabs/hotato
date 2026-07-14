"""Fail-closed resource and malformed-input probes for proof capsules."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from hotato.counterexample import (
    CounterexampleRefusal,
    bundle,
    compile_counterexample,
    verify_counterexample,
)
from hotato.counterexample.model import (
    MAX_CAPSULE_MEMBER_BYTES,
    canonical_json,
    load_json,
)


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
