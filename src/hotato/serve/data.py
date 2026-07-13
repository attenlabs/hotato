"""Read-models for the five workspace views (the ``?format=json`` payloads).

Each ``build_*`` function opens NOTHING and mutates NOTHING -- it takes an
already-open :class:`~hotato.fleet.registry.Registry` (and, for the inspector, an
:class:`~hotato.fleet.store.ArtifactStore`) and returns a plain, JSON-serialisable
dict. That dict is BOTH the machine API response and the input the HTML renderer
formats, so the JSON mirror and the page can never drift (agent-first, GOAL §6).

Honesty invariants are enforced HERE, at the data layer, so no renderer can
violate them:

* **No blended score** (invariant 1): every rollup is per-dimension counts only;
  nothing here computes a single composite quality number.
* **origin real|simulated is never merged** (invariant 5): every aggregate that
  spans conversations keeps ``real`` and ``simulated`` in separate buckets.
* **INCONCLUSIVE is first-class** (invariant 3): a dimension with inconclusive
  evaluations and no fail reports ``INCONCLUSIVE``, never a smoothed ``PASS``.
* **Sparse data is flagged, never smoothed** (GOAL §6): a small sample carries a
  ``low sample, N=k`` marker instead of a confident-looking rollup.
* **deterministic vs model-judged stay in separate lanes** (invariant 2): the
  inspector keeps assertion_runs' ``deterministic`` flag verbatim.

Reads use a large ``limit`` because a single-node workspace holds one team's
data; the registry's default ``limit=50`` is a CLI-ergonomics default, not a
correctness bound for an aggregate.
"""
from __future__ import annotations

import json
from ..errors import open_regular as _open_regular
import os
import re
from collections import Counter, OrderedDict
from typing import Any, Dict, List, Optional

from ..failure_record import LANES as _FR_LANES
from ..failure_record import KIND as _FR_KIND
from ..failure_record import validate_record as _validate_record
from ..fleet.registry import Registry
from ..fleet.trend import _day as _utc_day  # reuse the exact UTC-day bucketing

__all__ = [
    "DIMENSIONS",
    "STATUSES",
    "ORIGINS",
    "store_root_for",
    "records_root_for",
    "build_release_readiness",
    "build_scenario_matrix",
    "build_conversation_inspector",
    "build_failure_clusters",
    "build_production_health",
    "build_records_list",
    "build_record_detail",
]

# The five conversation-QA dimensions (GPT Pro §2; never blended into one score).
DIMENSIONS = ("outcome", "policy", "conversation", "speech", "reliability")
# The three honest verdicts (invariant 3: INCONCLUSIVE is a real state).
STATUSES = ("PASS", "FAIL", "INCONCLUSIVE")
# The permanently-separate synthetic/real axis (invariant 5).
ORIGINS = ("real", "simulated")

# A very large read bound: one team, one node -> pull the whole workspace for an
# aggregate rather than silently truncating at the CLI default of 50.
_ALL = 1_000_000
# Below this many conversations a rollup is flagged "low sample" rather than
# presented as a confident number (GOAL §6: sparse data flagged, never smoothed).
_SPARSE_N = 5


def store_root_for(home: str) -> str:
    """The content-addressed artifact store root for a registry ``home`` --
    ``<home>/artifacts``, mirroring :class:`hotato.fleet.api.FleetAPI`. The
    inspector resolves conversation evidence (manifest + transcript + trace)
    from here by digest."""
    import os

    return os.path.join(home, "artifacts")


# =========================================================================
# small shared helpers
# =========================================================================

def _row(x) -> Optional[dict]:
    """A ``get_*`` single row (``sqlite3.Row`` | ``None``) as a plain dict."""
    return dict(x) if x is not None else None


def _empty_dim_counts() -> "OrderedDict[str, Dict[str, int]]":
    return OrderedDict((d, {"PASS": 0, "FAIL": 0, "INCONCLUSIVE": 0}) for d in DIMENSIONS)


def _aggregate_status(counts: Dict[str, int]) -> Optional[str]:
    """Collapse per-status counts for ONE dimension to a single honest verdict:
    any FAIL -> FAIL; else any PASS with zero INCONCLUSIVE -> PASS; else any
    INCONCLUSIVE -> INCONCLUSIVE; else ``None`` (no evaluation on this
    dimension). Never upgrades an inconclusive dimension to PASS."""
    if counts.get("FAIL", 0) > 0:
        return "FAIL"
    if counts.get("PASS", 0) > 0 and counts.get("INCONCLUSIVE", 0) == 0:
        return "PASS"
    if counts.get("INCONCLUSIVE", 0) > 0:
        return "INCONCLUSIVE"
    return None


