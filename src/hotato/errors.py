"""The single structured error contract shared by both agent surfaces.

Success is a schema'd envelope (see ``schema/envelope.v1.json``); this module is
its failure counterpart (``schema/error.v1.json``). The CLI (``--format json``)
and the one MCP tool both emit the SAME ``ok: false`` object for the same
classes of bad input, so an agent needs a single parser for the whole call
lifecycle: branch on ``ok`` (absent on a success envelope), then on
``error_code``.

Additive only. It introduces no new success behaviour and never touches the
success envelope. The one deliberate surface difference is in the ``message``
text: on the MCP surface any CLI flag names are rewritten to the tool's
parameter names (``stereo`` / ``caller`` / ``agent`` / ``suite`` ...) so the
surfaced message instructs the model in its own vocabulary. The shape and the
``error_code`` are identical on both surfaces.
"""

from __future__ import annotations

import json as _json
import re as _re

from ._engine.vad import BackendUnavailable


_URL_IN_TEXT_RE = _re.compile(
    r"(?i)\b[a-z][a-z0-9+.-]*://[^\s<>\"']+"
)


def sanitize_url(url) -> str:
    """Return a URL that is safe to include in logs and error messages.

    URL userinfo and the complete query/fragment are untrusted secret-bearing
    surfaces: webhook URLs commonly carry tokens in the query, pre-signed
    recording URLs carry signatures there, and ``user:password@host`` embeds a
    credential in the authority.  Preserve only the routing context useful for
    diagnosis (scheme, host, explicit port, and path).  A query is represented
    by the literal marker ``?redacted``; userinfo and fragments are removed.

    Parsing failures return a constant marker rather than the original value,
    because echoing a malformed URL is the unsafe failure mode this helper is
    intended to prevent.
    """
    from urllib.parse import urlsplit, urlunsplit

    if not isinstance(url, str) or not url:
        return "<redacted-url>"
    try:
        parsed = urlsplit(url)
        host = parsed.hostname
        if not parsed.scheme or not host:
            return "<redacted-url>"
        port = parsed.port  # raises ValueError for a malformed/out-of-range port
    except (TypeError, ValueError):
        return "<redacted-url>"

    display_host = host
    if ":" in display_host and not display_host.startswith("["):
        display_host = f"[{display_host}]"
    netloc = display_host + (f":{port}" if port is not None else "")
    query = "redacted" if parsed.query else ""
    return urlunsplit((parsed.scheme.lower(), netloc, parsed.path, query, ""))


def sanitize_urls_in_text(value) -> str:
    """Redact every URL embedded in arbitrary exception/body text.

    Network libraries and remote error pages can repeat the request URL in an
    exception or response body.  Sanitizing only Hotato's explicit ``url``
    argument would therefore still leak the same query token through the
    diagnostic detail.  This is the single text-level path used before those
    details reach a user-facing error or warning.
    """
    text = str(value)
    return _URL_IN_TEXT_RE.sub(lambda match: sanitize_url(match.group(0)), text)


class HttpResponseTooLarge(ValueError):
    """A remote response exceeded an explicit in-process byte ceiling.

    This remains a ``ValueError`` so CLI/MCP callers keep the existing
    ``invalid_input`` / exit-2 error contract.  The distinct subclass lets
    transports with a stronger public error type (for example the rubric and
    state-adapter lanes) translate the refusal without brittle message
    matching.
    """


