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


def _load_gate():
    spec = importlib.util.spec_from_file_location(
        "action_gate", os.path.join(ACTION_DIR, "gate.py")
    )
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


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


@pytest.mark.skipif(
    sys.platform == "win32",
    reason="creating the escaping symlink this refusal is exercised against "
           "needs the SeCreateSymbolicLink privilege on Windows (absent by "
           "default); the symlink-escape refusal itself is POSIX-exercised here",
)
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


# ---------------------------------------------------------------------------
# Failure Records: records-by-default, one per non-passing unit (Workstream D)
# ---------------------------------------------------------------------------

_SHA256_DIR = re.compile(r"^sha256-[0-9a-f]{64}$")


def _load_index(ws, records_index):
    with open(os.path.join(str(ws), records_index), "r", encoding="utf-8") as fh:
        return json.load(fh)


def _verify_record_valid(ws, record_json_rel):
    """Run `hotato record verify` (Slice B) over a rendered record and return
    the parsed JSON verdict; exit 0 and valid=true is a valid share-safe
    record."""
    env = {k: v for k, v in os.environ.items()
           if not k.startswith(("HOTATO_ACTION_", "GITHUB_"))}
    env["PYTHONPATH"] = os.path.join(ROOT, "src") + os.pathsep + env.get(
        "PYTHONPATH", "")
    proc = subprocess.run(
        [sys.executable, "-m", "hotato", "record", "verify",
         record_json_rel, "--format", "json"],
        env=env, capture_output=True, text=True, cwd=str(ws), timeout=120)
    return proc.returncode, json.loads(proc.stdout)


def test_render_records_yaml_default_is_true():
    """The Action ships records-by-default: the render-records input default is
    the string 'true', record-limit is 1..500 with a 100 default, and the new
    count/index/total outputs are declared."""
    yaml = pytest.importorskip("yaml")
    with open(os.path.join(ROOT, "action.yml"), "r", encoding="utf-8") as fh:
        doc = yaml.safe_load(fh)
    assert doc["inputs"]["render-records"]["default"] == "true"
    assert "one share-safe Failure Record per non-passing unit" in \
        doc["inputs"]["render-records"]["description"]
    assert doc["inputs"]["record-limit"]["default"] == "100"
    for name in ("records-index", "records-count", "records-total", "records"):
        assert name in doc["outputs"], name


def test_records_mixed_fail_writes_index_and_one_valid_record(tmp_path):
    """A mixed pass/fail suite renders exactly one Failure Record (for the one
    non-passing unit), under a digest-scoped index, and it validates."""
    ws = _workspace(tmp_path)
    proc, outputs, step_summary = _run_gate(tmp_path, ws, {
        "suite": "qa/suite/consumer.suite.json",
        "agent": "consumer-agent",
        "render-records": "true",
    })
    assert proc.returncode == 1, proc.stderr
    assert outputs["status"] == "fail"
    assert outputs["records-count"] == "1"
    assert outputs["records-total"] == "1"
    assert "::warning::" not in proc.stdout

    records_dir = outputs["records"]
    assert records_dir.startswith(".hotato/results/records/sha256-")
    assert (ws / records_dir).is_dir()
    index = _load_index(ws, outputs["records-index"])
    assert index["kind"] == "hotato.failure-record-index.v1"
    assert index["rendered"] == 1 and index["total_failures"] == 1
    assert len(index["records"]) == 1
    entry = index["records"][0]
    assert entry["test_id"] == "escalate-not-handed-off-test"
    assert _SHA256_DIR.match(entry["directory"])
    # exactly the index + one child sha256-<hex>/ dir of record files
    children = sorted(p.name for p in (ws / records_dir).iterdir())
    assert children == sorted(["index.json", "index.md", entry["directory"]])
    record_json = os.path.join(records_dir, entry["directory"],
                               "failure-record.json")
    code, verdict = _verify_record_valid(ws, record_json)
    assert code == 0 and verdict["valid"] is True
    # the summary carries the bounded headline and the record path
    assert "### Failure Records (1)" in step_summary
    assert entry["headline"] in step_summary
    assert record_json.replace(".json", ".md") in step_summary


