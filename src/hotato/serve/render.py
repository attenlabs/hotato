"""Server-rendered HTML for the five workspace views, in the report house style.

MIRRORS ``hotato.report``'s visual system rather than reinventing it: it imports
that module's colour tokens (``_C``), HTML escaper (``_esc``) and finished
stylesheet fragments (``_CSS`` + the per-feature CSS blocks) so a workspace page
and a conversation report read as one product -- warm-charcoal ground, cream
text, ember accent, monospace for every number, and the same green/red/ember
PASS/FAIL/INCONCLUSIVE semantics. Those imports are wrapped in a defensive
fallback so this module keeps working even if the (separately-owned) report
internals shift; the fallback reproduces the same tokens.

Every renderer takes a model dict from :mod:`hotato.serve.data` and returns an
HTML string. NO renderer computes a number -- it only formats the honest
per-dimension data the data layer produced (no blended score can be introduced
here). All artifact/user text is escaped with ``_esc``; trace text honours the
``text_redacted`` flag so redacted spans never leak into the page.
"""
from __future__ import annotations

import html as _html
import json
from typing import Any, List, Optional

# --- house style, imported from the report with a same-look fallback ---------
try:
    from ..report import (
        _C as _C,
        _esc as _esc,
        _CSS as _REPORT_CSS,
        _TRACE_CSS as _TRACE_CSS,
        _TRANSCRIPT_CSS as _TRANSCRIPT_CSS,
        _CONVERSATION_CSS as _CONVERSATION_CSS,
        _SCORECARD_CSS as _SCORECARD_CSS,
        _ASSERTIONS_CSS as _ASSERTIONS_CSS,
    )
    _HAVE_REPORT = True
except Exception:  # pragma: no cover - report is a hard sibling; fallback is belt-and-braces
    _HAVE_REPORT = False
    _C = {
        "bg": "#1b1714", "card": "#241f1a", "card2": "#2b241d", "line": "#3a3128",
        "cream": "#f1e8d7", "muted": "#b7ab97", "mono": "#f6eddd",
        "caller": "#ead9a6", "agent": "#7fb2c4", "ember": "#f0663a",
        "green": "#74c98a", "red": "#e0664f", "grid": "#463b30",
    }

    def _esc(x) -> str:
        return _html.escape("" if x is None else str(x))

    _REPORT_CSS = (
        ":root{color-scheme:dark}*{box-sizing:border-box}"
        "body{margin:0;background:%(bg)s;color:%(cream)s;"
        "font-family:ui-sans-serif,system-ui,-apple-system,'Segoe UI',Roboto,sans-serif;"
        "font-size:15px;line-height:1.5}"
        ".mono{font-family:ui-monospace,SFMono-Regular,Menlo,Consolas,monospace}"
        ".wrap{max-width:980px;margin:0 auto;padding:28px 20px 56px}"
        ".card{background:%(card)s;border:1px solid %(line)s;border-radius:16px;"
        "padding:18px 20px;margin-bottom:18px}"
        ".pill{background:%(card2)s;border:1px solid %(line)s;border-radius:999px;"
        "padding:3px 11px;font-size:12px;color:%(muted)s}"
        ".chip{color:#15110d;font-weight:800;font-size:12.5px;letter-spacing:0.06em;"
        "padding:5px 12px;border-radius:8px}.chip.small{padding:2px 9px;font-size:11px;border-radius:6px}"
        ".foot{margin-top:26px;border-top:1px solid %(line)s;padding-top:18px;"
        "color:%(cream)s;font-size:13px}"
    ) % _C
    _TRACE_CSS = _TRANSCRIPT_CSS = _CONVERSATION_CSS = _SCORECARD_CSS = _ASSERTIONS_CSS = ""


# Extra CSS for the workspace chrome (nav tabs, matrix, clusters, health) --
# built from the SAME _C tokens so it stays inside the house palette. The report
# is 860px-wide single-column; the workspace tables are wider, so `.wrap` is
# widened here and dense tables scroll inside their own overflow container.
_WORKSPACE_CSS = ("""
.wrap{max-width:1040px}
.wsbar{display:flex;flex-wrap:wrap;gap:8px;align-items:center;margin:2px 0 4px}
.wsbar .pill b{color:%(cream)s;font-weight:600}
nav.tabs{display:flex;flex-wrap:wrap;gap:2px;border-bottom:1px solid %(line)s;
 margin:14px 0 22px}
nav.tabs a{color:%(muted)s;text-decoration:none;font-size:13.5px;font-weight:600;
 padding:9px 14px;border-bottom:2px solid transparent;margin-bottom:-1px}
nav.tabs a:hover{color:%(cream)s}
nav.tabs a.active{color:%(cream)s;border-bottom-color:%(ember)s}
h2.vh{font-size:18px;font-weight:700;margin:0 0 4px}
.vsub{color:%(muted)s;font-size:13px;margin:0 0 14px}
.grid{display:flex;flex-wrap:wrap;gap:10px 22px}
.kv{display:flex;flex-direction:column;gap:2px;min-width:120px}
.kv .k{font-size:11.5px;color:%(muted)s;text-transform:lowercase}
.kv .v{font-size:16px;font-weight:650;
 font-family:ui-monospace,SFMono-Regular,Menlo,Consolas,monospace}
.flag{display:inline-block;background:%(card2)s;border:1px solid %(ember)s;
 color:%(ember)s;border-radius:6px;padding:1px 8px;font-size:11px;
 font-weight:700;margin-left:8px}
.dash{color:%(muted)s;font-family:ui-monospace,SFMono-Regular,Menlo,Consolas,monospace}
.tablewrap{overflow-x:auto;padding-bottom:4px}
table.ws{border-collapse:collapse;font-size:12.5px;width:100%%}
table.ws th{text-align:left;color:%(muted)s;font-weight:600;font-size:11.5px;
 padding:7px 14px 7px 0;border-bottom:1px solid %(line)s;white-space:nowrap}
table.ws td{text-align:left;padding:7px 14px 7px 0;border-bottom:1px solid %(card2)s;
 vertical-align:top}
table.ws td.mono,table.ws th.mono{
 font-family:ui-monospace,SFMono-Regular,Menlo,Consolas,monospace}
.dimrow{display:flex;flex-wrap:wrap;gap:5px}
.dimtag{display:inline-flex;align-items:center;gap:5px;background:%(card2)s;
 border:1px solid %(line)s;border-radius:6px;padding:1px 6px;font-size:10.5px;
 color:%(muted)s}
.dot{width:8px;height:8px;border-radius:50%%;display:inline-block}
.rel{font-family:ui-monospace,SFMono-Regular,Menlo,Consolas,monospace;font-size:12px}
form.filters{display:flex;flex-wrap:wrap;gap:8px;align-items:flex-end;margin:0 0 16px}
form.filters label{display:flex;flex-direction:column;gap:3px;font-size:11px;
 color:%(muted)s}
form.filters input,form.filters select{background:%(card2)s;color:%(cream)s;
 border:1px solid %(line)s;border-radius:7px;padding:5px 8px;font-size:12.5px;
 font-family:ui-monospace,SFMono-Regular,Menlo,Consolas,monospace}
form.filters button{background:%(ember)s;color:#15110d;border:none;border-radius:7px;
 padding:6px 14px;font-size:12.5px;font-weight:700;cursor:pointer}
.clbar{display:inline-block;height:12px;border-radius:4px;background:%(ember)s;
 opacity:0.85;vertical-align:-1px}
.cldim{color:%(muted)s;font-size:11.5px;margin:2px 0 0 0}
.members{margin:8px 0 0;padding-left:18px;color:%(cream)s;font-size:12px}
.members a{color:%(agent)s}
.hseries{margin:10px 0 4px}
.hbar-row{display:flex;align-items:center;gap:8px;margin:3px 0;font-size:12px}
.hbar-day{min-width:92px;color:%(muted)s;
 font-family:ui-monospace,SFMono-Regular,Menlo,Consolas,monospace}
.hbar{display:inline-block;height:11px;border-radius:3px;background:%(red)s;opacity:0.8}
.nohist{color:%(muted)s;font-size:12px;font-style:italic;margin:4px 0}
.notice{background:%(card2)s;border:1px solid %(line)s;border-left:3px solid %(ember)s;
 border-radius:10px;padding:10px 13px;font-size:12.5px;color:%(cream)s;margin:6px 0}
.dg{font-family:ui-monospace,SFMono-Regular,Menlo,Consolas,monospace;font-size:11px;
 color:%(muted)s;word-break:break-all}
.dg a{color:%(agent)s}
.origin.real{color:%(caller)s}.origin.simulated{color:%(agent)s}
/* origin badge: inline-block so the padded pill always wraps its label (no clip),
   real and simulated carry distinct track colours and always dark, legible text */
.cvchip{display:inline-block;line-height:1.5;vertical-align:1px;white-space:nowrap;
 color:#15110d;font-weight:800;margin-left:6px}
.cvchip.origin.real,.cvchip.origin.simulated,.cvchip.origin.muted{color:#15110d}
section.card.conversation .cvorigin{display:flex;align-items:center;gap:2px}
a.drill{color:%(agent)s;text-decoration:none}a.drill:hover{text-decoration:underline}
""") % _C


