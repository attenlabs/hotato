"""Five-lane job-summary renderer for the root GitHub Action (stdlib only).

Reads ONE hotato machine result (a ``hotato.suite-run``, ``hotato.test-run``,
or ``contract-verify`` JSON document) and renders the Markdown job summary plus
the four-value status word (``pass`` / ``fail`` / ``inconclusive`` / ``error``).

The machine JSON stays primary; this module is presentation only. It renders
ONLY values present in the source document: a lane with no evaluated check is
``NOT_RUN``, a lane whose checks are missing required evidence is
``INCONCLUSIVE`` (never ``PASS``), and a missing or malformed result renders as
``ERROR`` with every lane ``NOT_RUN``. The exit code is owned by the
deterministic result; the advisory (model-judged) lane is reported in its own
section with its gate flag, and never changes the exit here.
"""

from __future__ import annotations

import json
from typing import Any, Dict, List, Optional, Tuple

LANES = ("Outcome", "Policy", "Conversation", "Speech", "Reliability")
_DIM_KEYS = ("outcome", "policy", "conversation", "speech")
_TITLE = "VOICE CONVERSATION REGRESSION"
_DETAIL_CAP = 140


def _clip(text: str, cap: int = _DETAIL_CAP) -> str:
    text = " ".join(str(text).split())
    return text if len(text) <= cap else text[: cap - 3] + "..."


def _lane_line(name: str, status: str, detail: str = "") -> str:
    line = f"{name:<14}{status}"
    if detail:
        line += f"  {_clip(detail)}"
    return line


def _lane_status(counts: Dict[str, Any]) -> str:
    fail = int(counts.get("fail") or 0)
    inconclusive = int(counts.get("inconclusive") or 0)
    passed = int(counts.get("pass") or 0)
    if fail > 0:
        return "FAIL"
    if inconclusive > 0:
        return "INCONCLUSIVE"
    if passed > 0:
        return "PASS"
    return "NOT_RUN"


def _reliability_lane(rel: Optional[Dict[str, Any]]) -> str:
    if not isinstance(rel, dict) or rel.get("n") in (None, ""):
        return _lane_line("Reliability", "NOT_RUN")
    passes = rel.get("passes")
    n = rel.get("n")
    parts: List[str] = []
    if rel.get("pass_at_1") is not None:
        parts.append(f"pass@1 {float(rel['pass_at_1']):.3f}")
    if rel.get("pass_caret_k") is not None:
        parts.append(f"pass^k {float(rel['pass_caret_k']):.3f}")
    ci = rel.get("ci") or {}
    if ci.get("low") is not None and ci.get("high") is not None:
        parts.append(
            f"Wilson interval: [{float(ci['low']):.3f}, {float(ci['high']):.3f}]"
        )
    return _lane_line("Reliability", f"{passes}/{n}", "  ".join(parts))


def _block(lane_lines: List[str], reproduce: str, checks: List[str]) -> str:
    lines = [_TITLE]
    lines.extend(lane_lines)
    lines.append("Reproduce:")
    lines.append(reproduce or "(no command was run)")
    lines.append("Acceptance checks:")
    lines.extend(checks or ["(none evaluated)"])
    return "```text\n" + "\n".join(lines) + "\n```"


def _cap_list(items: List[str], cap: int, more_hint: str) -> List[str]:
    if len(items) <= cap:
        return items
    return items[:cap] + [f"+{len(items) - cap} more (see {more_hint})"]


def _records_section(meta: Dict[str, Any]) -> List[str]:
    """The share-safe Failure Records section, built ONLY from index fields the
    gate already validated (test id, bounded headline, and the record's
    Markdown path). Rendered only when records were actually written and
    cross-checked; a truncated set states the omission explicitly."""
    rec = meta.get("record_set")
    if not isinstance(rec, dict):
        return []
    entries = rec.get("entries") or []
    if not entries:
        return []
    lines = [f"### Failure Records ({len(entries)})", ""]
    for entry in entries:
        lines.append(f"- `{entry['test_id']}` — "
                     f"{_clip(entry['headline'], 240)}  ")
        lines.append(f"  `{entry['path']}`")
    if rec.get("truncated"):
        lines.append("")
        lines.append(
            f"Rendered {rec.get('count')} of {rec.get('total')} non-passing"
            f" units (record-limit={rec.get('limit')})."
        )
    lines.append("")
    return lines


# ---------------------------------------------------------------------------
# suite-run
# ---------------------------------------------------------------------------

