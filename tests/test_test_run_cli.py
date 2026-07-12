"""``hotato test run``: the Phase-1 EXIT -- one conversation-test file, end to
end, over a real bundled call.

This is the tie-together slice (Phase-1 design H + the DoD demonstration). It
pins, on the CLI (not the Python API):

  * ONE bundled dual-channel call + its trace + transcript, evaluated for
    outcome / policy / timing / transcript-facts / tool-behaviour from ONE
    conversation-test file, producing a conversation artifact (digest-verify
    passes) + a per-dimension scorecard -- the single documented workflow;
  * the exit code honors inconclusive_policy (report / fail / refuse) end to
    end, and a success.required failure is non-zero;
  * a deterministic FAIL is non-zero; missing transcript/trace/state leaves the
    depending check INCONCLUSIVE, never guessed;
  * the rubric lane is quarantined (INCONCLUSIVE), never folded into the
    deterministic summary;
  * there is NO overall_score / blended number in ANY output, including json;
  * repetitions > 1 reports the per-run results + a plain run count, never a
    fabricated reliability number.
"""

import json
from importlib import resources

import pytest

from hotato import cli
from hotato import conversation as CV


# --- fixtures (the real bundled call + its committed trace/transcript) -------

def _bundled_wav() -> str:
    return str(resources.files("hotato").joinpath(
        "data", "audio", "01-hard-interruption.example.wav"))


def _data(name: str) -> str:
    # The committed conversation demo fixtures live next to the tests.
    import os
    return os.path.join(os.path.dirname(__file__), "data", "conversation", name)


def _demo_test_file() -> str:
    return _data("refund.conversation-test.yaml")


def _demo_trace() -> str:
    return _data("refund.voice_trace.jsonl")


def _demo_transcript() -> str:
    return _data("refund.transcript.json")


def _write_test(tmp_path, *, name="t1", agent="a", deterministic=None,
                rubric=None, success=None, policy=None, repetitions=None,
                simulator=None):
    """A conversation-test file written as JSON (the loader's JSON fast path),
    so a test never hand-indents YAML."""
    doc = {
        "kind": "hotato.conversation-test", "version": 1, "id": name,
        "agent": agent,
        "assertions": {"deterministic": deterministic or []},
    }
    if rubric is not None:
        doc["assertions"]["rubric"] = rubric
    if success is not None:
        doc["success"] = success
    if policy is not None:
        doc["inconclusive_policy"] = policy
    if repetitions is not None:
        doc["repetitions"] = repetitions
    if simulator is not None:
        doc["simulator"] = simulator
    p = tmp_path / f"{name}.json"
    p.write_text(json.dumps(doc), encoding="utf-8")
    return str(p)


def _run(argv):
    return cli.main(argv)


# --- the DoD demonstration: one real call, one file, end to end -------------

def test_bundled_call_one_file_end_to_end_scorecard_and_artifact(tmp_path, capsys):
    """ONE real bundled call (a dual-channel wav + its trace) evaluated for
    outcome/policy/timing/transcript-facts/tool-behaviour from ONE
    conversation-test file, producing a conversation artifact that digest-
    verifies + a per-dimension scorecard, via `hotato test run`."""
    out = tmp_path / "conv-artifact"
    code = _run([
        "test", "run", _demo_test_file(), "--agent", "support-v3",
        "--audio", _bundled_wav(),
        "--trace", _demo_trace(),
        "--transcript", _demo_transcript(),
        "--out", str(out), "--format", "html",
        "--created-at", "2026-07-12T00:00:00Z",
    ])
    # every deterministic check passed and the success conditions held
    assert code == 0

    # the conversation artifact exists and digest-verifies (nothing tampered)
    manifest = out / "conversation.json"
    assert manifest.is_file()
    verdict = CV.verify(str(out))
    assert verdict["ok"] is True and verdict["refused"] is False
    # the evidence is bound by digest: audio + trace + transcript + timing + assertions
    assert set(verdict["verified"]) == {
        "audio", "trace", "transcript", "timing", "assertions"}

    # the unified report carries the per-dimension scorecard across all five
    html = (out / "report.html").read_text(encoding="utf-8")
    assert '<div class="scorecard">' in html
    for dim in ("Outcome", "Policy", "Conversation", "Speech", "Reliability"):
        assert f'<span class="scname">{dim}</span>' in html
    # honesty: no blended score field anywhere on the page
    assert '"overall_score"' not in html

    # the stdout summary shows the per-dimension view + the separate tallies
    out_text = capsys.readouterr().out
    assert "per-dimension (grouped view; never blended)" in out_text
    assert "success: PASS" in out_text
    assert "deterministic: 4 pass" in out_text
    assert "judge: 0 pass, 0 fail" in out_text


