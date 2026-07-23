"""``--junit`` on ``hotato suite run`` and ``hotato prove``: the JUnit XML
projection a standard CI dashboard ingests, mirroring each command's existing
result grouping (one ``<testsuite>`` per dimension for a suite run, one per
evidence lane for a proof).

Pinned here:

  * suite run: a passing dimension renders a plain ``<testcase>``; a scored
    FAIL renders a ``<failure>`` carrying the measured reason; an
    INCONCLUSIVE dimension renders an ``<error>`` -- never a ``<failure>``
    and never a silent pass, so CI sees red exactly when the exit code gates;
  * prove: a passing lane renders plain; a failing contracts lane renders a
    ``<failure>``; a refused lane renders an ``<error>`` preserving the
    lane's refusal reason, with the non-zero exit intact;
  * both: the file parses as XML, the root ``<testsuites>`` counts match the
    child elements, and a repeat run writes byte-identical XML (no
    timestamp, no wall clock).
"""

from __future__ import annotations

import json
import os
import xml.etree.ElementTree as ET
from importlib import resources

import pytest

from hotato import cli

HARD = str(resources.files("hotato").joinpath(
    "data", "audio", "01-hard-interruption.example.wav"))          # yields at 2.40


@pytest.fixture(autouse=True)
def _isolated_home(tmp_path, monkeypatch):
    # The demo-contract fixture path (`start --demo`) reads HOTATO_HOME; keep
    # every run hermetic, exactly as tests/test_prove.py does.
    monkeypatch.setenv("HOTATO_HOME", str(tmp_path / "home"))


# =========================================================================
# tiny inline suite fixtures (the tests/test_suite_run.py pattern)
# =========================================================================

def _pass_scenario():
    return {
        "kind": "hotato.scenario", "version": 1, "id": "refund-ok",
        "goal": {"type": "get_refund", "target": "order A-1"},
        "facts": {"order_id": "A-1"},
        "caller": {"script": [{"say": "order A-1 arrived damaged"},
                              {"say": "i want a refund"}],
                   "behavior": {"backchannels": {"probability": 0.0}}},
        "variation_matrix": {"speaking_rate": [1.0], "noise": ["clean"],
                             "repetitions": 1},
        "agent_mock": {
            "tools": [{"name": "lookup_order", "arguments": {"order_id": "A-1"},
                       "result": {"found": True}, "latency_ms": 300},
                      {"name": "issue_refund", "arguments": {"order_id": "A-1"},
                       "result": {"status": "refunded"}, "latency_ms": 500}],
            "state": {"orders": [{"order_id": "A-1", "refund_status": "refunded"}]},
        },
    }


def _defect_scenario():
    # The agent claims a refund but never calls issue_refund (outcome DEFECT).
    return {
        "kind": "hotato.scenario", "version": 1, "id": "refund-broken",
        "goal": {"type": "get_refund", "target": "order A-2"},
        "facts": {"order_id": "A-2"},
        "caller": {"script": [{"say": "order A-2 never arrived"},
                              {"say": "please refund order A-2"}],
                   "behavior": {"backchannels": {"probability": 0.0}}},
        "variation_matrix": {"speaking_rate": [1.0], "noise": ["clean"],
                             "repetitions": 1},
        "agent_mock": {
            "tools": [{"name": "lookup_order", "arguments": {"order_id": "A-2"},
                       "result": {"found": True}, "latency_ms": 300}],
            "state": {"orders": [{"order_id": "A-2", "refund_status": "none"}]},
        },
    }


def _test_for(scn_id, expect_pass):
    det = [
        {"id": "asked-refund", "kind": "phrase", "regex": "refund",
         "role": "caller", "dimension": "conversation"},
        {"id": "refund-tool", "kind": "tool_result", "name": "issue_refund",
         "result_subset": {"status": "refunded"}, "dimension": "outcome"},
    ]
    return {
        "kind": "hotato.conversation-test", "version": 1,
        "id": f"{scn_id}-test", "agent": "agent-under-test",
        "scenario": f"{scn_id}.scenario.json",
        "assertions": {"deterministic": det},
        "success": {"required": ["all_deterministic_assertions_pass"]},
    }


def _write(path, obj):
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(obj, fh)


