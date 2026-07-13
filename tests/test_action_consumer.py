"""Local harness for the root GitHub Action (``action.yml`` + ``ci/action/``).

Runs the Action's core script pieces against the consumer conformance fixture
(``tests/fixtures/action-consumer/``) WITHOUT GitHub:

* the five-lane summary renderer over recorded hotato machine results
  (all-pass, mixed-fail, inconclusive, absent-lane, advisory-unavailable,
  contract-verify, malformed), asserting lanes render only what the source
  carries: NOT_RUN for an absent lane, INCONCLUSIVE (never PASS) for missing
  evidence, ERROR (never PASS) for an unusable result;
* the gate end to end as a subprocess (validate, run hotato, write summary +
  outputs, exit), asserting hotato's exit code is preserved exactly, the
  summary is written on pass AND on failure, and a path with spaces works;
* the pinning discipline: every third-party ``uses:`` in action.yml and the
  fixture workflow carries a full 40-hex commit SHA, and the scripts import
  no network module.
"""

import importlib.util
import json
import os
import re
import shutil
import subprocess
import sys

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
ACTION_DIR = os.path.join(ROOT, "ci", "action")
FIXTURE = os.path.join(ROOT, "tests", "fixtures", "action-consumer")
RECORDED = os.path.join(FIXTURE, "recorded")


def _load_summary():
    spec = importlib.util.spec_from_file_location(
        "action_summary", os.path.join(ACTION_DIR, "summary.py")
    )
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


summary = _load_summary()


def _recorded(name):
    with open(os.path.join(RECORDED, name), "r", encoding="utf-8") as fh:
        return json.load(fh)


def _render(doc, exit_code, **meta):
    meta.setdefault("reproduce", "hotato suite run fixture.suite.json")
    return summary.render(doc, exit_code, meta)


def _lane_line(markdown, lane):
    # A lane line is the lane name padded with at least two spaces before its
    # status token, which distinguishes it from prose mentioning the word.
    for line in markdown.splitlines():
        if re.match(rf"{re.escape(lane)}\s{{2,}}\S", line):
            return line
    raise AssertionError(f"lane {lane!r} missing from summary:\n{markdown}")


# ---------------------------------------------------------------------------
# summary rendering over recorded machine results
# ---------------------------------------------------------------------------

def test_summary_all_pass_suite():
    doc = _recorded("suite-run.pass.json")
    md, status = _render(doc, 0)
    assert status == "pass"
    assert "VOICE CONVERSATION REGRESSION" in md
    for lane in ("Outcome", "Conversation", "Speech"):
        assert "PASS" in _lane_line(md, lane)
    # the fixture's passing test has no policy assertion: absent, never PASS
    assert "NOT_RUN" in _lane_line(md, "Policy")
    rel = _lane_line(md, "Reliability")
    assert "3/3" in rel and "Wilson interval: [" in rel
    assert "Reproduce:" in md and "Acceptance checks:" in md
    assert "gate enabled: false" in md
    assert "### Failure headline" not in md


def test_summary_mixed_fail_suite():
    doc = _recorded("suite-run.mixed-fail.json")
    md, status = _render(doc, 1)
    assert status == "fail"
    policy = _lane_line(md, "Policy")
    assert "FAIL" in policy
    assert "no handoff to 'human_supervisor'" in policy
    assert "PASS" in _lane_line(md, "Outcome")
    assert "3/6" in _lane_line(md, "Reliability")
    assert "### Failure headline" in md
    assert "escalate-not-handed-off-test" in md
    # presentation never leaks payloads: reasons and ids only
    assert "exit code: 1" in md


def test_summary_inconclusive_never_renders_pass():
    doc = _recorded("test-run.inconclusive.json")
    md, status = _render(doc, doc["exit_code"])
    assert doc["exit_code"] == 1
    assert status == "inconclusive"
    for lane in ("Outcome", "Conversation", "Speech"):
        line = _lane_line(md, lane)
        assert "INCONCLUSIVE" in line
        assert "PASS" not in line
    assert "PASS" in _lane_line(md, "Policy")