def read_bounded_http_body(response, *, max_bytes: int,
                           subject: str = "HTTP response") -> bytes:
    """Read at most ``max_bytes`` from an urllib-style response.

    A plain ``response.read()`` gives a remote peer an unbounded allocation in
    this process.  Refuse a declared oversize ``Content-Length`` before reading
    any body bytes, then request exactly one byte beyond the ceiling so a
    chunked response (or one with no length header) is rejected deterministically
    as soon as it crosses the same limit.

    ``subject`` is caller-authored diagnostic context.  Callers that include a
    URL must pass it through :func:`sanitize_url` first.
    """
    if isinstance(max_bytes, bool) or not isinstance(max_bytes, int) or max_bytes < 1:
        raise ValueError("max_bytes must be a positive integer")

    raw_length = None
    headers = getattr(response, "headers", None)
    if headers is not None:
        try:
            raw_length = headers.get("Content-Length")
        except (AttributeError, TypeError):
            raw_length = None
    if raw_length is None:
        getheader = getattr(response, "getheader", None)
        if callable(getheader):
            try:
                raw_length = getheader("Content-Length")
            except (AttributeError, TypeError):
                raw_length = None

    try:
        declared_length = int(str(raw_length).strip()) if raw_length is not None else None
    except (TypeError, ValueError):
        declared_length = None
    if declared_length is not None and declared_length >= 0 and declared_length > max_bytes:
        raise HttpResponseTooLarge(
            f"{subject} exceeds the {max_bytes}-byte response limit; refusing "
            "the body before reading it"
        )

    body = response.read(max_bytes + 1)
    if len(body) > max_bytes:
        raise HttpResponseTooLarge(
            f"{subject} exceeds the {max_bytes}-byte response limit"
        )
    return body


def require_regular_file(path) -> None:
    """Refuse a non-regular file (FIFO/named pipe, device, socket) BEFORE any
    ``open()``/``wave.open()`` on it.

    Opening a FIFO for reading blocks at the OS level until a writer appears, so
    a FIFO path (deliberate, or a stale half-written capture pipe) would hang the
    whole process with no error. ``os.stat()`` never blocks and follows symlinks,
    so a symlink to a FIFO is caught too. A missing/unreadable path still raises
    the normal ``OSError`` here (same as ``open()`` would), so ``file_not_found``
    classification is unaffected; only a non-regular file gets the ``ValueError``.
    Every path that ultimately calls ``wave.open()`` should route through here.
    """
    import os as _os
    import stat as _stat

    st = _os.stat(path)
    if not _stat.S_ISREG(st.st_mode):
        raise ValueError(
            f"{path!r} is not a regular file (found a named pipe/FIFO or other "
            "special file); hotato can only read a plain PCM WAV file. If you "
            "are streaming from a live process, write the finished recording "
            "to a real file first, then pass that path."
        )


def wav_read(path):
    """``wave.open(path, "rb")`` with :func:`require_regular_file` applied first,
    so no read-mode WAV open anywhere can block forever on a writer-less FIFO.
    Returns the same ``Wave_read`` object (usable as a context manager)."""
    require_regular_file(path)
    import wave as _wave

    # open-ok: require_regular_file(path) ran above; this IS the guarded helper
    return _wave.open(path, "rb")


def open_regular(path, mode: str = "rb", **kwargs):
    """``open(path, mode, **kwargs)`` with :func:`require_regular_file` applied
    first, so a read on an externally supplied path (raw bytes for hashing, or a
    JSON/text config/scenario/trace) can never block forever on a writer-less
    FIFO. Read modes only. ``kwargs`` (e.g. ``encoding``) pass through to open."""
    require_regular_file(path)
    # open-ok: this IS the guarded helper; require_regular_file ran above
    return open(path, mode, **kwargs)


def safe_json_dumps(obj, **kwargs) -> str:
    """``json.dumps`` that refuses to emit RFC-8259-invalid output.

    Python's default (``allow_nan=True``) silently serializes
    ``float('nan')`` / ``float('inf')`` / ``float('-inf')`` as the bare,
    non-standard tokens ``NaN`` / ``Infinity`` / ``-Infinity``. Any strict
    JSON parser (JavaScript's ``JSON.parse``, most non-Python consumers)
    throws on those, and ``jq`` silently coerces them to ``null`` (data loss).
    Since hotato advertises its ``--format json`` output as literal,
    paste-ready, agent-drivable JSON, every emitter forces ``allow_nan=False``
    and converts the resulting ``ValueError`` into the tool's standard clean,
    finite-number usage error (caught by the CLI/MCP HANDLED boundary -> exit
    2, structured error) instead of shipping broken JSON."""
    kwargs.setdefault("allow_nan", False)
    try:
        return _json.dumps(obj, **kwargs)
    except ValueError as exc:
        raise ValueError(
            "a value in the output is not a finite number (NaN/Infinity); "
            "check the input fix plan / config for non-finite numbers."
        ) from exc


