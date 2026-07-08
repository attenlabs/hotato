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

import os
import sys
from typing import Optional

from . import errors as _errors
from .core import LIMITS, SUITE_ID, process_exit_code, run_single, run_suite

_TOOL_DESCRIPTION = f"""\
Find where your voice agent talks over callers, and keep it from coming back.
Offline turn-taking regression tests from your own call recordings. Score a call
recording and return a machine-readable verdict with an actionable, honest fix
for each failing event.

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

REPORT (optional): set `report_path` to also write the self-contained HTML
report (per-event timelines + analytics, offline, zero external requests) to
that path. The returned envelope then carries `report_path` (absolute path);
everything else in the envelope is unchanged. Purely additive.

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

SCHEMA: the returned envelope's shape is documented at https://hotato.dev/schema/envelope.v1.json (schema_version "1", additive-only).
RUN THIS SERVER (zero-install): `uvx --from "hotato[mcp]" hotato-mcp` (the bare `uvx hotato-mcp`, with no `--from`, fails).
"""


def _guard_report_path(report_path: str) -> str:
    """Validate an MCP-supplied ``report_path`` before it is written.

    This tool is called by an LLM agent, possibly acting on untrusted content it
    is summarising (a transcript / document that could carry an injected
    'write to ~/.ssh/authorized_keys' instruction). ``write_report`` does a bare
    truncate-and-overwrite, so the destination is validated here:

      * when ``HOTATO_MCP_REPORT_DIR`` is set, the resolved real path MUST stay
        inside that directory (no absolute escape, no ``..`` traversal);
      * an EXISTING destination is only overwritten if it is already a
        hotato-produced report (carries the ``hotato`` marker), so the tool can
        never clobber an arbitrary pre-existing file.

    Raises ValueError (surfaced as the shared structured error) on refusal."""
    real = os.path.realpath(os.path.expanduser(report_path))
    base = os.environ.get("HOTATO_MCP_REPORT_DIR", "").strip()
    if base:
        base_real = os.path.realpath(os.path.expanduser(base))
        try:
            inside = os.path.commonpath([base_real, real]) == base_real
        except ValueError:  # different drives (Windows)
            inside = False
        if not inside:
            raise ValueError(
                "report_path must resolve inside HOTATO_MCP_REPORT_DIR "
                f"({base}); refusing to write outside it."
            )
    if os.path.exists(real):
        if os.path.isdir(real):
            raise ValueError(
                f"report_path {report_path!r} is a directory; pass a file path."
            )
        try:
            with open(real, "r", encoding="utf-8", errors="ignore") as fh:
                head = fh.read(4096)
        except OSError as exc:
            raise ValueError(
                f"report_path {report_path!r} already exists and cannot be "
                f"inspected before overwrite ({exc})."
            ) from exc
        if "hotato" not in head.lower():
            raise ValueError(
                f"report_path {report_path!r} already exists and is not a "
                "hotato-produced report; refusing to overwrite it. Choose a new "
                "path (ideally inside HOTATO_MCP_REPORT_DIR)."
            )
    return report_path


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
    report_path: Optional[str] = None,
) -> dict:
    """The single MCP tool. Returns the success envelope, or the SAME structured
    error object the CLI emits (schema/error.v1.json) for a bad input, so the
    model parses one shape for the whole call lifecycle.

    Every expected failure (a missing / mono / mismatched / not-found file, an
    unknown suite, or an ambiguous input mode) comes back as ``ok: false`` with a
    stable ``error_code`` and a message in this tool's OWN parameter vocabulary,
    never as a raw uncaught exception. An input that is well formed but carries no
    scorable event surfaces as ``error_code: not_scorable`` rather than an
    envelope whose frozen ``exit_code`` reads 0. On success the envelope is
    byte-identical to the core; ``report_path`` remains purely additive.
    """
    try:
        # Structurally enforce EXACTLY ONE input mode (the oneOf / root-validator
        # equivalent) before any file is touched, so "only caller" or "suite and
        # a recording together" is a clean structured error, not a raw throw.
        _errors.validate_input_mode(
            stereo=stereo, caller=caller, agent=agent, suite=suite
        )
        env = _run_tool_impl(
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
            report_path=report_path,
        )
    except _errors.HANDLED as exc:
        return _errors.mcp_error(exc)
    # Unusable-input parity with the CLI: an all-not-scorable single recording is
    # the CLI's exit-2 case. Surface it to the model as the shared structured
    # error (its actionable reason) instead of an envelope reading exit_code 0.
    if process_exit_code(env) == 2:
        reason = "the recording carries no scorable event."
        events = env.get("events") or []
        if events and events[0].get("not_scorable_reason"):
            reason = events[0]["not_scorable_reason"]
        return _errors.error_object("not_scorable", _errors.rewrite_flags(reason))
    return env


def _run_tool_impl(
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
    report_path: Optional[str] = None,
) -> dict:
    """Score and return the JSON envelope (no error handling; see ``_run_tool``).

    With ``report_path`` set it also writes the self-contained HTML report
    there and adds ``report_path`` (absolute) to the envelope. Scoring is
    deterministic, so the envelope core is byte-identical either way.
    """
    if report_path:
        from . import report as _report

        _guard_report_path(report_path)
        if suite:
            env = _report.write_report(report_path, suite=suite, stack=stack)
        else:
            env = _report.write_report(
                report_path,
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
        env["report_path"] = os.path.abspath(report_path)
        return env
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
        report_path: Optional[str] = None,
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
            report_path=report_path,
        )

    return server


def main(argv=None) -> int:
    server = build_server()
    server.run()  # stdio transport by default
    return 0


if __name__ == "__main__":
    sys.exit(main())
