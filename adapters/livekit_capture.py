#!/usr/bin/env python3
"""LiveKit capture adapter for Hotato.

Capture your own LiveKit agent's turn-taking into a TWO-CHANNEL WAV
(caller on channel 0, agent on channel 1) and score it with this tool.

What this measures, and does not
--------------------------------
The scorer measures the *timing* of turn-taking from audio energy:
``did_yield``, ``seconds_to_yield``, ``talk_over_sec``. That is all. It makes no
accuracy claim; energy is not intent. It does NO speaker identification, NO
diarization, NO transcription, and NO emotion/intent detection.

Why two channels (read this before you record)
-----------------------------------------------
The scorer can only tell "the agent talked over the caller" from "the caller
talked over the agent" when the two are on SEPARATE channels. Keep the caller on
channel 0 and the agent on channel 1. A mono-mixed export (both parties summed
into one channel) cannot attribute overlap to the right party and degrades every
number -- do not mix them down before scoring.

Two ways to use this file
-------------------------
  * live capture -- play a caller stimulus into your LiveKit ``AgentSession`` and
    record the agent's output track, then write and score a two-channel WAV.
    Needs the optional ``livekit`` stack, wired at the ADJUST points below.
  * ``--demo`` (or no args) -- NO live agent, NO third-party deps: copy the
    bundled reference two-channel recording and run it through the scorer so you
    can watch the capture -> score loop work before wiring a live agent.

Run the demo:

    PYTHONPATH=src python adapters/livekit_capture.py --demo

LiveKit's Agents API evolves, so treat the capture body as the shape of the
integration, not a pinned snippet. Docs: https://docs.livekit.io/agents/
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import sys
import tempfile
from importlib import resources
from typing import List, Optional, Tuple

# Make the tool importable whether or not it is pip-installed: fall back to the
# in-repo source tree (../src) so a plain checkout runs with no setup.
try:  # pragma: no cover - import shim
    import hotato  # noqa: F401
except ModuleNotFoundError:  # pragma: no cover - import shim
    _SRC = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src")
    if os.path.isdir(_SRC):
        sys.path.insert(0, _SRC)

# read_wav / write_wav are the stdlib-only WAV helpers the scorer itself uses
# (vendored into the tool). Using the vendored write_wav means the file you
# capture is byte-for-byte what the scorer reads. run_single is the scorer.
from hotato._engine.audio import read_wav, write_wav  # noqa: E402
from hotato.core import run_single  # noqa: E402

STACK = "livekit"
CALLER_CHANNEL = 0
AGENT_CHANNEL = 1
# Bundled reference used by --demo (a two-channel fixture that ships in the tool).
DEMO_SCENARIO = "01-hard-interruption"

_INSTALL_HINT = (
    "\nLiveKit is not installed. The LIVE capture path needs the optional stack:\n"
    "    pip install livekit livekit-agents\n"
    "Then wire the three ADJUST points in capture_agent_response() to your\n"
    "AgentSession. No agent yet? Run the zero-dependency demo instead:\n"
    "    PYTHONPATH=src python adapters/livekit_capture.py --demo\n"
)


# --- bundled resources ----------------------------------------------------

def _bundled_audio(name: str):
    return resources.files("hotato").joinpath("data", "audio", name)


def _scenario_meta(scenario_id: str) -> Tuple[Optional[float], str, str]:
    """Read a bundled scenario label -> (caller_onset_sec, expect, title)."""
    label = resources.files("hotato").joinpath(
        "data", "scenarios", scenario_id + ".json"
    )
    sc = json.loads(label.read_text(encoding="utf-8"))
    onset = sc.get("caller_onset_sec")
    expect = "yield" if sc.get("expected", {}).get("yield", True) else "hold"
    return onset, expect, sc.get("title", scenario_id)


# --- live capture (wire this to your agent) -------------------------------

def capture_agent_response(
    caller_wav_path: str,
    onset_sec: float,
    sample_rate: int = 16000,
) -> Tuple[int, List[float]]:
    """Play ``caller_wav_path`` into your LiveKit agent and return
    ``(sample_rate, agent_samples)`` recorded from the agent's output track.

    This is the only stack-specific part. The ``livekit`` import is lazy so the
    module imports fine without it; the demo path never calls this function.
    """
    try:
        from livekit import rtc  # noqa: F401
        from livekit import agents  # noqa: F401
    except Exception:
        sys.stderr.write(_INSTALL_HINT)
        raise SystemExit(2)

    import asyncio

    caller = read_wav(caller_wav_path)
    caller_samples = caller.get(0)  # noqa: F841  (fed into ADJUST 2)

    async def _run() -> Tuple[int, List[float]]:
        # ADJUST 1: connect to your room and start the AgentSession UNDER TEST so
        #           the agent already holds the floor at caller onset. This is the
        #           turn-handling configuration you are actually evaluating
        #           (current Agents API, verified 2026-07-06): AgentSession's
        #           turn_handling=TurnHandlingOptions(...) with
        #             turn_detection  (inference.TurnDetector() / "realtime_llm" /
        #                              "vad" / "stt" / "manual"),
        #             endpointing     {"min_delay": ..., "max_delay": ...},
        #             interruption    {"min_duration": ..., "min_words": ...,
        #                              "false_interruption_timeout": ...,
        #                              "resume_false_interruption": ...}.
        #     room = rtc.Room()
        #     await room.connect(LIVEKIT_URL, ACCESS_TOKEN)
        #     from livekit.agents import AgentSession, TurnHandlingOptions
        #     session = AgentSession(
        #         turn_handling=TurnHandlingOptions(...),  # <-- knobs under test
        #         ...,
        #     )
        #     await session.start(room=room, ...)

        # ADJUST 2: publish `caller_samples` as an audio track from a test
        #           participant, starting at `onset_sec` so the agent is already
        #           speaking when the caller barges in.
        #     source = rtc.AudioSource(sample_rate, num_channels=1)
        #     track = rtc.LocalAudioTrack.create_audio_track("caller", source)
        #     await room.local_participant.publish_track(track, rtc.TrackPublishOptions())
        #     await asyncio.sleep(onset_sec)
        #     # push 10 ms rtc.AudioFrame chunks built from caller_samples into source

        # ADJUST 3: subscribe to the AGENT participant's published audio track and
        #           drain its frames into `agent_samples` at `sample_rate` until
        #           the scenario window elapses.
        #     agent_samples: List[float] = []
        #     @room.on("track_subscribed")
        #     def _on_track(track, publication, participant):
        #         ...  # pull frames from an rtc.AudioStream(track) into agent_samples
        #     return sample_rate, agent_samples

        raise NotImplementedError(
            "Wire ADJUST 1-3 to your LiveKit AgentSession, then return "
            "(sample_rate, agent_samples). Until then use --demo to exercise the "
            "capture -> score loop against the bundled reference recording."
        )

    return asyncio.run(_run())


def capture_to_wav(
    caller_wav_path: str,
    out_path: str,
    onset_sec: float,
    sample_rate: int = 16000,
) -> str:
    """Capture live and write the two-channel WAV [caller, agent] the scorer reads.

    Channel 0 is the caller stimulus, channel 1 is the recorded agent. Keeping
    them on separate channels is exactly what makes talk-over attributable.
    """
    sr, agent_samples = capture_agent_response(caller_wav_path, onset_sec, sample_rate)
    caller_samples = read_wav(caller_wav_path).get(0)
    n = min(len(caller_samples), len(agent_samples))
    write_wav(out_path, sr, [caller_samples[:n], agent_samples[:n]])
    return out_path


# The public `capture(...)` this adapter exposes: play a caller stimulus into your
# live AgentSession and write the two-channel WAV. If you instead run LiveKit
# Egress, capture each participant's audio track separately (RoomComposite mixes
# to one channel and cannot attribute overlap) -> two mono WAVs -> score with
# --caller/--agent. Scaffold that path with:  hotato setup --stack livekit
capture = capture_to_wav


# --- scoring --------------------------------------------------------------

def score(
    wav_path: str,
    stack: str = STACK,
    onset_sec: Optional[float] = None,
    expect: str = "yield",
) -> dict:
    """Score a two-channel capture through hotato and return the envelope.

    ``expect`` is 'yield' (the agent should stop for a real interruption) or
    'hold' (the caller event is a backchannel and the agent should keep talking).
    """
    return run_single(stereo=wav_path, stack=stack, onset_sec=onset_sec, expect=expect)


def _report(env: dict) -> int:
    """Print the three timing signals and the PASS/FAIL verdict; return exit code."""
    ev = env["events"][0]
    v = ev["verdict"]
    print(
        "[score] did_yield={} seconds_to_yield={} talk_over_sec={}".format(
            v["did_yield"], v["seconds_to_yield"], v["talk_over_sec"]
        )
    )
    if v["passed"]:
        print("[score] verdict: PASS")
    else:
        print("[score] verdict: FAIL -- " + "; ".join(v["reasons"]))
    return env["exit_code"]


# --- zero-dependency demo -------------------------------------------------

def demo(scenario_id: str = DEMO_SCENARIO) -> int:
    """Copy a bundled two-channel reference recording and score it end to end.

    No live agent and no third-party deps: this stands in for "a live capture
    wrote this WAV", proving the capture -> score loop before you wire an agent.
    """
    onset, expect, title = _scenario_meta(scenario_id)
    out = tempfile.NamedTemporaryFile(
        prefix="hotato-{}-".format(STACK), suffix=".captured.wav", delete=False
    ).name
    with resources.as_file(_bundled_audio(scenario_id + ".example.wav")) as src:
        shutil.copyfile(src, out)
    print("[demo] {}: bundled reference '{}' ({})".format(STACK, scenario_id, title))
    print("[demo] wrote two-channel capture -> {}".format(out))
    env = score(out, stack=STACK, onset_sec=onset, expect=expect)
    return _report(env)


# --- CLI ------------------------------------------------------------------

def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        description="LiveKit capture -> hotato scorer (two-channel WAV)."
    )
    parser.add_argument(
        "--demo", action="store_true",
        help="zero-dependency: copy the bundled reference recording and score it",
    )
    parser.add_argument("--caller", help="caller stimulus WAV to play into your agent (live capture)")
    parser.add_argument("--out", help="output two-channel WAV path for live capture")
    parser.add_argument("--onset", type=float, default=None, help="caller onset, seconds")
    parser.add_argument("--expect", choices=["yield", "hold"], default="yield")
    parser.add_argument("--stack", default=STACK, help="fix-map stack tag for the scorer")
    parser.add_argument("--score", dest="score_path", help="score an existing two-channel WAV and exit")
    args = parser.parse_args(argv)

    # No args (or --demo) -> prove the loop with the bundled reference, zero deps.
    if args.demo or (not args.caller and not args.score_path):
        return demo()

    if args.score_path:
        return _report(score(args.score_path, stack=args.stack, onset_sec=args.onset, expect=args.expect))

    out = args.out or (os.path.splitext(args.caller)[0] + ".captured.wav")
    capture_to_wav(args.caller, out, args.onset or 0.0)
    print("[capture] wrote two-channel capture -> {}".format(out))
    return _report(score(out, stack=args.stack, onset_sec=args.onset, expect=args.expect))


if __name__ == "__main__":
    raise SystemExit(main())