_STATUS_COLOR = {"PASS": "green", "FAIL": "red", "INCONCLUSIVE": "ember",
                 "ERROR": "red", "NOT_RUN": "muted", "UNAVAILABLE": "muted"}

# The top tabs. The conversation inspector + a single record are drill-ins.
_TABS = (
    ("/", "Release readiness"),
    ("/scenarios", "Scenario matrix"),
    ("/clusters", "Failure clusters"),
    ("/health", "Production health"),
    ("/records", "Failure records"),
)


# =========================================================================
# atoms
# =========================================================================

def _status_chip(status: Optional[str]) -> str:
    """A semantic PASS/FAIL/INCONCLUSIVE chip in the report palette; a muted dash
    when a dimension carries no evaluation (honest 'no data', not a fake pass)."""
    if not status:
        return '<span class="dash">-</span>'
    color = _C.get(_STATUS_COLOR.get(status, "muted"), _C["muted"])
    return f'<span class="chip small" style="background:{color}">{_esc(status)}</span>'


def _dot(status: Optional[str]) -> str:
    color = _C.get(_STATUS_COLOR.get(status or "", "muted"), _C["muted"])
    return f'<span class="dot" style="background:{color}"></span>'


def _origin_bg(origin: Optional[str]) -> str:
    """Badge background for a conversation origin. Real uses the human-track
    colour, simulated the machine-track colour, so the two never read as one
    bucket; anything unspecified stays muted. Text on all three is dark."""
    if origin == "real":
        return _C["caller"]
    if origin == "simulated":
        return _C["agent"]
    return _C["muted"]


def _dim_row(per_dim: dict) -> str:
    """The five dimensions as compact coloured tags (never combined)."""
    tags = []
    for d in ("outcome", "policy", "conversation", "speech", "reliability"):
        st = per_dim.get(d)
        tags.append(f'<span class="dimtag">{_dot(st)}{_esc(d)}</span>')
    return '<div class="dimrow">' + "".join(tags) + "</div>"


def _pct(x: Optional[float]) -> str:
    return "-" if x is None else f"{x * 100:.0f}%"


def _reliability_cell(rel: Optional[dict]) -> str:
    if not rel or rel.get("reps", 0) == 0:
        return '<span class="dash">-</span>'
    reps = rel["reps"]
    scored = rel.get("scored", 0)
    passed = rel.get("passed", 0)
    flag = '<span class="flag">low sample, N=%d</span>' % reps if reps < 2 else ""
    passk = "pass^%d ✓" % scored if rel.get("pass_all") else "pass^%d ✗" % scored
    rate = _pct(rel.get("rate"))
    return (f'<span class="mono">{passed}/{scored} pass · {rate}</span> '
            f'<span class="mono" style="color:{_C["muted"]}">({passk})</span>{flag}')


def _drill(conversation_id: str, label: Optional[str] = None) -> str:
    cid = _esc(conversation_id)
    return f'<a class="drill" href="/conversation/{cid}">{_esc(label or conversation_id)}</a>'


# =========================================================================
# page scaffold
# =========================================================================

def _active_attr(href, active):
    """The active-tab attribute, kept OUT of the f-string expression: an f-string
    expression part cannot include a backslash on Python < 3.12 (PEP 701)."""
    return ' class="active"' if href == active else ""


def page(title: str, active: str, body: str, *, workspace: str) -> str:
    """Assemble a full, self-contained HTML document: all CSS inline (report
    fragments + workspace chrome), no external request, the report's ember logo
    + header, the four-tab nav, the view body, and the read-only footer."""
    css = (_REPORT_CSS + _TRACE_CSS + _TRANSCRIPT_CSS + _CONVERSATION_CSS
           + _SCORECARD_CSS + _ASSERTIONS_CSS + _WORKSPACE_CSS)
    tabs = "".join(
        f'<a href="{href}"{_active_attr(href, active)}>{_esc(label)}</a>'
        for href, label in _TABS
    )
    return (
        "<!doctype html>\n<html lang=\"en\"><head>"
        "<meta charset=\"utf-8\">"
        "<meta name=\"viewport\" content=\"width=device-width, initial-scale=1\">"
        "<meta name=\"referrer\" content=\"no-referrer\">"
        f"<title>{_esc(title)} · hotato workspace</title>"
        f"<style>{css}</style></head><body><div class=\"wrap\">"
        "<header class=\"top\" style=\"display:flex;align-items:flex-start;gap:14px;"
        f"border-bottom:1px solid {_C['line']};padding-bottom:16px;margin-bottom:8px\">"
        f"<div class=\"logo\" style=\"width:30px;height:30px;border-radius:9px;"
        f"background:{_C['ember']};flex:none;margin-top:2px\"></div>"
        "<div><div class=\"h1\" style=\"font-size:22px;font-weight:700;margin:0\">"
        "hotato workspace</div>"
        f"<div class=\"wsbar\"><span class=\"pill\">workspace <b>{_esc(workspace)}</b></span>"
        "<span class=\"pill\">self-hosted · read-only</span></div></div></header>"
        f"<nav class=\"tabs\">{tabs}</nav>"
        f"{body}{_footer()}"
        "</div></body></html>"
    )


