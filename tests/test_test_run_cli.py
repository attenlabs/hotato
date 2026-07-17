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
  * the rubric lane REALLY runs through the model-judge engine (rubric.v1,
    deterministic:false), ADVISORY by default, never folded into the
    deterministic summary (here the judge backend is unreachable, so it is an
    honest ERROR that never gates);
  * there is NO overall_score / blended number in ANY output, including json;
  * repetitions > 1 reports the per-run results + the run count + a REAL
    reliability aggregate (pass@1 / pass@k / pass^k + a Wilson CI), never a
    fabricated number and never a Phase-2 deferral.
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


@pytest.fixture(autouse=True)
def _deterministic_judge(monkeypatch, tmp_path):
    """`hotato test run` REALLY evaluates the rubric lane with a local model.
    For a deterministic, network-free CLI suite we point the default judge at an
    UNREACHABLE local endpoint so a rubric lane resolves to an honest ERROR
    (advisory) instead of hitting a live daemon. This is production-real "judge
    down" behavior -- not a stub -- and keeps these tests reproducible whether or
    not Ollama is running. (The real model path is proven by the fake-judge unit
    tests and the live-Ollama integration test.) Verdicts are also cached under
    a per-test tmp dir so runs never touch ~/.hotato."""
    monkeypatch.setenv("HOTATO_JUDGE_ENDPOINT", "http://127.0.0.1:1")
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("USERPROFILE", str(tmp_path))


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
    # the model-judged lane is reported SEPARATELY (advisory), never merged into
    # the deterministic tally; with the judge backend unreachable its one rubric
    # is an honest ERROR and never gates.
    assert "rubric (model-judged, advisory)" in out_text
    # the report's judge shelf is populated (advisory), on its own shelf
    assert ">Model-assisted (advisory)<" in html


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


# --- the rubric lane REALLY runs (rubric.v1), advisory, never folded in ------

def test_rubric_lane_runs_real_judge_advisory(tmp_path, capsys):
    tf = _write_test(
        tmp_path, name="has-rubric",
        deterministic=[{"id": "refunded", "kind": "tool_call",
                        "name": "issue_refund", "dimension": "outcome"}],
        rubric=[{"id": "was-empathetic", "kind": "judge_rubric",
                 "dimension": "conversation",
                 "criterion": "was the agent empathetic?",
                 "evidence": ["transcript"]}],
        success={"required": ["all_deterministic_assertions_pass",
                              "no_rubric_failure"]},
    )
    # the autouse fixture points the judge at an unreachable endpoint, so the
    # one rubric resolves to an honest ERROR (backend down) -- a REAL rubric.v1
    # result, deterministic:false, ADVISORY (never gates), with the judge down.
    code = _run(["test", "run", tf, "--agent", "a", "--trace", _demo_trace(),
                 "--transcript", _demo_transcript(), "--format", "json"])
    result = json.loads(capsys.readouterr().out)
    rub = result["rubric"]
    assert rub["schema"] == "rubric.v1"
    assert rub["advisory"] is True and rub["gated"] is False
    r0 = rub["results"][0]
    assert r0["kind"] == "rubric"
    assert r0["deterministic"] is False
    assert r0["status"] == "ERROR"          # backend unreachable, honest
    assert r0["judge"]["provider"] in ("ollama", "none")
    # full provenance is present even on the error path
    assert r0["judge"]["prompt_id"] and r0["judge"]["temperature"] == 0
    # the rubric result is NOT counted in the deterministic summary
    det = result["assertions"]["summary"]["deterministic"]
    assert det == {"pass": 1, "fail": 0, "inconclusive": 0}
    # assert.v1's own judge lane stays the {0,0} quarantine (never conflated)
    assert result["assertions"]["summary"]["judge"] == {"pass": 0, "fail": 0}
    # advisory: a judge ERROR never gates -- the deterministic lane passed
    assert code == 0


# --- repetitions > 1: per-run + a REAL reliability aggregate (Phase 2) --------

