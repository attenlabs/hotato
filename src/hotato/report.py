"""Interactive visual report: one self-contained HTML file per evaluation.

``hotato report`` turns a scored recording (or the bundled self-test battery)
into a single, offline, double-clickable HTML page. For every event it draws a
to-scale timeline from the REAL frame data the scorer already produced:

  * a caller activity track and an agent activity track, drawn to scale
  * the talk-over span shaded, labelled with its measured seconds
  * the caller-onset marker and the yield-point marker
  * expected-vs-actual (should yield / did yield) and a PASS/FAIL chip
  * the exact ScoreConfig thresholds used, so the page is reproducible

On top of the per-event cards the page carries an analytics block computed from
the same real measurements: a time-to-yield distribution strip, a talk-over
histogram, failure clustering by fix class, and a collapsible per-event frame
inspector (the full frame dump as a table). Pass ``base`` (a previous envelope)
to render per-scenario regression deltas with worse/better marks.

Every number on the page is a measurement the scorer emitted (verdict,
measurements, signals) or a frame from ``frame_dump``. Nothing is invented and
there is no accuracy percentage anywhere. The page is fully self-contained:
inline CSS, inline SVG, zero external requests, works offline by double-click.

``embed_audio=True`` (CLI ``--embed-audio``) additionally embeds the exact
audio the scorer measured under each timeline as a base64 WAV data URI with a
native player. The page stays one self-contained offline file; it just grows
by roughly the audio size. Any file over ``_EMBED_MAX_BYTES`` (~8 MB) is noted
in plain text and skipped, never truncated. Default off, so plain reports stay
small.

``build_report_md`` mirrors the same content as plain Markdown (tables instead
of SVG). A future PDF needs no new renderer: the HTML already ships print CSS,
so "print to PDF" from any browser produces the document.
"""

from __future__ import annotations

import base64
import html
import math
import os
from typing import Optional

from ._engine.score import ScoreConfig
from ._stats import dist_summary
from .core import (
    LIMITS,
    SUITE_ID,
    _bundled_audio_path,
    dump_frames_for_input,
    run_single,
    run_suite,
)

__all__ = ["build_report_html", "build_report_md", "write_report"]

# --- warm charcoal / cream / ember theme ----------------------------------
_C = {
    "bg": "#1b1714",        # warm charcoal ground
    "card": "#241f1a",
    "card2": "#2b241d",
    "line": "#3a3128",
    "cream": "#f1e8d7",     # primary text
    "muted": "#b7ab97",
    "mono": "#f6eddd",
    "caller": "#ead9a6",    # human track (warm cream)
    "agent": "#7fb2c4",     # machine track (calm blue)
    "ember": "#f0663a",     # accent + onset marker + talk-over
    "green": "#74c98a",     # PASS + yield marker
    "red": "#e0664f",       # FAIL
    "grid": "#463b30",
}


# --- span + model helpers -------------------------------------------------

def _spans(frames: list, key: str, hop: float) -> list:
    """Contiguous [start, end) runs where ``key`` is true, in seconds."""
    out = []
    start = None
    last = 0.0
    for f in frames:
        if f.get(key):
            if start is None:
                start = f["t_sec"]
            last = f["t_sec"]
        elif start is not None:
            out.append((start, last + hop))
            start = None
    if start is not None:
        out.append((start, last + hop))
    return out


def _talkover_spans(frames: list, hop: float, onset: Optional[float], end: float) -> list:
    """Both-active runs inside the [onset, end) overlap window the scorer used."""
    lo = onset if onset is not None else 0.0
    out = []
    start = None
    last = 0.0
    for f in frames:
        t = f["t_sec"]
        both = f.get("caller_active") and f.get("agent_active") and lo <= t < end
        if both:
            if start is None:
                start = t
            last = t
        elif start is not None:
            out.append((start, last + hop))
            start = None
    if start is not None:
        out.append((start, last + hop))
    return out


def _event_model(event: dict, frames: list, hop: float, cfg: ScoreConfig) -> dict:
    """Everything the renderer needs for one event, all from real measurements."""
    v = event["verdict"]
    m = event.get("measurements", {})
    onset = m.get("caller_onset_sec")
    onset = onset if (onset is not None and onset >= 0) else None
    stt = v.get("seconds_to_yield")
    did_yield = bool(v.get("did_yield"))

    if frames:
        duration = frames[-1]["t_sec"] + hop
    else:
        base = onset or 0.0
        duration = max(base + cfg.max_search_sec, 1.0)

    yield_abs = None
    if did_yield and onset is not None and stt is not None:
        yield_abs = onset + stt

    if yield_abs is not None:
        overlap_end = yield_abs
    else:
        overlap_end = min(duration, (onset or 0.0) + cfg.max_search_sec)

    sig = event.get("signals", {}) or {}
    latency = sig.get("latency", {}) if isinstance(sig, dict) else {}

    return {
        "event": event,
        "duration": duration,
        "hop": hop,
        "caller_spans": _spans(frames, "caller_active", hop),
        "agent_spans": _spans(frames, "agent_active", hop),
        "talkover_spans": _talkover_spans(frames, hop, onset, overlap_end),
        "onset": onset,
        "yield_abs": yield_abs,
        "did_yield": did_yield,
        "passed": bool(v.get("passed")),
        "expected_yield": bool(event.get("expected_yield", True)),
        "seconds_to_yield": stt,
        "talk_over_sec": v.get("talk_over_sec"),
        "response_gap_sec": latency.get("response_gap_sec"),
        "premature_start_sec": latency.get("premature_start_sec"),
        "has_frames": bool(frames),
        "frames": frames,
    }


# --- formatting -----------------------------------------------------------

def _s(x: Optional[float]) -> str:
    return "n/a" if x is None else f"{x:.2f}s"


def _esc(x) -> str:
    return html.escape("" if x is None else str(x))


# --- SVG timeline ---------------------------------------------------------

_GUT = 92       # left gutter for track labels
_PW = 624       # plot width
_RPAD = 30
_W = _GUT + _PW + _RPAD
_TOP_LABEL_Y = 12
_CALLER_Y = 22
_TRACK_H = 24
_AGENT_Y = 54
_MARK_TOP = 18
_MARK_BOT = 82
_AXIS_Y = 96
_TICK_LABEL_Y = 111
_H = 120