def _build_suite(tmp_path, include_defect=True):
    _write(os.path.join(tmp_path, "refund-ok.scenario.json"), _pass_scenario())
    _write(os.path.join(tmp_path, "refund-ok.test.json"),
           _test_for("refund-ok", True))
    tests = ["refund-ok.test.json"]
    if include_defect:
        _write(os.path.join(tmp_path, "refund-broken.scenario.json"),
               _defect_scenario())
        _write(os.path.join(tmp_path, "refund-broken.test.json"),
               _test_for("refund-broken", False))
        tests.append("refund-broken.test.json")
    suite = {"kind": "hotato.suite", "version": 1, "suite_id": "smoke",
             "name": "smoke", "required_for_release": True,
             "inconclusive_policy": "fail", "tests": tests}
    p = os.path.join(tmp_path, "smoke.suite.json")
    _write(p, suite)
    return p


def _build_inconclusive_suite(tmp_path):
    # A test with NO scenario + an assertion needing a trace -> INCONCLUSIVE
    # (absent required input, never a guess), gating under policy fail.
    static = {
        "kind": "hotato.conversation-test", "version": 1, "id": "needs-trace",
        "agent": "agent-under-test",
        "assertions": {"deterministic": [
            {"id": "must-call", "kind": "tool_result", "name": "do_thing",
             "result_subset": {"ok": True}, "dimension": "outcome"}]},
        "success": {"required": ["all_deterministic_assertions_pass"]},
    }
    _write(os.path.join(tmp_path, "needs-trace.test.json"), static)
    suite = {"kind": "hotato.suite", "version": 1, "suite_id": "ci",
             "name": "ci", "required_for_release": True,
             "inconclusive_policy": "fail", "tests": ["needs-trace.test.json"]}
    p = os.path.join(tmp_path, "ci.suite.json")
    _write(p, suite)
    return p


def _suite_by_name(root, name):
    for ts in root.findall("testsuite"):
        if ts.get("name") == name:
            return ts
    raise AssertionError(f"no <testsuite name={name!r}> in the report")


def _case_by_name(ts, name):
    for tc in ts.findall("testcase"):
        if tc.get("name") == name:
            return tc
    raise AssertionError(f"no <testcase name={name!r}> in the testsuite")


# =========================================================================
# suite run --junit
# =========================================================================

def test_suite_run_pass_and_fail_map_to_testcase_and_failure(tmp_path):
    suite_path = _build_suite(tmp_path)
    junit = tmp_path / "suite.xml"
    rc = cli.main(["suite", "run", suite_path, "--agent", "agent-under-test",
                   "--no-registry", "--junit", str(junit)])
    assert rc == 1
    root = ET.parse(junit).getroot()
    assert root.tag == "testsuites"

    outcome = _suite_by_name(root, "outcome")
    ok = _case_by_name(outcome, "refund-ok-test")
    assert ok.find("failure") is None and ok.find("error") is None
    broken = _case_by_name(outcome, "refund-broken-test")
    failure = broken.find("failure")
    assert failure is not None
    # the measured reason travels with the failure, never a bare red mark
    assert "issue_refund" in failure.get("message")
    # a scored FAIL is a <failure>, never an <error>
    assert broken.find("error") is None

    # root counts match the emitted children
    all_cases = [tc for ts in root.findall("testsuite")
                 for tc in ts.findall("testcase")]
    assert int(root.get("tests")) == len(all_cases)
    assert int(root.get("failures")) == sum(
        1 for tc in all_cases if tc.find("failure") is not None)
    assert int(root.get("errors")) == sum(
        1 for tc in all_cases if tc.find("error") is not None)
    assert int(root.get("failures")) >= 1


def test_suite_run_inconclusive_is_error_never_failure_nor_green(tmp_path):
    suite_path = _build_inconclusive_suite(tmp_path)
    junit = tmp_path / "suite.xml"
    rc = cli.main(["suite", "run", suite_path, "--agent", "agent-under-test",
                   "--no-registry", "--junit", str(junit)])
    assert rc != 0
    root = ET.parse(junit).getroot()
    outcome = _suite_by_name(root, "outcome")
    tc = _case_by_name(outcome, "needs-trace")
    error = tc.find("error")
    assert error is not None
    assert "INCONCLUSIVE" in error.get("message")
    # never a <failure>, and never green: the non-zero exit shows as red
    assert tc.find("failure") is None
    assert int(root.get("errors")) >= 1
    assert int(root.get("failures")) + int(root.get("errors")) >= 1