def _footer() -> str:
    return (
        '<div class="foot">'
        '<div class="fline"><b>Read-only v1.</b> This server issues only SELECTs '
        'against your workspace; reviews and labels stay CLI-driven '
        '(<span class="mono">hotato fleet review</span> / '
        '<span class="mono">hotato label</span>). The only file it writes is the '
        'append-only audit log.</div>'
        '<div class="fline" style="color:%s">No telemetry, no external calls. '
        'Evidence (audio, traces, evaluations) stays on this machine. Every '
        'dimension is scored separately; there is no combined number.</div>'
        '</div>' % _C["muted"]
    )


# =========================================================================
# View 1 -- Release readiness
# =========================================================================

def render_release_readiness(m: dict) -> str:
    cur_rel = m.get("current_release")
    cur = m["current"]
    parts = ['<h2 class="vh">Release readiness</h2>',
             '<p class="vsub">The pre-ship home screen: does the current release '
             'clear its required suites, and what changed since the last one? '
             'Each dimension is scored on its own.</p>']

    if not cur_rel:
        parts.append('<div class="notice">No releases recorded yet in this '
                     'workspace. Add one with <span class="mono">hotato release</span> '
                     'and attach runs to see readiness here.</div>')
        return "".join(parts)

    sparse = ('<span class="flag">low sample, N=%d</span>' % cur["sample_n"]
              if cur.get("sparse") else "")
    parts.append('<section class="card">')
    parts.append('<div class="grid">')
    parts.append(_kv("current release", cur_rel["release_id"], mono=True))
    if cur_rel.get("model"):
        parts.append(_kv("model", cur_rel["model"], mono=True))
    parts.append(_kv("scenarios", str(cur["scenarios"])))
    parts.append(_kv("runs", str(cur["runs"])))
    parts.append(_kv("conversations", str(cur["conversations"]), raw_suffix=sparse))
    parts.append(_kv("inconclusive", str(cur["inconclusive_total"])))
    parts.append('</div>')

    # failures BY DIMENSION (never summed into one number)
    parts.append('<div class="cvsub" style="margin-top:16px">failures by '
                 'dimension</div><div class="dimrow">')
    for d in ("outcome", "policy", "conversation", "speech", "reliability"):
        c = cur["dim_counts"][d]
        st = None
        if c["FAIL"]:
            st = "FAIL"
        elif c["INCONCLUSIVE"] and not c["PASS"]:
            st = "INCONCLUSIVE"
        elif c["PASS"]:
            st = "PASS"
        parts.append(
            f'<span class="dimtag">{_dot(st)}{_esc(d)} '
            f'<span class="mono">{c["PASS"]}P/{c["FAIL"]}F/{c["INCONCLUSIVE"]}I</span></span>')
    parts.append('</div>')

    # origin split -- real and simulated ALWAYS separate
    osplit = cur["origin_split"]
    parts.append('<div class="cvsub" style="margin-top:16px">origin split '
                 '(never merged)</div><div class="dimrow">')
    for origin, n in osplit.items():
        cls = origin if origin in ("real", "simulated") else "muted"
        parts.append(f'<span class="dimtag"><span class="origin {_esc(cls)}">'
                     f'{_esc(origin)}</span> <span class="mono">{n}</span></span>')
    parts.append('</div>')
    parts.append('</section>')

    # required suites completeness
    parts.append('<section class="card"><div class="ctitle" '
                 'style="font-size:15px;font-weight:650;margin-bottom:8px">'
                 'Required suites</div>')
    req = m.get("required_suites") or []
    if not req:
        parts.append('<div class="cldim">No suites are marked '
                     '<span class="mono">required_for_release</span>.</div>')
    else:
        parts.append('<div class="tablewrap"><table class="ws"><thead><tr>'
                     '<th>suite</th><th>coverage</th><th>inconclusive policy</th>'
                     '<th>status</th></tr></thead><tbody>')
        for s in req:
            st = "PASS" if s["complete"] else "INCONCLUSIVE"
            parts.append(
                f'<tr><td class="mono">{_esc(s["suite_id"])}</td>'
                f'<td class="mono">{s["covered"]}/{s["scenarios"]} scenarios run</td>'
                f'<td class="mono">{_esc(s.get("inconclusive_policy"))}</td>'
                f'<td>{_status_chip(st)}</td></tr>')
        parts.append('</tbody></table></div>')
    parts.append('</section>')

    # new-vs-fixed since previous release
    parts.append('<section class="card"><div class="ctitle" '
                 'style="font-size:15px;font-weight:650;margin-bottom:8px">'
                 'Since previous release</div>')
    if not m.get("comparable_to_previous"):
        parts.append('<div class="cldim">No previous release to compare against.</div>')
    else:
        prev = m.get("previous_release") or {}
        parts.append('<div class="cldim" style="margin-bottom:8px">baseline: '
                     f'<span class="mono">{_esc(prev.get("release_id"))}</span> '
                     '&middot; compared per (scenario, dimension).</div>')
        parts.append(_change_list("New failures (regressions)", m["new_failures"], "FAIL"))
        parts.append(_change_list("Fixed since previous", m["fixed"], "PASS"))
    parts.append('</section>')

    return "".join(parts)


def _change_list(title: str, items: List[dict], status: str) -> str:
    if not items:
        return f'<div class="cldim">{_esc(title)}: none.</div>'
    rows = "".join(
        f'<li>{_status_chip(status)} <span class="mono">{_esc(i["scenario_id"])}</span> '
        f'&middot; <span class="rel">{_esc(i["dimension"])}</span></li>'
        for i in items
    )
    return (f'<div class="cvsub" style="margin-top:10px">{_esc(title)} '
            f'<span class="mono">({len(items)})</span></div>'
            f'<ul class="members">{rows}</ul>')


def _kv(label: str, value: str, *, mono: bool = False, raw_suffix: str = "") -> str:
    """A key/value tile. ``value`` is escaped; ``raw_suffix`` is appended as raw
    HTML (for pre-built markup like a low-sample flag) after the escaped value."""
    cls = "v mono" if mono else "v"
    return (f'<div class="kv"><span class="k">{_esc(label)}</span>'
            f'<span class="{cls}">{_esc(value)}{raw_suffix}</span></div>')


# =========================================================================
# View 2 -- Scenario matrix
# =========================================================================

