"""``hotato start --demo``: the guided, credential-less first run.

One command, no account, no network. It sweeps the two bundled real demo calls
(the same recordings ``hotato demo`` scores), writes the sweep result as JSON and
as a self-contained HTML dashboard, renders the threshold-funnel card, creates
and verifies one demo failure contract, and then prints the exact next
commands -- promote a candidate into a permanent fixture, run those fixtures
in CI, verify the demo contract, and render a card from any candidate.

Everything is offline by construction: the demo pulls from packaged audio, the
analyze/card/contract steps touch no network, and no credential is read.
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
_CONTRACTS_DIR = "contracts"
_DEMO_CONTRACT_ID = "demo-missed-interruption"
# The demo sweep's candidates are ranked by salience over the two FIXED
# bundled recordings, so this index is stable (asserted in
# tests/test_start_cli.py): candidate #2 is the real missed-interruption call
# (fd-01-missed-interruption.example.wav's agent-talks-over moment), the left
# failure the threshold-funnel card advertises. Scored with --expect yield it
# genuinely FAILS -- the agent talked over the caller instead of yielding --
# which is the point: the first run creates a contract from a real failure,
# not a contrived pass.
_DEMO_CONTRACT_CANDIDATE = 2


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


def _demo_contract_bundle_rel() -> str:
    """The demo contract's bundle path, relative to ``--dir`` (never joined
    with ``out_dir``): the same bare-relative convention ``_SWEEP_JSON`` and
    ``_FUNNEL_CARD`` use, since every printed/returned path here is meant to
    be read from inside ``--dir``."""
    from . import contract as _contract
    return f"{_CONTRACTS_DIR}/{_DEMO_CONTRACT_ID}{_contract.BUNDLE_SUFFIX}"


def _create_and_verify_demo_contract(out_dir: str, sweep_json_path: str) -> dict:
    """Create ``contracts/demo-missed-interruption.hotato`` from the bundled
    demo sweep's ``#_DEMO_CONTRACT_CANDIDATE`` candidate (the real
    missed-interruption call) with ``--expect yield``, then verify it
    immediately with ``hotato contract verify``. This turns the whole loop
    the README promises -- a real failure becomes a candidate, becomes a
    portable contract, and ``contract verify`` catches it -- into something a
    first run actually sees once, offline, with no credential.

    Returns ``{"bundle_dir", "bundle_rel", "contracts_dir", "passed",
    "scorable"}``. Raises on the SAME conditions ``contract create``/
    ``contract verify`` would (never on the bundled demo audio in practice);
    the caller treats a failure here the same defensive way it treats a card
    render failure -- the guided run still finishes.
    """
    from . import contract as _contract

    contracts_dir = os.path.join(out_dir, _CONTRACTS_DIR)
    create_result = _contract.create_contract(
        from_candidate=f"{sweep_json_path}#{_DEMO_CONTRACT_CANDIDATE}",
        contract_id=_DEMO_CONTRACT_ID,
        expect="yield",
        out_dir=contracts_dir,
        force=True,
    )
    verify = _contract.verify_contracts(contracts_dir)
    result = verify["results"][0]
    return {
        "bundle_dir": create_result["dir"],
        "bundle_rel": _demo_contract_bundle_rel(),
        "contracts_dir": contracts_dir,
        "passed": result["passed"],
        "scorable": result["scorable"],
    }


def _next_commands_text(card_written: bool, contract_written: bool) -> str:
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
    if contract_written:
        lines += [
            "",
            "  4. Re-verify the demo failure contract in CI (or create your "
            "own from any candidate with `hotato contract create`):",
            f"     hotato contract verify {_CONTRACTS_DIR}/",
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

    # Create and verify the demo failure contract. Defensive the same way the
    # card step is: the bundled demo audio always produces a scorable, always
    # -failing (by design) contract, but a hiccup here must never break the
    # rest of the guided first run.
    contract_info = None
    try:
        contract_info = _create_and_verify_demo_contract(
            out_dir, os.path.join(out_dir, _SWEEP_JSON))
    except Exception:  # pragma: no cover - the bundled candidate is always scorable
        contract_info = None
    contract_written = contract_info is not None

    written = ([_SWEEP_JSON, _SWEEP_HTML]
               + ([_FUNNEL_CARD] if card_written else [])
               + ([contract_info["bundle_rel"] + "/contract.json"]
                  if contract_written else []))
    sys.stderr.write(
        f"[start] demo: swept 2 bundled calls, {aggregate['total_candidates']} "
        f"candidate moments; wrote {', '.join(written)}\n")

    if fmt == "json":
        payload = {
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
        }
        if contract_written:
            payload["next_commands"].append(
                f"hotato contract verify {_CONTRACTS_DIR}/")
            payload["contract"] = {
                "id": _DEMO_CONTRACT_ID,
                "dir": contract_info["bundle_rel"],
                "expect": "yield",
                "scorable": contract_info["scorable"],
                "passed": contract_info["passed"],
                "verified_fail_as_expected": contract_info["passed"] is False,
            }
        print(_errors.safe_json_dumps(payload, indent=2))
    else:
        print("hotato start: swept the 2 bundled demo calls offline.")
        print(f"  sweep result:    {_SWEEP_JSON}")
        print(f"  sweep dashboard: {_SWEEP_HTML}")
        if card_written:
            print(f"  funnel card:     {_FUNNEL_CARD}")
        if contract_written:
            print(f"  demo contract:   {contract_info['bundle_rel']}")
            if contract_info["passed"] is False:
                print("  verified contract: FAIL as expected -- the demo "
                      "call really did miss the interruption; this is the "
                      "exact failure a CI regression gate would catch")
            else:  # pragma: no cover - the bundled demo candidate always fails
                mark = "PASS" if contract_info["passed"] else "NOT SCORABLE"
                print(f"  verified contract: {mark}")
        print(_next_commands_text(card_written, contract_written))
    return 0