def _suite_lanes(doc: Dict[str, Any]) -> List[str]:
    dims = doc.get("dimensions") or {}
    tests = doc.get("tests") or []
    lines = []
    for key, name in zip(_DIM_KEYS, LANES[:4]):
        counts = dims.get(key) or {}
        status = _lane_status(counts)
        detail = ""
        if status == "FAIL":
            reasons = [
                t.get("dim_reason", {}).get(key)
                for t in tests
                if isinstance(t.get("dim_reason"), dict)
                and t.get("dim_reason", {}).get(key)
            ]
            n_fail = int(counts.get("fail") or 0)
            detail = reasons[0] if reasons else f"{n_fail} failing checks"
            if reasons and n_fail > 1:
                detail += f" ({n_fail} failing checks)"
        elif status == "INCONCLUSIVE":
            detail = (
                f"{counts.get('inconclusive')} checks missing required "
                f"evidence; {counts.get('pass')} pass"
            )
        elif status == "PASS":
            detail = f"{counts.get('pass')} checks pass"
        lines.append(_lane_line(name, status, detail))
    lines.append(_reliability_lane(doc.get("reliability")))
    return lines


def _suite_checks(doc: Dict[str, Any]) -> List[str]:
    tests = doc.get("tests") or []
    failing: List[str] = []
    all_ids: List[str] = []
    for t in tests:
        tid = t.get("test_id", "?")
        for key in _DIM_KEYS + ("reliability",):
            ids = ((t.get("dim_counts") or {}).get(key) or {}).get("ids") or []
            all_ids.extend(f"{tid}:{i}" for i in ids)
        for key, reason in sorted((t.get("dim_reason") or {}).items()):
            failing.append(_clip(f"{tid} [{key}] {reason}"))
    if failing:
        return _cap_list(failing, 10, "the machine result JSON")
    return _cap_list(all_ids, 12, "the machine result JSON")


def _suite_status(doc: Dict[str, Any], exit_code: int) -> str:
    counts = doc.get("counts") or {}
    dims = doc.get("dimensions") or {}
    inconclusive = sum(
        int((dims.get(k) or {}).get("inconclusive") or 0) for k in _DIM_KEYS
    )
    refused = int(counts.get("refused_tests") or 0)
    if exit_code == 0:
        return "inconclusive" if (inconclusive or refused) else "pass"
    if exit_code == 1:
        return "fail"
    if exit_code == 2 and refused:
        return "inconclusive"
    return "error"


def _suite_header(doc: Dict[str, Any]) -> List[str]:
    counts = doc.get("counts") or {}
    dims = doc.get("dimensions") or {}
    inconclusive = sum(
        int((dims.get(k) or {}).get("inconclusive") or 0) for k in _DIM_KEYS
    )
    return [
        f"Suite `{doc.get('suite_id', '?')}` (agent `{doc.get('agent', '?')}`,"
        f" release `{doc.get('release_id', '?')}`).",
        f"Evaluated: {counts.get('tests', 0)} tests"
        f" ({counts.get('passed_tests', 0)} passed,"
        f" {counts.get('failed_tests', 0)} failed,"
        f" {counts.get('refused_tests', 0)} refused),"
        f" {counts.get('runs', 0)} runs ({counts.get('valid', 0)} valid,"
        f" {counts.get('simulator_invalid', 0)} simulator invalid),"
        f" {inconclusive} inconclusive checks.",
    ]


def _suite_advisory(doc: Dict[str, Any]) -> List[str]:
    totals = {"pass": 0, "fail": 0, "inconclusive": 0, "error": 0}
    for t in doc.get("tests") or []:
        rs = t.get("rubric_summary") or {}
        for k in totals:
            totals[k] += int(rs.get(k) or 0)
    return [
        "- gate enabled: false",
        f"- {totals['pass']} pass, {totals['fail']} fail,"
        f" {totals['inconclusive']} inconclusive, {totals['error']} error",
    ]


def _suite_failure_headline(doc: Dict[str, Any]) -> Optional[str]:
    for t in doc.get("tests") or []:
        if t.get("status") != "pass":
            reasons = t.get("dim_reason") or {}
            if reasons:
                key = sorted(reasons)[0]
                return _clip(f"`{t.get('test_id', '?')}` [{key}] {reasons[key]}")
            return f"`{t.get('test_id', '?')}` {t.get('status')}"
    return None


# ---------------------------------------------------------------------------
# test-run
# ---------------------------------------------------------------------------

