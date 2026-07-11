"""``hotato fleet trend``: a self-contained static HTML page of turn-taking
trend lines, built by reading the fleet SQLite registry (``hotato fleet``'s
local control plane, see ``registry.py``). A pure reporting view -- it never
writes to the registry, never labels, and never deploys.

Three views, all re-derived from stored measurements, never invented:

  1. per-agent talk-over / time-to-yield trend lines (p50 and p95 per day)
  2. candidate moments discovered per day
  3. experiment outcomes (improved / inconclusive / refused) per agent

Every number traces back to a candidate's ``measured_json`` (written by
``FleetAPI.discover`` straight from ``scan.scan_recording``) or a trial's
stored verdict (written by ``FleetAPI.experiment_run``). Nothing here re-scores
audio; it reads what was already measured and stored.

Day, not "run": the registry does not carry an explicit run/batch id linking
recordings ingested in the same ``fleet run`` invocation, so each series is
bucketed by the UTC calendar day its rows were created on. That is the "per
run/day" granularity this page renders.

Talk-over / time-to-yield are re-derived only from
``overlap_while_agent_talking`` candidates (the scanner kind that means "the
caller became active while the agent was talking"): the measured overlap
(``durations.overlap_sec``) is talk_over_sec, and, only when the agent went
quiet inside the scanner's search window, how long that took
(``agent_reaction.after_sec``) is time_to_yield_sec. A candidate that never
carries these fields (a different kind, or the agent never went quiet)
contributes nothing to that series -- never a fabricated zero.

HONESTY, stated once and repeated on the page: a day with no discovered
candidates or no completed trial has no bar and no point. A series with fewer
than 2 days of data renders "not enough history to trend" instead of a line --
one point cannot show a trend, and this page never stretches it into one or
interpolates a value for a day nothing was measured on.
"""
from __future__ import annotations

import json
import time
from typing import Optional

from .. import report as _report
from .._stats import dist_summary
from .registry import Registry

__all__ = ["collect", "build_trend_html", "DEFAULT_OUT"]

DEFAULT_OUT = "hotato-fleet-trend.html"

_C = _report._C
_esc = _report._esc

_W = 746  # same content width as report.py's timeline / aggregate.py's team trend


# --- data collection (pure reads; no re-scoring, no mutation) --------------

def _day(epoch: Optional[float]) -> Optional[str]:
    """UTC calendar day (YYYY-MM-DD) for a registry ``created_at`` epoch."""
    if epoch is None:
        return None
    return time.strftime("%Y-%m-%d", time.gmtime(epoch))


def _candidate_metrics(measured: dict):
    """Re-derive (talk_over_sec, time_to_yield_sec) from one candidate's stored
    ``measured_json``, straight from the scanner's own fields (see the module
    docstring). Returns (None, None) for any candidate kind that does not carry
    these measurements -- never backfilled from a different kind."""
    if not isinstance(measured, dict) or measured.get("kind") != "overlap_while_agent_talking":
        return None, None
    durations = measured.get("durations") or {}
    talk_over = durations.get("overlap_sec")
    reaction = measured.get("agent_reaction") or {}
    time_to_yield = (reaction.get("after_sec")
                     if reaction.get("went_silent_within_search") else None)
    return talk_over, time_to_yield


def _series(by_day: dict) -> list:
    """[{day, n, p50, p95}] sorted by day, one row per day that has >=1 real
    sample (``_stats.dist_summary``'s own definitions). A day absent from
    ``by_day`` never appears here -- no zero-fill, no interpolation."""
    out = []
    for day in sorted(by_day):
        d = dist_summary(by_day[day])
        if d is None:
            continue
        out.append({"day": day, "n": d["n"], "p50": d["median"], "p95": d["p95"]})
    return out


_OUTCOME_KEYS = ("improved", "inconclusive", "refused")


