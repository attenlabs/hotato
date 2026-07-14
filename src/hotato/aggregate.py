"""Team mode: aggregate many run envelopes into one honest trend view.

Input: a directory of hotato envelope JSONs, the exact files a run already
produces (``hotato run --format json > runs/001.json``). Output: an aggregate
envelope (machine JSON) plus an optional self-contained HTML page with a
pass-rate trend line (inline SVG, zero external requests).

Every number is computed from the envelopes' real measurements:

* runs, total events
* mean / median / p90 / p95 of talk-over, time-to-yield, and response-gap
  (dead air before the agent speaks), pooled across all events (definitions
  in ``hotato._stats``; time-to-yield and response-gap only over measured
  values, with the n stated)
* pass rate per run over time, ordered by file mtime or by filename (use a
  numeric filename prefix as an explicit index)
* the most common failure class across all runs
* a regression trend line (pass rate per run)
* an optional latency SLA gate: bound the pooled p95 response-gap with
  ``--max-response-gap SEC``; the gate fails (exit 1) exactly when p95
  exceeds the bound, and is not configured (never a failure) otherwise

Fewer than 2 runs is stated plainly and exits 0, never padded into a trend.
No accuracy percentage anywhere; pass rates are shown as counts and 0..1
fractions of real verdicts.
"""

from __future__ import annotations

import json
import os
from typing import Optional

from ._stats import dist_summary, latency_sla
from .errors import open_regular as _open_regular
from .report import _C, _esc

__all__ = ["load_run_dir", "aggregate_runs", "build_team_section_html",
           "build_team_page_html"]


# --- loading ----------------------------------------------------------------

def is_envelope(obj) -> bool:
    """True when ``obj`` looks like a hotato run envelope (not a frame dump)."""
    return (
        isinstance(obj, dict)
        and obj.get("tool") == "hotato"
        and obj.get("kind") != "frame-dump"
        and isinstance(obj.get("events"), list)
        and isinstance(obj.get("summary"), dict)
    )


def load_run_dir(dirpath: str, order: str = "name") -> dict:
    """Load every envelope JSON in ``dirpath``, ordered for the trend.

    ``order`` is 'name' (DEFAULT: lexicographic filename, so a numeric prefix
    acts as an explicit, content-derived index) or 'mtime' (file modification
    time, oldest first; name breaks ties). ``name`` is the default because the
    aggregate must be a pure function of the envelope FILES: mtime is filesystem
    metadata that a git checkout, tar/zip extract, rsync without -t, or docker
    COPY all rewrite, so ordering by it makes the same bytes produce a different
    trend. ``mtime`` stays available but is opt-in and filesystem-dependent.
    Non-envelope JSONs (frame dumps, unrelated files) are skipped and reported,
    never guessed at.
    """
    if order not in ("mtime", "name"):
        raise ValueError(f"unknown order {order!r}; use 'mtime' or 'name'")
    if not os.path.isdir(dirpath):
        raise ValueError(f"{dirpath!r} is not a directory of envelope JSONs")

    runs, skipped = [], []
    for name in sorted(os.listdir(dirpath)):
        if not name.endswith(".json"):
            continue
        path = os.path.join(dirpath, name)
        try:
            with _open_regular(path, "r", encoding="utf-8") as fh:
                obj = json.load(fh)
        except (OSError, ValueError) as exc:
            skipped.append({"file": name, "why": f"unreadable JSON ({exc})"})
            continue
        if not is_envelope(obj):
            skipped.append({"file": name, "why": "not a hotato run envelope"})
            continue
        runs.append({
            "file": name,
            "path": path,
            "mtime": os.path.getmtime(path),
            "env": obj,
        })
    if order == "mtime":
        runs.sort(key=lambda r: (r["mtime"], r["file"]))
    else:
        runs.sort(key=lambda r: r["file"])
    return {"runs": runs, "skipped": skipped, "order": order}


# --- aggregation --------------------------------------------------------------

