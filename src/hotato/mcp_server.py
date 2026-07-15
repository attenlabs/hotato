"""MCP server exposing the hotato evaluation and its local fleet control plane.

Run it (zero-install) with the MCP extra:

    uvx --from "hotato[mcp]" hotato-mcp

or, if installed:

    python -m hotato.mcp_server

It speaks MCP over stdio and exposes a local tool set: one scoring tool,
``voice_eval_run`` (whose schema states the scope and ceiling and which
returns the same JSON envelope as the CLI), counterexample compile/verify/
reproduce tools, plus fleet tools. Fleet reads
read/verify/propose over a local fleet workspace (``fleet_status``,
``candidate_list``, ``candidate_inspect``, ``contract_list``, ``trial_explain``,
``experiment_status``, ``artifact_verify``, ``experiment_propose``); three are
clone-scoped actions that recompute, never deploy (``experiment_create``,
``experiment_run``, ``clone_cleanup``). None of them deploys to production; the
deployment approval always stays a human gate. Everything runs locally; no audio
leaves the machine.

Every tool response carries a uniform control envelope (plan §17): four keys ride
on EVERY response -- ``evidence_status`` (or null for a pure read that carries no
verdict), ``refusal_reason`` (or null), ``artifact_digests`` (a list, or []), and
``pending_irreversible_action`` (the exact human-gated action still pending, e.g.
deployment approval, or null) -- so an autonomous caller parses one shape.
"""

from __future__ import annotations

import os
import sys
from typing import Optional

from . import errors as _errors
from . import evidence as _evidence
from .core import LIMITS, SUITE_ID, process_exit_code, run_single, run_suite
from .errors import open_regular as _open_regular

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