# =========================================================================
# Shared structural / parse helpers.
#
# These centralize idioms that were reimplemented across the schema validators
# (conversation / conversation-test / scenario / rubric) and the file loaders.
# The MECHANISM is shared here; each caller keeps its own schema-specific
# message text, which is load-bearing per that schema's honesty invariant.
# This module is a leaf (only ``_engine.vad``), so every caller can import it
# with no cycle.
# =========================================================================

def load_json_file(path, label=None, *, encoding: str = "utf-8"):
    """Open a regular file (FIFO-safe via :func:`open_regular`) and ``json.load``
    it, converting a ``JSONDecodeError`` into the standard usage ``ValueError``
    (the caller's exit-2 path). ``label`` overrides how the file is named in that
    error message (default ``repr(path)``)."""
    with open_regular(path, "r", encoding=encoding) as fh:
        try:
            return _json.load(fh)
        except _json.JSONDecodeError as exc:
            subject = label if label is not None else repr(path)
            raise ValueError(f"{subject} is not valid JSON: {exc}") from exc


def reject_overall_score(obj, message: str) -> None:
    """The honesty-wall mechanism: refuse a mapping that carries a forbidden
    ``overall_score`` key. One check, shared by every schema validator; the
    ``message`` stays the caller's, because each schema's wording is load-bearing
    (a scenario 'never scores anything', a conversation-test 'success is a
    boolean', ...)."""
    if isinstance(obj, dict) and "overall_score" in obj:
        raise ValueError(message)


def check_kind_version(doc, *, kind, version, subject: str) -> None:
    """Structural ``kind``+``version`` const check shared by the schema
    validators. The ``kind`` mismatch message is identical across schemas; the
    version-mismatch message differs only by ``subject`` (e.g. ``"conversation"``,
    ``"suite"``). Raises ``ValueError`` on a mismatch."""
    if doc.get("kind") != kind:
        raise ValueError(f"'kind' must be {kind!r}, got {doc.get('kind')!r}")
    if doc.get("version") != version:
        raise ValueError(
            f"unsupported {subject} version {doc.get('version')!r}; "
            f"this build supports version {version}"
        )


# A word-shaped token (snake_case / dotted / hyphenated) may be emitted BARE in
# the small YAML/flow subset the assertion + scenario + conversation-test
# emitters share (and the parser reads back); any other scalar must be quoted so
# the subset reads it back verbatim. ONE definition -- both assert_'s emitter and
# conversation_test's starter emitter import it (it had been copy-pasted between
# them to avoid an import cycle; there is none through this leaf module).
SAFE_BARE_TOKEN_RE = _re.compile(r"^[A-Za-z0-9_.\-]+$")
YAML_RESERVED_BARE = ("true", "false", "null", "~", "")


def is_safe_bare_token(s: str) -> bool:
    """True if ``s`` may be rendered as a bare token in the shared YAML/flow
    subset: a word-shaped token that is not a reserved scalar."""
    return bool(SAFE_BARE_TOKEN_RE.match(s)) and s.lower() not in YAML_RESERVED_BARE


class ChannelRangeError(ValueError):
    """A caller/agent channel index that is out of range for a recording. A
    ``ValueError`` subclass so every existing ``except ValueError`` and the
    :data:`HANDLED` contract still catch it (exit 2, structured error), but a
    distinct type so a batch command (``analyze`` / ``loop`` / ``sweep``) can
    tell a GLOBAL flag mistake -- the same bad ``--caller-channel`` /
    ``--agent-channel`` for every file -- apart from a genuinely per-file
    problem (a mono or corrupt WAV) and propagate it as a usage error instead
    of silently degrading it into a per-file skip."""