def render_scenario_matrix(m: dict) -> str:
    f = m["filters"]
    parts = ['<h2 class="vh">Scenario matrix</h2>',
             '<p class="vsub">Scenarios &times; current/previous release, each '
             'dimension scored on its own, plus reliability (pass^k where a '
             'scenario has repetitions).</p>']

    # filter form (GET, query-param driven)
    parts.append(
        '<form class="filters" method="get" action="/scenarios">'
        f'<label>agent<input name="agent" value="{_esc(f.get("agent") or "")}" '
        'placeholder="agent id"></label>'
        f'<label>release<input name="release" value="{_esc(f.get("release") or "")}" '
        'placeholder="release id"></label>'
        f'<label>suite<input name="suite" value="{_esc(f.get("suite") or "")}" '
        'placeholder="suite id"></label>'
        '<label>status<select name="status">'
        + _opt("", f.get("status")) + _opt("PASS", f.get("status"))
        + _opt("FAIL", f.get("status")) + _opt("INCONCLUSIVE", f.get("status"))
        + '</select></label>'
        '<button type="submit">filter</button></form>'
    )

    parts.append('<div class="cldim" style="margin-bottom:10px">current: '
                 f'<span class="rel">{_esc(m.get("current_release"))}</span> '
                 f'&middot; previous: <span class="rel">{_esc(m.get("previous_release"))}</span> '
                 f'&middot; <span class="mono">{m["row_count"]}</span> scenarios</div>')

    if not m["rows"]:
        parts.append('<div class="notice">No scenarios match. Add scenarios with '
                     '<span class="mono">hotato scenario init</span> or clear the '
                     'filters.</div>')
        return "".join(parts)

    parts.append('<div class="tablewrap"><table class="ws"><thead><tr>'
                 '<th>scenario</th><th>dimensions (current)</th><th>reliability</th>'
                 '<th>prev</th><th>agents</th></tr></thead><tbody>')
    for row in m["rows"]:
        cur = row["current"]
        prev = row["previous"]
        prev_agg = _status_chip(prev["aggregate"]) if prev.get("release_id") else '<span class="dash">-</span>'
        agents = ", ".join(_esc(a) for a in cur.get("agents") or []) or '<span class="dash">-</span>'
        goal = f'<div class="cldim">{_esc(row.get("goal"))}</div>' if row.get("goal") else ""
        parts.append(
            f'<tr><td class="mono">{_esc(row["scenario_id"])}{goal}</td>'
            f'<td>{_dim_row(cur["per_dim"])}</td>'
            f'<td>{_reliability_cell(cur.get("reliability"))}</td>'
            f'<td>{prev_agg}</td>'
            f'<td class="mono">{agents}</td></tr>')
    parts.append('</tbody></table></div>')
    return "".join(parts)


def _opt(value: str, current: Optional[str]) -> str:
    sel = " selected" if (current or "") == value else ""
    label = value or "(any)"
    return f'<option value="{_esc(value)}"{sel}>{_esc(label)}</option>'


# =========================================================================
# View 3 -- Conversation inspector
# =========================================================================

def render_conversation_inspector(m: dict) -> str:
    conv = m["conversation"]
    parts = [f'<h2 class="vh">Conversation <span class="mono">{_esc(conv["conversation_id"])}</span></h2>',
             '<p class="vsub">One conversation: its evidence manifest, '
             'transcript, trace, per-dimension evaluations and reviewer decisions. '
             'Every verdict links back to its source artifact.</p>']

    origin = m.get("origin") or "unspecified"
    ocls = origin if origin in ("real", "simulated") else "muted"
    parts.append('<section class="card conversation">')
    parts.append(f'<div class="cvorigin">origin '
                 f'<span class="cvchip origin {_esc(ocls)}" '
                 f'style="background:{_origin_bg(origin)}">{_esc(origin)}</span></div>')
    # lineage + digests (drill-to-evidence targets)
    parts.append('<table class="cvtab"><tbody>')
    parts.append(_cvrow("agent", conv.get("agent_id")))
    if m.get("run"):
        parts.append(_cvrow("run", m["run"].get("run_id")))
        parts.append(_cvrow("seed", m["run"].get("seed")))
        parts.append(_cvrow("provider route", m["run"].get("provider_route")))
    if m.get("scenario"):
        parts.append(_cvrow("scenario", m["scenario"].get("scenario_id")))
        parts.append(_cvrow("goal", m["scenario"].get("goal")))
    if m.get("release"):
        parts.append(_cvrow("release", m["release"].get("release_id")))
    parts.append(_cvrow_digest("artifact digest", m.get("artifact_digest")))
    parts.append(_cvrow("capture receipt", conv.get("capture_receipt")))
    parts.append('</tbody></table>')

    # manifest origin/provenance if the artifact resolved
    ev = m.get("evidence_status")
    man = m.get("manifest")
    if ev == "resolved" and isinstance(man, dict):
        parts.append('<div class="cvsub">artifact manifest '
                     '(<span class="mono">conversation.v1</span>, digest-bound)</div>')
        parts.append('<table class="cvtab"><tbody>')
        morigin = man.get("origin") or {}
        parts.append(_cvrow("manifest origin.kind", morigin.get("kind")))
        if morigin.get("provider"):
            parts.append(_cvrow("provider", morigin.get("provider")))
        if morigin.get("caller"):
            parts.append(_cvrow("caller", morigin.get("caller")))
        if isinstance(morigin.get("simulator"), dict):
            sim = morigin["simulator"]
            parts.append(_cvrow("simulator", json.dumps(sim, sort_keys=True)))
        for name, ref in (man.get("artifacts") or {}).items():
            sha = (ref or {}).get("sha256")
            parts.append('<tr><td>%s</td><td>%s</td></tr>' % (
                _esc(name + " sha256"), _digest_link(sha)))
        parts.append('</tbody></table>')
    elif ev == "unresolved":
        parts.append('<div class="notice">A conversation artifact digest is '
                     'bound to this row, but the artifact is not present in this '
                     'workspace\'s store, so transcript and trace cannot be '
                     'shown. (Evidence is refused, never fabricated.)</div>')
    else:
        parts.append('<div class="cldim">No conversation artifact is bound to '
                     'this row yet.</div>')
    parts.append('</section>')

    # per-dimension scorecard from evaluations (no blend)
    parts.append(_inspector_scorecard(m.get("evaluations") or [],
                                      m.get("assertion_runs") or []))

    # transcript (redaction respected)
    parts.append(_render_transcript(m.get("transcript")))
    # trace (respect text_redacted -> [redacted])
    parts.append(_render_trace(m.get("trace")))

    return "".join(parts)


def _cvrow(label: str, value: Any) -> str:
    if value in (None, ""):
        return ""
    return f'<tr><td>{_esc(label)}</td><td class="mono">{_esc(value)}</td></tr>'


def _cvrow_digest(label: str, digest: Optional[str]) -> str:
    if not digest:
        return ""
    return f'<tr><td>{_esc(label)}</td><td>{_digest_link(digest)}</td></tr>'


def _digest_link(digest: Optional[str]) -> str:
    """A digest rendered as a link to the raw evidence blob (drill-to-evidence).
    Only 64-hex digests become links; anything else renders as opaque text."""
    if isinstance(digest, str) and len(digest) == 64 and all(
            c in "0123456789abcdef" for c in digest):
        return f'<span class="dg"><a href="/evidence/{digest}">{digest}</a></span>'
    return f'<span class="dg">{_esc(digest)}</span>'


