#!/usr/bin/env python3
"""Vapi capture adapter for Hotato -- the flagship, near-zero-friction path.

Score a REAL Vapi call's turn-taking with one command and only an API key:

    export VAPI_API_KEY=<your private key>
    python adapters/vapi_capture.py --call-id <call-id>
    # or, installed:  hotato capture --stack vapi --call-id <call-id>

No SDK, no export step. Vapi produces a two-channel (stereo) recording for
recorded calls -- customer on channel 0, assistant on channel 1 -- and this
adapter downloads it and scores it OFFLINE.

API basis (verified)
--------------------
``GET https://api.vapi.ai/call/{id}`` with ``Authorization: Bearer <private key>``
returns the Call object; its ``artifact.stereoRecordingUrl`` is a pre-signed
2-channel WAV. We download that URL and score it. The only network egress is the
direct download from Vapi to your machine.

What this measures, and does not
--------------------------------
The scorer measures the *timing* of turn-taking from audio energy: ``did_yield``,
``seconds_to_yield``, ``talk_over_sec``. That is all. No accuracy claim; energy is
not intent. No speaker-ID, no diarization, no transcription, no emotion/intent.

The real logic is single-sourced in ``hotato.capture`` (so the CLI and this file
never drift). Live-verification (a real Vapi key + an ended, recorded call) is on
your side; it cannot be exercised in an offline build. Run ``--demo`` to watch the
capture -> score loop work with zero deps and zero network. Docs: https://docs.vapi.ai
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
    capture_vapi as capture,   # capture(*, call_id, api_key, out_path=None) -> wav path
    demo as _demo,
    run_capture,
    score,
)

STACK = "vapi"

__all__ = ["capture", "score", "demo", "main"]


def demo() -> int:
    """Zero-dependency, offline: copy a bundled two-channel reference and score it."""
    return _demo(STACK)


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        description="Vapi capture -> hotato scorer (downloads the call's stereo recording).",
    )
    parser.add_argument("--demo", action="store_true",
                        help="zero-dependency: copy the bundled reference recording and score it")
    parser.add_argument("--call-id", help="id of an ended, recorded Vapi call")
    parser.add_argument("--api-key", help="Vapi private key (else env VAPI_API_KEY)")
    parser.add_argument("--out", help="where to write the downloaded stereo WAV (else a temp file)")
    parser.add_argument("--stereo", "--wav", dest="stereo", help="score an existing 2-channel WAV instead")
    parser.add_argument("--onset", type=float, default=None, help="caller onset, seconds (else auto)")
    parser.add_argument("--expect", choices=["yield", "hold"], default="yield")
    parser.add_argument("--format", choices=["text", "json"], default="text")
    args = parser.parse_args(argv)

    # No args at all -> prove the loop offline, like the other adapters.
    if not (args.demo or args.call_id or args.stereo):
        return demo()

    try:
        return run_capture(
            STACK,
            demo=args.demo,
            stereo=args.stereo,
            call_id=args.call_id,
            api_key=args.api_key or os.environ.get("VAPI_API_KEY"),
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
