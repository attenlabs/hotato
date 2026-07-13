"""``hotato.test-run``: the Phase-1 EXIT -- evaluate ONE conversation-test file
against a supplied conversation and emit the per-dimension scorecard.

This is the tie-together slice (Phase-1 design H). It takes a validated
``hotato.conversation-test.v1`` doc plus an :class:`hotato.assert_.Context`
built from the supplied transcript/trace/state/timing, and produces a single
``hotato.test-run.v1`` result. The honesty wall is kept STRUCTURAL, not
documented-and-hoped:

* The two assertion lanes stay SEPARATE. The ``deterministic`` lane is
  evaluated through :func:`hotato.assert_.run_assertions` (its exit code honors
  ``inconclusive_policy`` exactly as Phase 0 does). The ``rubric`` lane is now
  REALLY evaluated by the model-judge engine (:mod:`hotato.rubric`): each rubric
  assertion is scored by a pinned LOCAL model into a ``rubric.v1`` result
  (``deterministic: false`` + full provenance), never folded into the
  deterministic summary. It is ADVISORY by default (a rubric FAIL does NOT gate
  the exit code); ``gate_judge=True`` opts a team into failing on a rubric FAIL.
  When the judge backend is unreachable, or a rubric's required evidence is
  absent, the result is honestly ERROR/INCONCLUSIVE -- never a fabricated
  verdict, and never a silent gate.
* Success is a BOOLEAN over the closed :data:`hotato.conversation_test.SUCCESS_CONDITIONS`
  vocabulary -- a conjunction of named conditions, NEVER a weight or a blended
  ``overall_score``. The per-dimension breakdown groups the SAME deterministic
  results by their optional ``dimension`` tag; each dimension keeps its own
  counts and nothing is blended.
* Missing transcript/trace/state leaves the depending assertions INCONCLUSIVE
  (the evaluators' own posture) -- never a guessed PASS/FAIL.
* Reliability (pass@1 / pass@k / pass^k) is REAL (Phase 2 shipped):
  ``repetitions > 1`` runs the deterministic lane N times and reports the per-run
  results, the run count, AND a real :func:`hotato.simulate.reliability`
  aggregate (pass@1 / pass@k / pass^k + a Wilson CI). Every run scores the SAME
  supplied recording, so the deterministic lane has zero variance and
  ``pass^k == pass@1`` -- reported honestly, not a fabricated number. pass^k is
  its OWN dimension, never blended into any other and never an ``overall_score``.

The exit code is the deterministic envelope's own ``exit_code`` (which already
honors ``inconclusive_policy``), raised to a non-zero when the file's
``success.required`` conjunction fails -- a refuse (exit 2) is never downgraded.
"""

from __future__ import annotations

import json
import os
import shutil
from typing import Any, Dict, List, Optional

from . import assert_ as A
from . import conversation as CV
from . import conversation_test as CT
from . import rubric as RUB

# Success conditions that read the model-judged (rubric) lane. These are
# ADVISORY by default: their boolean is reported honestly, but they only affect
# the exit code when the run opts in with ``gate_judge=True`` (the CLI's
# ``--gate-judge``). A model verdict never silently gates a release.
_RUBRIC_SUCCESS_CONDITIONS = frozenset({"no_rubric_failure"})

__all__ = [
    "KIND",
    "VERSION",
    "evaluate_success",
    "dimension_breakdown",
    "evaluate_conversation_test",
    "assemble_conversation_artifact",
    "render_summary_text",
]

KIND = "hotato.test-run"
VERSION = 1


def _lane(doc: Dict[str, Any], name: str) -> List[Dict[str, Any]]:
    """The ``deterministic`` or ``rubric`` assertion list from a validated
    conversation-test doc (an absent lane is an empty list)."""
    return list((doc.get("assertions") or {}).get(name) or [])


