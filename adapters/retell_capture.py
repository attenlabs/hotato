#!/usr/bin/env python3
"""Retell capture adapter for Hotato -- HONEST: no self-serve stereo export.

Status (verified at build time)
-------------------------------
No confirmed self-serve STEREO / dual-channel recording export was found for
Retell. ``GET /v2/get-call/{call_id}`` returns a single ``recording_url``
(mixed / mono). A mono mix CANNOT attribute overlap to caller vs agent, so
scoring it is degraded, not authoritative. Hotato will not fake a capture path
that does not exist.

Workarounds (in order of fidelity)
----------------------------------
1. Capture dual-channel at the TELEPHONY layer you control. If Retell rides on a
   Twilio number you own, record dual-channel there and use ``twilio_capture.py``.
2. Use a SIP / media-server recording that keeps the two legs on separate
   channels; export a 2-channel WAV (caller ch0, agent ch1), then score it:
       python adapters/retell_capture.py --stereo your_dual_channel.wav
       # or:  hotato capture --stack retell --stereo your_dual_channel.wav
3. Last resort (clearly degraded): score the mono recording with an onset label
       hotato run --caller retell_mono.wav --agent retell_mono.wav --onset <sec>
   and treat the result as indicative only.

OPEN QUESTION: if Retell has added a stereo/dual-channel export, please open an
issue with the API shape and we will add a first-class adapter.

Run ``--demo`` to prove the SCORE path on a bundled two-channel reference (offline,
zero deps). ``--setup`` prints the full workaround note. Docs: https://docs.retellai.com
"""

from __future__ import annotations

import argparse
import os
import sys
from typing import List, Optional

try:  # pragma: no cover - import shim
    import hotato  # noqa: F401
except ModuleNotFoundError:  # pragma: no cover - import shim
    _SRC = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src")
    if os.path.isdir(_SRC):
        sys.path.insert(0, _SRC)

from hotato.capture import (  # noqa: E402
    demo as _demo,
    run_capture,
    score,
    setup_text,
)

STACK = "retell"

__all__ = ["capture", "score", "demo", "main"]


def capture(*_args, **_kwargs):
    """Retell has no self-serve stereo export -- there is nothing to fetch.

    Score a dual-channel WAV you assembled via one of the documented workarounds
    with ``score(wav, stack='retell', ...)`` instead. See ``setup_text('retell')``.
    """
    raise ValueError(setup_text(STACK))


def demo() -> int:
    """Zero-dependency, offline: prove the SCORE path on a bundled reference."""
    return _demo(STACK)


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        description="Retell (honest: no self-serve stereo export) -> hotato scorer.",
    )
    parser.add_argument("--demo", action="store_true",
                        help="zero-dependency: prove the score path on a bundled reference")
    parser.add_argument("--setup", action="store_true", help="print the honest workaround note and exit")
    parser.add_argument("--stereo", "--wav", dest="stereo",
                        help="score a dual-channel WAV you assembled via the workaround")
    parser.add_argument("--onset", type=float, default=None, help="caller onset, seconds (else auto)")
    parser.add_argument("--expect", choices=["yield", "hold"], default="yield")
    parser.add_argument("--format", choices=["text", "json"], default="text")
    args = parser.parse_args(argv)

    if args.setup:
        print(setup_text(STACK), end="")
        return 0
    if not (args.demo or args.stereo):
        # Default to proving the loop offline, then remind the user of the status.
        rc = demo()
        sys.stderr.write(
            "note: Retell has no self-serve stereo export. Run --setup for the "
            "workaround, or --stereo <dual_channel.wav> to score a real call.\n"
        )
        return rc

    try:
        return run_capture(
            STACK,
            demo=args.demo,
            stereo=args.stereo,
            onset=args.onset,
            expect=args.expect,
            fmt=args.format,
        )
    except (ValueError, FileNotFoundError) as exc:
        sys.stderr.write(f"error: {exc}\n")
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