def collect(registry: Registry, workspace_id: str) -> dict:
    """Read every agent's candidates + trials for ``workspace_id`` and bucket
    them per day. A pure read over the registry; never mutates it."""
    agents = registry.list_agents(workspace_id)
    out_agents = []
    for a in agents:
        agent_id = a["agent_id"]
        cand_rows = registry._all(
            "SELECT candidate_id, measured_json, created_at FROM candidates "
            "WHERE workspace_id=? AND agent_id=? ORDER BY created_at",
            (workspace_id, agent_id))
        by_day_count: dict = {}
        by_day_tov: dict = {}
        by_day_tty: dict = {}
        for r in cand_rows:
            day = _day(r["created_at"])
            if day is None:
                continue
            by_day_count[day] = by_day_count.get(day, 0) + 1
            try:
                measured = json.loads(r["measured_json"] or "{}")
            except (TypeError, ValueError):
                measured = {}
            tov, tty = _candidate_metrics(measured)
            if tov is not None:
                by_day_tov.setdefault(day, []).append(tov)
            if tty is not None:
                by_day_tty.setdefault(day, []).append(tty)

        trial_rows = registry._all(
            "SELECT trial_id, verdict, created_at FROM trials "
            "WHERE workspace_id=? AND agent_id=? ORDER BY created_at",
            (workspace_id, agent_id))
        outcomes = {k: 0 for k in _OUTCOME_KEYS}
        outcomes["other"] = 0
        for t in trial_rows:
            v = t["verdict"]
            if v in outcomes:
                outcomes[v] += 1
            else:
                outcomes["other"] += 1  # e.g. "created": precommitted, not yet run

        out_agents.append({
            "agent_id": agent_id,
            "name": a.get("name") or agent_id,
            "stack": a.get("stack"),
            "candidates_total": len(cand_rows),
            "candidates_per_day": sorted(by_day_count.items()),
            "talk_over_sec": _series(by_day_tov),
            "time_to_yield_sec": _series(by_day_tty),
            "trials_total": len(trial_rows),
            "outcomes": outcomes,
        })
    return {
        "workspace_id": workspace_id,
        "generated_at": time.time(),
        "agents": out_agents,
    }


# --- SVG (hand-rendered, same theme as report.py's timeline/histogram) -----

def _svg_open(h: int, aria: str) -> str:
    return (f'<svg class="trend-svg" viewBox="0 0 {_W} {h}" width="{_W}" '
            f'height="{h}" role="img" aria-label="{_esc(aria)}" '
            f'font-family="ui-monospace, SFMono-Regular, Menlo, monospace">')


def _not_enough_history(label: str, rows: list, unit: str = "s") -> str:
    n = len(rows)
    detail = ""
    if n == 1:
        r = rows[0]
        detail = f' ({r["day"]}: p50 {r["p50"]:.2f}{unit}, p95 {r["p95"]:.2f}{unit}, n={r["n"]})'
    return (f'<div class="nohist">{_esc(label)}: not enough history to trend '
            f'({n} day{"s" if n != 1 else ""} with data; need at least 2)'
            f'{detail}</div>')


def _svg_metric_series(rows: list, *, label: str, unit: str = "s") -> str:
    """p50 (solid) / p95 (dashed) line across the days that have >=1 real
    sample. Fewer than 2 such days: the honest-empty note, no line at all."""
    if len(rows) < 2:
        return _not_enough_history(label, rows, unit)

    gut, rpad, top = 54, 20, 16
    H = 170
    ay = H - 32
    pw = _W - gut - rpad
    n = max(1, len(rows) - 1)
    vmax = max(max(r["p50"], r["p95"]) for r in rows) or 1.0

    def X(i: int) -> float:
        return gut + (i / n) * pw

    def Y(v: float) -> float:
        return ay - (v / vmax) * (ay - top)

    aria = (f'{label} trend: {len(rows)} days with data from {rows[0]["day"]} '
            f'to {rows[-1]["day"]}, p50 solid line, p95 dashed line')
    p = [_svg_open(H, aria)]
    for frac in (0.0, 0.5, 1.0):
        y = ay - frac * (ay - top)
        p.append(f'<line x1="{gut}" y1="{y:.1f}" x2="{gut + pw}" y2="{y:.1f}" '
                 f'stroke="{_C["grid"]}" stroke-width="1" />')
        p.append(f'<text x="{gut - 8}" y="{y + 3:.1f}" fill="{_C["muted"]}" '
                 f'font-size="10" text-anchor="end">{frac * vmax:.2f}</text>')
    p50_path = " ".join(f"{X(i):.1f},{Y(r['p50']):.1f}" for i, r in enumerate(rows))
    p95_path = " ".join(f"{X(i):.1f},{Y(r['p95']):.1f}" for i, r in enumerate(rows))
    p.append(f'<polyline points="{p95_path}" fill="none" stroke="{_C["muted"]}" '
             f'stroke-width="1.6" stroke-dasharray="4 3" />')
    p.append(f'<polyline points="{p50_path}" fill="none" stroke="{_C["ember"]}" '
             f'stroke-width="2" />')
    show_all = len(rows) <= 8
    for i, r in enumerate(rows):
        p.append(f'<circle cx="{X(i):.1f}" cy="{Y(r["p50"]):.1f}" r="3.6" '
                 f'fill="{_C["ember"]}"><title>{_esc(r["day"])}: p50 '
                 f'{r["p50"]:.2f}{unit}, p95 {r["p95"]:.2f}{unit}, n={r["n"]}'
                 f'</title></circle>')
        p.append(f'<circle cx="{X(i):.1f}" cy="{Y(r["p95"]):.1f}" r="2.6" '
                 f'fill="{_C["muted"]}" />')
        if show_all or i in (0, len(rows) - 1):
            p.append(f'<text x="{X(i):.1f}" y="{ay + 16}" fill="{_C["muted"]}" '
                     f'font-size="9" text-anchor="middle">{r["day"][5:]}</text>')
    p.append(f'<text x="{gut + pw / 2:.1f}" y="{H - 4}" fill="{_C["muted"]}" '
             f'font-size="10" text-anchor="middle">{_esc(label)} per day '
             f'({unit}) &middot; solid p50, dashed p95</text>')
    p.append("</svg>")
    return f'<div class="tl">{"".join(p)}</div>'


