"""Interactive visual report: one self-contained HTML file per evaluation.

``hotato report`` turns a scored recording (or the bundled self-test battery)
into a single, offline, double-clickable HTML page. For every event it draws a
to-scale timeline from the REAL frame data the scorer already produced:

  * a caller activity track and an agent activity track, drawn to scale
  * the talk-over span shaded, labelled with its measured seconds
  * the caller-onset marker and the yield-point marker
  * expected-vs-actual (should yield / did yield) and a PASS/FAIL chip; an
    event whose input cannot be judged gets a NOT SCORABLE chip with its
    reason, never a normal verdict
  * the exact ScoreConfig thresholds used, collapsed into one closed
    ``<details>`` block so the page is reproducible without stamping the same
    parameter table above every render

After the per-event cards, once there are at least three of them, the page
carries an analytics rollup computed from the same real measurements: a
time-to-yield distribution strip, a talk-over histogram, failure clustering by
fix class, and a collapsible per-event frame inspector (the full frame dump as
a table). A page with fewer than three events skips the rollup entirely --
there is nothing for it to say that the cards themselves do not already show.
Pass ``base`` (a previous envelope) to render per-scenario regression deltas
with worse/better marks.

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

Audio has three modes, selected by ``audio_mode``:

  * ``none`` (the default) writes no audio at all -- the small, portable report.
  * ``self_contained`` is the ``--embed-audio`` behaviour above: the PCM is
    inlined as a base64 data URI, for LOCAL sharing where playback should work
    with no other files.
  * ``audio_reference`` (the mode a FLEET-shared report uses) NEVER inlines the
    PCM. It renders a stable, content-addressed reference to each source -- its
    ``pcm_sha256`` and a relative locator (and a ``recording_id`` when the
    caller supplies one) -- plus a note that playback requires the fleet store.
    A copy of the page shared outside the store therefore leaks no spoken PII.

``embed_audio=True`` is kept as the shorthand for ``audio_mode="self_contained"``
so existing callers are unchanged; passing ``audio_mode`` wins when both appear.

``build_report_md`` mirrors the same content as plain Markdown (tables instead
of SVG). A future PDF needs no new renderer: the HTML already ships print CSS,
so "print to PDF" from any browser produces the document.

Pass ``transcript`` (optional, default ``None``) to attach an ASR transcript as
CONTEXT next to an event: either one ``hotato.transcribe.Transcript`` (applied
to the single scored recording, or to every event alike), or a ``dict`` mapping
each event's ``scenario_id``/``event_id`` to its own ``Transcript`` (a suite
with one audio file per event). This module never imports ``hotato.transcribe``
and never runs any speech-to-text itself -- the caller produces the
``Transcript`` (through the strictly opt-in ``[transcribe]`` extra) and hands
it here purely as data to render; report.py stays zero-dependency regardless.
The transcript is rendered as a collapsed, clearly-labelled "Transcript
(context, not a score)" panel per event and folded into the machine envelope
as an additive ``transcript_context`` key on that event -- it NEVER touches
``did_yield``, ``talk_over_sec``, ``seconds_to_yield``, or any other
scoring/verdict field, and a report built without ``transcript`` is
byte-identical to one built before this existed.

Pass ``trace`` (optional, default ``None``) to attach a voice trace as CONTEXT:
a ``hotato.voice_trace.v1`` object as loaded by
``hotato.trace.load_voice_trace_jsonl`` (a meta dict plus a list of span dicts)
or the equivalent dict/list. Exactly like ``base``, ``transcript``, and
``assertions``, this module never EVALUATES or scores a trace -- the caller
hands it an already-produced observability artifact and this purely renders it.
It is rendered as one collapsed, clearly-labelled call-level "Trace (context,
not a score)" section (a mono span table in HTML, a Markdown table in MD) and
folded into the machine envelope as an additive top-level ``trace_context``
key. It respects the trace's own redaction: a span carrying
``text_redacted: true`` (e.g. an ``asr_partial`` ingested without
``--include-text``) shows a ``[redacted]`` placeholder, never its text. The
section is context only; it NEVER touches ``did_yield``, ``talk_over_sec``,
``seconds_to_yield``, or any other scoring/verdict field, and a report built
without ``trace`` is byte-identical to one built before this existed. To avoid
the circular import ``hotato.trace`` imports THIS module, report.py never
imports ``hotato.trace`` at module scope -- the trace is duck-typed as data.
"""

from __future__ import annotations

from .errors import open_regular as _open_regular

import base64
import html
import math
import os
from typing import Any, Optional

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

__all__ = ["build_report_html", "build_report_md", "write_report",
           "AUDIO_NONE", "AUDIO_SELF_CONTAINED", "AUDIO_REFERENCE"]

# --- audio modes ----------------------------------------------------------
# The three ways a report treats the scored audio. ``none`` writes nothing;
# ``self_contained`` inlines the PCM (local sharing); ``audio_reference`` names
# the content-addressed audio by hash + locator and inlines NOTHING, so a
# fleet-shared page carries no spoken PII.
AUDIO_NONE = "none"
AUDIO_SELF_CONTAINED = "self_contained"
AUDIO_REFERENCE = "audio_reference"
_AUDIO_MODES = (AUDIO_NONE, AUDIO_SELF_CONTAINED, AUDIO_REFERENCE)


def _resolve_audio_mode(embed_audio: bool, audio_mode: Optional[str]) -> str:
    """Reconcile the legacy ``embed_audio`` bool with the explicit ``audio_mode``.

    ``audio_mode`` wins when given (and must be one of the three modes);
    otherwise ``embed_audio`` maps to ``self_contained`` (True) or ``none``
    (False), so every existing caller behaves exactly as before."""
    if audio_mode is not None:
        if audio_mode not in _AUDIO_MODES:
            raise ValueError(
                f"unknown audio_mode {audio_mode!r}; use one of {_AUDIO_MODES}")
        return audio_mode
    return AUDIO_SELF_CONTAINED if embed_audio else AUDIO_NONE

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


def _event_status(model: dict) -> str:
    """Render status for one event model: ``"not_scorable"`` when the scorer
    marked the input as impossible to judge (``event["scorable"]`` is False),
    else ``"pass"`` / ``"fail"``. Every PASS/FAIL decision in both renderers
    goes through here, so a not-scorable input can never surface as a normal
    verdict on the page."""
    if model["event"].get("scorable") is False:
        return "not_scorable"
    return "pass" if model["passed"] else "fail"


