"""``hotato suite run``: execute a ``suite.v1`` of conversation-tests and emit a
per-dimension + reliability suite report (1.3 item 4 / GPT-Pro §12).

A suite is a NAMED SET of conversation-test refs (never a blend). This module is
the integration layer that turns that set into a real run:

* Each referenced conversation-test that declares a ``scenario`` is executed
  OFFLINE through the deterministic scripted-caller simulator
  (:func:`hotato.simulate.run_matrix`): the scenario's variation matrix expands
  into concrete runs, each rendered + validated + scored against the test's
  DETERMINISTIC lane (Authority-1 tool spans + Authority-2 mock-state sandbox
  come from the scenario's ``agent_mock``; there is no live agent, no network).
  A test WITHOUT a scenario is evaluated once against an empty context -- its
  input-dependent checks are honestly INCONCLUSIVE, never guessed.
* The results are recorded into the fleet registry (Release / Suite / Scenario /
  Run / Conversation / Evaluation), so ``hotato serve`` renders the five views
  over them.
* The report carries per-dimension counts + reliability (pass@1 / pass@k /
  pass^k) ONLY -- there is NO blended score and no ``overall_score`` anywhere
  (invariant 1). Simulated origin is always labelled (invariant 5). A
  SIMULATOR_INVALID run is a broken fixture, never an agent PASS/FAIL
  (invariant 5), bucketed separately.

Honesty of the exit code: the SUITE's ``inconclusive_policy`` governs the run
(report | fail | refuse), so a required CI/compliance suite can make an
INCONCLUSIVE (absent required input) FAIL the gate (invariant 3). The suite exit
code is the WORST test outcome under that policy (refuse=2 > fail=1 > pass=0),
raised to >=1 by any SIMULATOR_INVALID run.
"""

from __future__ import annotations

import json
import os
from typing import Any, Dict, List, Optional, Tuple

from . import assert_ as A
from . import conversation_test as CT
from . import scenario as SCN
from . import simulate as SIM
from . import test_run as TR
from .errors import open_regular as _open_regular

__all__ = [
    "KIND",
    "VERSION",
    "load_suite_file",
    "resolve_test_refs",
    "run_suite",
    "render_summary_text",
    "render_report_md",
    "render_report_html",
]

KIND = "hotato.suite-run"
VERSION = 1

_DIMS = CT.REPORT_DIMENSIONS


# =========================================================================
# loading + ref resolution
# =========================================================================

def load_suite_file(path: str) -> Tuple[Dict[str, Any], str]:
    """Load, parse, and validate a ``suite.v1`` file. Returns
    ``(normalized_suite_doc, base_dir)`` -- ``base_dir`` is the directory the
    suite file lives in, against which relative ``tests`` refs resolve. A
    FIFO/named-pipe path raises immediately; a malformed suite raises
    ``ValueError`` (the caller's exit-2 path)."""
    with _open_regular(path, "r", encoding="utf-8") as fh:
        text = fh.read()
    doc = CT.validate_suite(CT.parse_conversation_test(text))
    return doc, os.path.dirname(os.path.abspath(path))


def resolve_test_refs(tests: List[str], base_dir: str) -> List[str]:
    """Resolve each conversation-test ref to a concrete file path. A ref that is
    already an existing path is used as-is; otherwise it is resolved relative to
    the suite file's directory. A ref that resolves to no file raises
    ``ValueError`` (exit 2) -- a suite must not silently skip a test it names."""
    resolved: List[str] = []
    for ref in tests:
        cand = ref if os.path.isabs(ref) else os.path.join(base_dir, ref)
        if os.path.isfile(ref):
            resolved.append(ref)
        elif os.path.isfile(cand):
            resolved.append(cand)
        else:
            raise ValueError(
                f"suite test ref {ref!r} resolves to no file (looked at {ref!r} "
                f"and {cand!r}); a suite must not silently skip a named test"
            )
    return resolved