def test_summary_absent_lanes_render_not_run():
    doc = _recorded("test-run.absent-lanes.json")
    md, status = _render(doc, doc["exit_code"])
    assert status == "pass"
    assert "NOT_RUN" in _lane_line(md, "Outcome")
    assert "NOT_RUN" in _lane_line(md, "Speech")
    assert "PASS" in _lane_line(md, "Policy")
    assert "PASS" in _lane_line(md, "Conversation")


def test_summary_advisory_unavailable_reports_error_not_verdict():
    doc = _recorded("test-run.advisory-unavailable.json")
    md, status = _render(doc, doc["exit_code"])
    assert doc["exit_code"] == 0
    assert status == "pass"
    assert "gate enabled: false" in md
    # the unreachable judge reports as ERROR in the advisory section
    assert re.search(r"\d+ error", md)
    assert "1 error" in md or "2 error" in md
    statuses = {r["status"] for r in doc["rubric"]["results"]}
    assert statuses <= {"ERROR", "INCONCLUSIVE"}


def test_summary_contract_verify():
    doc = _recorded("contract-verify.pass.json")
    md, status = _render(doc, doc["exit_code"])
    assert status == "pass"
    # no embedded assertion lane in this bundle: NOT_RUN, never PASS
    for lane in ("Outcome", "Policy", "Conversation", "Speech", "Reliability"):
        assert "NOT_RUN" in _lane_line(md, lane)
    assert "fixture-yield-001 PASS" in md
    assert "1 contracts" in md


def test_summary_malformed_result_is_error_never_pass():
    doc, error = summary.load_result(os.path.join(RECORDED, "malformed.json"))
    assert doc is None and error
    md, status = _render(None, 0, error=error)
    assert status == "error"
    for lane in ("Outcome", "Policy", "Conversation", "Speech", "Reliability"):
        assert "NOT_RUN" in _lane_line(md, lane)
    assert "never a PASS" in md


def test_summary_missing_run_is_error():
    md, status = _render(None, None)
    assert status == "error"
    assert "no machine result was produced" in md


# ---------------------------------------------------------------------------
# gate end to end (subprocess; no GitHub)
# ---------------------------------------------------------------------------

def _workspace(tmp_path):
    ws = tmp_path / "workspace"
    shutil.copytree(FIXTURE, ws / "qa")
    return ws


def _run_gate(tmp_path, ws, inputs):
    gh_summary = tmp_path / "step-summary.md"
    gh_output = tmp_path / "outputs.txt"
    env = {
        k: v for k, v in os.environ.items()
        if not k.startswith(("HOTATO_ACTION_", "GITHUB_"))
    }
    src = os.path.join(ROOT, "src")
    env["PYTHONPATH"] = src + os.pathsep + env.get("PYTHONPATH", "")
    env["HOTATO_HOME"] = str(tmp_path / "hotato-home")
    env["GITHUB_WORKSPACE"] = str(ws)
    env["GITHUB_STEP_SUMMARY"] = str(gh_summary)
    env["GITHUB_OUTPUT"] = str(gh_output)
    env["HOTATO_ACTION_VERSION"] = "preinstalled"
    for key, value in inputs.items():
        # the same input-name mapping action.yml performs
        name = "VERSION" if key == "hotato-version" else (
            key.upper().replace("-", "_"))
        env[f"HOTATO_ACTION_{name}"] = value
    proc = subprocess.run(
        [sys.executable, os.path.join(ACTION_DIR, "gate.py")],
        env=env, capture_output=True, text=True, cwd=str(ws), timeout=300,
    )
    outputs = {}
    if gh_output.exists():
        for line in gh_output.read_text().splitlines():
            key, _, value = line.partition("=")
            outputs[key] = value
    step_summary = gh_summary.read_text() if gh_summary.exists() else ""
    return proc, outputs, step_summary