_STATUS_LABEL = {"pass": "PASS", "fail": "FAIL", "not_scorable": "NOT SCORABLE"}


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

    # Accessible name restating the measured markers the drawing shows.
    if model.get("onset") is not None and model.get("yield_abs") is not None:
        aria = (f'Scored timeline: the caller track starts at '
                f'{model["onset"]:.2f} seconds and the agent track stops at a '
                f'yield marker {model["yield_abs"] - model["onset"]:.2f} '
                f'seconds after onset')
    elif model.get("onset") is not None:
        aria = (f'Scored timeline: the caller track starts at '
                f'{model["onset"]:.2f} seconds and the agent track keeps '
                f'going with no yield marker')
    else:
        aria = 'Scored timeline of caller and agent activity from the recording'

    p = []
    p.append(f'<svg class="tl-svg" viewBox="0 0 {_W} {_H}" width="{_W}" height="{_H}" '
             f'role="img" aria-label="{_esc(aria)}" '
             f'font-family="ui-monospace, SFMono-Regular, Menlo, monospace">')

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
    comes straight from the envelope / models; nothing is invented.

    Not-scorable events are excluded: their null measurements would mislead
    the latency and talk-over distributions. They get their own section."""
    tty, no_yield, tov = [], [], []
    for m in models:
        if _event_status(m) == "not_scorable":
            continue
        sid = m["event"].get("scenario_id") or m["event"].get("event_id") or "event"
        if m["seconds_to_yield"] is not None:
            tty.append((sid, m["seconds_to_yield"]))
        else:
            no_yield.append(sid)
        tov.append((sid, m["talk_over_sec"] if m["talk_over_sec"] is not None else 0.0))
    return {"tty": tty, "no_yield": no_yield, "tov": tov}


def _failure_clusters(events: list) -> dict:
    """Failures grouped by fix_class, with category and event breakdowns.
    Not-scorable events are input problems, not failures: excluded."""
    groups = {}
    for e in events:
        if e.get("scorable") is False:
            continue
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

    if vals:
        aria = (f'Strip plot of time to yield: {len(vals)} measured yields '
                f'between {min(vals):.2f} and {max(vals):.2f} seconds, one dot '
                f'per event')
    else:
        aria = 'Strip plot of time to yield: no measured yields'
    p = [f'<svg class="an-svg" viewBox="0 0 {_W} {H}" width="{_W}" height="{H}" '
         f'role="img" aria-label="{_esc(aria)}" '
         f'font-family="ui-monospace, SFMono-Regular, Menlo, monospace">']
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

    aria = (f'Talk-over histogram: {len(values)} events bucketed in '
            f'{width:.2f} second bins, tallest bin {cmax} events')
    p = [f'<svg class="an-svg" viewBox="0 0 {_W} {H}" width="{_W}" height="{H}" '
         f'role="img" aria-label="{_esc(aria)}" '
         f'font-family="ui-monospace, SFMono-Regular, Menlo, monospace">']
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


def _no_failures_text(env: dict) -> str:
    """Empty-cluster wording: stays byte-identical for fully-scorable runs and
    says "scorable" only when a not-scorable input is actually present."""
    if _not_scorable_events(env):
        return "No failures to cluster. Every scorable event passed."
    return "No failures to cluster. Every event passed."


def _failure_clusters_html(env: dict) -> str:
    groups = _failure_clusters(env["events"])
    if not groups:
        return f'<div class="anempty">{_no_failures_text(env)}</div>'
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


# --- not-scorable inputs (input problems, never agent verdicts) -------------

def _not_scorable_events(env: dict) -> list:
    return [e for e in env.get("events", []) if e.get("scorable") is False]


def _not_scorable_section_html(env: dict) -> str:
    """Short section listing every not-scorable input with its reason. Rendered
    only when at least one exists, so all-scorable pages are untouched."""
    ns = _not_scorable_events(env)
    if not ns:
        return ""
    rows = []
    for e in ns:
        sid = e.get("scenario_id") or e.get("event_id") or "event"
        reason = e.get("not_scorable_reason") or ""
        rows.append(f'<div class="fcrow"><span class="fck mono">{_esc(sid)}</span></div>'
                    f'<div class="fcd">{_esc(reason)}</div>')
    return (
        '<section class="card">'
        '<div class="ctitle">Not scorable inputs</div>'
        '<div class="tnote">Input problems, never agent verdicts. These events '
        'are excluded from the pass/fail counts, the failure clusters, and the '
        'timing distributions.</div>'
        + "".join(rows) + "</section>"
    )


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
        status_base = ("not_scorable" if b.get("scorable") is False
                       else "pass" if p_base else "fail")
        status_cur = ("not_scorable" if e.get("scorable") is False
                      else "pass" if p_cur else "fail")
        expected_yield = bool(e.get("expected_yield", True))

        if "not_scorable" in (status_base, status_cur):
            # An unjudgeable input on either side carries no verdict to
            # compare against; worse/better would be invented.
            mark = "n/a"
        elif p_base != p_cur:
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
            "status_base": status_base, "status_cur": status_cur,
            "tov_base": bv.get("talk_over_sec"), "tov_cur": cur.get("talk_over_sec"),
            "d_tov": d_tov,
            "tty_base": bv.get("seconds_to_yield"), "tty_cur": cur.get("seconds_to_yield"),
            "d_tty": d_tty,
        })
    return rows, sorted(base_by.keys())


_MARK_COLORS = {"worse": "red", "better": "green", "same": "muted",
                "mixed": "ember", "n/a": "muted"}
_STATUS_COLORS = {"pass": "green", "fail": "red", "not_scorable": "ember"}


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
    summary = ", ".join(f"{counts[k]} {k}"
                        for k in ("worse", "mixed", "better", "same", "n/a")
                        if counts.get(k))
    body = []
    for r in rows:
        if not r["match"]:
            body.append(f'<tr><td class="mono">{_esc(r["id"])}</td>'
                        '<td colspan="3" class="dimcell">not in base</td>'
                        f'<td><span class="mark" style="background:{_C["line"]};'
                        f'color:{_C["muted"]}">NEW</span></td></tr>')
            continue
        pb = _STATUS_LABEL[r["status_base"]]
        pc = _STATUS_LABEL[r["status_cur"]]
        pcol = _C[_STATUS_COLORS[r["status_cur"]]]
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


# --- assertions (assert.v1): a pre-built envelope rendered as typed cards --
#
# report.py never evaluates an assertion: exactly like ``base`` (a previous
# run envelope) and ``transcript`` (an already-produced ASR artifact), the
# caller hands this module an ALREADY-EVALUATED assert.v1 envelope (build one
# with hotato.assert_.run_assertions / run_assertions_from_file / .../
# _from_yaml) and this module purely renders it -- nothing here recomputes a
# result or invents a field the envelope did not already carry.
#
# Two visually separate shelves, always: "Deterministic" (the five regex /
# checksum / span-lookup kinds this build actually evaluates -- phrase, pii,
# policy, tool_call, outcome) and "Model-assisted (advisory, quarantined)".
# The latter is ALWAYS empty in this build and says so: a judge (LLM-scored)
# kind is a separate, quarantined capability, not built here. The headline is
# ALWAYS "N deterministic pass / M fail  K judge-scored (advisory)" -- two
# counts side by side, never one blended number, and there is NO
# ``overall_score`` anywhere on the page.
#
# Absent by default: a report built with ``assertions=None`` (the default) is
# byte-identical to one built before this feature existed -- no new markup,
# no new CSS.
#
# ``_ASSERT_SCHEMA`` mirrors ``hotato.assert_.SCHEMA`` as a bare string
# constant rather than importing it: ``hotato.assert_`` imports ``hotato.trace``,
# which imports ``hotato.contract``, which imports THIS module -- an
# ``import hotato.assert_`` at module scope here would be a circular import.
# report.py only ever needs the one literal string to validate an envelope's
# ``schema`` key, so it stays decoupled instead of importing across that cycle.
_ASSERT_SCHEMA = "assert.v1"

# The five report DIMENSIONS the per-dimension scorecard groups results into.
# Defined here as a bare tuple -- mirroring ``hotato.assert_.RESULT_DIMENSIONS``
# and ``hotato.conversation_test.REPORT_DIMENSIONS`` -- rather than imported:
# report.py is imported (via ``hotato.contract``) by ``hotato.trace``, which
# ``hotato.assert_`` imports, so ``import hotato.assert_`` here would close that
# cycle (the same reason ``_ASSERT_SCHEMA`` is a bare literal). The scorecard is
# an ADDITIONAL grouped VIEW of the SAME deterministic results -- each dimension
# shows its own pass/fail/inconclusive counts, never a merged or overall number.
_REPORT_DIMENSIONS = ("outcome", "policy", "conversation", "speech", "reliability")

# Reliability (pass@1 / pass@k / pass^k across repeated runs) is Reliability's
# OWN dimension. When a report carries REAL repetition data (a run_matrix
# aggregate or a reliability() dict, threaded via the ``reliability=`` param) the
# scorecard renders those real numbers (see _reliability_block_html); when it
# carries NONE, the dimension shows an honest empty-state -- never a fabricated
# value, and pass^k is never blended into any other dimension.
_RELIABILITY_DIMENSION = "reliability"
# An honest empty assert.v1 envelope: used ONLY when real reliability data is
# supplied with no assertions envelope, so the Reliability dimension still
# renders instead of the data being silently dropped (0 assertions is stated
# plainly; nothing is fabricated).
_EMPTY_ASSERT_ENVELOPE = {
    "schema": "assert.v1",
    "exit_code": 0,
    "results": [],
    "summary": {
        "deterministic": {"pass": 0, "fail": 0, "inconclusive": 0},
        "judge": {"pass": 0, "fail": 0},
        "note": "no assertions in this run (reliability-only report)",
    },
}

_RELIABILITY_EMPTY_NOTE = (
    "Reliability (pass@1 / pass@k / pass^k across repeated runs) not measured: "
    "no repeated runs in this report."
)

_ASSERT_STATUS_COLORS = {"PASS": "green", "FAIL": "red", "INCONCLUSIVE": "ember"}

# A left-border accent per kind so the five assertion dimensions read as
# visually distinct card types at a glance. Purely decorative: the
# PASS/FAIL/INCONCLUSIVE chip (the same palette as every other verdict on the
# page) is the only element that ever carries a verdict.
_ASSERT_KIND_ACCENT = {
    "phrase": "agent",
    "pii": "ember",
    "policy": "caller",
    "tool_call": "green",
    "outcome": "muted",
}


def _validate_assertions_envelope(assertions: Any) -> None:
    """Reject anything that is not a well-formed ``assert.v1`` envelope up
    front (a clean usage error, mirroring ``_resolve_audio_mode``'s ValueError
    on a bad ``audio_mode``) -- never silently render a malformed input as an
    empty shelf, which would look identical to "no assertions ran"."""
    if not isinstance(assertions, dict) or assertions.get("schema") != _ASSERT_SCHEMA:
        raise ValueError(
            f"assertions must be an {_ASSERT_SCHEMA!r} envelope dict (build "
            "one with hotato.assert_.run_assertions / "
            f"run_assertions_from_file / run_assertions_from_yaml); got "
            f"{assertions!r}"
        )
    if not isinstance(assertions.get("results"), list) or not isinstance(
        assertions.get("summary"), dict
    ):
        raise ValueError(
            "assertions envelope is missing its 'results' list or 'summary' dict"
        )


def _assertions_headline(assertions: dict, rubric: Optional[dict] = None):
    """``(headline, inconclusive_count)`` from the envelope's summary. The
    headline ALWAYS carries the deterministic pass/fail split and the
    judge-scored count SIDE BY SIDE -- never a merged single number, never an
    ``overall_score``. The judge count comes from the SEPARATE ``rubric.v1``
    envelope (``rubric``) when one is supplied; ``assert.v1``'s own
    ``summary.judge`` stays the ``{0, 0}`` quarantine it always was, so the two
    lanes are never conflated in the count."""
    summary = assertions.get("summary") or {}
    det = summary.get("deterministic") or {}
    d_pass = det.get("pass", 0)
    d_fail = det.get("fail", 0)
    d_inconclusive = det.get("inconclusive", 0)
    if rubric is not None:
        rs = rubric.get("summary") or {}
        j_total = rs.get("pass", 0) + rs.get("fail", 0)
    else:
        judge = summary.get("judge") or {}
        j_total = judge.get("pass", 0) + judge.get("fail", 0)
    headline = (
        f"{d_pass} deterministic pass / {d_fail} fail  "
        f"{j_total} judge-scored (advisory)"
    )
    return headline, d_inconclusive


def _assertion_kind_body(r: dict) -> str:
    """Kind-specific fields already present on THIS result -- nothing here is
    recomputed or fabricated; a field the result did not carry (e.g. a
    passing ``phrase`` assertion carries no extra fields at all) simply
    renders nothing extra."""
    kind = r.get("kind")
    parts = []

    if kind == "pii":
        hits = r.get("hits") or []
        if hits:
            detectors = sorted({h.get("detector") for h in hits})
            rows = "".join(
                f'<div class="asrthit mono">turn {_esc(h.get("turn"))} '
                f'({_esc(h.get("role") or "n/a")}): {_esc(h.get("detector"))}</div>'
                for h in hits
            )
            parts.append(
                f'<div class="asrtline">{len(hits)} hit(s): '
                f'{_esc(", ".join(detectors))}</div>'
                f'<details class="asrtdetail"><summary>hit detail</summary>'
                f'{rows}</details>'
            )
        redacted = r.get("redacted_transcript")
        if redacted:
            rows = "".join(
                '<div class="trow"><span class="tt mono">'
                f'{_esc(t.get("role") or "")}</span>'
                f'<span class="tx">{_esc(t.get("text") or "")}</span></div>'
                for t in redacted
            )
            parts.append(
                '<details class="asrtdetail"><summary>redacted transcript'
                f'</summary><div class="trows">{rows}</div></details>'
            )

    elif kind == "policy":
        pack = r.get("pack")
        if pack:
            parts.append(
                f'<div class="asrtline mono">pack {_esc(pack.get("name"))} '
                f'v{_esc(pack.get("version"))}</div>'
            )
        matched = r.get("matched_rules")
        if matched:
            items = "".join(
                f'<li>{_esc(m.get("rule"))} ({_esc(m.get("type"))})</li>'
                for m in matched
            )
            parts.append(f'<ul class="reasons">{items}</ul>')

    elif kind == "tool_call":
        span_ids = r.get("span_ids")
        if span_ids:
            parts.append(
                '<div class="asrtline mono">spans: '
                f'{_esc(", ".join(span_ids))}</div>'
            )

    elif kind == "outcome":
        met, of = r.get("met"), r.get("of")
        if met is not None and of is not None:
            parts.append(
                f'<div class="asrtline mono">{met} of {of} predicate(s) met</div>'
            )

    return "".join(parts)


def _assertion_card(r: dict) -> str:
    """One PER-DIMENSION TYPED card: id, kind (typed tag + left accent),
    the honesty-wall ``deterministic`` flag stated plainly, the PASS/FAIL/
    INCONCLUSIVE chip, then whatever kind-specific fields this particular
    result actually carries, then its ``reason`` (if any)."""
    status = r.get("status", "INCONCLUSIVE")
    chip_c = _C[_ASSERT_STATUS_COLORS.get(status, "muted")]
    kind = r.get("kind", "")
    accent = _C[_ASSERT_KIND_ACCENT.get(kind, "line")]
    reason = r.get("reason")
    reason_html = f'<div class="asrtreason">{_esc(reason)}</div>' if reason else ""
    return (
        f'<div class="acard" style="border-left:3px solid {accent}">'
        '<div class="achead">'
        f'<div><span class="kindtag mono">{_esc(kind)}</span> '
        f'<span class="mono aid">{_esc(r.get("id", ""))}</span> '
        '<span class="detflag mono">deterministic</span></div>'
        f'<div class="chip small" style="background:{chip_c}">{_esc(status)}</div>'
        '</div>'
        f'{_assertion_kind_body(r)}{reason_html}'
        '</div>'
    )


# --- per-dimension scorecard (a grouped VIEW of the same results, no blend) --
#
# The scorecard groups the SAME deterministic results by their optional
# ``dimension`` TAG into the five report dimensions plus an "Ungrouped" bucket.
# It is an ADDITIONAL grouped view, never a replacement scorer: each dimension
# shows its OWN pass/fail/inconclusive counts and its own typed cards -- there
# is no blended or overall number across dimensions or within one, and no
# untagged result is ever dropped. Absent unless at least one result carries a
# dimension, so an envelope with no dimensions renders exactly as before.


def _assertions_have_dimensions(assertions: Any) -> bool:
    """True iff at least one result in the envelope carries a ``dimension`` tag.
    The scorecard is rendered only then; otherwise the assertions section is
    byte-identical to before this feature existed. Tolerant of a malformed input
    (returns ``False``) because it is consulted for CSS gating BEFORE
    ``_assertions_section`` validates the envelope -- a non-envelope still
    raises the clean ValueError there, never here."""
    if not isinstance(assertions, dict):
        return False
    results = assertions.get("results")
    if not isinstance(results, list):
        return False
    return any(isinstance(r, dict) and r.get("dimension") for r in results)


def _group_results_by_dimension(results: list):
    """Group results by their optional ``dimension`` TAG into the five report
    dimensions plus an untagged "Ungrouped" bucket. Returns
    ``([(dimension, [results]), ...], [untagged results])`` where the first
    list is always the five dimensions in :data:`_REPORT_DIMENSIONS` order.
    Nothing is merged and nothing is dropped: an untagged result (or a result
    tagged with an unknown dimension) always lands in Ungrouped, and every
    dimension keeps its OWN results, hence its own counts."""
    grouped: dict = {d: [] for d in _REPORT_DIMENSIONS}
    ungrouped: list = []
    for r in results:
        dim = r.get("dimension")
        if dim in grouped:
            grouped[dim].append(r)
        else:
            ungrouped.append(r)
    return [(d, grouped[d]) for d in _REPORT_DIMENSIONS], ungrouped


def _status_counts(results: list) -> dict:
    """A dimension's OWN PASS/FAIL/INCONCLUSIVE tally -- three separate counts,
    never combined into a single score."""
    counts = {"PASS": 0, "FAIL": 0, "INCONCLUSIVE": 0}
    for r in results:
        st = r.get("status", "INCONCLUSIVE")
        counts[st] = counts.get(st, 0) + 1
    return counts


def _dim_counts_text(counts: dict) -> str:
    return (f'{counts["PASS"]} pass / {counts["FAIL"]} fail / '
            f'{counts["INCONCLUSIVE"]} inconclusive')


def _scorecard_dim_block(title: str, results: list, *,
                         is_reliability: bool = False,
                         reliability_data: Optional[dict] = None) -> str:
    """One dimension block: its name, its OWN counts, then its typed cards. The
    Reliability dimension renders REAL pass@1/pass@k/pass^k content when
    ``reliability_data`` is supplied (:func:`_reliability_block_html`); with none
    it shows the honest empty-state (no repeated runs) -- never a fabricated
    value. Any other dimension with no results shows an honest empty state."""
    counts = _status_counts(results)
    parts = [
        '<div class="scdim">'
        f'<div class="schead"><span class="scname">{_esc(title)}</span>'
        f'<span class="sccounts mono">{_dim_counts_text(counts)}</span></div>'
    ]
    if is_reliability:
        if reliability_data is not None:
            parts.append(_reliability_block_html(reliability_data))
        else:
            parts.append(
                f'<div class="scplaceholder">{_esc(_RELIABILITY_EMPTY_NOTE)}</div>'
            )
    if results:
        cards = "".join(_assertion_card(r) for r in results)
        parts.append(f'<div class="shelf">{cards}</div>')
    elif not is_reliability:
        parts.append(
            '<div class="scempty">No results tagged to this dimension.</div>'
        )
    parts.append('</div>')
    return "".join(parts)


def _assertions_scorecard(results: list,
                          reliability: Optional[dict] = None) -> str:
    """The per-dimension scorecard body: the five dimension blocks in order,
    then an Ungrouped block for any untagged results. A grouped VIEW of the
    same results -- no blended number anywhere. ``reliability`` (a normalized
    reliability summary, or None) feeds the Reliability dimension's REAL
    pass@1/pass@k/pass^k content; every other dimension ignores it."""
    dims, ungrouped = _group_results_by_dimension(results)
    blocks = [
        _scorecard_dim_block(
            dim.capitalize(), dim_results,
            is_reliability=(dim == _RELIABILITY_DIMENSION),
            reliability_data=(reliability if dim == _RELIABILITY_DIMENSION
                              else None),
        )
        for dim, dim_results in dims
    ]
    if ungrouped:
        blocks.append(
            _scorecard_dim_block("Ungrouped (no dimension tag)", ungrouped)
        )
    return '<div class="scorecard">' + "".join(blocks) + '</div>'


# --- Reliability dimension: REAL pass@1 / pass@k / pass^k content -----------
#
# Reliability is its OWN dimension: pass@1 (single-run pass rate), pass@k (>=1 of
# k passed), pass^k (ALL k passed), with n, k, and a Wilson CI on pass@1 -- each
# number LABELED, tabular mono, NEVER blended into any other dimension and never
# an overall_score. A run_matrix aggregate additionally carries per-variation
# cells (each its own pass^k) and a SIMULATOR_INVALID bucket (broken fixtures,
# excluded from n). When the data came from SIMULATED runs the section is
# labeled origin=simulated -- a simulator's replay reliability is never
# presented as production reliability.


def _normalize_reliability(reliability: Any) -> Optional[dict]:
    """Normalize a caller-supplied reliability summary into the internal shape
    the scorecard's Reliability dimension renders -- or ``None`` when there is
    genuinely NO repetition data (the honest empty-state).

    Accepts either a :func:`hotato.simulate.run_matrix` summary (a
    ``simulate-matrix`` aggregate + per-variation cells + a SIMULATOR_INVALID
    bucket), a bare :func:`hotato.simulate.reliability` dict (carries
    ``pass_at_1`` ...), or a ``{"aggregate": <reliability dict>, "origin": ...}``
    wrapper (what ``hotato test run`` threads through for a repeated recording).
    Never fabricates: an aggregate with ``n == 0`` and no cells and no broken
    fixtures is treated as no data (``None``)."""
    if reliability is None:
        return None
    if not isinstance(reliability, dict):
        raise ValueError(
            "reliability must be a run_matrix summary, a reliability() dict, or "
            f"a {{'aggregate': ...}} wrapper dict; got {reliability!r}"
        )
    if reliability.get("kind") == "simulate-matrix" or (
        "reliability" in reliability and "variation_cells" in reliability
    ):
        agg = reliability.get("reliability") or {}
        invalid = reliability.get("simulator_invalid") or []
        counts = reliability.get("counts") or {}
        invalid_count = counts.get("simulator_invalid", len(invalid))
        norm = {
            "aggregate": agg,
            "cells": reliability.get("variation_cells") or [],
            "invalid": invalid,
            "invalid_count": invalid_count,
            "basis": reliability.get("reliability_basis"),
            "note": reliability.get("reliability_note") or agg.get("note"),
            # run_matrix is always origin=simulated (every produced conversation
            # is labelled simulated -- never real, never merged into a real bucket)
            "origin": "simulated",
            "runs": counts.get("runs", agg.get("n", 0) + invalid_count),
        }
    elif "aggregate" in reliability:
        agg = reliability.get("aggregate") or {}
        invalid = (reliability.get("invalid")
                   or reliability.get("simulator_invalid") or [])
        norm = {
            "aggregate": agg,
            "cells": (reliability.get("cells")
                      or reliability.get("variation_cells") or []),
            "invalid": invalid,
            "invalid_count": reliability.get("invalid_count", len(invalid)),
            "basis": reliability.get("basis"),
            "note": reliability.get("note") or agg.get("note"),
            "origin": reliability.get("origin"),
            "runs": reliability.get("runs", agg.get("n", 0)),
        }
    elif "pass_at_1" in reliability:
        agg = reliability
        norm = {
            "aggregate": agg,
            "cells": [],
            "invalid": [],
            "invalid_count": 0,
            "basis": None,
            "note": reliability.get("note"),
            "origin": reliability.get("origin"),
            "runs": reliability.get("n", 0),
        }
    else:
        raise ValueError(
            "unrecognized reliability payload: expected a run_matrix summary "
            "(kind='simulate-matrix'), a reliability() dict (with 'pass_at_1'), "
            f"or a {{'aggregate': ...}} wrapper; got keys {sorted(reliability)!r}"
        )
    agg = norm["aggregate"]
    n = agg.get("n", 0) if isinstance(agg, dict) else 0
    # No runs, no cells, no broken fixtures == genuinely no repetition data: the
    # honest empty-state, signalled as None (never a zero-row fabricated table).
    if not n and not norm["cells"] and not norm["invalid_count"]:
        return None
    return norm


def _fmt_prob(x: Any) -> str:
    """A stable 3-decimal string for a pass-rate probability (matching the CLI
    matrix summary), or 'n/a' when the value is missing / non-numeric."""
    try:
        return f"{float(x):.3f}"
    except (TypeError, ValueError):
        return "n/a"


def _reliability_ci_text(ci: Optional[dict]) -> str:
    """The Wilson CI on pass@1 as '[low, high] (method)', or 'n/a' when absent
    (n == 0). Read verbatim from the reliability() dict -- never recomputed."""
    if not isinstance(ci, dict):
        return "n/a"
    return (f"[{_fmt_prob(ci.get('low'))}, {_fmt_prob(ci.get('high'))}] "
            f"({ci.get('method', 'wilson')})")


def _reliability_origin_line(origin: Optional[str], runs: Any) -> str:
    """A plain origin + run-count sentence. When origin=simulated it says so
    explicitly -- the simulator's replay reliability, never production."""
    n = runs if isinstance(runs, int) else "?"
    if origin == "simulated":
        return (f"Over {n} simulated run(s) (origin=simulated) -- the simulator's "
                "replay reliability, never production reliability.")
    if origin == "real":
        return (f"Over {n} repeated run(s) of the supplied recording "
                "(origin=real).")
    return f"Over {n} repeated run(s)."