def test_suite_run_junit_byte_stable_on_repeat(tmp_path):
    suite_path = _build_suite(tmp_path)
    junit_a = tmp_path / "a.xml"
    junit_b = tmp_path / "b.xml"
    cli.main(["suite", "run", suite_path, "--agent", "agent-under-test",
              "--no-registry", "--junit", str(junit_a)])
    cli.main(["suite", "run", suite_path, "--agent", "agent-under-test",
              "--no-registry", "--junit", str(junit_b)])
    assert junit_a.read_bytes() == junit_b.read_bytes()


# =========================================================================
# prove --junit
# =========================================================================

def _passing_contracts_dir(tmp_path):
    """One passing contract via `contract create` (the tests/test_prove.py
    pattern): the bundled hard-interruption example yields at 2.40."""
    cdir = tmp_path / "contracts"
    cdir.mkdir()
    rc = cli.main([
        "contract", "create", "--stereo", HARD, "--id", "prove-pass-001",
        "--onset", "2.40", "--expect", "yield", "--out", str(cdir),
    ])
    assert rc == 0
    return cdir


def _failing_contracts_dir(tmp_path):
    """The demo failure contract `start --demo` creates (FAIL as expected)."""
    demo_dir = tmp_path / "demo"
    demo_dir.mkdir()
    rc = cli.main(["start", "--demo", "--dir", str(demo_dir)])
    assert rc == 0
    return demo_dir / "contracts"


def test_prove_green_contracts_lane_is_a_plain_testcase(tmp_path):
    cdir = _passing_contracts_dir(tmp_path)
    junit = tmp_path / "proof.xml"
    rc = cli.main(["prove", "--contracts", str(cdir),
                   "--out", str(tmp_path / "proofout"), "--junit", str(junit)])
    assert rc == 0
    root = ET.parse(junit).getroot()
    assert root.tag == "testsuites"
    assert int(root.get("failures")) == 0
    assert int(root.get("errors")) == 0
    lane_suite = _suite_by_name(root, "contracts")
    tc = _case_by_name(lane_suite, "contracts")
    assert tc.find("failure") is None and tc.find("error") is None


def test_prove_failing_contract_is_a_failure(tmp_path):
    cdir = _failing_contracts_dir(tmp_path)
    junit = tmp_path / "proof.xml"
    rc = cli.main(["prove", "--contracts", str(cdir),
                   "--out", str(tmp_path / "proofout"), "--junit", str(junit)])
    assert rc == 1
    root = ET.parse(junit).getroot()
    tc = _case_by_name(_suite_by_name(root, "contracts"), "contracts")
    failure = tc.find("failure")
    assert failure is not None
    assert "failed" in failure.get("message")
    assert int(root.get("failures")) == 1


def test_prove_refused_lane_is_an_error_with_the_reason_preserved(tmp_path):
    # An empty contracts directory refuses; the refusal is an <error> --
    # never a <failure>, never green -- with the lane's reason intact.
    empty = tmp_path / "contracts"
    empty.mkdir()
    junit = tmp_path / "proof.xml"
    rc = cli.main(["prove", "--contracts", str(empty),
                   "--out", str(tmp_path / "proofout"), "--junit", str(junit)])
    assert rc == 2
    root = ET.parse(junit).getroot()
    tc = _case_by_name(_suite_by_name(root, "contracts"), "contracts")
    error = tc.find("error")
    assert error is not None
    assert "no usable" in error.get("message")
    assert tc.find("failure") is None
    assert int(root.get("errors")) == 1
    assert int(root.get("failures")) == 0


def test_prove_junit_byte_stable_on_repeat(tmp_path, monkeypatch):
    monkeypatch.setenv("SOURCE_DATE_EPOCH", "1700000000")
    cdir = _passing_contracts_dir(tmp_path)
    junit_a = tmp_path / "a.xml"
    junit_b = tmp_path / "b.xml"
    assert cli.main(["prove", "--contracts", str(cdir),
                     "--out", str(tmp_path / "out-a"),
                     "--junit", str(junit_a)]) == 0
    assert cli.main(["prove", "--contracts", str(cdir),
                     "--out", str(tmp_path / "out-b"),
                     "--junit", str(junit_b)]) == 0
    assert junit_a.read_bytes() == junit_b.read_bytes()