def _worst(statuses: List[Optional[str]]) -> Optional[str]:
    """The most severe status across dimensions, for a single cell headline
    (FAIL > INCONCLUSIVE > PASS > none). This is a per-cell worst-case, NOT a
    blended score -- it does not average or weight dimensions."""
    order = {"FAIL": 3, "INCONCLUSIVE": 2, "PASS": 1}
    best = None
    best_rank = 0
    for s in statuses:
        r = order.get(s or "", 0)
        if r > best_rank:
            best_rank, best = r, s
    return best


def _evals_by_dimension(evals: List[dict]) -> "OrderedDict[str, Dict[str, int]]":
    counts = _empty_dim_counts()
    for e in evals:
        d = e.get("dimension")
        s = e.get("status")
        if d in counts and s in ("PASS", "FAIL", "INCONCLUSIVE"):
            counts[d][s] += 1
    return counts


def _run_ids(runs: List[dict]) -> List[str]:
    return [r["run_id"] for r in runs if r.get("run_id")]


def _conversations_for_runs(reg: Registry, ws: str, runs: List[dict]) -> List[dict]:
    convs: List[dict] = []
    for rid in _run_ids(runs):
        convs.extend(reg.list_conversations(ws, run_id=rid, limit=_ALL))
    return convs


def _evals_for_conversations(reg: Registry, ws: str, convs: List[dict]) -> List[dict]:
    evals: List[dict] = []
    for c in convs:
        cid = c.get("conversation_id")
        if cid:
            evals.extend(reg.list_evaluations(ws, conversation_id=cid, limit=_ALL))
    return evals


def _origin_split(convs: List[dict]) -> "OrderedDict[str, int]":
    """Conversation counts per origin, real and simulated ALWAYS separate; any
    other/absent origin lands in its own bucket rather than being folded into
    real (invariant 5: never conflate synthetic with real)."""
    split: "OrderedDict[str, int]" = OrderedDict((o, 0) for o in ORIGINS)
    for c in convs:
        o = c.get("origin") or "unspecified"
        split[o] = split.get(o, 0) + 1
    return split


def _run_passes(reg: Registry, ws: str, run: dict) -> Optional[bool]:
    """Did a single run (one repetition) pass? A run passes only when it has at
    least one evaluation and EVERY evaluation is PASS -- an INCONCLUSIVE or FAIL
    makes it not-a-pass (invariant 3). Returns ``None`` when the run has no
    evaluation at all (unknown, not counted as pass or fail)."""
    convs = reg.list_conversations(ws, run_id=run["run_id"], limit=_ALL)
    evals = _evals_for_conversations(reg, ws, convs)
    if not evals:
        return None
    return all(e.get("status") == "PASS" for e in evals)


# =========================================================================
# View 1 -- Release readiness (home)
# =========================================================================

def _release_rollup(reg: Registry, ws: str, release_id: Optional[str]) -> dict:
    """Per-release rollup from suites/runs/conversations/evaluations: scenario +
    run counts, per-dimension PASS/FAIL/INCONCLUSIVE, inconclusive total, origin
    split (never merged), and a per-(scenario,dimension) status map used to diff
    against the previous release. Sparse data is flagged, not smoothed."""
    if not release_id:
        return {
            "release_id": None, "runs": 0, "scenarios": 0, "conversations": 0,
            "evaluations": 0, "dim_counts": _empty_dim_counts(),
            "failures_by_dimension": {d: 0 for d in DIMENSIONS},
            "inconclusive_total": 0, "origin_split": _origin_split([]),
            "sparse": False, "scenario_dim_status": {},
        }
    runs = reg.list_runs(ws, release_id=release_id, limit=_ALL)
    run_scn = {r["run_id"]: r.get("scenario_id") for r in runs}
    scenario_ids = sorted({s for s in run_scn.values() if s})
    convs = _conversations_for_runs(reg, ws, runs)
    conv_run = {c["conversation_id"]: c.get("run_id") for c in convs}
    evals = _evals_for_conversations(reg, ws, convs)
    dim_counts = _evals_by_dimension(evals)

    # per-(scenario, dimension) aggregate: conv -> run -> scenario
    per_key: Dict[tuple, Dict[str, int]] = {}
    for e in evals:
        cid = e.get("conversation_id")  # present on every evaluations row
        rid = conv_run.get(cid)
        scn = run_scn.get(rid)
        d = e.get("dimension")
        if not scn or d not in DIMENSIONS:
            continue
        key = (scn, d)
        bucket = per_key.setdefault(key, {"PASS": 0, "FAIL": 0, "INCONCLUSIVE": 0})
        if e.get("status") in bucket:
            bucket[e["status"]] += 1
    scenario_dim_status = {
        f"{scn}\t{d}": _aggregate_status(c) for (scn, d), c in per_key.items()
    }

    return {
        "release_id": release_id,
        "runs": len(runs),
        "scenarios": len(scenario_ids),
        "scenario_ids": scenario_ids,
        "conversations": len(convs),
        "evaluations": len(evals),
        "dim_counts": dim_counts,
        "failures_by_dimension": {d: dim_counts[d]["FAIL"] for d in DIMENSIONS},
        "inconclusive_total": sum(dim_counts[d]["INCONCLUSIVE"] for d in DIMENSIONS),
        "origin_split": _origin_split(convs),
        "sparse": 0 < len(convs) < _SPARSE_N,
        "sample_n": len(convs),
        "scenario_dim_status": scenario_dim_status,
    }