def test_repetitions_report_per_run_with_real_reliability(tmp_path, capsys):
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
    # a REAL reliability aggregate now (Phase 2 shipped): pass@1/pass@k/pass^k +
    # a Wilson CI, with the honest deterministic-replay note. Never a Phase-2
    # deferral, never a fabricated number.
    rel = result["reliability"]
    assert rel["runs"] == 3
    # File-supplied stored evidence is provenance FIXTURE, never "real" (a plain
    # `test run` never authenticates a live capture -- invariant 5, fail-closed).
    assert rel["origin"] == "fixture"
    agg = rel["aggregate"]
    assert agg["n"] == 3 and agg["k"] == 3 and agg["passes"] == 3
    assert agg["pass_at_1"] == 1.0
    assert agg["pass_at_k"] == 1.0
    # every run scores the SAME recording -> zero variance -> pass^k == pass@1
    assert agg["pass_caret_k"] == agg["pass_at_1"] == 1.0
    assert agg["ci"]["method"] == "wilson"
    assert "Phase 2" not in json.dumps(rel)
    # per-run attribution is present and honest
    assert all(r["passed"] for r in rel["per_run"])
    # no overall_score / blended number anywhere
    assert "overall_score" not in json.dumps(result)
    assert code == 0


# --- origin: FIXTURE for file-supplied evidence (never "real"), simulated only
# --- with a simulator block ---------------------------------------------------

def test_origin_is_fixture_for_file_supplied_evidence(tmp_path):
    # A plain `test run` reads its evidence from files (--transcript/--trace/
    # --state/--audio) and never authenticates a live capture, so the bound
    # conversation manifest is provenance FIXTURE -- byte-DISTINGUISHABLE from a
    # genuinely-captured live call (which would be origin.kind "real"). Asserting
    # "real" for a stored file would launder its provenance (the P0-2 defect).
    tf = _write_test(
        tmp_path, name="fixture-origin",
        deterministic=[{"id": "refunded", "kind": "tool_call",
                        "name": "issue_refund"}],
    )
    out = tmp_path / "ca"
    _run(["test", "run", tf, "--agent", "a", "--trace", _demo_trace(),
          "--out", str(out)])
    manifest = json.loads((out / "conversation.json").read_text(encoding="utf-8"))
    assert manifest["origin"]["kind"] == "fixture"
    assert manifest["origin"]["kind"] != "real"


def test_stored_fixture_run_is_byte_distinguishable_from_real_capture(
        tmp_path, capsys):
    # The exact P0-2 repro: `test run` over stored --transcript/--trace fixture
    # files, with NO simulator block. The bound conversation manifest -- in the
    # JSON result AND on disk -- and the reliability block must both be
    # provenance FIXTURE, byte-DISTINGUISHABLE from a genuinely-captured live
    # call (origin.kind "real"). "real" stays a VALID, accepted kind (reserved
    # for an authenticated capture that supplies its own origin=), so the fix
    # narrows what may CLAIM "real"; it does not delete the kind.
    tf = _write_test(
        tmp_path, name="fixture-repro",
        deterministic=[{"id": "refunded", "kind": "tool_call",
                        "name": "issue_refund", "dimension": "outcome"}],
    )
    out = tmp_path / "ca"
    _run(["test", "run", tf, "--agent", "support-v3",
          "--transcript", _demo_transcript(), "--trace", _demo_trace(),
          "--out", str(out), "--format", "json"])
    result = json.loads(capsys.readouterr().out)
    assert result["reliability"]["origin"] == "fixture"
    assert result["conversation"]["origin"] == {"kind": "fixture"}

    disk = json.loads((out / "conversation.json").read_text(encoding="utf-8"))
    assert disk["origin"] == {"kind": "fixture"}
    assert disk["origin"]["kind"] != "real"

    # "real" remains a valid origin kind -- the authenticated capture path (e.g.
    # hotato.drive) supplies it explicitly and it still builds/validates.
    assert "real" in CV.ORIGIN_KINDS
    real_manifest = CV.build_manifest(
        conversation_id="c1", agent_id="a",
        origin={"kind": "real", "provider": "vapi", "provider_call_id": "x"},
        created_at="2026-07-16T00:00:00Z",
    )
    assert real_manifest["origin"]["kind"] == "real"


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