def test_records_two_failures_write_two_records(tmp_path):
    """A synthetic two-failure suite renders two records under a count-2
    index, one digest directory each, in source order."""
    ws = _workspace(tmp_path)
    proc, outputs, step_summary = _run_gate(tmp_path, ws, {
        "suite": "qa/suite/two-fail.suite.json",
        "agent": "consumer-agent",
        "render-records": "true",
    })
    assert proc.returncode == 1, proc.stderr
    assert outputs["records-count"] == "2"
    assert outputs["records-total"] == "2"
    index = _load_index(ws, outputs["records-index"])
    assert index["rendered"] == 2 and index["total_failures"] == 2
    ids = [e["test_id"] for e in index["records"]]
    assert ids == ["escalate-not-handed-off-test",
                   "escalate-not-handed-off-b-test"]  # source order preserved
    dirs = {e["directory"] for e in index["records"]}
    assert len(dirs) == 2 and all(_SHA256_DIR.match(d) for d in dirs)
    for entry in index["records"]:
        rj = os.path.join(outputs["records"], entry["directory"],
                          "failure-record.json")
        code, verdict = _verify_record_valid(ws, rj)
        assert code == 0 and verdict["valid"] is True
    assert "### Failure Records (2)" in step_summary


def test_records_pass_returns_empty_outputs(tmp_path):
    """An all-pass suite fabricates no failure: the record outputs are empty
    and the summary states zero non-passing units."""
    ws = _workspace(tmp_path)
    proc, outputs, step_summary = _run_gate(tmp_path, ws, {
        "suite": "qa/suite/pass.suite.json",
        "agent": "consumer-agent",
        "release": "harness-release",
        "render-records": "true",
    })
    assert proc.returncode == 0, proc.stderr
    assert outputs["status"] == "pass"
    assert outputs["records"] == ""
    assert outputs["records-index"] == ""
    assert outputs["records-count"] == "0"
    assert outputs["records-total"] == "0"
    assert "0 non-passing units" in step_summary
    assert "### Failure Records" not in step_summary
    assert "::warning::" not in proc.stdout


def test_records_disabled_returns_prior_behavior(tmp_path):
    """render-records: false is the opt-out: no record attempt, empty outputs,
    and the prior gate behavior is unchanged."""
    ws = _workspace(tmp_path)
    proc, outputs, step_summary = _run_gate(tmp_path, ws, {
        "suite": "qa/suite/consumer.suite.json",
        "agent": "consumer-agent",
        "render-records": "false",
    })
    assert proc.returncode == 1, proc.stderr
    assert outputs["status"] == "fail"
    assert outputs["records"] == ""
    assert outputs["records-index"] == ""
    assert outputs["records-count"] == ""
    assert outputs["records-total"] == ""
    assert not (ws / ".hotato" / "results" / "records").exists()
    assert "### Failure Records" not in step_summary


def test_records_truncation_is_explicit_in_outputs_and_summary(tmp_path):
    """record-limit caps the set: the first N in source order render, the
    total is still reported, and the truncation is stated in the summary."""
    ws = _workspace(tmp_path)
    proc, outputs, step_summary = _run_gate(tmp_path, ws, {
        "suite": "qa/suite/two-fail.suite.json",
        "agent": "consumer-agent",
        "render-records": "true",
        "record-limit": "1",
    })
    assert proc.returncode == 1, proc.stderr
    assert outputs["records-count"] == "1"
    assert outputs["records-total"] == "2"
    index = _load_index(ws, outputs["records-index"])
    assert index["rendered"] == 1 and index["total_failures"] == 2
    assert index["truncated"] is True
    # only the first non-passing unit, in source order
    assert [e["test_id"] for e in index["records"]] == \
        ["escalate-not-handed-off-test"]
    assert "Rendered 1 of 2 non-passing units (record-limit=1)." in step_summary