TOOL = "hotato"
SCHEMA_VERSION = "1"
# Every failure class maps to the CLI's existing exit-2 (unusable input / usage
# error) convention; the result envelope's own exit_code stays frozen to 0 or 1.
ERROR_EXIT_CODE = 2

# The exception classes the input-hardening layer raises for a clean, expected
# error (never a bug). Both surfaces catch exactly these and turn them into the
# structured error object; anything else is a real fault and must not be masked.
#
# ``OSError`` (the superclass of FileNotFoundError, PermissionError,
# IsADirectoryError, NotADirectoryError, FileExistsError, ...) is included so that
# EVERY filesystem-input problem -- not just a missing file -- gets the same clean
# exit-2 / structured-json treatment instead of leaking a raw traceback and the
# wrong exit code. A directory passed where a file is expected, an unreadable
# (chmod 000) input, or an existing --out that cannot be replaced are all input
# errors, not bugs.
#
# ``MemoryError`` is included so that an oversized recording (the decode funnel
# fully materializes a WAV's PCM bytes in one ``readframes`` call, with no
# pre-flight size cap) is refused cleanly -- exit 2, structured error -- instead
# of a raw traceback and the uncaught-exception default exit code.
#
# ``RecursionError`` is included so a pathologically deeply nested JSON input
# (e.g. thousands of nested arrays in a webhook payload, a --scenarios-dir
# scenario file, or a contract.json) gets the same clean exit-2 treatment
# instead of a bare traceback: CPython's stdlib ``json`` decoder recurses once
# per nesting level and raises a bare ``RecursionError`` (not a
# ``json.JSONDecodeError``) on deeply nested input. Neither MemoryError nor
# RecursionError is a subclass of ValueError or OSError, so both must be
# listed explicitly.
HANDLED = (ValueError, OSError, BackendUnavailable, MemoryError, RecursionError)

# Stable error_code slugs. Append-only: a consumer may branch on these, so an
# existing slug never changes meaning. Kept in sync with the enum in
# schema/error.v1.json.
ERROR_CODES = (
    "missing_input",
    "mode_conflict",
    "mono_as_stereo",
    "sample_rate_mismatch",
    "file_not_found",
    "unknown_suite",
    "not_scorable",
    "backend_unavailable",
    "usage_error",
    "input_too_deeply_nested",
)

# CLI flag -> MCP parameter name, for rewriting a surfaced message so it
# instructs the model in its own vocabulary. Applied longest-key-first so that
# for example "--caller-channel" is rewritten before "--caller".
_FLAG_TO_PARAM = {
    "--caller-channel": "caller_channel",
    "--agent-channel": "agent_channel",
    "--max-talk-over": "max_talk_over_sec",
    "--max-time-to-yield": "max_time_to_yield_sec",
    "--stereo": "stereo",
    "--caller": "caller",
    "--agent": "agent",
    "--suite": "suite",
    "--onset": "onset_sec",
    "--stack": "stack",
    "--expect": "expect",
}


def rewrite_flags(message: str) -> str:
    """Rewrite CLI flag names in a message to the MCP tool's parameter names, so
    a message authored for the CLI correctly instructs a model calling the tool.
    Longest flag first so a prefix flag never eats a longer one."""
    out = message
    for flag in sorted(_FLAG_TO_PARAM, key=len, reverse=True):
        out = out.replace(flag, _FLAG_TO_PARAM[flag])
    return out


