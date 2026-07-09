"""``hotato start --demo``: the guided, credential-less first run.

One command, no account, no network. It sweeps the two bundled real demo calls
(the same recordings ``hotato demo`` scores), writes the sweep result as JSON and
as a self-contained HTML dashboard, renders the threshold-funnel card, and then
prints the exact next commands -- promote a candidate into a permanent fixture,
run those fixtures in CI, and render a card from any candidate.

Everything is offline by construction: the demo pulls from packaged audio, the
analyze and card steps touch no network, and no credential is read.
"""

from __future__ import annotations

import os
import sys
from importlib import resources
from typing import Optional

from . import card as _card
from . import errors as _errors

_SWEEP_JSON = "hotato-sweep.json"
_SWEEP_HTML = "hotato-sweep.html"
_FUNNEL_CARD = "hotato-no-single-threshold.svg"


def _demo_audio_dir() -> str:
    return str(resources.files("hotato").joinpath("data", "demo", "failing",
                                                   "audio"))


def _write_text(path: str, text: str) -> None:
    from .cli import _atomic_write_text
    _atomic_write_text(path, text)


def _funnel_plan() -> dict:
    """Build the threshold-funnel fix plan from the bundled failing demo
    battery, in process (no subprocess, no network). Same code the CLI's
    ``plan`` path runs: score the demo suite, diagnose it, build the plan."""
    from .core import run_suite
    from .diagnose import diagnose_envelope
    from .fixplan import build_plan

    root = resources.files("hotato").joinpath("data", "demo", "failing")
    env = run_suite(scenarios_dir=str(root.joinpath("scenarios")),
                    audio_dir=str(root.joinpath("audio")))
    return build_plan(diagnosis=diagnose_envelope(env))


def _sweep_demo(out_dir: str) -> dict:
    """Sweep the bundled demo calls into ``out_dir``: writes the JSON result and
    the HTML dashboard, and returns the aggregate. Mirrors ``capture.run_sweep``
    ``--demo``'s JSON envelope so the printed commands work against a real sweep
    unchanged."""
    from . import analyze as _analyze

    audio_dir = _demo_audio_dir()
    aggregate, per_file = _analyze.analyze_folder(audio_dir)

    top = 25
    capped = dict(aggregate)
    capped["candidates"] = aggregate["candidates"][:top]
    capped["shown"] = len(capped["candidates"])
    capped["pull"] = {"stack": "demo", "listed": aggregate["calls_scanned"],
                      "pulled": aggregate["calls_scanned"], "skipped": 0}

    json_path = os.path.join(out_dir, _SWEEP_JSON)
    _write_text(json_path, _errors.safe_json_dumps(capped, indent=2) + "\n")

    html = _analyze.build_dashboard_html(
        aggregate, per_file, top=top, audio_top=8, report_json=_SWEEP_JSON)
    _write_text(os.path.join(out_dir, _SWEEP_HTML), html)
    return aggregate


def _next_commands_text(card_written: bool) -> str:
    lines = [
        "",
        "Next steps (all offline, no credentials):",
        "",
        "  1. Save a candidate as a permanent regression test (you choose the "
        "label):",
        f"     hotato fixture promote {_SWEEP_JSON}#1 --expect <yield|hold> \\",
        "         --id my-first-fixture --out tests/hotato",
        "",
        "  2. Run your fixtures in CI (exits non-zero on a regression):",
        "     hotato run --scenarios tests/hotato/scenarios --audio "
        "tests/hotato/audio",
        "",
        "  3. Render a shareable card from any candidate:",
        f"     hotato card {_SWEEP_JSON}#1 --out candidate.svg",
    ]
    if card_written:
        lines += [
            "",
            f"The threshold-funnel card is already rendered: {_FUNNEL_CARD}",
        ]
    return "\n".join(lines)


def run_start(*, demo: bool = False, stack: Optional[str] = None,
              folder: Optional[str] = None, stereo: Optional[str] = None,
              out_dir: Optional[str] = None, fmt: str = "text") -> int:
    """``hotato start``. Only ``--demo`` fully runs in this build; the other
    modes are stubbed and route to the shipped command that does the job."""
    modes = [m for m, on in (("--demo", demo), ("--stack", stack),
                             ("--folder", folder), ("--stereo", stereo)) if on]
    if not modes:
        raise ValueError(
            "choose a mode: hotato start --demo (the guided, credential-less "
            "first run). --stack/--folder/--stereo are placeholders in this "
            "build; use hotato sweep / hotato analyze / hotato run for those."
        )
    if not demo:
        # Stub modes: point at the shipped primitive rather than pretend.
        route = {"--stack": "hotato sweep --stack <stack>",
                 "--folder": "hotato analyze <folder>",
                 "--stereo": "hotato run --stereo <call.wav>"}[modes[0]]
        msg = (f"hotato start {modes[0]} is not yet in this build. "
               f"For now, run: {route}")
        if fmt == "json":
            print(_errors.safe_json_dumps(
                {"tool": "hotato", "kind": "start", "mode": modes[0],
                 "ran": False, "route": route, "message": msg}, indent=2))
        else:
            print(msg)
        return 0

    out_dir = out_dir or "."
    if not os.path.isdir(out_dir):
        raise ValueError(f"--dir {out_dir!r} is not a directory")

    aggregate = _sweep_demo(out_dir)

    # Render the hero card. "If the plan path works": the bundled demo always
    # funnels, but never let a card hiccup break the guided first run.
    card_written = False
    try:
        svg = _card.render_plan_card(_funnel_plan())
        _write_text(os.path.join(out_dir, _FUNNEL_CARD), svg)
        card_written = True
    except Exception:  # pragma: no cover - the demo plan is always the funnel
        card_written = False

    written = [_SWEEP_JSON, _SWEEP_HTML] + ([_FUNNEL_CARD] if card_written else [])
    sys.stderr.write(
        f"[start] demo: swept 2 bundled calls, {aggregate['total_candidates']} "
        f"candidate moments; wrote {', '.join(written)}\n")

    if fmt == "json":
        print(_errors.safe_json_dumps({
            "tool": "hotato", "kind": "start", "mode": "--demo", "ran": True,
            "offline": True, "written": written,
            "total_candidates": aggregate["total_candidates"],
            "next_commands": [
                f"hotato fixture promote {_SWEEP_JSON}#1 --expect "
                "<yield|hold> --id my-first-fixture --out tests/hotato",
                "hotato run --scenarios tests/hotato/scenarios --audio "
                "tests/hotato/audio",
                f"hotato card {_SWEEP_JSON}#1 --out candidate.svg",
            ],
        }, indent=2))
    else:
        print("hotato start: swept the 2 bundled demo calls offline.")
        print(f"  sweep result:    {_SWEEP_JSON}")
        print(f"  sweep dashboard: {_SWEEP_HTML}")
        if card_written:
            print(f"  funnel card:     {_FUNNEL_CARD}")
        print(_next_commands_text(card_written))
    return 0