def _reliability_cell_text(cell: dict) -> str:
    """A stable one-line description of a variation cell (locale / rate / noise /
    behavior), matching the CLI matrix summary's per-cell line."""
    return (f"{cell.get('locale')} rate={cell.get('speaking_rate')} "
            f"noise={cell.get('noise')} behavior={cell.get('behavior')}")


_RELIABILITY_ROW_LABELS = (
    ("pass_at_1", "pass@1 (single-run pass rate)"),
    ("pass_at_k", "pass@k (>=1 of k passed)"),
    ("pass_caret_k", "pass^k (all k passed)"),
)


def _reliability_block_html(data: dict) -> str:
    """The Reliability dimension's REAL content: an origin line, a labeled mono
    table of pass@1/pass@k/pass^k + n/k/passes + the Wilson CI, optional
    per-variation-cell rows, and a SIMULATOR_INVALID bucket (excluded from n).
    No blended score anywhere; pass^k stands on its own."""
    agg = data["aggregate"]
    rows = [(label, _fmt_prob(agg.get(key)))
            for key, label in _RELIABILITY_ROW_LABELS]
    rows += [
        ("n (runs in aggregate)", str(agg.get("n", 0))),
        ("k (samples)", str(agg.get("k", agg.get("n", 0)))),
        ("passes", str(agg.get("passes", 0))),
        ("95% Wilson CI (on pass@1)", _reliability_ci_text(agg.get("ci"))),
    ]
    table = "".join(
        f'<tr><td class="rellabel">{_esc(label)}</td>'
        f'<td class="relval mono">{_esc(val)}</td></tr>'
        for label, val in rows
    )
    parts = [
        '<div class="relblock">',
        '<div class="relnote">Reliability is its OWN number -- pass^k is never '
        'blended into any other dimension, and there is no overall_score.</div>',
        '<div class="relorigin">'
        f'{_esc(_reliability_origin_line(data.get("origin"), data.get("runs")))}'
        '</div>',
        f'<table class="reltable"><tbody>{table}</tbody></table>',
    ]
    cells = data.get("cells") or []
    if cells:
        crows = "".join(
            '<tr>'
            f'<td class="rellabel mono">{_esc(_reliability_cell_text(c.get("cell") or {}))}</td>'
            f'<td class="relval mono">{_esc(str((c.get("runs") if c.get("runs") is not None else (c.get("reliability") or {}).get("n", 0))))}</td>'
            f'<td class="relval mono">{_esc(_fmt_prob((c.get("reliability") or {}).get("pass_at_1")))}</td>'
            f'<td class="relval mono">{_esc(_fmt_prob((c.get("reliability") or {}).get("pass_at_k")))}</td>'
            f'<td class="relval mono">{_esc(_fmt_prob((c.get("reliability") or {}).get("pass_caret_k")))}</td>'
            f'<td class="relval mono">{_esc(str((c.get("reliability") or {}).get("n", 0)))}</td>'
            '</tr>'
            for c in cells
        )
        parts.append(
            '<div class="relsub">Per-variation cells (each its own pass^k, never '
            'blended)</div>'
            '<table class="reltable relcells"><thead><tr>'
            '<th>cell</th><th>runs</th><th>pass@1</th><th>pass@k</th>'
            '<th>pass^k</th><th>n</th></tr></thead>'
            f'<tbody>{crows}</tbody></table>'
        )
    invalid_count = data.get("invalid_count") or 0
    if invalid_count:
        items = "".join(
            f'<li class="mono">{_esc(r.get("run_id") or r.get("index"))}: '
            f'{_esc(r.get("reason") or "")}</li>'
            for r in (data.get("invalid") or [])
        )
        reasons = f'<ul class="reasons">{items}</ul>' if items else ""
        parts.append(
            f'<div class="relinvalid">{invalid_count} run(s) SIMULATOR_INVALID '
            '(broken fixtures, excluded from n; never an agent PASS/FAIL).'
            f'{reasons}</div>'
        )
    if data.get("basis"):
        parts.append(f'<div class="relbasis mono">basis: {_esc(data["basis"])}</div>')
    if data.get("note"):
        parts.append(f'<div class="relnote">{_esc(data["note"])}</div>')
    parts.append('</div>')
    return "".join(parts)


def _reliability_block_md(data: dict) -> list:
    """The Markdown mirror of :func:`_reliability_block_html`: same origin line,
    same labeled table, same per-cell rows and SIMULATOR_INVALID bucket."""
    agg = data["aggregate"]
    L = [
        "Reliability is its OWN number -- pass^k is never blended into any other "
        "dimension, and there is no overall_score.",
        "",
        _reliability_origin_line(data.get("origin"), data.get("runs")),
        "",
        _md_row(["metric", "value"]),
        _md_row(["---", "---"]),
    ]
    L += [_md_row([label, _fmt_prob(agg.get(key))])
          for key, label in _RELIABILITY_ROW_LABELS]
    L += [
        _md_row(["n (runs in aggregate)", str(agg.get("n", 0))]),
        _md_row(["k (samples)", str(agg.get("k", agg.get("n", 0)))]),
        _md_row(["passes", str(agg.get("passes", 0))]),
        _md_row(["95% Wilson CI (on pass@1)", _reliability_ci_text(agg.get("ci"))]),
        "",
    ]
    cells = data.get("cells") or []
    if cells:
        L.append("Per-variation cells (each its own pass^k, never blended):")
        L.append("")
        L.append(_md_row(["cell", "runs", "pass@1", "pass@k", "pass^k", "n"]))
        L.append(_md_row(["---"] * 6))
        for c in cells:
            rel = c.get("reliability") or {}
            runs = c.get("runs") if c.get("runs") is not None else rel.get("n", 0)
            L.append(_md_row([
                _reliability_cell_text(c.get("cell") or {}), runs,
                _fmt_prob(rel.get("pass_at_1")), _fmt_prob(rel.get("pass_at_k")),
                _fmt_prob(rel.get("pass_caret_k")), rel.get("n", 0),
            ]))
        L.append("")
    invalid_count = data.get("invalid_count") or 0
    if invalid_count:
        L.append(f"{invalid_count} run(s) SIMULATOR_INVALID (broken fixtures, "
                 "excluded from n; never an agent PASS/FAIL).")
        for r in (data.get("invalid") or []):
            L.append(f"- {r.get('run_id') or r.get('index')}: "
                     f"{r.get('reason') or ''}")
        L.append("")
    if data.get("basis"):
        L.append(f"basis: {data['basis']}")
        L.append("")
    if data.get("note"):
        L.append(data["note"])
        L.append("")
    return L


_JUDGE_STATUS_COLORS = {"PASS": "green", "FAIL": "red",
                        "INCONCLUSIVE": "ember", "ERROR": "ember"}


def _judge_card(r: dict) -> str:
    """One model-judged (``rubric.v1``) card: id, the PASS/FAIL/INCONCLUSIVE/
    ERROR chip, the ``deterministic:false`` flag stated plainly, the full
    provenance line (model + digest, prompt id+version, temperature, cached/
    fresh), the rationale, and citations to the exact transcript turns / trace
    events. Never merged with the deterministic cards; never a score."""
    j = r.get("judge") or {}
    status = r.get("status", "INCONCLUSIVE")
    chip_c = _C[_JUDGE_STATUS_COLORS.get(status, "muted")]
    model = j.get("model", "")
    digest = (j.get("model_digest") or "")
    digest_short = f"@{digest[:12]}" if digest else ""
    cached = "cached" if j.get("cached") else "fresh"
    prov = (
        f'model {_esc(model)}{_esc(digest_short)} · '
        f'{_esc(str(j.get("prompt_id", "")))} v{_esc(str(j.get("prompt_version", "")))} · '
        f'temp {_esc(str(j.get("temperature", 0)))} · {cached}'
    )
    votes = j.get("votes") or []
    if votes:
        _conf = _esc(format(j.get("confidence", 0), ".2f"))
        prov += (f' · votes {_esc(", ".join(votes))}'
                 f' · confidence {_conf}')
    rationale = r.get("rationale")
    rationale_html = f'<div class="asrtreason">{_esc(rationale)}</div>' if rationale else ""
    cites = j.get("citations") or []
    cite_html = ""
    if cites:
        parts = []
        for c in cites:
            if c.get("type") == "trace_event" or c.get("span_id"):
                parts.append(f'trace {_esc(str(c.get("span_id", "")))}')
            else:
                q = c.get("quote", "")
                parts.append(f'turn {_esc(str(c.get("turn", "")))}: “{_esc(q)}”')
        cite_html = ('<div class="jcite mono">cites ' + " · ".join(parts) + '</div>')
    rev = r.get("review") or {}
    review_html = ""
    if rev.get("human_required"):
        review_html = ('<div class="jcite mono">human review required: '
                       + _esc(", ".join(rev.get("reasons") or [])) + '</div>')
    drift_html = ""
    if j.get("drift"):
        d = j["drift"]
        drift_html = ('<div class="jcite mono">DRIFT vs cached: '
                      f'{_esc(str(d.get("cached_status")))} → '
                      f'{_esc(str(d.get("fresh_status")))}</div>')
    return (
        '<div class="jcard" style="border-left:3px solid ' + _C["ember"] + '">'
        '<div class="achead">'
        f'<div><span class="kindtag mono">rubric</span> '
        f'<span class="mono aid">{_esc(r.get("id", ""))}</span> '
        '<span class="detflag mono">deterministic:false</span></div>'
        f'<div class="chip small" style="background:{chip_c}">{_esc(status)}</div>'
        '</div>'
        f'<div class="jprov mono">{prov}</div>'
        f'{rationale_html}{cite_html}{review_html}{drift_html}'
        '</div>'
    )