def _run_deterministic(
    det_list: List[Dict[str, Any]], ctx: A.Context, policy: str, reps: int
) -> List[Dict[str, Any]]:
    """Evaluate the deterministic lane ``reps`` times, returning the per-run
    ``assert.v1`` envelopes. An EMPTY deterministic lane is a valid, honest run
    (no deterministic assertion to check) -- :func:`hotato.assert_.run_assertions`
    rejects an empty ``assertions`` list, so that case is served directly by
    :func:`hotato.assert_.envelope_from_results` with no results. Every run is
    over the SAME context, so in Phase 1 the runs are identical by construction
    (the deterministic lane has zero variance); the per-run list exists so
    Phase 2's simulator-driven variance drops in without changing the shape."""
    if not det_list:
        return [A.envelope_from_results([], inconclusive_policy=policy)
                for _ in range(reps)]
    assert_doc = {"version": 1, "assertions": det_list, "inconclusive_policy": policy}
    return [A.run_assertions(assert_doc, ctx, inconclusive_policy=policy)
            for _ in range(reps)]


def _evaluate_rubric_lane(
    rubric_list: List[Dict[str, Any]],
    ctx: A.Context,
    *,
    judge: Any = None,
    cache: Any = None,
    no_cache: bool = False,
    gate_judge: bool = False,
) -> Dict[str, Any]:
    """REALLY evaluate the model-judged rubric lane through
    :mod:`hotato.rubric`, returning a ``rubric.v1`` envelope
    (``deterministic: false`` + full provenance per result). An empty lane is a
    valid, honest run (an empty envelope). ``judge`` defaults to a LOCAL
    :class:`hotato.rubric.OllamaJudge` (zero egress); tests inject a
    deterministic fake. Advisory by default (``gate_judge`` opts into gating)."""
    if not rubric_list:
        return RUB.rubric_envelope([], gate=gate_judge)
    if judge is None:
        judge = RUB.OllamaJudge()
    return RUB.evaluate_rubric_lane(
        rubric_list,
        transcript=ctx.transcript,
        trace=ctx.spans,
        judge=judge,
        cache=cache,
        no_cache=no_cache,
        gate=gate_judge,
    )


def evaluate_success(
    required: List[str],
    det_results: List[Dict[str, Any]],
    rubric_results: List[Dict[str, Any]],
    *,
    gate_judge: bool = False,
) -> Dict[str, Any]:
    """Evaluate the ``success.required`` conjunction over the two lanes and
    return ``{"required", "conditions": {name: bool}, "passed", "rubric_gated"}``.

    Each condition is a plain boolean predicate over the results -- there is no
    weighting and no score. The deterministic conditions read the deterministic
    lane; ``no_rubric_failure`` reads the REAL model-judged rubric lane.

    ``passed`` is the conjunction of the conditions that actually GATE this run:
    the deterministic conditions always, plus the rubric condition
    (``no_rubric_failure``) ONLY when ``gate_judge=True``. Every condition's
    boolean is still reported in ``conditions`` (honest), but a model verdict is
    advisory unless the run opts into ``--gate-judge`` -- so a rubric FAIL never
    silently blocks a release. ``no_inconclusive`` is scoped to the
    deterministic lane."""
    det_status = [r["status"] for r in det_results]
    rubric_status = [r.get("status") for r in rubric_results]
    available = {
        "all_deterministic_assertions_pass": all(s == "PASS" for s in det_status),
        "no_deterministic_fail": not any(s == "FAIL" for s in det_status),
        "no_rubric_failure": not any(s == "FAIL" for s in rubric_status),
        "no_inconclusive": not any(s == "INCONCLUSIVE" for s in det_status),
    }
    conditions = {name: available[name] for name in required}
    gating = {
        name: val for name, val in conditions.items()
        if name not in _RUBRIC_SUCCESS_CONDITIONS or gate_judge
    }
    return {
        "required": list(required),
        "conditions": conditions,
        "passed": all(gating.values()),
        "rubric_gated": gate_judge,
    }