def test_records_bad_limit_is_refused(tmp_path):
    """record-limit outside 1..500 is refused before any run (exit 2)."""
    ws = _workspace(tmp_path)
    proc, outputs, _ = _run_gate(tmp_path, ws, {
        "suite": "qa/suite/consumer.suite.json",
        "agent": "consumer-agent",
        "render-records": "true",
        "record-limit": "0",
    })
    assert proc.returncode == 2
    assert outputs["status"] == "error"


def test_renderer_failure_warns_without_changing_exit(tmp_path):
    """A failing evaluation whose record set is refused (duplicate unit id):
    the gate STILL exits 1, a ::warning:: is emitted, and no records are
    reported present."""
    ws = _workspace(tmp_path)
    proc, outputs, step_summary = _run_gate(tmp_path, ws, {
        "suite": "qa/suite/dup-ids.suite.json",
        "agent": "consumer-agent",
        "render-records": "true",
    })
    # the evaluation exit is preserved exactly -- the renderer never touches it
    assert proc.returncode == 1, proc.stderr
    assert outputs["exit-code"] == "1"
    assert outputs["status"] == "fail"
    assert "::warning::" in proc.stdout
    assert "Failure Record rendering failed" in proc.stdout
    # a renderer error never reports records as present
    assert outputs["records"] == ""
    assert outputs["records-index"] == ""
    assert outputs["records-count"] == ""
    assert outputs["records-total"] == ""


def test_failed_and_refused_exits_are_preserved_with_records_on(tmp_path):
    """Records-on never changes the gate contract: a deterministic failure
    stays exit 1 and a refused/usage error stays exit 2."""
    ws = _workspace(tmp_path)
    proc, outputs, _ = _run_gate(tmp_path, ws, {
        "suite": "qa/suite/consumer.suite.json",
        "agent": "consumer-agent",
        "render-records": "true",
    })
    assert proc.returncode == 1 and outputs["exit-code"] == "1"

    ws2 = _workspace(tmp_path / "b")
    proc2, outputs2, _ = _run_gate(tmp_path / "b", ws2, {
        "suite": "qa/suite/consumer.suite.json",
        "agent": "bad agent!",  # unsafe id -> refused
        "render-records": "true",
    })
    assert proc2.returncode == 2 and outputs2["exit-code"] == "2"
    assert outputs2["status"] == "error"


def test_records_paths_contained_within_output_spaces_and_symlinks(tmp_path):
    """The digest-scoped record paths stay strictly beneath the configured
    output, even with spaces in the path and an unrelated symlink in the tree."""
    ws = _workspace(tmp_path)
    external = tmp_path / "external"
    external.mkdir()
    # an unrelated symlink in the workspace must not let records resolve out
    (ws / "qa" / "outside-link").symlink_to(external, target_is_directory=True)
    proc, outputs, _ = _run_gate(tmp_path, ws, {
        "suite": "qa/suite/two-fail.suite.json",
        "agent": "consumer-agent",
        "output": "out dir/results",
        "render-records": "true",
    })
    assert proc.returncode == 1, proc.stderr
    assert "::warning::" not in proc.stdout  # containment held; nothing refused
    assert outputs["records-count"] == "2"
    records_dir = outputs["records"]
    assert records_dir.startswith("out dir/results/records/sha256-")
    out_root = os.path.realpath(str(ws / "out dir" / "results"))
    index = _load_index(ws, outputs["records-index"])
    for entry in index["records"]:
        for leaf in ("failure-record.json", "failure-record.md",
                     "failure-record.html", "failure-record.svg"):
            real = os.path.realpath(
                str(ws / records_dir / entry["directory"] / leaf))
            assert real == os.path.join(str(ws), records_dir,
                                        entry["directory"], leaf) \
                or real.startswith(out_root + os.sep)
            assert real.startswith(out_root + os.sep), real


def test_unsupported_older_hotato_is_a_note_not_a_warning():
    """An exact pinned older hotato without `record render --all` is detected
    from argparse's refusal text and reported as a graceful compat note, never
    as a renderer-error warning; a genuine error is NOT misread as unsupported."""
    gate = _load_gate()
    assert gate._classify_unsupported(
        "usage: hotato ...\nhotato: error: argument command: invalid choice:"
        " 'record' (choose from 'suite', 'test')")
    assert gate._classify_unsupported(
        "hotato record render: error: unrecognized arguments: --all --limit 100")
    # a real renderer error is not the unsupported case
    assert not gate._classify_unsupported(
        "error: cannot render a record set: the source contains duplicate unit"
        " id(s) dup")
    assert not gate._classify_unsupported("")


