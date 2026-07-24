"""End-to-end CLI contracts for proof-preserving counterexamples."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys

from hotato import cli

FIXTURES = os.path.join(os.path.dirname(__file__), "fixtures", "counterexample")


def _inputs(tmp_path):
    workspace = tmp_path / "workspace with spaces"
    workspace.mkdir()
    scenario = workspace / "scenario with spaces.json"
    test = workspace / "test with spaces.json"
    shutil.copyfile(os.path.join(FIXTURES, "pii.scenario.json"), scenario)
    shutil.copyfile(os.path.join(FIXTURES, "pii.test.json"), test)
    return workspace, scenario, test


def _compile_args(workspace, scenario, test, output, *, budget=512, fmt="json"):
    return [
        "counterexample", "compile",
        "--scenario", str(scenario),
        "--test", str(test),
        "--target", "pii-email",
        "--out", str(output),
        "--workspace", str(workspace),
        "--budget", str(budget),
        "--format", fmt,
    ]


def test_cli_json_full_cycle_is_machine_pure_with_space_paths(tmp_path, capsys):
    workspace, scenario, test = _inputs(tmp_path)
    private = tmp_path / "private output with spaces.hotato-repro"

    assert cli.main(_compile_args(workspace, scenario, test, private)) == 0
    compiled = json.loads(capsys.readouterr().out)
    assert compiled["kind"] == "counterexample-compile"
    assert compiled["minimality"] == "one_minimal"
    assert compiled["output"] == str(private)

    for command, kind in (
        ("verify", "counterexample-verify"),
        ("reproduce", "counterexample-reproduce"),
        ("inspect", "counterexample-inspect"),
    ):
        assert cli.main(["counterexample", command, str(private), "--format", "json"]) == 0
        payload = json.loads(capsys.readouterr().out)
        assert payload["kind"] == kind
        assert payload["exit_code"] == 0

    share = tmp_path / "share output with spaces"
    assert cli.main([
        "counterexample", "export", str(private), "--out", str(share),
        "--format", "json",
    ]) == 0
    exported = json.loads(capsys.readouterr().out)
    assert exported["kind"] == "counterexample-export"
    assert exported["runnable"] is False
    assert not (share / "input").exists()

    assert cli.main(["counterexample", "predicate", str(private)]) == 1
    assert capsys.readouterr().out == ""


def test_cli_budget_exhaustion_writes_capsule_and_exits_one(tmp_path, capsys):
    workspace, scenario, test = _inputs(tmp_path)
    output = tmp_path / "budget.hotato-repro"
    assert cli.main(_compile_args(
        workspace, scenario, test, output, budget=1,
    )) == 1
    payload = json.loads(capsys.readouterr().out)
    assert payload["exit_code"] == 1
    assert payload["minimality"] == "budget_exhausted"
    assert output.is_dir()


def test_lab_listing_promises_explicit_proof_status_not_unconditional_minimality():
    # counterexample lives on the lab surface (1.17.0 narrowing), so its
    # one-line description renders in `hotato lab --help` rather than the
    # top-level listing.
    help_text = " ".join(cli._render_lab_help(cli.build_parser()).split())
    assert "offline regression capsule with explicit proof status" in help_text
    assert "into a minimal, portable regression capsule" not in help_text


def test_cli_refusal_is_structured_json_and_leaves_no_partial_output(tmp_path, capsys):
    workspace, scenario, test = _inputs(tmp_path)
    output = tmp_path / "must not exist"
    assert cli.main(_compile_args(
        workspace, scenario, test, output, budget=0,
    )) == 2
    payload = json.loads(capsys.readouterr().out)
    assert payload["ok"] is False
    assert payload["exit_code"] == 2
    assert payload["error_code"] == "usage_error"
    assert not output.exists()


def test_cli_text_escapes_terminal_control_characters(tmp_path, capsys):
    workspace, scenario, test = _inputs(tmp_path)
    document = json.loads(test.read_text(encoding="utf-8"))
    document["assertions"]["deterministic"][0]["id"] = "pii\x1b[2J\nline"
    test.write_text(json.dumps(document), encoding="utf-8")
    output = tmp_path / "control.hotato-repro"

    args = _compile_args(workspace, scenario, test, output, fmt="text")
    target_index = args.index("pii-email")
    args[target_index] = "pii\x1b[2J\nline"
    assert cli.main(args) == 0
    captured = capsys.readouterr()
    assert "\x1b" not in captured.out
    assert "\\u001b" in captured.out
    assert "\\u000a" in captured.out
    assert captured.err == ""


def test_generated_scripts_are_executable_and_predicate_maps_present_failure(tmp_path):
    workspace, scenario, test = _inputs(tmp_path)
    output = tmp_path / "scripts.hotato-repro"
    assert cli.main(_compile_args(workspace, scenario, test, output)) == 0
    reproduce = output / "reproduce.sh"
    predicate = output / "predicate.sh"
    if sys.platform == "win32":
        # Windows records no POSIX execute bit; the helpers run through the
        # interpreter. Exercise the documented `sh reproduce.sh` form when a
        # shell and the entry point are present, and the CLI verb the helper
        # wraps either way.
        sh = shutil.which("sh")
        if sh and shutil.which("hotato"):
            completed = subprocess.run(
                [sh, str(reproduce)], capture_output=True, text=True
            )
            assert completed.returncode == 0, completed.stderr
        assert cli.main(
            ["counterexample", "reproduce", str(output), "--format", "json"]
        ) == 0
    else:
        assert os.access(reproduce, os.X_OK)
        assert os.access(predicate, os.X_OK)
    assert "counterexample reproduce" in reproduce.read_text(encoding="utf-8")
    assert "counterexample reproduce" in predicate.read_text(encoding="utf-8")
    assert "counterexample verify" not in predicate.read_text(encoding="utf-8")