def _new_vs_fixed(current: dict, previous: dict) -> dict:
    """Diff the current vs previous release at (scenario, dimension) granularity.
    Only keys present in BOTH releases are diffed -- a scenario the previous
    release never ran is new coverage, not a regression, and is reported
    separately. Honest: no invented baseline."""
    cur = current.get("scenario_dim_status", {})
    prev = previous.get("scenario_dim_status", {})
    new_failures, fixed = [], []
    for key, cur_status in cur.items():
        prev_status = prev.get(key)
        if prev_status is None:
            continue  # no comparable prior result
        scn, dim = key.split("\t", 1)
        if cur_status == "FAIL" and prev_status == "PASS":
            new_failures.append({"scenario_id": scn, "dimension": dim})
        elif cur_status == "PASS" and prev_status == "FAIL":
            fixed.append({"scenario_id": scn, "dimension": dim})
    new_failures.sort(key=lambda x: (x["scenario_id"], x["dimension"]))
    fixed.sort(key=lambda x: (x["scenario_id"], x["dimension"]))
    return {"new_failures": new_failures, "fixed": fixed,
            "comparable": bool(prev)}


def build_release_readiness(reg: Registry, ws: str) -> dict:
    """The pre-ship home screen: the current release's rollup, required-suite
    completeness, failures by dimension, inconclusive count, and new-vs-fixed
    against the previous release. Everything per-dimension; nothing blended."""
    releases = reg.list_releases(ws, limit=_ALL)  # DESC created_at
    current = releases[0] if releases else None
    previous = releases[1] if len(releases) > 1 else None
    cur_id = current["release_id"] if current else None
    prev_id = previous["release_id"] if previous else None

    cur_rollup = _release_rollup(reg, ws, cur_id)
    prev_rollup = _release_rollup(reg, ws, prev_id)
    diff = _new_vs_fixed(cur_rollup, prev_rollup)

    # required-suite completeness for the current release
    suites = reg.list_suites(ws, limit=_ALL)
    required = [s for s in suites if s.get("required_for_release")]
    cur_scn = set(cur_rollup.get("scenario_ids", []))
    required_status = []
    for s in required:
        scns = reg.list_scenarios(ws, suite_id=s["suite_id"], limit=_ALL)
        total = len(scns)
        covered = sum(1 for sc in scns if sc["scenario_id"] in cur_scn)
        required_status.append({
            "suite_id": s["suite_id"],
            "name": s.get("name"),
            "inconclusive_policy": s.get("inconclusive_policy"),
            "scenarios": total,
            "covered": covered,
            "complete": total > 0 and covered == total,
        })
    all_required_complete = bool(required_status) and all(
        r["complete"] for r in required_status)

    return {
        "view": "release_readiness",
        "workspace": ws,
        "releases_total": len(releases),
        "current_release": _row(current),
        "previous_release": _row(previous),
        "current": cur_rollup,
        "previous": prev_rollup,
        "required_suites": required_status,
        "all_required_complete": all_required_complete,
        "new_failures": diff["new_failures"],
        "fixed": diff["fixed"],
        "comparable_to_previous": diff["comparable"],
    }


# =========================================================================
# View 2 -- Scenario matrix
# =========================================================================