def _scenario_path_for(test_doc: Dict[str, Any], test_path: str) -> Optional[str]:
    """The scenario file a conversation-test points at (``scenario`` field),
    resolved relative to the TEST file's directory, or ``None`` when the test
    declares no scenario (a static, context-supplied test)."""
    ref = test_doc.get("scenario")
    if not ref:
        return None
    if os.path.isabs(ref) and os.path.isfile(ref):
        return ref
    cand = os.path.join(os.path.dirname(os.path.abspath(test_path)), ref)
    if os.path.isfile(cand):
        return cand
    if os.path.isfile(ref):
        return ref
    raise ValueError(
        f"conversation-test {test_doc['id']!r} references scenario {ref!r}, "
        f"which resolves to no file (looked at {cand!r})"
    )


# =========================================================================
# per-test execution
# =========================================================================

def _status_from_exit(exit_code: int) -> str:
    return {0: "pass", 1: "fail", 2: "refuse"}.get(exit_code, "fail")


def _dim_rollup(breakdown: Dict[str, Any]) -> Dict[str, Optional[str]]:
    """Per-dimension aggregate status (FAIL > INCONCLUSIVE > PASS) from a
    :func:`hotato.test_run.dimension_breakdown` ``dimensions`` map, or ``None``
    for a dimension with no assertion tagged into it. A grouping view, never a
    blend across dimensions."""
    out: Dict[str, Optional[str]] = {}
    for d in _DIMS:
        b = breakdown.get(d) or {"pass": 0, "fail": 0, "inconclusive": 0}
        if b["fail"]:
            out[d] = "FAIL"
        elif b["inconclusive"]:
            out[d] = "INCONCLUSIVE"
        elif b["pass"]:
            out[d] = "PASS"
        else:
            out[d] = None
    return out


def _representative_eval(
    test_doc: Dict[str, Any], scenario_doc: Dict[str, Any], first_seed: int,
    *, agent_id: str,
) -> Dict[str, Any]:
    """Evaluate the conversation-test against ONE rendered run's context -- the
    per-dimension breakdown + success + rubric envelope. For a deterministic
    scripted caller every run of a scenario produces the SAME transcript / mock
    tool spans / mock state, so this representative breakdown is identical across
    the matrix (only reliability, computed over all runs by run_matrix, carries
    the run-to-run picture). The state adapter (Authority 2) is built from the
    scenario's ``agent_mock.state`` so ``state``/``state_change`` assertions
    evaluate offline; a test with no rubric lane never touches a model."""
    produced = SIM.render(scenario_doc, first_seed)
    state_data = (scenario_doc.get("agent_mock") or {}).get("state")
    state_adapter = None
    if state_data:
        from .state_adapter import MockStateAdapter
        state_adapter = MockStateAdapter(state_data)
    ctx = A.build_context(
        transcript=produced["transcript"]["segments"],
        spans=produced["trace"]["spans"],
        state_adapter=state_adapter,
    )
    # Deterministic-only reference tests carry no rubric lane, so judge=None is
    # never reached; if a caller supplies a rubric lane with no judge the rubric
    # results are honest ERROR/INCONCLUSIVE, never a fabricated verdict.
    return TR.evaluate_conversation_test(
        test_doc, ctx, agent_id=agent_id, repetitions=1,
    )