def _svg_timeline(model: dict) -> str:
    dur = model["duration"] or 1.0
    scale = _PW / dur

    def X(t: float) -> float:
        t = max(0.0, min(t, dur))
        return _GUT + t * scale

    p = []
    p.append(f'<svg class="tl-svg" viewBox="0 0 {_W} {_H}" width="{_W}" height="{_H}" '
             f'role="img" font-family="ui-monospace, SFMono-Regular, Menlo, monospace">')

    # second gridlines + axis ticks
    step = 1.0 if dur <= 9 else 2.0
    t = 0.0
    while t <= dur + 1e-9:
        x = X(t)
        p.append(f'<line x1="{x:.1f}" y1="{_CALLER_Y}" x2="{x:.1f}" y2="{_AXIS_Y}" '
                 f'stroke="{_C["grid"]}" stroke-width="1" />')
        p.append(f'<text x="{x:.1f}" y="{_TICK_LABEL_Y}" fill="{_C["muted"]}" '
                 f'font-size="10" text-anchor="middle">{t:.0f}s</text>')
        t += step

    # talk-over shaded band(s), behind the tracks
    for (a, b) in model["talkover_spans"]:
        x = X(a)
        w = max(1.5, X(b) - x)
        p.append(f'<rect x="{x:.1f}" y="{_MARK_TOP}" width="{w:.1f}" '
                 f'height="{_MARK_BOT - _MARK_TOP}" fill="{_C["ember"]}" '
                 f'fill-opacity="0.16" />')

    # track labels
    p.append(f'<text x="{_GUT - 12}" y="{_CALLER_Y + _TRACK_H / 2 + 4:.0f}" '
             f'fill="{_C["cream"]}" font-size="12" text-anchor="end">Caller</text>')
    p.append(f'<text x="{_GUT - 12}" y="{_AGENT_Y + _TRACK_H / 2 + 4:.0f}" '
             f'fill="{_C["cream"]}" font-size="12" text-anchor="end">Agent</text>')

    # baselines for empty tracks
    for y in (_CALLER_Y + _TRACK_H / 2, _AGENT_Y + _TRACK_H / 2):
        p.append(f'<line x1="{_GUT}" y1="{y:.0f}" x2="{_GUT + _PW}" y2="{y:.0f}" '
                 f'stroke="{_C["line"]}" stroke-width="1" />')

    # activity spans
    for (a, b) in model["caller_spans"]:
        x = X(a)
        w = max(1.5, X(b) - x)
        p.append(f'<rect x="{x:.1f}" y="{_CALLER_Y}" width="{w:.1f}" '
                 f'height="{_TRACK_H}" rx="4" fill="{_C["caller"]}" />')
    for (a, b) in model["agent_spans"]:
        x = X(a)
        w = max(1.5, X(b) - x)
        p.append(f'<rect x="{x:.1f}" y="{_AGENT_Y}" width="{w:.1f}" '
                 f'height="{_TRACK_H}" rx="4" fill="{_C["agent"]}" />')

    # onset marker (ember, dashed)
    onset = model["onset"]
    if onset is not None:
        x = X(onset)
        p.append(f'<line x1="{x:.1f}" y1="{_MARK_TOP}" x2="{x:.1f}" y2="{_MARK_BOT}" '
                 f'stroke="{_C["ember"]}" stroke-width="1.6" stroke-dasharray="3 3" />')
        p.append(f'<text x="{x - 4:.1f}" y="{_TOP_LABEL_Y}" fill="{_C["ember"]}" '
                 f'font-size="10" text-anchor="end">onset</text>')

    # yield marker (green, solid)
    ya = model["yield_abs"]
    if ya is not None:
        x = X(ya)
        p.append(f'<line x1="{x:.1f}" y1="{_MARK_TOP}" x2="{x:.1f}" y2="{_MARK_BOT}" '
                 f'stroke="{_C["green"]}" stroke-width="1.8" />')
        p.append(f'<text x="{x + 4:.1f}" y="{_TOP_LABEL_Y}" fill="{_C["green"]}" '
                 f'font-size="10" text-anchor="start">yield</text>')

    p.append("</svg>")
    return "".join(p)


# --- analytics (computed from the same real measurements) ------------------

def _analytics_data(env: dict, models: list) -> dict:
    """Aggregate the per-event measurements for the analytics block. Every value
    comes straight from the envelope / models; nothing is invented."""
    tty, no_yield, tov = [], [], []
    for m in models:
        sid = m["event"].get("scenario_id") or m["event"].get("event_id") or "event"
        if m["seconds_to_yield"] is not None:
            tty.append((sid, m["seconds_to_yield"]))
        else:
            no_yield.append(sid)
        tov.append((sid, m["talk_over_sec"] if m["talk_over_sec"] is not None else 0.0))
    return {"tty": tty, "no_yield": no_yield, "tov": tov}


def _failure_clusters(events: list) -> dict:
    """Failures grouped by fix_class, with category and event breakdowns."""
    groups = {}
    for e in events:
        if e["verdict"].get("passed"):
            continue
        fx = e.get("fix") or {}
        fc = fx.get("fix_class") or "unclassified"
        cat = e.get("category") or "uncategorized"
        g = groups.setdefault(fc, {"count": 0, "categories": {}, "events": []})
        g["count"] += 1
        g["categories"][cat] = g["categories"].get(cat, 0) + 1
        g["events"].append(e.get("scenario_id") or e.get("event_id") or "event")
    return groups


def _svg_latency_strip(pairs: list) -> str:
    """Strip plot of time-to-yield across events: one dot per measured yield."""
    vals = [v for _, v in pairs]
    vmax = max(vals + [0.01])
    H, gut, rpad, ay = 84, 46, 30, 46
    pw = _W - gut - rpad

    def X(t: float) -> float:
        return gut + (t / vmax) * pw

    p = [f'<svg class="an-svg" viewBox="0 0 {_W} {H}" width="{_W}" height="{H}" '
         f'role="img" font-family="ui-monospace, SFMono-Regular, Menlo, monospace">']
    p.append(f'<line x1="{gut}" y1="{ay}" x2="{gut + pw}" y2="{ay}" '
             f'stroke="{_C["grid"]}" stroke-width="1" />')
    for t in (0.0, vmax / 2.0, vmax):
        x = X(t)
        p.append(f'<line x1="{x:.1f}" y1="{ay - 5}" x2="{x:.1f}" y2="{ay + 5}" '
                 f'stroke="{_C["grid"]}" stroke-width="1" />')
        p.append(f'<text x="{x:.1f}" y="{ay + 22}" fill="{_C["muted"]}" '
                 f'font-size="10" text-anchor="middle">{t:.2f}s</text>')
    for label, v in pairs:
        x = X(v)
        p.append(f'<circle cx="{x:.1f}" cy="{ay}" r="6" fill="{_C["green"]}" '
                 f'fill-opacity="0.5" stroke="{_C["green"]}" stroke-width="1.4">'
                 f'<title>{_esc(label)}: {v:.2f}s</title></circle>')
    p.append("</svg>")
    return "".join(p)


def _hist_bins(values: list):
    """Deterministic bins on a 0.05 s grid, covering the range in <= 8 bins."""
    vmax = max(values) if values else 0.0
    if vmax <= 0:
        return 0.05, [len(values)]
    width = max(0.05, math.ceil(vmax / 8.0 / 0.05) * 0.05)
    nbins = int(vmax / width + 1e-9) + 1
    counts = [0] * nbins
    for v in values:
        counts[min(nbins - 1, int((v + 1e-9) / width))] += 1
    return width, counts