TRANSCRIPT (optional, opt-in, default off): set `transcribe=true` on a single
recording (`stereo`, or `caller`+`agent`) to attach a plain-text transcript as
CONTEXT -- so a reader can see WHAT was said next to WHEN the timing engine
says it was said. This requires the optional `[transcribe]` extra
(faster-whisper); when it is not installed, the call cleanly refuses
(`error_code: "backend_unavailable"`, naming `pip install 'hotato[transcribe]'`)
instead of crashing. A transcript is an aid, never an accuracy improvement: it
NEVER changes did_yield / seconds_to_yield / talk_over_sec or any other
timing/verdict field -- the energy VAD stays the pinned reference, and running
the same recording with `transcribe=true` produces byte-identical timing
numbers to the same run without it. Not yet supported with `suite`. On success
the envelope additionally carries a top-level `transcript` block (text,
segments, model/device provenance) and each event gains a `transcript_context`
key (the overlapping transcript text for that event's window); both are purely
additive. Every transcription is content-addressed and cached (a repeat call
over the same audio+settings replays byte-identical and skips the model,
recorded on `transcript.cache`); set `transcribe_no_cache=true` to force a
fresh re-transcription that DIFFS against any cached baseline and surfaces
drift, never silently overwriting a mismatch.

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
            with _open_regular(real, "r", encoding="utf-8", errors="ignore") as fh:
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


def _counterexample_roots() -> list:
    """Explicit roots available to counterexample MCP reads/writes."""
    import tempfile

    roots = []
    for variable in ("HOTATO_MCP_INPUT_DIR", "HOTATO_MCP_REPORT_DIR"):
        value = os.environ.get(variable, "").strip()
        if value:
            roots.append(os.path.realpath(os.path.expanduser(value)))
    if not roots:
        roots.extend([
            os.path.realpath(tempfile.gettempdir()),
            os.path.realpath(os.getcwd()),
        ])
    return sorted(set(roots))


def _guard_counterexample_path(path: str, param: str, *, must_be_new: bool = False) -> str:
    """Confine an MCP counterexample directory to configured local roots."""
    real = os.path.realpath(os.path.expanduser(path))
    roots = _counterexample_roots()
    inside = False
    for root in roots:
        try:
            if os.path.commonpath([root, real]) == root:
                inside = True
                break
        except ValueError:
            continue
    if not inside:
        raise ValueError(
            f"{param} must resolve inside HOTATO_MCP_INPUT_DIR or "
            "HOTATO_MCP_REPORT_DIR (the OS temp directory/current working "
            "directory when neither is configured)."
        )
    if must_be_new and os.path.lexists(real):
        raise ValueError(f"{param} {path!r} already exists; counterexample compilation never overwrites")
    return real


# The little slack (seconds) padded onto each side of an event's onset/yield
# window before overlapping it against transcript segments -- see
# _event_window. Purely a context-surfacing convenience; it plays no part in
# any timing/verdict computation.
_TRANSCRIPT_CONTEXT_PAD_SEC = 1.0

_TRANSCRIPT_NOTE = (
    "optional CONTEXT only: text next to a timestamp for a human/agent reading "
    "the report. It is NEVER used to compute did_yield, seconds_to_yield, "
    "talk_over_sec, or any other timing/verdict field -- the energy VAD stays "
    "the pinned reference, and this makes no accuracy claim of its own."
)


def _event_window(event: dict) -> dict:
    """Build a ``{start_sec, end_sec}`` (or ``{}``) window for one scored event,
    for :func:`hotato.transcribe.align_transcript_to_events` to overlap against.

    Real envelope events carry their timing nested (``measurements.
    caller_onset_sec``, ``verdict.seconds_to_yield``), not the top-level
    ``start_sec``/``end_sec``/``time_sec`` shape that function expects (that
    convention belongs to ``trace.py`` spans) -- so this adapts one shape to the
    other. READ-ONLY: it only reads ``measurements``/``verdict``, never writes
    them, and returns a brand-new dict the real event is never mutated through.

    The window is padded by :data:`_TRANSCRIPT_CONTEXT_PAD_SEC` on each side (a
    little slack around the onset/yield so nearby speech is still surfaced as
    context) and clamped to a non-negative start. An event with no detected
    caller onset (e.g. a not-scorable placeholder) gets ``{}``, which
    ``align_transcript_to_events`` turns into an empty context rather than a
    crash."""
    m = event.get("measurements") or {}
    onset = m.get("caller_onset_sec")
    if onset is None:
        return {}
    v = event.get("verdict") or {}
    yield_sec = v.get("seconds_to_yield")
    end = onset + (yield_sec if yield_sec is not None else 0.0)
    pad = _TRANSCRIPT_CONTEXT_PAD_SEC
    return {"start_sec": max(0.0, onset - pad), "end_sec": end + pad}


def _attach_transcript(
    env: dict,
    *,
    stereo: Optional[str],
    caller: Optional[str],
    agent: Optional[str],
    suite: Optional[str],
    no_cache: bool = False,
) -> dict:
    """Attach a transcript as pure, additive CONTEXT to an already-scored
    envelope. Never called for ``suite`` (raises a clean ``ValueError`` instead
    -- per-scenario transcript attachment is not implemented yet, and staying
    silent about a requested-but-missing transcript would be dishonest).

    Lazily imports :mod:`hotato.transcribe` (which itself only imports
    faster-whisper inside its own ``transcribe()``, never at module import
    time), so requesting ``transcribe=False`` (the default) costs nothing and
    never touches the optional extra. Absent the extra, the underlying
    ``transcribe()`` call raises ``BackendUnavailable`` (in ``errors.HANDLED``),
    which the caller (:func:`_run_tool`) turns into the SAME clean refusal
    envelope every other optional-extra failure uses -- never a crash.

    Every file is routed through ``hotato.transcribe.transcribe_cached`` (the
    content-addressed cache mirroring ``hotato.rubric.VerdictCache``): a cache
    hit replays a byte-identical transcript and skips the model. The DEFAULT
    ``~/.hotato/transcribe-cache`` location gracefully degrades to no caching
    (with a ``cache_note`` on the response, never a crash) when unwritable.
    ``no_cache=True`` re-transcribes fresh and surfaces drift against any
    cached baseline; advisory provenance only, never a gate.

    Returns a NEW envelope dict: the input ``env`` is not mutated. Every
    existing key (timing, verdict, summary, funnel, ...) passes through
    unchanged; only two keys are added -- a top-level ``transcript`` block and,
    on each event, a ``transcript_context`` key -- so this is additive with
    respect to ``schema/envelope.v1.json``."""
    if suite:
        raise ValueError(
            "transcribe currently supports only a single recording (stereo, or "
            "caller+agent); it does not yet attach a per-scenario transcript "
            "for suite. Omit transcribe=true, or drop suite and pass "
            "stereo (or caller+agent) instead."
        )
    from . import transcribe as _transcribe

    cache, cache_warning = _transcribe.build_transcript_cache()

    tagged_segments = []
    cache_provenance = []
    if stereo:
        r = _transcribe.transcribe_cached(stereo, cache=cache, no_cache=no_cache)
        t = r.transcript
        cache_provenance.append({
            "role": "stereo", "cache_key": r.cache_key, "cached": r.cached,
            "drift": r.drift,
        })
        for seg in t.segments:
            tagged_segments.append((seg.start, seg.end, seg.text, None))
        language, model, device, compute_type = (
            t.language, t.model, t.device, t.compute_type,
        )
    else:
        rc = _transcribe.transcribe_cached(caller, cache=cache, no_cache=no_cache)
        ra = _transcribe.transcribe_cached(agent, cache=cache, no_cache=no_cache)
        tc, ta = rc.transcript, ra.transcript
        cache_provenance.append({
            "role": "caller", "cache_key": rc.cache_key, "cached": rc.cached,
            "drift": rc.drift,
        })
        cache_provenance.append({
            "role": "agent", "cache_key": ra.cache_key, "cached": ra.cached,
            "drift": ra.drift,
        })
        for seg in tc.segments:
            tagged_segments.append((seg.start, seg.end, seg.text, "caller"))
        for seg in ta.segments:
            tagged_segments.append((seg.start, seg.end, seg.text, "agent"))
        tagged_segments.sort(key=lambda s: s[0])
        language = tc.language or ta.language
        model, device, compute_type = tc.model, tc.device, tc.compute_type

    transcript = _transcribe.Transcript(
        text=" ".join(s[2] for s in tagged_segments if s[2]).strip(),
        segments=[
            _transcribe.TranscriptSegment(start=s[0], end=s[1], text=s[2])
            for s in tagged_segments
        ],
        language=language, model=model, device=device, compute_type=compute_type,
    )

    events = env.get("events") or []
    windows = [_event_window(e) for e in events]
    aligned = _transcribe.align_transcript_to_events(transcript, windows)
    new_events = []
    for e, a in zip(events, aligned):
        ne = dict(e)
        ne["transcript_context"] = a["transcript_context"]
        new_events.append(ne)

    new_env = dict(env)
    new_env["events"] = new_events
    new_env["transcript"] = {
        "text": transcript.text,
        "language": transcript.language,
        "model": transcript.model,
        "device": transcript.device,
        "compute_type": transcript.compute_type,
        "segments": [
            {"start": s[0], "end": s[1], "text": s[2], "role": s[3]}
            for s in tagged_segments
        ],
        "note": _TRANSCRIPT_NOTE,
        "cache": cache_provenance,
        "cache_note": cache_warning,
    }
    return new_env


def _scoring_digests(env: dict) -> list:
    """The content-addressed artifacts a scoring run touched: each scored
    recording's audio-provenance ``sha256`` (the file-identity digest already in
    the envelope). One per event, so a single recording yields one and a suite
    yields one per fixture. Empty only if the envelope carries no provenance."""
    digests = []
    for event in env.get("events") or []:
        prov = event.get("audio_provenance") or {}
        digest = prov.get("sha256")
        if digest:
            digests.append(digest)
    return digests


def _control_error(obj: dict) -> dict:
    """Give a structured MCP error object the SAME uniform control envelope the
    success path and every fleet tool carry, so an autonomous caller parses one
    shape for results AND errors. ``refusal_reason`` mirrors the human-readable
    ``message`` (why the request was declined); a refused call produced no
    verdict (``evidence_status`` None), touched no artifact (``artifact_digests``
    []), and leaves nothing human-gated pending."""
    return _envelope(obj, refusal_reason=obj.get("message"))


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
    transcribe: bool = False,
    transcribe_no_cache: bool = False,
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

    ``transcribe`` (opt-in, default off) attaches a transcript as CONTEXT ONLY
    (see :func:`_attach_transcript`); it never changes any timing/verdict field,
    and a missing ``[transcribe]`` extra is the same clean refusal envelope as
    any other optional-extra failure, never a crash. Every transcription is
    routed through the content-addressed transcript cache (a repeat call over
    the same audio+settings replays byte-identical and skips the model);
    ``transcribe_no_cache=True`` re-transcribes fresh and surfaces drift
    against any cached baseline on ``transcript.cache`` (mirrors the CLI's
    ``--no-transcribe-cache``), never silently overwriting a mismatch.
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
        if transcribe:
            env = _attach_transcript(
                env, stereo=stereo, caller=caller, agent=agent, suite=suite,
                no_cache=transcribe_no_cache,
            )
    except _errors.HANDLED as exc:
        return _control_error(_errors.mcp_error(exc))
    # Unusable-input parity with the CLI: an all-not-scorable single recording is
    # the CLI's exit-2 case. Surface it to the model as the shared structured
    # error (its actionable reason) instead of an envelope reading exit_code 0.
    if process_exit_code(env) == 2:
        reason = "the recording carries no scorable event."
        events = env.get("events") or []
        if events and events[0].get("not_scorable_reason"):
            reason = events[0]["not_scorable_reason"]
        return _control_error(
            _errors.error_object("not_scorable", _errors.rewrite_flags(reason)))
    # The scoring success envelope carries the SAME uniform control envelope as
    # every fleet tool. A real scored recording is evidence TIER_MEASURED
    # (recomputed from audio); the touched content-addressed artifacts are each
    # scored recording's audio provenance digest; a pure score has nothing
    # human-gated pending. The four keys are additive -- the envelope CORE stays
    # byte-identical to the CLI/core (tests pop the control keys before compare).
    return _envelope(
        env,
        evidence_status=_evidence.TIER_MEASURED,
        artifact_digests=_scoring_digests(env),
    )


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

def _envelope(payload: dict, *, evidence_status=None, refusal_reason=None,
              artifact_digests=None, pending_irreversible_action=None) -> dict:
    """Give every MCP tool response the uniform plan-§17 control envelope.

    Four keys ride on EVERY response (pure reads included): ``evidence_status``
    (an evidence tier / authenticity axis, or None for a pure read with no
    verdict), ``refusal_reason`` (why a request was declined, or None),
    ``artifact_digests`` (the content-addressed digests this response touched, or
    []), and ``pending_irreversible_action`` (the exact human-gated action that
    still has to happen, e.g. deployment approval, or None).

    A value ALREADY on the payload wins -- a tool that computed a real verdict or
    named its own pending gate keeps it -- so this only fills a key that is absent.
    An autonomous caller can then parse one shape for the whole tool surface."""
    payload.setdefault("evidence_status", evidence_status)
    payload.setdefault("refusal_reason", refusal_reason)
    payload.setdefault("artifact_digests",
                       list(artifact_digests) if artifact_digests else [])
    payload.setdefault("pending_irreversible_action", pending_irreversible_action)
    return payload


def _guarded(fn, *args, **kwargs):
    """Absolute choke point for the uniform control envelope (module docstring).

    Run a tool body and convert ANY escaping exception into the SAME structured
    ``ok: false`` control envelope every tool promises, so no MCP tool can ever
    surface a raw uncaught exception to the caller. An expected failure class
    (bad input, a missing / oversized / mismatched recording, an unavailable
    optional backend -- :data:`errors.HANDLED`) is mapped through
    :func:`errors.mcp_error` to its stable, schema-enumerated ``error_code``;
    anything else (a backend adapter, registry, or SQLite layer raising an
    unlisted type such as ``RuntimeError``/``KeyError``/``sqlite3.OperationalError``)
    becomes the schema's catch-all ``usage_error`` envelope carrying the failure
    text, rather than escaping. Every returned envelope is the same
    :func:`_control_error` shape (``ok: false``, ``exit_code`` 2, the four control
    keys, ``refusal_reason`` mirroring the message): the call fails SAFE and
    uniform, never a fabricated passing verdict. Wrapping every registration
    body here is defense in depth over the tools that already guard their own
    body, and it is drift-proof for any tool added later."""
    try:
        return fn(*args, **kwargs)
    except _errors.HANDLED as exc:
        return _control_error(_errors.mcp_error(exc))
    except Exception as exc:  # noqa: BLE001 - the envelope contract is absolute
        # An unexpected, unlisted exception: keep the promise anyway. The schema
        # enum has no "internal" slug, so use its designated catch-all
        # (usage_error) with the exception's own text as the message.
        return _control_error(_errors.error_object("usage_error", str(exc)))


def mcp_fleet_status(home: Optional[str] = None, workspace_id: str = "default") -> dict:
    from .fleet.api import FleetAPI
    from .fleet.registry import DEFAULT_HOME
    api = FleetAPI(home=home or DEFAULT_HOME)
    try:
        return _envelope({"tool": "hotato", "kind": "fleet_status",
                          **api.status(workspace_id)})
    finally:
        api.close()


def mcp_candidate_list(home: Optional[str] = None, workspace_id: str = "default",
                       agent_id: Optional[str] = None, limit: int = 10) -> dict:
    from .fleet.api import FleetAPI
    from .fleet.registry import DEFAULT_HOME
    api = FleetAPI(home=home or DEFAULT_HOME)
    try:
        cands = api.review_queue(workspace_id, agent_id=agent_id, limit=limit)
        return _envelope({"tool": "hotato", "kind": "candidate_list",
                          "candidates": cands, "count": len(cands),
                          "note": "candidate MOMENTS, not labelled failures; a human must label."})
    finally:
        api.close()


def mcp_candidate_inspect(home: Optional[str] = None, workspace_id: str = "default",
                          candidate_id: str = "", agent_id: Optional[str] = None) -> dict:
    """Single-candidate detail: onset, the stored measured components
    (severity / input_health / recurrence / novelty / covered_by_contract), and
    the trust findings recorded at discovery. Read-only; NEVER labels -- a
    candidate stays an unlabelled MOMENT until a human decides."""
    import json as _json

    from .fleet.api import FleetAPI
    from .fleet.registry import DEFAULT_HOME
    api = FleetAPI(home=home or DEFAULT_HOME)
    try:
        # Query the registry the SAME way candidate_list does (review_queue),
        # then select the requested candidate -- no raw-row escape hatch, so this
        # is scoped to exactly the workspace/agent candidate_list can see.
        rows = api.review_queue(workspace_id, agent_id=agent_id, limit=100000)
    finally:
        api.close()
    match = next((r for r in rows if r.get("candidate_id") == candidate_id), None)
    if match is None:
        return _envelope({"tool": "hotato", "kind": "candidate_inspect",
                          "found": False, "candidate_id": candidate_id})
    try:
        measured = _json.loads(match.get("measured_json") or "{}")
    except (ValueError, TypeError):
        measured = {}
    components = measured.get("components") or {}
    # Trust findings, all from stored data (this is a pure read; the audio is not
    # re-opened): the input health recorded by the discover-time trust preflight,
    # plus any echo caveat (an echo-correlated candidate's "caller" energy may be
    # leaked agent TTS, not a real interruption).
    trust = {"input_health": components.get("input_health"), "echo_caveat": None}
    if measured.get("kind") == "echo_correlated_activity":
        ar = measured.get("agent_reaction") or {}
        trust["echo_caveat"] = {
            "coherence": ar.get("coherence"),
            "echo_suspected": ar.get("echo_suspected"),
            "note": "caller energy may be leaked agent TTS, not a real interruption."}
    onset = match.get("onset_sec")
    if onset is None:
        onset = measured.get("t_sec")
    return _envelope({
        "tool": "hotato", "kind": "candidate_inspect", "found": True,
        "candidate_id": candidate_id,
        "onset_sec": onset,
        "cluster": match.get("cluster"),
        "status": match.get("status"),
        "components": {
            "severity": components.get("severity"),
            "input_health": components.get("input_health"),
            "recurrence": components.get("recurrence"),
            "novelty": components.get("novelty"),
            "covered_by_contract": components.get("covered_by_contract"),
        },
        "trust": trust,
        "durations": measured.get("durations"),
        "note": "a candidate MOMENT, not a labelled failure; a human must label."})


def mcp_contract_list(home: Optional[str] = None, workspace_id: str = "default") -> dict:
    from .fleet.api import FleetAPI
    from .fleet.registry import DEFAULT_HOME
    api = FleetAPI(home=home or DEFAULT_HOME)
    try:
        rows = api.registry._all(
            "SELECT contract_id, agent_id, policy_hash, canonical_digest, high_stakes "
            "FROM contracts WHERE workspace_id=? ORDER BY created_at DESC", (workspace_id,))
        return _envelope({"tool": "hotato", "kind": "contract_list",
                          "contracts": rows, "count": len(rows)},
                         artifact_digests=[r["canonical_digest"] for r in rows
                                           if r.get("canonical_digest")])
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
            return _envelope({"tool": "hotato", "kind": "trial_explain", "found": False})
        row = dict(row)
        return _envelope(
            {"tool": "hotato", "kind": "trial_explain", "found": True,
             "trial": row, "verdict": row.get("verdict"),
             "evidence_tier": row.get("evidence_tier"),
             "recommendation": (dict(dec).get("recommendation") if dec else None),
             "pending_irreversible_action": (
                 "deployment approval (human-gated)" if row.get("verdict") == "improved"
                 else None)},
            evidence_status=row.get("evidence_tier"),
            artifact_digests=([row["manifest_digest"]] if row.get("manifest_digest") else []))
    finally:
        api.close()


def mcp_experiment_status(home: Optional[str] = None, workspace_id: str = "default",
                          trial_id: str = "") -> dict:
    """A trial's CURRENT state: verdict, evidence tier, recommendation, and
    manifest hash from the trials + decisions tables, plus any pending
    human-gated action. Read-only."""
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
    finally:
        api.close()
    if row is None:
        return _envelope({"tool": "hotato", "kind": "experiment_status",
                          "found": False, "trial_id": trial_id})
    row = dict(row)
    dec = dict(dec) if dec else None
    verdict = row.get("verdict")
    return _envelope(
        {"tool": "hotato", "kind": "experiment_status", "found": True,
         "trial_id": trial_id, "verdict": verdict,
         "evidence_tier": row.get("evidence_tier"),
         "manifest_hash": row.get("manifest_hash"),
         "recommendation": (dec.get("recommendation") if dec else None),
         "approved": (dec.get("approved") if dec else None)},
        evidence_status=row.get("evidence_tier"),
        artifact_digests=([row["manifest_digest"]] if row.get("manifest_digest") else []),
        pending_irreversible_action=(
            "deployment approval (human-gated)" if verdict == "improved" else None))


def mcp_artifact_verify(report_path: str) -> dict:
    """Verify a contract bundle's authenticity + evidence WITHOUT trusting it.
    Read-only; recomputes the canonical digest and reports the authenticity axis."""
    safe = _guard_input_path(report_path, "report_path")
    import os as _os

    from . import contract as _contract
    target = _os.path.dirname(safe) if _os.path.isfile(safe) else safe
    try:
        v = _contract.verify_contracts(target)
    except Exception as exc:  # noqa: BLE001
        return _envelope({"tool": "hotato", "kind": "artifact_verify", "ok": False,
                          "error": str(exc)}, refusal_reason=str(exc))
    results = v.get("results", [])
    first = results[0] if results else {}
    return _envelope(
        {"tool": "hotato", "kind": "artifact_verify", "ok": True,
         "authenticity": first.get("authenticity"),
         "authenticated": first.get("authenticated"),
         "passed": first.get("passed"),
         "summary": v.get("summary"),
         "note": "unsigned bundles are internally consistent, NOT authenticated."},
        evidence_status=first.get("authenticity"),
        artifact_digests=([first["canonical_digest"]] if first.get("canonical_digest") else []))


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
    return _envelope(
        {"tool": "hotato", "kind": "experiment_propose",
         "workspace_id": workspace_id, "agent_id": agent_id,
         "contract_id": contract_id, "parameter": parameter,
         "variants": variants,
         "pending_irreversible_action": None,
         "note": ("a proposal only; run under a pinned trial manifest with a "
                  "fresh recapture. Production deployment stays human-gated.")})


def mcp_experiment_run(home: Optional[str] = None, workspace_id: str = "default",
                      agent_id: str = "", trial_id: str = "",
                      battery_path: str = "", before_path: str = "",
                      after_path: str = "", min_n: int = 1) -> dict:
    """Clone-scoped action: recompute a before/after trial (offline, no network,
    no production mutation) and record a recommendation. Never deploys."""
    import json as _json
    import os as _os

    from .fleet.api import FleetAPI
    from .fleet.registry import DEFAULT_HOME

    def _load(path):
        safe = _guard_input_path(path, "path")
        if _os.path.isdir(safe):
            safe = _os.path.join(safe, "run.json")
        return _json.load(_open_regular(safe, "r", encoding="utf-8"))

    try:
        before_env = _load(before_path)
        after_env = _load(after_path)
        battery_env = _load(battery_path) if battery_path else before_env
    except Exception as exc:  # noqa: BLE001
        return _envelope({"tool": "hotato", "kind": "experiment_run", "ok": False,
                          "error": str(exc)}, refusal_reason=str(exc))
    before_dir = before_path if _os.path.isdir(before_path) else _os.path.dirname(before_path)
    after_dir = after_path if _os.path.isdir(after_path) else _os.path.dirname(after_path)
    api = FleetAPI(home=home or DEFAULT_HOME)
    try:
        res = api.experiment_run(workspace_id, agent_id, trial_id=trial_id,
                                 battery_env=battery_env, before_env=before_env,
                                 before_dir=before_dir, after_env=after_env,
                                 after_dir=after_dir, min_n=min_n)
    finally:
        api.close()
    refusal = res.get("refusal")
    return _envelope(
        {"tool": "hotato", "kind": "experiment_run", "ok": True,
         "verdict": res["verdict"], "evidence_tier": res["evidence_tier"],
         "recommendation": res["recommendation"], "refusal": refusal,
         "pending_irreversible_action": (
             "deployment approval (human-gated)" if res["verdict"] == "improved" else None)},
        evidence_status=res.get("evidence_tier"),
        refusal_reason=(refusal.get("reason") if isinstance(refusal, dict) else None),
        artifact_digests=([res["manifest_hash"]] if res.get("manifest_hash") else []))


def mcp_experiment_create(home: Optional[str] = None, workspace_id: str = "default",
                          agent_id: str = "", trial_id: str = "",
                          battery_path: str = "", min_n: int = 1) -> dict:
    """Clone-scoped action: PRECOMMIT a trial manifest from a committed battery
    BEFORE any after-side capture, so the pinned fixture universe is fixed ahead
    of the results and cannot be cherry-picked later. Never captures, never
    deploys. Wraps ``FleetAPI.experiment_create``; ``experiment run --manifest``
    then consumes exactly this manifest."""
    import json as _json
    import os as _os

    from .fleet.api import FleetAPI
    from .fleet.registry import DEFAULT_HOME

    def _load(path):
        safe = _guard_input_path(path, "battery_path")
        if _os.path.isdir(safe):
            safe = _os.path.join(safe, "run.json")
        return _json.load(_open_regular(safe, "r", encoding="utf-8"))

    try:
        battery_env = _load(battery_path)
    except Exception as exc:  # noqa: BLE001
        return _envelope({"tool": "hotato", "kind": "experiment_create", "ok": False,
                          "error": str(exc)}, refusal_reason=str(exc))
    api = FleetAPI(home=home or DEFAULT_HOME)
    try:
        res = api.experiment_create(workspace_id, agent_id, trial_id=trial_id,
                                    battery_env=battery_env, min_n=min_n)
    finally:
        api.close()
    return _envelope(
        {"tool": "hotato", "kind": "experiment_create", "ok": True,
         "trial_id": res["trial_id"], "manifest_hash": res["manifest_hash"],
         "manifest_digest": res["manifest_digest"], "fixtures": res["fixtures"],
         "min_n": res["min_n"], "next": res["next"]},
        evidence_status=None,
        artifact_digests=[res["manifest_digest"]],
        # A precommitted manifest carries no verdict yet, so no deployment is
        # pending. The gate arrives downstream: experiment_run may reach
        # "improved", and only then does human-gated deployment approval apply.
        pending_irreversible_action=None)


def mcp_clone_cleanup(home: Optional[str] = None, workspace_id: str = "default",
                      trial_id: str = "", receipt_id: str = "", stack: str = "mock",
                      work_dir: str = ".") -> dict:
    """Clone-scoped action: delete a STAGING clone THIS tool created, authorized by
    the DURABLE clone receipt recorded at clone-creation time and referenced by
    ``trial_id`` or ``receipt_id`` -- NEVER by an unconstrained clone id or a
    mutable provider display name. The receipt is resolved from the workspace-
    scoped registry, so an unregistered clone id (e.g. a production assistant that
    merely carries a 'hotato' prefix) has no receipt and is refused. Never touches
    production."""
    from .fleet import adapters as _ad
    from .fleet.api import FleetAPI
    from .fleet.registry import DEFAULT_HOME
    if not (trial_id or receipt_id):
        reason = ("clone_cleanup requires a governed reference: pass trial_id or "
                  "receipt_id (the durable clone receipt this tool recorded when the "
                  "staging clone was created); a raw clone id is not accepted.")
        return _envelope({"tool": "hotato", "kind": "clone_cleanup", "ok": False,
                          "error": reason}, refusal_reason=reason)
    adapter = _ad.get_adapter(stack, work_dir=work_dir)
    if not adapter.supports("delete_clone"):
        reason = f"{stack} adapter does not support delete_clone"
        return _envelope({"tool": "hotato", "kind": "clone_cleanup", "ok": False,
                          "error": reason}, refusal_reason=reason)
    api = FleetAPI(home=home or DEFAULT_HOME)
    try:
        result = api.cleanup_clone(workspace_id, adapter=adapter,
                                   receipt_id=receipt_id or None, trial_id=trial_id or None)
    except Exception as exc:  # noqa: BLE001
        return _envelope({"tool": "hotato", "kind": "clone_cleanup", "ok": False,
                          "error": str(exc)}, refusal_reason=str(exc))
    finally:
        api.close()
    return _envelope({"tool": "hotato", "kind": "clone_cleanup", "ok": True,
                      "result": result})


# --- proof-preserving counterexamples (local, sandboxed, no overwrite) -----

def mcp_counterexample_compile(
    scenario_path: str,
    test_path: str,
    target: str,
    out_dir: str,
    budget: int = 512,
    seed: Optional[int] = None,
) -> dict:
    """Compile one scripted deterministic failure inside the MCP sandboxes."""
    try:
        scenario = _guard_input_path(scenario_path, "scenario_path")
        test = _guard_input_path(test_path, "test_path")
        output = _guard_counterexample_path(out_dir, "out_dir", must_be_new=True)
        workspace = os.path.commonpath([
            os.path.dirname(os.path.realpath(scenario)),
            os.path.dirname(os.path.realpath(test)),
        ])
        from .counterexample import compile_counterexample

        result = compile_counterexample(
            scenario,
            test,
            target=target,
            out_dir=output,
            workspace=workspace,
            budget=budget,
            seed=seed,
        )
    except _errors.HANDLED as exc:
        return _control_error(_errors.mcp_error(exc))
    return _envelope(
        {"tool": "hotato", **result},
        evidence_status=_evidence.TIER_ASSERTED,
        artifact_digests=[result["counterexample_id"], result["target"]["fingerprint"]],
    )


def mcp_counterexample_verify(path: str) -> dict:
    """Audit a capsule under its recorded proof-engine implementation."""
    try:
        safe = _guard_counterexample_path(path, "path")
        from .counterexample import verify_counterexample

        result = verify_counterexample(safe)
    except _errors.HANDLED as exc:
        return _control_error(_errors.mcp_error(exc))
    return _envelope(
        {"tool": "hotato", **result},
        evidence_status=_evidence.TIER_ASSERTED,
        artifact_digests=[value for value in (
            result["counterexample_id"], result.get("failure_fingerprint")
        ) if value],
    )


def mcp_counterexample_reproduce(path: str) -> dict:
    """Check the reduced fixture under the current evaluator implementation."""
    try:
        safe = _guard_counterexample_path(path, "path")
        from .counterexample import reproduce_counterexample

        result = reproduce_counterexample(safe)
    except _errors.HANDLED as exc:
        return _control_error(_errors.mcp_error(exc))
    return _envelope(
        {"tool": "hotato", **result},
        evidence_status=_evidence.TIER_ASSERTED,
        artifact_digests=[value for value in (
            result["counterexample_id"], result.get("failure_fingerprint")
        ) if value],
    )


# --- Canonical MCP tool inventory -------------------------------------------
# The single source of truth for which tools this server registers: one scoring
# tool (``voice_eval_run``), counterexample tools, and fleet tools. ``build_server`` registers
# EXACTLY these names, docs/MCP.md lists them, and tests/test_mcp_parity.py asserts
# the registered set equals this tuple. Add a tool name here in the SAME change that
# adds its ``@server.tool`` registration -- test_expected_tools_registered fails on
# any drift between this inventory and what is actually registered.
TOOL_NAMES = (
    "voice_eval_run",
    "counterexample_compile",
    "counterexample_verify",
    "counterexample_reproduce",
    "fleet_status",
    "candidate_list",
    "candidate_inspect",
    "contract_list",
    "trial_explain",
    "experiment_status",
    "artifact_verify",
    "experiment_propose",
    "experiment_create",
    "experiment_run",
    "clone_cleanup",
)


def build_server():
    """Construct the FastMCP server with the scoring tool and the fleet tools registered."""
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
    # FastMCP does not forward an application version to its underlying low-level
    # Server, so serverInfo.version in the initialize handshake would otherwise
    # default to the MCP SDK's own version (create_initialization_options() falls
    # back to importlib version("mcp") when Server.version is None). Pin it to
    # hotato's explicit application version so the client learns which application
    # it is talking to, not the transport SDK. Verified at the floor SDK
    # (mcp==1.2.0) and current.
    from . import __version__ as _hotato_version

    # Set the application version on the low-level server when it is present.
    # The unit-test doubles (_FakeFastMCP) that run without the mcp extra do not
    # expose _mcp_server; the real SDK does, and the floor+current wire tests
    # assert the initialize payload carries hotato's version.
    _low_server = getattr(server, "_mcp_server", None)
    if _low_server is not None:
        _low_server.version = _hotato_version

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
        transcribe: bool = False,
        transcribe_no_cache: bool = False,
    ) -> dict:
        return _guarded(
            _run_tool,
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
            transcribe=transcribe,
            transcribe_no_cache=transcribe_no_cache,
        )

    @server.tool(
        name="counterexample_compile",
        description=(
            "Compile one deterministic scripted-scenario failure into a private "
            "proof-preserving .hotato-repro capsule. Offline; inputs and output "
            "are sandboxed; existing output is never replaced."
        ),
    )
    def counterexample_compile(
        scenario_path: str,
        test_path: str,
        target: str,
        out_dir: str,
        budget: int = 512,
        seed: Optional[int] = None,
    ) -> dict:
        return _guarded(
            mcp_counterexample_compile,
            scenario_path, test_path, target, out_dir, budget, seed,
        )

    @server.tool(
        name="counterexample_verify",
        description=(
            "Audit a private .hotato-repro capsule: manifest, frozen source, "
            "proof-engine provenance, delete-only chain, exact failure, and "
            "claimed local minimality. Read-only."
        ),
    )
    def counterexample_verify(path: str) -> dict:
        return _guarded(mcp_counterexample_verify, path)

    @server.tool(
        name="counterexample_reproduce",
        description=(
            "Run a private capsule's reduced fixture under the current Hotato "
            "evaluator. Permits evaluator-version drift while requiring the "
            "source-selected structured failure branch. Read-only."
        ),
    )
    def counterexample_reproduce(path: str) -> dict:
        return _guarded(mcp_counterexample_reproduce, path)

    @server.tool(name="fleet_status", description="Read the local fleet workspace rollup (counts + jobs). Read-only.")
    def fleet_status(home: Optional[str] = None, workspace_id: str = "default") -> dict:
        return _guarded(mcp_fleet_status, home, workspace_id)

    @server.tool(name="candidate_list", description="List top candidate moments awaiting human review. Read-only; never labels.")
    def candidate_list(home: Optional[str] = None, workspace_id: str = "default",
                       agent_id: Optional[str] = None, limit: int = 10) -> dict:
        return _guarded(mcp_candidate_list, home, workspace_id, agent_id, limit)

    @server.tool(name="candidate_inspect", description="Inspect one candidate moment: onset, measured components (severity/input_health/recurrence/novelty/covered_by_contract), and trust findings. Read-only; never labels.")
    def candidate_inspect(home: Optional[str] = None, workspace_id: str = "default",
                          candidate_id: str = "", agent_id: Optional[str] = None) -> dict:
        return _guarded(mcp_candidate_inspect, home, workspace_id, candidate_id, agent_id)

    @server.tool(name="contract_list", description="List contracts in a workspace. Read-only.")
    def contract_list(home: Optional[str] = None, workspace_id: str = "default") -> dict:
        return _guarded(mcp_contract_list, home, workspace_id)

    @server.tool(name="trial_explain", description="Explain a recorded trial's verdict, evidence tier, recommendation, and any pending human-gated action. Read-only.")
    def trial_explain(home: Optional[str] = None, workspace_id: str = "default",
                      trial_id: str = "") -> dict:
        return _guarded(mcp_trial_explain, home, workspace_id, trial_id)

    @server.tool(name="experiment_status", description="Report a trial's current verdict, evidence tier, recommendation, manifest hash, and any pending human-gated action. Read-only.")
    def experiment_status(home: Optional[str] = None, workspace_id: str = "default",
                          trial_id: str = "") -> dict:
        return _guarded(mcp_experiment_status, home, workspace_id, trial_id)

    @server.tool(name="artifact_verify", description="Verify a contract bundle's authenticity + evidence without trusting it. Read-only.")
    def artifact_verify(report_path: str) -> dict:
        return _guarded(mcp_artifact_verify, report_path)

    @server.tool(name="experiment_propose", description="Propose a bounded variant set with expected effects. Read-only; does not clone, apply, or deploy.")
    def experiment_propose(home: Optional[str] = None, workspace_id: str = "default",
                           agent_id: str = "", contract_id: str = "",
                           parameter: str = "interrupt_sensitivity") -> dict:
        return _guarded(mcp_experiment_propose, home, workspace_id, agent_id, contract_id, parameter)

    @server.tool(name="experiment_create", description="Clone-scoped: precommit a trial manifest from a committed battery BEFORE any capture, so the fixture universe is fixed ahead of the results. Never captures, never deploys.")
    def experiment_create(home: Optional[str] = None, workspace_id: str = "default",
                          agent_id: str = "", trial_id: str = "",
                          battery_path: str = "", min_n: int = 1) -> dict:
        return _guarded(mcp_experiment_create, home, workspace_id, agent_id, trial_id,
                                     battery_path, min_n)

    @server.tool(name="experiment_run", description="Clone-scoped: recompute a before/after trial (offline, no network, no production mutation) and record a recommendation. Names any pending human-gated action.")
    def experiment_run(home: Optional[str] = None, workspace_id: str = "default",
                       agent_id: str = "", trial_id: str = "", battery_path: str = "",
                       before_path: str = "", after_path: str = "", min_n: int = 1) -> dict:
        return _guarded(mcp_experiment_run, home, workspace_id, agent_id, trial_id, battery_path,
                                  before_path, after_path, min_n)

    @server.tool(name="clone_cleanup", description="Clone-scoped: delete a STAGING clone THIS tool created, authorized by its durable clone receipt (referenced by trial_id or receipt_id) -- never a raw clone id or display name. Never touches production.")
    def clone_cleanup(home: Optional[str] = None, workspace_id: str = "default",
                      trial_id: str = "", receipt_id: str = "", stack: str = "mock",
                      work_dir: str = ".") -> dict:
        return _guarded(mcp_clone_cleanup, home, workspace_id, trial_id, receipt_id, stack, work_dir)

    return server


def main(argv=None) -> int:
    server = build_server()
    server.run()  # stdio transport by default
    return 0


if __name__ == "__main__":
    sys.exit(main())