def test_index_validation_rejects_a_digest_mismatch():
    """The gate never trusts the index blindly: a source-digest mismatch, a
    wrong kind, or a rendered/records-length disagreement is refused."""
    gate = _load_gate()
    good = {
        "kind": "hotato.failure-record-index.v1", "version": "1.0",
        "source": {"kind": "hotato.suite-run", "digest": "sha256:" + "a" * 64},
        "total_failures": 1, "rendered": 1, "truncated": False,
        "records": [{"record_id": "sha256:" + "b" * 64, "status": "FAIL",
                     "test_id": "t", "headline": "h",
                     "directory": "sha256-" + "b" * 64}],
    }
    assert gate._validate_index(good, "a" * 64) is None
    assert gate._validate_index(good, "c" * 64) is not None  # digest mismatch
    bad_kind = dict(good, kind="something.else")
    assert gate._validate_index(bad_kind, "a" * 64) is not None
    bad_count = dict(good, rendered=2)
    assert gate._validate_index(bad_count, "a" * 64) is not None


def test_action_performs_no_upload_comment_or_network(tmp_path):
    """The Action source contains no upload/comment/notification API and no
    HTTP client: zero-egress, read-only, presentation-only."""
    banned = (
        "api.github.com", "uploads.github.com", "github.com/repos",
        "/issues/", "create_comment", "createComment", "issue_comment",
        "requests.post", "requests.get", "urlopen", "http.client",
        "smtplib", "webhook",
    )
    for name in ("gate.py", "summary.py"):
        with open(os.path.join(ACTION_DIR, name), "r", encoding="utf-8") as fh:
            src = fh.read()
        for token in banned:
            assert token not in src, f"{name} references {token!r}"

    yaml = pytest.importorskip("yaml")
    with open(os.path.join(ROOT, "action.yml"), "r", encoding="utf-8") as fh:
        doc = yaml.safe_load(fh)
    # a composite Action must not escalate permissions
    assert "permissions" not in doc
    # the only steps are setup-python and the local gate script -- no upload,
    # comment, or github-script step is present
    for step in doc["runs"]["steps"]:
        uses = step.get("uses", "")
        assert "upload-artifact" not in uses
        assert "github-script" not in uses
        assert "comment" not in uses.lower()
        run = step.get("run", "")
        assert "curl" not in run and "gh api" not in run


# ---------------------------------------------------------------------------
# contracts mode: hotato's own tight verify Markdown on the job summary
# (gap #8, PR result reporting; `hotato contract verify --step-summary`)
# ---------------------------------------------------------------------------

def _create_contract_bundle(ws, cid, *extra):
    """Build one real contract bundle inside the workspace via the CLI (the
    engine scores it at create and re-scores it inside the gate run)."""
    from importlib import resources
    hard = str(resources.files("hotato").joinpath(
        "data", "audio", "01-hard-interruption.example.wav"))
    env = {k: v for k, v in os.environ.items()
           if not k.startswith(("HOTATO_ACTION_", "GITHUB_"))}
    env["PYTHONPATH"] = os.path.join(ROOT, "src") + os.pathsep + env.get(
        "PYTHONPATH", "")
    out = ws / "qa" / "contracts"
    out.mkdir(parents=True, exist_ok=True)
    proc = subprocess.run(
        [sys.executable, "-m", "hotato", "contract", "create",
         "--stereo", hard, "--id", cid, "--onset", "2.40",
         "--expect", "yield", "--out", str(out), *extra],
        env=env, capture_output=True, text=True, timeout=300)
    assert proc.returncode == 0, proc.stderr
    return "qa/contracts"