def _svg_histogram(values: list) -> str:
    """Talk-over histogram: real per-event seconds bucketed on a fixed grid."""
    width, counts = _hist_bins(values)
    n = len(counts)
    H, gut, rpad, top = 150, 46, 20, 18
    ay = H - 34
    pw = _W - gut - rpad
    bw = pw / n
    cmax = max(counts) or 1

    p = [f'<svg class="an-svg" viewBox="0 0 {_W} {H}" width="{_W}" height="{H}" '
         f'role="img" font-family="ui-monospace, SFMono-Regular, Menlo, monospace">']
    p.append(f'<line x1="{gut}" y1="{ay}" x2="{gut + pw}" y2="{ay}" '
             f'stroke="{_C["grid"]}" stroke-width="1" />')
    for i, c in enumerate(counts):
        x0 = gut + i * bw
        # bin edge label
        p.append(f'<text x="{x0:.1f}" y="{ay + 16}" fill="{_C["muted"]}" '
                 f'font-size="10" text-anchor="middle">{i * width:.2f}</text>')
        if c:
            bh = max(2.0, (ay - top) * (c / cmax))
            p.append(f'<rect x="{x0 + 3:.1f}" y="{ay - bh:.1f}" '
                     f'width="{max(2.0, bw - 6):.1f}" height="{bh:.1f}" rx="3" '
                     f'fill="{_C["ember"]}" fill-opacity="0.8">'
                     f'<title>{i * width:.2f}s to {(i + 1) * width:.2f}s: '
                     f'{c} events</title></rect>')
            p.append(f'<text x="{x0 + bw / 2:.1f}" y="{ay - bh - 5:.1f}" '
                     f'fill="{_C["cream"]}" font-size="10" '
                     f'text-anchor="middle">{c}</text>')
    # final bin edge
    p.append(f'<text x="{gut + pw:.1f}" y="{ay + 16}" fill="{_C["muted"]}" '
             f'font-size="10" text-anchor="middle">{n * width:.2f}</text>')
    p.append(f'<text x="{gut + pw / 2:.1f}" y="{ay + 30}" fill="{_C["muted"]}" '
             f'font-size="10" text-anchor="middle">talk-over seconds per event</text>')
    p.append("</svg>")
    return "".join(p)


def _dist_caption(d: Optional[dict], unit: str = "s") -> str:
    if not d:
        return ""
    return ('<div class="ancap mono">'
            f'n={d["n"]} min {d["min"]:.2f}{unit} mean {d["mean"]:.2f}{unit} '
            f'median {d["median"]:.2f}{unit} p90 {d["p90"]:.2f}{unit} '
            f'max {d["max"]:.2f}{unit}</div>')


def _failure_clusters_html(env: dict) -> str:
    groups = _failure_clusters(env["events"])
    if not groups:
        return '<div class="anempty">No failures to cluster. Every event passed.</div>'
    total = sum(g["count"] for g in groups.values())
    maxc = max(g["count"] for g in groups.values())
    out = []
    for fc in sorted(groups, key=lambda k: (-groups[k]["count"], k)):
        g = groups[fc]
        w = max(8, int(300 * g["count"] / maxc))
        cats = ", ".join(
            f"{c} ({n} events)" if n > 1 else c
            for c, n in sorted(g["categories"].items())
        )
        out.append(
            '<div class="fcrow">'
            f'<span class="fck mono">{_esc(fc)}</span>'
            f'<span class="fcbar" style="width:{w}px"></span>'
            f'<span class="fcn mono">{g["count"]} of {total}</span></div>'
            f'<div class="fcd">{_esc(cats)}: {_esc(", ".join(g["events"]))}</div>'
        )
    return "".join(out)


def _analytics_section(env: dict, models: list) -> str:
    a = _analytics_data(env, models)
    parts = ['<section class="card an">'
             '<div class="ctitle">Analytics</div>'
             '<div class="tnote">Aggregated from the events below. Same real '
             'measurements, no new numbers invented.</div>']

    parts.append('<div class="antitle">Time to yield (one dot per event)</div>')
    if a["tty"]:
        parts.append(f'<div class="anwrap">{_svg_latency_strip(a["tty"])}</div>')
        parts.append(_dist_caption(dist_summary([v for _, v in a["tty"]])))
    else:
        parts.append('<div class="anempty">No yields measured in this run.</div>')
    if a["no_yield"]:
        parts.append(f'<div class="ancap">no yield measured: '
                     f'{_esc(", ".join(a["no_yield"]))}</div>')

    parts.append('<div class="antitle">Talk-over histogram</div>')
    parts.append(f'<div class="anwrap">{_svg_histogram([v for _, v in a["tov"]])}</div>')
    parts.append(_dist_caption(dist_summary([v for _, v in a["tov"]])))

    parts.append('<div class="antitle">Failure clusters (by fix class)</div>')
    parts.append(_failure_clusters_html(env))
    parts.append("</section>")
    return "".join(parts)


# --- base comparison (regression deltas vs a previous envelope) -------------

_EPS = 0.0005  # values are rounded to 3 decimals; anything beyond this is real


def _base_rows(env: dict, base_env: dict):
    """Per-scenario deltas vs a previous envelope. Positive delta = more
    talk-over / slower yield = worse. Pass transitions dominate the mark."""
    base_by = {}
    for be in base_env.get("events", []):
        k = be.get("scenario_id") or be.get("event_id")
        if k is not None:
            base_by.setdefault(k, be)
    rows = []
    for e in env["events"]:
        k = e.get("scenario_id") or e.get("event_id") or "event"
        b = base_by.pop(k, None)
        cur = e["verdict"]
        if b is None:
            rows.append({"id": k, "match": False})
            continue
        bv = b.get("verdict", {})

        def _d(c, ba):
            return None if (c is None or ba is None) else round(c - ba, 3)

        d_tov = _d(cur.get("talk_over_sec"), bv.get("talk_over_sec"))
        d_tty = _d(cur.get("seconds_to_yield"), bv.get("seconds_to_yield"))
        p_base, p_cur = bool(bv.get("passed")), bool(cur.get("passed"))
        expected_yield = bool(e.get("expected_yield", True))

        if p_base != p_cur:
            mark = "worse" if p_base else "better"
        else:
            worse = any(d is not None and d > _EPS for d in (d_tov, d_tty))
            better = any(d is not None and d < -_EPS for d in (d_tov, d_tty))
            if expected_yield:
                # a yield lost vs base is worse; a yield gained is better
                if bv.get("seconds_to_yield") is not None and cur.get("seconds_to_yield") is None:
                    worse = True
                if bv.get("seconds_to_yield") is None and cur.get("seconds_to_yield") is not None:
                    better = True
            mark = ("mixed" if (worse and better) else
                    "worse" if worse else "better" if better else "same")
        rows.append({
            "id": k, "match": True, "mark": mark,
            "pass_base": p_base, "pass_cur": p_cur,
            "tov_base": bv.get("talk_over_sec"), "tov_cur": cur.get("talk_over_sec"),
            "d_tov": d_tov,
            "tty_base": bv.get("seconds_to_yield"), "tty_cur": cur.get("seconds_to_yield"),
            "d_tty": d_tty,
        })
    return rows, sorted(base_by.keys())


_MARK_COLORS = {"worse": "red", "better": "green", "same": "muted", "mixed": "ember"}


def _delta_cell(base, cur, d) -> str:
    txt = f"{_s(base)} to {_s(cur)}"
    if d is None:
        return f'<span class="mono">{_esc(txt)}</span>'
    col = _C["red"] if d > _EPS else _C["green"] if d < -_EPS else _C["muted"]
    return (f'<span class="mono">{_esc(txt)} '
            f'<b style="color:{col}">{d:+.2f}s</b></span>')