def test_gate_pass_suite_exit_zero(tmp_path):
    ws = _workspace(tmp_path)
    proc, outputs, step_summary = _run_gate(tmp_path, ws, {
        "suite": "qa/suite/pass.suite.json",
        "agent": "consumer-agent",
        "release": "harness-release",
    })
    assert proc.returncode == 0, proc.stderr
    assert outputs["exit-code"] == "0"
    assert outputs["status"] == "pass"
    assert outputs["hotato-version"]

    result = ws / outputs["suite-result"]
    assert result.is_file()
    assert json.loads(result.read_text())["kind"] == "hotato.suite-run"
    assert (ws / outputs["summary"]).is_file()
    # the job summary is written on pass, not only on failure
    assert "VOICE CONVERSATION REGRESSION" in step_summary


def test_gate_mixed_fail_preserves_exit_and_writes_summary(tmp_path):
    ws = _workspace(tmp_path)
    proc, outputs, step_summary = _run_gate(tmp_path, ws, {
        "suite": "qa/suite/consumer.suite.json",
        "agent": "consumer-agent",
    })
    assert proc.returncode == 1, proc.stderr
    assert outputs["exit-code"] == "1"
    assert outputs["status"] == "fail"
    assert "VOICE CONVERSATION REGRESSION" in step_summary
    assert "FAIL" in step_summary
    assert "Reproduce:" in step_summary
    # the reproduce command is the exact executed argv
    assert "hotato suite run qa/suite/consumer.suite.json" in step_summary


def test_gate_paths_with_spaces(tmp_path):
    ws = _workspace(tmp_path)
    proc, outputs, _ = _run_gate(tmp_path, ws, {
        "suite": "qa/with spaces/spaced.suite.json",
        "agent": "consumer-agent",
        "output": "out dir/results",
    })
    assert proc.returncode == 0, proc.stderr
    assert outputs["status"] == "pass"
    assert (ws / "out dir" / "results" / "suite-run.json").is_file()
    assert "'qa/with spaces/spaced.suite.json'" in (
        ws / outputs["summary"]).read_text()


def test_gate_test_run_with_evidence(tmp_path):
    ws = _workspace(tmp_path)
    proc, outputs, step_summary = _run_gate(tmp_path, ws, {
        "test": "qa/test/two-lane.conversation-test.yaml",
        "agent": "consumer-agent",
        "transcript": "qa/test/refund.transcript.json",
    })
    assert proc.returncode == 0, proc.stderr
    assert outputs["status"] == "pass"
    assert "NOT_RUN" in step_summary  # outcome and speech lanes are absent


@pytest.mark.parametrize("inputs", [
    {},  # no mode at all
    {"suite": "qa/suite/pass.suite.json",
     "test": "qa/test/two-lane.conversation-test.yaml",
     "agent": "consumer-agent"},  # mutually exclusive
    {"suite": "../outside.suite.json", "agent": "consumer-agent"},  # traversal
    {"suite": "/etc/hostname", "agent": "consumer-agent"},  # absolute
    {"suite": "qa/suite/pass.suite.json", "agent": "bad agent!"},  # unsafe id
    {"suite": "qa/suite/pass.suite.json"},  # agent missing
    {"suite": "qa/suite/pass.suite.json", "agent": "a",
     "gate-advisory": "true"},  # advisory gate is a test-run flag
    {"suite": "qa/suite/pass.suite.json", "agent": "a",
     "hotato-version": "latest"},  # unpinned install refused
])
def test_gate_refuses_bad_inputs(tmp_path, inputs):
    ws = _workspace(tmp_path)
    proc, outputs, step_summary = _run_gate(tmp_path, ws, inputs)
    assert proc.returncode == 2
    assert outputs["status"] == "error"
    assert outputs["exit-code"] == "2"
    # the summary renders even when the run was refused
    assert "ERROR" in step_summary


def test_gate_symlink_escape_refused(tmp_path):
    ws = _workspace(tmp_path)
    outside = tmp_path / "outside.suite.json"
    outside.write_text("{}")
    (ws / "link.suite.json").symlink_to(outside)
    proc, outputs, _ = _run_gate(tmp_path, ws, {
        "suite": "link.suite.json", "agent": "consumer-agent",
    })
    assert proc.returncode == 2
    assert outputs["status"] == "error"


