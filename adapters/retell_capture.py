#!/usr/bin/env python3
"""Retell capture adapter for Hotato.

Score a REAL Retell call's turn-taking with one command and only an API key:

    export RETELL_API_KEY=<your api key>
    python adapters/retell_capture.py --call-id <call-id>
    # or, installed:  hotato capture --stack retell --call-id <call-id>

No SDK, no export step. Retell exposes per-party (multi-channel) recordings on
the call object after the call ends; this adapter downloads the 2-channel WAV,
validates it, and scores it OFFLINE.

API basis (verified against docs.retellai.com/api-references/get-call, 2026-07-06)
-----------------------------------------------------------------------------------
``GET https://api.retellai.com/v2/get-call/{call_id}`` with
``Authorization: Bearer <RETELL_API_KEY>`` returns the call object with:

  * ``scrubbed_recording_multi_channel_url`` -- per-party channels, PII scrubbed
    (preferred),
  * ``recording_multi_channel_url``          -- per-party channels,
  * ``recording_url``                        -- plain mono mix.

Hotato prefers the scrubbed multi-channel file, falls back to the unscrubbed
one, and validates the download has exactly 2 channels. The plain mono
``recording_url`` is REJECTED by default: a mono mix cannot attribute talk-over
to caller vs agent. ``--allow-mono`` opts into scoring it anyway, clearly marked
degraded and indicative only.

What this measures, and does not
--------------------------------
The scorer measures the *timing* of turn-taking from audio energy: ``did_yield``,
``seconds_to_yield``, ``talk_over_sec``. That is all. No accuracy claim; energy is
not intent. No speaker-ID, no diarization, no transcription, no emotion/intent.

The real logic is single-sourced in ``hotato.capture`` (so the CLI and this file
never drift). Live-verification (a real Retell key + an ended, recorded call) is
on your side; it cannot be exercised in an offline build. Run ``--demo`` to watch
the capture -> score loop work with zero deps and zero network.
Docs: https://docs.retellai.com/api-references/get-call
"""

from __future__ import annotations

import argparse
import os
import sys
from typing import List, Optional

# Make the tool importable whether or not it is pip-installed: fall back to the
# in-repo source tree (../src) so a plain checkout runs with no setup.
try:  # pragma: no cover - import shim
    import hotato  # noqa: F401
except ModuleNotFoundError:  # pragma: no cover - import shim
    _SRC = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src")
    if os.path.isdir(_SRC):
        sys.path.insert(0, _SRC)

from hotato.capture import (  # noqa: E402
    capture_retell as capture,  # capture(*, call_id, api_key, out_path=None, allow_mono=False) -> wav path
    demo as _demo,
    run_capture,
    score,
    setup_text,
)

STACK = "retell"

__all__ = ["capture", "score", "demo", "main"]


def demo() -> int:
    """Zero-dependency, offline: copy a bundled two-channel reference and score it."""
    return _demo(STACK)


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        description="Retell capture -> hotato scorer (downloads the call's multi-channel recording).",
    )
    parser.add_argument("--demo", action="store_true",
                        help="zero-dependency: copy the bundled reference recording and score it")
    parser.add_argument("--setup", action="store_true",
                        help="print the recording setup note and exit")
    parser.add_argument("--call-id", help="id of an ended, recorded Retell call")
    parser.add_argument("--api-key", help="Retell API key (else env RETELL_API_KEY)")
    parser.add_argument("--allow-mono", action="store_true",
                        help="degraded: score the mono recording_url when no multi-channel recording exists")
    parser.add_argument("--out", help="where to write the downloaded WAV (else a temp file)")
    parser.add_argument("--stereo", "--wav", dest="stereo", help="score an existing 2-channel WAV instead")
    parser.add_argument("--onset", type=float, default=None, help="caller onset, seconds (else auto)")
    parser.add_argument("--expect", choices=["yield", "hold"], default="yield")
    parser.add_argument("--format", choices=["text", "json"], default="text")
    args = parser.parse_args(argv)

    if args.setup:
        print(setup_text(STACK), end="")
        return 0

    # No args at all -> prove the loop offline, like the other adapters.
    if not (args.demo or args.call_id or args.stereo):
        return demo()

    try:
        return run_capture(
            STACK,
            demo=args.demo,
            stereo=args.stereo,
            call_id=args.call_id,
            api_key=args.api_key or os.environ.get("RETELL_API_KEY"),
            allow_mono=args.allow_mono,
            onset=args.onset,
            expect=args.expect,
            out=args.out,
            fmt=args.format,
        )
    except (ValueError, FileNotFoundError) as exc:
        sys.stderr.write(f"error: {exc}\n")
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
