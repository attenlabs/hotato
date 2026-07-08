"""``hotato loop``: one-command orchestration of the closed fix loop, with memory.

The closed loop is: find a bad moment -> label it -> plan a fix -> apply it ->
prove it held. ``hotato loop`` drives the parts Hotato can drive and REMEMBERS
where it left off in a small local state file (``.hotato/loop-state.json`` in the
current directory by default), so a second run tells you what is waiting on YOU:

  * first run over a folder of calls: it runs discovery (``analyze`` -> ``scan``
    -> rank) and records the candidate moments. Stage: ``awaiting_label`` ->
    "you have N candidate moments awaiting your label".
  * you label the ones that matter with ``hotato fixture create`` (the loop
    NEVER labels for you: only a human supplies the yield/hold intent).
  * next run, with those labeled fixtures present: it runs them, ``diagnose``s
    the battery (including the threshold-funnel check), and ``plan``s a guarded
    fix. Stage: ``awaiting_verify`` -> "a fix plan is ready; apply it with hotato
    patch, then prove it with hotato verify".

What the loop does NOT do, by hard rule:

* it never AUTO-LABELS: the human supplies every yield/hold label via
  ``fixture create``. The loop only runs fixtures that already exist.
* it never AUTO-APPLIES: it produces a plan (and points at ``hotato patch``);
  applying a config change and running ``hotato verify`` stay human steps.
* it never mutates any platform and makes no network call of its own beyond what
  the discovery scan already does (which is offline).

The loop orchestrates and tracks state; the human keeps the two irreversible
decisions -- which moment is a real bug, and whether to apply the fix.
"""

from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from typing import Optional, Tuple

STATE_SCHEMA_ID = "hotato.loop-state.v1"

STAGES = ("awaiting_label", "awaiting_verify", "complete")


def default_state_path() -> str:
    """``.hotato/loop-state.json`` under the current directory (project-local,
    git-ignorable). Override with ``--state PATH``."""
    return os.path.join(os.getcwd(), ".hotato", "loop-state.json")


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def load_state(path: str) -> Optional[dict]:
    if not os.path.exists(path):
        return None
    try:
        with open(path, encoding="utf-8") as fh:
            obj = json.load(fh)
    except (OSError, ValueError) as exc:
        raise ValueError(
            f"loop state {path!r} is not readable JSON ({exc}). Fix or delete it "
            "and re-run hotato loop."
        ) from exc
    if not isinstance(obj, dict) or obj.get("schema") != STATE_SCHEMA_ID:
        raise ValueError(
            f"{path!r} is not a hotato loop-state file. Delete it and re-run."
        )
    return obj