def _run_scenario_test(
    test_doc: Dict[str, Any], scenario_path: str, *, agent_id: str,
    out_dir: Optional[str], max_workers: Optional[int],
    created_at: Optional[str] = None,
) -> Dict[str, Any]:
    """Execute one scenario-driven conversation-test through the deterministic
    simulator matrix + compute its representative per-dimension breakdown.

    ``created_at`` pins the written conversation manifests' timestamp; omitted,
    run_matrix uses a reproducible SOURCE_DATE_EPOCH-style default (never the wall
    clock), so re-running the suite writes byte-identical artifacts."""
    scenario_doc = SCN.load_scenario_file(scenario_path)
    runs = SIM.expand(scenario_doc)

    test_out = os.path.join(out_dir, test_doc["id"]) if out_dir else None
    summary = SIM.run_matrix(
        scenario_doc, conversation_test=test_doc, out_dir=test_out,
        max_workers=max_workers, created_at=created_at,
    )

    # The representative breakdown is computed over the FIRST valid run. When
    # every run is SIMULATOR_INVALID there is no agent evidence to evaluate -- an
    # honest all-invalid outcome, never a fabricated pass.
    first_valid = next((r for r in summary["runs"] if r["valid"]), None)
    dim_reason: Dict[str, str] = {}
    if first_valid is not None:
        rep = _representative_eval(
            test_doc, scenario_doc, first_valid["seed"], agent_id=agent_id,
        )
        breakdown = rep["dimensions"]
        success = rep["success"]
        rubric = rep.get("rubric")
        ungrouped = rep.get("ungrouped")
        # The first FAILing assertion's reason per dimension -> a real, observable
        # failure signature for the workspace's failure-cluster view.
        for r in rep["assertions"]["results"]:
            if r.get("status") == "FAIL":
                d = r.get("dimension")
                if d and d not in dim_reason and r.get("reason"):
                    dim_reason[d] = f"{r.get('kind', 'assertion')}: {r['reason']}"
    else:
        breakdown = {d: {"pass": 0, "fail": 0, "inconclusive": 0} for d in _DIMS}
        success = {"required": list((test_doc.get("success") or {}).get("required") or []),
                   "conditions": {}, "passed": False, "rubric_gated": False}
        rubric = None
        ungrouped = None

    # The suite exit for this test is run_matrix's own exit (which honors the
    # effective inconclusive_policy: 0/1/2, refuse-precedence + SIMULATOR_INVALID
    # -> >=1), raised to non-zero when a GATING success.required condition failed.
    exit_code = summary["exit_code"]
    if first_valid is not None and not success["passed"] and exit_code == 0:
        exit_code = 1

    return {
        "test_id": test_doc["id"],
        "scenario_id": scenario_doc["id"],
        "agent": agent_id,
        "kind": "scenario",
        "inconclusive_policy": test_doc["inconclusive_policy"],
        "dimensions": _dim_rollup(breakdown),
        "dim_counts": {d: breakdown.get(d) for d in _DIMS},
        "dim_reason": dim_reason,
        "ungrouped": ungrouped,
        "success": success,
        "rubric_summary": (rubric or {}).get("summary") if rubric else None,
        "reliability": summary["reliability"],
        "reliability_basis": summary["reliability_basis"],
        "counts": summary["counts"],
        "variation_cells": summary["variation_cells"],
        "simulator_invalid": summary["simulator_invalid"],
        "runs": summary["runs"],
        "exit_code": exit_code,
        "status": _status_from_exit(exit_code),
        "origin": "simulated",
    }


def _run_static_test(
    test_doc: Dict[str, Any], *, agent_id: str,
) -> Dict[str, Any]:
    """Evaluate a conversation-test that declares NO scenario against an EMPTY
    context: every input-dependent check is honestly INCONCLUSIVE (absent input,
    never a guess). Exit honors the effective inconclusive_policy."""
    ctx = A.build_context()
    rep = TR.evaluate_conversation_test(test_doc, ctx, agent_id=agent_id, repetitions=1)
    return {
        "test_id": test_doc["id"],
        "scenario_id": None,
        "agent": agent_id,
        "kind": "static",
        "inconclusive_policy": test_doc["inconclusive_policy"],
        "dimensions": _dim_rollup(rep["dimensions"]),
        "dim_counts": {d: rep["dimensions"].get(d) for d in _DIMS},
        "ungrouped": rep.get("ungrouped"),
        "success": rep["success"],
        "rubric_summary": rep.get("rubric", {}).get("summary"),
        "reliability": rep["reliability"]["aggregate"],
        "reliability_basis": "static_no_scenario",
        "counts": {"runs": 1, "valid": 1, "simulator_invalid": 0, "scored": 1},
        "variation_cells": [],
        "simulator_invalid": [],
        "runs": [],
        "exit_code": rep["exit_code"],
        "status": _status_from_exit(rep["exit_code"]),
        "origin": "real",
    }


# =========================================================================
# registry population (so `hotato serve` renders the five views)
# =========================================================================

