#!/usr/bin/env python3
"""Regenerate the committed card assets under docs/assets/cards/.

Deterministic and fully offline. Renders:

- ``no-single-threshold-card.svg`` -- the threshold-funnel hero card, from the
  fix plan the bundled failing demo battery produces (``hotato demo`` scored,
  then ``hotato plan``).
- ``talk-over-card.svg`` -- the top talk-over (overlap) candidate from a sweep
  of the two bundled real demo calls (``hotato sweep --demo``).
- ``false-stop-card.svg`` -- the top false-stop candidate from a sweep of a
  synthetic two-channel recording built here: an agent run, a silence longer
  than the reportable-gap floor, then a second agent run, with the caller
  silent throughout. The bundled demo calls contain no reportable false stop,
  so the illustrative card is rendered from a recording that genuinely does.
- ``say-do-card.svg`` -- the say-do failure card, from the test-run result the
  bundled scripted say-do conversation produces (the same check act two of
  ``hotato start --demo`` runs and writes as ``saydo/test-run.json``).

Each SVG is a pure function of the bundled inputs, so re-running this script on
an unchanged tree reproduces the committed bytes exactly. Run:

    PYTHONPATH=src python3 scripts/render_card_assets.py
"""

from __future__ import annotations

import json
import math
import os
import struct
import sys
import tempfile
import wave

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


def _write_false_stop_wav(path, sr=16000):
    """A deterministic two-channel WAV that genuinely contains one bounded
    ``agent_stop_no_caller`` moment: the agent talks 0.5s-3.0s, stops for
    2.5s (over the 2.0s reportable-gap floor), then talks again 5.5s-7.0s,
    while the caller channel stays silent. Caller on channel 0, agent on
    channel 1; a pure sine inside each active span, exact digital silence
    outside it."""
    caller_segments = ()
    agent_segments = ((0.5, 3.0), (5.5, 7.0))
    duration_sec = 7.5
    n = int(duration_sec * sr)

    def _on(segments, t):
        return any(start <= t < end for start, end in segments)

    frames = bytearray()
    for i in range(n):
        t = i / sr
        c = (int(0.35 * 32767 * math.sin(2 * math.pi * 220.0 * i / sr))
             if _on(caller_segments, t) else 0)
        a = (int(0.35 * 32767 * math.sin(2 * math.pi * 330.0 * i / sr))
             if _on(agent_segments, t) else 0)
        frames += struct.pack("<hh", c, a)
    with wave.open(str(path), "wb") as w:
        w.setnchannels(2)
        w.setsampwidth(2)
        w.setframerate(sr)
        w.writeframes(bytes(frames))
    return str(path)


def _rank_of_first(candidates, kinds) -> int:
    for i, c in enumerate(candidates, 1):
        if c.get("kind") in kinds:
            return i
    raise SystemExit(f"no candidate of kind {kinds} in the demo sweep")


def build_cards() -> dict:
    """Render the four illustrative cards (funnel, talk-over, false-stop,
    say-do) from the bundled demo data plus the synthetic false-stop
    recording built above, with the brand faces embedded. Returns
    ``{filename: svg}``. The guard test in tests/test_card_cli.py calls this so
    the committed assets stay in lockstep with the generator."""
    from hotato import start as _start

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

        # The false-stop card comes from its own synthetic recording: the
        # bundled demo calls carry no silence long enough to be reportable.
        fs_dir = os.path.join(tmp, "false-stop")
        os.makedirs(fs_dir)
        _write_false_stop_wav(os.path.join(fs_dir, "false-stop.wav"))
        fs_aggregate, _ = _analyze.analyze_folder(fs_dir)
        fs_json = os.path.join(tmp, "hotato-false-stop.json")
        with open(fs_json, "w", encoding="utf-8") as fh:
            json.dump(fs_aggregate, fh)
        n_fs = _rank_of_first(fs_aggregate["candidates"], _FALSE_STOP)
        # The say-do test-run result: the exact check act two of `hotato
        # start --demo` runs on the bundled scripted conversation.
        _start._run_saydo_check(tmp)
        saydo_json = os.path.join(tmp, _start._SAYDO_DIR,
                                  _start._SAYDO_RESULT)
        return {
            "no-single-threshold-card.svg":
                _card.render_plan_card(plan, font_css=font_css),
            "talk-over-card.svg":
                _card.make_card(f"{sweep_json}#{n_tov}", font_css=font_css),
            "false-stop-card.svg":
                _card.make_card(f"{fs_json}#{n_fs}", font_css=font_css),
            "say-do-card.svg":
                _card.make_card(saydo_json, font_css=font_css),
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