def _judge_shelf_html(rubric: dict) -> str:
    """The populated "Model-assisted (advisory)" shelf: real counts + one typed
    card per rubric result. Reads ONLY the separate ``rubric.v1`` envelope, so
    the deterministic counts are never touched and there is no ``overall_score``."""
    results = rubric.get("results") or []
    s = rubric.get("summary") or {}
    gated = rubric.get("gated")
    mode = "GATED (a FAIL fails CI)" if gated else "advisory (never gates by itself)"
    head = (
        f'<div class="jsummary mono">{s.get("pass", 0)} pass / {s.get("fail", 0)} '
        f'fail / {s.get("inconclusive", 0)} inconclusive / {s.get("error", 0)} '
        f'error &mdash; model-judged, {mode}, deterministic:false, never merged '
        'into the deterministic counts, no overall_score.</div>'
    )
    if not results:
        return head + '<div class="anempty">No rubric assertions in this run.</div>'
    return head + "".join(_judge_card(r) for r in results)


def _assertions_section(assertions: dict, rubric: Optional[dict] = None,
                        reliability: Optional[dict] = None) -> str:
    """The two-shelf assertion section: "Deterministic" (``assert.v1`` typed
    cards, one per result -- OR, when any result carries a ``dimension`` OR real
    ``reliability`` data is supplied, the same cards grouped into the
    per-dimension scorecard) and "Model-assisted (advisory)". When a separate
    ``rubric.v1`` envelope (``rubric``) is supplied the judge shelf is POPULATED
    with real model-judged results (model id + digest, prompt id+version,
    cached/fresh, rationale, citations); with none it stays the empty note,
    byte-identical to before. ``reliability`` (a NORMALIZED reliability summary,
    or None) feeds the scorecard's Reliability dimension its real pass@1 /
    pass@k / pass^k content. The headline never collapses the deterministic
    pass/fail split and the judge count into one number, the two shelves never
    share a count, and the scorecard never blends dimensions."""
    _validate_assertions_envelope(assertions)
    headline, d_inconclusive = _assertions_headline(assertions, rubric)
    results = assertions.get("results") or []
    if _assertions_have_dimensions(assertions) or reliability is not None:
        # Grouped VIEW: the SAME cards, arranged into the five dimensions. Each
        # dimension keeps its own counts; nothing is merged, nothing dropped.
        det_body = (
            '<div class="scnote">Grouped into the five report dimensions. Each '
            'dimension keeps its OWN pass / fail / inconclusive counts -- there '
            'is no blended or overall number across dimensions or within one. '
            'Untagged results go to Ungrouped; nothing is dropped.</div>'
            + _assertions_scorecard(results, reliability)
        )
    else:
        cards = "".join(_assertion_card(r) for r in results)
        det_body = cards or (
            '<div class="anempty">No deterministic assertions in this run.</div>'
        )
    inconclusive_note = (
        f'<div class="ancap mono">{d_inconclusive} inconclusive (required '
        'context absent; not a failure)</div>' if d_inconclusive else ""
    )
    if rubric is not None:
        judge_title = 'Model-assisted (advisory)'
        judge_body = _judge_shelf_html(rubric)
        tnote = (
            'assert.v1 (deterministic:true) + rubric.v1 (deterministic:false, '
            'advisory) rendered as two separate shelves. Two counts side by '
            'side, never a merged score -- no overall_score anywhere.'
        )
    else:
        judge_title = 'Model-assisted (advisory, quarantined)'
        judge_body = (
            '<div class="anempty">No judge-scored assertions in this build. A '
            'judge (model-scored) kind is a separate, quarantined capability, '
            'not built here -- see docs/ASSERTIONS.md.</div>'
        )
        tnote = (
            'assert.v1: every result below carries a kind tag and '
            'deterministic:true. Two counts side by side, never a merged score '
            '-- no overall_score anywhere.'
        )
    return (
        '<section class="card assertions">'
        '<div class="ctitle">Assertions</div>'
        f'<div class="tnote">{tnote}</div>'
        f'<div class="asrt-headline mono">{_esc(headline)}</div>'
        f'{inconclusive_note}'
        '<div class="shelf-title">Deterministic (audio / timing / '
        'transcript / trace derived)</div>'
        f'<div class="shelf det-shelf">{det_body}</div>'
        f'<div class="shelf-title">{judge_title}</div>'
        f'<div class="shelf judge-shelf">{judge_body}</div>'
        '</section>'
    )


# --- trace context (voice_trace.v1): a pre-built observability artifact ----
#
# report.py never EVALUATES or scores a trace: exactly like ``base`` (a
# previous run envelope), ``transcript`` (an already-produced ASR artifact),
# and ``assertions`` (an already-evaluated assert.v1 envelope), the caller
# hands this module a ``hotato.voice_trace.v1`` object -- the dict
# ``hotato.trace.load_voice_trace_jsonl`` returns (meta keys plus a "spans"
# list), or the equivalent dict/list -- and this purely renders it as data.
#
# This module NEVER does ``import hotato.trace`` at module scope: hotato.trace
# imports hotato.contract, which imports THIS module -- an import across that
# cycle would be circular (the same reason ``_ASSERT_SCHEMA`` is a bare literal
# rather than an ``import hotato.assert_``). The trace is duck-typed instead.
#
# Absent by default: a report built with ``trace=None`` (the default) is
# byte-identical to one built before this feature existed -- no new markup,
# no new CSS.

TraceLike = Any  # a voice_trace.v1 object / dict / span list; never imported here


def _normalize_trace(trace: TraceLike) -> dict:
    """Reduce a ``hotato.voice_trace.v1`` object (the dict
    ``load_voice_trace_jsonl`` returns -- meta keys plus a ``spans`` list) OR a
    bare list of span dicts to the small, JSON-safe, purely-additive payload
    the report renders and folds into the envelope: a ``meta`` dict (every
    top-level key that is not the span list) and a ``spans`` list (each span a
    shallow copy, so the caller's data is never mutated). Nothing here is a
    timing or verdict field; nothing computed here is read by the scorer."""
    if isinstance(trace, dict):
        spans = trace.get("spans") or []
        meta = {k: v for k, v in trace.items() if k != "spans"}
    elif isinstance(trace, (list, tuple)):
        spans, meta = list(trace), {}
    else:
        raise ValueError(
            "trace must be a hotato.voice_trace.v1 object (the dict "
            "hotato.trace.load_voice_trace_jsonl returns -- meta keys plus a "
            f"'spans' list) or a list of span dicts; got {trace!r}"
        )
    return {"meta": meta, "spans": [dict(s) for s in spans]}


def _trace_span_times(span: dict):
    """``(start_txt, end_txt)`` for one span: an interval span
    (``start_sec``/``end_sec``) shows both; a point span (``time_sec``) shows
    the time as the start and leaves the end blank. A missing value renders as
    an empty cell, never a fabricated ``0.00s``."""
    start = span.get("start_sec")
    if start is None:
        start = span.get("time_sec")
    end = span.get("end_sec")

    def _f(v) -> str:
        return "" if v is None else f"{float(v):.2f}s"

    return _f(start), _f(end)


def _trace_span_detail(span: dict) -> str:
    """The one place the trace redaction wall lives: a span flagged
    ``text_redacted: true`` (e.g. an ``asr_partial`` ingested without
    ``--include-text``) NEVER yields its transcript -- it returns the literal
    ``[redacted]`` placeholder, and its ``attributes`` are skipped too (an
    ingested redacted span can still carry the raw text inside ``attributes``,
    so the flag is honored BEFORE anything else is read). A non-redacted span
    shows its ``text`` if it carries one, else a compact ``k=v`` list of its
    ``latency_ms`` and ``attributes``. Context only; nothing here is a score."""
    if span.get("text_redacted"):
        return "[redacted]"
    text = span.get("text")
    if text:
        return str(text)
    parts = []
    if span.get("latency_ms") is not None:
        parts.append(f"latency_ms={span['latency_ms']}")
    attrs = span.get("attributes")
    if isinstance(attrs, dict):
        parts.extend(f"{k}={v}" for k, v in attrs.items())
    return ", ".join(parts)


def _trace_section(trace: dict) -> str:
    """The one collapsed, clearly-labelled call-level "Trace (context, not a
    score)" section: the caller-supplied voice trace rendered as tabular mono,
    one row per span (type, name, start, end, detail). report.py never
    evaluates or scores a trace; this purely renders the already-produced
    artifact. Context only -- it never touches any timing/verdict field, and a
    redacted span shows ``[redacted]`` in its detail cell, never its text."""
    spans = trace.get("spans") or []
    if spans:
        rows = []
        for s in spans:
            start_txt, end_txt = _trace_span_times(s)
            rows.append(
                '<tr>'
                f'<td>{_esc(s.get("type", "event"))}</td>'
                f'<td>{_esc(s.get("name") or "")}</td>'
                f'<td>{_esc(start_txt)}</td>'
                f'<td>{_esc(end_txt)}</td>'
                f'<td>{_esc(_trace_span_detail(s))}</td>'
                '</tr>'
            )
        body = (
            '<table class="tracetab mono"><thead><tr><th>span</th><th>name</th>'
            '<th>start</th><th>end</th><th>detail</th></tr></thead>'
            f'<tbody>{"".join(rows)}</tbody></table>'
        )
    else:
        body = '<div class="anempty">No spans in this trace.</div>'
    return (
        '<details class="card trace">'
        '<summary>Trace (context, not a score)</summary>'
        '<div class="tnote">A caller-supplied voice trace '
        '(hotato.voice_trace.v1): discrete voice-pipeline events -- TTS '
        'cancel/stop, ASR partials, tool calls -- rendered as context '
        'ALONGSIDE the timing above. It is never scored and never fed back '
        'into any measurement: did_yield, talk-over, time to yield, and the '
        'PASS/FAIL verdict are unaffected whether or not a trace is attached. '
        'A redacted span shows [redacted], never its text.</div>'
        f'{body}'
        '</details>'
    )


# --- transcript context (opt-in aid; never a scoring input) ---------------
#
# report.py never imports hotato.transcribe and never runs any speech-to-text:
# the caller already has a Transcript (produced through the strictly opt-in
# [transcribe] extra) and hands it here purely as data. Everything below is
# duck-typed against hotato.transcribe.Transcript / TranscriptSegment (``.text``
# / ``.segments`` / ``.start`` / ``.end`` / ``.model`` / ``.device`` /
# ``.compute_type`` / ``.language``) so this module stays dependency-free, and
# a plain equivalent dict (same keys) works too.

TranscriptLike = Any  # a Transcript-like object or dict; never imported here


def _normalize_segment(seg: Any) -> dict:
    if isinstance(seg, dict):
        return {
            "start": float(seg["start"]),
            "end": float(seg["end"]),
            "text": seg.get("text") or "",
        }
    return {
        "start": float(seg.start),
        "end": float(seg.end),
        "text": getattr(seg, "text", "") or "",
    }


def _normalize_transcript(t: TranscriptLike) -> dict:
    """Reduce a Transcript-like object (or an equivalent dict) to the small,
    JSON-safe, purely-additive payload the report renders: ``text``,
    ``segments`` (each ``{start, end, text}``), and provenance (``model``,
    ``device``, ``compute_type``, ``language``). Nothing here is a timing or
    verdict field; nothing computed here is read by the scorer."""
    if isinstance(t, dict):
        segments = t.get("segments") or []
        return {
            "text": t.get("text") or "",
            "segments": [_normalize_segment(s) for s in segments],
            "model": t.get("model") or "unknown",
            "device": t.get("device") or "unknown",
            "compute_type": t.get("compute_type") or "unknown",
            "language": t.get("language"),
        }
    segments = getattr(t, "segments", None) or []
    return {
        "text": getattr(t, "text", "") or "",
        "segments": [_normalize_segment(s) for s in segments],
        "model": getattr(t, "model", "unknown"),
        "device": getattr(t, "device", "unknown"),
        "compute_type": getattr(t, "compute_type", "unknown"),
        "language": getattr(t, "language", None),
    }


def _is_per_event_mapping(transcript: dict) -> bool:
    """Tell a per-event mapping (``{scenario_id: Transcript-like, ...}``) apart
    from a single Transcript-like dict (``{"text": ..., "segments": ...}``).

    A Transcript-like payload always has a top-level ``"text"`` and/or
    ``"segments"`` key (the dataclass's own fields); a per-event mapping's
    keys are scenario/event ids, which are never those two literal strings in
    practice. This is a heuristic, documented on ``build_report_html``: pass a
    single Transcript-like object (or a dict shaped like one) to apply it to
    every event, or a dict keyed by scenario_id/event_id for one per event."""
    return "text" not in transcript and "segments" not in transcript


