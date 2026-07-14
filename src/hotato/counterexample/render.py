"""Deterministic human renderers over the canonical capsule model."""

from __future__ import annotations

import html
from typing import Any, Dict


def _pct(before: int, after: int) -> str:
    if before <= 0:
        return "n/a"
    return f"{((before - after) / before) * 100.0:.1f}%"


def _visible(value: Any, limit: int = 180) -> str:
    """Bound untrusted labels and render terminal/markup controls visibly."""
    out = []
    for char in str(value):
        code = ord(char)
        out.append(char if 32 <= code != 127 else f"\\u{code:04x}")
    text = "".join(out)
    return text if len(text) <= limit else text[: limit - 1] + "…"


def _markdown_code(value: Any) -> str:
    return _visible(value).replace("`", "\\`")


def render_markdown(capsule: Dict[str, Any]) -> str:
    target = capsule["target"]
    initial = capsule["reduction"]["initial"]
    final = capsule["reduction"]["final"]
    minimality = capsule["minimality"]
    lines = [
        f"# Counterexample: `{_markdown_code(target['assertion_id'])}`",
        "",
        f"- Dimension: `{_markdown_code(target.get('dimension') or 'ungrouped')}`",
        f"- Assertion kind: `{_markdown_code(target['kind'])}`",
        "- Authority: `deterministic`",
        f"- Failure fingerprint: `{target['fingerprint']}`",
        f"- Minimality: `{minimality['status']}` under `{minimality['reducer_set']}`",
        f"- Candidate evaluations: `{capsule['reduction']['attempts']}`",
        f"- Source qualification: `{capsule['preservation']['source_matching_failures']}/{capsule['preservation']['source_executions']}` matching failures",
        f"- Final verification: `{capsule['preservation']['final_matching_failures']}/{capsule['preservation']['final_executions']}` matching failures",
        "",
        "## Reduction",
        "",
        "| Measure | Source | Reduced | Reduction |",
        "|---|---:|---:|---:|",
    ]
    for key, label in (
        ("bytes", "Scenario bytes"),
        ("turns", "Caller turns"),
        ("trace_spans", "Trace spans"),
        ("tools", "Mock tool calls"),
        ("state_leaves", "State leaves"),
    ):
        before = int(initial.get(key, 0))
        after = int(final.get(key, 0))
        lines.append(f"| {label} | {before} | {after} | {_pct(before, after)} |")
    lines.append("")
    if capsule.get("privacy", {}).get("profile") == "private-runnable-v1":
        lines.extend([
            "## Reproduce",
            "",
            "```bash",
            "./reproduce.sh",
            "git bisect run ./predicate.sh  # Hotato evaluator/scenario behavior",
            "```",
            "",
        ])
    else:
        lines.extend([
            "## Reproduction boundary",
            "",
            "This share-safe projection omits the runnable scenario and scripts. Reproduce from the corresponding private capsule.",
            "",
        ])
    lines.extend([
        "The claim is local: the reduced input reproduces the same typed deterministic failure and is 1-minimal only when the recorded final deletion pass completed. It does not establish root cause or a global minimum.",
        "",
    ])
    return "\n".join(lines)