def _inspector_scorecard(evals: List[dict], assertion_runs: List[dict]) -> str:
    """The five-dimension scorecard for one conversation: per-dimension
    evaluation verdicts with rationale/citations, plus the deterministic vs
    model-judged assertion lanes kept visibly separate (invariant 2). No
    dimension is combined with another."""
    parts = ['<section class="card"><div class="scnote">Per-dimension results. '
             'Deterministic checks and model-judged/advisory results are shown in '
             'separate lanes; each dimension stands on its own.</div>'
             '<div class="scorecard">']
    by_dim = {d: [] for d in ("outcome", "policy", "conversation", "speech", "reliability")}
    other = []
    for e in evals:
        (by_dim[e["dimension"]] if e.get("dimension") in by_dim else other).append(e)
    ar_by_dim = {}
    for a in assertion_runs:
        ar_by_dim.setdefault(a.get("dimension"), []).append(a)

    for d in ("outcome", "policy", "conversation", "speech", "reliability"):
        ev = by_dim.get(d) or []
        ars = ar_by_dim.get(d) or []
        if not ev and not ars:
            parts.append(f'<div class="scdim"><div class="schead">'
                         f'<span class="scname">{_esc(d)}</span>'
                         f'<span class="sccounts">no evaluation</span></div></div>')
            continue
        parts.append('<div class="scdim"><div class="schead">'
                     f'<span class="scname">{_esc(d)}</span>'
                     f'<span class="sccounts mono">{_dim_counts_text(ev + ars)}</span></div>')
        for e in ev:
            parts.append(_eval_card(e))
        for a in ars:
            parts.append(_assertion_run_card(a))
        parts.append('</div>')

    for e in other:
        parts.append(_eval_card(e))
    parts.append('</div></section>')
    return "".join(parts)


def _dim_counts_text(items: List[dict]) -> str:
    c = {"PASS": 0, "FAIL": 0, "INCONCLUSIVE": 0}
    for i in items:
        if i.get("status") in c:
            c[i["status"]] += 1
    return f'{c["PASS"]} pass / {c["FAIL"]} fail / {c["INCONCLUSIVE"]} inconclusive'


def _eval_card(e: dict) -> str:
    refs = e.get("evidence_refs")
    cites = _render_citations(refs)
    reviews = e.get("reviews") or []
    rv = ""
    if reviews:
        items = "".join(
            f'<li><span class="mono">{_esc(r.get("reviewer"))}</span>: '
            f'{_esc(r.get("decision"))}'
            + (f' &middot; {_esc(r.get("rationale"))}' if r.get("rationale") else "")
            + f' <span class="cldim">[{_esc(r.get("adjudication_state") or "review")}]</span></li>'
            for r in reviews)
        rv = f'<ul class="members">{items}</ul>'
    prov = e.get("provenance")
    prov_html = ""
    if prov:
        prov_html = (f'<div class="jprov mono">provenance: '
                     f'{_esc(json.dumps(prov, sort_keys=True) if not isinstance(prov, str) else prov)}</div>')
    return (
        '<div class="acard">'
        '<div class="achead"><div>'
        f'<span class="kindtag mono">{_esc(e.get("evaluator_id") or "evaluation")}</span> '
        f'<span class="mono aid">{_esc(e.get("evaluation_id"))}</span></div>'
        f'{_status_chip(e.get("status"))}</div>'
        f'{cites}{prov_html}{rv}</div>'
    )


def _assertion_run_card(a: dict) -> str:
    lane = "deterministic" if a.get("deterministic") else "model-judged / advisory"
    reason = f'<div class="asrtreason">{_esc(a.get("reason"))}</div>' if a.get("reason") else ""
    cites = _render_citations(a.get("evidence_refs"))
    return (
        '<div class="acard">'
        '<div class="achead"><div>'
        f'<span class="kindtag mono">{_esc(a.get("kind") or "assertion")}</span> '
        f'<span class="mono aid">{_esc(a.get("assertion_id"))}</span> '
        f'<span class="detflag mono">{_esc(lane)}</span></div>'
        f'{_status_chip(a.get("status"))}</div>'
        f'{reason}{cites}</div>'
    )


def _render_citations(refs: Any) -> str:
    """Render evaluation/assertion evidence refs as drill-to-evidence links: a
    64-hex ref becomes a link to the raw blob; other refs render as text so the
    citation is always visible."""
    if not refs:
        return ""
    items = refs if isinstance(refs, list) else [refs]
    out = []
    for r in items:
        if isinstance(r, dict):
            out.append('<span class="dg">' + _esc(json.dumps(r, sort_keys=True)) + '</span>')
        else:
            s = str(r)
            # accept "sha256:<hex>" or bare hex
            hexpart = s.split(":")[-1]
            out.append(_digest_link(hexpart) if (len(hexpart) == 64 and all(
                c in "0123456789abcdef" for c in hexpart)) else f'<span class="dg">{_esc(s)}</span>')
    return '<div class="jcite">evidence: ' + " · ".join(out) + "</div>"


def _render_transcript(transcript: Any) -> str:
    """Transcript panel in the report style. Segments may be a list of
    ``{start,end,text}`` (or ``{t0,t1,text}``) or a dict wrapping ``segments``.
    A segment flagged ``redacted`` shows ``[redacted]`` rather than its text."""
    segs = _transcript_segments(transcript)
    if segs is None:
        return ('<section class="card"><div class="cvsub">transcript</div>'
                '<div class="cldim">No transcript artifact bound to this '
                'conversation.</div></section>')
    if not segs:
        return ('<section class="card"><div class="cvsub">transcript</div>'
                '<div class="cldim">Transcript present but empty.</div></section>')
    rows = []
    for s in segs:
        t0 = s.get("start", s.get("t0"))
        t1 = s.get("end", s.get("t1"))
        stamp = ""
        if t0 is not None:
            stamp = f'{_fmt_t(t0)}-{_fmt_t(t1)}' if t1 is not None else _fmt_t(t0)
        redacted = s.get("redacted") or s.get("text_redacted")
        text = "[redacted]" if redacted else (s.get("text") or "")
        spk = f'<span class="tt mono">{_esc(s.get("speaker"))}</span> ' if s.get("speaker") else ""
        rows.append(f'<div class="trow"><span class="tt mono">{_esc(stamp)}</span>'
                    f'{spk}<span class="tx">{_esc(text)}</span></div>')
    return ('<details class="card transcript" open><summary>transcript</summary>'
            '<div class="tnote">Context for the evaluation, not itself a score.</div>'
            f'<div class="trows">{"".join(rows)}</div></details>')


def _transcript_segments(transcript: Any) -> Optional[list]:
    if transcript is None:
        return None
    if isinstance(transcript, dict):
        for key in ("segments", "utterances", "turns"):
            if isinstance(transcript.get(key), list):
                return transcript[key]
        return []
    if isinstance(transcript, list):
        return transcript
    return []


def _fmt_t(x: Any) -> str:
    try:
        return f"{float(x):.2f}s"
    except (TypeError, ValueError):
        return _esc(x)