def _base_section(env: dict, base_env: dict, base_label: Optional[str]) -> str:
    rows, unmatched = _base_rows(env, base_env)
    counts = {}
    for r in rows:
        if r["match"]:
            counts[r["mark"]] = counts.get(r["mark"], 0) + 1
    summary = ", ".join(f"{counts[k]} {k}" for k in ("worse", "mixed", "better", "same")
                        if counts.get(k))
    body = []
    for r in rows:
        if not r["match"]:
            body.append(f'<tr><td class="mono">{_esc(r["id"])}</td>'
                        '<td colspan="3" class="dimcell">not in base</td>'
                        f'<td><span class="mark" style="background:{_C["line"]};'
                        f'color:{_C["muted"]}">NEW</span></td></tr>')
            continue
        pb = "PASS" if r["pass_base"] else "FAIL"
        pc = "PASS" if r["pass_cur"] else "FAIL"
        pcol = _C["green"] if r["pass_cur"] else _C["red"]
        mcol = _C[_MARK_COLORS[r["mark"]]]
        mstyle = (f'background:{mcol}' if r["mark"] in ("worse", "better", "mixed")
                  else f'background:{_C["line"]};color:{_C["muted"]}')
        body.append(
            f'<tr><td class="mono">{_esc(r["id"])}</td>'
            f'<td class="mono">{pb} to <b style="color:{pcol}">{pc}</b></td>'
            f'<td>{_delta_cell(r["tov_base"], r["tov_cur"], r["d_tov"])}</td>'
            f'<td>{_delta_cell(r["tty_base"], r["tty_cur"], r["d_tty"])}</td>'
            f'<td><span class="mark" style="{mstyle}">{r["mark"].upper()}</span></td></tr>'
        )
    note = ""
    if unmatched:
        note = (f'<div class="ancap">in base but not in this run: '
                f'{_esc(", ".join(unmatched))}</div>')
    label = f" ({_esc(base_label)})" if base_label else ""
    return (
        '<section class="card">'
        f'<div class="ctitle">Vs base{label}</div>'
        '<div class="tnote">Per-scenario deltas against the base envelope. '
        'Positive delta = more talk-over or a slower yield = worse. '
        f'{_esc(summary)}.</div>'
        '<table class="basetab"><thead><tr><th>scenario</th><th>pass</th>'
        '<th>talk-over</th><th>time to yield</th><th>mark</th></tr></thead>'
        f'<tbody>{"".join(body)}</tbody></table>{note}'
        '</section>'
    )


# --- event card -----------------------------------------------------------

def _stat(label: str, value: str, color: Optional[str] = None) -> str:
    col = color or _C["mono"]
    return (
        f'<div class="stat"><span class="k">{_esc(label)}</span>'
        f'<span class="v" style="color:{col}">{_esc(value)}</span></div>'
    )


def _event_card(model: dict, embed_audio: bool = False) -> str:
    e = model["event"]
    v = e["verdict"]
    passed = model["passed"]
    chip_c = _C["green"] if passed else _C["red"]
    chip = "PASS" if passed else "FAIL"

    title = e.get("title") or e.get("event_id") or "event"
    sid = e.get("scenario_id") or e.get("event_id") or ""
    cat = e.get("category") or ""

    exp = "yield" if model["expected_yield"] else "hold"
    did = "yes" if model["did_yield"] else "no"

    parts = [f'<section class="card">']
    parts.append('<div class="chead">')
    parts.append(
        f'<div><div class="ctitle">{_esc(title)}</div>'
        f'<div class="cmeta"><span class="mono">{_esc(sid)}</span>'
        + (f'<span class="tag">{_esc(cat)}</span>' if cat else "")
        + "</div></div>"
    )
    parts.append(f'<div class="chip" style="background:{chip_c}">{chip}</div>')
    parts.append("</div>")

    # expected vs actual
    parts.append(
        '<div class="cmp">'
        f'<span>expected <b class="mono">{exp}</b></span>'
        f'<span class="arrow">to</span>'
        f'<span>did yield <b class="mono" style="color:'
        f'{_C["green"] if model["did_yield"] else _C["ember"]}">{did}</b></span>'
        "</div>"
    )

    # timeline
    if model["has_frames"]:
        parts.append(f'<div class="tl">{_svg_timeline(model)}</div>')
    else:
        parts.append('<div class="tl novad">no frame data for this event '
                     '(fixture audio not present)</div>')

    # the scored audio itself, embedded (opt-in; keeps plain reports small)
    if embed_audio:
        parts.append(_audio_block(model))

    # measured stats (all real)
    parts.append('<div class="stats">')
    parts.append(_stat("caller onset", _s(model["onset"]), _C["ember"]))
    parts.append(_stat("time to yield", _s(model["seconds_to_yield"]),
                       _C["green"] if model["did_yield"] else _C["muted"]))
    parts.append(_stat("talk-over", _s(model["talk_over_sec"]), _C["ember"]))
    parts.append(_stat("response gap", _s(model["response_gap_sec"])))
    parts.append(_stat("premature start", _s(model["premature_start_sec"])))
    parts.append("</div>")

    # reasons (only when failed)
    reasons = v.get("reasons") or []
    if reasons:
        items = "".join(f"<li>{_esc(r)}</li>" for r in reasons)
        parts.append(f'<ul class="reasons">{items}</ul>')

    # fix pointer (only present on a failure)
    fix = e.get("fix")
    if fix:
        detail = fix.get("detail") or ""
        parts.append(
            f'<div class="fix"><b>fix</b> [{_esc(fix.get("fix_class"))}] '
            f'{_esc(fix.get("title"))}<div class="fixd">{_esc(detail)}</div></div>'
        )

    # frame inspector: the full frame dump behind the timeline, collapsible
    parts.append(_frame_inspector(model))

    parts.append("</section>")
    return "".join(parts)


def _frame_inspector(model: dict) -> str:
    """Collapsible per-frame evidence table: every value from ``frame_dump``."""
    frames = model.get("frames") or []
    if not frames:
        return ""
    f0 = frames[0]
    rows = "".join(
        f'<tr><td>{f["t_sec"]:.2f}</td><td>{f["caller_dbfs"]:.1f}</td>'
        f'<td>{f["agent_dbfs"]:.1f}</td>'
        f'<td>{1 if f["caller_active"] else 0}</td>'
        f'<td>{1 if f["agent_active"] else 0}</td>'
        f'<td>{f["caller_threshold_db"]:.1f}</td>'
        f'<td>{f["agent_threshold_db"]:.1f}</td></tr>'
        for f in frames
    )
    return (
        '<details class="inspector"><summary>frame inspector: '
        f'{len(frames)} frames, hop {model["hop"]:.3f}s, noise floor caller '
        f'{f0["caller_noise_floor_db"]:.1f} dBFS / agent '
        f'{f0["agent_noise_floor_db"]:.1f} dBFS</summary>'
        '<div class="inswrap"><table class="frames"><thead><tr>'
        '<th>t (s)</th><th>caller dBFS</th><th>agent dBFS</th>'
        '<th>caller active</th><th>agent active</th>'
        '<th>caller thr dB</th><th>agent thr dB</th></tr></thead>'
        f'<tbody>{rows}</tbody></table></div></details>'
    )


# --- audio embedding (opt-in) ----------------------------------------------

# Per-file embed ceiling. A WAV over this is noted in plain text and skipped:
# the report must stay a page a browser opens instantly, never a silent 100 MB
# download. Tests monkeypatch this to exercise the oversize path cheaply.
_EMBED_MAX_BYTES = 8 * 1024 * 1024


def _is_synthetic_fixture(path: str) -> bool:
    """True when ``path`` is one of the bundled synthetic fixtures, so the
    player is labelled honestly (synthetic audio, not a real call)."""
    bundled_dir = os.path.dirname(_bundled_audio_path("x"))
    try:
        return os.path.dirname(os.path.abspath(path)) == bundled_dir
    except (OSError, ValueError):
        return False