def _test_results_by_dim(doc: Dict[str, Any]) -> Dict[str, List[Dict[str, Any]]]:
    grouped: Dict[str, List[Dict[str, Any]]] = {k: [] for k in _DIM_KEYS}
    for r in ((doc.get("assertions") or {}).get("results")) or []:
        dim = r.get("dimension")
        if dim in grouped:
            grouped[dim].append(r)
    return grouped


def _test_result_detail(r: Dict[str, Any]) -> str:
    bits = [str(r.get("id", "?"))]
    if r.get("measured_ms") is not None:
        bits.append(f"measured_ms={r['measured_ms']}")
    if r.get("reason"):
        bits.append(str(r["reason"]))
    return " ".join(bits)


def _test_lanes(doc: Dict[str, Any]) -> List[str]:
    dims = doc.get("dimensions") or {}
    grouped = _test_results_by_dim(doc)
    lines = []
    for key, name in zip(_DIM_KEYS, LANES[:4]):
        counts = dims.get(key) or {}
        status = _lane_status(counts)
        detail = ""
        if status in ("FAIL", "INCONCLUSIVE"):
            hits = [r for r in grouped[key] if r.get("status") == status]
            if hits:
                detail = _test_result_detail(hits[0])
        elif status == "PASS":
            detail = "; ".join(_test_result_detail(r) for r in grouped[key][:3])
        lines.append(_lane_line(name, status, detail))
    rel = (doc.get("reliability") or {}).get("aggregate")
    lines.append(_reliability_lane(rel))
    return lines


def _test_checks(doc: Dict[str, Any]) -> List[str]:
    out = []
    for r in ((doc.get("assertions") or {}).get("results")) or []:
        out.append(f"{r.get('id', '?')} {r.get('status', '?')}")
    return _cap_list(out, 12, "the machine result JSON")


def _test_status(doc: Dict[str, Any], exit_code: int) -> str:
    results = ((doc.get("assertions") or {}).get("results")) or []
    inconclusive = any(r.get("status") == "INCONCLUSIVE" for r in results)
    failed = any(r.get("status") == "FAIL" for r in results)
    if exit_code == 0:
        return "inconclusive" if inconclusive else "pass"
    if exit_code == 1:
        # A gate that failed only because required evidence was missing is
        # INCONCLUSIVE, never a deterministic FAIL; the exit stays 1.
        return "fail" if failed or not inconclusive else "inconclusive"
    if exit_code == 2 and inconclusive:
        return "inconclusive"
    return "error"


def _test_header(doc: Dict[str, Any]) -> List[str]:
    results = ((doc.get("assertions") or {}).get("results")) or []
    n_inc = sum(1 for r in results if r.get("status") == "INCONCLUSIVE")
    success = doc.get("success") or {}
    conditions = success.get("conditions") or {}
    cond_text = ", ".join(f"{k}={str(v).lower()}" for k, v in sorted(conditions.items()))
    return [
        f"Conversation test `{doc.get('test_id', '?')}`"
        f" (agent `{doc.get('agent', '?')}`,"
        f" inconclusive_policy `{doc.get('inconclusive_policy', '?')}`).",
        f"Evaluated: {len(results)} deterministic checks"
        f" ({n_inc} inconclusive); success.required"
        f" passed={str(success.get('passed', '?')).lower()} ({cond_text}).",
    ]


def _test_advisory(doc: Dict[str, Any]) -> List[str]:
    env = doc.get("rubric") or {}
    results = env.get("results") or []
    counts = {"PASS": 0, "FAIL": 0, "INCONCLUSIVE": 0, "ERROR": 0}
    first_error = ""
    for r in results:
        status = str(r.get("status", "")).upper()
        if status in counts:
            counts[status] += 1
        if status == "ERROR" and not first_error:
            first_error = str(r.get("reason") or r.get("error") or "")
    lines = [
        f"- gate enabled: {str(bool(env.get('gated'))).lower()}",
        f"- {counts['PASS']} pass, {counts['FAIL']} fail,"
        f" {counts['INCONCLUSIVE']} inconclusive, {counts['ERROR']} error",
    ]
    if first_error:
        lines.append(f"- judge lane error: {_clip(first_error, 120)}")
    return lines


def _test_failure_headline(doc: Dict[str, Any]) -> Optional[str]:
    for r in ((doc.get("assertions") or {}).get("results")) or []:
        if r.get("status") == "FAIL":
            return _clip(f"`{r.get('id', '?')}` {r.get('reason') or 'FAIL'}")
    success = doc.get("success") or {}
    if success.get("passed") is False:
        return "a success.required condition failed"
    return None