def test_markdown_report_also_renders_scorecard(tmp_path):
    out = tmp_path / "ca"
    code = _run([
        "test", "run", _demo_test_file(), "--agent", "support-v3",
        "--audio", _bundled_wav(), "--trace", _demo_trace(),
        "--transcript", _demo_transcript(), "--out", str(out), "--format", "md",
    ])
    assert code == 0
    md = (out / "report.md").read_text(encoding="utf-8")
    for dim in ("#### Outcome", "#### Policy", "#### Conversation",
                "#### Speech", "#### Reliability"):
        assert dim in md
    assert '"overall_score"' not in md


# --- no overall_score in any output, including json -------------------------

def test_no_overall_score_in_json_output(tmp_path, capsys):
    code = _run([
        "test", "run", _demo_test_file(), "--agent", "support-v3",
        "--trace", _demo_trace(), "--transcript", _demo_transcript(),
        "--format", "json",
    ])
    raw = capsys.readouterr().out
    assert "overall_score" not in raw
    result = json.loads(raw)
    assert result["kind"] == "hotato.test-run"
    assert "overall_score" not in result
    assert "overall_score" not in result["assertions"]["summary"]
    # the deterministic and judge tallies stay separate (never merged)
    assert result["assertions"]["summary"]["judge"] == {"pass": 0, "fail": 0}
    assert code == 0


# --- missing input -> INCONCLUSIVE (never a guess) --------------------------

def test_missing_transcript_is_inconclusive_not_a_guess(tmp_path, capsys):
    tf = _write_test(
        tmp_path, name="needs-transcript",
        deterministic=[{"id": "said-hi", "kind": "phrase",
                        "regex": "hello", "role": "agent", "dimension": "policy"}],
        success={"required": []},
    )
    # no --transcript supplied
    _run(["test", "run", tf, "--agent", "a", "--format", "json"])
    result = json.loads(capsys.readouterr().out)
    r = result["assertions"]["results"][0]
    assert r["status"] == "INCONCLUSIVE"
    assert result["dimensions"]["policy"]["inconclusive"] == 1


def test_missing_state_leaves_state_check_inconclusive(tmp_path, capsys):
    tf = _write_test(
        tmp_path, name="needs-state",
        deterministic=[{"id": "refund-recorded", "kind": "state",
                        "resource": "refunds", "expect": {"status": "issued"},
                        "dimension": "outcome"}],
        success={"required": []},
    )
    _run(["test", "run", tf, "--agent", "a", "--format", "json"])
    result = json.loads(capsys.readouterr().out)
    assert result["assertions"]["results"][0]["status"] == "INCONCLUSIVE"


# --- exit code honors inconclusive_policy end to end ------------------------

@pytest.mark.parametrize("policy,expected", [
    ("report", 0),   # INCONCLUSIVE never gates
    ("fail", 1),     # INCONCLUSIVE gates like a FAIL
    ("refuse", 2),   # INCONCLUSIVE withholds the verdict
])
def test_inconclusive_policy_gates_exit_code(tmp_path, policy, expected):
    # a phrase check with no transcript -> INCONCLUSIVE; success.required holds
    # (no_rubric_failure is vacuously true) so the exit code is driven purely by
    # the policy, exactly like envelope_from_results.
    tf = _write_test(
        tmp_path, name=f"pol-{policy}", policy=policy,
        deterministic=[{"id": "said-hi", "kind": "phrase", "regex": "hello",
                        "dimension": "policy"}],
        success={"required": ["no_rubric_failure"]},
    )
    code = _run(["test", "run", tf, "--agent", "a", "--format", "json"])
    assert code == expected


# --- a success.required failure -> non-zero (even when the policy would be 0) --

def test_success_required_failure_is_nonzero_under_report(tmp_path, capsys):
    # policy=report -> the envelope alone would exit 0 on an INCONCLUSIVE; but
    # success.required demands no_inconclusive, which fails -> non-zero.
    tf = _write_test(
        tmp_path, name="needs-no-inconclusive", policy="report",
        deterministic=[{"id": "said-hi", "kind": "phrase", "regex": "hello",
                        "dimension": "policy"}],
        success={"required": ["no_inconclusive"]},
    )
    code = _run(["test", "run", tf, "--agent", "a", "--format", "json"])
    result = json.loads(capsys.readouterr().out)
    # the deterministic envelope itself is exit 0 (report policy), but the run is
    # non-zero because success failed.
    assert result["assertions"]["exit_code"] == 0
    assert result["success"]["passed"] is False
    assert code == 1


def test_deterministic_fail_is_nonzero(tmp_path):
    # a tool that never appears in the trace -> FAIL
    tf = _write_test(
        tmp_path, name="wants-missing-tool",
        deterministic=[{"id": "escalated", "kind": "tool_call",
                        "name": "escalate_to_human", "dimension": "outcome"}],
        success={"required": ["all_deterministic_assertions_pass"]},
    )
    code = _run(["test", "run", tf, "--agent", "a", "--trace", _demo_trace()])
    assert code == 1