def _register_results(
    reg: Any, workspace: str, *, suite_doc: Dict[str, Any], agent_id: str,
    release_id: str, per_test: List[Dict[str, Any]], created_at_epoch: Optional[float],
) -> None:
    """Index the run into the 8-entity model: one Release, one Suite, and per
    test a Scenario, then per matrix run a Run + Conversation + per-dimension
    Evaluations. The DB only INDEXES the evidence (origin=simulated on every
    conversation, per-dimension PASS/FAIL/INCONCLUSIVE on every evaluation, never
    a blended score); the immutable artifacts stay the source of truth."""
    reg.ensure_workspace(workspace)
    reg.add_release(workspace, release_id, agent_id=agent_id, created_at=created_at_epoch)
    reg.add_suite(
        workspace, suite_doc["suite_id"], name=suite_doc.get("name"),
        purpose=suite_doc.get("purpose"),
        required_for_release=suite_doc.get("required_for_release", False),
        inconclusive_policy=suite_doc["inconclusive_policy"],
        created_at=created_at_epoch,
    )
    for t in per_test:
        scn_id = t["test_id"]
        reg.add_scenario(
            workspace, scn_id, suite_id=suite_doc["suite_id"],
            goal=t.get("scenario_id"),
            assertions_json=json.dumps(t["dimensions"], sort_keys=True),
            created_at=created_at_epoch,
        )
        # Per-dimension status for this test (identical across its runs, since the
        # deterministic caller has zero variance); written once per run so serve's
        # reliability (fraction of runs passing) is real.
        dim_status = t["dimensions"]
        for rec in t["runs"]:
            # Namespace the registry ids by RELEASE: run_matrix mints run/
            # conversation ids from (scenario_id, index) only, so the SAME suite
            # run against two releases would otherwise collide (the upsert would
            # clobber the first release's rows). Prefixing with release_id keeps
            # each release's runs distinct so `release compare` can diff them.
            run_id = f"{release_id}:{rec['run_id']}"
            if not rec.get("valid"):
                # A SIMULATOR_INVALID run is a broken fixture -> recorded as a
                # refused Run with NO evaluations (never an agent PASS/FAIL).
                reg.add_run(workspace, run_id, scenario_id=scn_id,
                            release_id=release_id, seed=str(rec["seed"]),
                            status="refused", created_at=created_at_epoch)
                continue
            conv_id = f"{release_id}:{rec.get('conversation_id') or rec['run_id']}"
            reg.add_run(workspace, run_id, scenario_id=scn_id, release_id=release_id,
                        seed=str(rec["seed"]), status="completed",
                        created_at=created_at_epoch)
            reg.add_conversation(
                workspace, conv_id, run_id=run_id, agent_id=agent_id,
                origin="simulated", artifact_digest=rec.get("content_hash"),
                created_at=created_at_epoch,
            )
            for dim, status in dim_status.items():
                if status is None:
                    continue
                prov = {"basis": "agent_deterministic", "origin": "simulated"}
                if status == "FAIL" and t.get("dim_reason", {}).get(dim):
                    prov["reason"] = t["dim_reason"][dim]
                reg.add_evaluation(
                    workspace, f"{conv_id}:{dim}", conversation_id=conv_id,
                    evaluator_id="deterministic-suite", dimension=dim,
                    status=status,
                    evidence_refs=json.dumps({"seed": rec["seed"],
                                              "content_hash": rec.get("content_hash")},
                                             sort_keys=True),
                    provenance=json.dumps(prov, sort_keys=True),
                    created_at=created_at_epoch,
                )


# =========================================================================
# the suite run
# =========================================================================

