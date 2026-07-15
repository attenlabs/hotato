#!/usr/bin/env python3
"""Regenerate the committed card assets under docs/assets/cards/.

Deterministic and fully offline. Renders:

- ``no-single-threshold-card.svg`` -- the threshold-funnel hero card, from the
  fix plan the bundled failing demo battery produces (``hotato demo`` scored,
  then ``hotato plan``).
- ``talk-over-card.svg`` and ``false-stop-card.svg`` -- the two candidate cards,
  from a sweep of the two bundled real demo calls (``hotato sweep --demo``): the
  top talk-over (overlap) candidate and the top false-stop candidate.

Each SVG is a pure function of the bundled inputs, so re-running this script on
an unchanged tree reproduces the committed bytes exactly. Run:

    PYTHONPATH=src python3 scripts/render_card_assets.py
"""

from __future__ import annotations

import json
import os
import sys
import tempfile

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_HERE)
sys.path.insert(0, os.path.join(_ROOT, "src"))

from importlib import resources  # noqa: E402

from hotato import analyze as _analyze  # noqa: E402
from hotato import card as _card  # noqa: E402
from hotato.core import run_suite  # noqa: E402
from hotato.diagnose import diagnose_envelope  # noqa: E402
from hotato.fixplan import build_plan  # noqa: E402

_OUT = os.path.join(_ROOT, "docs", "assets", "cards")
_FONTS_JSON = os.path.join(_OUT, "_fonts.json")

_TALK_OVER = ("overlap_while_agent_talking", "agent_start_during_caller")
_FALSE_STOP = ("agent_stop_no_caller",)

# Brand faces, subset to Basic Latin and embedded so the illustrative cards
# render Bricolage / Hanken / Spline Sans Mono wherever they are dropped in as
# an image. The subset woff2 lives in docs/assets/cards/_fonts.json, which is
# pruned from the sdist (MANIFEST ``prune docs/assets``), so it adds no weight
# to the shipped package; the runtime ``hotato card`` never embeds fonts.
_FONT_FACES = (
    ("Bricolage", "200 800"),
    ("Hanken", "100 900"),
    ("SplineMono", "300 700"),
)


def card_font_css() -> str:
    """The ``@font-face`` block embedding the subset brand faces as woff2 data
    URIs. Deterministic: a pure function of the committed _fonts.json, so a card
    built with it reproduces the same bytes forever."""
    with open(_FONTS_JSON, encoding="utf-8") as fh:
        blobs = json.load(fh)
    rules = []
    for family, weight in _FONT_FACES:
        b64 = blobs[family]
        rules.append(
            f'@font-face{{font-family:"{family}";'
            f'src:url("data:font/woff2;base64,{b64}") format("woff2");'
            f'font-weight:{weight};font-style:normal;font-display:swap}}')
    return "".join(rules)


def _rank_of_first(candidates, kinds) -> int:
    for i, c in enumerate(candidates, 1):
        if c.get("kind") in kinds:
            return i
    raise SystemExit(f"no candidate of kind {kinds} in the demo sweep")


def build_cards() -> dict:
    """Render the three illustrative cards (funnel, talk-over, false-stop) from
    the bundled demo battery, with the brand faces embedded. Returns
    ``{filename: svg}``. The guard test in tests/test_card_cli.py calls this so
    the committed assets stay in lockstep with the generator."""
    font_css = card_font_css()

    root = resources.files("hotato").joinpath("data", "demo", "failing")
    env = run_suite(scenarios_dir=str(root.joinpath("scenarios")),
                    audio_dir=str(root.joinpath("audio")))
    plan = build_plan(diagnosis=diagnose_envelope(env))

    audio_dir = str(root.joinpath("audio"))
    aggregate, _ = _analyze.analyze_folder(audio_dir)
    cands = aggregate["candidates"]
    with tempfile.TemporaryDirectory() as tmp:
        sweep_json = os.path.join(tmp, "hotato-sweep.json")
        with open(sweep_json, "w", encoding="utf-8") as fh:
            json.dump(aggregate, fh)
        n_tov = _rank_of_first(cands, _TALK_OVER)
        n_fs = _rank_of_first(cands, _FALSE_STOP)
        return {
            "no-single-threshold-card.svg":
                _card.render_plan_card(plan, font_css=font_css),
            "talk-over-card.svg":
                _card.make_card(f"{sweep_json}#{n_tov}", font_css=font_css),
            "false-stop-card.svg":
                _card.make_card(f"{sweep_json}#{n_fs}", font_css=font_css),
        }


def main() -> int:
    os.makedirs(_OUT, exist_ok=True)
    for name, svg in build_cards().items():
        path = os.path.join(_OUT, name)
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(svg)
        print(f"wrote {os.path.relpath(path, _ROOT)} ({len(svg)} bytes)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