def _scenario_release_cell(reg: Registry, ws: str, scenario_id: str,
                           release_id: Optional[str]) -> dict:
    """One (scenario x release) cell: per-dimension status + reliability
    (pass^k where repetitions exist). Reliability is the fraction of runs that
    fully passed -- an operational reliability measure, NOT a quality score."""
    if not release_id:
        return {"release_id": None, "reps": 0, "per_dim": {}, "agents": [],
                "aggregate": None, "reliability": None, "sparse": False}
    runs = reg.list_runs(ws, scenario_id=scenario_id, release_id=release_id, limit=_ALL)
    reps = len(runs)
    convs = _conversations_for_runs(reg, ws, runs)
    evals = _evals_for_conversations(reg, ws, convs)
    dim_counts = _evals_by_dimension(evals)
    per_dim = {d: _aggregate_status(dim_counts[d]) for d in DIMENSIONS}
    agents = sorted({c.get("agent_id") for c in convs if c.get("agent_id")})

    reliability = None
    if reps > 0:
        verdicts = [_run_passes(reg, ws, r) for r in runs]
        scored = [v for v in verdicts if v is not None]
        passed = sum(1 for v in scored if v)
        reliability = {
            "reps": reps,
            "scored": len(scored),
            "passed": passed,
            # pass^k in the operational sense: all k scored repetitions passed
            "pass_all": len(scored) > 0 and passed == len(scored),
            "rate": (passed / len(scored)) if scored else None,
        }
    return {
        "release_id": release_id,
        "reps": reps,
        "dim_counts": dim_counts,
        "per_dim": per_dim,
        "agents": agents,
        "aggregate": _worst([per_dim[d] for d in DIMENSIONS]),
        "reliability": reliability,
        "sparse": 0 < reps < 2,  # a single run cannot show reliability
    }


def build_scenario_matrix(reg: Registry, ws: str, *, agent: Optional[str] = None,
                          release: Optional[str] = None, suite: Optional[str] = None,
                          status: Optional[str] = None) -> dict:
    """Rows = scenarios, cols = current + previous release, per-dimension status
    and reliability. Filterable by agent / release / suite / status via query
    params. Nothing is blended across dimensions."""
    releases = reg.list_releases(ws, limit=_ALL)  # DESC created_at
    rel_ids = [r["release_id"] for r in releases]
    cur_id = release or (rel_ids[0] if rel_ids else None)
    # previous = the release created immediately before the current one
    prev_id = None
    if cur_id in rel_ids:
        i = rel_ids.index(cur_id)
        prev_id = rel_ids[i + 1] if i + 1 < len(rel_ids) else None

    scenarios = reg.list_scenarios(ws, suite_id=suite or None, limit=_ALL)
    want_status = status.upper() if status else None
    rows = []
    for scn in scenarios:
        sid = scn["scenario_id"]
        cur = _scenario_release_cell(reg, ws, sid, cur_id)
        prev = _scenario_release_cell(reg, ws, sid, prev_id)
        if agent and agent not in cur["agents"]:
            continue
        if want_status and cur["aggregate"] != want_status:
            continue
        rows.append({
            "scenario_id": sid,
            "suite_id": scn.get("suite_id"),
            "goal": scn.get("goal"),
            "current": cur,
            "previous": prev,
        })

    return {
        "view": "scenario_matrix",
        "workspace": ws,
        "current_release": cur_id,
        "previous_release": prev_id,
        "releases": rel_ids,
        "filters": {"agent": agent, "release": release, "suite": suite,
                    "status": want_status},
        "rows": rows,
        "row_count": len(rows),
    }


# =========================================================================
# View 3 -- Conversation inspector
# =========================================================================

def _looks_like_digest(s: Any) -> bool:
    return isinstance(s, str) and len(s) == 64 and all(
        c in "0123456789abcdef" for c in s)


def _parse_trace_bytes(data: bytes) -> Any:
    """Parse a trace child that may be a single JSON object OR JSONL (one span
    per line, ``voice_trace.jsonl``). Returns the parsed object/list; the
    renderer normalises + redacts."""
    text = data.decode("utf-8", "replace").strip()
    if not text:
        return []
    try:
        return json.loads(text)
    except ValueError:
        spans = []
        for line in text.splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                spans.append(json.loads(line))
            except ValueError:
                continue
        return spans


