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
Offline turn-taking analysis and regression evidence for dual-channel
voice-agent recordings, built from your own call recordings. Score a call
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
  * It does NOT do: speaker identification (a diarizer assigns anonymous
    SPEAKER_00/01; it never says who a person is), transcription, or emotion
    detection, and it makes no claim about any vendor's internal accuracy. A
    single-channel (mono) recording is scorable via the opt-in, quality-gated
    diarization front-end, labeled indicative below the confidence bar.
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
    import tempfile

    real = os.path.realpath(os.path.expanduser(report_path))
    base = os.environ.get("HOTATO_MCP_REPORT_DIR", "").strip()
    if base:
        base_real = os.path.realpath(os.path.expanduser(base))
        base_label = f"HOTATO_MCP_REPORT_DIR ({base})"
    else:
        # SANDBOX BY DEFAULT. Without an explicit HOTATO_MCP_REPORT_DIR the write
        # is still confined -- to the OS temp directory -- so an agent (or
        # untrusted content steering it) can never make this tool drop an HTML
        # file at an arbitrary sensitive path (~/.ssh/authorized_keys, a source
        # file, a shell rc, /etc/...). Operators who want reports elsewhere set
        # HOTATO_MCP_REPORT_DIR explicitly.
        base_real = os.path.realpath(tempfile.gettempdir())
        base_label = (
            f"the OS temp directory ({tempfile.gettempdir()}); set "
            "HOTATO_MCP_REPORT_DIR to write reports elsewhere"
        )
    try:
        inside = os.path.commonpath([base_real, real]) == base_real
    except ValueError:  # different drives (Windows)
        inside = False
    if not inside:
        raise ValueError(
            f"report_path must resolve inside {base_label}; refusing to write "
            "outside it."
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


def _guard_input_path(path: str, param: str) -> str:
    """Sandbox an MCP-supplied INPUT recording path (``stereo`` / ``caller`` /
    ``agent``) before it is opened.

    Same threat model as :func:`_guard_report_path`, but for READS: this tool is
    driven by an LLM that may be acting on untrusted content carrying an injected
    'score /some/other/tenant/call.wav' instruction. A read is a disclosure
    primitive -- scoring an arbitrary readable 2-channel WAV reveals exactly when
    each party spoke, and with ``report_path`` emits a full HTML timeline of it.
    So input paths are confined, mirroring ``report_path``:

      * when ``HOTATO_MCP_INPUT_DIR`` is set, the resolved real path MUST stay
        inside it (no absolute escape, no ``..`` traversal);
      * otherwise it fails CLOSED to a small default allowlist -- the OS temp
        directory, the server's working directory, and hotato's OWN bundled
        fixtures -- never an arbitrary absolute path anywhere on the host.

    Raises ValueError (surfaced as the shared structured error) on refusal."""
    import tempfile

    real = os.path.realpath(os.path.expanduser(path))
    base = os.environ.get("HOTATO_MCP_INPUT_DIR", "").strip()
    if base:
        roots = [os.path.realpath(os.path.expanduser(base))]
        label = f"HOTATO_MCP_INPUT_DIR ({base})"
    else:
        roots = [
            os.path.realpath(tempfile.gettempdir()),
            os.path.realpath(os.getcwd()),
        ]
        try:  # hotato's own bundled fixtures (read-only shipped demo audio)
            from importlib import resources

            roots.append(os.path.realpath(
                str(resources.files("hotato").joinpath("data"))))
        except Exception:  # pragma: no cover - resources always present in prod
            pass
        label = (
            "the OS temp directory, the server working directory, or hotato's "
            "bundled fixtures; set HOTATO_MCP_INPUT_DIR to read recordings from "
            "another directory"
        )
    inside = False
    for r in roots:
        try:
            if os.path.commonpath([r, real]) == r:
                inside = True
                break
        except ValueError:  # different drives (Windows)
            continue
    if not inside:
        raise ValueError(
            f"{param} must resolve inside {label}; refusing to read {path!r}. "
            "This sandbox stops an MCP caller (or untrusted content steering it) "
            "from scoring an arbitrary file on the host."
        )
    return path


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
        # Sandbox every INPUT recording path the same way report_path is
        # sandboxed: an LLM tool-caller (or untrusted content steering it) must
        # not be able to score an arbitrary file anywhere on the host.
        for _param, _val in (("stereo", stereo), ("caller", caller),
                             ("agent", agent)):
            if _val:
                _guard_input_path(_val, _param)
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


# --- agent-native fleet tools (read / verify / propose; NO production mutation) ---
# Every response carries an evidence/refusal status and names any irreversible
# action that remains PENDING a human. An MCP caller (an LLM agent, possibly
# steered by untrusted content) can inspect and PROPOSE, never deploy.

def mcp_fleet_status(home: Optional[str] = None, workspace_id: str = "default") -> dict:
    from .fleet.api import FleetAPI
    from .fleet.registry import DEFAULT_HOME
    api = FleetAPI(home=home or DEFAULT_HOME)
    try:
        return {"tool": "hotato", "kind": "fleet_status", **api.status(workspace_id)}
    finally:
        api.close()


def mcp_candidate_list(home: Optional[str] = None, workspace_id: str = "default",
                       agent_id: Optional[str] = None, limit: int = 10) -> dict:
    from .fleet.api import FleetAPI
    from .fleet.registry import DEFAULT_HOME
    api = FleetAPI(home=home or DEFAULT_HOME)
    try:
        cands = api.review_queue(workspace_id, agent_id=agent_id, limit=limit)
        return {"tool": "hotato", "kind": "candidate_list",
                "candidates": cands, "count": len(cands),
                "note": "candidate MOMENTS, not labelled failures; a human must label."}
    finally:
        api.close()


def mcp_contract_list(home: Optional[str] = None, workspace_id: str = "default") -> dict:
    from .fleet.api import FleetAPI
    from .fleet.registry import DEFAULT_HOME
    api = FleetAPI(home=home or DEFAULT_HOME)
    try:
        rows = api.registry._all(
            "SELECT contract_id, agent_id, policy_hash, canonical_digest, high_stakes "
            "FROM contracts WHERE workspace_id=? ORDER BY created_at DESC", (workspace_id,))
        return {"tool": "hotato", "kind": "contract_list",
                "contracts": rows, "count": len(rows)}
    finally:
        api.close()


def mcp_trial_explain(home: Optional[str] = None, workspace_id: str = "default",
                      trial_id: str = "") -> dict:
    from .fleet.api import FleetAPI
    from .fleet.registry import DEFAULT_HOME
    api = FleetAPI(home=home or DEFAULT_HOME)
    try:
        row = api.registry._one(
            "SELECT * FROM trials WHERE workspace_id=? AND trial_id=?",
            (workspace_id, trial_id))
        dec = api.registry._one(
            "SELECT recommendation, approved FROM decisions WHERE workspace_id=? AND trial_id=?",
            (workspace_id, trial_id))
        if row is None:
            return {"tool": "hotato", "kind": "trial_explain", "found": False}
        row = dict(row)
        return {"tool": "hotato", "kind": "trial_explain", "found": True,
                "trial": row, "verdict": row.get("verdict"),
                "evidence_tier": row.get("evidence_tier"),
                "recommendation": (dict(dec).get("recommendation") if dec else None),
                "pending_irreversible_action": (
                    "deployment approval (human-gated)" if row.get("verdict") == "improved"
                    else None)}
    finally:
        api.close()


def mcp_artifact_verify(report_path: str) -> dict:
    """Verify a contract bundle's authenticity + evidence WITHOUT trusting it.
    Read-only; recomputes the canonical digest and reports the authenticity axis."""
    safe = _guard_input_path(report_path, "report_path")
    from . import contract as _contract
    import os as _os
    target = _os.path.dirname(safe) if _os.path.isfile(safe) else safe
    try:
        v = _contract.verify_contracts(target)
    except Exception as exc:  # noqa: BLE001
        return {"tool": "hotato", "kind": "artifact_verify", "ok": False, "error": str(exc)}
    results = v.get("results", [])
    first = results[0] if results else {}
    return {"tool": "hotato", "kind": "artifact_verify", "ok": True,
            "authenticity": first.get("authenticity"),
            "authenticated": first.get("authenticated"),
            "passed": first.get("passed"),
            "summary": v.get("summary"),
            "note": "unsigned bundles are internally consistent, NOT authenticated."}


def mcp_experiment_propose(home: Optional[str] = None, workspace_id: str = "default",
                           agent_id: str = "", contract_id: str = "",
                           parameter: str = "interrupt_sensitivity") -> dict:
    """Propose a BOUNDED variant set (baseline + one lower + one higher step) with
    expected directional effects. Read-only: it does NOT clone, apply, or deploy."""
    variants = [
        {"variant": "baseline", "delta": {}, "expected": "current behavior (control)"},
        {"variant": "lower_one_step", "delta": {parameter: "-1 documented step"},
         "expected": "faster yield on true interruptions; higher false-stop risk on backchannels"},
        {"variant": "higher_one_step", "delta": {parameter: "+1 documented step"},
         "expected": "fewer false stops; slower yield on true interruptions"},
    ]
    return {"tool": "hotato", "kind": "experiment_propose",
            "workspace_id": workspace_id, "agent_id": agent_id,
            "contract_id": contract_id, "parameter": parameter,
            "variants": variants,
            "pending_irreversible_action": None,
            "note": ("a proposal only; run under a pinned trial manifest with a "
                     "fresh recapture. Production deployment stays human-gated.")}


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

    @server.tool(name="fleet_status", description="Read the local fleet workspace rollup (counts + jobs). Read-only.")
    def fleet_status(home: Optional[str] = None, workspace_id: str = "default") -> dict:
        return mcp_fleet_status(home, workspace_id)

    @server.tool(name="candidate_list", description="List top candidate moments awaiting human review. Read-only; never labels.")
    def candidate_list(home: Optional[str] = None, workspace_id: str = "default",
                       agent_id: Optional[str] = None, limit: int = 10) -> dict:
        return mcp_candidate_list(home, workspace_id, agent_id, limit)

    @server.tool(name="contract_list", description="List contracts in a workspace. Read-only.")
    def contract_list(home: Optional[str] = None, workspace_id: str = "default") -> dict:
        return mcp_contract_list(home, workspace_id)

    @server.tool(name="trial_explain", description="Explain a recorded trial's verdict, evidence tier, recommendation, and any pending human-gated action. Read-only.")
    def trial_explain(home: Optional[str] = None, workspace_id: str = "default",
                      trial_id: str = "") -> dict:
        return mcp_trial_explain(home, workspace_id, trial_id)

    @server.tool(name="artifact_verify", description="Verify a contract bundle's authenticity + evidence without trusting it. Read-only.")
    def artifact_verify(report_path: str) -> dict:
        return mcp_artifact_verify(report_path)

    @server.tool(name="experiment_propose", description="Propose a bounded variant set with expected effects. Read-only; does not clone, apply, or deploy.")
    def experiment_propose(home: Optional[str] = None, workspace_id: str = "default",
                           agent_id: str = "", contract_id: str = "",
                           parameter: str = "interrupt_sensitivity") -> dict:
        return mcp_experiment_propose(home, workspace_id, agent_id, contract_id, parameter)

    return server


def main(argv=None) -> int:
    server = build_server()
    server.run()  # stdio transport by default
    return 0


if __name__ == "__main__":
    sys.exit(main())