def _audio_block(model: dict) -> str:
    """Inline players for the exact audio the scorer measured, one row per
    source file (the stereo recording, or the caller and agent channels).

    Each file is embedded as a base64 WAV data URI so the page stays ONE
    self-contained offline file with zero external requests. A file over
    ``_EMBED_MAX_BYTES`` gets a plain note instead of a player."""
    sources = model.get("audio_sources") or []
    rows = []
    for src in sources:
        path = src.get("path")
        if not path or not os.path.exists(path):
            continue
        name = os.path.basename(path)
        synth = " (synthetic fixture)" if _is_synthetic_fixture(path) else ""
        size = os.path.getsize(path)
        if size > _EMBED_MAX_BYTES:
            rows.append(
                f'<div class="audrow"><span class="audk">{_esc(src["label"])}</span>'
                f'<span class="audnote">audio not embedded: {_esc(name)} is '
                f'{size / 1048576.0:.1f} MB, over the '
                f'{_EMBED_MAX_BYTES / 1048576.0:.0f} MB embed limit{synth}'
                f'</span></div>'
            )
            continue
        with open(path, "rb") as fh:
            b64 = base64.b64encode(fh.read()).decode("ascii")
        rows.append(
            f'<div class="audrow"><span class="audk">{_esc(src["label"])}</span>'
            f'<audio controls preload="metadata" '
            f'src="data:audio/wav;base64,{b64}"></audio>'
            f'<span class="audnote">{_esc(name)}{synth}</span></div>'
        )
    if not rows:
        return ""
    return (
        '<div class="audio">'
        '<div class="audcap">The exact audio the scorer measured, embedded in '
        'this file. Nothing is fetched.</div>'
        + "".join(rows) + "</div>"
    )


# --- thresholds table (exact ScoreConfig used) ----------------------------

def _thresholds(cfg: ScoreConfig) -> str:
    rows = [
        ("frame_ms", cfg.frame_ms),
        ("hop_ms", cfg.hop_ms),
        ("yield_hangover_sec", cfg.yield_hangover_sec),
        ("max_search_sec", cfg.max_search_sec),
        ("caller_proximity_sec", cfg.caller_proximity_sec),
        ("turn_end_silence_sec", cfg.turn_end_silence_sec),
        ("premature_tolerance_sec", cfg.premature_tolerance_sec),
    ]
    vad_rows = [
        ("rel_db", cfg.caller_vad.rel_db, cfg.agent_vad.rel_db),
        ("abs_gate_db", cfg.caller_vad.abs_gate_db, cfg.agent_vad.abs_gate_db),
        ("hangover_sec", cfg.caller_vad.hangover_sec, cfg.agent_vad.hangover_sec),
        ("noise_percentile", cfg.caller_vad.noise_percentile, cfg.agent_vad.noise_percentile),
        ("dyn_margin_db", cfg.caller_vad.dyn_margin_db, cfg.agent_vad.dyn_margin_db),
    ]
    cells = "".join(
        f'<div class="th"><span class="k">{_esc(k)}</span>'
        f'<span class="v mono">{_esc(val)}</span></div>'
        for k, val in rows
    )
    vad_cells = "".join(
        f'<tr><td class="mono">{_esc(k)}</td>'
        f'<td class="mono">{_esc(c)}</td><td class="mono">{_esc(a)}</td></tr>'
        for k, c, a in vad_rows
    )
    return (
        '<section class="card thresholds">'
        '<div class="ctitle">Thresholds used</div>'
        '<div class="tnote">Every value the scorer read. Same audio and config '
        'reproduce every number above.</div>'
        f'<div class="thgrid">{cells}</div>'
        '<table class="vadtab"><thead><tr><th>VAD parameter</th>'
        '<th>caller</th><th>agent</th></tr></thead>'
        f'<tbody>{vad_cells}</tbody></table>'
        '</section>'
    )


# --- footer (honest limits, from LIMITS) ----------------------------------

def _footer() -> str:
    does = "".join(f"<li>{_esc(x)}</li>" for x in LIMITS["does_not_do"])
    return (
        '<footer class="foot">'
        f'<div class="fline"><b>Method.</b> {_esc(LIMITS["method"])}</div>'
        f'<div class="fline"><b>Reproducible.</b> {_esc(LIMITS["reproducible"])}</div>'
        f'<div class="fline"><b>Ceiling.</b> {_esc(LIMITS["ceiling"])}</div>'
        f'<div class="fline"><b>Out of scope.</b><ul class="does">{does}</ul></div>'
        '<div class="fline dim">Reproducible timing measurements with an exposed '
        'method and an explicit ceiling. No accuracy score.</div>'
        '</footer>'
    )


# --- page shell -----------------------------------------------------------