def run_suite(
    suite_doc: Dict[str, Any], base_dir: str, *,
    agent_id: str,
    release_id: Optional[str] = None,
    workspace: str = "default",
    registry: Any = None,
    out_dir: Optional[str] = None,
    max_workers: Optional[int] = None,
    created_at_epoch: Optional[float] = None,
    created_at: Optional[str] = None,
) -> Dict[str, Any]:
    """Run every conversation-test the suite names and return a suite-run
    envelope (per-dimension + reliability, never a blended score).

    ``registry`` (a :class:`hotato.fleet.registry.Registry`, or ``None`` to skip
    indexing) receives the Release/Suite/Scenario/Run/Conversation/Evaluation
    rows so ``hotato serve`` renders them. ``out_dir`` (optional) receives the
    per-test simulated conversation artifacts.

    Two independent provenance knobs, deliberately distinct: ``created_at`` (ISO
    string) pins the written conversation manifests' timestamp and DEFAULTS to a
    reproducible SOURCE_DATE_EPOCH-style instant (never the wall clock), so the
    immutable artifacts are byte-identical across re-runs. ``created_at_epoch``
    (float) stamps the registry ROWS, which are runtime bookkeeping (the fleet.db
    is mutable index state, explicitly NOT part of the byte-identical artifact
    claim); left ``None`` the registry uses its own runtime clock. The SUITE's
    ``inconclusive_policy``
    is the effective policy for every test (so a required CI suite can make an
    INCONCLUSIVE FAIL the gate); the suite exit code is the WORST test outcome
    under it, raised to >=1 by any SIMULATOR_INVALID run."""
    policy = suite_doc["inconclusive_policy"]
    release_id = release_id or f"{agent_id}@{suite_doc['suite_id']}"
    test_paths = resolve_test_refs(suite_doc.get("tests") or [], base_dir)

    per_test: List[Dict[str, Any]] = []
    for tp in test_paths:
        raw = CT.load_conversation_test_file(tp)
        # The SUITE policy is authoritative for the run (invariant 3): a test's
        # own policy never weakens a required CI suite. Re-validate the override
        # copy so the normalized doc is consistent.
        overridden = CT.validate_conversation_test_doc(
            {**raw, "inconclusive_policy": policy}
        )
        scenario_path = _scenario_path_for(overridden, tp)
        if scenario_path is not None:
            per_test.append(_run_scenario_test(
                overridden, scenario_path, agent_id=agent_id,
                out_dir=out_dir, max_workers=max_workers,
                created_at=created_at,
            ))
        else:
            per_test.append(_run_static_test(overridden, agent_id=agent_id))

    # Suite-level per-dimension grouped view: sum each test's representative
    # per-dimension counts. A grouping across tests, never a blend within or
    # across dimensions.
    dim_counts: Dict[str, Dict[str, int]] = {
        d: {"pass": 0, "fail": 0, "inconclusive": 0} for d in _DIMS
    }
    for t in per_test:
        for d in _DIMS:
            b = t["dim_counts"].get(d)
            if b:
                for k in ("pass", "fail", "inconclusive"):
                    dim_counts[d][k] += b.get(k, 0)

    # Suite-level reliability: pass@1 / pass@k / pass^k over EVERY valid run in
    # the whole suite (never blended into any other number). A run passes iff its
    # scored deterministic envelope's exit_code was 0.
    all_run_passes: List[bool] = []
    total_invalid: List[Dict[str, Any]] = []
    for t in per_test:
        for rec in t["runs"]:
            if not rec.get("valid"):
                continue
            score = rec.get("score")
            all_run_passes.append(bool(score) and score["exit_code"] == 0)
        for inv in t["simulator_invalid"]:
            total_invalid.append({"test_id": t["test_id"], **inv})
    reliability = dict(SIM.reliability(all_run_passes))

    total_runs = sum(t["counts"].get("runs", 0) for t in per_test)
    total_valid = sum(t["counts"].get("valid", 0) for t in per_test)
    passed_tests = sum(1 for t in per_test if t["exit_code"] == 0)
    failed_tests = sum(1 for t in per_test if t["exit_code"] == 1)
    refused_tests = sum(1 for t in per_test if t["exit_code"] == 2)

    # Suite exit = the WORST test outcome under the suite policy (refuse=2 >
    # fail=1 > pass=0), raised to >=1 by any SIMULATOR_INVALID run.
    exit_code = max((t["exit_code"] for t in per_test), default=0)
    if total_invalid and exit_code == 0:
        exit_code = 1

    if registry is not None:
        _register_results(
            registry, workspace, suite_doc=suite_doc, agent_id=agent_id,
            release_id=release_id, per_test=per_test,
            created_at_epoch=created_at_epoch,
        )

    return {
        "kind": KIND,
        "version": VERSION,
        "suite_id": suite_doc["suite_id"],
        "name": suite_doc.get("name"),
        "agent": agent_id,
        "release_id": release_id,
        "workspace": workspace,
        "inconclusive_policy": policy,
        "required_for_release": bool(suite_doc.get("required_for_release", False)),
        "origin": "simulated",
        "counts": {
            "tests": len(per_test),
            "runs": total_runs,
            "valid": total_valid,
            "simulator_invalid": len(total_invalid),
            "passed_tests": passed_tests,
            "failed_tests": failed_tests,
            "refused_tests": refused_tests,
        },
        "dimensions": dim_counts,
        "reliability": reliability,
        "reliability_note": (
            "pass@1 / pass@k / pass^k over every VALID simulated run in the "
            "suite (origin=simulated; a simulator's replay reliability is never "
            "production reliability). SIMULATOR_INVALID runs are excluded "
            "(bucketed as broken fixtures, never an agent PASS/FAIL). Never "
            "blended into any dimension; there is no overall_score."
        ),
        "tests": per_test,
        "simulator_invalid": total_invalid,
        "exit_code": exit_code,
    }