def test_contracts_argv_step_summary_only_when_supported():
    """The verify argv carries --step-summary ONLY when the installed hotato
    was probed to support it: an exact older pin keeps its unchanged argv, so
    the Action's exit-code contract is untouched by the summary feature."""
    gate = _load_gate()
    cfg = {"mode": "contracts", "target": "qa/contracts",
           "output": ".hotato/results"}
    assert "--step-summary" not in gate.build_argv(cfg)
    cfg["step_summary"] = ".hotato/results/contract-summary.md"
    argv = gate.build_argv(cfg)
    i = argv.index("--step-summary")
    assert argv[i + 1] == ".hotato/results/contract-summary.md"


def test_step_summary_probe_detects_current_cli(monkeypatch):
    gate = _load_gate()
    src = os.path.join(ROOT, "src")
    monkeypatch.setenv("PYTHONPATH",
                       src + os.pathsep + os.environ.get("PYTHONPATH", ""))
    assert gate._step_summary_supported() is True


def test_append_contract_step_summary_fails_open_on_missing_leaf(
        tmp_path, monkeypatch):
    """A missing leaf (an exact older pin never wrote one) appends nothing,
    raises nothing, and reports nothing as present."""
    gate = _load_gate()
    gh = tmp_path / "s.md"
    monkeypatch.setenv("GITHUB_STEP_SUMMARY", str(gh))
    meta = {}
    gate.append_contract_step_summary(
        {"workspace": str(tmp_path),
         "step_summary": "missing/contract-summary.md"}, meta)
    assert not gh.exists()
    assert "contract_summary_path" not in meta


def test_gate_contracts_step_summary_appends_tight_markdown(tmp_path):
    ws = _workspace(tmp_path)
    contracts = _create_contract_bundle(ws, "gate-ss-001")
    proc, outputs, step_summary = _run_gate(tmp_path, ws,
                                            {"contracts": contracts})
    assert proc.returncode == 0, proc.stderr
    assert outputs["exit-code"] == "0"
    assert outputs["status"] == "pass"
    # hotato's own tight verify Markdown lands ahead of the five-lane summary
    assert "### hotato contract verify" in step_summary
    assert "**PASS**: 1/1 contracts pass (exit code 0)" in step_summary
    assert step_summary.index("hotato contract verify") < step_summary.index(
        "VOICE CONVERSATION REGRESSION")
    # the leaf itself sits inside the private output directory
    assert (ws / ".hotato" / "results" / "contract-summary.md").is_file()
    # the reproduce line is the exact executed argv, flag included
    assert "--step-summary" in step_summary


def test_gate_contracts_step_summary_fail_preserves_exit(tmp_path):
    ws = _workspace(tmp_path)
    contracts = _create_contract_bundle(ws, "gate-ss-bad-001",
                                        "--max-time-to-yield", "0.0")
    proc, outputs, step_summary = _run_gate(tmp_path, ws,
                                            {"contracts": contracts})
    # the summary is additive: hotato's exit code flows through untouched
    assert proc.returncode == 1, proc.stderr
    assert outputs["exit-code"] == "1"
    assert "**FAIL**: 0/1 contracts pass, 1 failing (exit code 1)" in step_summary
    assert "`gate-ss-bad-001`" in step_summary
    assert "seconds_to_yield" in step_summary


# ---------------------------------------------------------------------------
# P0.2: the Action must never follow a planted result / summary symlink
# (a consumer PR commits an output leaf as a symlink so the pinned Action
# truncates an accessible target and still exits 0)
# ---------------------------------------------------------------------------

def _load_gate():
    spec = importlib.util.spec_from_file_location(
        "action_gate", os.path.join(ACTION_DIR, "gate.py")
    )
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


# every fixed output path the Action publishes below the output directory
_OUTPUT_LEAVES = [
    "suite-run.json", "test-run.json", "contract-verify.json",
    "summary.md", "contracts-junit.xml", "artifact", "records",
]


