"""Conversation-QA Foundation 1.3 -- the 5-part BENCHMARK (deliverable item 12).

Five real, asserted proofs over the reference agent + the deterministic machinery:

(a) SIMULATION VALIDITY   -- every produced conversation is classified ok OR
    SIMULATOR_INVALID (no silent invalids); the SIMULATOR_INVALID detector fires
    on a genuinely-unfaithful rendering.
(b) OUTCOME-GROUNDING     -- a LYING transcript never PASSes a state / tool_result
    assertion (the structural no-model guarantee, proven end-to-end); the honest
    trace + state does PASS.
(c) ASSERTION DETERMINISM -- the same reference subset scored twice is byte-identical.
(d) REPORT REPRODUCIBILITY-- the same inputs render byte-identical HTML + Markdown.
(e) FAILURE -> REGRESSION -- a FAILing conversation from the reference run is frozen
    into a regression fixture that GATES (`hotato test run` exits non-zero) on
    re-run.

Ordinary pytest: fast (small subsets, not the full 375), real assertions, no
network, no model.
"""

from __future__ import annotations

import json
import os

from hotato import assert_ as A
from hotato import cli
from hotato import conversation_test as CT
from hotato import scenario as SCN
from hotato import simulate as SIM
from hotato import suite_run as SR
from hotato import test_run as TR
from hotato.state_adapter import MockStateAdapter

REF = os.path.join(os.path.dirname(__file__), "..", "examples", "reference-agent")


def _canon(obj) -> str:
    return json.dumps(obj, sort_keys=True, indent=2)


def _load_scn(name):
    return SCN.load_scenario_file(os.path.join(REF, "scenarios", f"{name}.scenario.json"))


def _load_test(name):
    return CT.load_conversation_test_file(os.path.join(REF, "tests", f"{name}.test.json"))


# A representative spread of the reference agent: 2 that pass, 2 defects.
_SUBSET = [
    "refund-damaged-order",
    "appointment-cancel-after-cutoff",
    "refund-claimed-not-issued",     # defect: outcome
    "escalate-not-handed-off",       # defect: policy
]


def test_reference_agent_files_exist_and_validate():
    """The reference agent ships >=25 scenario + >=25 conversation-test files and
    a suite binding them -- the substrate the rest of the benchmark stands on."""
    scns = [f for f in os.listdir(os.path.join(REF, "scenarios")) if f.endswith(".json")]
    tests = [f for f in os.listdir(os.path.join(REF, "tests")) if f.endswith(".json")]
    assert len(scns) >= 25
    assert len(tests) >= 25
    suite, _ = SR.load_suite_file(os.path.join(REF, "suite.json"))
    assert len(suite["tests"]) >= 25
    for name in _SUBSET:
        assert SCN.load_scenario_file(os.path.join(REF, "scenarios", f"{name}.scenario.json"))
        assert CT.load_conversation_test_file(os.path.join(REF, "tests", f"{name}.test.json"))


# =========================================================================
# (a) simulation validity -- every produced conversation is classified
# =========================================================================

def test_a_simulation_validity_no_silent_invalids():
    """Over the reference matrix every produced conversation is EITHER a faithful
    rendering (ok) OR flagged SIMULATOR_INVALID -- never silently accepted, never
    a third state; and every produced conversation is origin=simulated."""
    total_runs = 0
    for name in _SUBSET:
        scn = _load_scn(name)
        summary = SIM.run_matrix(scn)
        total_runs += summary["total"]
        # counts partition exactly: total == valid + simulator_invalid.
        assert summary["counts"]["runs"] == (
            summary["counts"]["valid"] + summary["counts"]["simulator_invalid"]
        )
        assert summary["all_simulated"] is True
        for rec in summary["runs"]:
            # Every run is classified: valid True, or a SIMULATOR_INVALID status.
            assert rec["origin_kind"] == "simulated"
            assert rec["valid"] is True or rec["simulation_status"] == SIM.SIMULATOR_INVALID
            # And re-validating the produced render agrees (no silent invalid).
            produced = SIM.render(scn, rec["seed"])
            v = SIM.validate_simulation(scn, produced)
            assert v["ok"] is True or v["status"] == SIM.SIMULATOR_INVALID
    assert total_runs > 0