_CSS = """
:root{color-scheme:dark}
*{box-sizing:border-box}
body{margin:0;background:%(bg)s;color:%(cream)s;
 font-family:ui-sans-serif,system-ui,-apple-system,"Segoe UI",Roboto,sans-serif;
 font-size:15px;line-height:1.5}
.mono{font-family:ui-monospace,SFMono-Regular,Menlo,Consolas,monospace}
.wrap{max-width:860px;margin:0 auto;padding:28px 20px 56px}
header.top{display:flex;align-items:flex-start;gap:14px;
 border-bottom:1px solid %(line)s;padding-bottom:18px;margin-bottom:20px}
.logo{width:30px;height:30px;border-radius:9px;background:%(ember)s;flex:none;
 margin-top:2px;box-shadow:0 0 0 4px rgba(240,102,58,0.14)}
.h1{font-size:26px;font-weight:700;letter-spacing:-0.01em;margin:0}
.tagline{color:%(muted)s;margin:2px 0 8px}
.subtle{color:%(cream)s;font-size:13.5px}
.metarow{display:flex;flex-wrap:wrap;gap:8px;margin-top:12px}
.pill{background:%(card2)s;border:1px solid %(line)s;border-radius:999px;
 padding:3px 11px;font-size:12px;color:%(muted)s}
.pill b{color:%(cream)s;font-weight:600}
.summary{display:flex;align-items:center;gap:14px;background:%(card)s;
 border:1px solid %(line)s;border-radius:14px;padding:14px 18px;margin-bottom:22px}
.bignum{font-size:22px;font-weight:700}
.legend{display:flex;gap:16px;flex-wrap:wrap;margin-left:auto;font-size:12.5px;
 color:%(muted)s}
.sw{display:inline-block;width:12px;height:12px;border-radius:3px;
 vertical-align:-1px;margin-right:6px}
.card{background:%(card)s;border:1px solid %(line)s;border-radius:16px;
 padding:18px 20px;margin-bottom:18px;box-shadow:0 1px 0 rgba(0,0,0,0.25)}
.chead{display:flex;align-items:flex-start;gap:12px}
.ctitle{font-size:16.5px;font-weight:650}
.cmeta{display:flex;gap:10px;align-items:center;margin-top:3px;
 color:%(muted)s;font-size:12.5px}
.tag{background:%(card2)s;border:1px solid %(line)s;border-radius:6px;
 padding:1px 8px;font-size:11.5px}
.chip{margin-left:auto;color:#15110d;font-weight:800;font-size:12.5px;
 letter-spacing:0.06em;padding:5px 12px;border-radius:8px}
.cmp{display:flex;align-items:center;gap:12px;margin:12px 0 6px;
 color:%(muted)s;font-size:13.5px}
.cmp b{color:%(cream)s}
.arrow{color:%(muted)s;font-size:12px}
.tl{overflow-x:auto;margin:6px 0 2px;padding-bottom:4px}
.tl svg{display:block}
.novad{color:%(muted)s;font-size:13px;font-style:italic;padding:14px 0}
.audio{margin:10px 0 2px;background:%(card2)s;border:1px solid %(line)s;
 border-radius:10px;padding:10px 13px}
.audcap{color:%(muted)s;font-size:12px;margin-bottom:6px}
.audrow{display:flex;align-items:center;gap:10px;flex-wrap:wrap;margin:6px 0 0}
.audk{min-width:104px;color:%(muted)s;font-size:11.5px;text-transform:lowercase}
.audrow audio{flex:1 1 200px;min-width:0;height:34px}
.audnote{color:%(muted)s;font-size:11.5px;
 font-family:ui-monospace,SFMono-Regular,Menlo,Consolas,monospace}
.stats{display:flex;flex-wrap:wrap;gap:10px 20px;margin-top:12px;
 border-top:1px solid %(line)s;padding-top:12px}
.stat{display:flex;flex-direction:column;gap:2px}
.stat .k{font-size:11.5px;color:%(muted)s;text-transform:lowercase}
.stat .v{font-size:15px;font-weight:600;
 font-family:ui-monospace,SFMono-Regular,Menlo,Consolas,monospace}
.reasons{margin:12px 0 0;padding-left:20px;color:%(cream)s;font-size:13.5px}
.fix{margin-top:12px;background:%(card2)s;border:1px solid %(line)s;
 border-left:3px solid %(ember)s;border-radius:10px;padding:10px 13px;font-size:13.5px}
.fix b{color:%(ember)s}
.fixd{color:%(muted)s;margin-top:3px;font-size:12.5px}
.thresholds .tnote{color:%(muted)s;font-size:12.5px;margin:2px 0 12px}
.thgrid{display:flex;flex-wrap:wrap;gap:8px 10px;margin-bottom:14px}
.th{display:flex;flex-direction:column;gap:2px;background:%(card2)s;
 border:1px solid %(line)s;border-radius:9px;padding:7px 11px;min-width:150px}
.th .k{font-size:11px;color:%(muted)s}
.th .v{font-size:14px;color:%(mono)s}
.vadtab{border-collapse:collapse;width:auto;font-size:13px}
.vadtab th,.vadtab td{text-align:left;padding:5px 18px 5px 0;
 border-bottom:1px solid %(line)s}
.vadtab th{color:%(muted)s;font-weight:600;font-size:12px}
.foot{margin-top:26px;border-top:1px solid %(line)s;padding-top:18px;
 color:%(cream)s;font-size:13px}
.fline{margin-bottom:9px}
.fline b{color:%(cream)s}
.foot .dim{color:%(muted)s}
.does{margin:6px 0 0;padding-left:20px;color:%(muted)s}
.an .antitle{font-size:13px;font-weight:650;margin:16px 0 6px;color:%(cream)s}
.ancap{color:%(muted)s;font-size:12px;margin:4px 0 2px;
 font-family:ui-monospace,SFMono-Regular,Menlo,Consolas,monospace}
.anempty{color:%(muted)s;font-size:13px;font-style:italic;margin:4px 0}
.an svg{display:block}
.anwrap{overflow-x:auto;padding-bottom:4px}
.fcrow{display:flex;align-items:center;gap:10px;margin:8px 0 2px}
.fck{min-width:160px;font-size:12.5px;color:%(cream)s}
.fcbar{display:inline-block;height:12px;border-radius:4px;background:%(ember)s;
 opacity:0.8}
.fcn{font-size:12px;color:%(muted)s}
.fcd{color:%(muted)s;font-size:12px;margin:0 0 8px 170px}
details.inspector{margin-top:12px;border-top:1px solid %(line)s;padding-top:10px}
details.inspector summary{cursor:pointer;color:%(muted)s;font-size:12.5px}
.inswrap{max-height:320px;overflow:auto;margin-top:8px;
 border:1px solid %(line)s;border-radius:8px}
table.frames{border-collapse:collapse;font-size:11.5px;
 font-family:ui-monospace,SFMono-Regular,Menlo,Consolas,monospace}
table.frames th{position:sticky;top:0;background:%(card2)s;color:%(muted)s;
 font-weight:600;text-align:right;padding:4px 12px;white-space:nowrap}
table.frames td{text-align:right;padding:1px 12px;color:%(mono)s;
 border-bottom:1px solid %(card2)s}
.basetab{border-collapse:collapse;font-size:12.5px;margin-top:8px}
.basetab th,.basetab td{text-align:left;padding:6px 16px 6px 0;
 border-bottom:1px solid %(line)s}
.basetab th{color:%(muted)s;font-size:12px;font-weight:600}
.basetab .dimcell{color:%(muted)s;font-style:italic}
.mark{font-weight:700;font-size:11px;letter-spacing:0.05em;
 padding:2px 8px;border-radius:6px;color:#15110d}
@media print{
 body{background:#ffffff;color:#1b1714}
 .card,.summary{background:#ffffff;border-color:#d8d2c6;box-shadow:none;
  break-inside:avoid}
 .pill,.tag,.fix{background:#f4efe4;color:#3a3128}
 details.inspector{display:none}
 .audio{display:none}
}
""" % _C


def _render_page(env: dict, models: list, cfg: ScoreConfig,
                 base_env: Optional[dict] = None,
                 base_label: Optional[str] = None,
                 embed_audio: bool = False) -> str:
    s = env["summary"]
    overall_pass = s["failed"] == 0
    overall_c = _C["green"] if overall_pass else _C["red"]
    overall_t = "ALL PASS" if overall_pass else "REGRESSION"

    eng = env.get("engine", {})
    mode = env.get("mode", "")
    stack = env.get("stack", "generic")
    if env.get("suite"):
        mode_label = f"suite: {env['suite']}"
    else:
        mode_label = mode

    cards = "".join(_event_card(m, embed_audio=embed_audio) for m in models)

    legend = (
        f'<div class="legend">'
        f'<span><i class="sw" style="background:{_C["caller"]}"></i>caller</span>'
        f'<span><i class="sw" style="background:{_C["agent"]}"></i>agent</span>'
        f'<span><i class="sw" style="background:{_C["ember"]};opacity:.55"></i>talk-over</span>'
        f'<span><i class="sw" style="background:{_C["green"]}"></i>yield</span>'
        f'</div>'
    )

    head = (
        '<header class="top"><div class="logo"></div><div>'
        '<h1 class="h1">hotato</h1>'
        '<div class="tagline">Open, offline turn-taking eval for voice agents.</div>'
        '<div class="subtle">Deterministic offline timing. Every value below is a '
        'real measurement from the scorer.</div>'
        '<div class="metarow">'
        f'<span class="pill"><b>{_esc(mode_label)}</b></span>'
        f'<span class="pill">stack <b>{_esc(stack)}</b></span>'
        f'<span class="pill">engine <b>{_esc(eng.get("name", ""))}</b> '
        f'{_esc(eng.get("version", ""))}</span>'
        '<span class="pill">offline <b>yes</b></span>'
        '</div></div></header>'
    )

    summary = (
        '<div class="summary">'
        f'<div><div class="bignum">{s["passed"]} of {s["events"]}</div>'
        '<div class="subtle" style="color:' + _C["muted"] + '">events pass</div></div>'
        f'<div class="chip" style="background:{overall_c}">{overall_t}</div>'
        f'{legend}'
        '</div>'
    )

    base_html = _base_section(env, base_env, base_label) if base_env else ""

    body = (
        f'<div class="wrap">{head}{summary}'
        f'{_analytics_section(env, models)}{base_html}{cards}'
        f'{_thresholds(cfg)}{_footer()}</div>'
    )

    return (
        "<!doctype html>\n<html lang=\"en\"><head>"
        "<meta charset=\"utf-8\">"
        "<meta name=\"viewport\" content=\"width=device-width, initial-scale=1\">"
        "<title>hotato report</title>"
        f"<style>{_CSS}</style></head><body>{body}</body></html>\n"
    )