def _transcript_for_event(transcript: Optional[TranscriptLike], event: dict) -> Optional[dict]:
    """Resolve the transcript payload (if any) for ONE event.

    ``transcript`` may be a single Transcript-like object -- a
    ``hotato.transcribe.Transcript`` or an equivalent dict shaped like one
    (applied to every event alike -- the natural case for a single-recording
    report) -- or a dict keyed by ``scenario_id`` (falling back to
    ``event_id``) so a suite with one audio file per event can hand each event
    its own transcript (see ``_is_per_event_mapping``). Returns ``None``
    (never a fabricated empty payload) when nothing matches, so an event with
    no transcript stays untouched."""
    if transcript is None:
        return None
    if isinstance(transcript, dict) and _is_per_event_mapping(transcript):
        key = event.get("scenario_id") or event.get("event_id")
        t = transcript.get(key) if key is not None else None
        return _normalize_transcript(t) if t is not None else None
    return _normalize_transcript(transcript)


def _with_transcript_context(event: dict, transcript: Optional[TranscriptLike]) -> dict:
    """Attach transcript CONTEXT to a shallow copy of one envelope event.

    Pure and additive, exactly like ``hotato.transcribe.align_transcript_to_events``:
    returns the SAME event object (untouched) when no transcript matches, or a
    NEW dict (a shallow copy plus exactly one added key, ``transcript_context``)
    when one does. Every existing key -- every timing/verdict field -- passes
    through unchanged; this never mutates the input and never feeds back into
    scoring."""
    payload = _transcript_for_event(transcript, event)
    if payload is None:
        return event
    return dict(event, transcript_context=payload)


def _transcript_panel(model: dict) -> str:
    """Collapsed, clearly-labelled transcript CONTEXT panel for one event.
    Present only when the event actually carries a ``transcript_context``
    (strictly opt-in, attached by the caller via ``transcript=``); absent by
    default, so a plain report renders none of this. States up front, every
    time, that this is context and never a scoring input."""
    ctx = model["event"].get("transcript_context")
    if not ctx:
        return ""
    text = ctx.get("text") or ""
    segs = ctx.get("segments") or []
    if not text and not segs:
        return ""
    if segs:
        rows = "".join(
            '<div class="trow"><span class="tt mono">'
            f'{s["start"]:.2f}-{s["end"]:.2f}s</span>'
            f'<span class="tx">{_esc(s["text"])}</span></div>'
            for s in segs
        )
    else:
        rows = f'<div class="trow"><span class="tx">{_esc(text)}</span></div>'
    prov = []
    if ctx.get("model") and ctx["model"] != "unknown":
        prov.append(f'model {ctx["model"]}')
    if ctx.get("language"):
        prov.append(f'language {ctx["language"]}')
    prov_line = (f'<div class="tprov mono">{_esc(", ".join(prov))}</div>'
                if prov else "")
    return (
        '<details class="transcript"><summary>Transcript '
        '(context, not a score)</summary>'
        '<div class="tnote">Optional speech-to-text aid, aligned next to the '
        'timeline for a human or agent to read. It is NEVER fed back into the '
        'measurement above: did_yield, talk-over, time to yield, and the '
        'PASS/FAIL verdict are unaffected whether or not a transcript is '
        'attached.</div>'
        f'{prov_line}<div class="trows">{rows}</div></details>'
    )


# --- event card -----------------------------------------------------------

def _stat(label: str, value: str, color: Optional[str] = None) -> str:
    col = color or _C["mono"]
    return (
        f'<div class="stat"><span class="k">{_esc(label)}</span>'
        f'<span class="v" style="color:{col}">{_esc(value)}</span></div>'
    )