def test_a_simulator_invalid_detector_actually_fires():
    """A genuinely-unfaithful rendering (the caller made to speak the AGENT's
    line) is caught as SIMULATOR_INVALID, never scored as an agent failure."""
    scn = _load_scn("refund-damaged-order")
    produced = SIM.render(scn, 0)
    assert SIM.validate_simulation(scn, produced)["ok"] is True
    # Tamper: inject a non-caller turn (the sim speaking for the agent).
    tampered = json.loads(_canon(produced))
    tampered["transcript"]["segments"].append(
        {"role": "agent", "text": "your refund is complete", "start": 99.0,
         "end": 100.0, "kind": "scripted"}
    )
    v = SIM.validate_simulation(scn, tampered)
    assert v["ok"] is False
    assert v["status"] == SIM.SIMULATOR_INVALID


# =========================================================================
# (b) outcome grounding -- a lying transcript never PASSes state/tool_result
# =========================================================================

_OUTCOME_ASSERTIONS = {
    "version": 1,
    "assertions": [
        {"id": "tool-issued", "kind": "tool_result", "name": "issue_refund",
         "result_subset": {"status": "refunded"}, "dimension": "outcome"},
        {"id": "state-refunded", "kind": "state", "resource": "orders",
         "filters": {"order_id": "A-1001"}, "expect": {"refund_status": "refunded"},
         "dimension": "outcome"},
    ],
}

# The agent's spoken CLAIM of success -- prose, not evidence.
_LYING_TRANSCRIPT = [
    {"role": "agent", "text": "Good news, your refund for order A-1001 is complete "
                              "and fully issued.", "start": 1.0, "end": 3.0},
]


def _statuses(env):
    return {r["id"]: r["status"] for r in env["results"]}


def _has_key(obj, key) -> bool:
    """Whether ``key`` appears anywhere in a nested dict/list (invariant-1 check:
    no ``overall_score`` KEY anywhere in an envelope)."""
    if isinstance(obj, dict):
        if key in obj:
            return True
        return any(_has_key(v, key) for v in obj.values())
    if isinstance(obj, list):
        return any(_has_key(v, key) for v in obj)
    return False


def test_b_lying_transcript_never_passes_state_or_tool_result():
    """The agent SAYS the refund is done, but no tool trace and no state adapter
    back it up -> tool_result and state are NEVER PASS (the structural guarantee:
    Authority-1/2 read the trace / system of record, never the spoken claim)."""
    ctx = A.build_context(transcript=_LYING_TRANSCRIPT)  # no spans, no state adapter
    env = A.run_assertions(_OUTCOME_ASSERTIONS, ctx, inconclusive_policy="report")
    st = _statuses(env)
    assert st["tool-issued"] != "PASS"
    assert st["state-refunded"] != "PASS"
    # Absent input -> honest INCONCLUSIVE, never a fabricated FAIL/PASS.
    assert st["tool-issued"] == "INCONCLUSIVE"
    assert st["state-refunded"] == "INCONCLUSIVE"


def test_b_lying_transcript_with_empty_trace_and_empty_state_fails_not_passes():
    """Even WITH a trace and a state adapter present, if the tool never ran and
    the record is not there, the grounded verdict is FAIL -- still never PASS
    from the spoken claim."""
    ctx = A.build_context(
        transcript=_LYING_TRANSCRIPT,
        spans=[{"type": "caller_audio_active", "start_sec": 0.0, "end_sec": 1.0}],
        state_adapter=MockStateAdapter({"orders": [{"order_id": "A-1001",
                                                    "refund_status": "none"}]}),
    )
    st = _statuses(A.run_assertions(_OUTCOME_ASSERTIONS, ctx, inconclusive_policy="report"))
    assert st["tool-issued"] == "FAIL"    # tool never called -> grounded FAIL
    assert st["state-refunded"] == "FAIL"  # record present but refund_status != refunded
    assert "PASS" not in st.values()


def test_b_honest_trace_and_state_do_pass():
    """The SAME assertions PASS only when the authenticated trace shows the tool
    succeeded AND the state adapter confirms it -- outcome grounded in evidence."""
    ctx = A.build_context(
        transcript=_LYING_TRANSCRIPT,   # the claim is irrelevant; evidence decides
        spans=[{"type": "tool_call", "name": "issue_refund",
                "result": {"status": "refunded", "amount": 42}}],
        state_adapter=MockStateAdapter({"orders": [{"order_id": "A-1001",
                                                    "refund_status": "refunded"}]}),
    )
    st = _statuses(A.run_assertions(_OUTCOME_ASSERTIONS, ctx, inconclusive_policy="report"))
    assert st["tool-issued"] == "PASS"
    assert st["state-refunded"] == "PASS"


