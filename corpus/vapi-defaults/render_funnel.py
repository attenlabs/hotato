#!/usr/bin/env python3
"""Render the shareable both-directions-fail visual for this corpus.

Builds ONE self-contained SVG that stacks two REAL scored event timelines
from the committed vapi-defaults battery, using the repo's own report
timeline renderer (``hotato.report._svg_timeline``) so the drawing is the
identical geometry a full HTML report would show, not a hand-drawn mockup:

  * vapi-default-10-quiet-interrupt: a genuine interruption the agent MISSED
    (should_yield, did_yield=false).
  * vapi-default-04-backchannel-halt: a soft backchannel the agent
    FALSE-STOPPED for (should_not_yield, did_yield=true).

Both come from the same battery, at the same default configuration, in the
same run: no single sensitivity threshold moves one right without moving the
other wrong. That is the funnel this directory documents (see RESULTS.md).

Usage:
    PYTHONPATH=src python3 corpus/vapi-defaults/render_funnel.py \
        --out corpus/vapi-defaults/both-directions-fail.svg

Does not touch src/hotato/_engine or any scoring/golden output; it only
calls the already-shipped, already-tested report renderer on the already-
committed battery audio and scenarios.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT / "src"))

from hotato.report import _score_and_model, _svg_timeline, _C  # noqa: E402

CORPUS_DIR = Path(__file__).resolve().parent
MISSED_ID = "vapi-default-10-quiet-interrupt"
FALSE_STOP_ID = "vapi-default-04-backchannel-halt"

_PANEL_W = 746
_PANEL_H = 120
_HEADER_H = 34
_GAP = 18
_TOP_H = 78
_BOTTOM_H = 26
_GUTTER_X = 24


def _panel(title: str, subtitle: str, verdict: str, svg_inner: str, y: float) -> str:
    """One titled panel: header text + the embedded timeline SVG, at offset y."""
    verdict_color = _C["ember"] if verdict == "FAIL" else _C["green"]
    return f"""
<g transform="translate(0,{y:.1f})">
  <rect x="0" y="0" width="{_PANEL_W + 2 * _GUTTER_X}" height="{_HEADER_H + _PANEL_H}"
        rx="10" fill="{_C['card']}" stroke="{_C['line']}" stroke-width="1" />
  <text x="{_GUTTER_X}" y="22" fill="{_C['cream']}" font-size="15" font-weight="600"
        font-family="ui-monospace, SFMono-Regular, Menlo, monospace">{title}</text>
  <text x="{_PANEL_W + _GUTTER_X}" y="22" fill="{verdict_color}" font-size="14"
        font-weight="700" text-anchor="end"
        font-family="ui-monospace, SFMono-Regular, Menlo, monospace">[{verdict}]</text>
  <text x="{_GUTTER_X}" y="{_HEADER_H + 12}" fill="{_C['muted']}" font-size="11.5"
        font-family="ui-monospace, SFMono-Regular, Menlo, monospace">{subtitle}</text>
  <g transform="translate({_GUTTER_X},{_HEADER_H + 14})">{svg_inner}</g>
</g>"""


def build_svg(models_by_id: dict) -> str:
    missed = models_by_id[MISSED_ID]
    false_stop = models_by_id[FALSE_STOP_ID]

    missed_ev = missed["event"]
    false_stop_ev = false_stop["event"]

    caption_lines = [
        "Turning sensitivity UP catches the miss above but worsens the",
        "backchannel below; turning it DOWN reverses that. No single",
        "threshold wins both rows: hotato diagnose returns",
        "do_not_tune_single_threshold. Provider default settings, one",
        "assistant, one recording date -- corpus/vapi-defaults/.",
    ]

    panel_h = _HEADER_H + _PANEL_H
    total_w = _PANEL_W + 2 * _GUTTER_X
    caption_h = 22 * len(caption_lines) + 12
    total_h = 40 + panel_h * 2 + _GAP + caption_h

    parts = []
    parts.append(
        f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {total_w} {total_h}" '
        f'width="{total_w}" height="{total_h}" '
        f'font-family="ui-monospace, SFMono-Regular, Menlo, monospace" '
        f'role="img" aria-label="One battery, one default configuration, two opposite '
        f'failures: a missed real interruption and a false stop on a backchannel">'
    )
    parts.append(f'<rect x="0" y="0" width="{total_w}" height="{total_h}" fill="{_C["bg"]}" />')

    parts.append(
        f'<text x="{_GUTTER_X}" y="26" fill="{_C["cream"]}" font-size="17" font-weight="700">'
        f"Same battery, same default config, one run: fails BOTH directions</text>"
    )

    parts.append(
        _panel(
            "Missed a real interruption",
            f"{missed_ev['event_id']} -- talk_over="
            f"{missed_ev['verdict']['talk_over_sec']:.2f}s, no yield in the search window",
            "FAIL",
            _svg_timeline(missed),
            40,
        )
    )
    parts.append(
        _panel(
            "False-stopped on a backchannel",
            f"{false_stop_ev['event_id']} -- soft ack, not a bid for the floor; "
            f"yielded in {false_stop_ev['verdict']['seconds_to_yield']:.2f}s anyway",
            "FAIL",
            _svg_timeline(false_stop),
            40 + panel_h + _GAP,
        )
    )

    caption_y0 = 40 + panel_h * 2 + _GAP + 20
    for i, line in enumerate(caption_lines):
        parts.append(
            f'<text x="{_GUTTER_X}" y="{caption_y0 + i * 22}" fill="{_C["muted"]}" '
            f'font-size="13">{line}</text>'
        )

    parts.append("</svg>")
    return "".join(parts)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--out", default=str(CORPUS_DIR / "both-directions-fail.svg"))
    args = ap.parse_args()

    env, models, cfg = _score_and_model(
        stack="vapi",
        suite="barge-in",
        scenarios_dir=str(CORPUS_DIR / "scenarios"),
        audio_dir=str(CORPUS_DIR / "audio"),
        suffix=".example.wav",
    )
    models_by_id = {m["event"]["event_id"]: m for m in models}
    missing = [i for i in (MISSED_ID, FALSE_STOP_ID) if i not in models_by_id]
    if missing:
        print(f"render_funnel: missing expected event ids: {missing}", file=sys.stderr)
        return 1

    svg = build_svg(models_by_id)
    out_path = Path(args.out)
    out_path.write_text(svg, encoding="utf-8")
    print(f"wrote {out_path} ({len(svg)} bytes)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