def classify(exc: Exception) -> tuple[str, str]:
    """Map a raised, expected error to ``(error_code, message)``. Pure; no side
    effects. The message is the CLI-vocabulary text (flags intact); callers that
    surface to a model rewrite it with :func:`rewrite_flags`."""
    if isinstance(exc, BackendUnavailable):
        return "backend_unavailable", str(exc)
    if isinstance(exc, RecursionError):
        # RecursionError's own str() names the innermost recursive call
        # (interpreter-internal, not useful to a caller), so it needs its own
        # branch ahead of the generic ``msg = str(exc)`` fallback below.
        return (
            "input_too_deeply_nested",
            "input JSON is too deeply nested to parse safely.",
        )
    if isinstance(exc, MemoryError):
        # MemoryError's own str() is usually empty, so it needs its own
        # branch ahead of the generic ``msg = str(exc)`` fallback below --
        # otherwise the surfaced message would be blank.
        return (
            "usage_error",
            "the recording is too large to decode in memory. Score a "
            "shorter clip, or run on a machine with more RAM.",
        )
    if isinstance(exc, FileNotFoundError):
        name = getattr(exc, "filename", None)
        if name:
            return "file_not_found", f"{name!r}: no such file."
        return "file_not_found", str(exc) or "no such file."
    if isinstance(exc, OSError):
        # Any other filesystem-input error (a directory where a file was
        # expected, a permission-denied read/write, an --out that already
        # exists). A clean usage error, not a crash: name the path and the OS
        # reason without a Python traceback.
        name = getattr(exc, "filename", None)
        reason = getattr(exc, "strerror", None) or str(exc)
        if name:
            return "usage_error", f"{name!r}: {reason}."
        return "usage_error", reason or "unusable file input."

    msg = str(exc)
    low = msg.lower()
    if "has one channel" in low:
        return "mono_as_stereo", msg
    if "sample-rate mismatch" in low:
        return "sample_rate_mismatch", msg
    if "unknown suite" in low:
        return "unknown_suite", msg
    if ("cannot be combined with a single recording" in low
            or "two different result files" in low):
        return "mode_conflict", msg
    if low.startswith("provide "):
        return "missing_input", msg
    return "usage_error", msg


def error_object(error_code: str, message: str) -> dict:
    """Build the structured error object (schema/error.v1.json)."""
    return {
        "tool": TOOL,
        "schema_version": SCHEMA_VERSION,
        "ok": False,
        "error_code": error_code,
        "message": message,
        "exit_code": ERROR_EXIT_CODE,
    }


def cli_error(exc: Exception) -> dict:
    """The structured error for the CLI ``--format json`` path: CLI-vocabulary
    message (the user is on the CLI, so the flag names are the right vocabulary)."""
    code, message = classify(exc)
    return error_object(code, message)


def mcp_error(exc: Exception) -> dict:
    """The structured error for the one MCP tool: same shape and error_code as
    the CLI, with the message rewritten from CLI flags to tool parameter names."""
    code, message = classify(exc)
    return error_object(code, rewrite_flags(message))


def validate_input_mode(
    stereo=None, caller=None, agent=None, suite=None
) -> None:
    """Structurally enforce EXACTLY ONE input mode for the MCP tool (the wire
    equivalent of a ``oneOf`` / pydantic root validator): a single recording
    (``stereo``, or BOTH ``caller`` and ``agent``) OR a ``suite``, never both and
    never an incomplete single. Raises a ValueError that :func:`classify` maps to
    ``mode_conflict`` or ``missing_input``, so a bad combination is a clean
    structured error rather than a raw throw. Messages are already in tool
    (parameter-name) vocabulary."""
    single_any = bool(stereo or caller or agent)
    if suite and single_any:
        raise ValueError(
            "suite runs the bundled labelled battery and cannot be combined "
            "with a single recording (stereo / caller / agent). Pass exactly "
            "one input mode: a single recording, or suite."
        )
    if suite:
        return
    if stereo:
        return
    if caller and agent:
        return
    raise ValueError(
        "provide a single recording (stereo, or BOTH caller and agent), or "
        "suite to run the bundled battery. Pass exactly one input mode."
    )
