"""One-tool MCP server exposing the identical evaluation as a single tool.

Run it (zero-install) with the MCP extra:

    uvx --from "hotato[mcp]" hotato-mcp

or, if installed:

    python -m hotato.mcp_server

It speaks MCP over stdio and exposes exactly one tool, ``voice_eval_run``, whose
schema states the honest scope and ceiling. The tool returns the same JSON
envelope as the CLI. Everything runs locally; no audio leaves the machine.
"""

from __future__ import annotations

import sys
from typing import Optional

from .core import LIMITS, SUITE_ID, run_single, run_suite

_TOOL_DESCRIPTION = f"""\
Score voice-agent reliability from a call recording and return a machine-readable
verdict with an actionable, honest fix for each failing event.

WHAT IT MEASURES (scope): barge-in, turn-taking, overlap / talk-over, and
backchannel handling. For each event it returns three objective TIMING signals:
did_yield (did the agent stop for the caller), seconds_to_yield (latency of that
yield), and talk_over_sec (overlapping seconds before it yielded).

TWO MODES:
  * single recording  -> pass `stereo` (a two-channel WAV, caller on one channel
                         and agent on the other) OR both `caller` and `agent`
                         mono WAV paths. Set `expect` to "yield" (the agent
                         should stop for the caller) or "hold" (the caller event
                         is a backchannel and the agent should keep the floor).
  * battery           -> pass suite="{SUITE_ID}" to run the bundled 8-scenario
                         labelled battery shipped inside the package.

FIX MAP: every failing event carries a fix. fix_class is one of:
  * "config"            - a concrete knob for the named `stack`
                          (livekit|pipecat|vapi|generic), with the direction to
                          move it and the honest trade-off it makes. No upsell.
  * "engagement-control"- the failure is a discrimination problem (a genuine bid
                          for the floor vs a backchannel / speech not addressed
                          to the agent) that a single sensitivity dial cannot
                          solve; points, high level and with no numbers, at an
                          engagement-control / addressee-detection layer.

HONEST SCOPE AND LIMITS (read before trusting a number):
  * Method: {LIMITS['method']}
  * There is NO accuracy percentage and none is implied. These are reproducible
    timing measurements with every threshold exposed and every frame inspectable.
  * Ceiling: {LIMITS['ceiling']}
  * Best input: {LIMITS['best_input']}
  * It does NOT do: speaker identification, diarization, transcription, or emotion
    detection, and it makes no claim about any vendor's internal accuracy.
  * Offline: runs locally; no audio egress.
"""


def _run_tool(
    stereo: Optional[str] = None,
    caller: Optional[str] = None,
    agent: Optional[str] = None,
    suite: Optional[str] = None,
    stack: str = "generic",
    expect: str = "yield",
    onset_sec: Optional[float] = None,
    caller_channel: int = 0,
    agent_channel: int = 1,
    max_talk_over_sec: Optional[float] = None,
    max_time_to_yield_sec: Optional[float] = None,
) -> dict:
    """Shared implementation for the single MCP tool. Returns the JSON envelope."""
    if suite:
        return run_suite(suite=suite, stack=stack)
    return run_single(
        stereo=stereo,
        caller=caller,
        agent=agent,
        caller_channel=caller_channel,
        agent_channel=agent_channel,
        onset_sec=onset_sec,
        expect=expect,
        stack=stack,
        max_talk_over_sec=max_talk_over_sec,
        max_time_to_yield_sec=max_time_to_yield_sec,
    )


def build_server():
    """Construct the FastMCP server with the single tool registered."""
    try:
        from mcp.server.fastmcp import FastMCP
    except Exception as exc:  # pragma: no cover - only when extra not installed
        raise SystemExit(
            "the MCP SDK is not installed. Install the extra:\n"
            "    pip install 'hotato[mcp]'\n"
            "or run zero-install:\n"
            '    uvx --from "hotato[mcp]" hotato-mcp\n'
            f"(import error: {exc})"
        )

    server = FastMCP("hotato")

    @server.tool(name="voice_eval_run", description=_TOOL_DESCRIPTION)
    def voice_eval_run(
        stereo: Optional[str] = None,
        caller: Optional[str] = None,
        agent: Optional[str] = None,
        suite: Optional[str] = None,
        stack: str = "generic",
        expect: str = "yield",
        onset_sec: Optional[float] = None,
        caller_channel: int = 0,
        agent_channel: int = 1,
        max_talk_over_sec: Optional[float] = None,
        max_time_to_yield_sec: Optional[float] = None,
    ) -> dict:
        return _run_tool(
            stereo=stereo,
            caller=caller,
            agent=agent,
            suite=suite,
            stack=stack,
            expect=expect,
            onset_sec=onset_sec,
            caller_channel=caller_channel,
            agent_channel=agent_channel,
            max_talk_over_sec=max_talk_over_sec,
            max_time_to_yield_sec=max_time_to_yield_sec,
        )

    return server


def main(argv=None) -> int:
    server = build_server()
    server.run()  # stdio transport by default
    return 0


if __name__ == "__main__":
    sys.exit(main())