# ---------------------------------------------------------------------------
# contract-verify
# ---------------------------------------------------------------------------

def _contract_lanes(doc: Dict[str, Any]) -> List[str]:
    per_dim: Dict[str, Dict[str, int]] = {
        k: {"pass": 0, "fail": 0, "inconclusive": 0} for k in _DIM_KEYS
    }
    reasons: Dict[str, str] = {}
    for r in doc.get("results") or []:
        env = r.get("assertions") or {}
        for a in env.get("results") or []:
            dim = a.get("dimension")
            if dim not in per_dim:
                continue
            status = str(a.get("status", "")).upper()
            if status == "PASS":
                per_dim[dim]["pass"] += 1
            elif status == "FAIL":
                per_dim[dim]["fail"] += 1
                reasons.setdefault(
                    dim, f"{a.get('id', '?')} {a.get('reason') or 'FAIL'}"
                )
            elif status == "INCONCLUSIVE":
                per_dim[dim]["inconclusive"] += 1
    lines = []
    for key, name in zip(_DIM_KEYS, LANES[:4]):
        counts = per_dim[key]
        status = _lane_status(counts)
        detail = ""
        if status == "FAIL":
            detail = reasons.get(key, f"{counts['fail']} failing checks")
        elif status == "INCONCLUSIVE":
            detail = f"{counts['inconclusive']} checks missing required evidence"
        elif status == "PASS":
            detail = f"{counts['pass']} embedded checks pass"
        lines.append(_lane_line(name, status, detail))
    lines.append(_reliability_lane(None))
    return lines


def _contract_checks(doc: Dict[str, Any]) -> List[str]:
    out = []
    for r in doc.get("results") or []:
        if not r.get("scorable", True):
            out.append(
                _clip(f"{r.get('id', '?')} NOT_SCORABLE"
                      f" {r.get('not_scorable_reason') or ''}")
            )
        elif not r.get("verdict_eligible", True):
            out.append(
                _clip(f"{r.get('id', '?')} REFUSED"
                      f" {r.get('verdict_ineligible_reason') or ''}")
            )
        else:
            mark = "PASS" if r.get("passed") else "FAIL"
            m = r.get("measurement") or {}
            out.append(
                _clip(
                    f"{r.get('id', '?')} {mark} expect={r.get('expect', '?')}"
                    f" did_yield={m.get('did_yield')}"
                    f" seconds_to_yield={m.get('seconds_to_yield')}"
                    f" talk_over={m.get('talk_over_sec')}"
                )
            )
    return _cap_list(out, 12, "the machine result JSON")


def _contract_status(doc: Dict[str, Any], exit_code: int) -> str:
    if exit_code == 0:
        results = doc.get("results") or []
        not_scorable = any(not r.get("scorable", True) for r in results)
        return "inconclusive" if not_scorable else "pass"
    if exit_code == 1:
        return "fail"
    return "error"


def _contract_header(doc: Dict[str, Any]) -> List[str]:
    summary = doc.get("summary") or {}
    return [
        f"Contract verification of `{doc.get('dir', '?')}`.",
        f"Evaluated: {doc.get('count', 0)} contracts"
        f" ({summary.get('passed', 0)} passed, {summary.get('failed', 0)} failed,"
        f" {doc.get('refused', 0)} refused, {doc.get('tampered', 0)} tampered,"
        f" {doc.get('assertions_failed', 0)} with a failing embedded check).",
    ]


def _contract_failure_headline(doc: Dict[str, Any]) -> Optional[str]:
    for r in doc.get("results") or []:
        if r.get("scorable", True) and not r.get("passed", True):
            m = r.get("measurement") or {}
            return _clip(
                f"`{r.get('id', '?')}` regressed: expect={r.get('expect', '?')}"
                f" did_yield={m.get('did_yield')}"
                f" seconds_to_yield={m.get('seconds_to_yield')}"
                f" talk_over={m.get('talk_over_sec')}"
            )
        if not r.get("scorable", True):
            return _clip(
                f"`{r.get('id', '?')}` not scorable:"
                f" {r.get('not_scorable_reason') or ''}"
            )
    return None


def _contract_advisory(doc: Dict[str, Any]) -> List[str]:
    return [
        "- gate enabled: false",
        "- this source carries no model-judged rubric lane",
    ]


# ---------------------------------------------------------------------------
# assembly
# ---------------------------------------------------------------------------