def _event_card(model: dict, audio_mode: str = AUDIO_NONE) -> str:
    e = model["event"]
    v = e["verdict"]
    status = _event_status(model)
    chip_c = _C[_STATUS_COLORS[status]]
    chip = _STATUS_LABEL[status]

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

    # a not-scorable input carries its reason on the card, never a verdict
    if status == "not_scorable":
        parts.append(
            f'<div class="fix"><b>not scorable</b> '
            f'{_esc(e.get("not_scorable_reason") or "")}</div>'
        )

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

    # the scored audio itself: inlined (self_contained), referenced by hash
    # (audio_reference), or absent (none -- keeps plain reports small).
    if audio_mode == AUDIO_SELF_CONTAINED:
        parts.append(_audio_block(model))
    elif audio_mode == AUDIO_REFERENCE:
        parts.append(_audio_reference_block(model))

    # measured stats (all real)
    parts.append('<div class="stats">')
    parts.append(_stat("caller onset", _s(model["onset"]), _C["ember"]))
    parts.append(_stat("time to yield", _s(model["seconds_to_yield"]),
                       _C["green"] if model["did_yield"] else _C["muted"]))
    parts.append(_stat("talk-over", _s(model["talk_over_sec"]), _C["ember"]))
    parts.append(_stat("response gap", _s(model["response_gap_sec"])))
    parts.append(_stat("premature start", _s(model["premature_start_sec"])))
    parts.append("</div>")

    # reasons (only on a real failure; the not-scorable reason is shown above)
    reasons = v.get("reasons") or []
    if reasons and status == "fail":
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

    # transcript context: opt-in, present only when the caller attached one
    parts.append(_transcript_panel(model))

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
        with _open_regular(path) as fh:
            b64 = base64.b64encode(fh.read()).decode("ascii")
        rows.append(
            f'<div class="audrow"><span class="audk">{_esc(src["label"])}</span>'
            f'<audio controls preload="metadata" '
            f'aria-label="Play clip: {_esc(src["label"])}" '
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


def _source_pcm_sha256(path: str) -> Optional[str]:
    """The content address (decoded-PCM sha256) of a source recording -- the
    SAME digest the fleet store keys audio by (``api.ingest_recording``), so a
    reference here resolves to the stored artifact. None if it cannot be read."""
    if not path or not os.path.exists(path):
        return None
    try:
        from .core import _audio_provenance
        prov = _audio_provenance(("stereo", path))
        return prov["sides"][0]["pcm_sha256"]
    except Exception:  # noqa: BLE001 - a missing hash still leaves a locator
        return None


def _audio_reference_block(model: dict) -> str:
    """Render a STABLE, content-addressed reference to the scored audio instead
    of inlining it. For a fleet-shared report this is the PII-safe default: the
    page names each source by its ``pcm_sha256`` (and a ``recording_id`` when the
    caller passes one) plus a relative locator, and says playback needs the fleet
    store. No ``data:`` URI, no PCM, so a copy shared outside the store leaks no
    spoken audio.

    Each ``audio_sources`` entry may already carry ``recording_id`` / ``pcm_sha256``
    / ``locator`` (a fleet caller has them); anything missing is derived from the
    file (the hash is computed, the locator falls back to the basename)."""
    sources = model.get("audio_sources") or []
    rows = []
    for src in sources:
        path = src.get("path")
        rec_id = src.get("recording_id")
        pcm = src.get("pcm_sha256") or _source_pcm_sha256(path)
        locator = src.get("locator") or (os.path.basename(path) if path else None)
        if not (rec_id or pcm or locator):
            continue
        ref = []
        if rec_id:
            ref.append('<span class="audk">recording_id</span>'
                       f'<span class="audref mono">{_esc(rec_id)}</span>')
        if pcm:
            ref.append('<span class="audk">pcm_sha256</span>'
                       f'<span class="audref mono">{_esc(pcm)}</span>')
        if locator:
            ref.append('<span class="audk">locator</span>'
                       f'<span class="audref mono">{_esc(locator)}</span>')
        rows.append(
            f'<div class="audrow"><span class="audk">{_esc(src.get("label", ""))}</span>'
            + "".join(ref) + "</div>"
        )
    if not rows:
        return ""
    return (
        '<div class="audio audioref">'
        '<div class="audcap">Audio reference only. The spoken audio is NOT in '
        'this file: it stays content-addressed in the fleet store, named here by '
        'hash and locator. Playback requires that store, so a copy of this page '
        'shared outside it carries no caller audio.</div>'
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
        '<details class="card thresholds">'
        '<summary>Thresholds used</summary>'
        '<div class="tnote">Every value the scorer read. Same audio and config '
        'reproduce every number above.</div>'
        f'<div class="thgrid">{cells}</div>'
        '<table class="vadtab"><thead><tr><th>VAD parameter</th>'
        '<th>caller</th><th>agent</th></tr></thead>'
        f'<tbody>{vad_cells}</tbody></table>'
        '</details>'
    )


# --- footer (honest limits, from LIMITS) ----------------------------------

# One canonical destination for "what this does not measure", linked once
# instead of stamping the same negation bullets on every generated report.
_HOW_IT_WORKS_SCOPE_URL = "https://hotato.dev/docs/how-it-works.html#scope"


def _footer() -> str:
    """One Method line carries the whole determinism / reproducibility /
    ceiling / no-accuracy-score story that used to be restated across a
    header line and three separate footer paragraphs. The out-of-scope
    bullet list is folded into a single link to the canonical explanation."""
    return (
        '<footer class="foot">'
        f'<div class="fline"><b>Method.</b> {_esc(LIMITS["method"])} '
        'Deterministic given the same audio and config, with an explicit '
        'ceiling; every threshold above is an exposed parameter and every '
        'frame is inspectable. No accuracy score. '
        f'<a href="{_esc(_HOW_IT_WORKS_SCOPE_URL)}">What this measures, and '
        'what it does not</a>.</div>'
        '</footer>'
    )


# --- page shell -----------------------------------------------------------

_CSS = """
:root{color-scheme:dark}
:where(a,button,summary,audio,[tabindex]):focus-visible{
 outline:3px solid %(ember)s;outline-offset:3px;border-radius:3px}
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
.audref{color:%(mono)s;font-size:11.5px;word-break:break-all;margin-right:6px}
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
details.thresholds summary{cursor:pointer;color:%(cream)s;font-size:16.5px;
 font-weight:650}
.thresholds .tnote{color:%(muted)s;font-size:12.5px;margin:8px 0 12px}
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

# Appended to the page's <style> ONLY when at least one event actually carries
# a transcript_context (see _render_page): a report built without a transcript
# must stay byte-identical to one built before this feature existed, so these
# rules are never baked into the always-present _CSS above.
_TRANSCRIPT_CSS = """
details.transcript{margin-top:12px;border-top:1px solid %(line)s;padding-top:10px}
details.transcript summary{cursor:pointer;color:%(cream)s;font-size:13px;
 font-weight:600}
.transcript .tnote{color:%(muted)s;font-size:12px;margin:8px 0 10px}
.tprov{color:%(muted)s;font-size:11.5px;margin-bottom:6px}
.trows{display:flex;flex-direction:column;gap:6px}
.trow{display:flex;align-items:baseline;gap:10px;font-size:13px}
.tt{color:%(muted)s;font-size:11.5px;white-space:nowrap}
.tx{color:%(cream)s}
""" % _C

# Appended to the page's <style> ONLY when an ``assertions`` envelope was
# actually passed in (see _render_page): a report built with the default
# assertions=None must stay byte-identical to one built before this feature
# existed, so these rules are never baked into the always-present _CSS above.
_ASSERTIONS_CSS = """
.assertions .asrt-headline{font-size:14px;font-weight:650;margin:6px 0 4px;
 color:%(cream)s}
.shelf-title{font-size:12px;font-weight:650;color:%(muted)s;
 text-transform:uppercase;letter-spacing:0.04em;margin:16px 0 8px}
.shelf{display:flex;flex-direction:column;gap:10px}
.acard{background:%(card2)s;border:1px solid %(line)s;border-radius:10px;
 padding:10px 13px}
.achead{display:flex;align-items:center;justify-content:space-between;gap:10px}
.kindtag{background:%(bg)s;border:1px solid %(line)s;border-radius:6px;
 padding:1px 8px;font-size:11px;color:%(muted)s;text-transform:lowercase}
.aid{color:%(muted)s;font-size:12px;margin-left:6px}
.detflag{color:%(muted)s;font-size:10.5px;margin-left:8px;
 text-transform:lowercase}
.chip.small{padding:2px 9px;font-size:11px;border-radius:6px}
.asrtline{margin-top:8px;color:%(muted)s;font-size:12.5px}
.asrtreason{margin-top:8px;color:%(cream)s;font-size:12.5px}
.asrtdetail{margin-top:6px}
.asrtdetail summary{cursor:pointer;color:%(muted)s;font-size:11.5px}
.asrthit{color:%(muted)s;font-size:11.5px;margin-top:4px}
""" % _C

# Appended ONLY when a ``rubric.v1`` envelope was passed (see _render_page), so
# a report with no model-judged lane stays byte-identical. The judge card is
# visually distinct from a deterministic .acard so the two shelves never read as
# one -- an ember accent, the provenance line, and citations.
_JUDGE_CSS = """
.jsummary{color:%(muted)s;font-size:12px;margin-bottom:10px}
.jcard{background:%(card2)s;border:1px solid %(line)s;border-radius:10px;
 padding:10px 13px}
.jprov{color:%(muted)s;font-size:11px;margin-top:8px;word-break:break-word}
.jcite{color:%(muted)s;font-size:11.5px;margin-top:6px}
""" % _C

# Appended to the page's <style> ONLY when a ``trace`` was actually passed in
# (see _render_page): a report built with the default trace=None must stay
# byte-identical to one built before this feature existed, so these rules are
# never baked into the always-present _CSS above.
_TRACE_CSS = """
details.trace summary{cursor:pointer;color:%(cream)s;font-size:16.5px;
 font-weight:650}
.trace .tnote{color:%(muted)s;font-size:12.5px;margin:8px 0 12px}
.trace .anempty{color:%(muted)s;font-size:13px;font-style:italic}
table.tracetab{border-collapse:collapse;width:auto;font-size:12px}
table.tracetab th{text-align:left;color:%(muted)s;font-weight:600;
 font-size:11.5px;padding:5px 18px 5px 0;border-bottom:1px solid %(line)s;
 white-space:nowrap}
table.tracetab td{text-align:left;color:%(mono)s;padding:3px 18px 3px 0;
 border-bottom:1px solid %(card2)s;vertical-align:top}
""" % _C

# Appended to the page's <style> ONLY when at least one assertion result carries
# a ``dimension`` (see _render_page): an assertions envelope with no dimensions
# renders the flat deterministic shelf exactly as before, so it needs none of
# this and stays byte-identical to a report built before the scorecard existed.
_SCORECARD_CSS = """
.scnote{color:%(muted)s;font-size:12px;margin:8px 0 12px}
.scorecard{display:flex;flex-direction:column;gap:14px}
.scdim{border:1px solid %(line)s;border-radius:12px;padding:12px 14px;
 background:%(card2)s}
.schead{display:flex;align-items:baseline;justify-content:space-between;
 gap:10px;flex-wrap:wrap}
.scname{font-size:13.5px;font-weight:650;color:%(cream)s}
.sccounts{font-size:12px;color:%(muted)s}
.scdim .shelf{margin-top:10px}
.scplaceholder,.scempty{color:%(muted)s;font-size:12.5px;font-style:italic;
 margin-top:8px}
.relblock{margin-top:8px}
.relorigin{font-size:12.5px;color:%(cream)s;margin:2px 0 8px}
.relnote{color:%(muted)s;font-size:12px;margin:6px 0}
.relsub,.relbasis,.relinvalid{font-size:12px;color:%(muted)s;margin:8px 0 4px}
.reltable{border-collapse:collapse;font-size:12.5px;margin:4px 0}
.reltable td,.reltable th{border:1px solid %(line)s;padding:4px 8px;
 text-align:left}
.reltable th{color:%(muted)s;font-weight:600}
.rellabel{color:%(cream)s}
.relval{color:%(cream)s;text-align:right}
""" % _C

# Appended to the page's <style> ONLY when a ``conversation`` manifest was
# actually passed in (see _render_page): a report built with the default
# conversation=None must stay byte-identical to one built before this feature
# existed, so these rules are never baked into the always-present _CSS above.
_CONVERSATION_CSS = """
section.card.conversation .cvorigin{font-size:14px;margin:6px 0 4px;
 color:%(cream)s}
.cvchip{color:#15110d;font-weight:800;font-size:11px;letter-spacing:0.06em;
 padding:2px 9px;border-radius:6px;margin-left:4px;text-transform:uppercase}
.cvline{color:%(muted)s;font-size:12px;margin:4px 0;word-break:break-all}
.cvsub{font-size:12px;font-weight:650;color:%(muted)s;text-transform:uppercase;
 letter-spacing:0.04em;margin:14px 0 8px}
table.cvtab{border-collapse:collapse;width:auto;font-size:12px}
table.cvtab th{text-align:left;color:%(muted)s;font-weight:600;font-size:11.5px;
 padding:5px 18px 5px 0;border-bottom:1px solid %(line)s;white-space:nowrap}
table.cvtab td{text-align:left;color:%(mono)s;padding:3px 18px 3px 0;
 border-bottom:1px solid %(card2)s;vertical-align:top;word-break:break-all}
""" % _C


# --- conversation artifact (hotato.conversation.v1): provenance CONTEXT ------
#
# report.py never verifies or scores a conversation artifact: exactly like
# ``base``, ``transcript``, ``assertions``, and ``trace``, the caller hands this
# module an already-built ``hotato.conversation.v1`` manifest dict (build one
# with ``hotato.conversation.build_manifest``; verify digests with
# ``hotato.conversation.verify``) and this purely renders its ORIGIN
# (real|simulated, invariant 5 -- synthetic is never conflated with real) and
# the artifact digests it binds. It never re-hashes a child and never touches
# any timing/verdict field.
#
# Duck-typed as data (never ``import hotato.conversation`` here), matching the
# trace integration: the manifest is read structurally, and the origin-kind
# vocabulary is a bare tuple mirroring ``hotato.conversation.ORIGIN_KINDS``.
#
# Absent by default: a report built with ``conversation=None`` (the default) is
# byte-identical to one built before this feature existed -- no new markup, no
# new CSS.

ConversationLike = Any  # a hotato.conversation.v1 manifest dict; never imported

_ORIGIN_KINDS = ("real", "simulated")  # mirrors hotato.conversation.ORIGIN_KINDS


def _normalize_conversation(conversation: ConversationLike) -> dict:
    """Reduce a ``hotato.conversation.v1`` manifest to the small, render-only
    payload the report shows: origin (kind + provider/simulator), the bound
    artifact digests, the ids, and the scenario/release digests. A shallow copy,
    so the caller's manifest is never mutated; nothing here is a timing or
    verdict field. Rejects a non-dict or a missing/invalid ``origin.kind`` up
    front (a clean usage error, mirroring ``_normalize_trace``) -- invariant 5:
    the real/simulated axis is required, synthetic never conflated with real."""
    if not isinstance(conversation, dict):
        raise ValueError(
            "conversation must be a hotato.conversation.v1 manifest dict (build "
            "one with hotato.conversation.build_manifest); got "
            f"{conversation!r}"
        )
    origin = conversation.get("origin")
    if not isinstance(origin, dict):
        raise ValueError(
            "conversation manifest is missing its 'origin' mapping (origin.kind "
            "is required -- synthetic is never conflated with real)"
        )
    kind = origin.get("kind")
    if kind not in _ORIGIN_KINDS:
        raise ValueError(
            f"conversation origin.kind is required and must be one of "
            f"{_ORIGIN_KINDS} (synthetic is never conflated with real), got "
            f"{kind!r}"
        )
    artifacts = {
        k: dict(v)
        for k, v in (conversation.get("artifacts") or {}).items()
        if isinstance(v, dict)
    }
    return {
        "conversation_id": conversation.get("conversation_id"),
        "agent_id": conversation.get("agent_id"),
        "created_at": conversation.get("created_at"),
        "origin": dict(origin),
        "artifacts": artifacts,
        "scenario_digest": conversation.get("scenario_digest"),
        "release_digest": conversation.get("release_digest"),
    }


def _origin_detail_html(origin: dict) -> str:
    """The provider line and, for a SIMULATED origin, the simulator block -- so
    a synthetic conversation always declares its model/scenario/seed and is
    never mistaken for a real recording."""
    parts = []
    prov, call_id = origin.get("provider"), origin.get("provider_call_id")
    if prov or call_id:
        bits = []
        if prov:
            bits.append(f"provider {_esc(prov)}")
        if call_id:
            bits.append(f"call {_esc(call_id)}")
        parts.append(f'<div class="cvline mono">{" &middot; ".join(bits)}</div>')
    if origin.get("kind") == "simulated":
        sim = origin.get("simulator") or {}
        parts.append(
            '<div class="cvline mono">simulator: model '
            f'{_esc(sim.get("model_id"))}, scenario {_esc(sim.get("scenario_id"))}, '
            f'seed {_esc(sim.get("seed"))}</div>'
        )
    return "".join(parts)


def _conversation_digests_html(cv: dict) -> str:
    artifacts = cv.get("artifacts") or {}
    if not artifacts:
        return '<div class="anempty">No artifacts bound in this manifest.</div>'
    rows = []
    for name in sorted(artifacts):
        ref = artifacts[name]
        nbytes = ref.get("bytes")
        size = f"{nbytes} bytes" if isinstance(nbytes, int) else ""
        rows.append(
            '<tr>'
            f'<td>{_esc(name)}</td>'
            f'<td class="mono">{_esc(ref.get("sha256") or "")}</td>'
            f'<td class="mono">{_esc(size)}</td>'
            '</tr>'
        )
    return (
        '<table class="cvtab mono"><thead><tr><th>artifact</th><th>sha256</th>'
        '<th>size</th></tr></thead>'
        f'<tbody>{"".join(rows)}</tbody></table>'
    )


def _conversation_section(cv: dict) -> str:
    """One "Conversation artifact (provenance)" section: the source this report
    was produced from (hotato.conversation.v1). It states whether the call was
    REAL or SIMULATED (never conflated) and lists the evidence it binds by
    sha256. Provenance, never a score; it changes no measurement above."""
    origin = cv["origin"]
    kind = origin.get("kind")
    kind_c = _C["green"] if kind == "real" else _C["ember"]
    meta = []
    for label, key in (("conversation", "conversation_id"),
                       ("agent", "agent_id"),
                       ("created", "created_at")):
        val = cv.get(key)
        if val:
            meta.append(f'<span class="pill">{label} <b>{_esc(val)}</b></span>')
    meta_html = f'<div class="metarow">{"".join(meta)}</div>' if meta else ""
    extra_digests = "".join(
        f'<div class="cvline mono">{label}: {_esc(cv.get(key))}</div>'
        for label, key in (("scenario_digest", "scenario_digest"),
                           ("release_digest", "release_digest"))
        if cv.get(key)
    )
    return (
        '<section class="card conversation">'
        '<div class="ctitle">Conversation artifact (provenance)</div>'
        '<div class="tnote">The source this report was produced from '
        '(hotato.conversation.v1). It states whether the call was a REAL '
        'recording or a SIMULATED one -- the two are never conflated -- and '
        'binds each piece of evidence by sha256. Provenance, never a score; it '
        'changes no measurement above.</div>'
        '<div class="cvorigin">origin '
        f'<span class="cvchip" style="background:{kind_c}">{_esc(kind)}</span>'
        '</div>'
        f'{_origin_detail_html(origin)}'
        f'{meta_html}'
        '<div class="cvsub">Bound evidence (by digest)</div>'
        f'{_conversation_digests_html(cv)}'
        f'{extra_digests}'
        '</section>'
    )


def _conversation_md(cv: dict) -> list:
    """The Markdown mirror of ``_conversation_section``: origin (real|simulated,
    with the simulator block when simulated) and the bound artifact digests."""
    origin = cv["origin"]
    kind = origin.get("kind")
    L = ["## Conversation artifact (provenance)", ""]
    L.append("The source this report was produced from (hotato.conversation.v1): "
             "whether the call was a REAL recording or a SIMULATED one -- never "
             "conflated -- plus each piece of evidence bound by sha256. "
             "Provenance, never a score; it changes no measurement above.")
    L.append("")
    L.append(f"- origin: {kind}")
    if origin.get("provider"):
        L.append(f"- provider: {origin['provider']}")
    if origin.get("provider_call_id"):
        L.append(f"- provider_call_id: {origin['provider_call_id']}")
    if kind == "simulated":
        sim = origin.get("simulator") or {}
        L.append(f"- simulator: model {sim.get('model_id')}, scenario "
                 f"{sim.get('scenario_id')}, seed {sim.get('seed')}")
    for label in ("conversation_id", "agent_id", "created_at"):
        if cv.get(label):
            L.append(f"- {label}: {cv[label]}")
    for key in ("scenario_digest", "release_digest"):
        if cv.get(key):
            L.append(f"- {key}: {cv[key]}")
    L.append("")
    artifacts = cv.get("artifacts") or {}
    if artifacts:
        L.append(_md_row(["artifact", "sha256", "size"]))
        L.append(_md_row(["---"] * 3))
        for name in sorted(artifacts):
            ref = artifacts[name]
            nbytes = ref.get("bytes")
            size = f"{nbytes} bytes" if isinstance(nbytes, int) else ""
            L.append(_md_row([name, ref.get("sha256") or "", size]))
    else:
        L.append("No artifacts bound in this manifest.")
    L.append("")
    return L


def _render_page(env: dict, models: list, cfg: ScoreConfig,
                 base_env: Optional[dict] = None,
                 base_label: Optional[str] = None,
                 audio_mode: str = AUDIO_NONE,
                 assertions: Optional[dict] = None,
                 trace: Optional[dict] = None,
                 conversation: Optional[dict] = None,
                 rubric: Optional[dict] = None,
                 reliability: Optional[dict] = None) -> str:
    s = env["summary"]
    # A real failure always dominates: REGRESSION beats NOT SCORABLE beats
    # ALL PASS. NOT SCORABLE appears only when nothing failed but at least one
    # input could not be judged.
    if s["failed"] > 0:
        overall_c, overall_t = _C["red"], "REGRESSION"
    elif s.get("not_scorable", 0) > 0:
        overall_c, overall_t = _C["ember"], "NOT SCORABLE"
    else:
        overall_c, overall_t = _C["green"], "ALL PASS"

    eng = env.get("engine", {})
    mode = env.get("mode", "")
    stack = env.get("stack", "generic")
    if env.get("suite"):
        mode_label = f"suite: {env['suite']}"
    else:
        mode_label = mode

    cards = "".join(_event_card(m, audio_mode=audio_mode) for m in models)

    # Extra CSS is appended ONLY when a transcript panel actually rendered (an
    # empty transcript_context, e.g. {"text": "", "segments": []}, renders no
    # panel and needs none), so a plain report stays byte-identical to before
    # this feature existed -- nothing about the stylesheet changes otherwise.
    # Same rule for the assertions CSS: appended ONLY when an assertions
    # envelope was actually passed in, so assertions=None (the default) stays
    # byte-identical to a report built before this feature existed.
    style_css = _CSS
    if 'details class="transcript"' in cards:
        style_css += _TRANSCRIPT_CSS
    if assertions is not None:
        style_css += _ASSERTIONS_CSS
    # The judge-shelf CSS is appended ONLY when a rubric.v1 envelope was
    # actually supplied, so a report with no rubric lane stays byte-identical.
    if rubric is not None:
        style_css += _JUDGE_CSS
    # The scorecard CSS is appended ONLY when a result actually carries a
    # dimension OR real reliability data was supplied, so an assertions envelope
    # with no dimensions and reliability=None renders the flat deterministic
    # shelf byte-identically to before the scorecard existed.
    if assertions is not None and (
        _assertions_have_dimensions(assertions) or reliability is not None
    ):
        style_css += _SCORECARD_CSS
    if trace is not None:
        style_css += _TRACE_CSS
    # Conversation-provenance CSS is appended ONLY when a manifest was supplied,
    # so conversation=None (the default) stays byte-identical.
    if conversation is not None:
        style_css += _CONVERSATION_CSS

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
        '<div class="metarow">'
        f'<span class="pill"><b>{_esc(mode_label)}</b></span>'
        f'<span class="pill">stack <b>{_esc(stack)}</b></span>'
        f'<span class="pill">engine <b>{_esc(eng.get("name", ""))}</b> '
        f'{_esc(eng.get("version", ""))}</span>'
        '<span class="pill">offline <b>yes</b></span>'
        '</div></div></header>'
    )

    # The counts line mirrors the CLI text: not_scorable=N appears only when
    # N > 0, so fully-scorable pages stay byte-identical.
    n_ns = s.get("not_scorable", 0)
    # The headline fraction is over SCORABLE events only (passed + failed); a
    # not-scorable event is an input problem, excluded from both sides of the
    # ratio so it never deflates the big number. It is surfaced separately in the
    # label and its own section below.
    n_scorable = s["passed"] + s["failed"]
    pass_label = "events pass"
    if n_ns:
        pass_label = f'events pass (failed={s["failed"]}, not_scorable={n_ns})'

    summary = (
        '<div class="summary">'
        f'<div><div class="bignum">{s["passed"]} of {n_scorable}</div>'
        '<div class="subtle" style="color:' + _C["muted"] + f'">{pass_label}</div></div>'
        f'<div class="chip" style="background:{overall_c}">{overall_t}</div>'
        f'{legend}'
        '</div>'
    )

    base_html = _base_section(env, base_env, base_label) if base_env else ""

    # The analytics rollup reads AFTER the event cards it aggregates (a reader
    # sees the individual moments before the summary of them), and only when
    # there are enough events for a rollup to say anything a single card
    # doesn't already: fewer than 3 events, and it is skipped entirely.
    analytics_html = _analytics_section(env, models) if len(models) >= 3 else ""

    # The assertions section (assert.v1, two shelves) reads after the timing
    # analytics: timing and assertions are two different axes of measurement,
    # so a reader sees the timing story completely before the separate
    # assertion story starts. Absent by default (assertions=None), so a plain
    # report carries none of this markup.
    assertions_html = (
        _assertions_section(assertions, rubric, reliability)
        if assertions is not None else ""
    )

    # The trace section (voice_trace.v1, one call-level table) reads after the
    # assertion story: it is supplementary observability CONTEXT, never a score,
    # so it sits last before the "Thresholds used" config panel. Absent by
    # default (trace=None), so a plain report carries none of this markup.
    trace_html = _trace_section(trace) if trace is not None else ""

    # The conversation-artifact provenance (real|simulated + bound digests)
    # reads right after the headline summary -- a reader sees WHAT the report
    # was produced from (and whether it was a real or simulated call) before the
    # per-event detail. Absent by default (conversation=None), so a plain report
    # carries none of this markup.
    conversation_html = (
        _conversation_section(conversation) if conversation is not None else ""
    )

    body = (
        f'<div class="wrap">{head}<main>{summary}'
        f'{conversation_html}'
        f'{_not_scorable_section_html(env)}'
        f'{base_html}{cards}'
        f'{analytics_html}{assertions_html}{trace_html}'
        f'{_thresholds(cfg)}</main>{_footer()}</div>'
    )

    # Distinguishing title + description built only from measured counts.
    title = "hotato report"
    if env.get("suite"):
        title += f": {_esc(env['suite'])} suite"
    title += f", {s['passed']} of {n_scorable} events pass"
    desc = (
        f"Self-contained hotato report: {n_scorable} events scored offline, "
        f"{s['passed']} pass, {s['failed']} fail. Every value is a real "
        "measurement from the scorer; the page embeds its own evidence and "
        "opens offline."
    )

    return (
        "<!doctype html>\n<html lang=\"en\"><head>"
        "<meta charset=\"utf-8\">"
        "<meta name=\"viewport\" content=\"width=device-width, initial-scale=1\">"
        f"<title>{title}</title>"
        f"<meta name=\"description\" content=\"{_esc(desc)}\">"
        f"<style>{style_css}</style></head><body>{body}</body></html>\n"
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


def _assertion_md_row(r: dict) -> str:
    """One result as a Markdown table row (id, kind, status, deterministic,
    detail) -- the same cells used by the flat table and the scorecard."""
    return _md_row([
        r.get("id", ""), r.get("kind", ""), r.get("status", ""),
        str(bool(r.get("deterministic", True))).lower(),
        r.get("reason", ""),
    ])


def _assertions_scorecard_md(results: list,
                             reliability: Optional[dict] = None) -> list:
    """The Markdown mirror of ``_assertions_scorecard``: a per-dimension
    subsection with its OWN counts and table, then an Ungrouped subsection for
    untagged results. No blended number; nothing dropped. ``reliability`` (a
    normalized reliability summary, or None) feeds the Reliability dimension its
    real pass@1 / pass@k / pass^k content."""
    dims, ungrouped = _group_results_by_dimension(results)
    L = ["Grouped into the five report dimensions. Each dimension keeps its OWN "
         "pass / fail / inconclusive counts -- no blended or overall number "
         "across dimensions or within one. Untagged results go to Ungrouped; "
         "nothing is dropped.", ""]
    for dim, dim_results in dims:
        counts = _status_counts(dim_results)
        L.append(f"#### {dim.capitalize()}")
        L.append("")
        L.append(_dim_counts_text(counts))
        L.append("")
        if dim == _RELIABILITY_DIMENSION:
            if reliability is not None:
                L.extend(_reliability_block_md(reliability))
            else:
                L.append(_RELIABILITY_EMPTY_NOTE)
                L.append("")
        if dim_results:
            L.append(_md_row(["id", "kind", "status", "deterministic", "detail"]))
            L.append(_md_row(["---"] * 5))
            for r in dim_results:
                L.append(_assertion_md_row(r))
            L.append("")
        elif dim != _RELIABILITY_DIMENSION:
            L.append("No results tagged to this dimension.")
            L.append("")
    if ungrouped:
        counts = _status_counts(ungrouped)
        L.append("#### Ungrouped (no dimension tag)")
        L.append("")
        L.append(_dim_counts_text(counts))
        L.append("")
        L.append(_md_row(["id", "kind", "status", "deterministic", "detail"]))
        L.append(_md_row(["---"] * 5))
        for r in ungrouped:
            L.append(_assertion_md_row(r))
        L.append("")
    return L


def _judge_shelf_md(rubric: dict) -> list:
    """The Markdown mirror of the populated judge shelf: real counts + a table
    of rubric results with model + provenance + rationale. Reads ONLY the
    separate ``rubric.v1`` envelope; never merged with the deterministic
    counts."""
    results = rubric.get("results") or []
    s = rubric.get("summary") or {}
    gated = rubric.get("gated")
    mode = "GATED (a FAIL fails CI)" if gated else "advisory (never gates by itself)"
    L = [
        f"{s.get('pass', 0)} pass / {s.get('fail', 0)} fail / "
        f"{s.get('inconclusive', 0)} inconclusive / {s.get('error', 0)} error "
        f"-- model-judged, {mode}, deterministic:false, never merged into the "
        "deterministic counts, no overall_score.",
        "",
    ]
    if not results:
        L.append("No rubric assertions in this run.")
        L.append("")
        return L
    L.append(_md_row(["id", "status", "deterministic", "model", "cached", "rationale"]))
    L.append(_md_row(["---"] * 6))
    for r in results:
        j = r.get("judge") or {}
        digest = (j.get("model_digest") or "")
        model = j.get("model", "")
        if digest:
            model = f"{model}@{digest[:12]}"
        cached = "cached" if j.get("cached") else "fresh"
        L.append(_md_row([
            r.get("id", ""), r.get("status", ""), "false", model, cached,
            (r.get("rationale") or "").replace("|", "\\|"),
        ]))
    L.append("")
    return L


def _assertions_md(assertions: dict, rubric: Optional[dict] = None,
                   reliability: Optional[dict] = None) -> list:
    """The Markdown mirror of ``_assertions_section``: same two shelves, same
    headline, as tables instead of typed HTML cards. When any result carries a
    ``dimension`` OR real ``reliability`` data is supplied, the deterministic
    shelf is grouped into the per-dimension scorecard (each dimension its own
    counts, no blend). When a separate ``rubric.v1`` envelope is supplied the
    judge shelf is POPULATED with real model-judged rows; with none it stays the
    empty note, byte-identical to before this feature existed. ``reliability``
    (a normalized reliability summary, or None) feeds the Reliability
    dimension's real pass@1 / pass@k / pass^k content."""
    _validate_assertions_envelope(assertions)
    headline, d_inconclusive = _assertions_headline(assertions, rubric)
    results = assertions.get("results") or []

    L = ["## Assertions", ""]
    L.append("assert.v1: every result carries a kind tag and deterministic:true. "
             "Two counts side by side, never a merged score -- no overall_score "
             "anywhere.")
    L.append("")
    L.append(f"**{headline}**")
    if d_inconclusive:
        L.append("")
        L.append(f"{d_inconclusive} inconclusive (required context absent; "
                 "not a failure).")
    L.append("")
    L.append("### Deterministic (audio / timing / transcript / trace derived)")
    L.append("")
    if _assertions_have_dimensions(assertions) or reliability is not None:
        L.extend(_assertions_scorecard_md(results, reliability))
    else:
        if results:
            L.append(_md_row(["id", "kind", "status", "deterministic", "detail"]))
            L.append(_md_row(["---"] * 5))
            for r in results:
                L.append(_assertion_md_row(r))
        else:
            L.append("No deterministic assertions in this run.")
        L.append("")
    if rubric is not None:
        L.append("### Model-assisted (advisory)")
        L.append("")
        L.extend(_judge_shelf_md(rubric))
    else:
        L.append("### Model-assisted (advisory, quarantined)")
        L.append("")
        L.append("No judge-scored assertions in this build. A judge (model-scored) "
                 "kind is a separate, quarantined capability, not built here -- "
                 "see docs/ASSERTIONS.md.")
        L.append("")
    return L


def _trace_md(trace: dict) -> list:
    """The Markdown mirror of ``_trace_section``: the same caller-supplied
    voice trace as a table, one row per span, with the same redaction wall (a
    ``text_redacted: true`` span shows ``[redacted]``, never its text)."""
    spans = trace.get("spans") or []
    L = ["## Trace (context, not a score)", ""]
    L.append("A caller-supplied voice trace (hotato.voice_trace.v1): discrete "
             "voice-pipeline events rendered as context alongside the timing "
             "above. Never scored, never fed back into any measurement -- "
             "did_yield, talk-over, time to yield, and the PASS/FAIL verdict "
             "are unaffected. A redacted span shows [redacted], never its text.")
    L.append("")
    if spans:
        L.append(_md_row(["span", "name", "start", "end", "detail"]))
        L.append(_md_row(["---"] * 5))
        for s in spans:
            start_txt, end_txt = _trace_span_times(s)
            L.append(_md_row([
                s.get("type", "event"), s.get("name") or "",
                start_txt, end_txt, _trace_span_detail(s),
            ]))
    else:
        L.append("No spans in this trace.")
    L.append("")
    return L


def _render_md(env: dict, models: list, cfg: ScoreConfig,
               base_env: Optional[dict] = None,
               base_label: Optional[str] = None,
               assertions: Optional[dict] = None,
               trace: Optional[dict] = None,
               conversation: Optional[dict] = None,
               rubric: Optional[dict] = None,
               reliability: Optional[dict] = None) -> str:
    s = env["summary"]
    eng = env.get("engine", {})
    mode_label = f"suite: {env['suite']}" if env.get("suite") else env.get("mode", "")
    # Same precedence as the HTML chip: REGRESSION beats NOT SCORABLE beats
    # ALL PASS.
    if s["failed"] > 0:
        verdict = "REGRESSION"
    elif s.get("not_scorable", 0) > 0:
        verdict = "NOT SCORABLE"
    else:
        verdict = "ALL PASS"
    n_ns = s.get("not_scorable", 0)
    counts = f" (failed={s['failed']}, not_scorable={n_ns})" if n_ns else ""
    # Headline fraction over scorable events only (passed + failed); not-scorable
    # input problems are excluded from the ratio and reported separately.
    n_scorable = s["passed"] + s["failed"]

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
    L.append(f"**{s['passed']} of {n_scorable} events pass{counts}.** "
             f"Verdict: {verdict}.")
    L.append("")
    # Conversation-artifact provenance (real|simulated + bound digests) reads
    # right after the headline summary, mirroring the HTML. Absent by default
    # (conversation=None), so a plain report's Markdown is byte-identical.
    if conversation is not None:
        L.extend(_conversation_md(conversation))
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
            _STATUS_LABEL[_event_status(m)],
        ]))
    L.append("")

    # failures: reasons + fix (real failures only, never not-scorable inputs)
    failed = [m for m in models if _event_status(m) == "fail"]
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

    # not-scorable inputs: id + reason, never listed as failures
    ns_events = _not_scorable_events(env)
    if ns_events:
        L.append("## Not scorable inputs")
        L.append("")
        L.append("Input problems, never agent verdicts. These events are "
                 "excluded from the pass/fail counts, the failure clusters, "
                 "and the timing distributions.")
        L.append("")
        for e in ns_events:
            sid = e.get("scenario_id") or e.get("event_id") or "event"
            L.append(f"- {sid}: {e.get('not_scorable_reason') or ''}")
        L.append("")

    # transcripts: opt-in ASR context, present only on events that carry one
    with_transcript = [e for e in env["events"] if e.get("transcript_context")]
    if with_transcript:
        L.append("## Transcripts (context, not a score)")
        L.append("")
        L.append("Optional speech-to-text aid, never fed back into the "
                 "measurement above: did_yield, talk-over, time to yield, "
                 "and the PASS/FAIL verdict are unaffected whether or not a "
                 "transcript is attached.")
        L.append("")
        for e in with_transcript:
            sid = e.get("scenario_id") or e.get("event_id") or "event"
            ctx = e["transcript_context"]
            L.append(f"### {sid}")
            L.append("")
            segs = ctx.get("segments") or []
            if segs:
                for s in segs:
                    L.append(f"- {s['start']:.2f}-{s['end']:.2f}s: {s['text']}")
            elif ctx.get("text"):
                L.append(f"- {ctx['text']}")
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
        L.append(_no_failures_text(env))
    L.append("")

    # assertions (assert.v1): absent by default, byte-identical without it
    if assertions is not None:
        L.extend(_assertions_md(assertions, rubric, reliability))

    # trace (voice_trace.v1): absent by default, byte-identical without it
    if trace is not None:
        L.extend(_trace_md(trace))

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
                f"{_STATUS_LABEL[r['status_base']]} to "
                f"{_STATUS_LABEL[r['status_cur']]}",
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
    transcript: Optional[TranscriptLike] = None,
    trace: Optional[TraceLike] = None,
    cfg: Optional[ScoreConfig] = None,
):
    """Score the input and return ``(envelope, models, cfg)``: everything both
    renderers (HTML and Markdown) need, all from real measurements.

    ``transcript`` (default ``None``) attaches optional ASR CONTEXT: a single
    Transcript-like object (applied to every event alike) or a dict keyed by
    ``scenario_id``/``event_id`` (one transcript per suite event). It is
    additive only -- ``env["events"]`` is byte-identical to before whenever
    ``transcript`` is ``None`` or does not match a given event.

    ``trace`` (default ``None``) attaches an optional call-level voice trace as
    CONTEXT and is folded into the envelope as an additive top-level
    ``trace_context`` key (``{"meta", "spans"}``). It is additive only -- the
    envelope carries no ``trace_context`` at all whenever ``trace`` is
    ``None``, so a plain report's envelope is byte-identical to before."""
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
        if transcript is not None:
            env["events"] = [_with_transcript_context(e, transcript)
                             for e in env["events"]]
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
        if transcript is not None:
            env["events"] = [_with_transcript_context(e, transcript)
                             for e in env["events"]]
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

    # Fold the call-level voice trace into the envelope as one additive
    # top-level key (the trace is per-call, not per-event, so unlike
    # transcript_context it lives on the envelope itself). Only when a trace
    # was actually supplied, so a trace=None envelope stays byte-identical.
    if trace is not None:
        env["trace_context"] = _normalize_trace(trace)

    return env, models, cfg