def render_html(capsule: Dict[str, Any]) -> str:
    target = capsule["target"]
    initial = capsule["reduction"]["initial"]
    final = capsule["reduction"]["final"]
    rows = []
    for key, label in (
        ("bytes", "Scenario bytes"),
        ("turns", "Caller turns"),
        ("trace_spans", "Trace spans"),
        ("tools", "Mock tool calls"),
        ("state_leaves", "State leaves"),
    ):
        before = int(initial.get(key, 0))
        after = int(final.get(key, 0))
        rows.append(
            f"<tr><th>{html.escape(label)}</th><td>{before}</td><td>{after}</td>"
            f"<td>{html.escape(_pct(before, after))}</td></tr>"
        )
    return """<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Hotato counterexample</title><style>
:root{color-scheme:dark;--bg:#0b0d10;--panel:#13171c;--ink:#f5f1e8;--muted:#9da6af;--ember:#ff6b2c;--line:#2a3139}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--ink);font:15px/1.55 ui-monospace,SFMono-Regular,Menlo,monospace}
main{max-width:920px;margin:0 auto;padding:56px 24px}.eyebrow{color:var(--ember);text-transform:uppercase;letter-spacing:.12em;font-size:12px}
h1{font:700 clamp(28px,5vw,52px)/1.04 system-ui,sans-serif;margin:.3em 0}.fingerprint{overflow-wrap:anywhere;color:var(--muted)}
.grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(190px,1fr));gap:12px;margin:28px 0}.card,table{background:var(--panel);border:1px solid var(--line);border-radius:12px}.card{padding:18px}.label{color:var(--muted);font-size:12px}.value{font-size:18px;margin-top:5px}
table{border-collapse:separate;border-spacing:0;width:100%;overflow:hidden}th,td{text-align:left;padding:12px;border-bottom:1px solid var(--line)}tr:last-child th,tr:last-child td{border-bottom:0}code{color:#ff9b70}
</style></head><body><main>""" + f"""
<div class="eyebrow">Hotato · proof-preserving counterexample</div>
<h1>{html.escape(_visible(target['assertion_id']))}</h1>
<p class="fingerprint">{html.escape(target['fingerprint'])}</p>
<div class="grid">
 <div class="card"><div class="label">Dimension</div><div class="value">{html.escape(_visible(target.get('dimension') or 'ungrouped'))}</div></div>
 <div class="card"><div class="label">Assertion</div><div class="value">{html.escape(_visible(target['kind']))}</div></div>
 <div class="card"><div class="label">Minimality</div><div class="value">{html.escape(capsule['minimality']['status'])}</div></div>
 <div class="card"><div class="label">Final replay</div><div class="value">{capsule['preservation']['final_matching_failures']}/{capsule['preservation']['final_executions']}</div></div>
</div>
<table><thead><tr><th>Measure</th><th>Source</th><th>Reduced</th><th>Reduction</th></tr></thead><tbody>{''.join(rows)}</tbody></table>
<p>{'Run <code>./reproduce.sh</code> from this private capsule.' if capsule.get('privacy', {}).get('profile') == 'private-runnable-v1' else 'This share-safe projection is non-runnable; use the corresponding private capsule.'} The verifier requires the same typed deterministic failure; an inconclusive candidate never counts.</p>
</main></body></html>"""


def render_svg(capsule: Dict[str, Any]) -> str:
    target = capsule["target"]
    initial = capsule["reduction"]["initial"]
    final = capsule["reduction"]["final"]
    title = html.escape(_visible(target["assertion_id"]))
    fingerprint = html.escape(target["fingerprint"])
    status = html.escape(capsule["minimality"]["status"])
    turns = f"{initial.get('turns', 0)} -> {final.get('turns', 0)} turns"
    spans = f"{initial.get('trace_spans', 0)} -> {final.get('trace_spans', 0)} spans"
    return f"""<svg xmlns="http://www.w3.org/2000/svg" width="1200" height="630" viewBox="0 0 1200 630" role="img" aria-labelledby="t d">
<title id="t">Hotato counterexample: {title}</title><desc id="d">Same deterministic failure, reduced under {status}</desc>
<defs><linearGradient id="g" x1="0" y1="0" x2="1" y2="1"><stop stop-color="#ff6b2c"/><stop offset="1" stop-color="#ffb36b"/></linearGradient></defs>
<rect width="1200" height="630" fill="#0b0d10"/><rect x="58" y="56" width="1084" height="518" rx="26" fill="#13171c" stroke="#2a3139"/>
<text x="96" y="116" fill="url(#g)" font-family="ui-monospace,monospace" font-size="22" font-weight="700">HOTATO · COUNTEREXAMPLE</text>
<text x="96" y="210" fill="#f5f1e8" font-family="system-ui,sans-serif" font-size="54" font-weight="750">{title}</text>
<text x="96" y="264" fill="#9da6af" font-family="ui-monospace,monospace" font-size="18">{html.escape(_visible(target.get('dimension') or 'ungrouped'))} · {html.escape(_visible(target['kind']))} · deterministic</text>
<rect x="96" y="318" width="300" height="112" rx="14" fill="#0b0d10"/><text x="120" y="354" fill="#9da6af" font-family="ui-monospace,monospace" font-size="16">INPUT</text><text x="120" y="394" fill="#f5f1e8" font-family="ui-monospace,monospace" font-size="24">{html.escape(turns)}</text>
<rect x="420" y="318" width="300" height="112" rx="14" fill="#0b0d10"/><text x="444" y="354" fill="#9da6af" font-family="ui-monospace,monospace" font-size="16">EVIDENCE</text><text x="444" y="394" fill="#f5f1e8" font-family="ui-monospace,monospace" font-size="24">{html.escape(spans)}</text>
<rect x="744" y="318" width="360" height="112" rx="14" fill="#0b0d10"/><text x="768" y="354" fill="#9da6af" font-family="ui-monospace,monospace" font-size="16">MINIMALITY</text><text x="768" y="394" fill="#ff9b70" font-family="ui-monospace,monospace" font-size="24">{status}</text>
<text x="96" y="494" fill="#707b86" font-family="ui-monospace,monospace" font-size="14">{fingerprint}</text>
<text x="96" y="536" fill="#f5f1e8" font-family="ui-monospace,monospace" font-size="18">Same typed failure. Every accepted deletion re-evaluated.</text></svg>"""