def aggregate_runs(runs: list, order: str = "name",
                   skipped: Optional[list] = None,
                   max_response_gap_sec: Optional[float] = None) -> dict:
    """Aggregate loaded runs (see ``load_run_dir``) into a team envelope.

    Requires at least 2 runs; the caller states the shortfall plainly instead
    (see the CLI). Every field is computed from the envelopes' verdicts and
    measurements. ``max_response_gap_sec`` optionally bounds the pooled p95
    response-gap (the latency SLA gate, see ``hotato._stats.latency_sla``);
    left ``None`` the gate is not configured and never fails the aggregate.
    """
    if len(runs) < 2:
        raise ValueError(
            f"team aggregation needs at least 2 run envelopes; got {len(runs)}"
        )

    tov, tty, rg = [], [], []
    over_time = []
    failure_classes = {}
    events_total = 0
    for r in runs:
        env = r["env"]
        s = env.get("summary", {})
        events = env.get("events", [])
        events_total += len(events)
        for e in events:
            v = e.get("verdict", {})
            if v.get("talk_over_sec") is not None:
                tov.append(v["talk_over_sec"])
            if v.get("seconds_to_yield") is not None:
                tty.append(v["seconds_to_yield"])
            sig = e.get("signals") or {}
            lat = sig.get("latency") or {}
            if lat.get("response_gap_sec") is not None:
                rg.append(lat["response_gap_sec"])
            fx = e.get("fix")
            if not v.get("passed") and fx:
                fc = fx.get("fix_class") or "unclassified"
                failure_classes[fc] = failure_classes.get(fc, 0) + 1
        # The pass-rate denominator is the SCORABLE population, never the raw
        # event total: a not-scorable event (an input problem) is neither a pass
        # nor a fail, so counting it in the denominator would silently deflate the
        # rate (and n_ev - n_pass would mislabel it as a failure). This matches
        # core._envelope, compare.py and verify.py, which all exclude not-scorable
        # events from both sides of any ratio.
        scor_events = [e for e in events if e.get("scorable") is not False]
        n_pass = s.get("passed", sum(
            1 for e in scor_events if e.get("verdict", {}).get("passed")))
        n_fail = s.get("failed", sum(
            1 for e in scor_events if not e.get("verdict", {}).get("passed")))
        n_scorable = n_pass + n_fail
        # Deliberately no ``mtime`` field here: the aggregate output must be a
        # pure function of the envelope FILES, and mtime is filesystem metadata
        # that a checkout / extract / rsync rewrites. Ordering already uses it
        # only as an opt-in tie-break inside load_run_dir; it never leaks into the
        # emitted, comparable result.
        over_time.append({
            "file": r["file"],
            "events": n_scorable,
            "passed": n_pass,
            "failed": n_fail,
            "pass_rate": round(n_pass / n_scorable, 4) if n_scorable else None,
        })

    rates = [p["pass_rate"] for p in over_time if p["pass_rate"] is not None]
    first_rate = rates[0] if rates else None
    latest_rate = rates[-1] if rates else None
    if first_rate is None or latest_rate is None:
        direction = "flat"
    elif latest_rate > first_rate:
        direction = "up"
    elif latest_rate < first_rate:
        direction = "down"
    else:
        direction = "flat"

    most_common = None
    if failure_classes:
        fc = sorted(failure_classes.items(), key=lambda kv: (-kv[1], kv[0]))[0]
        most_common = {"fix_class": fc[0], "count": fc[1],
                       "of_failures": sum(failure_classes.values())}

    response_gap_sec = dist_summary(rg)
    sla = latency_sla(response_gap_sec, max_response_gap_sec)

    return {
        "tool": "hotato",
        "kind": "team-aggregate",
        "schema_version": "1",
        "offline": True,
        "runs": len(runs),
        "ordered_by": order,
        "events_total": events_total,
        "talk_over_sec": dist_summary(tov),
        "seconds_to_yield": dist_summary(tty),
        "response_gap_sec": response_gap_sec,
        "latency_sla": sla,
        "pass_rate": {
            "latest": latest_rate,
            "first": first_rate,
            "mean": round(sum(rates) / len(rates), 4) if rates else None,
            "direction": direction,
        },
        "pass_rate_over_time": over_time,
        "failure_classes": failure_classes,
        "most_common_failure_class": most_common,
        "skipped": skipped or [],
        "exit_code": 1 if sla["passed"] is False else 0,
    }


# --- HTML (self-contained, inline SVG, same theme as the report) -------------

_TEAM_W = 746
_TEAM_H = 170