def dimension_breakdown(det_results: List[Dict[str, Any]]) -> Dict[str, Any]:
    """Group the deterministic results by their optional ``dimension`` tag into
    the five report dimensions plus an ``ungrouped`` bucket -- each with its OWN
    pass/fail/inconclusive counts and the ids that landed there. A GROUPING
    VIEW, never a blend: there is no combined number across dimensions or within
    one, and no ``overall_score``. Untagged results go to ``ungrouped`` and are
    never dropped."""
    def _empty() -> Dict[str, Any]:
        return {"pass": 0, "fail": 0, "inconclusive": 0, "ids": []}

    dims: Dict[str, Any] = {d: _empty() for d in CT.REPORT_DIMENSIONS}
    ungrouped = _empty()
    for r in det_results:
        dim = r.get("dimension")
        bucket = dims[dim] if dim in dims else ungrouped
        bucket[r["status"].lower()] += 1
        bucket["ids"].append(r["id"])
    out: Dict[str, Any] = {"dimensions": dims}
    if ungrouped["ids"]:
        out["ungrouped"] = ungrouped
    return out


def evaluate_conversation_test(
    doc: Dict[str, Any],
    ctx: A.Context,
    *,
    agent_id: str,
    repetitions: Optional[int] = None,
    judge: Any = None,
    cache: Any = None,
    no_cache: bool = False,
    gate_judge: bool = False,
) -> Dict[str, Any]:
    """Evaluate one validated conversation-test ``doc`` against ``ctx`` and
    return a ``hotato.test-run.v1`` result dict (carrying its own ``exit_code``).

    ``repetitions`` overrides the doc's own ``repetitions``. The two assertion
    lanes stay separate: the deterministic lane produces an ``assert.v1``
    envelope; the rubric lane is REALLY scored by :mod:`hotato.rubric` into a
    ``rubric.v1`` envelope (``deterministic: false`` + provenance), ADVISORY by
    default. ``judge`` defaults to a LOCAL Ollama judge (zero egress); tests
    inject a fake. ``gate_judge`` opts into failing the exit code on a rubric
    FAIL. The returned dict never contains an ``overall_score`` or any blended
    number; the exit code honors the doc's ``inconclusive_policy`` and is raised
    to non-zero only when a GATING ``success.required`` condition fails."""
    policy = doc["inconclusive_policy"]
    reps = repetitions if repetitions is not None else doc.get("repetitions", 1)
    if isinstance(reps, bool) or not isinstance(reps, int) or reps < 1:
        raise ValueError(f"repetitions must be an integer >= 1, got {reps!r}")

    det_list = _lane(doc, "deterministic")
    rubric_list = _lane(doc, "rubric")

    per_run = _run_deterministic(det_list, ctx, policy, reps)
    det_env = per_run[0]
    rubric_env = _evaluate_rubric_lane(
        rubric_list, ctx, judge=judge, cache=cache, no_cache=no_cache,
        gate_judge=gate_judge,
    )
    rubric_results = rubric_env["results"]

    required = list((doc.get("success") or {}).get("required") or [])
    success = evaluate_success(required, det_env["results"], rubric_results,
                              gate_judge=gate_judge)

    # Exit code: start from the deterministic envelope's own code (which honors
    # inconclusive_policy -- report/fail/refuse -- exactly as Phase 0). A GATING
    # success.required failure raises a passing (0) run to 1; a refuse (2) or an
    # already-failing (1) run is never downgraded. A rubric FAIL only gates when
    # gate_judge=True (rubric_env's own advisory exit_code encodes that).
    exit_code = det_env["exit_code"]
    if not success["passed"] and exit_code == 0:
        exit_code = 1
    if gate_judge and rubric_env["exit_code"] == 1 and exit_code == 0:
        exit_code = 1

    breakdown = dimension_breakdown(det_env["results"])

    # Reliability (pass@1 / pass@k / pass^k) over the ``reps`` deterministic runs
    # (Phase 2 shipped). A run passes iff its deterministic envelope's exit_code
    # is 0 (which already honors inconclusive_policy). Every run scores the SAME
    # supplied recording, so the lane has zero variance and pass^k == pass@1 --
    # reported honestly, not fabricated variance. Reused from the same
    # reliability() the simulator matrix uses, so the number means exactly what it
    # does everywhere else.
    from . import simulate as _sim
    origin_kind = _origin_from_doc(doc)["kind"]
    per_run_pass = [env["exit_code"] == 0 for env in per_run]
    rel_note = (
        f"reliability over {reps} repeated run(s) of the deterministic lane on "
        f"the same supplied {origin_kind} recording; the lane has zero variance, "
        "so pass^k == pass@1 -- reported honestly, not fabricated variance"
    )
    rel_aggregate = dict(_sim.reliability(per_run_pass))
    rel_aggregate["note"] = rel_note

    result: Dict[str, Any] = {
        "kind": KIND,
        "version": VERSION,
        "test_id": doc["id"],
        "agent": agent_id,
        "inconclusive_policy": policy,
        "exit_code": exit_code,
        "success": success,
        "assertions": det_env,
        "rubric": rubric_env,
        "dimensions": breakdown["dimensions"],
        # Reliability (pass@1 / pass@k / pass^k) over the reps runs, plus the run
        # count and the per-run outcomes. A real aggregate now (Phase 2 shipped) --
        # never a fabricated number, never blended into any other dimension. The
        # ``aggregate`` is the same reliability() dict the simulator matrix emits;
        # ``origin`` labels whether these runs were over a REAL or SIMULATED
        # recording (a simulator's replay reliability is never production
        # reliability). The report threads this into the scorecard's Reliability
        # dimension when reps > 1.
        "reliability": {
            "aggregate": rel_aggregate,
            "origin": origin_kind,
            "runs": reps,
            "basis": "agent_deterministic_replay",
            "note": rel_note,
            "per_run": [
                {"run": i + 1, "exit_code": env["exit_code"],
                 "passed": env["exit_code"] == 0}
                for i, env in enumerate(per_run)
            ],
        },
        "repetitions": {
            "runs": reps,
            "per_run": [
                {"run": i + 1, "exit_code": env["exit_code"],
                 "summary": env["summary"]["deterministic"]}
                for i, env in enumerate(per_run)
            ],
        },
    }
    if "ungrouped" in breakdown:
        result["ungrouped"] = breakdown["ungrouped"]
    return result