_RENDERERS = {
    "hotato.suite-run": (
        _suite_lanes, _suite_checks, _suite_status, _suite_header,
        _suite_advisory, _suite_failure_headline,
    ),
    "hotato.test-run": (
        _test_lanes, _test_checks, _test_status, _test_header,
        _test_advisory, _test_failure_headline,
    ),
    "contract-verify": (
        _contract_lanes, _contract_checks, _contract_status, _contract_header,
        _contract_advisory, _contract_failure_headline,
    ),
}


def _artifact_lines(meta: Dict[str, Any]) -> List[str]:
    lines = []
    if meta.get("output"):
        lines.append(f"- results directory: `{meta['output']}`")
    if meta.get("result_path"):
        lines.append(f"- machine result (primary): `{meta['result_path']}`")
    if meta.get("summary_path"):
        lines.append(f"- summary: `{meta['summary_path']}`")
    if meta.get("junit_path"):
        lines.append(f"- JUnit report: `{meta['junit_path']}`")
    if meta.get("records"):
        lines.append(f"- Failure Records: `{meta['records']}`")
    elif meta.get("records_note"):
        lines.append(f"- Failure Records: {meta['records_note']}")
    lines.append(
        "- artifact upload is a consumer workflow step (actions/upload-artifact"
        " pinned by full commit SHA); this Action never uploads"
    )
    return lines


def render(
    doc: Optional[Dict[str, Any]],
    exit_code: Optional[int],
    meta: Dict[str, Any],
    error: Optional[str] = None,
) -> Tuple[str, str]:
    """Render (markdown, status) for one machine result.

    ``doc`` is the parsed hotato JSON (or ``None``), ``exit_code`` the captured
    hotato process exit (or ``None`` when no process ran), ``meta`` carries the
    presentation context (reproduce command, artifact paths, versions), and
    ``error`` an internal failure description when the result is unusable.
    """
    kind = (doc or {}).get("kind") if isinstance(doc, dict) else None
    renderer = _RENDERERS.get(kind or "")
    if renderer is None or error is not None or exit_code is None:
        reason = error or (
            f"unrecognized result kind {kind!r}" if doc is not None
            else "no machine result was produced"
        )
        lanes = [_lane_line(name, "NOT_RUN") for name in LANES]
        checks = ["(none evaluated: the machine result is missing or malformed)"]
        status = "error"
        header = [
            "The run produced no usable machine result, so no lane is scored."
            " A missing result is never a PASS.",
            f"Error: {_clip(reason, 300)}",
        ]
        advisory = ["- gate enabled: false", "- no advisory lane was evaluated"]
        headline = _clip(reason, 200)
    else:
        lanes_fn, checks_fn, status_fn, header_fn, advisory_fn, headline_fn = renderer
        lanes = lanes_fn(doc)
        checks = checks_fn(doc)
        status = status_fn(doc, exit_code)
        header = header_fn(doc)
        advisory = advisory_fn(doc)
        headline = headline_fn(doc) if status in ("fail", "inconclusive", "error") else None

    shown_exit = exit_code if exit_code is not None else meta.get("fallback_exit", 2)
    md: List[str] = [f"## hotato conversation QA: {status.upper()}", ""]
    md.extend(header)
    provenance = []
    if meta.get("hotato_version"):
        provenance.append(f"hotato {meta['hotato_version']}")
    if meta.get("install_source"):
        provenance.append(f"install: {meta['install_source']}")
    if meta.get("action_ref"):
        provenance.append(f"action revision: {meta['action_ref']}")
    provenance.append(f"exit code: {shown_exit}")
    md.append(" | ".join(provenance))
    md.append("")
    md.append(_block(lanes, meta.get("reproduce", ""), checks))
    md.append("")
    if headline:
        md.append("### Failure headline")
        md.append(headline)
        md.append("")
    md.extend(_records_section(meta))
    md.append("### Advisory (model-judged rubric lane)")
    md.extend(advisory)
    md.append(
        "- an advisory verdict never changes the exit code unless the run"
        " opts in through hotato's own gate flag"
    )
    md.append("")
    md.append("### Artifacts")
    md.extend(_artifact_lines(meta))
    md.append("")
    return "\n".join(md), status


def load_result(path: str) -> Tuple[Optional[Dict[str, Any]], Optional[str]]:
    """Parse a machine result file; returns (doc, error). Never raises."""
    try:
        with open(path, "r", encoding="utf-8") as fh:
            doc = json.load(fh)
    except (OSError, ValueError) as exc:
        return None, f"unreadable machine result {path}: {exc}"
    if not isinstance(doc, dict):
        return None, f"machine result {path} is not a JSON object"
    return doc, None
