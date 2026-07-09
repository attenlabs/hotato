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

_TALK_OVER = ("overlap_while_agent_talking", "agent_start_during_caller")
_FALSE_STOP = ("agent_stop_no_caller",)


def _write(name: str, svg: str) -> None:
    path = os.path.join(_OUT, name)
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(svg)
    print(f"wrote {os.path.relpath(path, _ROOT)} ({len(svg)} bytes)")


def _rank_of_first(candidates, kinds) -> int:
    for i, c in enumerate(candidates, 1):
        if c.get("kind") in kinds:
            return i
    raise SystemExit(f"no candidate of kind {kinds} in the demo sweep")


def main() -> int:
    os.makedirs(_OUT, exist_ok=True)

    # C -- the threshold-funnel hero card, from the demo battery's fix plan.
    root = resources.files("hotato").joinpath("data", "demo", "failing")
    env = run_suite(scenarios_dir=str(root.joinpath("scenarios")),
                    audio_dir=str(root.joinpath("audio")))
    plan = build_plan(diagnosis=diagnose_envelope(env))
    _write("no-single-threshold-card.svg", _card.render_plan_card(plan))

    # A / B -- the candidate cards, from a sweep of the two bundled demo calls.
    audio_dir = str(resources.files("hotato").joinpath(
        "data", "demo", "failing", "audio"))
    aggregate, _ = _analyze.analyze_folder(audio_dir)
    cands = aggregate["candidates"]
    with tempfile.TemporaryDirectory() as tmp:
        sweep_json = os.path.join(tmp, "hotato-sweep.json")
        with open(sweep_json, "w", encoding="utf-8") as fh:
            json.dump(aggregate, fh)
        n_tov = _rank_of_first(cands, _TALK_OVER)
        n_fs = _rank_of_first(cands, _FALSE_STOP)
        _write("talk-over-card.svg", _card.make_card(f"{sweep_json}#{n_tov}"))
        _write("false-stop-card.svg", _card.make_card(f"{sweep_json}#{n_fs}"))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