# ---------------------------------------------------------------------------
# pinning and egress discipline
# ---------------------------------------------------------------------------

_PINNED = re.compile(r"^\s*-?\s*uses:\s*(\S+)")


def _uses_of(path):
    out = []
    with open(path, "r", encoding="utf-8") as fh:
        for line in fh:
            m = _PINNED.match(line)
            if m:
                out.append(m.group(1))
    return out


def test_every_third_party_action_is_sha_pinned():
    for rel in ("action.yml",
                os.path.join("tests", "fixtures", "action-consumer",
                             "workflows", "consumer.yml")):
        for ref in _uses_of(os.path.join(ROOT, rel)):
            if ref == "./":
                continue  # the Action-path fixture mechanism
            _, _, pin = ref.partition("@")
            assert re.fullmatch(r"[0-9a-f]{40}", pin), (
                f"{rel}: {ref} is not pinned by a full commit SHA"
            )


def test_action_yaml_parses():
    yaml = pytest.importorskip("yaml")
    with open(os.path.join(ROOT, "action.yml"), "r", encoding="utf-8") as fh:
        doc = yaml.safe_load(fh)
    assert doc["runs"]["using"] == "composite"
    for name in ("output", "suite-result", "summary", "records",
                 "exit-code", "status", "hotato-version"):
        assert name in doc["outputs"], name
    for name in ("suite", "test", "contracts", "agent", "release", "output",
                 "parallel", "gate-advisory", "hotato-version"):
        assert name in doc["inputs"], name


def test_action_scripts_import_no_network_module():
    banned = re.compile(
        r"^\s*(import|from)\s+(socket|urllib|http|requests|ssl)\b",
        re.MULTILINE,
    )
    for name in ("gate.py", "summary.py"):
        with open(os.path.join(ACTION_DIR, name), "r", encoding="utf-8") as fh:
            source = fh.read()
        assert not banned.search(source), f"{name} imports a network module"
def test_gate_action_mode_runs_zero_egress_via_pythonpath(tmp_path):
    """The default 'action' mode must run the pinned checkout off PYTHONPATH --
    never a pip install -- so a consumer's gate makes no package-index request."""
    ws = _workspace(tmp_path)
    gh_summary = tmp_path / "summary.md"
    gh_output = tmp_path / "outputs.txt"
    # A clean env: do NOT preset PYTHONPATH, so gate.py's own PYTHONPATH wiring
    # is what makes the checkout importable. HOTATO_ACTION_PATH is the checkout.
    env = {k: v for k, v in os.environ.items()
           if not k.startswith(("HOTATO_ACTION_", "GITHUB_", "PYTHONPATH"))}
    env["HOTATO_HOME"] = str(tmp_path / "hotato-home")
    env["GITHUB_WORKSPACE"] = str(ws)
    env["GITHUB_STEP_SUMMARY"] = str(gh_summary)
    env["GITHUB_OUTPUT"] = str(gh_output)
    env["HOTATO_ACTION_VERSION"] = "action"
    env["HOTATO_ACTION_PATH"] = ROOT
    env["HOTATO_ACTION_SUITE"] = "qa/suite/pass.suite.json"
    env["HOTATO_ACTION_AGENT"] = "consumer-agent"
    env["HOTATO_ACTION_RELEASE"] = "harness-release"
    proc = subprocess.run(
        [sys.executable, os.path.join(ACTION_DIR, "gate.py")],
        env=env, capture_output=True, text=True, cwd=str(ws), timeout=300)
    assert proc.returncode == 0, proc.stderr
    summary = gh_summary.read_text() if gh_summary.exists() else ""
    assert "PYTHONPATH, zero-egress" in summary, summary
    # the pip install path must not have run for the action mode
    assert "pip install" not in (proc.stdout + proc.stderr)