def _svg_trend(over_time: list) -> str:
    """Pass-rate trend line: one point per run, y = passed/events (0..1)."""
    pts = [(i, p) for i, p in enumerate(over_time) if p["pass_rate"] is not None]
    gut, rpad, top = 56, 24, 14
    ay = _TEAM_H - 34
    pw = _TEAM_W - gut - rpad
    n = max(1, len(over_time) - 1)

    def X(i: int) -> float:
        return gut + (i / n) * pw

    def Y(rate: float) -> float:
        return ay - rate * (ay - top)

    aria = (f'Pass-rate trend line: one point per run, {len(pts)} plotted '
            f'runs of {len(over_time)} total, y axis pass rate 0 to 1')
    p = [f'<svg class="trend-svg" viewBox="0 0 {_TEAM_W} {_TEAM_H}" '
         f'width="{_TEAM_W}" height="{_TEAM_H}" role="img" '
         f'aria-label="{_esc(aria)}" '
         f'font-family="ui-monospace, SFMono-Regular, Menlo, monospace">']
    # y axis: 0, 0.5, 1.0 pass-rate gridlines
    for rate in (0.0, 0.5, 1.0):
        y = Y(rate)
        p.append(f'<line x1="{gut}" y1="{y:.1f}" x2="{gut + pw}" y2="{y:.1f}" '
                 f'stroke="{_C["grid"]}" stroke-width="1" />')
        p.append(f'<text x="{gut - 8}" y="{y + 3:.1f}" fill="{_C["muted"]}" '
                 f'font-size="10" text-anchor="end">{rate:.1f}</text>')
    if len(pts) >= 2:
        path = " ".join(f"{X(i):.1f},{Y(p_['pass_rate']):.1f}" for i, p_ in pts)
        p.append(f'<polyline points="{path}" fill="none" '
                 f'stroke="{_C["ember"]}" stroke-width="2" />')
    for i, pt in pts:
        color = _C["green"] if pt["failed"] == 0 else _C["red"]
        p.append(f'<circle cx="{X(i):.1f}" cy="{Y(pt["pass_rate"]):.1f}" r="4.5" '
                 f'fill="{color}"><title>{_esc(pt["file"])}: {pt["passed"]} of '
                 f'{pt["events"]} pass</title></circle>')
    for i, pt in enumerate(over_time):
        p.append(f'<text x="{X(i):.1f}" y="{ay + 16}" fill="{_C["muted"]}" '
                 f'font-size="9" text-anchor="middle">{i + 1}</text>')
    p.append(f'<text x="{gut + pw / 2:.1f}" y="{ay + 30}" fill="{_C["muted"]}" '
             f'font-size="10" text-anchor="middle">runs in order (pass rate, '
             f'0 to 1)</text>')
    p.append("</svg>")
    return "".join(p)


def _tile(label: str, value: str) -> str:
    return (f'<div class="th"><span class="k">{_esc(label)}</span>'
            f'<span class="v mono">{_esc(value)}</span></div>')


def _dist_tiles(name: str, d: Optional[dict]) -> str:
    if not d:
        return _tile(name, "no measurements")
    return "".join([
        _tile(f"{name} mean", f'{d["mean"]:.2f}s'),
        _tile(f"{name} median (p50)", f'{d["median"]:.2f}s'),
        _tile(f"{name} p90", f'{d["p90"]:.2f}s'),
        _tile(f"{name} p95", f'{d["p95"]:.2f}s'),
        _tile(f"{name} n", str(d["n"])),
    ])


def _latency_sla_tile(sla: dict) -> str:
    if sla["bound_sec"] is None:
        return _tile("latency SLA (p95 response gap)", "not configured")
    observed = (f'{sla["observed_p95_sec"]:.2f}s'
                if sla["observed_p95_sec"] is not None else "no measurements")
    verdict = "pass" if sla["passed"] else "fail"
    return _tile("latency SLA (p95 response gap)",
                 f'{observed} vs bound {sla["bound_sec"]:.2f}s ({verdict})')