def _render_trace(trace: Any) -> str:
    """Trace-span table in the report style. Honours ``text_redacted`` on any
    span (renders ``[redacted]`` for that span's text/detail) so redacted
    content never reaches the page."""
    spans = _trace_spans(trace)
    if spans is None:
        return ('<section class="card"><div class="cvsub">trace</div>'
                '<div class="cldim">No trace artifact bound to this '
                'conversation.</div></section>')
    if not spans:
        return ('<section class="card"><div class="cvsub">trace</div>'
                '<div class="anempty">No spans in this trace.</div></section>')
    rows = []
    for sp in spans:
        redacted = sp.get("text_redacted")
        detail = "[redacted]" if redacted else _span_detail(sp)
        rows.append(
            '<tr>'
            f'<td>{_esc(sp.get("type") or sp.get("kind") or "")}</td>'
            f'<td>{_esc(sp.get("name") or "")}</td>'
            f'<td>{_esc(_span_time(sp, "start"))}</td>'
            f'<td>{_esc(_span_time(sp, "end"))}</td>'
            f'<td>{_esc(detail)}</td></tr>')
    return ('<details class="card trace" open><summary>trace (context, not a score)</summary>'
            '<div class="tnote">Tool/turn events. Redacted spans show '
            '<span class="mono">[redacted]</span>.</div>'
            '<div class="anwrap"><table class="tracetab mono"><thead><tr>'
            '<th>span</th><th>name</th><th>start</th><th>end</th><th>detail</th>'
            f'</tr></thead><tbody>{"".join(rows)}</tbody></table></div></details>')


def _trace_spans(trace: Any) -> Optional[list]:
    if trace is None:
        return None
    if isinstance(trace, dict):
        for key in ("spans", "events"):
            if isinstance(trace.get(key), list):
                return trace[key]
        return []
    if isinstance(trace, list):
        return trace
    return []


def _span_time(sp: dict, which: str) -> str:
    aliases = {"start": ("start", "start_sec", "t0", "ts"),
               "end": ("end", "end_sec", "t1", "te")}
    for key in aliases.get(which, (which,)):
        if key in sp and sp[key] is not None:
            return _fmt_t(sp[key])
    return ""


def _span_detail(sp: dict) -> str:
    for key in ("detail", "text", "summary", "arguments", "result"):
        if key in sp and sp[key] not in (None, ""):
            v = sp[key]
            return v if isinstance(v, str) else json.dumps(v, sort_keys=True)
    return ""


# =========================================================================
# View 4 -- Failure clusters
# =========================================================================

def render_failure_clusters(m: dict) -> str:
    f = m["filters"]
    parts = ['<h2 class="vh">Failure clusters</h2>',
             '<p class="vsub">%s: failed evaluations and assertions grouped by '
             '(dimension + assertion kind + reason-class). This is what was '
             '<i>observed</i>, deliberately not a claim about causality.</p>'
             % _esc(m.get("label", "clusters by observable signature"))]

    parts.append(
        '<form class="filters" method="get" action="/clusters">'
        f'<label>dimension<input name="dimension" value="{_esc(f.get("dimension") or "")}" '
        'placeholder="outcome…"></label>'
        f'<label>kind<input name="kind" value="{_esc(f.get("kind") or "")}" '
        'placeholder="tool_result…"></label>'
        '<button type="submit">filter</button></form>'
    )

    parts.append(f'<div class="cldim" style="margin-bottom:10px">'
                 f'<span class="mono">{m["cluster_count"]}</span> clusters over '
                 f'<span class="mono">{m["failure_total"]}</span> failed records.</div>')

    if not m["clusters"]:
        parts.append('<div class="notice">No failures in scope. Either nothing '
                     'has failed, or your filters exclude everything.</div>')
        return "".join(parts)

    maxc = max(c["count"] for c in m["clusters"]) or 1
    for c in m["clusters"]:
        width = int(round(220 * c["count"] / maxc))
        members = c["members"][:25]
        mlist = "".join(
            _cluster_member(mem) for mem in members)
        more = ("" if len(c["members"]) <= 25
                else f'<li class="cldim">… and {len(c["members"]) - 25} more</li>')
        parts.append(
            '<section class="card">'
            '<div class="fcrow">'
            f'<span class="fck">{_status_chip("FAIL")} <span class="rel">{_esc(c["dimension"])}</span> '
            f'&middot; <span class="mono">{_esc(c["kind"])}</span></span>'
            f'<span class="clbar" style="width:{width}px"></span>'
            f'<span class="fcn mono">{c["count"]}</span></div>'
            f'<div class="cldim">lane: <span class="mono">{_esc(c["lane"])}</span> '
            f'&middot; signature: <span class="mono">{_esc(c["reason_class"])}</span></div>'
            f'<ul class="members">{mlist}{more}</ul>'
            '</section>')
    return "".join(parts)


def _cluster_member(mem: dict) -> str:
    cid = mem.get("conversation_id")
    link = _drill(cid) if cid else '<span class="dash">(no conversation)</span>'
    extra = []
    if mem.get("assertion_id"):
        extra.append('assert <span class="mono">%s</span>' % _esc(mem["assertion_id"]))
    if mem.get("evaluation_id"):
        extra.append('eval <span class="mono">%s</span>' % _esc(mem["evaluation_id"]))
    if mem.get("reason"):
        extra.append('&middot; %s' % _esc(mem["reason"]))
    tail = (" &middot; " + " ".join(extra)) if extra else ""
    return f'<li>{link}{tail}</li>'


# =========================================================================
# View 5 -- Production health
# =========================================================================

def render_production_health(m: dict) -> str:
    parts = ['<h2 class="vh">Production health</h2>',
             '<p class="vsub">Ingest volume, evaluated coverage, and per-dimension '
             'failure rate over time; real and simulated kept strictly '
             'apart, no combined number.</p>']

    parts.append('<section class="card"><div class="grid">')
    parts.append(_kv("ingested (total)", str(m["ingested_total"])))
    parts.append(_kv("days of history", str(m["days_of_history"])))
    parts.append('</div></section>')

    if not m["enough_history"] and m["ingested_total"] == 0:
        parts.append('<div class="notice">No conversations ingested yet. Pull '
                     'production calls with <span class="mono">hotato production '
                     'ingest</span> to populate health.</div>')
        return "".join(parts)

    for origin, stats in m["origins"].items():
        if stats["ingested"] == 0:
            continue
        cls = origin if origin in ("real", "simulated") else "muted"
        parts.append('<section class="card">')
        parts.append(f'<div class="cvorigin">origin '
                     f'<span class="cvchip origin {_esc(cls)}" '
                     f'style="background:{_origin_bg(origin)}">{_esc(origin)}</span></div>')
        parts.append('<div class="grid" style="margin-top:8px">')
        parts.append(_kv("ingested", str(stats["ingested"])))
        parts.append(_kv("evaluated", str(stats["evaluated"])))
        parts.append(_kv("coverage", _pct(stats["coverage"])))
        parts.append('</div>')

        parts.append('<div class="cvsub" style="margin-top:14px">failure rate by '
                     'dimension over time</div>')
        series = stats.get("series") or {}
        for d in ("outcome", "policy", "conversation", "speech", "reliability"):
            s = series.get(d)
            parts.append(f'<div class="hseries"><div class="cldim">'
                         f'<span class="rel">{_esc(d)}</span></div>')
            if not s or not s.get("enough_history"):
                nd = (s or {}).get("days_with_data", 0)
                parts.append(f'<div class="nohist">not enough history to trend '
                             f'({nd} day(s) with data; need at least 2)</div></div>')
                continue
            for pt in s["points"]:
                width = int(round(200 * pt["rate"]))
                parts.append(
                    '<div class="hbar-row">'
                    f'<span class="hbar-day">{_esc(pt["day"])}</span>'
                    f'<span class="hbar" style="width:{width}px"></span>'
                    f'<span class="mono">{_pct(pt["rate"])} '
                    f'({pt["fail"]}/{pt["total"]})</span></div>')
            parts.append('</div>')
        parts.append('</section>')

    if m.get("release_markers"):
        parts.append('<section class="card"><div class="cvsub">release markers</div>'
                     '<div class="dimrow">')
        for mk in m["release_markers"]:
            parts.append(f'<span class="dimtag"><span class="rel">{_esc(mk["release_id"])}</span> '
                         f'<span class="mono">{_esc(mk.get("day") or "-")}</span></span>')
        parts.append('</div></section>')
    return "".join(parts)


