"""Deterministic renderers for the ONE canonical ``hotato.failure-record.v1``
dict: canonical JSON, Markdown, a self-contained HTML page, and a 1200x630
SVG share card.

Every format derives from the same validated record and shows the same
``record_id`` -- there is no per-format re-scoring, no wall clock, and no
randomness, so rendering the same record twice is byte-identical. The HTML is
inert (inline CSS only, no remote asset, no script element, no event handler)
and every record-supplied string is escaped before it reaches markup. The
first visible sentence of every human format is the record's headline
(``{ASSERTION} failed: {bounded observed evidence}``).
"""

from __future__ import annotations

import html
import shlex
from typing import Any, Dict, List, Optional

from .errors import safe_json_dumps
from .failure_record import LANES, validate_record

__all__ = [
    "render_json",
    "render_markdown",
    "render_html",
    "render_svg",
    "render_all",
    "record_directory",
    "build_record_index",
    "render_index_md",
    "render_record_set",
    "INDEX_KIND",
    "INDEX_VERSION",
]

INDEX_KIND = "hotato.failure-record-index.v1"
INDEX_VERSION = "1.0"

# The same warm-charcoal family the report/suite renderers use.
_BG = "#1b1714"
_SURFACE = "#241f1a"
_BORDER = "#493d31"
_TEXT = "#f1e8d7"
_MUTED = "#b7ab97"
_PASS = "#74c98a"
_FAIL = "#e0664f"

_STATUS_COLOR = {
    "PASS": _PASS, "FAIL": _FAIL, "ERROR": _FAIL,
    "INCONCLUSIVE": _MUTED, "NOT_RUN": _MUTED, "UNAVAILABLE": _MUTED,
}


def _esc(value: Any) -> str:
    return html.escape(str(value), quote=True)


def _md_text(value: Any) -> str:
    """One Markdown-safe text run: pipes and newlines cannot break the table,
    and raw HTML (a script tag pasted into a label) renders as text."""
    text = str(value).replace("|", "\\|").replace("\n", " ")
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def _clip(value: Any, limit: int) -> str:
    text = " ".join(str(value).split())
    return text if len(text) <= limit else text[: max(1, limit - 3)] + "..."