def _svg_day_bars(rows: list, *, label: str = "candidate moments per day") -> str:
    """Candidate moments discovered per day; a day with none has no bar (never
    a synthetic zero-filled day)."""
    if len(rows) < 2:
        n = len(rows)
        detail = f' ({rows[0][0]}: {rows[0][1]})' if n == 1 else ""
        return (f'<div class="nohist">{_esc(label)}: not enough history to '
                f'trend ({n} day{"s" if n != 1 else ""} with data; need at '
                f'least 2){detail}</div>')

    gut, rpad, top = 44, 16, 14
    H = 140
    ay = H - 30
    pw = _W - gut - rpad
    n = len(rows)
    bw = pw / n
    cmax = max(c for _, c in rows) or 1
    aria = (f'{label}: {n} days with data, tallest day {cmax}')
    p = [_svg_open(H, aria)]
    p.append(f'<line x1="{gut}" y1="{ay}" x2="{gut + pw}" y2="{ay}" '
             f'stroke="{_C["grid"]}" stroke-width="1" />')
    show_all = n <= 10
    for i, (day, count) in enumerate(rows):
        x0 = gut + i * bw
        bh = max(2.0, (ay - top) * (count / cmax))
        p.append(f'<rect x="{x0 + 2:.1f}" y="{ay - bh:.1f}" '
                 f'width="{max(2.0, bw - 4):.1f}" height="{bh:.1f}" rx="2" '
                 f'fill="{_C["agent"]}" fill-opacity="0.85">'
                 f'<title>{_esc(day)}: {count} candidate moment'
                 f'{"s" if count != 1 else ""}</title></rect>')
        if show_all or i in (0, n - 1):
            p.append(f'<text x="{x0 + bw / 2:.1f}" y="{ay + 14}" '
                     f'fill="{_C["muted"]}" font-size="9" '
                     f'text-anchor="middle">{day[5:]}</text>')
    p.append(f'<text x="{gut + pw / 2:.1f}" y="{H - 4}" fill="{_C["muted"]}" '
             f'font-size="10" text-anchor="middle">{_esc(label)}</text>')
    p.append("</svg>")
    return f'<div class="tl">{"".join(p)}</div>'


def _outcomes_html(outcomes: dict, total: int) -> str:
    if total == 0:
        return '<div class="nohist">no experiment trials recorded yet for this agent.</div>'
    maxc = max(outcomes.values()) or 1
    rows = []
    for k in (*_OUTCOME_KEYS, "other"):
        c = outcomes.get(k, 0)
        if k == "other" and c == 0:
            continue  # nothing landed outside the three named verdicts
        w = max(6, int(220 * c / maxc)) if c else 0
        rows.append(
            '<div class="fcrow">'
            f'<span class="fck mono">{_esc(k)}</span>'
            f'<span class="fcbar" style="width:{w}px"></span>'
            f'<span class="fcn mono">{c} of {total}</span></div>'
        )
    return "".join(rows)


# --- page assembly ----------------------------------------------------------

_TREND_EXTRA_CSS = """
.nohist{color:%(muted)s;font-size:13px;font-style:italic;padding:10px 0}
.agentgrid{display:grid;gap:18px;margin-top:4px}
.stackpill{background:%(card2)s;border:1px solid %(line)s;border-radius:999px;
 padding:2px 10px;font-size:11.5px;color:%(muted)s;margin-left:8px}
.antitle{font-size:13px;font-weight:650;margin:16px 0 6px;color:%(cream)s}
"""