def build_conversation_inspector(reg: Registry, ws: str, conversation_id: str,
                                 store=None) -> Optional[dict]:
    """One conversation = its manifest (origin + provenance + digests),
    transcript, trace spans (redaction respected downstream), per-dimension
    evaluations with rationale/citations, and reviewer decisions -- every number
    linking back to its source artifact digest (drill-to-evidence). Returns
    ``None`` when the conversation id is unknown (renders 404)."""
    conv = _row(reg.get_conversation(ws, conversation_id))
    if conv is None:
        return None
    run = _row(reg.get_run(ws, conv["run_id"])) if conv.get("run_id") else None
    scenario = _row(reg.get_scenario(ws, run["scenario_id"])) if run and run.get(
        "scenario_id") else None
    release = _row(reg.get_release(ws, run["release_id"])) if run and run.get(
        "release_id") else None

    # evaluations (per dimension) + their reviews
    evals = reg.list_evaluations(ws, conversation_id=conversation_id, limit=_ALL)
    eval_blocks = []
    for e in evals:
        reviews = reg.list_reviews(ws, evaluation_id=e["evaluation_id"], limit=_ALL)
        refs = _parse_json_field(e.get("evidence_refs"))
        eval_blocks.append({
            "evaluation_id": e["evaluation_id"],
            "evaluator_id": e.get("evaluator_id"),
            "dimension": e.get("dimension"),
            "status": e.get("status"),
            "evidence_refs": refs,
            "provenance": _parse_json_field(e.get("provenance")),
            "reviews": [dict(r) for r in reviews],
        })
    # assertion_runs carry the deterministic-vs-model lane + kind + reason
    araw = reg.list_assertion_runs(ws, conversation_id=conversation_id, limit=_ALL)
    assertion_runs = []
    for a in araw:
        assertion_runs.append({
            "assertion_run_id": a.get("assertion_run_id"),
            "assertion_id": a.get("assertion_id"),
            "kind": a.get("kind"),
            "dimension": a.get("dimension"),
            "deterministic": bool(a.get("deterministic")),
            "status": a.get("status"),
            "reason": a.get("reason"),
            "evidence_refs": _parse_json_field(a.get("evidence_refs")),
        })

    # resolve the conversation artifact (manifest + transcript + trace) by digest
    manifest = None
    transcript = None
    trace = None
    evidence_status = "none"
    digest = conv.get("artifact_digest")
    if digest and store is not None and _looks_like_digest(digest) and store.has(digest):
        try:
            manifest = store.get_json(digest)
        except Exception:
            manifest = None
        if isinstance(manifest, dict):
            evidence_status = "resolved"
            children = manifest.get("artifacts") or {}
            for name, ref in children.items():
                csha = (ref or {}).get("sha256")
                if not (_looks_like_digest(csha) and store.has(csha)):
                    continue
                try:
                    raw = store.get_bytes(csha)
                except Exception:
                    continue
                if name == "transcript":
                    transcript = _redact_transcript(
                        _parse_json_field(raw.decode("utf-8", "replace")))
                elif name == "trace":
                    trace = _redact_trace(_parse_trace_bytes(raw))
    elif digest:
        evidence_status = "unresolved"  # a digest is bound but not in this store

    return {
        "view": "conversation_inspector",
        "workspace": ws,
        "conversation": conv,
        "run": run,
        "scenario": scenario,
        "release": release,
        "origin": conv.get("origin"),
        "artifact_digest": digest,
        "evidence_status": evidence_status,
        "manifest": manifest if isinstance(manifest, dict) else None,
        "transcript": transcript,
        "trace": trace,
        "evaluations": eval_blocks,
        "assertion_runs": assertion_runs,
    }


_REDACTED = "[redacted]"
# Content-bearing fields a redacted span/segment might carry; all are scrubbed so
# redacted text can never reach the JSON mirror OR the HTML.
_SPAN_TEXT_FIELDS = ("text", "detail", "summary", "arguments", "result", "value")


def _redact_transcript(transcript: Any) -> Any:
    """Scrub the text of any transcript segment flagged ``redacted`` /
    ``text_redacted`` AT THE DATA LAYER, so the ``?format=json`` mirror is as safe
    as the HTML (redacted content never leaves the box in either surface)."""
    if isinstance(transcript, dict):
        out = dict(transcript)
        for key in ("segments", "utterances", "turns"):
            if isinstance(out.get(key), list):
                out[key] = [_redact_segment(s) for s in out[key]]
        return out
    if isinstance(transcript, list):
        return [_redact_segment(s) for s in transcript]
    return transcript


def _redact_segment(seg: Any) -> Any:
    if not isinstance(seg, dict):
        return seg
    if seg.get("redacted") or seg.get("text_redacted"):
        seg = dict(seg)
        if "text" in seg:
            seg["text"] = _REDACTED
    return seg


def _redact_trace(trace: Any) -> Any:
    """Scrub the content fields of any trace span flagged ``text_redacted`` at the
    data layer (invariant-preserving: the flag stays so the UI still labels it)."""
    if isinstance(trace, dict):
        out = dict(trace)
        for key in ("spans", "events"):
            if isinstance(out.get(key), list):
                out[key] = [_redact_span(s) for s in out[key]]
        return out
    if isinstance(trace, list):
        return [_redact_span(s) for s in trace]
    return trace


