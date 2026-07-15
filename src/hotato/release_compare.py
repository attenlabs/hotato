"""``hotato release compare <releaseA> <releaseB>``: a digest-exact, per-dimension
comparison of two releases from the fleet registry (1.3 item 4 / GPT-Pro §12).

``releaseA`` is the BASELINE, ``releaseB`` the CANDIDATE. The comparison reads
the registry's Releases / Runs / Conversations / Evaluations and reports:

* per-dimension counts for each side + their delta (never a single blended delta
  score -- invariant 1: there is no combined number),
* NEW FAILURES (a scenario x dimension that PASSED on the baseline and FAILS on
  the candidate) and FIXED-SINCE (the reverse), diffed only where BOTH sides
  have a comparable result -- a scenario the baseline never ran is new coverage,
  not a regression,
* every per-scenario x dimension status CHANGE.

Honesty: when a side has no runs the comparison says so plainly and offers no
invented baseline (invariant 3, no fabricated verdict). The releases' pinned
digests are surfaced so the reader knows exactly which two snapshots were
compared (digest-exact, never a fuzzy name match).
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional

from .fleet.registry import DEFAULT_HOME, Registry
from .serve.data import _new_vs_fixed, _release_rollup

__all__ = [
    "KIND",
    "VERSION",
    "compare_releases",
    "render_text",
]

KIND = "hotato.release-compare"
VERSION = 1

_DIMS = ("outcome", "policy", "conversation", "speech", "reliability")


def _release_digests(reg: Registry, ws: str, release_id: str) -> Optional[Dict[str, Any]]:
    """The pinned content-address snapshot of a release (or ``None`` when the
    release id is not registered in this workspace). Digest-exact provenance:
    the reader sees exactly which prompt/model/tool/workflow snapshot each side
    was."""
    row = reg.get_release(ws, release_id)
    if row is None:
        return None
    row = dict(row)  # get_release returns a sqlite3.Row; normalize to a dict
    return {
        "release_id": row.get("release_id"),
        "agent_id": row.get("agent_id"),
        "model": row.get("model"),
        "prompt_digest": row.get("prompt_digest"),
        "tool_schema_digest": row.get("tool_schema_digest"),
        "workflow_digest": row.get("workflow_digest"),
        "provider_config_digest": row.get("provider_config_digest"),
    }


def _per_dimension(a_rollup: Dict[str, Any], b_rollup: Dict[str, Any]) -> Dict[str, Any]:
    """Per-dimension counts for both sides + the per-count delta. NEVER a single
    blended delta: each dimension keeps its own three counts, and the delta is
    per count (pass/fail/inconclusive), never merged."""
    out: Dict[str, Any] = {}
    a = a_rollup.get("dim_counts") or {}
    b = b_rollup.get("dim_counts") or {}
    for d in _DIMS:
        av = a.get(d) or {"PASS": 0, "FAIL": 0, "INCONCLUSIVE": 0}
        bv = b.get(d) or {"PASS": 0, "FAIL": 0, "INCONCLUSIVE": 0}
        out[d] = {
            "baseline": {k.lower(): av.get(k, 0) for k in ("PASS", "FAIL", "INCONCLUSIVE")},
            "candidate": {k.lower(): bv.get(k, 0) for k in ("PASS", "FAIL", "INCONCLUSIVE")},
            "delta": {
                k.lower(): bv.get(k, 0) - av.get(k, 0)
                for k in ("PASS", "FAIL", "INCONCLUSIVE")
            },
        }
    return out


def _scenario_changes(a_rollup: Dict[str, Any], b_rollup: Dict[str, Any]) -> List[Dict[str, Any]]:
    """Every per-(scenario, dimension) status change from baseline to candidate,
    diffed only where BOTH sides have a comparable result (an added/removed
    scenario is not a status change). Sorted for a stable, byte-reproducible
    output."""
    a = a_rollup.get("scenario_dim_status", {})
    b = b_rollup.get("scenario_dim_status", {})
    changes: List[Dict[str, Any]] = []
    for key in sorted(set(a) & set(b)):
        if a[key] != b[key]:
            scn, dim = key.split("\t", 1)
            changes.append({
                "scenario_id": scn, "dimension": dim,
                "baseline": a[key], "candidate": b[key],
            })
    return changes


def compare_releases(release_a: str, release_b: str, *,
                     registry_home: Optional[str] = None,
                     workspace: str = "default") -> Dict[str, Any]:
    """Compare two releases from the registry and return a per-dimension +
    per-scenario comparison envelope. ``release_a`` is the baseline,
    ``release_b`` the candidate. Never emits a single delta score. A side with no
    runs is stated directly (``*_present`` + ``*_has_runs``), with no aggregate
    substitute."""
    home = registry_home or DEFAULT_HOME
    reg = Registry(home)
    try:
        a_dig = _release_digests(reg, workspace, release_a)
        b_dig = _release_digests(reg, workspace, release_b)
        a_roll = _release_rollup(reg, workspace, release_a if a_dig else None)
        b_roll = _release_rollup(reg, workspace, release_b if b_dig else None)
        # _new_vs_fixed(current, previous): current=candidate, previous=baseline.
        diff = _new_vs_fixed(b_roll, a_roll)
        # Comparable = there is at least one (scenario x dimension) result present
        # in BOTH releases. `_new_vs_fixed`'s own `comparable` only means the
        # baseline is non-empty; a truthful compare needs both sides to share a
        # result, else there is nothing to diff (an empty side, or two disjoint
        # scenario sets).
        comparable = bool(set(a_roll.get("scenario_dim_status", {}))
                          & set(b_roll.get("scenario_dim_status", {})))
        return {
            "kind": KIND,
            "version": VERSION,
            "workspace": workspace,
            "baseline": {
                "release_id": release_a,
                "present": a_dig is not None,
                "has_runs": a_roll.get("runs", 0) > 0,
                "runs": a_roll.get("runs", 0),
                "conversations": a_roll.get("conversations", 0),
                "evaluations": a_roll.get("evaluations", 0),
                "release": a_dig,
            },
            "candidate": {
                "release_id": release_b,
                "present": b_dig is not None,
                "has_runs": b_roll.get("runs", 0) > 0,
                "runs": b_roll.get("runs", 0),
                "conversations": b_roll.get("conversations", 0),
                "evaluations": b_roll.get("evaluations", 0),
                "release": b_dig,
            },
            "comparable": comparable,
            "per_dimension": _per_dimension(a_roll, b_roll),
            "new_failures": diff["new_failures"],
            "fixed_since": diff["fixed"],
            "scenario_changes": _scenario_changes(a_roll, b_roll),
            "note": (
                "digest-exact per-dimension comparison from the registry; no "
                "single blended delta score. new_failures / fixed_since are "
                "diffed only where BOTH releases ran the same scenario x "
                "dimension (a scenario one side never ran is new coverage, not a "
                "regression). A side with no runs is stated plainly, never a "
                "fabricated baseline."
            ),
        }
    finally:
        reg.close()


def _empty_side_note(side: Dict[str, Any], label: str) -> Optional[str]:
    if not side["present"]:
        return f"  {label} release {side['release_id']!r}: NOT REGISTERED in this workspace."
    if not side["has_runs"]:
        return (f"  {label} release {side['release_id']!r}: registered but has NO "
                "runs -- nothing to compare on this side (honest empty state, "
                "never a fabricated baseline).")
    return None


def render_text(cmp: Dict[str, Any]) -> str:
    """A human-readable per-dimension comparison. Honest empty-state when a side
    has no runs; never a single delta score."""
    a, b = cmp["baseline"], cmp["candidate"]
    lines = [
        f"hotato release compare: baseline {a['release_id']} -> candidate "
        f"{b['release_id']}  (workspace {cmp['workspace']})",
    ]
    empties = [n for n in (_empty_side_note(a, "baseline"),
                           _empty_side_note(b, "candidate")) if n]
    if empties:
        lines.append("empty side(s) (nothing to compare there):")
        lines.extend(empties)
    lines.append(
        f"baseline runs={a['runs']} conv={a['conversations']} eval={a['evaluations']}  "
        f"candidate runs={b['runs']} conv={b['conversations']} eval={b['evaluations']}"
    )
    lines.append("per-dimension counts (baseline -> candidate, delta; never blended):")
    for d in _DIMS:
        pd = cmp["per_dimension"][d]
        base, cand, delta = pd["baseline"], pd["candidate"], pd["delta"]

        def _fmt(c):
            return f"{c['pass']}P/{c['fail']}F/{c['inconclusive']}I"

        def _sd(x):
            return f"+{x}" if x > 0 else str(x)

        lines.append(
            f"  {d:<13} {_fmt(base)}  ->  {_fmt(cand)}   "
            f"(dP {_sd(delta['pass'])}, dF {_sd(delta['fail'])}, "
            f"dI {_sd(delta['inconclusive'])})"
        )
    if not cmp["comparable"]:
        lines.append(
            "no comparable (scenario x dimension) results across both releases: "
            "new-failures / fixed-since need a scenario BOTH releases ran."
        )
    else:
        nf = cmp["new_failures"]
        fx = cmp["fixed_since"]
        lines.append(f"new failures (PASS on baseline -> FAIL on candidate): {len(nf)}")
        for x in nf:
            lines.append(f"  - {x['scenario_id']} / {x['dimension']}")
        lines.append(f"fixed since (FAIL on baseline -> PASS on candidate): {len(fx)}")
        for x in fx:
            lines.append(f"  + {x['scenario_id']} / {x['dimension']}")
        changes = cmp["scenario_changes"]
        if changes:
            lines.append("all per-scenario status changes:")
            for x in changes:
                lines.append(
                    f"  ~ {x['scenario_id']} / {x['dimension']}: "
                    f"{x['baseline']} -> {x['candidate']}"
                )
    lines.append(f"  {cmp['note']}")
    return "\n".join(lines) + "\n"