def _primary_assertion(record: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """The assertion the headline is built from, selected by the SAME severity
    precedence as :func:`hotato.failure_record._headline` -- deterministic
    ERROR, then FAIL, then INCONCLUSIVE (matching the gate), scanning lanes in
    the fixed order. ``None`` for a purely advisory-gated record (which has no
    deterministic failure to show)."""
    for wanted in ("ERROR", "FAIL", "INCONCLUSIVE"):
        for lane in LANES:
            for assertion in record["dimensions"][lane]["assertions"]:
                if assertion["authority"] == "deterministic" \
                        and assertion["status"] == wanted:
                    return assertion
    return None


def _lane_observed(record: Dict[str, Any], lane: str) -> str:
    assertions = record["dimensions"][lane]["assertions"]
    if not assertions:
        return "No assertion ran in this dimension."
    return assertions[0]["observed"]


def _command(record: Dict[str, Any]) -> str:
    return shlex.join(record["reproduction"]["argv"])


def _reliability_line(record: Dict[str, Any]) -> str:
    rel = record["dimensions"]["reliability"]
    if rel["pass_at_1"] is None:
        return (f"no repetition data (trials={rel['trials']}); rates and "
                "interval are not shown for a single run")
    interval = rel["wilson_interval"]
    line = (
        f"pass@1={rel['pass_at_1']:.3f} pass@k={rel['pass_at_k']:.3f} "
        f"pass^k={rel['pass_caret_k']:.3f} "
        f"({rel['passes']} of {rel['trials']} trials passed)"
    )
    if interval is not None:
        pct = f"{interval['confidence'] * 100:g}%"
        line += (f"; {pct} Wilson interval "
                 f"[{interval['lower']:.6f}, {interval['upper']:.6f}]")
    return line


def _advisory_line(record: Dict[str, Any]) -> str:
    adv = record["advisory"]
    gate = "enabled" if adv["gate_enabled"] else "not enabled"
    line = f"{adv['status']} (gate {gate}"
    if adv.get("reason_code"):
        line += f", reason {adv['reason_code']}"
    return line + ")"


# =========================================================================
# JSON
# =========================================================================

def render_json(record: Dict[str, Any]) -> str:
    """Canonical machine form: UTF-8, sorted keys, two-space indent, newline
    at EOF. The byte-level source the other three formats are views of."""
    return safe_json_dumps(record, indent=2, sort_keys=True,
                           ensure_ascii=False) + "\n"


# =========================================================================
# Markdown
# =========================================================================

def render_markdown(record: Dict[str, Any]) -> str:
    primary = _primary_assertion(record)
    lines = [
        f"# {_md_text(record['headline'])}",
        "",
        (f"`{record['status']}` failure record for "
         f"`{_md_text(record['subject']['test_id'])}` "
         f"(origin `{_md_text(record['origin']['kind'])}`)."),
        "",
        "| Dimension | Status | Observed |",
        "|---|---|---|",
    ]
    for lane in LANES:
        dim = record["dimensions"][lane]
        lines.append(
            f"| {_md_text(lane.title())} | {_md_text(dim['status'])} "
            f"| {_md_text(_lane_observed(record, lane))} |"
        )
    lines.extend([
        "",
        f"**Deterministic gate:** `{_md_text(record['gate']['status'])}` "
        f"(policy `{_md_text(record['gate']['policy'])}`)  ",
        f"**Model advisory:** {_md_text(_advisory_line(record))}",
        "",
    ])
    if primary is not None:
        refs = ", ".join(f"`{_md_text(r)}`" for r in primary["evidence_refs"]) \
            or "none"
        lines.extend([
            "## Primary assertion",
            "",
            f"- Rule: `{_md_text(primary['rule_id'])}`",
            f"- Status: `{_md_text(primary['status'])}`",
            f"- Expected: {_md_text(primary['expected'])}",
            f"- Observed: {_md_text(primary['observed'])}",
            f"- Evidence references: {refs}",
            "",
        ])
    lines.extend([
        "## Reliability",
        "",
        _md_text(_reliability_line(record)),
        "",
        "## Reproduce",
        "",
        "```bash",
        _command(record),
        "```",
        "",
        (f"`{record['record_id']}` · hotato "
         f"{_md_text(record['provenance']['hotato']['version'])} · privacy "
         f"profile `{_md_text(record['privacy']['profile'])}`"),
        "",
    ])
    return "\n".join(lines)


# =========================================================================
# HTML (self-contained, inert)
# =========================================================================

def render_html(record: Dict[str, Any]) -> str:
    primary = _primary_assertion(record)
    rows: List[str] = []
    for lane in LANES:
        dim = record["dimensions"][lane]
        color = _STATUS_COLOR.get(dim["status"], _MUTED)
        rows.append(
            "<tr>"
            f"<th>{_esc(lane.title())}</th>"
            f'<td><span class="status" style="color:{color}">'
            f"{_esc(dim['status'])}</span></td>"
            f"<td>{_esc(_lane_observed(record, lane))}</td>"
            "</tr>"
        )
    if primary is not None:
        refs = _esc(", ".join(primary["evidence_refs"]) or "none")
        primary_html = (
            "<section><h2>Primary assertion</h2>"
            f"<p><code>{_esc(primary['rule_id'])}</code> · "
            f"<code>{_esc(primary['status'])}</code></p>"
            f"<p><strong>Expected:</strong> {_esc(primary['expected'])}</p>"
            f"<p><strong>Observed:</strong> {_esc(primary['observed'])}</p>"
            f"<p><strong>Evidence:</strong> {refs}</p></section>"
        )
    else:
        primary_html = (
            "<section><h2>Primary assertion</h2>"
            "<p>The deterministic lanes passed; this record exists because "
            "the model advisory gate is enabled and reported "
            f"{_esc(record['advisory']['status'])}.</p></section>"
        )
    evidence_rows = "".join(
        "<tr>"
        f"<td><code>{_esc(item['evidence_id'])}</code></td>"
        f"<td>{_esc(item['kind'])}</td>"
        f"<td>{_esc(item.get('locator') or '(digest only)')}</td>"
        f"<td><code>{_esc(_clip(item['digest'], 23))}</code></td>"
        "</tr>"
        for item in record["evidence"]
    ) or '<tr><td colspan="4">No evidence entries.</td></tr>'
    status_color = _STATUS_COLOR.get(record["status"], _FAIL)
    return f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta name="color-scheme" content="dark">
<title>{_esc(record['status'])} · {_esc(record['subject']['test_id'])} · hotato failure record</title>
<style>
* {{ box-sizing:border-box; }}
body {{ margin:0; background:{_BG}; color:{_TEXT}; font-family:ui-sans-serif,system-ui,sans-serif; }}
main {{ max-width:960px; margin:0 auto; padding:32px 20px 48px; }}
h1 {{ font-size:1.5rem; line-height:1.35; }} h1,h2 {{ font-weight:650; }}
.eyebrow,.meta {{ color:{_MUTED}; font-size:0.9rem; }}
.panel {{ background:{_SURFACE}; border:1px solid {_BORDER}; border-radius:14px; padding:18px; margin:18px 0; }}
table {{ width:100%; border-collapse:collapse; margin:18px 0; }}
th,td {{ padding:10px 12px; text-align:left; border-bottom:1px solid {_BORDER}; vertical-align:top; overflow-wrap:anywhere; }}
th {{ width:18%; }}
.status {{ font-family:ui-monospace,SFMono-Regular,monospace; font-weight:700; }}
code,pre {{ font-family:ui-monospace,SFMono-Regular,monospace; overflow-wrap:anywhere; }}
pre {{ white-space:pre-wrap; background:{_SURFACE}; border:1px solid {_BORDER}; border-radius:10px; padding:12px; }}
</style>
</head>
<body>
<main>
<div class="eyebrow">hotato failure record · <span class="status" style="color:{status_color}">{_esc(record['status'])}</span> · {_esc(record['subject']['test_id'])}</div>
<h1>{_esc(record['headline'])}</h1>
<table aria-label="Five evaluation dimensions"><thead><tr><th>Dimension</th><th>Status</th><th>Observed</th></tr></thead><tbody>{''.join(rows)}</tbody></table>
<div class="panel"><h2>Gate authority</h2>
<p>Deterministic gate: <strong>{_esc(record['gate']['status'])}</strong> · policy <code>{_esc(record['gate']['policy'])}</code></p>
<p>Model advisory: <strong>{_esc(record['advisory']['status'])}</strong> · gate {_esc('enabled' if record['advisory']['gate_enabled'] else 'not enabled')}</p></div>
<div class="panel"><h2>Reliability</h2><p>{_esc(_reliability_line(record))}</p></div>
{primary_html}
<section><h2>Evidence references</h2>
<table aria-label="Evidence references"><thead><tr><th>Reference</th><th>Kind</th><th>Locator</th><th>Digest</th></tr></thead><tbody>{evidence_rows}</tbody></table></section>
<section><h2>Reproduce</h2><pre><code>{_esc(_command(record))}</code></pre></section>
<p class="meta"><code>{_esc(record['record_id'])}</code><br>
hotato {_esc(record['provenance']['hotato']['version'])} · origin {_esc(record['origin']['kind'])} · privacy profile {_esc(record['privacy']['profile'])}</p>
</main>
</body>
</html>
"""


# =========================================================================
# SVG share card (1200x630)
# =========================================================================

def _svg_esc(value: Any, limit: int) -> str:
    return _esc(_clip(value, limit))


def render_svg(record: Dict[str, Any]) -> str:
    lanes: List[str] = []
    for x, lane in zip((56, 286, 516, 746, 976), LANES):
        dim = record["dimensions"][lane]
        color = _STATUS_COLOR.get(dim["status"], _MUTED)
        lanes.append(
            f'<g transform="translate({x} 230)">'
            f'<rect width="208" height="104" rx="12" fill="{_SURFACE}" stroke="{_BORDER}"/>'
            f'<text x="16" y="32" fill="{_MUTED}" font-size="17">{_svg_esc(lane.title(), 20)}</text>'
            f'<text x="16" y="70" fill="{color}" font-size="22" font-weight="700">{_svg_esc(dim["status"], 14)}</text>'
            "</g>"
        )
    primary = _primary_assertion(record)
    if primary is not None:
        observed = primary["observed"]
    else:
        observed = ("the deterministic lanes passed; the enabled model "
                    f"advisory gate reported {record['advisory']['status']}")
    status_color = _STATUS_COLOR.get(record["status"], _FAIL)
    font = "ui-sans-serif,system-ui,sans-serif"
    mono = "ui-monospace,SFMono-Regular,monospace"
    return f"""<svg xmlns="http://www.w3.org/2000/svg" width="1200" height="630" viewBox="0 0 1200 630" role="img" aria-labelledby="title desc">
<title id="title">{_svg_esc(record['status'], 14)} · {_svg_esc(record['subject']['test_id'], 60)}</title>
<desc id="desc">{_svg_esc(record['headline'], 200)}</desc>
<rect width="1200" height="630" fill="{_BG}"/>
<text x="56" y="55" fill="{_MUTED}" font-family="{font}" font-size="18">HOTATO · FAILURE RECORD</text>
<text x="56" y="112" fill="{status_color}" font-family="{font}" font-size="44" font-weight="700">{_svg_esc(record['status'], 14)} · {_svg_esc(record['subject']['test_id'], 40)}</text>
<text x="56" y="166" fill="{_TEXT}" font-family="{font}" font-size="23">{_svg_esc(record['headline'], 112)}</text>
<text x="56" y="205" fill="{_MUTED}" font-family="{font}" font-size="16">FIVE DIMENSIONS · EACH WITH ITS OWN STATUS · NEVER BLENDED</text>
{''.join(lanes)}
<text x="56" y="390" fill="{_MUTED}" font-family="{font}" font-size="17">PRIMARY EVIDENCE</text>
<text x="56" y="426" fill="{_TEXT}" font-family="{font}" font-size="22">{_svg_esc(observed, 120)}</text>
<text x="56" y="477" fill="{_MUTED}" font-family="{font}" font-size="17">REPRODUCE</text>
<text x="56" y="510" fill="{_TEXT}" font-family="{mono}" font-size="17">{_svg_esc(_command(record), 118)}</text>
<line x1="56" y1="548" x2="1144" y2="548" stroke="{_BORDER}"/>
<text x="56" y="583" fill="{_MUTED}" font-family="{mono}" font-size="14">{_svg_esc(record['record_id'], 80)}</text>
<text x="56" y="608" fill="{_MUTED}" font-family="{font}" font-size="14">deterministic gate: {_svg_esc(record['gate']['status'], 14)} · advisory: {_svg_esc(record['advisory']['status'], 14)} · origin: {_svg_esc(record['origin']['kind'], 14)}</text>
</svg>
"""


# =========================================================================
# all four, from the one validated record
# =========================================================================

def render_all(record: Dict[str, Any]) -> Dict[str, str]:
    """Validate ``record`` once, then render every format from it. Keys are
    the canonical output file names. Deterministic: same record, same bytes."""
    validate_record(record)
    return {
        "failure-record.json": render_json(record),
        "failure-record.md": render_markdown(record),
        "failure-record.html": render_html(record),
        "failure-record.svg": render_svg(record),
    }


# =========================================================================
# record SET: a deterministic index over every rendered failure record
# =========================================================================

def record_directory(record: Dict[str, Any]) -> str:
    """The digest-scoped child directory name for one record: ``sha256-<64
    lowercase hex>`` derived from the content-addressed ``record_id``. NEVER a
    test id -- the digest prevents traversal, collisions, case-folding bugs,
    and accidental disclosure of a test id in a path."""
    record_id = record["record_id"]
    hexpart = record_id.split(":", 1)[1] if ":" in record_id else record_id
    return "sha256-" + hexpart


def build_record_index(
    records: List[Dict[str, Any]],
    source_digest: str,
    total_failures: int,
    truncated: bool = False,
    *,
    source_kind: Optional[str] = None,
) -> Dict[str, Any]:
    """The closed ``hotato.failure-record-index.v1`` dict over a set of records
    rendered from ONE source. Structure only: the source kind + digest, how
    many non-passing units the source had (``total_failures``), how many were
    rendered, whether the set was truncated by a record limit, and one entry
    per record in SOURCE ORDER pointing at its content-addressed child
    directory. No wall-clock field and no aggregate score -- an index groups
    records, it never blends them.

    ``source_kind`` defaults to the records' shared ``origin.source``; it must
    be supplied for an empty (all-pass) set, which has no record to derive it
    from."""
    if source_kind is None:
        source_kind = records[0]["origin"]["source"] if records else "unknown"
    entries = [
        {
            "record_id": record["record_id"],
            "status": record["status"],
            "test_id": record["subject"]["test_id"],
            "headline": record["headline"],
            "directory": record_directory(record),
        }
        for record in records
    ]
    return {
        "kind": INDEX_KIND,
        "version": INDEX_VERSION,
        "source": {"kind": source_kind, "digest": source_digest},
        "total_failures": int(total_failures),
        "rendered": len(records),
        "truncated": bool(truncated),
        "records": entries,
    }


def render_index_json(index: Dict[str, Any]) -> str:
    """Canonical machine form of the index: UTF-8, sorted keys, two-space
    indent, newline at EOF. The ``records`` array stays in source order (only
    object keys sort), so the bytes are deterministic for the same source."""
    return safe_json_dumps(index, indent=2, sort_keys=True,
                           ensure_ascii=False) + "\n"


def render_index_md(index: Dict[str, Any]) -> str:
    """The human index: one row per record, linking its relative Markdown /
    HTML / SVG paths and carrying the same headline. Byte-deterministic for the
    same index; an all-pass source renders an explicit zero-record note (never
    a fabricated failure)."""
    source = index["source"]
    lines = [
        f"# Failure records ({index['rendered']})",
        "",
        (f"Source `{_md_text(source['kind'])}` `{_md_text(source['digest'])}` "
         f"· rendered {index['rendered']} of {index['total_failures']} "
         "non-passing unit(s)"
         + (" (truncated by the record limit)" if index["truncated"] else "")
         + "."),
        "",
    ]
    if not index["records"]:
        lines.append(
            "Every check passed: no non-passing unit was found, so no failure "
            "record was written."
        )
        lines.append("")
        return "\n".join(lines)
    lines.append("| Status | Test | Headline | Formats |")
    lines.append("|---|---|---|---|")
    for entry in index["records"]:
        directory = entry["directory"]
        formats = (
            f"[json]({directory}/failure-record.json) · "
            f"[md]({directory}/failure-record.md) · "
            f"[html]({directory}/failure-record.html) · "
            f"[svg]({directory}/failure-record.svg)"
        )
        lines.append(
            f"| {_md_text(entry['status'])} | {_md_text(entry['test_id'])} "
            f"| {_md_text(entry['headline'])} | {formats} |"
        )
    lines.append("")
    return "\n".join(lines)


def render_record_set(
    records: List[Dict[str, Any]],
    source_digest: str,
    total_failures: int,
    truncated: bool = False,
    *,
    source_kind: Optional[str] = None,
) -> Dict[str, Any]:
    """Build the record-set index for a set of ALREADY-PROJECTED records
    (each re-validated here against the same oracle before it is indexed).
    Returns ``{"index": <dict>, "index.json": <str>, "index.md": <str>}``.
    Deterministic: the same records + the same source digest = the same bytes.
    No wall clock, no aggregate score."""
    for record in records:
        validate_record(record)
    index = build_record_index(
        records, source_digest, total_failures, truncated,
        source_kind=source_kind,
    )
    return {
        "index": index,
        "index.json": render_index_json(index),
        "index.md": render_index_md(index),
    }
