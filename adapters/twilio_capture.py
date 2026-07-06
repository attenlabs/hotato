#!/usr/bin/env python3
"""Twilio capture adapter for Hotato.

Score a REAL Twilio DUAL-CHANNEL recording's turn-taking with one command:

    export TWILIO_ACCOUNT_SID=AC...  TWILIO_AUTH_TOKEN=...
    python adapters/twilio_capture.py --recording-sid RE...
    # or, installed:  hotato capture --stack twilio --recording-sid RE...

Record dual-channel so caller and agent land on SEPARATE channels
--------------------------------------------------------------------
Request dual-channel when the recording is created:
    <Record recordingChannels="dual" .../>          (TwiML)
    <Dial record="record-from-answer-dual">         (TwiML Dial)
    RecordingChannels=dual                          (REST create-recording)

A mono mix cannot attribute overlap to caller vs agent -- keep the two on
separate channels all the way to the WAV.

API basis (verified against twilio.com/docs/voice/api/recording, 2026-07-06)
-----------------------------------------------------------------------------
``GET .../Accounts/{AccountSid}/Recordings/{RE...}.wav?RequestedChannels=2`` with
HTTP Basic auth (AccountSid:AuthToken). Appending ``?RequestedChannels=2`` is the
documented way to request the dual-channel media. When the dual-channel format is
not available Twilio returns ``400 Bad Request``; Hotato then stops with a clear
message (the recording is mono and cannot attribute talk-over) unless you opt
into the degraded mono path with ``--allow-mono``. The download is validated to
have exactly 2 channels.

Channel order (per Twilio's dual-channel docs): two-party calls put the
customer/caller on the first (left) channel and the agent on the second (right)
channel -- Hotato's default caller=ch0, agent=ch1 matches. Conference recordings
put the FIRST participant to join on the first channel; if caller/agent look
swapped, pass different --caller-channel/--agent-channel.

What this measures, and does not
--------------------------------
Timing only: ``did_yield``, ``seconds_to_yield``, ``talk_over_sec``. No accuracy
claim; energy is not intent. No speaker-ID, diarization, transcription, or emotion.

The real logic is single-sourced in ``hotato.capture``. Live-verification (real
Twilio credentials + a dual-channel recording) is on your side. Run ``--demo`` to
watch the loop work offline. Docs: https://www.twilio.com/docs/voice/api/recording
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
    capture_twilio as capture,  # capture(*, recording_sid, account_sid, auth_token, out_path=None, allow_mono=False)
    demo as _demo,
    run_capture,
    score,
)

STACK = "twilio"

__all__ = ["capture", "score", "demo", "main"]


def demo() -> int:
    """Zero-dependency, offline: copy a bundled two-channel reference and score it."""
    return _demo(STACK)


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        description="Twilio dual-channel recording -> hotato scorer.",
    )
    parser.add_argument("--demo", action="store_true",
                        help="zero-dependency: copy the bundled reference recording and score it")
    parser.add_argument("--recording-sid", help="Recording SID (RE...) of a dual-channel recording")
    parser.add_argument("--account-sid", help="Account SID (else env TWILIO_ACCOUNT_SID)")
    parser.add_argument("--auth-token", help="Auth Token (else env TWILIO_AUTH_TOKEN)")
    parser.add_argument("--allow-mono", action="store_true",
                        help="degraded: fall back to the mono media when dual-channel is unavailable (400)")
    parser.add_argument("--out", help="where to write the downloaded WAV (else a temp file)")
    parser.add_argument("--stereo", "--wav", dest="stereo", help="score an existing 2-channel WAV instead")
    parser.add_argument("--onset", type=float, default=None, help="caller onset, seconds (else auto)")
    parser.add_argument("--expect", choices=["yield", "hold"], default="yield")
    parser.add_argument("--caller-channel", type=int, default=0)
    parser.add_argument("--agent-channel", type=int, default=1)
    parser.add_argument("--format", choices=["text", "json"], default="text")
    args = parser.parse_args(argv)

    if not (args.demo or args.recording_sid or args.stereo):
        return demo()

    try:
        return run_capture(
            STACK,
            demo=args.demo,
            stereo=args.stereo,
            recording_sid=args.recording_sid,
            account_sid=args.account_sid or os.environ.get("TWILIO_ACCOUNT_SID"),
            auth_token=args.auth_token or os.environ.get("TWILIO_AUTH_TOKEN"),
            allow_mono=args.allow_mono,
            onset=args.onset,
            expect=args.expect,
            caller_channel=args.caller_channel,
            agent_channel=args.agent_channel,
            out=args.out,
            fmt=args.format,
        )
    except (ValueError, FileNotFoundError) as exc:
        sys.stderr.write(f"error: {exc}\n")
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