def build_report_html(*, base: Optional[dict] = None,
                      base_label: Optional[str] = None,
                      embed_audio: bool = False,
                      audio_mode: Optional[str] = None,
                      transcript: Optional[TranscriptLike] = None,
                      assertions: Optional[dict] = None,
                      trace: Optional[TraceLike] = None,
                      conversation: Optional[ConversationLike] = None,
                      rubric: Optional[dict] = None,
                      reliability: Optional[dict] = None,
                      **kwargs):
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

    ``audio_mode`` selects the audio treatment explicitly (``none`` /
    ``self_contained`` / ``audio_reference``) and, when given, wins over
    ``embed_audio``. ``audio_reference`` renders a content-addressed reference
    (``pcm_sha256`` + locator) instead of the PCM, so a fleet-shared page leaks
    no caller audio.

    ``transcript`` (default ``None``) attaches an optional ASR transcript as
    CONTEXT: a single ``hotato.transcribe.Transcript`` (or an equivalent dict
    shaped like one, i.e. carrying a top-level ``"text"`` and/or ``"segments"``
    key -- applied to every event alike), or a dict keyed by
    ``scenario_id``/``event_id`` whose VALUES are Transcript-like (one per
    suite event; distinguished from the single-object dict by NOT having a
    top-level ``"text"``/``"segments"`` key). It renders as a collapsed
    "Transcript (context, not a score)" panel per event and is folded into the
    returned envelope as an additive ``transcript_context`` key; it never
    touches any timing/verdict field, and a report built with
    ``transcript=None`` (the default) is byte-identical to one built before
    this parameter existed. This function never imports
    ``hotato.transcribe`` -- the caller produces the ``Transcript`` (through
    the strictly opt-in ``[transcribe]`` extra) and hands it here as data.

    ``assertions`` (default ``None``) attaches an already-evaluated
    ``assert.v1`` envelope (build one with ``hotato.assert_.run_assertions`` /
    ``run_assertions_from_file`` / ``run_assertions_from_yaml``; this function
    never evaluates an assertion itself, exactly like ``base``). It renders as
    a new "Assertions" section with two shelves: "Deterministic" (one typed
    card per result -- phrase, pii, policy, tool_call, outcome) and
    "Model-assisted (advisory, quarantined)" (always empty in this build, with
    a note). The headline is always the deterministic pass/fail split plus the
    judge-scored count side by side -- never a merged number, and there is no
    ``overall_score`` anywhere. ``None`` (the default) leaves the HTML
    byte-identical to a report built before this parameter existed. See
    ``docs/ASSERTIONS.md``.

    ``trace`` (default ``None``) attaches an optional voice trace as CONTEXT: a
    ``hotato.voice_trace.v1`` object as loaded by
    ``hotato.trace.load_voice_trace_jsonl`` (a meta dict plus a list of span
    dicts) or the equivalent dict/list. This function never evaluates or scores
    a trace itself, exactly like ``base`` and ``assertions``. It renders as a
    collapsed, clearly-labelled call-level "Trace (context, not a score)"
    section (a mono span table) and is folded into the returned envelope as an
    additive top-level ``trace_context`` key; it never touches any timing/
    verdict field, and a span carrying ``text_redacted: true`` shows a
    ``[redacted]`` placeholder, never its text. ``None`` (the default) leaves
    the HTML byte-identical to a report built before this parameter existed.
    report.py never imports ``hotato.trace`` (a circular import); the trace is
    duck-typed as data. See ``docs/REPORTS.md`` and ``docs/TRACE.md``.

    ``conversation`` (default ``None``) attaches an optional
    ``hotato.conversation.v1`` manifest (build one with
    ``hotato.conversation.build_manifest``) as provenance CONTEXT. It renders a
    "Conversation artifact (provenance)" section stating whether the call was a
    REAL recording or a SIMULATED one (invariant 5: the two are never conflated;
    a simulated origin shows its model/scenario/seed) plus the artifact digests
    the manifest binds by sha256, and is folded into the returned envelope as an
    additive top-level ``conversation`` key. This function never verifies or
    re-hashes a child and never touches any timing/verdict field; ``None`` (the
    default) leaves the HTML byte-identical to a report built before this
    parameter existed.

    ``reliability`` (default ``None``) attaches REAL repetition data -- a
    :func:`hotato.simulate.run_matrix` summary, a bare
    :func:`hotato.simulate.reliability` dict, or a
    ``{"aggregate": <reliability dict>, "origin": ...}`` wrapper -- so the
    scorecard's Reliability dimension renders its OWN pass@1 / pass@k / pass^k
    (with n and the Wilson CI), any per-variation cells, the run count, and a
    SIMULATOR_INVALID bucket (excluded from n). pass^k is never blended into any
    other dimension and there is no ``overall_score``. When the data came from
    simulated runs the section is labeled origin=simulated -- a simulator's
    replay reliability is never presented as production reliability. ``None``
    (the default, and any payload with genuinely no repetition data) leaves the
    Reliability dimension showing the honest empty-state and is byte-identical to
    a report built without this parameter. See ``docs/REPORTS.md``.
    """
    env, models, cfg = _score_and_model(transcript=transcript, trace=trace,
                                        **kwargs)
    mode = _resolve_audio_mode(embed_audio, audio_mode)
    # Provenance CONTEXT only: normalized + folded into the envelope additively,
    # never verified/re-hashed here and never fed into any measurement.
    cv = _normalize_conversation(conversation) if conversation is not None else None
    if cv is not None:
        env["conversation"] = cv
    rel = _normalize_reliability(reliability)
    if rel is not None and assertions is None:
        # Data supplied = data rendered, never silently dropped: real reliability
        # numbers with no assertions envelope still render their Reliability
        # dimension, over an honest empty envelope (0 assertions, stated in the
        # note). assertions=None WITH reliability=None stays byte-identical.
        assertions = _EMPTY_ASSERT_ENVELOPE
    page = _render_page(env, models, cfg, base_env=base, base_label=base_label,
                        audio_mode=mode, assertions=assertions,
                        trace=env.get("trace_context"), conversation=cv,
                        rubric=rubric, reliability=rel)
    return page, env


def build_report_md(*, base: Optional[dict] = None,
                    base_label: Optional[str] = None,
                    transcript: Optional[TranscriptLike] = None,
                    assertions: Optional[dict] = None,
                    trace: Optional[TraceLike] = None,
                    conversation: Optional[ConversationLike] = None,
                    rubric: Optional[dict] = None,
                    reliability: Optional[dict] = None,
                    **kwargs):
    """Score the input and return ``(markdown_str, envelope)``.

    Mirrors the HTML report's content with tables instead of SVG: summary,
    per-event measurements, failures with fixes, analytics aggregates, the
    optional base comparison, thresholds, and the honest limits.

    ``transcript`` is the same optional ASR-context parameter as
    ``build_report_html`` (see there): a collapsed "Transcripts (context, not a
    score)" section per event that carries one, additive to the envelope,
    never touching any timing/verdict field. ``None`` (the default) leaves the
    Markdown byte-identical to before this parameter existed.

    ``assertions`` is the same optional ``assert.v1`` envelope parameter as
    ``build_report_html`` (see there): an "Assertions" section with the same
    two shelves, as Markdown tables. ``None`` (the default) leaves the
    Markdown byte-identical to before this parameter existed.

    ``trace`` is the same optional ``hotato.voice_trace.v1`` context parameter
    as ``build_report_html`` (see there): a "Trace (context, not a score)"
    section as a Markdown span table, folded into the envelope as an additive
    top-level ``trace_context`` key, never touching any timing/verdict field,
    with the same ``[redacted]`` redaction wall. ``None`` (the default) leaves
    the Markdown byte-identical to before this parameter existed.

    ``conversation`` is the same optional ``hotato.conversation.v1`` manifest
    parameter as ``build_report_html`` (see there): a "Conversation artifact
    (provenance)" section stating real|simulated origin and the bound artifact
    digests, folded into the envelope as an additive top-level ``conversation``
    key, never touching any timing/verdict field. ``None`` (the default) leaves
    the Markdown byte-identical to before this parameter existed.

    ``reliability`` is the same optional REAL-repetition parameter as
    ``build_report_html`` (see there): the scorecard's Reliability dimension
    renders its OWN pass@1 / pass@k / pass^k (with n + Wilson CI), per-variation
    cells, and the SIMULATOR_INVALID bucket, as Markdown tables. ``None`` (the
    default, and any payload with no repetition data) leaves the Reliability
    dimension showing the honest empty-state, byte-identical to before this
    parameter existed.
    """
    env, models, cfg = _score_and_model(transcript=transcript, trace=trace,
                                        **kwargs)
    cv = _normalize_conversation(conversation) if conversation is not None else None
    if cv is not None:
        env["conversation"] = cv
    rel = _normalize_reliability(reliability)
    if rel is not None and assertions is None:
        # Data supplied = data rendered, never silently dropped: real reliability
        # numbers with no assertions envelope still render their Reliability
        # dimension, over an honest empty envelope (0 assertions, stated in the
        # note). assertions=None WITH reliability=None stays byte-identical.
        assertions = _EMPTY_ASSERT_ENVELOPE
    return (
        _render_md(env, models, cfg, base_env=base, base_label=base_label,
                  assertions=assertions, trace=env.get("trace_context"),
                  conversation=cv, rubric=rubric, reliability=rel),
        env,
    )


def write_report(path: str, fmt: str = "html", embed_audio: bool = False,
                 audio_mode: Optional[str] = None, **kwargs):
    """Build the report in ``fmt`` ('html' or 'md') and write it to ``path``.
    Returns the envelope. For PDF, print the HTML from any browser: the page
    ships print CSS, so no separate renderer is needed.

    ``audio_mode`` (``none`` / ``self_contained`` / ``audio_reference``) picks
    the audio treatment for the HTML report; it wins over ``embed_audio`` and is
    HTML-only, exactly like ``embed_audio``. ``transcript`` (optional ASR
    context), ``assertions`` (optional pre-built ``assert.v1`` envelope), and
    ``trace`` (optional ``hotato.voice_trace.v1`` context; see
    ``build_report_html``) pass through via ``**kwargs`` to either renderer."""
    embeds_audio = _resolve_audio_mode(embed_audio, audio_mode) != AUDIO_NONE
    if fmt == "html":
        text, env = build_report_html(embed_audio=embed_audio,
                                      audio_mode=audio_mode, **kwargs)
    elif fmt == "md":
        if embeds_audio:
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