@pytest.mark.skipif(
    sys.platform == "win32",
    reason="planting the result symlink this refusal is exercised against needs "
           "the SeCreateSymbolicLink privilege on Windows (absent by default); "
           "the no-follow refusal is POSIX-exercised here",
)
@pytest.mark.parametrize("leaf", _OUTPUT_LEAVES)
def test_gate_refuses_planted_output_symlink(tmp_path, leaf):
    """Plant a symlink at every fixed output path (suite-run.json, test-run.json,
    contract-verify.json, summary.md, JUnit, artifact, records) and drive the
    Action end to end through the consumer fixture. The Action must refuse the
    run (exit 2, status error), never truncate the link target, and never exit 0
    through the link. On the vulnerable code the suite result and summary leaves
    were truncated and the run exited 0."""
    ws = _workspace(tmp_path)
    victim = ws / "KEEP_ME.txt"
    victim.write_bytes(b"KEEP_ME\n")
    planted = ws / ".hotato" / "results"
    planted.mkdir(parents=True)
    (planted / leaf).symlink_to(victim)

    proc, outputs, _ = _run_gate(tmp_path, ws, {
        "suite": "qa/suite/pass.suite.json",
        "agent": "consumer-agent",
    })

    # never a green exit through a planted link
    assert proc.returncode == 2, (proc.stdout, proc.stderr)
    assert outputs.get("status") == "error"
    assert outputs.get("exit-code") == "2"
    # the link target is byte-for-byte intact (never opened for writing)
    assert victim.read_bytes() == b"KEEP_ME\n", (
        f"the Action truncated the victim through the planted {leaf!r} symlink"
    )
    # the planted symlink was not replaced by a fresh regular file either
    assert (planted / leaf).is_symlink()


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX symlink test")
def test_gate_test_mode_result_symlink_not_followed(tmp_path):
    """The test-mode result leaf (test-run.json) is the file run_hotato writes
    directly in a test run; prove it is protected in its own matching mode."""
    ws = _workspace(tmp_path)
    victim = ws / "victim.txt"
    victim.write_bytes(b"do-not-clobber\n")
    results = ws / ".hotato" / "results"
    results.mkdir(parents=True)
    (results / "test-run.json").symlink_to(victim)

    proc, _outputs, _ = _run_gate(tmp_path, ws, {
        "test": "qa/test/two-lane.conversation-test.yaml",
        "agent": "consumer-agent",
        "transcript": "qa/test/refund.transcript.json",
    })
    assert proc.returncode == 2, (proc.stdout, proc.stderr)
    assert victim.read_bytes() == b"do-not-clobber\n"


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX symlink test")
def test_gate_open_new_refuses_symlink_leaf(tmp_path):
    """Unit-level proof of the leaf primitive: _open_new opens with
    O_CREAT|O_EXCL|O_NOFOLLOW|O_WRONLY, so it refuses a pre-existing symlink
    (never truncating its target) yet creates a fresh, private (no group/other
    access) file for a new name."""
    gate = _load_gate()
    outdir = tmp_path / "out"
    outdir.mkdir()
    victim = tmp_path / "victim.txt"
    victim.write_bytes(b"intact\n")
    (outdir / "suite-run.json").symlink_to(victim)

    dir_fd = os.open(str(outdir), os.O_RDONLY)
    try:
        with pytest.raises(gate.InputError):
            gate._open_new("suite-run.json", dir_fd=dir_fd)
        assert victim.read_bytes() == b"intact\n"
        with gate._open_new("summary.md", dir_fd=dir_fd) as fh:
            fh.write("ok")
    finally:
        os.close(dir_fd)
    assert (outdir / "summary.md").read_text() == "ok"
    assert (os.stat(outdir / "summary.md").st_mode & 0o077) == 0


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX symlink test")
def test_gate_refuses_preexisting_output_dir(tmp_path):
    """The output directory must be created privately by the Action; a committed
    output tree (which could hide planted leaves) is refused outright."""
    ws = _workspace(tmp_path)
    (ws / ".hotato" / "results").mkdir(parents=True)
    proc, outputs, _ = _run_gate(tmp_path, ws, {
        "suite": "qa/suite/pass.suite.json",
        "agent": "consumer-agent",
    })
    assert proc.returncode == 2, (proc.stdout, proc.stderr)
    assert outputs.get("status") == "error"