def _redact_span(sp: Any) -> Any:
    if not isinstance(sp, dict):
        return sp
    if sp.get("text_redacted"):
        sp = dict(sp)
        for f in _SPAN_TEXT_FIELDS:
            if f in sp:
                sp[f] = _REDACTED
    return sp


def _parse_json_field(v: Any) -> Any:
    if v in (None, ""):
        return None
    if isinstance(v, (dict, list)):
        return v
    try:
        return json.loads(v)
    except (ValueError, TypeError):
        return v  # opaque string; render as-is


# =========================================================================
# View 4 -- Failure clusters (by observable signature)
# =========================================================================

_ID_TOKEN = re.compile(r"\b(?:[0-9a-f]{8,}|[a-z]+[-_]?\d+|\d+)\b")
_WS = re.compile(r"\s+")


def _reason_class(reason: Optional[str]) -> str:
    """Normalise a failure reason to a short OBSERVABLE class: lowercase, strip
    concrete ids/digits/hex so ``cancel_appointment (appt_772) not called`` and
    ``... (appt_913) not called`` collapse to the same signature, then keep the
    leading words. This is a signature of what was OBSERVED, deliberately NOT a
    claimed root cause."""
    if not reason:
        return "unspecified"
    s = _ID_TOKEN.sub("#", reason.lower())
    s = _WS.sub(" ", s).strip()
    words = s.split(" ")
    return " ".join(words[:8]) or "unspecified"


def build_failure_clusters(reg: Registry, ws: str, *, dimension: Optional[str] = None,
                           kind: Optional[str] = None) -> dict:
    """Group FAILED evaluations + assertion_runs by observable failure signature
    = (dimension, assertion kind, reason-class), with counts and drill-through
    member lists. Labelled 'clusters by observable signature' -- never 'root
    cause' (GPT Pro §10 view 4)."""
    clusters: "OrderedDict[str, dict]" = OrderedDict()

    def _add(sig_dim, sig_kind, lane, reason_cls, member):
        key = f"{sig_dim}\t{sig_kind}\t{lane}\t{reason_cls}"
        c = clusters.get(key)
        if c is None:
            c = {"dimension": sig_dim, "kind": sig_kind, "lane": lane,
                 "reason_class": reason_cls, "count": 0, "members": []}
            clusters[key] = c
        c["count"] += 1
        c["members"].append(member)

    # failed assertion_runs: carry kind + reason + the deterministic lane
    for a in reg.list_assertion_runs(ws, dimension=dimension or None, limit=_ALL):
        if a.get("status") != "FAIL":
            continue
        if kind and a.get("kind") != kind:
            continue
        lane = "deterministic" if a.get("deterministic") else "model-judged"
        _add(a.get("dimension") or "unspecified", a.get("kind") or "assertion",
             lane, _reason_class(a.get("reason")), {
                 "conversation_id": a.get("conversation_id"),
                 "call_id": a.get("call_id"),
                 "assertion_id": a.get("assertion_id"),
                 "reason": a.get("reason"),
             })

    # failed evaluations without a per-assertion kind: cluster under "evaluation"
    if not kind or kind == "evaluation":
        for e in reg.list_evaluations(ws, dimension=dimension or None, limit=_ALL):
            if e.get("status") != "FAIL":
                continue
            prov = _parse_json_field(e.get("provenance"))
            reason = None
            if isinstance(prov, dict):
                reason = prov.get("reason") or prov.get("note")
            _add(e.get("dimension") or "unspecified", "evaluation",
                 (e.get("evaluator_id") or "evaluation"),
                 _reason_class(reason), {
                     "conversation_id": e.get("conversation_id"),
                     "evaluation_id": e.get("evaluation_id"),
                     "evaluator_id": e.get("evaluator_id"),
                 })

    ordered = sorted(clusters.values(),
                     key=lambda c: (-c["count"], c["dimension"], c["kind"]))
    return {
        "view": "failure_clusters",
        "workspace": ws,
        "label": "clusters by observable signature",
        "filters": {"dimension": dimension, "kind": kind},
        "clusters": ordered,
        "cluster_count": len(ordered),
        "failure_total": sum(c["count"] for c in ordered),
    }


# =========================================================================
# View 5 -- Production health
# =========================================================================