# =========================================================================
# View 6 -- Failure records (read-only viewer over hotato.failure-record.v1)
# =========================================================================

_RECORD_LANES = ("outcome", "policy", "conversation", "speech", "reliability")


def _record_origin_cls(origin: Optional[str]) -> str:
    """A record's origin is ``captured`` or ``simulated`` (never ``real`` -- that
    axis belongs to conversations). Simulated keeps the machine-track colour; a
    captured or unspecified origin stays muted."""
    return "simulated" if origin == "simulated" else "muted"


def render_records_list(m: dict) -> str:
    """The read-only Failure Record index. Each row keeps its five dimensions
    separate and the deterministic gate apart from the model advisory; there is
    no combined number. An empty workspace renders an explicit empty state, never
    a fabricated record."""
    parts = ['<h2 class="vh">Failure records</h2>',
             '<p class="vsub">Share-safe Failure Records projected from failing '
             'results. Each keeps its five dimensions separate, with the '
             'deterministic gate shown apart from the model advisory. Read-only.</p>']

    records = m.get("records") or []
    if not records:
        parts.append('<div class="notice">No failure records in this workspace. '
                     'Render one with <span class="mono">hotato record render '
                     'RESULT --out DIR</span> and place it under the workspace '
                     '<span class="mono">records/</span> directory to view it '
                     'here.</div>')
        return "".join(parts)

    parts.append(f'<div class="cldim" style="margin-bottom:10px">'
                 f'<span class="mono">{m.get("record_count", len(records))}</span> '
                 'records.</div>')
    parts.append('<div class="tablewrap"><table class="ws"><thead><tr>'
                 '<th>record</th><th>status</th><th>dimensions</th>'
                 '<th>gate</th><th>advisory</th><th>origin</th>'
                 '</tr></thead><tbody>')
    for r in records:
        rid = _esc(r.get("record_id_ref"))
        head = (f'<div class="cldim">{_esc(r.get("headline"))}</div>'
                if r.get("headline") else "")
        subj = (f'<div class="cldim">{_esc(r.get("test_id"))}</div>'
                if r.get("test_id") else "")
        origin = r.get("origin")
        parts.append(
            f'<tr><td class="mono"><a class="drill" href="/records/{rid}">{rid}</a>'
            f'{subj}{head}</td>'
            f'<td>{_status_chip(r.get("status"))}</td>'
            f'<td>{_dim_row(r.get("lane_status") or {})}</td>'
            f'<td>{_status_chip(r.get("gate_status"))}</td>'
            f'<td class="mono">{_esc(r.get("advisory_status"))}</td>'
            f'<td><span class="origin {_record_origin_cls(origin)}">'
            f'{_esc(origin)}</span></td></tr>')
    parts.append('</tbody></table></div>')
    return "".join(parts)


def _record_lane_observed(dim: dict) -> str:
    assertions = dim.get("assertions") or []
    if not assertions:
        return "No assertion ran in this dimension."
    return assertions[0].get("observed") or ""


def _record_reliability_line(rel: dict) -> str:
    if not rel or rel.get("pass_at_1") is None:
        return ("no repetition data (trials=%s); rates and interval are not "
                "shown for a single run" % (rel or {}).get("trials", 0))
    interval = rel.get("wilson_interval")
    line = ("pass@1=%.3f pass@k=%.3f pass^k=%.3f (%s of %s trials passed)" % (
        rel["pass_at_1"], rel["pass_at_k"], rel["pass_caret_k"],
        rel.get("passes"), rel.get("trials")))
    if interval:
        line += ("; %g%% Wilson interval [%.6f, %.6f]" % (
            interval["confidence"] * 100, interval["lower"], interval["upper"]))
    return line