# =========================================================================
# rendering (deterministic given the envelope -> byte-reproducible reports)
# =========================================================================

def _dim_counts_line(b: Optional[Dict[str, int]]) -> str:
    b = b or {"pass": 0, "fail": 0, "inconclusive": 0}
    return f"{b['pass']} pass / {b['fail']} fail / {b['inconclusive']} inconclusive"


def render_summary_text(result: Dict[str, Any]) -> str:
    """The ``hotato suite run`` per-dimension summary: the suite verdict, each
    test's status, the suite's per-dimension counts (never blended), and the
    reliability aggregate (origin=simulated). Deterministic given the envelope."""
    c = result["counts"]
    lines = [
        f"hotato suite run: {result['suite_id']} ({result.get('name') or ''}) "
        f"-- agent {result['agent']}, release {result['release_id']} "
        f"-- exit_code={result['exit_code']}",
        f"inconclusive_policy: {result['inconclusive_policy']}  "
        f"required_for_release: {result['required_for_release']}",
        f"tests: {c['tests']}  ({c['passed_tests']} pass, {c['failed_tests']} fail, "
        f"{c['refused_tests']} refuse)",
        f"runs: {c['runs']} ({c['valid']} valid, {c['simulator_invalid']} "
        "SIMULATOR_INVALID -- broken fixtures, never an agent PASS/FAIL), "
        "origin=simulated",
    ]
    lines.append("per-dimension (grouped across tests; never blended):")
    for d in _DIMS:
        lines.append(f"  {d:<13} {_dim_counts_line(result['dimensions'][d])}")
    rel = result["reliability"]
    lines.append(
        f"reliability [origin=simulated]: pass@1={rel.get('pass_at_1', 0.0):.3f} "
        f"pass@k={rel.get('pass_at_k', 0.0):.3f} "
        f"pass^k={rel.get('pass_caret_k', 0.0):.3f} (n={rel.get('n', 0)})"
    )
    lines.append("per-test:")
    for t in result["tests"]:
        dims = " ".join(
            f"{d[:4]}={(t['dimensions'][d] or '-')[:4]}" for d in _DIMS
        )
        lines.append(
            f"  [{t['status']:<7}] {t['test_id']:<32} runs={t['counts'].get('runs', 0):<4} "
            f"{dims}"
        )
    if result["simulator_invalid"]:
        lines.append("SIMULATOR_INVALID (broken fixtures, never agent PASS/FAIL):")
        for inv in result["simulator_invalid"]:
            lines.append(f"  {inv.get('test_id')}/{inv['run_id']}: {inv['reason']}")
    return "\n".join(lines) + "\n"


def render_report_md(result: Dict[str, Any]) -> str:
    """A deterministic Markdown suite report (byte-reproducible given the
    envelope): per-dimension counts + reliability + per-test table. No blended
    score anywhere."""
    c = result["counts"]
    out = [
        f"# Suite report: {result['suite_id']}",
        "",
        f"- Agent: `{result['agent']}`",
        f"- Release: `{result['release_id']}`",
        f"- inconclusive_policy: `{result['inconclusive_policy']}` "
        f"(required_for_release: {result['required_for_release']})",
        "- Origin: **simulated** (a scripted-caller simulation; a simulator's "
        "replay reliability is never production reliability)",
        f"- Exit code: **{result['exit_code']}**",
        "",
        f"Tests: {c['tests']} ({c['passed_tests']} pass, {c['failed_tests']} fail, "
        f"{c['refused_tests']} refuse). Runs: {c['runs']} "
        f"({c['valid']} valid, {c['simulator_invalid']} SIMULATOR_INVALID).",
        "",
        "## Per-dimension (grouped across tests; never blended)",
        "",
        "| Dimension | Pass | Fail | Inconclusive |",
        "| --- | --- | --- | --- |",
    ]
    for d in _DIMS:
        b = result["dimensions"][d]
        out.append(f"| {d} | {b['pass']} | {b['fail']} | {b['inconclusive']} |")
    rel = result["reliability"]
    out += [
        "",
        "## Reliability (its own dimension; never blended, no overall_score)",
        "",
        f"pass@1 = {rel.get('pass_at_1', 0.0):.3f} &nbsp; "
        f"pass@k = {rel.get('pass_at_k', 0.0):.3f} &nbsp; "
        f"pass^k = {rel.get('pass_caret_k', 0.0):.3f} &nbsp; (n = {rel.get('n', 0)})",
        "",
        "## Per-test",
        "",
        "| Test | Scenario | Status | Runs | Outcome | Policy | Conversation | Speech | Reliability |",
        "| --- | --- | --- | --- | --- | --- | --- | --- | --- |",
    ]
    for t in result["tests"]:
        dv = t["dimensions"]
        out.append(
            f"| {t['test_id']} | {t.get('scenario_id') or '-'} | {t['status']} | "
            f"{t['counts'].get('runs', 0)} | "
            + " | ".join((dv[d] or "-") for d in _DIMS) + " |"
        )
    out.append("")
    return "\n".join(out) + "\n"