def save_state(path: str, state: dict) -> None:
    d = os.path.dirname(path)
    if d:
        os.makedirs(d, exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(state, fh, indent=2)
        fh.write("\n")
    os.replace(tmp, path)


def _count_labeled_fixtures(fixtures_dir: Optional[str]) -> Tuple[int, Optional[str], Optional[str]]:
    """How many labeled scenarios live under ``fixtures_dir`` (the ``--out`` of
    ``hotato fixture create``: DIR/scenarios/*.json + DIR/audio/). Returns
    ``(count, scenarios_dir, audio_dir)``; count 0 when none/absent."""
    if not fixtures_dir:
        return 0, None, None
    scen = os.path.join(fixtures_dir, "scenarios")
    audio = os.path.join(fixtures_dir, "audio")
    if not os.path.isdir(scen):
        return 0, None, None
    n = sum(1 for name in os.listdir(scen) if name.endswith(".json"))
    return n, scen, audio


def _discover(folder: str, *, min_gap: float, top: int) -> dict:
    """Discovery leg: analyze the folder (scan + rank) into a candidate summary
    the human uses to decide what to label. Never a verdict."""
    from . import analyze as _analyze

    aggregate, _per_file = _analyze.analyze_folder(folder, min_gap_sec=min_gap)
    cands = aggregate.get("candidates", [])
    top_list = []
    for c in cands[: max(0, top)]:
        top_list.append({
            "source": c.get("source"),
            "onset_sec": c.get("t_sec"),
            "kind": c.get("kind"),
            "detail": _analyze._headline(c),
        })
    return {
        "total_candidates": aggregate.get("total_candidates", len(cands)),
        "calls_scanned": aggregate.get("calls_scanned"),
        "calls_skipped": aggregate.get("calls_skipped"),
        "top": top_list,
        "at": _now(),
    }


def _plan_fix(scenarios_dir: str, audio_dir: str, *, stack: str,
              state_dir: str) -> dict:
    """Planning leg: run the labeled fixtures -> diagnose (with the
    threshold-funnel check) -> build one guarded fix plan. Writes the plan JSON
    next to the state file and returns a summary. No apply, ever."""
    from . import diagnose as _diagnose
    from . import fixplan as _fixplan
    from .core import SUITE_ID, run_suite

    env = run_suite(
        suite=SUITE_ID,
        stack=stack,
        scenarios_dir=scenarios_dir,
        audio_dir=audio_dir,
    )
    diagnosis = _diagnose.diagnose_envelope(env, source=scenarios_dir)
    plan = _fixplan.build_plan(diagnosis=diagnosis, inspected=None, stack=stack)

    os.makedirs(state_dir, exist_ok=True)
    plan_path = os.path.join(state_dir, "loop-fixplan.json")
    with open(plan_path, "w", encoding="utf-8") as fh:
        json.dump(plan, fh, indent=2)
        fh.write("\n")

    battery = diagnosis["battery"]
    n_changes = len(plan.get("changes") or [])
    return {
        "ran_fixtures": battery["events"],
        "failing": battery["failed"],
        "decision": plan["decision"],
        "finding": plan["finding"],
        "fixes_awaiting_verify": n_changes,
        "plan_path": plan_path,
        "at": _now(),
    }


def _message(stage: str, state: dict) -> Tuple[str, list]:
    """The stage-appropriate human message and the concrete next commands."""
    if stage == "awaiting_label":
        n = state["discovery"]["total_candidates"]
        if n == 0:
            return (
                "no candidate turn-taking moments found in this folder yet.",
                ["drop more dual-channel call recordings in the folder and "
                 "re-run hotato loop, or point it at a folder that has calls"],
            )
        return (
            f"you have {n} candidate moment(s) awaiting your label.",
            [
                "review the candidates and label the ones that are real bugs "
                "(you supply the yield/hold intent; hotato never labels for you):",
                "  hotato fixture create --stereo <call>.wav --onset <sec> "
                "--expect yield|hold --id <slug> --out tests/hotato",
                "then re-run hotato loop --fixtures tests/hotato to plan a fix",
            ],
        )
    if stage == "awaiting_verify":
        p = state["planning"]
        if p["decision"] == "no_change":
            return ("all labeled fixtures pass; nothing to fix or verify.", [])
        if p["decision"] == "do_not_tune_single_threshold":
            return (
                "the battery fails on both axes at once: no single config "
                "threshold fixes it. hotato patch will print the "
                "engagement-control pointer, not a config patch.",
                [f"hotato patch {p['plan_path']}"],
            )
        return (
            f"a fix plan is ready ({p['fixes_awaiting_verify']} config step "
            f"awaiting verify, decision {p['decision']}). Apply it yourself, "
            "then prove it held.",
            [
                f"hotato patch {p['plan_path']}   # produces the paste-ready "
                "patch; you apply it",
                "re-capture the failing fixtures after applying the change",
                "hotato verify --before <old-run>.json --after <new-run>.json",
            ],
        )
    # complete
    return ("loop complete: the fix was verified across the battery.", [])


def run_loop(
    folder: Optional[str],
    *,
    fixtures_dir: Optional[str] = None,
    state_path: Optional[str] = None,
    rediscover: bool = False,
    stack: str = "generic",
    min_gap: float = 2.0,
    top: int = 10,
) -> Tuple[dict, int]:
    """Advance the loop one step and persist state. Deterministic transitions,
    driven by observable facts (does the fixtures dir have labeled scenarios
    yet?). Returns ``(result, exit_code)``. Never auto-labels, never
    auto-applies. Raises ValueError (exit 2) on unusable input."""
    state_path = state_path or default_state_path()
    state = load_state(state_path)
    state_dir = os.path.dirname(os.path.abspath(state_path))
    n_fixtures, scen_dir, audio_dir = _count_labeled_fixtures(fixtures_dir)

    if state is None:
        state = {
            "schema": STATE_SCHEMA_ID,
            "root": os.path.abspath(folder) if folder else None,
            "fixtures_dir": os.path.abspath(fixtures_dir) if fixtures_dir else None,
            "stage": None,
            "created_at": _now(),
            "updated_at": None,
            "run": 0,
            "discovery": None,
            "planning": None,
            "history": [],
        }

    state["run"] = state.get("run", 0) + 1
    if fixtures_dir:
        state["fixtures_dir"] = os.path.abspath(fixtures_dir)
    if folder:
        state["root"] = os.path.abspath(folder)

    prior_stage = state.get("stage")
    advanced = False

    # Transition rules, most-progressed first. The human's labeling is what
    # advances awaiting_label -> awaiting_verify; the loop only reacts to it.
    if n_fixtures > 0 and prior_stage in (None, "awaiting_label"):
        # The human has labeled fixtures: plan a guarded fix.
        state["planning"] = _plan_fix(
            scen_dir, audio_dir, stack=stack, state_dir=state_dir
        )
        state["stage"] = (
            "complete" if state["planning"]["decision"] == "no_change"
            else "awaiting_verify"
        )
        advanced = prior_stage != state["stage"]
    elif prior_stage is None or rediscover:
        # First run (or an explicit re-scan): discover candidate moments.
        if not folder:
            raise ValueError(
                "hotato loop needs a FOLDER of call recordings on the first run "
                "(discovery), e.g. hotato loop ./recordings"
            )
        state["discovery"] = _discover(folder, min_gap=min_gap, top=top)
        state["stage"] = "awaiting_label"
        advanced = prior_stage != state["stage"]
    else:
        # No new labels and no re-scan: re-report where we left off, from memory.
        if prior_stage == "awaiting_label" and n_fixtures == 0 and folder is None:
            raise ValueError(
                "hotato loop needs the folder it discovered, or --fixtures with "
                "labeled scenarios to advance. Re-pass the folder or label first."
            )

    stage = state["stage"]
    message, next_cmds = _message(stage, state)
    state["updated_at"] = _now()
    state["history"].append({
        "run": state["run"],
        "stage": stage,
        "advanced": advanced,
        "at": state["updated_at"],
        "note": message,
    })
    save_state(state_path, state)

    result = {
        "tool": "hotato",
        "kind": "loop",
        "schema_version": "1",
        "state_path": state_path,
        "run": state["run"],
        "stage": stage,
        "advanced": advanced,
        "message": message,
        "discovery": state.get("discovery"),
        "planning": state.get("planning"),
        "next": next_cmds,
        "guarantees": [
            "no auto-label: you supply every yield/hold intent",
            "no auto-apply: hotato produces the patch; you apply it",
        ],
    }
    return result, 0


def render_text(result: dict) -> str:
    lines = [
        f"hotato loop [run {result['run']}] stage={result['stage']}"
        + ("  (advanced)" if result["advanced"] else "  (unchanged)"),
        f"  {result['message']}",
    ]
    disc = result.get("discovery")
    if disc and result["stage"] == "awaiting_label":
        lines.append(
            f"  discovery: {disc['total_candidates']} candidate(s) across "
            f"{disc.get('calls_scanned')} call(s)"
        )
        for c in disc.get("top", [])[:5]:
            lines.append(
                f"    {c.get('source')} @ {c.get('onset_sec')}s "
                f"[{c.get('kind')}] {c.get('detail')}"
            )
    plan = result.get("planning")
    if plan and result["stage"] in ("awaiting_verify", "complete"):
        lines.append(
            f"  plan: ran {plan['ran_fixtures']} fixture(s), {plan['failing']} "
            f"failing, decision={plan['decision']}"
        )
        lines.append(f"    plan file: {plan['plan_path']}")
    if result.get("next"):
        lines.append("  next:")
        for cmd in result["next"]:
            lines.append(f"    - {cmd}")
    for g in result.get("guarantees", []):
        lines.append(f"  guarantee: {g}")
    lines.append(f"  state remembered at: {result['state_path']}")
    return "\n".join(lines)