def _health_series_for(reg: Registry, ws: str, convs: List[dict]) -> dict:
    """Per-dimension failure rate over time for one ORIGIN bucket. Buckets by the
    conversation's UTC day (reusing trend.py's ``_day``); emits NO point for a
    day with no evaluated conversation, and marks a dimension 'not enough
    history' when fewer than 2 days carry data (mirrors trend.py honesty)."""
    # day -> dimension -> {"fail": n, "total": n}
    by_day: "OrderedDict[str, Dict[str, Dict[str, int]]]" = OrderedDict()
    conv_day = {}
    for c in convs:
        conv_day[c["conversation_id"]] = _utc_day(c.get("created_at"))
    evals = _evals_for_conversations(reg, ws, convs)
    for e in evals:
        day = conv_day.get(e.get("conversation_id"))
        d = e.get("dimension")
        if day is None or d not in DIMENSIONS:
            continue
        slot = by_day.setdefault(day, {})
        cell = slot.setdefault(d, {"fail": 0, "total": 0})
        cell["total"] += 1
        if e.get("status") == "FAIL":
            cell["fail"] += 1

    days = sorted(by_day)
    series = {}
    for d in DIMENSIONS:
        rows = []
        for day in days:
            cell = by_day[day].get(d)
            if not cell or cell["total"] == 0:
                continue  # no evaluated sample that day -> no fabricated point
            rows.append({"day": day, "total": cell["total"], "fail": cell["fail"],
                         "rate": cell["fail"] / cell["total"]})
        if len(rows) < 2:
            series[d] = {"enough_history": False, "days_with_data": len(rows),
                         "points": rows}
        else:
            series[d] = {"enough_history": True, "days_with_data": len(rows),
                         "points": rows}
    return series


def build_production_health(reg: Registry, ws: str) -> dict:
    """Ingest counts, evaluated coverage, and per-dimension failure rates over
    time -- computed SEPARATELY for real and simulated conversations (invariant
    5: never merged), with trend.py's empty-day + not-enough-history honesty and
    NO blended quality number (GPT Pro §10 view 5)."""
    convs = reg.list_conversations(ws, limit=_ALL)
    # keep real / simulated (and any other origin) in separate buckets
    buckets: "OrderedDict[str, List[dict]]" = OrderedDict((o, []) for o in ORIGINS)
    for c in convs:
        buckets.setdefault(c.get("origin") or "unspecified", []).append(c)

    per_origin = OrderedDict()
    for origin, clist in buckets.items():
        evaluated = 0
        for c in clist:
            if reg.list_evaluations(ws, conversation_id=c["conversation_id"], limit=1):
                evaluated += 1
        per_origin[origin] = {
            "ingested": len(clist),
            "evaluated": evaluated,
            "coverage": (evaluated / len(clist)) if clist else None,
            "series": _health_series_for(reg, ws, clist) if clist else {},
        }

    releases = reg.list_releases(ws, limit=_ALL)
    markers = [{"release_id": r["release_id"], "day": _utc_day(r.get("created_at"))}
               for r in releases]
    days_present = sorted({_utc_day(c.get("created_at")) for c in convs
                           if c.get("created_at") is not None})

    return {
        "view": "production_health",
        "workspace": ws,
        "ingested_total": len(convs),
        "origins": per_origin,           # real / simulated separated
        "release_markers": markers,
        "days_of_history": len(days_present),
        "enough_history": len(days_present) >= 2,
    }


# =========================================================================
# View 6 -- Failure records (read-only viewer over hotato.failure-record.v1)
# =========================================================================

# A record is addressed in the URL by a single, safe path segment (the on-disk
# directory or file name it lives under). Anything outside this alphabet, a bare
# ``.``/``..``, or a name with a separator is REJECTED (never sanitised) so a
# hostile id cannot select a file outside the records root.
_RECORD_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
# A share-safe Failure Record is small JSON (five lanes + evidence refs, never a
# payload/audio body). Cap the read so a stray large blob is never parsed here.
_RECORD_MAX_BYTES = 2 * 1024 * 1024


def records_root_for(home: str) -> str:
    """The read-only Failure Record directory for a registry ``home`` --
    ``<home>/records``. Each record is either ``<root>/<id>/failure-record.json``
    (the ``hotato record render --out`` layout) or a flat ``<root>/<id>.json``.
    The server only reads from here; it never creates or writes it."""
    return os.path.join(home, "records")