def _esc(x: Any) -> str:
    return (str(x).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;"))


def render_report_html(result: Dict[str, Any]) -> str:
    """A deterministic, self-contained HTML suite report (byte-reproducible
    given the envelope). Per-dimension counts + reliability + per-test table; no
    blended score, simulated origin labelled."""
    c = result["counts"]
    rel = result["reliability"]
    rows = []
    for d in _DIMS:
        b = result["dimensions"][d]
        rows.append(f"<tr><td>{d}</td><td>{b['pass']}</td><td>{b['fail']}</td>"
                    f"<td>{b['inconclusive']}</td></tr>")
    test_rows = []
    for t in result["tests"]:
        dv = t["dimensions"]
        cells = "".join(f"<td>{_esc(dv[d] or '-')}</td>" for d in _DIMS)
        test_rows.append(
            f"<tr><td>{_esc(t['test_id'])}</td><td>{_esc(t.get('scenario_id') or '-')}</td>"
            f"<td class='st-{t['status']}'>{t['status']}</td>"
            f"<td>{t['counts'].get('runs', 0)}</td>{cells}</tr>"
        )
    return (
        "<section class=\"suite-report\">"
        "<style>.suite-report table{border-collapse:collapse}"
        ".suite-report td,.suite-report th{border:1px solid #ccc;padding:4px 8px}"
        ".st-fail{color:#b00}.st-refuse{color:#b00}.st-pass{color:#080}</style>"
        f"<h1>Suite report: {_esc(result['suite_id'])}</h1>"
        f"<p>Agent <code>{_esc(result['agent'])}</code>, release "
        f"<code>{_esc(result['release_id'])}</code>. inconclusive_policy "
        f"<code>{_esc(result['inconclusive_policy'])}</code>. Origin "
        f"<strong>simulated</strong>. Exit code <strong>{result['exit_code']}</strong>.</p>"
        f"<p>Tests: {c['tests']} ({c['passed_tests']} pass, {c['failed_tests']} fail, "
        f"{c['refused_tests']} refuse). Runs: {c['runs']} ({c['valid']} valid, "
        f"{c['simulator_invalid']} SIMULATOR_INVALID).</p>"
        "<h2>Per-dimension (grouped across tests; never blended)</h2>"
        "<table><thead><tr><th>Dimension</th><th>Pass</th><th>Fail</th>"
        "<th>Inconclusive</th></tr></thead><tbody>"
        + "".join(rows) +
        "</tbody></table>"
        "<h2>Reliability (its own dimension; no overall_score)</h2>"
        f"<p>pass@1 = {rel.get('pass_at_1', 0.0):.3f}, "
        f"pass@k = {rel.get('pass_at_k', 0.0):.3f}, "
        f"pass^k = {rel.get('pass_caret_k', 0.0):.3f} (n = {rel.get('n', 0)}).</p>"
        "<h2>Per-test</h2>"
        "<table><thead><tr><th>Test</th><th>Scenario</th><th>Status</th><th>Runs</th>"
        + "".join(f"<th>{d}</th>" for d in _DIMS) +
        "</tr></thead><tbody>"
        + "".join(test_rows) +
        "</tbody></table>"
        "</section>"
    )