def build_team_section_html(agg: dict) -> str:
    """The team aggregate as an HTML section (embeddable, self-contained)."""
    pr = agg["pass_rate"]
    latest = agg["pass_rate_over_time"][-1]
    mc = agg["most_common_failure_class"]
    tiles = [
        _tile("runs", str(agg["runs"])),
        _tile("events total", str(agg["events_total"])),
        _tile("pass rate latest",
              f'{latest["passed"]} of {latest["events"]} ({pr["latest"]:.2f})'
              if pr["latest"] is not None else "n/a"),
        _tile("pass rate mean", f'{pr["mean"]:.2f}' if pr["mean"] is not None else "n/a"),
        _tile("trend", f'{pr["first"]:.2f} to {pr["latest"]:.2f} ({pr["direction"]})'
              if pr["first"] is not None else "n/a"),
    ]
    tiles.append(_dist_tiles("talk-over", agg["talk_over_sec"]))
    tiles.append(_dist_tiles("time to yield", agg["seconds_to_yield"]))
    tiles.append(_dist_tiles("response gap", agg["response_gap_sec"]))
    tiles.append(_latency_sla_tile(agg["latency_sla"]))
    if mc:
        tiles.append(_tile("most common failure class",
                           f'{mc["fix_class"]} ({mc["count"]} of '
                           f'{mc["of_failures"]} failures)'))
    else:
        tiles.append(_tile("most common failure class", "no failures"))

    rows = "".join(
        f'<tr><td class="mono">{i + 1}</td><td class="mono">{_esc(p["file"])}</td>'
        f'<td class="mono">{p["passed"]} of {p["events"]}</td>'
        f'<td class="mono">{p["pass_rate"]:.2f}</td></tr>'
        for i, p in enumerate(agg["pass_rate_over_time"])
    )
    return (
        '<section class="card">'
        '<div class="ctitle">Team aggregate</div>'
        f'<div class="tnote">{agg["runs"]} runs ordered by '
        f'{_esc(agg["ordered_by"])}. Every number is pooled from the runs\' '
        'real measurements. Definitions: mean, median, p90 by linear '
        'interpolation.</div>'
        f'<div class="thgrid">{"".join(tiles)}</div>'
        f'<div class="tl">{_svg_trend(agg["pass_rate_over_time"])}</div>'
        '<table class="vadtab"><thead><tr><th>run</th><th>file</th>'
        '<th>passed</th><th>pass rate</th></tr></thead>'
        f'<tbody>{rows}</tbody></table>'
        '</section>'
    )


_TEAM_CSS = """
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
 margin-top:2px}
.h1{font-size:26px;font-weight:700;letter-spacing:-0.01em;margin:0}
.tagline{color:%(muted)s;margin:2px 0 8px}
.card{background:%(card)s;border:1px solid %(line)s;border-radius:16px;
 padding:18px 20px;margin-bottom:18px}
.ctitle{font-size:16.5px;font-weight:650}
.tnote{color:%(muted)s;font-size:12.5px;margin:2px 0 12px}
.thgrid{display:flex;flex-wrap:wrap;gap:8px 10px;margin-bottom:14px}
.th{display:flex;flex-direction:column;gap:2px;background:%(card2)s;
 border:1px solid %(line)s;border-radius:9px;padding:7px 11px;min-width:150px}
.th .k{font-size:11px;color:%(muted)s}
.th .v{font-size:14px;color:%(mono)s}
.tl{overflow-x:auto;margin:6px 0 12px;padding-bottom:4px}
.tl svg{display:block}
.vadtab{border-collapse:collapse;width:auto;font-size:13px}
.vadtab th,.vadtab td{text-align:left;padding:5px 18px 5px 0;
 border-bottom:1px solid %(line)s}
.vadtab th{color:%(muted)s;font-weight:600;font-size:12px}
.foot{margin-top:26px;border-top:1px solid %(line)s;padding-top:18px;
 color:%(muted)s;font-size:13px}
@media print{body{background:#ffffff;color:#1b1714}
 .card{background:#ffffff;border-color:#d8d2c6}}
""" % _C


def build_team_page_html(agg: dict) -> str:
    """A full standalone team page: header + aggregate section + honest footer."""
    head = (
        '<header class="top"><div class="logo"></div><div>'
        '<h1 class="h1">hotato team</h1>'
        '<div class="tagline">Turn-taking trend across runs. Offline, '
        'deterministic, every number a real measurement.</div>'
        '</div></header>'
    )
    foot = (
        '<footer class="foot">Reproducible timing measurements with an exposed '
        'method and an explicit ceiling. No accuracy score. Pass rates are '
        'counts of real verdicts, shown as 0 to 1 fractions.</footer>'
    )
    body = (f'<div class="wrap">{head}<main>{build_team_section_html(agg)}'
            f'</main>{foot}</div>')
    return (
        "<!doctype html>\n<html lang=\"en\"><head>"
        "<meta charset=\"utf-8\">"
        "<meta name=\"viewport\" content=\"width=device-width, initial-scale=1\">"
        "<title>hotato team</title>"
        f"<style>{_TEAM_CSS}</style></head><body>{body}</body></html>\n"
    )