# --- the rubric lane is quarantined, never folded into deterministic --------

def test_rubric_lane_is_quarantined_inconclusive(tmp_path, capsys):
    tf = _write_test(
        tmp_path, name="has-rubric",
        deterministic=[{"id": "refunded", "kind": "tool_call",
                        "name": "issue_refund", "dimension": "outcome"}],
        rubric=[{"id": "was-empathetic", "kind": "judge_rubric",
                 "dimension": "conversation"}],
        success={"required": ["all_deterministic_assertions_pass",
                              "no_rubric_failure"]},
    )
    code = _run(["test", "run", tf, "--agent", "a", "--trace", _demo_trace(),
                 "--format", "json"])
    result = json.loads(capsys.readouterr().out)
    assert result["rubric"]["quarantined"] is True
    assert result["rubric"]["results"][0]["status"] == "INCONCLUSIVE"
    assert result["rubric"]["results"][0]["deterministic"] is False
    # the rubric INCONCLUSIVE is NOT counted in the deterministic summary
    det = result["assertions"]["summary"]["deterministic"]
    assert det == {"pass": 1, "fail": 0, "inconclusive": 0}
    assert result["assertions"]["summary"]["judge"] == {"pass": 0, "fail": 0}
    assert code == 0


# --- repetitions > 1: per-run + a plain count, never a fabricated number -----

def test_repetitions_report_per_run_without_reliability_number(tmp_path, capsys):
    tf = _write_test(
        tmp_path, name="reps",
        deterministic=[{"id": "refunded", "kind": "tool_call",
                        "name": "issue_refund", "dimension": "outcome"}],
    )
    code = _run(["test", "run", tf, "--agent", "a", "--trace", _demo_trace(),
                 "--repetitions", "3", "--format", "json"])
    result = json.loads(capsys.readouterr().out)
    assert result["repetitions"]["runs"] == 3
    assert len(result["repetitions"]["per_run"]) == 3
    assert [r["run"] for r in result["repetitions"]["per_run"]] == [1, 2, 3]
    # a plain count + an explicit Phase-2 deferral, never a fabricated pass^k
    assert result["reliability"] == {
        "runs": 3,
        "note": "reliability: 3 runs; pass^k in Phase 2 (not computed)",
    }
    # no fabricated reliability number leaks into the results/summary
    assert "pass^k" not in json.dumps(result["assertions"])
    assert code == 0


# --- origin: real by default, simulated only with a simulator block ----------

def test_origin_is_real_by_default(tmp_path):
    tf = _write_test(
        tmp_path, name="real-origin",
        deterministic=[{"id": "refunded", "kind": "tool_call",
                        "name": "issue_refund"}],
    )
    out = tmp_path / "ca"
    _run(["test", "run", tf, "--agent", "a", "--trace", _demo_trace(),
          "--out", str(out)])
    manifest = json.loads((out / "conversation.json").read_text(encoding="utf-8"))
    assert manifest["origin"]["kind"] == "real"


def test_origin_is_simulated_with_a_simulator_block(tmp_path):
    tf = _write_test(
        tmp_path, name="sim-origin",
        deterministic=[{"id": "refunded", "kind": "tool_call",
                        "name": "issue_refund"}],
        simulator={"model_id": "gpt-sim-x", "scenario_id": "refund-1", "seed": 7},
    )
    out = tmp_path / "ca"
    _run(["test", "run", tf, "--agent", "a", "--trace", _demo_trace(),
          "--out", str(out)])
    manifest = json.loads((out / "conversation.json").read_text(encoding="utf-8"))
    assert manifest["origin"]["kind"] == "simulated"
    assert manifest["origin"]["simulator"]["model_id"] == "gpt-sim-x"


# --- html/md need --out and --audio; missing -> clean usage error -----------

def test_html_without_out_is_usage_error(tmp_path):
    tf = _write_test(tmp_path, name="x",
                     deterministic=[{"id": "r", "kind": "tool_call",
                                     "name": "issue_refund"}])
    code = _run(["test", "run", tf, "--agent", "a", "--audio", _bundled_wav(),
                 "--format", "html"])
    assert code == 2


def test_html_without_audio_is_usage_error(tmp_path):
    tf = _write_test(tmp_path, name="x",
                     deterministic=[{"id": "r", "kind": "tool_call",
                                     "name": "issue_refund"}])
    out = tmp_path / "ca"
    code = _run(["test", "run", tf, "--agent", "a", "--trace", _demo_trace(),
                 "--out", str(out), "--format", "html"])
    assert code == 2


def test_malformed_conversation_test_is_exit_2(tmp_path):
    bad = tmp_path / "bad.json"
    bad.write_text(json.dumps({"kind": "hotato.conversation-test", "version": 1,
                               "id": "x"}), encoding="utf-8")  # missing agent
    code = _run(["test", "run", str(bad), "--agent", "a"])
    assert code == 2