def _contained(root_real: str, candidate: str) -> Optional[str]:
    """The real path of ``candidate`` iff it resolves to a regular file strictly
    inside ``root_real`` (symlinks resolved). Returns ``None`` on any escape --
    a traversal id or a symlink pointing outside the records root is refused,
    never followed."""
    real = os.path.realpath(candidate)
    if real != root_real and not real.startswith(root_real + os.sep):
        return None
    if not os.path.isfile(real):
        return None
    return real


def _read_valid_record(path: str) -> Optional[Dict[str, Any]]:
    """Load ONE ``hotato.failure-record.v1`` from ``path`` if it is a small,
    well-formed, VALID record; otherwise ``None``. Validation runs the canonical
    :func:`validate_record` oracle (content address, five separate lanes, no
    aggregate score, share-safe privacy, no absolute paths) with no ``root`` so a
    portable record is accepted without its evidence files present. Nothing that
    fails to validate is ever shown."""
    try:
        if os.path.getsize(path) > _RECORD_MAX_BYTES:
            return None
        with _open_regular(path, "r", encoding="utf-8") as fh:
            doc = json.load(fh)
    except (OSError, ValueError):
        return None
    if not isinstance(doc, dict) or doc.get("kind") != _FR_KIND:
        return None
    try:
        _validate_record(doc)
    except Exception:
        return None
    return doc


def _record_candidates(root: str, record_id: str) -> List[str]:
    return [
        os.path.join(root, record_id, "failure-record.json"),
        os.path.join(root, record_id + ".json"),
    ]


def _record_summary(record_id: str, doc: Dict[str, Any]) -> Dict[str, Any]:
    """The share-safe list row for one record: its URL-safe ref, content address,
    headline, subject, per-lane status (each separate, never blended), and the
    deterministic-gate/advisory statuses kept apart. Only fields the record
    already publishes -- no payload, no absolute path."""
    dims = doc.get("dimensions") or {}
    lane_status = OrderedDict(
        (lane, (dims.get(lane) or {}).get("status")) for lane in _FR_LANES)
    return {
        "record_id_ref": record_id,                     # URL routing id
        "record_id": doc.get("record_id"),              # content address
        "status": doc.get("status"),
        "headline": doc.get("headline"),
        "test_id": (doc.get("subject") or {}).get("test_id"),
        "origin": (doc.get("origin") or {}).get("kind"),
        "gate_status": (doc.get("gate") or {}).get("status"),
        "advisory_status": (doc.get("advisory") or {}).get("status"),
        "lane_status": lane_status,
    }


def build_records_list(home: str, ws: str) -> Dict[str, Any]:
    """List the validated Failure Records under ``<home>/records``. Reads the
    directory fresh on every call (read-only), skips anything that is not a
    small, valid ``hotato.failure-record.v1``, and never follows a symlink out of
    the root. An absent or empty directory yields an explicit empty list -- no
    record is ever fabricated."""
    root = records_root_for(home)
    records: List[Dict[str, Any]] = []
    if not os.path.isdir(root):
        return {"view": "failure_records", "workspace": ws,
                "records": records, "record_count": 0}
    root_real = os.path.realpath(root)
    try:
        names = sorted(os.listdir(root))
    except OSError:
        names = []
    seen: set = set()
    for name in names:
        full = os.path.join(root, name)
        if os.path.isdir(full):
            record_id = name
            path = os.path.join(full, "failure-record.json")
        elif name.endswith(".json"):
            record_id = name[:-5]
            path = full
        else:
            continue
        if record_id in seen or not _RECORD_ID_RE.match(record_id):
            continue
        real = _contained(root_real, path)
        if real is None:
            continue
        doc = _read_valid_record(real)
        if doc is None:
            continue
        seen.add(record_id)
        records.append(_record_summary(record_id, doc))
    records.sort(key=lambda r: r["record_id_ref"])
    return {"view": "failure_records", "workspace": ws,
            "records": records, "record_count": len(records)}


def build_record_detail(home: str, record_id: str) -> Optional[Dict[str, Any]]:
    """Resolve ONE Failure Record by its URL-safe ``record_id`` and return the
    validated canonical record dict, or ``None`` when the id is unsafe, escapes
    the records root, is absent, or fails validation. The returned dict is the
    canonical ``hotato.failure-record.v1`` -- the same object the JSON mirror and
    the HTML view both render (one record, one source of truth)."""
    if not _RECORD_ID_RE.match(record_id or ""):
        return None
    root = records_root_for(home)
    if not os.path.isdir(root):
        return None
    root_real = os.path.realpath(root)
    for candidate in _record_candidates(root, record_id):
        real = _contained(root_real, candidate)
        if real is None:
            continue
        doc = _read_valid_record(real)
        if doc is not None:
            return doc
    return None