def _agent_card_html(agent: dict) -> str:
    body = [
        '<section class="card">',
        '<div class="chead"><div>',
        f'<div class="ctitle">{_esc(agent["name"])}'
        f'<span class="stackpill mono">{_esc(agent.get("stack") or "unknown stack")}'
        '</span></div>',
        '<div class="cmeta">'
        f'<span class="tag mono">{_esc(agent["agent_id"])}</span>'
        f'<span>{agent["candidates_total"]} candidate moment'
        f'{"s" if agent["candidates_total"] != 1 else ""}</span>'
        f'<span>{agent["trials_total"]} experiment trial'
        f'{"s" if agent["trials_total"] != 1 else ""}</span>'
        '</div></div></div>',
        '<div class="antitle">Talk-over (talk_over_sec)</div>',
        _svg_metric_series(agent["talk_over_sec"], label="talk-over"),
        '<div class="antitle">Time to yield (time_to_yield_sec)</div>',
        _svg_metric_series(agent["time_to_yield_sec"], label="time-to-yield"),
        '<div class="antitle">Candidate moments discovered</div>',
        _svg_day_bars(agent["candidates_per_day"]),
        '<div class="antitle">Experiment outcomes</div>',
        _outcomes_html(agent["outcomes"], agent["trials_total"]),
        '</section>',
    ]
    return "".join(body)


def build_trend_html(data: dict) -> str:
    """Render the full self-contained ``hotato fleet trend`` page. Offline,
    zero external requests: the CSS is inlined, every chart is hand-rendered
    inline SVG, and no ``<script>``/``<link>``/network reference appears."""
    css = _report._CSS + (_TREND_EXTRA_CSS % _C)
    ws = data["workspace_id"]
    agents = data["agents"]
    total_candidates = sum(a["candidates_total"] for a in agents)
    total_trials = sum(a["trials_total"] for a in agents)

    body = [
        '<main class="wrap">',
        '<header class="top"><div class="logo"></div><div>',
        '<h1 class="h1">hotato fleet trend</h1>',
        f'<div class="tagline">Turn-taking trend across every agent in '
        f'workspace {_esc(ws)}, from the fleet registry.</div>',
        '<div class="subtle">Candidate-moment measurements you review and '
        'label, never a decided verdict. Day-bucketed; never interpolated '
        'across a missing run.</div>',
        '<div class="metarow">'
        f'<span class="pill"><b>{len(agents)}</b> agent'
        f'{"s" if len(agents) != 1 else ""}</span>'
        f'<span class="pill"><b>{total_candidates}</b> candidate moment'
        f'{"s" if total_candidates != 1 else ""}</span>'
        f'<span class="pill"><b>{total_trials}</b> experiment trial'
        f'{"s" if total_trials != 1 else ""}</span>'
        '<span class="pill">offline <b>yes</b></span>'
        '</div></div></header>',
    ]

    if not agents:
        body.append(
            '<section class="card"><div class="subtle">No agents registered '
            f'in workspace {_esc(ws)} yet: nothing to trend. Run '
            '<span class="mono">hotato fleet agent add</span> and '
            '<span class="mono">hotato fleet run</span> first.</div></section>'
        )
    else:
        body.append('<div class="agentgrid">')
        for agent in agents:
            body.append(_agent_card_html(agent))
        body.append('</div>')

    body.append(
        '<footer class="foot">'
        '<div class="fline"><b>Method.</b> Talk-over and time-to-yield are '
        'read from candidates the scanner already measured on ingest, never '
        're-scored here. Definitions: p50/p95 are '
        '<span class="mono">hotato._stats.dist_summary</span>\'s linear-'
        'interpolation percentiles.</div>'
        '<div class="fline"><b>Bucketing.</b> One point per UTC calendar day '
        'that has at least one measurement; a day with none has no point, '
        'and a series is only drawn as a line once it has 2 or more.</div>'
        '<div class="fline dim">Reproducible timing measurements, no '
        'accuracy score. Experiment outcomes are the stored trial verdicts, '
        'never re-derived on this page.</div>'
        '</footer>'
    )
    body.append('</main>')

    desc = (f"Self-contained hotato fleet trend dashboard for workspace {ws}: "
            f"{len(agents)} agents, {total_candidates} candidate moments, "
            f"{total_trials} experiment trials. Offline, no external assets.")
    return (
        "<!doctype html><html lang=\"en\"><head><meta charset=\"utf-8\">"
        "<meta name=\"viewport\" content=\"width=device-width, initial-scale=1\">"
        f"<title>hotato fleet trend: {_esc(ws)}</title>"
        f"<meta name=\"description\" content=\"{_esc(desc)}\">"
        f"<style>{css}</style></head><body>"
        + "".join(body)
        + "</body></html>\n"
    )