def test_b_end_to_end_through_the_reference_defect():
    """End-to-end through the reference agent's OWN outcome defect: the mock agent
    claimed a refund but never called issue_refund. Scored through the simulator +
    conversation-test, the outcome assertions FAIL -- the run never PASSes on a
    claim, and the whole test FAILs (exit_code != 0)."""
    scn = _load_scn("refund-claimed-not-issued")
    test = _load_test("refund-claimed-not-issued")
    summary = SIM.run_matrix(scn, conversation_test=test)
    assert summary["exit_code"] != 0
    # No valid run scored a passing outcome (every run's scored envelope failed).
    assert all(r["score"]["exit_code"] != 0 for r in summary["runs"] if r["valid"])


# =========================================================================
# (c) assertion determinism -- the same subset scored twice is byte-identical
# =========================================================================

def test_c_assertion_determinism_byte_identical():
    """Scoring the same reference subset twice yields byte-identical results --
    a seeded replay is byte-identical (never 'the model is deterministic')."""
    def _score_all():
        out = {}
        for name in _SUBSET:
            scn = _load_scn(name)
            test = _load_test(name)
            summary = SIM.run_matrix(scn, conversation_test=test)
            # Drop the artifact paths (None here) and keep the scoring content.
            out[name] = {
                "reliability": summary["reliability"],
                "runs": [{"seed": r["seed"], "content_hash": r["content_hash"],
                          "valid": r["valid"], "score": r.get("score")}
                         for r in summary["runs"]],
                "exit_code": summary["exit_code"],
            }
        return out

    first = _canon(_score_all())
    second = _canon(_score_all())
    assert first == second


def test_c_evaluate_conversation_test_is_deterministic():
    """The per-dimension breakdown from evaluate_conversation_test is byte-stable."""
    scn = _load_scn("refund-damaged-order")
    test = _load_test("refund-damaged-order")
    produced = SIM.render(scn, 0)
    state = (scn.get("agent_mock") or {}).get("state")

    def _one():
        ctx = A.build_context(
            transcript=produced["transcript"]["segments"],
            spans=produced["trace"]["spans"],
            state_adapter=MockStateAdapter(state) if state else None,
        )
        r = TR.evaluate_conversation_test(test, ctx, agent_id="ref-v1", repetitions=1)
        return {"dimensions": r["dimensions"], "success": r["success"],
                "exit_code": r["exit_code"]}

    assert _canon(_one()) == _canon(_one())


# =========================================================================
# (d) report reproducibility -- the same inputs render byte-identical HTML+MD
# =========================================================================

def _tiny_suite(tmp_path):
    """A 2-test suite (one pass, one defect) copied into tmp so the run writes
    artifacts without touching the committed example."""
    suite = {
        "kind": "hotato.suite", "version": 1, "suite_id": "repro-suite",
        "name": "reproducibility subset", "inconclusive_policy": "fail",
        "tests": [
            os.path.join(REF, "tests", "refund-damaged-order.test.json"),
            os.path.join(REF, "tests", "refund-claimed-not-issued.test.json"),
        ],
    }
    p = tmp_path / "repro.suite.json"
    p.write_text(json.dumps(suite), encoding="utf-8")
    return str(p)


def test_d_report_reproducibility_md_and_html(tmp_path):
    """Running the whole suite twice (no registry, no timestamps) produces
    byte-identical Markdown AND HTML reports."""
    suite_path = _tiny_suite(tmp_path)
    suite_doc, base_dir = SR.load_suite_file(suite_path)

    def _run():
        return SR.run_suite(suite_doc, base_dir, agent_id="reference-agent-v1",
                            release_id="rc", registry=None)

    r1, r2 = _run(), _run()
    assert SR.render_report_md(r1) == SR.render_report_md(r2)
    assert SR.render_report_html(r1) == SR.render_report_html(r2)
    assert SR.render_summary_text(r1) == SR.render_summary_text(r2)
    # The envelope carries per-dimension + reliability, never an overall_score
    # KEY anywhere (invariant 1), checked structurally.
    assert not _has_key(r1, "overall_score")
    md = SR.render_report_md(r1)
    assert "Per-dimension" in md and "Reliability" in md


# =========================================================================
# (e) production-failure -> regression -- freeze a failing call, verify it gates
# =========================================================================