# --- frame resolution for suite events ------------------------------------

def _suite_event_wav(event, audio_dir, suffix) -> Optional[str]:
    """Path of a suite event's audio on disk, or None when there is none
    (then there is nothing to draw or embed)."""
    sid = event.get("scenario_id") or event.get("event_id")
    if not sid:
        return None
    if audio_dir:
        wav = os.path.join(audio_dir, sid + suffix)
    else:
        wav = _bundled_audio_path(sid, suffix)
    return wav if os.path.exists(wav) else None


def _frames_for_suite_event(event, audio_dir, suffix, cc, ac, cfg):
    wav = _suite_event_wav(event, audio_dir, suffix)
    if not wav:
        return [], cfg.hop_ms / 1000.0
    dump = dump_frames_for_input(
        stereo=wav, caller_channel=cc, agent_channel=ac, onset_sec=None, cfg=cfg
    )
    return dump["frames"], dump["hop_sec"]


# --- markdown renderer (same content, tables instead of SVG) ---------------

def _md_row(cells) -> str:
    return "| " + " | ".join(str(c) for c in cells) + " |"


def _render_md(env: dict, models: list, cfg: ScoreConfig,
               base_env: Optional[dict] = None,
               base_label: Optional[str] = None) -> str:
    s = env["summary"]
    eng = env.get("engine", {})
    mode_label = f"suite: {env['suite']}" if env.get("suite") else env.get("mode", "")
    verdict = "ALL PASS" if s["failed"] == 0 else "REGRESSION"

    L = []
    L.append("# hotato report")
    L.append("")
    L.append("Open, offline turn-taking eval for voice agents. Every value below "
             "is a real measurement from the scorer. No accuracy score: "
             "reproducible timing with an exposed method and an explicit ceiling.")
    L.append("")
    L.append(f"- mode: {mode_label}")
    L.append(f"- stack: {env.get('stack', 'generic')}")
    L.append(f"- engine: {eng.get('name', '')} {eng.get('version', '')}".rstrip())
    L.append("- offline: yes")
    L.append("")
    L.append("## Summary")
    L.append("")
    L.append(f"**{s['passed']} of {s['events']} events pass.** Verdict: {verdict}.")
    L.append("")
    L.append(_md_row(["event", "category", "expected", "did yield", "time to yield",
                      "talk-over", "response gap", "premature start", "verdict"]))
    L.append(_md_row(["---"] * 9))
    for m in models:
        e = m["event"]
        L.append(_md_row([
            e.get("scenario_id") or e.get("event_id") or "event",
            e.get("category") or "",
            "yield" if m["expected_yield"] else "hold",
            "yes" if m["did_yield"] else "no",
            _s(m["seconds_to_yield"]),
            _s(m["talk_over_sec"]),
            _s(m["response_gap_sec"]),
            _s(m["premature_start_sec"]),
            "PASS" if m["passed"] else "FAIL",
        ]))
    L.append("")

    # failures: reasons + fix
    failed = [m for m in models if not m["passed"]]
    if failed:
        L.append("## Failures and fixes")
        L.append("")
        for m in failed:
            e = m["event"]
            sid = e.get("scenario_id") or e.get("event_id") or "event"
            L.append(f"### {sid}: FAIL")
            L.append("")
            for r in e["verdict"].get("reasons") or []:
                L.append(f"- reason: {r}")
            fx = e.get("fix")
            if fx:
                L.append(f"- fix [{fx.get('fix_class')}]: {fx.get('title')}")
                if fx.get("detail"):
                    L.append(f"  - {fx['detail']}")
                knob = fx.get("knob")
                if knob:
                    L.append(f"  - knob: {knob.get('parameter')}")
                    L.append(f"  - move: {knob.get('direction')}")
            L.append("")

    # analytics: same aggregates as the HTML charts, as tables
    a = _analytics_data(env, models)
    L.append("## Analytics")
    L.append("")
    L.append("### Time to yield")
    L.append("")
    if a["tty"]:
        L.append(_md_row(["event", "seconds to yield"]))
        L.append(_md_row(["---"] * 2))
        for sid, v in sorted(a["tty"], key=lambda p: p[1]):
            L.append(_md_row([sid, f"{v:.2f}s"]))
        d = dist_summary([v for _, v in a["tty"]])
        L.append("")
        L.append(f"n={d['n']}, min {d['min']:.2f}s, mean {d['mean']:.2f}s, "
                 f"median {d['median']:.2f}s, p90 {d['p90']:.2f}s, max {d['max']:.2f}s.")
    else:
        L.append("No yields measured in this run.")
    if a["no_yield"]:
        L.append("")
        L.append(f"No yield measured: {', '.join(a['no_yield'])}.")
    L.append("")
    L.append("### Talk-over histogram")
    L.append("")
    width, counts = _hist_bins([v for _, v in a["tov"]])
    L.append(_md_row(["bin (seconds)", "events"]))
    L.append(_md_row(["---"] * 2))
    for i, c in enumerate(counts):
        L.append(_md_row([f"{i * width:.2f} to {(i + 1) * width:.2f}", c]))
    d = dist_summary([v for _, v in a["tov"]])
    if d:
        L.append("")
        L.append(f"n={d['n']}, min {d['min']:.2f}s, mean {d['mean']:.2f}s, "
                 f"median {d['median']:.2f}s, p90 {d['p90']:.2f}s, max {d['max']:.2f}s.")
    L.append("")
    L.append("### Failure clusters")
    L.append("")
    groups = _failure_clusters(env["events"])
    if groups:
        L.append(_md_row(["fix class", "count", "categories", "events"]))
        L.append(_md_row(["---"] * 4))
        for fc in sorted(groups, key=lambda k: (-groups[k]["count"], k)):
            g = groups[fc]
            cats = ", ".join(f"{c} ({n})" for c, n in sorted(g["categories"].items()))
            L.append(_md_row([fc, g["count"], cats, ", ".join(g["events"])]))
    else:
        L.append("No failures to cluster. Every event passed.")
    L.append("")

    # base comparison
    if base_env:
        rows, unmatched = _base_rows(env, base_env)
        label = f" ({base_label})" if base_label else ""
        L.append(f"## Vs base{label}")
        L.append("")
        L.append("Positive delta = more talk-over or a slower yield = worse.")
        L.append("")
        L.append(_md_row(["scenario", "pass", "talk-over", "time to yield", "mark"]))
        L.append(_md_row(["---"] * 5))
        for r in rows:
            if not r["match"]:
                L.append(_md_row([r["id"], "not in base", "", "", "NEW"]))
                continue

            def _cell(base, cur, dd):
                txt = f"{_s(base)} to {_s(cur)}"
                return f"{txt} ({dd:+.2f}s)" if dd is not None else txt

            L.append(_md_row([
                r["id"],
                f"{'PASS' if r['pass_base'] else 'FAIL'} to "
                f"{'PASS' if r['pass_cur'] else 'FAIL'}",
                _cell(r["tov_base"], r["tov_cur"], r["d_tov"]),
                _cell(r["tty_base"], r["tty_cur"], r["d_tty"]),
                r["mark"].upper(),
            ]))
        if unmatched:
            L.append("")
            L.append(f"In base but not in this run: {', '.join(unmatched)}.")
        L.append("")

    # thresholds (exact ScoreConfig used)
    L.append("## Thresholds used")
    L.append("")
    L.append("Every value the scorer read. Same audio and config reproduce "
             "every number above.")
    L.append("")
    L.append(_md_row(["parameter", "value"]))
    L.append(_md_row(["---"] * 2))
    for k, v in [("frame_ms", cfg.frame_ms), ("hop_ms", cfg.hop_ms),
                 ("yield_hangover_sec", cfg.yield_hangover_sec),
                 ("max_search_sec", cfg.max_search_sec),
                 ("caller_proximity_sec", cfg.caller_proximity_sec),
                 ("turn_end_silence_sec", cfg.turn_end_silence_sec),
                 ("premature_tolerance_sec", cfg.premature_tolerance_sec)]:
        L.append(_md_row([k, v]))
    L.append("")
    L.append(_md_row(["VAD parameter", "caller", "agent"]))
    L.append(_md_row(["---"] * 3))
    for k, c, ag in [("rel_db", cfg.caller_vad.rel_db, cfg.agent_vad.rel_db),
                     ("abs_gate_db", cfg.caller_vad.abs_gate_db, cfg.agent_vad.abs_gate_db),
                     ("hangover_sec", cfg.caller_vad.hangover_sec, cfg.agent_vad.hangover_sec),
                     ("noise_percentile", cfg.caller_vad.noise_percentile, cfg.agent_vad.noise_percentile),
                     ("dyn_margin_db", cfg.caller_vad.dyn_margin_db, cfg.agent_vad.dyn_margin_db)]:
        L.append(_md_row([k, c, ag]))
    L.append("")

    # honest limits footer
    L.append("## Method and limits")
    L.append("")
    L.append(f"- Method. {LIMITS['method']}")
    L.append(f"- Reproducible. {LIMITS['reproducible']}")
    L.append(f"- Ceiling. {LIMITS['ceiling']}")
    L.append("- Out of scope: " + "; ".join(LIMITS["does_not_do"]) + ".")
    L.append("- Frame-level evidence: `hotato export` writes frames.csv; "
             "`hotato run --dump-frames` writes the same frames as JSON.")
    L.append("")
    L.append("Reproducible timing measurements with an exposed method and an "
             "explicit ceiling. No accuracy score.")
    L.append("")
    return "\n".join(L)