def render_record_detail(record: dict) -> str:
    """One Failure Record in the workspace chrome: its headline, the five lanes
    each with its own status and observed line, the deterministic gate shown
    apart from the model advisory, reliability with denominators, the evidence
    references (id, kind, relative locator, clipped digest -- never a payload),
    and the relative reproduction command. Every record string is escaped; the
    page carries no script and no remote asset. The record is already share-safe
    (validated: no absolute path, no embedded payload), so every field shown is
    safe to display."""
    subj = record.get("subject") or {}
    gate = record.get("gate") or {}
    advisory = record.get("advisory") or {}
    dims = record.get("dimensions") or {}

    parts = [
        '<h2 class="vh">Failure record '
        f'<span class="mono">{_esc(subj.get("test_id"))}</span></h2>',
        f'<p class="vsub">{_esc(record.get("headline"))}</p>',
    ]

    # summary card
    parts.append('<section class="card"><div class="grid">')
    parts.append(_kv("status", record.get("status") or "-"))
    parts.append(_kv("origin", (record.get("origin") or {}).get("kind") or "-"))
    parts.append(_kv("gate", gate.get("status") or "-"))
    parts.append(_kv("advisory", advisory.get("status") or "-"))
    parts.append('</div>')
    parts.append('<div class="dg" style="margin-top:12px">record id: '
                 f'{_esc(record.get("record_id"))}</div>')
    parts.append('</section>')

    # five lanes, each scored on its own (no blend)
    parts.append('<section class="card"><div class="cvsub">Five dimensions, each '
                 'scored on its own (never combined)</div>'
                 '<div class="tablewrap"><table class="ws"><thead><tr>'
                 '<th>dimension</th><th>status</th><th>observed</th>'
                 '</tr></thead><tbody>')
    for lane in _RECORD_LANES:
        d = dims.get(lane) or {}
        parts.append(
            f'<tr><td class="rel">{_esc(lane)}</td>'
            f'<td>{_status_chip(d.get("status"))}</td>'
            f'<td>{_esc(_record_lane_observed(d))}</td></tr>')
    parts.append('</tbody></table></div></section>')

    # gate authority (deterministic) shown APART from the model advisory
    adv_gate = "enabled" if advisory.get("gate_enabled") else "not enabled"
    adv_reason = (f' &middot; reason <span class="mono">'
                  f'{_esc(advisory.get("reason_code"))}</span>'
                  if advisory.get("reason_code") else "")
    parts.append('<section class="card"><div class="cvsub">Gate authority</div>'
                 '<div class="cldim">Deterministic gate: '
                 f'{_status_chip(gate.get("status"))} &middot; policy '
                 f'<span class="mono">{_esc(gate.get("policy"))}</span> &middot; '
                 f'exit <span class="mono">{_esc(gate.get("exit_code"))}</span></div>'
                 '<div class="cldim" style="margin-top:6px">Model advisory: '
                 f'{_status_chip(advisory.get("status"))} &middot; gate '
                 f'<span class="mono">{_esc(adv_gate)}</span>{adv_reason}</div>'
                 '</section>')

    # reliability, values copied with denominators
    rel = dims.get("reliability") or {}
    parts.append('<section class="card"><div class="cvsub">Reliability</div>'
                 f'<div class="cldim mono">{_esc(_record_reliability_line(rel))}</div>'
                 '</section>')

    # evidence references: id / kind / relative locator / clipped digest -- no payloads
    evidence = record.get("evidence") or []
    parts.append('<section class="card"><div class="cvsub">Evidence references</div>')
    if not evidence:
        parts.append('<div class="cldim">No evidence entries.</div>')
    else:
        parts.append('<div class="tablewrap"><table class="ws"><thead><tr>'
                     '<th>reference</th><th>kind</th><th>locator</th><th>digest</th>'
                     '</tr></thead><tbody>')
        for item in evidence:
            digest = item.get("digest") or ""
            short = digest if len(digest) <= 27 else digest[:24] + "..."
            parts.append(
                f'<tr><td class="mono">{_esc(item.get("evidence_id"))}</td>'
                f'<td class="mono">{_esc(item.get("kind"))}</td>'
                f'<td class="mono">{_esc(item.get("locator") or "(digest only)")}</td>'
                f'<td class="dg">{_esc(short)}</td></tr>')
        parts.append('</tbody></table></div>')
    parts.append('</section>')

    # reproduction: relative argv only (the record guarantees no absolute path)
    repro = record.get("reproduction") or {}
    argv = repro.get("argv") or []
    cmd = " ".join(str(a) for a in argv)
    parts.append('<section class="card"><div class="cvsub">Reproduce</div>'
                 f'<div class="dg">{_esc(cmd)}</div></section>')
    return "".join(parts)


# =========================================================================
# error pages
# =========================================================================

def render_404(what: str) -> str:
    return (f'<h2 class="vh">Not found</h2><div class="notice">{_esc(what)}</div>')


_GATE_CSS = ("""
.gate{max-width:560px;margin:0 auto}
.gate .brand{display:flex;align-items:center;gap:12px;margin:8vh 0 22px}
.gate .mark{width:36px;height:36px;border-radius:11px;background:%(ember)s;flex:none;
 box-shadow:0 6px 18px -8px %(ember)s}
.gate h1{font-size:23px;font-weight:700;margin:0;letter-spacing:-0.01em}
.gate .lede{color:%(cream)s;font-size:14.5px;margin:0 0 22px;line-height:1.6}
.gate ol{margin:0 0 20px;padding-left:20px;line-height:1.75;font-size:14px;color:%(cream)s}
.gate ol li{margin:0 0 4px}
.gate code{color:%(ember)s;font-family:ui-monospace,SFMono-Regular,Menlo,Consolas,monospace;
 font-size:13px}
.gate .urlbox{background:%(card2)s;border:1px solid %(line)s;border-left:3px solid %(ember)s;
 border-radius:10px;padding:12px 14px;font-family:ui-monospace,SFMono-Regular,Menlo,Consolas,monospace;
 font-size:13px;color:%(cream)s;word-break:break-all;margin:0 0 22px}
.gate .note{color:%(muted)s;font-size:12.5px;border-top:1px solid %(line)s;padding-top:16px;
 line-height:1.65}
""") % _C


def _standalone_page(title: str, inner: str) -> str:
    """A self-contained, unauthenticated page in the workspace house style (warm
    charcoal ground, ember mark). It shares no workspace data and never shows the
    token; used for the courtesy landing page and the access-token notice."""
    css = _REPORT_CSS + _GATE_CSS
    return (
        "<!doctype html>\n<html lang=\"en\"><head>"
        "<meta charset=\"utf-8\">"
        "<meta name=\"viewport\" content=\"width=device-width, initial-scale=1\">"
        "<meta name=\"referrer\" content=\"no-referrer\">"
        f"<title>{_esc(title)} · hotato workspace</title>"
        f"<style>{css}</style></head><body><div class=\"wrap\">"
        f"<div class=\"gate\">{inner}</div></div></body></html>"
    )


def render_landing_html(*, workspace: str, host_display: str = "") -> str:
    """The courtesy home page shown when the workspace root is opened without a
    token. It welcomes the user, explains this is their local read-only workspace,
    and shows exactly how to get in. It reveals no token and no workspace data."""
    ws = _esc(workspace)
    host = _esc(host_display or "127.0.0.1:8321")
    inner = (
        '<div class="brand"><div class="mark"></div>'
        '<h1>hotato workspace</h1></div>'
        f'<p class="lede">This is your local workspace for <b>{ws}</b>. It runs on '
        'this machine and is read-only. Open it with the access link that hotato '
        'printed in your terminal.</p>'
        '<ol>'
        '<li>Switch to the terminal where you ran <code>hotato serve</code>.</li>'
        '<li>Open the link it printed (it looks like the one below), or click it.</li>'
        '<li>Closed that terminal? Run <code>hotato serve</code> again to reprint '
        'the link.</li>'
        '</ol>'
        f'<div class="urlbox">http://{host}/?token=…</div>'
        '<div class="note">The access token stays in your terminal and is never '
        'shown on this page. Nothing leaves this machine: the workspace is served '
        'locally, read-only, with no telemetry and no external calls.</div>'
    )
    return _standalone_page("Local workspace", inner)


def render_401_html(*, host_display: str = "") -> str:
    """The access-token notice returned for a token-gated path opened without a
    valid token (HTML clients). It points the user to the workspace home for
    step-by-step access and to the bearer header for API clients."""
    host = _esc(host_display or "127.0.0.1:8321")
    inner = (
        '<div class="brand"><div class="mark"></div>'
        '<h1>Access token needed</h1></div>'
        '<p class="lede">This view needs the workspace access token. Open the '
        'workspace home for step-by-step access, or send the token as a bearer '
        'header from an API client.</p>'
        f'<div class="urlbox">http://{host}/</div>'
        '<div class="note">Open <code>/</code> to see how to get in, or send '
        '<code>Authorization: Bearer &lt;token&gt;</code>. The workspace is local '
        'and read-only.</div>'
    )
    return _standalone_page("Access token needed", inner)