def test_e_failure_to_regression_gates(tmp_path):
    """Take a FAILing conversation from the reference run, FREEZE its evidence
    (transcript + trace + mock state) plus the conversation-test into a permanent
    regression fixture, then verify `hotato test run` GATES on it (exit != 0) --
    the SIMULATE -> EVALUATE -> FREEZE-AS-REGRESSION -> RUN-EVERY-RELEASE loop,
    every arrow preserving verifiable evidence."""
    # 1. SIMULATE + EVALUATE: the reference agent's outcome defect fails.
    name = "refund-claimed-not-issued"
    scn = _load_scn(name)
    test = _load_test(name)
    summary = SIM.run_matrix(scn, conversation_test=test)
    failing = next(r for r in summary["runs"] if r["valid"] and r["score"]["exit_code"] != 0)

    # 2. FREEZE the exact evidence that produced the failure, by digest-stable render.
    produced = SIM.render(scn, failing["seed"])
    frozen = tmp_path / "frozen-regression"
    frozen.mkdir()
    transcript_path = frozen / "transcript.json"
    trace_path = frozen / "trace.jsonl"
    state_path = frozen / "state.json"
    transcript_path.write_text(
        json.dumps({"segments": produced["transcript"]["segments"]}), encoding="utf-8")
    # voice_trace.v1 JSONL: a meta line, then one span per line.
    trace = produced["trace"]
    meta = {k: v for k, v in trace.items() if k != "spans"}
    meta["_meta"] = True
    lines = [json.dumps(meta)] + [json.dumps(s) for s in trace["spans"]]
    trace_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    state_path.write_text(
        json.dumps((scn.get("agent_mock") or {}).get("state") or {}), encoding="utf-8")

    # The frozen conversation-test pins ONLY the deterministic assertions (no
    # scenario ref -- it evaluates the FROZEN evidence, not a fresh render), with
    # inconclusive_policy: fail so a regression can never silently pass.
    frozen_test = {
        "kind": "hotato.conversation-test", "version": 1,
        "id": f"regression-{name}", "agent": "reference-agent-v1",
        "inconclusive_policy": "fail",
        "assertions": {"deterministic": test["assertions"]["deterministic"]},
        "success": {"required": ["all_deterministic_assertions_pass"]},
    }
    test_path = frozen / "regression.test.json"
    test_path.write_text(json.dumps(frozen_test), encoding="utf-8")

    # 3. RUN-EVERY-RELEASE: the existing `hotato test run` re-evaluates the frozen
    #    evidence and GATES (exit != 0) -- the regression is now permanent.
    code = cli.main([
        "test", "run", str(test_path), "--agent", "reference-agent-v1",
        "--transcript", str(transcript_path), "--trace", str(trace_path),
        "--state", str(state_path), "--format", "json",
    ])
    assert code != 0, "the frozen failing conversation must gate on re-run"


def test_e_a_passing_conversation_frozen_does_not_gate(tmp_path):
    """Control: freezing a PASSING reference conversation the same way does NOT
    gate (exit 0) -- the gate reflects real evidence, not a rigged fixture."""
    name = "refund-damaged-order"
    scn = _load_scn(name)
    test = _load_test(name)
    produced = SIM.render(scn, 0)
    frozen = tmp_path / "frozen-pass"
    frozen.mkdir()
    (frozen / "transcript.json").write_text(
        json.dumps({"segments": produced["transcript"]["segments"]}), encoding="utf-8")
    trace = produced["trace"]
    meta = {k: v for k, v in trace.items() if k != "spans"}
    meta["_meta"] = True
    (frozen / "trace.jsonl").write_text(
        "\n".join([json.dumps(meta)] + [json.dumps(s) for s in trace["spans"]]) + "\n",
        encoding="utf-8")
    (frozen / "state.json").write_text(
        json.dumps((scn.get("agent_mock") or {}).get("state") or {}), encoding="utf-8")
    frozen_test = {
        "kind": "hotato.conversation-test", "version": 1,
        "id": f"regression-{name}", "agent": "reference-agent-v1",
        "inconclusive_policy": "fail",
        "assertions": {"deterministic": test["assertions"]["deterministic"]},
        "success": {"required": ["all_deterministic_assertions_pass"]},
    }
    (frozen / "regression.test.json").write_text(json.dumps(frozen_test), encoding="utf-8")
    code = cli.main([
        "test", "run", str(frozen / "regression.test.json"),
        "--agent", "reference-agent-v1",
        "--transcript", str(frozen / "transcript.json"),
        "--trace", str(frozen / "trace.jsonl"),
        "--state", str(frozen / "state.json"), "--format", "json",
    ])
    assert code == 0
