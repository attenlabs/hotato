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

from ._engine.vad import BackendUnavailable

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
HANDLED = (ValueError, OSError, BackendUnavailable)

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