# =========================================================================
# The Conversation Artifact (conversation.v1): bind the supplied evidence by
# digest into --out so `hotato conversation verify` can re-check it.
# =========================================================================

def _origin_from_doc(doc: Dict[str, Any]) -> Dict[str, Any]:
    """``origin`` for the conversation artifact: REAL by default (Phase 1
    evaluates a supplied real recording), SIMULATED only when the test file
    carries an explicit ``simulator`` block. Synthetic is never conflated with
    real (invariant 5): a simulated origin must declare its model/scenario/seed,
    which :func:`hotato.conversation.build_manifest` enforces."""
    sim = doc.get("simulator")
    if isinstance(sim, dict) and sim:
        return {"kind": "simulated", "simulator": sim}
    return {"kind": "real"}


def _write_json(path: str, obj: Any) -> None:
    # open-ok: path is inside the --out directory this run created/owns.
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(obj, fh, indent=2, sort_keys=True)
        fh.write("\n")


def assemble_conversation_artifact(
    out_dir: str,
    *,
    conversation_id: str,
    agent_id: str,
    origin: Dict[str, Any],
    created_at: str,
    assertions_env: Dict[str, Any],
    audio_path: Optional[str] = None,
    transcript_path: Optional[str] = None,
    trace_path: Optional[str] = None,
    timing: Any = None,
    scenario_digest: Optional[str] = None,
) -> Dict[str, Any]:
    """Copy/write the supplied evidence into ``out_dir`` and bind it by sha256
    into a ``hotato.conversation.v1`` manifest (written as
    ``<out_dir>/conversation.json``), returning the manifest.

    Each child lands under ``out_dir`` (so the manifest travels with the
    directory and :func:`hotato.conversation.verify` re-hashes it there): the
    audio copied under ``audio/``, the transcript/trace copied verbatim, and the
    scored ``timing`` + the evaluated ``assertions`` envelope written as JSON.
    ``created_at`` is caller-supplied (never ``Date.now()`` on this
    deterministic path). Only the children actually supplied are bound."""
    os.makedirs(out_dir, exist_ok=True)
    artifact_files: Dict[str, str] = {}

    if audio_path is not None:
        audio_dir = os.path.join(out_dir, "audio")
        os.makedirs(audio_dir, exist_ok=True)
        dst = os.path.join(audio_dir, os.path.basename(audio_path))
        shutil.copy2(audio_path, dst)
        artifact_files["audio"] = dst
    if transcript_path is not None:
        dst = os.path.join(out_dir, "transcript.json")
        shutil.copy2(transcript_path, dst)
        artifact_files["transcript"] = dst
    if trace_path is not None:
        dst = os.path.join(out_dir, "trace.jsonl")
        shutil.copy2(trace_path, dst)
        artifact_files["trace"] = dst
    if timing is not None:
        dst = os.path.join(out_dir, "timing.json")
        _write_json(dst, timing)
        artifact_files["timing"] = dst
    # The evaluated deterministic envelope is always bound: it IS the evidence of
    # what was checked and how it came out.
    assertions_dst = os.path.join(out_dir, "assertions.json")
    _write_json(assertions_dst, assertions_env)
    artifact_files["assertions"] = assertions_dst

    manifest = CV.build_manifest(
        conversation_id=conversation_id,
        agent_id=agent_id,
        origin=origin,
        created_at=created_at,
        artifact_files=artifact_files,
        base_dir=out_dir,
        scenario_digest=scenario_digest,
    )
    CV.write_conversation(manifest, out_dir)
    return manifest


