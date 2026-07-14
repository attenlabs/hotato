"""MCP parity and sandboxing for counterexample compile/verify/reproduce."""

from __future__ import annotations

import os
import shutil

from hotato import mcp_server


FIXTURES = os.path.join(os.path.dirname(__file__), "fixtures", "counterexample")


def _roots(tmp_path, monkeypatch):
    inputs = tmp_path / "inputs"
    reports = tmp_path / "reports"
    inputs.mkdir()
    reports.mkdir()
    scenario = inputs / "pii.scenario.json"
    test = inputs / "pii.test.json"
    shutil.copyfile(os.path.join(FIXTURES, "pii.scenario.json"), scenario)
    shutil.copyfile(os.path.join(FIXTURES, "pii.test.json"), test)
    monkeypatch.setenv("HOTATO_MCP_INPUT_DIR", str(inputs))
    monkeypatch.setenv("HOTATO_MCP_REPORT_DIR", str(reports))
    return inputs, reports, scenario, test


def _assert_control(response):
    assert set((
        "evidence_status", "refusal_reason", "artifact_digests",
        "pending_irreversible_action",
    )).issubset(response)
    assert response["pending_irreversible_action"] is None


def test_mcp_counterexample_full_cycle(tmp_path, monkeypatch):
    _inputs, reports, scenario, test = _roots(tmp_path, monkeypatch)
    out = reports / "pii.hotato-repro"
    compiled = mcp_server.mcp_counterexample_compile(
        str(scenario), str(test), "pii-email", str(out), budget=512,
    )
    _assert_control(compiled)
    assert compiled["exit_code"] == 0
    assert compiled["minimality"] == "one_minimal"
    assert compiled["evidence_status"] == 1
    assert compiled["artifact_digests"] == [
        compiled["counterexample_id"], compiled["target"]["fingerprint"],
    ]

    verified = mcp_server.mcp_counterexample_verify(str(out))
    _assert_control(verified)
    assert verified["ok"] is True
    assert verified["accepted_steps_replayed"] == compiled["reduction"]["accepted"]

    reproduced = mcp_server.mcp_counterexample_reproduce(str(out))
    _assert_control(reproduced)
    assert reproduced["status"] == "failure_reproduced"
    assert reproduced["evaluator_match"] is True


def test_mcp_counterexample_refuses_path_escape_and_overwrite(tmp_path, monkeypatch):
    _inputs, reports, scenario, test = _roots(tmp_path, monkeypatch)
    escaped = mcp_server.mcp_counterexample_compile(
        str(scenario), str(test), "pii-email", str(tmp_path.parent / "escape"),
    )
    _assert_control(escaped)
    assert escaped["ok"] is False
    assert escaped["exit_code"] == 2

    occupied = reports / "occupied"
    occupied.mkdir()
    overwrite = mcp_server.mcp_counterexample_compile(
        str(scenario), str(test), "pii-email", str(occupied),
    )
    _assert_control(overwrite)
    assert overwrite["ok"] is False
    assert overwrite["exit_code"] == 2


def test_counterexample_tools_are_in_canonical_mcp_inventory():
    assert {
        "counterexample_compile", "counterexample_verify", "counterexample_reproduce",
    }.issubset(set(mcp_server.TOOL_NAMES))