# --- public API -----------------------------------------------------------

def _score_and_model(
    *,
    stereo: Optional[str] = None,
    caller: Optional[str] = None,
    agent: Optional[str] = None,
    caller_channel: int = 0,
    agent_channel: int = 1,
    onset_sec: Optional[float] = None,
    expect: str = "yield",
    stack: Optional[str] = None,
    suite: Optional[str] = None,
    scenarios_dir: Optional[str] = None,
    audio_dir: Optional[str] = None,
    suffix: str = ".example.wav",
    max_talk_over_sec: Optional[float] = None,
    max_time_to_yield_sec: Optional[float] = None,
    cfg: Optional[ScoreConfig] = None,
):
    """Score the input and return ``(envelope, models, cfg)``: everything both
    renderers (HTML and Markdown) need, all from real measurements."""
    cfg = cfg or ScoreConfig()

    if suite:
        env = run_suite(
            suite=suite,
            stack=stack,
            scenarios_dir=scenarios_dir,
            audio_dir=audio_dir,
            suffix=suffix,
            caller_channel=caller_channel,
            agent_channel=agent_channel,
            cfg=cfg,
        )
        models = []
        for e in env["events"]:
            frames, hop = _frames_for_suite_event(
                e, audio_dir, suffix, caller_channel, agent_channel, cfg
            )
            m = _event_model(e, frames, hop, cfg)
            wav = _suite_event_wav(e, audio_dir, suffix)
            if wav:
                m["audio_sources"] = [{"label": "scenario audio", "path": wav}]
            models.append(m)
    else:
        env = run_single(
            stereo=stereo,
            caller=caller,
            agent=agent,
            caller_channel=caller_channel,
            agent_channel=agent_channel,
            onset_sec=onset_sec,
            expect=expect,
            stack=stack,
            max_talk_over_sec=max_talk_over_sec,
            max_time_to_yield_sec=max_time_to_yield_sec,
            cfg=cfg,
        )
        dump = dump_frames_for_input(
            stereo=stereo,
            caller=caller,
            agent=agent,
            caller_channel=caller_channel,
            agent_channel=agent_channel,
            onset_sec=None,
            cfg=cfg,
        )
        model = _event_model(env["events"][0], dump["frames"], dump["hop_sec"], cfg)
        if stereo:
            model["audio_sources"] = [{"label": "recording", "path": stereo}]
        elif caller and agent:
            model["audio_sources"] = [{"label": "caller", "path": caller},
                                      {"label": "agent", "path": agent}]
        models = [model]

    return env, models, cfg


def build_report_html(*, base: Optional[dict] = None,
                      base_label: Optional[str] = None,
                      embed_audio: bool = False, **kwargs):
    """Score the input and return ``(html_str, envelope)``.

    Pass ``suite`` for the labelled battery, or a single recording via
    ``stereo`` / ``caller``+``agent``. Pass ``base`` (a previous envelope dict,
    e.g. loaded from ``hotato run --format json`` output) to render regression
    deltas per scenario. The HTML is a single self-contained file. The report
    is always scored with the energy reference config so the page is
    reproducible.

    ``embed_audio=True`` embeds the exact scored audio under each timeline as
    a base64 WAV data URI with a native player. The page stays one offline
    file with zero external requests; it just grows by roughly the audio size.
    Files over ~8 MB are noted in plain text and skipped. Default False keeps
    plain reports small.
    """
    env, models, cfg = _score_and_model(**kwargs)
    page = _render_page(env, models, cfg, base_env=base, base_label=base_label,
                        embed_audio=embed_audio)
    return page, env


def build_report_md(*, base: Optional[dict] = None,
                    base_label: Optional[str] = None, **kwargs):
    """Score the input and return ``(markdown_str, envelope)``.

    Mirrors the HTML report's content with tables instead of SVG: summary,
    per-event measurements, failures with fixes, analytics aggregates, the
    optional base comparison, thresholds, and the honest limits.
    """
    env, models, cfg = _score_and_model(**kwargs)
    return _render_md(env, models, cfg, base_env=base, base_label=base_label), env


def write_report(path: str, fmt: str = "html", embed_audio: bool = False,
                 **kwargs):
    """Build the report in ``fmt`` ('html' or 'md') and write it to ``path``.
    Returns the envelope. For PDF, print the HTML from any browser: the page
    ships print CSS, so no separate renderer is needed."""
    if fmt == "html":
        text, env = build_report_html(embed_audio=embed_audio, **kwargs)
    elif fmt == "md":
        if embed_audio:
            # Rejected up front (clean usage error -> exit 2 in the CLI):
            # silently writing an md file without the requested audio would mislead.
            raise ValueError(
                "audio embedding applies to the HTML report only; drop "
                "--format md or drop --embed-audio"
            )
        text, env = build_report_md(**kwargs)
    else:
        raise ValueError(f"unknown report format {fmt!r}; use 'html' or 'md'")
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(text)
    return env