# =========================================================================
# Human-readable per-dimension summary (text). Prints the deterministic and the
# quarantined judge tallies SEPARATELY -- never one merged number.
# =========================================================================

def render_summary_text(result: Dict[str, Any]) -> str:
    """The ``hotato test run`` per-dimension summary: the success verdict, each
    dimension's own pass/fail/inconclusive counts (never blended), the plain
    reliability run count, and the deterministic vs judge tallies printed
    separately -- the same structural honesty the JSON output carries."""
    lines = [
        f"hotato test run: {result['test_id']} (agent {result['agent']}) "
        f"-- exit_code={result['exit_code']}"
    ]
    lines.append(f"inconclusive_policy: {result['inconclusive_policy']}")

    success = result["success"]
    verdict = "PASS" if success["passed"] else "FAIL"
    req = ", ".join(success["required"]) or "(none required)"
    lines.append(f"success: {verdict}  (required: {req})")
    for name, ok in success["conditions"].items():
        lines.append(f"  [{'ok' if ok else 'X '}] {name}")

    lines.append("per-dimension (grouped view; never blended):")
    dims = result["dimensions"]
    for name in CT.REPORT_DIMENSIONS:
        b = dims[name]
        lines.append(
            f"  {name:<13} {b['pass']} pass / {b['fail']} fail / "
            f"{b['inconclusive']} inconclusive"
        )
    ung = result.get("ungrouped")
    if ung:
        lines.append(
            f"  {'ungrouped':<13} {ung['pass']} pass / {ung['fail']} fail / "
            f"{ung['inconclusive']} inconclusive"
        )

    rel = result["reliability"]
    agg = rel.get("aggregate") or {}
    if rel.get("runs", 1) > 1 and agg:
        lines.append(
            f"reliability [{rel.get('basis')}, origin={rel.get('origin')}]: "
            f"pass@1={agg.get('pass_at_1', 0.0):.3f} "
            f"pass@k={agg.get('pass_at_k', 0.0):.3f} "
            f"pass^k={agg.get('pass_caret_k', 0.0):.3f} (n={agg.get('n', 0)})"
        )
    lines.append(rel["note"])

    det = result["assertions"]["summary"]["deterministic"]
    lines.append(
        f"deterministic: {det['pass']} pass, {det['fail']} fail, "
        f"{det['inconclusive']} inconclusive"
    )

    rubric_env = result["rubric"]
    rs = rubric_env["summary"]
    gated = "GATED" if rubric_env.get("gated") else "advisory"
    lines.append(
        f"rubric (model-judged, {gated}): {rs['pass']} pass, {rs['fail']} fail, "
        f"{rs['inconclusive']} inconclusive, {rs['error']} error "
        "(deterministic:false; never merged into the deterministic counts)"
    )
    for r in rubric_env["results"]:
        j = r.get("judge") or {}
        cached = "cached" if j.get("cached") else "fresh"
        model = j.get("model", "?")
        lines.append(f"  [{r['status']:<12}] {r['id']}  ({model}, {cached})")
    return "\n".join(lines) + "\n"
