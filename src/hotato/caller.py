"""Bounded, evidence-producing caller programs for voice-agent exercises.

The caller is deliberately a *participant*, never an evaluator.  A model may
propose one of a node's allow-listed caller actions.  It cannot write tool
results, backend state, outcomes, assertions, or verdicts.  Every external
action and every model/TTS artifact is content-addressed so a completed run can
be replayed without invoking either model again.

This module owns orchestration only.  Media and signaling are supplied by a
``CallerSession`` implementation (for example a Pipecat or LiveKit sidecar).
Unsupported and unobservable operations remain explicit capability states.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import stat
import time
import urllib.parse
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Dict, List, Mapping, Optional, Protocol, Sequence, Tuple

PLAN_SCHEMA = "hotato.caller-plan.v1"
RESULT_SCHEMA = "hotato.caller-result.v1"
PACKAGE_SCHEMA = "hotato.caller-package.v1"

SUPPORTED = "SUPPORTED"
UNSUPPORTED = "UNSUPPORTED"
UNOBSERVABLE = "UNOBSERVABLE"
CAPABILITY_STATES = {SUPPORTED, UNSUPPORTED, UNOBSERVABLE}

MODES = {"scripted", "hybrid", "generative", "frozen_replay"}
NODE_TYPES = {
    "say", "generate", "listen", "wait", "dtmf", "silence", "impairment",
    "expect", "set_state", "branch", "repeat_bounded", "transfer_expect", "hangup",
}
MODEL_ACTIONS = {"say", "dtmf", "silence", "hangup"}
EVENT_KINDS = {
    "transcript", "tool_result", "state_snapshot", "dtmf", "lifecycle",
    "transfer", "hold", "timing", "timeout", "custom",
}
_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
_DTMF_RE = re.compile(r"^[0-9A-Da-d*#]{1,64}$")
_RESERVED_STATE_ROOTS = {
    "outcome", "passed", "verdict", "overall_score", "assertions", "tool_result",
    "backend_state", "policy", "reliability",
}

DEFAULT_LIMITS = {
    "max_steps": 256,
    "max_turns": 64,
    "max_duration_ms": 300_000,
    "max_visits_per_node": 16,
    "max_events": 512,
    "max_event_chars": 200_000,
    "max_model_calls": 32,
    "max_model_input_chars": 100_000,
    "max_model_output_chars": 100_000,
    "max_text_chars": 200_000,
    "max_audio_bytes": 100_000_000,
    "max_tokens": 20_000,
    # Hosted adapters must opt in by setting a positive ceiling.  Local models
    # report zero.  Unknown spend is refused instead of treated as free.
    "max_cost_microusd": 0,
    "max_wait_ms": 30_000,
}

_MAX_PACKAGE_JSON_BYTES = 64 * 1024 * 1024
_MAX_PACKAGE_ARTIFACT_BYTES = 256 * 1024 * 1024
_MAX_PACKAGE_ENTRIES = 100_000

# Caller triggers deliberately expose only a small, bounded regular-expression
# language.  Python's general backtracking engine is not a safe policy boundary
# for untrusted plans: expressions such as ``(a+)+$`` can consume exponential
# time.  The subset below has no grouping or alternation, permits at most one
# flat unbounded repeat, and caps both optional paths and searched input.
MAX_TRIGGER_REGEX_CHARS = 512
MAX_TRIGGER_SEARCH_CHARS = 1_024
_MAX_REGEX_REPEAT_UPPER = 1_024
_MAX_REGEX_PATH_BUDGET = 4_096


def _read_regular_bytes_no_follow(
    path: Path, *, max_bytes: int, reject_symlink: bool = True
) -> bytes:
    """Read one bounded regular file through a race-resistant descriptor.

    ``Path.is_file()`` followed by ``Path.read_bytes()`` leaves both a FIFO
    blocking window and a symlink-swap window.  Open nonblocking/no-follow
    where the platform exposes those flags, validate the opened descriptor,
    and bind it to the pre-open inode before consuming bytes.  Once opened,
    later pathname swaps cannot change what is read.
    """

    if isinstance(max_bytes, bool) or not isinstance(max_bytes, int) or max_bytes < 0:
        raise ValueError("max_bytes must be a non-negative integer")
    raw_path = os.fspath(path)
    before = os.lstat(raw_path) if reject_symlink else os.stat(raw_path)
    if reject_symlink and stat.S_ISLNK(before.st_mode):
        raise ValueError(f"{raw_path!r} must not be a symbolic link")
    if not stat.S_ISREG(before.st_mode):
        raise ValueError(f"{raw_path!r} must be a regular file")
    if before.st_size > max_bytes:
        raise ValueError(f"{raw_path!r} exceeds the {max_bytes}-byte read limit")

    flags = os.O_RDONLY
    flags |= getattr(os, "O_BINARY", 0)
    flags |= getattr(os, "O_NONBLOCK", 0)
    if reject_symlink:
        flags |= getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(raw_path, flags)
    try:
        opened = os.fstat(descriptor)
        if not stat.S_ISREG(opened.st_mode):
            raise ValueError(f"{raw_path!r} must be a regular file")
        before_identity = (before.st_dev, before.st_ino)
        opened_identity = (opened.st_dev, opened.st_ino)
        if before_identity != opened_identity:
            raise ValueError(f"{raw_path!r} changed while it was being opened")
        if opened.st_size > max_bytes:
            raise ValueError(f"{raw_path!r} exceeds the {max_bytes}-byte read limit")
        chunks: List[bytes] = []
        remaining = max_bytes + 1
        while remaining:
            chunk = os.read(descriptor, min(1024 * 1024, remaining))
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
        value = b"".join(chunks)
        if len(value) > max_bytes:
            raise ValueError(f"{raw_path!r} exceeds the {max_bytes}-byte read limit")
        return value
    finally:
        os.close(descriptor)


def _exclusive_write_bytes(path: Path, data: bytes) -> None:
    """Create one package file without following or replacing a path."""

    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_BINARY", 0)
    flags |= getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(path, flags, 0o600)
    opened = os.fstat(descriptor)
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
    except BaseException:
        try:
            current = os.lstat(path)
            if (current.st_dev, current.st_ino) == (opened.st_dev, opened.st_ino):
                os.unlink(path)
        except OSError:
            pass
        raise


def _empty_output_root(output_dir: str) -> Path:
    """Prepare an empty output directory without resolving a symlink root."""

    root = Path(os.path.abspath(output_dir))
    if os.path.lexists(root):
        info = os.lstat(root)
        if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode):
            raise ValueError("caller output directory must be a non-symlink directory")
    else:
        root.mkdir(parents=True, mode=0o700)
    if any(root.iterdir()):
        raise ValueError("caller output directory must be empty")
    return root


def _normalize_limits(value: Any) -> Dict[str, int]:
    if not isinstance(value, dict):
        raise ValueError("limits must be a mapping")
    _check_keys(value, set(DEFAULT_LIMITS), "limits")
    limits = dict(DEFAULT_LIMITS)
    limits.update(value)
    for name, raw in limits.items():
        high = 10**12 if name in {"max_audio_bytes", "max_cost_microusd"} else 10**9
        limits[name] = _integer(
            raw, "limits." + name, 0 if name == "max_cost_microusd" else 1, high
        )
    return limits


class CallerSession(Protocol):
    """Media/signaling boundary implemented outside the caller engine."""

    def capabilities(self) -> Mapping[str, str]: ...

    def send_text(self, text: str, metadata: Mapping[str, Any]) -> None: ...

    def send_audio(
        self, pcm_s16le: bytes, sample_rate_hz: int, metadata: Mapping[str, Any]
    ) -> None: ...

    def receive(self, timeout_ms: int) -> Optional[Mapping[str, Any]]: ...

    def send_dtmf(self, digits: str) -> None: ...

    def wait(self, duration_ms: int) -> None: ...

    def silence(self, duration_ms: int) -> None: ...

    def set_impairment(self, profile: Mapping[str, Any]) -> None: ...

    def hangup(self, reason: str) -> None: ...


class CallerModel(Protocol):
    """A caller model returns a proposal plus complete, JSON-safe provenance."""

    def propose(self, request: Mapping[str, Any]) -> Mapping[str, Any]: ...


class CallerTTS(Protocol):
    """TTS adapters return PCM16LE plus the settings that produced it."""

    def synthesize(self, text: str) -> Mapping[str, Any]: ...


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):  # type: ignore[no-untyped-def]
        raise RuntimeError("Ollama caller endpoint refused an HTTP redirect")


def _local_http_post(url: str, body: bytes, timeout_seconds: float) -> bytes:
    request = urllib.request.Request(
        url, data=body, headers={"Content-Type": "application/json"}, method="POST"
    )
    # Ignore environment proxy variables: a loopback-only adapter must not
    # route its prompt through an operator's configured HTTP proxy.
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), _NoRedirect())
    with opener.open(request, timeout=timeout_seconds) as response:
        data = response.read(1_000_001)
    if len(data) > 1_000_000:
        raise RuntimeError("Ollama caller response exceeded 1000000 bytes")
    return data


class OllamaCallerModel:
    """Strict local Ollama adapter for caller action proposals.

    The endpoint is constrained to loopback HTTP and redirects are refused.
    This adapter reports Ollama's token counts and a zero API charge; machine
    compute remains outside that field.  The engine still validates the exact
    proposal shape and the node-specific allow-list.
    """

    def __init__(
        self,
        model: str,
        *,
        endpoint: str = "http://127.0.0.1:11434",
        temperature: float = 0.0,
        seed: int = 0,
        timeout_seconds: float = 60.0,
        post: Callable[[str, bytes, float], bytes] = _local_http_post,
    ):
        self.model = _nonempty(model, "Ollama model", 500)
        parsed = urllib.parse.urlparse(endpoint)
        if (
            parsed.scheme != "http" or parsed.hostname not in {"127.0.0.1", "localhost", "::1"}
            or parsed.username is not None or parsed.password is not None
            or parsed.query or parsed.fragment or parsed.path not in {"", "/"}
        ):
            raise ValueError("Ollama caller endpoint must be loopback HTTP with no path or credentials")
        try:
            port = parsed.port or 11434
        except ValueError as exc:
            raise ValueError("Ollama caller endpoint port is invalid") from exc
        if not 1 <= port <= 65535:
            raise ValueError("Ollama caller endpoint port is invalid")
        if isinstance(temperature, bool) or not isinstance(temperature, (int, float)) or not 0 <= float(temperature) <= 2:
            raise ValueError("Ollama caller temperature must be in [0, 2]")
        if isinstance(seed, bool) or not isinstance(seed, int):
            raise ValueError("Ollama caller seed must be an integer")
        if isinstance(timeout_seconds, bool) or not isinstance(timeout_seconds, (int, float)) or not 0 < float(timeout_seconds) <= 600:
            raise ValueError("Ollama caller timeout_seconds must be in (0, 600]")
        normalized_host = "127.0.0.1" if parsed.hostname == "localhost" else parsed.hostname
        host = f"[{normalized_host}]" if normalized_host == "::1" else normalized_host
        self.endpoint = f"http://{host}:{port}"
        self.temperature = float(temperature)
        self.seed = seed
        self.timeout_seconds = float(timeout_seconds)
        self._post = post

    def propose(self, request: Mapping[str, Any]) -> Mapping[str, Any]:
        allowed = request.get("allowed_actions")
        if not isinstance(allowed, list) or not allowed:
            raise ValueError("caller model request requires allowed_actions")
        system = (
            "You are the caller participant in a bounded voice-agent test. Return one JSON "
            "object only. Choose an action from the supplied allowed_actions. Exact shapes: "
            "say={action,text}; dtmf={action,digits}; silence={action,duration_ms}; "
            "hangup={action,reason}. Never return state, tool results, outcomes, assertions, "
            "scores, or verdicts."
        )
        payload = {
            "model": self.model,
            "stream": False,
            "format": "json",
            "options": {"temperature": self.temperature, "seed": self.seed},
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": _canonical(dict(request)).decode("utf-8")},
            ],
        }
        raw = self._post(
            self.endpoint + "/api/chat", _canonical(payload), self.timeout_seconds
        )
        try:
            response = json.loads(
                raw.decode("utf-8"),
                parse_constant=lambda value: (_ for _ in ()).throw(ValueError(value)),
            )
            content = response["message"]["content"]
            proposal = json.loads(
                content,
                parse_constant=lambda value: (_ for _ in ()).throw(ValueError(value)),
            )
        except (UnicodeDecodeError, ValueError, KeyError, TypeError) as exc:
            raise RuntimeError("Ollama caller returned an invalid JSON proposal") from exc
        input_tokens = response.get("prompt_eval_count")
        output_tokens = response.get("eval_count")
        if (
            isinstance(input_tokens, bool) or not isinstance(input_tokens, int) or input_tokens < 0
            or isinstance(output_tokens, bool) or not isinstance(output_tokens, int) or output_tokens < 0
        ):
            raise RuntimeError("Ollama caller response omitted non-negative token counts")
        response_model = response.get("model", self.model)
        if not isinstance(response_model, str) or not response_model:
            raise RuntimeError("Ollama caller response model is invalid")
        return {
            "proposal": proposal,
            "raw": response,
            "provider": "ollama-local",
            "model": response_model,
            "parameters": {
                "endpoint": self.endpoint, "temperature": self.temperature,
                "seed": self.seed, "format": "json",
            },
            "usage": {
                "input_tokens": input_tokens, "output_tokens": output_tokens,
                "cost_microusd": 0,
            },
        }


@dataclass(frozen=True)
class CallerRun:
    output_dir: str
    result: Dict[str, Any]
    verification: Dict[str, Any]

    @property
    def exit_code(self) -> int:
        return int(self.result["exit_code"])


class _RuntimeBlocked(RuntimeError):
    def __init__(self, code: str, message: str, **details: Any):
        super().__init__(message)
        self.code = code
        self.details = details


class _RuntimeInputError(RuntimeError):
    def __init__(self, code: str, message: str, **details: Any):
        super().__init__(message)
        self.code = code
        self.details = details


def _external_error(code: str, exc: BaseException) -> Dict[str, Any]:
    """Build a package-safe error without persisting adapter exception text.

    Adapter and session exceptions may contain authorization headers, signed
    URLs, transcript fragments, or subprocess arguments.  The stable category
    and exception type are sufficient for machine handling; arbitrary text
    stays out of shareable caller packages.
    """

    messages = {
        "SESSION_OR_ADAPTER_ERROR": "caller session or adapter failed",
        "SESSION_EVIDENCE_INVALID": "caller session evidence was invalid",
    }
    return {
        "code": code,
        "message": messages.get(code, "external caller boundary failed"),
        "exception_type": type(exc).__name__,
    }


def _canonical(value: Any) -> bytes:
    try:
        return json.dumps(
            value, sort_keys=True, separators=(",", ":"), ensure_ascii=False,
            allow_nan=False,
        ).encode("utf-8")
    except (TypeError, ValueError) as exc:
        raise ValueError("caller values must be finite JSON values") from exc


def _sha(data: bytes) -> str:
    return "sha256:" + hashlib.sha256(data).hexdigest()


def _check_keys(value: Mapping[str, Any], allowed: set, where: str) -> None:
    unknown = sorted(set(value) - allowed)
    if unknown:
        raise ValueError(f"{where} has unknown fields: {', '.join(unknown)}")


def _nonempty(value: Any, where: str, maximum: int = 100_000) -> str:
    if not isinstance(value, str) or not value or len(value) > maximum:
        raise ValueError(f"{where} must be a non-empty string of at most {maximum} characters")
    return value


def _integer(value: Any, where: str, low: int, high: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not low <= value <= high:
        raise ValueError(f"{where} must be an integer in [{low}, {high}]")
    return value


def _validate_safe_regex(pattern: str, where: str) -> str:
    """Validate Hotato's bounded, non-branching trigger-regex subset.

    Supported atoms are literals, escaped characters, ``.``, and character
    classes.  ``^`` and ``$`` are accepted only at the pattern boundaries.
    Atoms may use ``?``, ``*``, ``+``, or ``{m}``/``{m,n}``/``{m,}``, subject
    to a bounded path budget and at most one start-anchored unbounded repeat.
    Groups, alternation, lookarounds, conditionals, and backreferences are
    refused.
    """

    if len(pattern) > MAX_TRIGGER_REGEX_CHARS:
        raise ValueError(
            f"{where} must contain at most {MAX_TRIGGER_REGEX_CHARS} characters"
        )
    index = 0
    atom_available = False
    quantified = False
    unbounded = 0
    repeat_upper = 0
    path_budget = 1
    while index < len(pattern):
        character = pattern[index]
        if character == "\\":
            index += 1
            if index >= len(pattern):
                raise ValueError(f"{where} ends with an incomplete escape")
            escaped = pattern[index]
            if escaped.isdigit() or escaped in {"g", "k"}:
                raise ValueError(f"{where} cannot contain backreferences")
            atom_available = True
            quantified = False
            index += 1
            continue
        if character == "[":
            cursor = index + 1
            if cursor < len(pattern) and pattern[cursor] == "^":
                cursor += 1
            content = 0
            while cursor < len(pattern):
                if pattern[cursor] == "\\":
                    cursor += 2
                    content += 1
                    continue
                if pattern[cursor] == "]":
                    break
                if pattern[cursor] == "[":
                    raise ValueError(f"{where} cannot contain nested character classes")
                cursor += 1
                content += 1
            if cursor >= len(pattern) or content == 0:
                raise ValueError(f"{where} contains an invalid character class")
            atom_available = True
            quantified = False
            index = cursor + 1
            continue
        if character in "()|":
            raise ValueError(f"{where} cannot contain groups or alternation")
        if character == "^":
            if index != 0:
                raise ValueError(f"{where} permits ^ only at the start")
            atom_available = False
            quantified = False
            index += 1
            continue
        if character == "$":
            if index != len(pattern) - 1:
                raise ValueError(f"{where} permits $ only at the end")
            atom_available = False
            quantified = False
            index += 1
            continue
        if character in "*+?" or character == "{":
            if not atom_available or quantified:
                raise ValueError(f"{where} contains a nested or unattached repeat")
            minimum = 0
            maximum: Optional[int]
            if character == "?":
                maximum = 1
                index += 1
            elif character == "*":
                maximum = None
                index += 1
            elif character == "+":
                minimum = 1
                maximum = None
                index += 1
            else:
                closing = pattern.find("}", index + 1)
                if closing < 0:
                    raise ValueError(f"{where} contains an unterminated repeat")
                body = pattern[index + 1:closing]
                if not body or body.count(",") > 1:
                    raise ValueError(f"{where} contains an invalid repeat")
                pieces = body.split(",")
                if any(piece and (not piece.isdigit() or len(piece) > 4) for piece in pieces):
                    raise ValueError(f"{where} contains an invalid repeat")
                if len(pieces) == 1:
                    if not pieces[0]:
                        raise ValueError(f"{where} contains an invalid repeat")
                    minimum = int(pieces[0])
                    maximum = minimum
                else:
                    if not pieces[0]:
                        raise ValueError(f"{where} repeat requires a lower bound")
                    minimum = int(pieces[0])
                    maximum = int(pieces[1]) if pieces[1] else None
                if maximum is not None and minimum > maximum:
                    raise ValueError(f"{where} repeat lower bound exceeds its upper bound")
                index = closing + 1
            if maximum is None:
                unbounded += 1
                if unbounded > 1:
                    raise ValueError(f"{where} permits at most one unbounded repeat")
            else:
                if maximum > _MAX_REGEX_REPEAT_UPPER:
                    raise ValueError(
                        f"{where} bounded repeat exceeds {_MAX_REGEX_REPEAT_UPPER}"
                    )
                repeat_upper += maximum
                choices = maximum - minimum + 1
                path_budget *= choices
                if (
                    repeat_upper > _MAX_REGEX_REPEAT_UPPER
                    or path_budget > _MAX_REGEX_PATH_BUDGET
                ):
                    raise ValueError(f"{where} exceeds the bounded repeat budget")
            quantified = True
            atom_available = False
            continue
        if character in "]}":
            raise ValueError(f"{where} contains an unmatched delimiter")
        atom_available = True
        quantified = False
        index += 1
    if unbounded and not pattern.startswith("^"):
        raise ValueError(
            f"{where} unbounded repeat requires a start-anchored pattern"
        )
    try:
        re.compile(pattern, re.IGNORECASE)
    except re.error as exc:
        raise ValueError(f"{where} is invalid: {exc}") from exc
    return pattern


def _validate_trigger(value: Any, where: str = "trigger", depth: int = 0) -> Dict[str, Any]:
    if depth > 8 or not isinstance(value, dict) or not value:
        raise ValueError(f"{where} must be a non-empty trigger mapping")
    combinators = [name for name in ("all", "any", "not") if name in value]
    if combinators:
        if len(value) != 1 or len(combinators) != 1:
            raise ValueError(f"{where} must contain one trigger combinator")
        kind = combinators[0]
        child = value[kind]
        if kind == "not":
            return {kind: _validate_trigger(child, where + ".not", depth + 1)}
        if not isinstance(child, list) or not child:
            raise ValueError(f"{where}.{kind} must be a non-empty list")
        return {
            kind: [_validate_trigger(item, f"{where}.{kind}[{i}]", depth + 1)
                   for i, item in enumerate(child)]
        }
    allowed = {
        "event", "text_regex", "tool", "status", "digits", "path", "equals",
        "metric", "gte", "lte", "actor_state",
    }
    _check_keys(value, allowed, where)
    event = value.get("event")
    if event is not None and event not in EVENT_KINDS:
        raise ValueError(f"{where}.event must be one of {sorted(EVENT_KINDS)}")
    if "text_regex" in value:
        pattern = _nonempty(
            value["text_regex"], where + ".text_regex", MAX_TRIGGER_REGEX_CHARS
        )
        _validate_safe_regex(pattern, where + ".text_regex")
    for key in ("tool", "status", "digits", "path", "metric"):
        if key in value:
            _nonempty(value[key], where + "." + key, 1_000)
    for key in ("gte", "lte"):
        if key in value and (isinstance(value[key], bool) or not isinstance(value[key], (int, float))):
            raise ValueError(f"{where}.{key} must be a finite number")
        if key in value and not (-1e300 < float(value[key]) < 1e300):
            raise ValueError(f"{where}.{key} must be a finite number")
    if "actor_state" in value:
        state = value["actor_state"]
        if not isinstance(state, dict) or set(state) != {"key", "equals"}:
            raise ValueError(f"{where}.actor_state must contain exactly key and equals")
        _validate_state_key(state["key"], where + ".actor_state.key")
    if not any(key in value for key in allowed):
        raise ValueError(f"{where} is empty")
    return json.loads(_canonical(value))


def _validate_state_key(value: Any, where: str) -> str:
    key = _nonempty(value, where, 256)
    parts = key.split(".")
    if any(not _ID_RE.fullmatch(part) for part in parts):
        raise ValueError(f"{where} must be a dotted identifier")
    if parts[0].lower() in _RESERVED_STATE_ROOTS:
        raise ValueError(f"{where} uses reserved authority root {parts[0]!r}")
    return key


def _validate_node(node: Any, index: int) -> Dict[str, Any]:
    where = f"nodes[{index}]"
    if not isinstance(node, dict):
        raise ValueError(f"{where} must be a mapping")
    common = {"id", "type", "next"}
    node_type = node.get("type")
    if node_type not in NODE_TYPES:
        raise ValueError(f"{where}.type must be one of {sorted(NODE_TYPES)}")
    specific = {
        "say": {"text"},
        "generate": {"prompt", "allowed_actions"},
        "listen": {"timeout_ms", "max_events", "until", "on_timeout"},
        "wait": {"duration_ms"},
        "dtmf": {"digits"},
        "silence": {"duration_ms"},
        "impairment": {"profile"},
        "expect": {"when", "on_miss"},
        "set_state": {"key", "value"},
        "branch": {"cases", "default"},
        "repeat_bounded": {"target", "max_iterations"},
        "transfer_expect": {"timeout_ms", "max_events", "on_miss"},
        "hangup": {"reason"},
    }[node_type]
    _check_keys(node, common | specific, where)
    node_id = _nonempty(node.get("id"), where + ".id", 128)
    if not _ID_RE.fullmatch(node_id):
        raise ValueError(f"{where}.id must be a safe identifier")
    normalized: Dict[str, Any] = {"id": node_id, "type": node_type}
    if "next" in node:
        normalized["next"] = _nonempty(node["next"], where + ".next", 128)
    if node_type == "say":
        normalized["text"] = _nonempty(node.get("text"), where + ".text")
    elif node_type == "generate":
        normalized["prompt"] = _nonempty(node.get("prompt"), where + ".prompt")
        actions = node.get("allowed_actions", ["say"])
        if not isinstance(actions, list) or not actions or len(actions) > len(MODEL_ACTIONS):
            raise ValueError(f"{where}.allowed_actions must be a non-empty list")
        if any(action not in MODEL_ACTIONS for action in actions) or len(set(actions)) != len(actions):
            raise ValueError(f"{where}.allowed_actions contains an invalid or duplicate action")
        normalized["allowed_actions"] = list(actions)
    elif node_type in {"listen", "transfer_expect"}:
        normalized["timeout_ms"] = _integer(node.get("timeout_ms", 10_000), where + ".timeout_ms", 0, 300_000)
        normalized["max_events"] = _integer(node.get("max_events", 16), where + ".max_events", 1, 1_000)
        if node_type == "listen" and "until" in node:
            normalized["until"] = _validate_trigger(node["until"], where + ".until")
        if "on_timeout" in node:
            normalized["on_timeout"] = _nonempty(node["on_timeout"], where + ".on_timeout", 128)
        if "on_miss" in node:
            normalized["on_miss"] = _nonempty(node["on_miss"], where + ".on_miss", 128)
    elif node_type in {"wait", "silence"}:
        normalized["duration_ms"] = _integer(node.get("duration_ms"), where + ".duration_ms", 0, 300_000)
    elif node_type == "dtmf":
        digits = _nonempty(node.get("digits"), where + ".digits", 64)
        if not _DTMF_RE.fullmatch(digits):
            raise ValueError(f"{where}.digits contains invalid DTMF symbols")
        normalized["digits"] = digits.upper()
    elif node_type == "impairment":
        profile = node.get("profile")
        if not isinstance(profile, dict) or not profile:
            raise ValueError(f"{where}.profile must be a non-empty mapping")
        normalized["profile"] = json.loads(_canonical(profile))
    elif node_type == "expect":
        normalized["when"] = _validate_trigger(node.get("when"), where + ".when")
        if "on_miss" in node:
            normalized["on_miss"] = _nonempty(node["on_miss"], where + ".on_miss", 128)
    elif node_type == "set_state":
        normalized["key"] = _validate_state_key(node.get("key"), where + ".key")
        normalized["value"] = json.loads(_canonical(node.get("value")))
    elif node_type == "branch":
        cases = node.get("cases")
        if not isinstance(cases, list) or not cases or len(cases) > 100:
            raise ValueError(f"{where}.cases must be a non-empty list")
        normalized["cases"] = []
        for case_index, case in enumerate(cases):
            if not isinstance(case, dict) or set(case) != {"when", "next"}:
                raise ValueError(f"{where}.cases[{case_index}] must contain exactly when and next")
            normalized["cases"].append({
                "when": _validate_trigger(case["when"], f"{where}.cases[{case_index}].when"),
                "next": _nonempty(case["next"], f"{where}.cases[{case_index}].next", 128),
            })
        if "default" in node:
            normalized["default"] = _nonempty(node["default"], where + ".default", 128)
    elif node_type == "repeat_bounded":
        normalized["target"] = _nonempty(node.get("target"), where + ".target", 128)
        normalized["max_iterations"] = _integer(node.get("max_iterations"), where + ".max_iterations", 1, 10_000)
    elif node_type == "hangup":
        normalized["reason"] = _nonempty(node.get("reason", "scenario_complete"), where + ".reason", 1_000)
    return normalized


def validate_plan(value: Any) -> Dict[str, Any]:
    if not isinstance(value, dict):
        raise ValueError("caller plan must be a mapping")
    allowed = {"schema", "id", "mode", "start", "nodes", "limits", "initial_state", "frozen_package", "metadata"}
    _check_keys(value, allowed, "caller plan")
    if value.get("schema") != PLAN_SCHEMA:
        raise ValueError(f"caller plan schema must be {PLAN_SCHEMA!r}")
    plan_id = _nonempty(value.get("id"), "id", 128)
    if not _ID_RE.fullmatch(plan_id):
        raise ValueError("id must be a safe identifier")
    mode = value.get("mode")
    if mode not in MODES:
        raise ValueError(f"mode must be one of {sorted(MODES)}")
    metadata = value.get("metadata", {})
    if not isinstance(metadata, dict):
        raise ValueError("metadata must be a mapping")
    normalized: Dict[str, Any] = {
        "schema": PLAN_SCHEMA, "id": plan_id, "mode": mode,
        "metadata": json.loads(_canonical(metadata)),
    }
    if mode == "frozen_replay":
        _check_keys(
            value, {"schema", "id", "mode", "frozen_package", "metadata", "limits"},
            "frozen replay plan",
        )
        normalized["frozen_package"] = _nonempty(value.get("frozen_package"), "frozen_package", 4_096)
        normalized["limits"] = _normalize_limits(value.get("limits", {}))
        return normalized
    nodes_value = value.get("nodes")
    if not isinstance(nodes_value, list) or not nodes_value or len(nodes_value) > 10_000:
        raise ValueError("nodes must be a non-empty list with at most 10000 entries")
    nodes = [_validate_node(node, index) for index, node in enumerate(nodes_value)]
    by_id = {node["id"]: node for node in nodes}
    if len(by_id) != len(nodes):
        raise ValueError("node ids must be unique")
    start = _nonempty(value.get("start"), "start", 128)
    if start not in by_id:
        raise ValueError("start must reference a node id")
    refs: List[Tuple[str, str]] = []
    for node in nodes:
        for key in ("next", "on_timeout", "on_miss", "default", "target"):
            if key in node:
                refs.append((f"node {node['id']}.{key}", node[key]))
        for case in node.get("cases", []):
            refs.append((f"node {node['id']} branch", case["next"]))
    for where, ref in refs:
        if ref not in by_id:
            raise ValueError(f"{where} references unknown node {ref!r}")
    if mode == "scripted" and any(node["type"] == "generate" for node in nodes):
        raise ValueError("scripted mode cannot contain generate nodes")
    limits = _normalize_limits(value.get("limits", {}))
    initial = value.get("initial_state", {})
    if not isinstance(initial, dict):
        raise ValueError("initial_state must be a mapping")
    for key in initial:
        _validate_state_key(key, "initial_state key")
        if "." in key:
            raise ValueError("initial_state keys must be top-level identifiers; use nested mappings")
    normalized.update({
        "start": start, "nodes": nodes, "limits": limits,
        "initial_state": json.loads(_canonical(initial)),
    })
    return normalized


def load_plan(path: str) -> Dict[str, Any]:
    try:
        value = json.loads(
            _read_regular_bytes_no_follow(
                Path(path), max_bytes=_MAX_PACKAGE_JSON_BYTES
            )
        )
    except UnicodeDecodeError as exc:
        raise ValueError("caller plan must be UTF-8 JSON") from exc
    return validate_plan(value)


# ---------------------------------------------------------------------------
# Reactive barge-in: the agent-speech-onset convention + a named plan builder.
#
# The agent-speech-onset event rides the already-supported ``lifecycle`` kind
# (see EVENT_KINDS) with a dedicated status, so it needs no new event kind and
# no schema change: a ``{"event": "lifecycle", "status": AGENT_SPEECH_ONSET}``
# trigger already routes today (status matching is an existing capability).
# ---------------------------------------------------------------------------

AGENT_SPEECH_ONSET_STATUS = "agent_speech_started"


def agent_speech_started_event() -> Dict[str, Any]:
    """The session-emitted lifecycle event marking agent-speech ONSET.

    A caller session emits this the instant the agent starts talking -- in the
    hermetic harness the simulated agent stream emits it deterministically at a
    known time; in a future, operator-gated live path it would come from an
    activity/VAD detector on real audio.  It carries speech ACTIVITY, never
    transcribed content: the reactive caller reacts to onset, it does not read
    the agent's words, and it never replaces provider STT.
    """

    return {"kind": "lifecycle", "status": AGENT_SPEECH_ONSET_STATUS}


def agent_speech_onset_trigger() -> Dict[str, Any]:
    """The caller-plan trigger that fires on the agent-speech-onset event."""

    return {"event": "lifecycle", "status": AGENT_SPEECH_ONSET_STATUS}


def reactive_barge_in_plan(
    *,
    text: str,
    delay_ms: int,
    listen_timeout_ms: int,
    on_timeout: str = "giveup",
    plan_id: str = "reactive-barge-in",
) -> Dict[str, Any]:
    """Build the caller plan that barges in REACTIVELY, keyed to agent-speech onset.

    Reactive vs fixed-timeline.  A fixed-timeline caller (``hotato.drive``'s
    TwiML path) speaks at wall-clock offsets from call start and CANNOT react to
    the agent.  This plan instead waits for the agent to start talking: it
    ``listen``s until the agent-speech-onset event (``lifecycle`` /
    ``agent_speech_started``), then ``wait``s ``delay_ms`` past that onset,
    ``say``s ``text`` over the agent, and hangs up.  The interrupt is therefore
    measured from the ONSET EVENT, not from call start -- shift the onset and the
    barge-in shifts with it.  If the agent never starts talking within
    ``listen_timeout_ms``, the ``on_timeout="giveup"`` policy hangs up WITHOUT
    speaking: the caller reacts to the signal, it does not speak on a clock.

    Hermetic-only boundary.  This helper decides WHEN to barge in relative to the
    agent-speech onset event and produces the caller's turn timing and evidence.
    It does not score anything.  Whether the resulting two-channel audio actually
    overlaps the agent (talk-over) and whether the agent yielded or held the floor
    remain the job of the existing two-channel scoring engine.  In the hermetic
    harness the onset event is emitted deterministically by the simulated agent
    stream; a live activity/VAD detector on real audio is a separate,
    operator-gated step and is not part of this plan.

    ``delay_ms`` and ``listen_timeout_ms`` are milliseconds in ``[0, 300000]``.
    The returned plan is already validated (a normalized ``caller-plan.v1``
    mapping) and is ready to hand to :func:`run_caller`.
    """

    text = _nonempty(text, "reactive_barge_in_plan text")
    delay_ms = _integer(delay_ms, "reactive_barge_in_plan delay_ms", 0, 300_000)
    listen_timeout_ms = _integer(
        listen_timeout_ms, "reactive_barge_in_plan listen_timeout_ms", 0, 300_000
    )
    if on_timeout != "giveup":
        raise ValueError("reactive_barge_in_plan on_timeout must be 'giveup'")
    if not isinstance(plan_id, str) or not _ID_RE.fullmatch(plan_id):
        raise ValueError("reactive_barge_in_plan plan_id must be a safe identifier")

    trigger = agent_speech_onset_trigger()
    nodes = [
        {
            "id": "await_agent_onset", "type": "listen", "next": "settle",
            "timeout_ms": listen_timeout_ms, "max_events": 16,
            "until": trigger, "on_timeout": "giveup",
        },
        {"id": "settle", "type": "wait", "next": "barge_in", "duration_ms": delay_ms},
        {"id": "barge_in", "type": "say", "next": "done", "text": text},
        {"id": "done", "type": "hangup", "reason": "reactive_barge_in_complete"},
        {"id": "giveup", "type": "hangup", "reason": "agent_speech_onset_not_detected"},
    ]
    # max_wait_ms gates both the listen timeout and the settle wait at runtime;
    # size it to whichever the caller asked for so a legitimate delay is honored.
    max_wait_ms = max(DEFAULT_LIMITS["max_wait_ms"], delay_ms, listen_timeout_ms)
    return validate_plan({
        "schema": PLAN_SCHEMA,
        "id": plan_id,
        "mode": "scripted",
        "start": "await_agent_onset",
        "nodes": nodes,
        "limits": {"max_wait_ms": max_wait_ms},
        "metadata": {
            "kind": "reactive_barge_in",
            "reacts_to": trigger,
            "delay_ms": delay_ms,
            "listen_timeout_ms": listen_timeout_ms,
            "on_timeout": on_timeout,
        },
    })


def _json_path(value: Any, path: str) -> Any:
    current = value
    for part in path.split("."):
        if not isinstance(current, dict) or part not in current:
            return _MISSING
        current = current[part]
    return current


_MISSING = object()


def _event_value(event: Mapping[str, Any], name: str) -> Any:
    if name in event:
        return event[name]
    data = event.get("data")
    if isinstance(data, dict):
        return data.get(name, _MISSING)
    return _MISSING


def _trigger_matches(trigger: Mapping[str, Any], event: Optional[Mapping[str, Any]], state: Mapping[str, Any]) -> bool:
    if "all" in trigger:
        return all(_trigger_matches(child, event, state) for child in trigger["all"])
    if "any" in trigger:
        return any(_trigger_matches(child, event, state) for child in trigger["any"])
    if "not" in trigger:
        return not _trigger_matches(trigger["not"], event, state)
    actor = trigger.get("actor_state")
    if actor is not None and _json_path(state, actor["key"]) != actor["equals"]:
        return False
    event_fields = set(trigger) - {"actor_state"}
    if event_fields and event is None:
        return False
    if "event" in trigger and event.get("kind") != trigger["event"]:
        return False
    if "text_regex" in trigger:
        text = _event_value(event, "text")
        if not isinstance(text, str):
            return False
        if len(text) > MAX_TRIGGER_SEARCH_CHARS:
            raise _RuntimeInputError(
                "REGEX_SEARCH_TEXT_LIMIT",
                "trigger text exceeded the bounded regex search limit",
                observed=len(text), maximum=MAX_TRIGGER_SEARCH_CHARS,
            )
        if re.search(trigger["text_regex"], text, re.IGNORECASE) is None:
            return False
    pairs = {"tool": "tool", "status": "status", "digits": "digits"}
    for key, event_key in pairs.items():
        if key in trigger and _event_value(event, event_key) != trigger[key]:
            return False
    if "path" in trigger:
        data = event.get("data", event)
        observed = _json_path(data, trigger["path"])
        if "equals" in trigger and observed != trigger["equals"]:
            return False
    elif "equals" in trigger and actor is None:
        if _event_value(event, "value") != trigger["equals"]:
            return False
    if "metric" in trigger:
        if _event_value(event, "metric") != trigger["metric"]:
            return False
        observed = _event_value(event, "value")
        if isinstance(observed, bool) or not isinstance(observed, (int, float)):
            return False
        if "gte" in trigger and observed < trigger["gte"]:
            return False
        if "lte" in trigger and observed > trigger["lte"]:
            return False
    return True


def _history_matches(trigger: Mapping[str, Any], events: Sequence[Mapping[str, Any]], state: Mapping[str, Any]) -> bool:
    if set(trigger) == {"actor_state"}:
        return _trigger_matches(trigger, None, state)
    return any(_trigger_matches(trigger, event, state) for event in events)


def _validate_capabilities(raw: Mapping[str, str]) -> Dict[str, str]:
    if not isinstance(raw, Mapping):
        raise _RuntimeBlocked("CAPABILITIES_INVALID", "session capabilities must be a mapping")
    result: Dict[str, str] = {}
    for key, state in raw.items():
        if not isinstance(key, str) or state not in CAPABILITY_STATES:
            raise _RuntimeBlocked(
                "CAPABILITIES_INVALID", "invalid session capability declaration"
            )
        result[key] = state
    return result


def _require_capability(capabilities: Mapping[str, str], operation: str) -> None:
    state = capabilities.get(operation, UNSUPPORTED)
    if state != SUPPORTED:
        raise _RuntimeBlocked(
            "CAPABILITY_" + state, f"session operation {operation!r} is {state.lower()}",
            operation=operation, capability_state=state,
        )


def _validate_event(raw: Any, sequence: int) -> Dict[str, Any]:
    if not isinstance(raw, Mapping):
        raise _RuntimeBlocked("EVENT_INVALID", "session event must be a mapping")
    value = json.loads(_canonical(dict(raw)))
    if "event_sha256" in value:
        raise _RuntimeBlocked("EVENT_INVALID", "session event cannot supply event_sha256")
    kind = value.get("kind")
    if kind not in EVENT_KINDS:
        raise _RuntimeBlocked("EVENT_INVALID", "session event kind is unsupported")
    if "sequence" in value:
        value["source_sequence"] = value.pop("sequence")
    value["sequence"] = sequence
    value["event_sha256"] = _sha(_canonical(value))
    return value


class _Executor:
    def __init__(
        self,
        plan: Mapping[str, Any],
        session: CallerSession,
        output: Path,
        model: Optional[CallerModel],
        tts: Optional[CallerTTS],
        clock: Callable[[], float],
        created_at: str,
    ):
        self.plan = plan
        self.session = session
        self.output = output
        self.model = model
        self.tts = tts
        self.clock = clock
        self.created_at = created_at
        self.limits = plan["limits"]
        self.capabilities = _validate_capabilities(session.capabilities())
        self.nodes = {node["id"]: node for node in plan["nodes"]}
        self.state = json.loads(_canonical(plan["initial_state"]))
        self.events: List[Dict[str, Any]] = []
        self.actions: List[Dict[str, Any]] = []
        self.model_calls: List[Dict[str, Any]] = []
        self.visits: Dict[str, int] = {}
        self.repeats: Dict[str, int] = {}
        self.counters = {
            "steps": 0, "turns": 0, "text_chars": 0, "audio_bytes": 0,
            "event_chars": 0,
            "model_calls": 0, "model_input_chars": 0, "model_output_chars": 0,
            "tokens": 0, "cost_microusd": 0,
        }
        self.started = clock()

    def _limit(self, counter: str, increment: int, limit: str) -> None:
        value = self.counters[counter] + increment
        if value > self.limits[limit]:
            raise _RuntimeBlocked(
                "LIMIT_REACHED", f"{limit} reached", limit=limit,
                observed=value, maximum=self.limits[limit],
            )
        self.counters[counter] = value

    def _check_time(self) -> None:
        elapsed = max(0, int(round((self.clock() - self.started) * 1_000)))
        if elapsed > self.limits["max_duration_ms"]:
            raise _RuntimeBlocked(
                "LIMIT_REACHED", "max_duration_ms reached", limit="max_duration_ms",
                observed=elapsed, maximum=self.limits["max_duration_ms"],
            )

    def _artifact(self, category: str, sequence: int, suffix: str, data: bytes) -> Tuple[str, str]:
        rel = f"artifacts/{category}/{sequence:04d}.{suffix}"
        path = self.output / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        _exclusive_write_bytes(path, data)
        return rel, _sha(data)

    def _external_action(self, node_id: str, action: str, **fields: Any) -> Dict[str, Any]:
        row = {"sequence": len(self.actions) + 1, "node_id": node_id, "action": action, **fields}
        self.actions.append(row)
        return row

    def _say(self, node_id: str, text: str, source: str, model_call: Optional[int] = None) -> None:
        self._limit("turns", 1, "max_turns")
        self._limit("text_chars", len(text), "max_text_chars")
        sequence = len(self.actions) + 1
        text_path, text_sha = self._artifact("text", sequence, "txt", text.encode("utf-8"))
        fields: Dict[str, Any] = {
            "text_path": text_path, "text_sha256": text_sha, "source": source,
        }
        if model_call is not None:
            fields["model_call"] = model_call
        if self.tts is None:
            _require_capability(self.capabilities, "send_text")
            self.session.send_text(text, {"text_sha256": text_sha, "source": source})
            fields["delivery"] = "text"
        else:
            _require_capability(self.capabilities, "send_audio")
            synthesis = self.tts.synthesize(text)
            if not isinstance(synthesis, Mapping):
                raise _RuntimeBlocked("TTS_INVALID", "TTS result must be a mapping")
            allowed = {"pcm_s16le", "sample_rate_hz", "provider", "model", "voice", "settings"}
            unknown = sorted(set(synthesis) - allowed)
            if unknown:
                raise _RuntimeBlocked("TTS_INVALID", f"TTS result has unknown fields: {unknown}")
            missing = sorted(allowed - set(synthesis))
            if missing:
                raise _RuntimeBlocked("TTS_PROVENANCE_MISSING", f"TTS result is missing fields: {missing}")
            pcm = synthesis.get("pcm_s16le")
            rate = synthesis.get("sample_rate_hz")
            if not isinstance(pcm, bytes) or len(pcm) % 2:
                raise _RuntimeBlocked("TTS_INVALID", "TTS pcm_s16le must be even-length bytes")
            if isinstance(rate, bool) or not isinstance(rate, int) or not 1 <= rate <= 192_000:
                raise _RuntimeBlocked("TTS_INVALID", "TTS sample_rate_hz is invalid")
            for key in ("provider", "model", "voice"):
                if not isinstance(synthesis[key], str) or not synthesis[key]:
                    raise _RuntimeBlocked("TTS_PROVENANCE_MISSING", f"TTS {key} must be a non-empty string")
            if not isinstance(synthesis["settings"], Mapping):
                raise _RuntimeBlocked("TTS_PROVENANCE_MISSING", "TTS settings must be a mapping")
            provenance = {key: synthesis.get(key) for key in ("provider", "model", "voice", "settings")}
            try:
                provenance = json.loads(_canonical(provenance))
            except ValueError as exc:
                raise _RuntimeBlocked("TTS_INVALID", str(exc)) from exc
            self._limit("audio_bytes", len(pcm), "max_audio_bytes")
            audio_path, pcm_sha = self._artifact("audio", sequence, "pcm", pcm)
            self.session.send_audio(pcm, rate, {"pcm_sha256": pcm_sha, "encoding": "pcm_s16le"})
            fields.update({
                "delivery": "audio", "audio_path": audio_path, "pcm_sha256": pcm_sha,
                "audio_bytes": len(pcm), "sample_rate_hz": rate, "tts": provenance,
            })
        self._external_action(node_id, "say", **fields)

    def _model_proposal(self, node: Mapping[str, Any]) -> Dict[str, Any]:
        if self.model is None:
            raise _RuntimeBlocked("MODEL_REQUIRED", "generate node requires a caller model")
        self._limit("model_calls", 1, "max_model_calls")
        context = {
            "schema": "hotato.caller-model-request.v1",
            "plan_id": self.plan["id"], "node_id": node["id"], "prompt": node["prompt"],
            "allowed_actions": node["allowed_actions"], "actor_state": self.state,
            "events": self.events,
        }
        request_raw = _canonical(context)
        self._limit("model_input_chars", len(request_raw.decode("utf-8")), "max_model_input_chars")
        sequence = len(self.model_calls) + 1
        request_path, request_sha = self._artifact("model-request", sequence, "json", request_raw)
        response = self.model.propose(context)
        if not isinstance(response, Mapping):
            raise _RuntimeBlocked("MODEL_RESPONSE_INVALID", "caller model response must be a mapping")
        allowed = {"proposal", "raw", "provider", "model", "parameters", "usage"}
        unknown = sorted(set(response) - allowed)
        if unknown:
            raise _RuntimeBlocked("MODEL_RESPONSE_INVALID", f"model response has unknown fields: {unknown}")
        missing = sorted(allowed - set(response))
        if missing:
            raise _RuntimeBlocked(
                "MODEL_PROVENANCE_MISSING", f"model response is missing fields: {missing}"
            )
        try:
            response_json = json.loads(_canonical(dict(response)))
        except ValueError as exc:
            raise _RuntimeBlocked("MODEL_RESPONSE_INVALID", str(exc)) from exc
        raw_value = response_json.get("raw")
        raw_text = raw_value if isinstance(raw_value, str) else _canonical(raw_value).decode("utf-8")
        self._limit("model_output_chars", len(raw_text), "max_model_output_chars")
        for key in ("provider", "model"):
            if not isinstance(response_json[key], str) or not response_json[key]:
                raise _RuntimeBlocked("MODEL_PROVENANCE_MISSING", f"model {key} must be a non-empty string")
        if not isinstance(response_json["parameters"], dict):
            raise _RuntimeBlocked("MODEL_PROVENANCE_MISSING", "model parameters must be a mapping")
        usage = response_json.get("usage")
        if not isinstance(usage, dict) or set(usage) != {"input_tokens", "output_tokens", "cost_microusd"}:
            raise _RuntimeBlocked(
                "MODEL_USAGE_MISSING",
                "model usage must report input_tokens, output_tokens, and cost_microusd",
            )
        for key in ("input_tokens", "output_tokens", "cost_microusd"):
            if isinstance(usage[key], bool) or not isinstance(usage[key], int) or usage[key] < 0:
                raise _RuntimeBlocked("MODEL_USAGE_INVALID", f"model usage {key} must be an integer >= 0")
        self._limit("tokens", usage["input_tokens"] + usage["output_tokens"], "max_tokens")
        self._limit("cost_microusd", usage["cost_microusd"], "max_cost_microusd")
        raw_path, raw_sha = self._artifact("model", sequence, "json", _canonical(response_json))
        call = {
            "sequence": sequence, "node_id": node["id"], "status": "received",
            "request_path": request_path, "request_sha256": request_sha,
            "response_sha256": _sha(_canonical(response_json)),
            "raw_path": raw_path, "raw_sha256": raw_sha,
            "provider": response_json.get("provider"), "model": response_json.get("model"),
            "parameters": response_json.get("parameters", {}), "usage": usage,
        }
        self.model_calls.append(call)
        proposal = response_json.get("proposal")
        if not isinstance(proposal, dict):
            raise _RuntimeBlocked("MODEL_PROPOSAL_INVALID", "model proposal must be a mapping")
        action = proposal.get("action")
        if action not in node["allowed_actions"]:
            raise _RuntimeBlocked(
                "MODEL_ACTION_REFUSED", f"model proposed action {action!r} outside the node allow-list",
                proposed=action, allowed=node["allowed_actions"],
            )
        action_fields = {
            "say": {"action", "text"}, "dtmf": {"action", "digits"},
            "silence": {"action", "duration_ms"}, "hangup": {"action", "reason"},
        }[action]
        if set(proposal) != action_fields:
            raise _RuntimeBlocked(
                "MODEL_PROPOSAL_INVALID",
                f"{action} proposal must contain exactly {sorted(action_fields)}",
            )
        if any(key.lower() in _RESERVED_STATE_ROOTS for key in proposal):
            raise _RuntimeBlocked("MODEL_AUTHORITY_REFUSED", "model proposal attempted an authority field")
        if action == "say":
            _nonempty(proposal["text"], "model proposal text")
        elif action == "dtmf":
            if not isinstance(proposal["digits"], str) or not _DTMF_RE.fullmatch(proposal["digits"]):
                raise _RuntimeBlocked("MODEL_PROPOSAL_INVALID", "model proposed invalid DTMF digits")
        elif action == "silence":
            try:
                _integer(proposal["duration_ms"], "model proposal duration_ms", 0, self.limits["max_wait_ms"])
            except ValueError as exc:
                raise _RuntimeBlocked("MODEL_PROPOSAL_INVALID", str(exc)) from exc
        else:
            _nonempty(proposal["reason"], "model proposal reason", 1_000)
        call.update({"status": "accepted", "proposal": proposal})
        return proposal

    def _receive_until(self, node: Mapping[str, Any], trigger: Optional[Mapping[str, Any]]) -> bool:
        _require_capability(self.capabilities, "receive")
        timeout_ms = node["timeout_ms"]
        if timeout_ms > self.limits["max_wait_ms"]:
            raise _RuntimeBlocked(
                "LIMIT_REACHED", "max_wait_ms reached", limit="max_wait_ms",
                observed=timeout_ms, maximum=self.limits["max_wait_ms"],
            )
        for _ in range(node["max_events"]):
            if len(self.events) >= self.limits["max_events"]:
                raise _RuntimeBlocked("LIMIT_REACHED", "max_events reached", limit="max_events")
            raw = self.session.receive(timeout_ms)
            if raw is None:
                event = _validate_event({"kind": "timeout", "timeout_ms": timeout_ms, "authority": "local_timer"}, len(self.events) + 1)
                self.events.append(event)
                return False
            if not isinstance(raw, Mapping):
                raise _RuntimeBlocked("EVENT_INVALID", "session event must be a mapping")
            self._limit(
                "event_chars", len(_canonical(dict(raw)).decode("utf-8")), "max_event_chars"
            )
            event = _validate_event(raw, len(self.events) + 1)
            self.events.append(event)
            if trigger is None or _trigger_matches(trigger, event, self.state):
                return True
        return False

    def drain_session_events(self) -> None:
        """Persist transport events observed between caller receive nodes.

        Some sidecars interleave a target receipt with a command result.  The
        WebSocket session queues those events so the caller package can retain
        them even when the scenario hangs up without another ``listen`` node.
        Sessions without this optional hook keep their existing behavior.
        """

        drain = getattr(self.session, "drain_events", None)
        if not callable(drain):
            return
        raw_events = drain()
        if not isinstance(raw_events, list):
            raise RuntimeError("caller session drain_events must return a list")
        for raw in raw_events:
            if len(self.events) >= self.limits["max_events"]:
                raise _RuntimeBlocked(
                    "LIMIT_REACHED", "max_events reached", limit="max_events"
                )
            if not isinstance(raw, Mapping):
                raise RuntimeError("caller session drained a non-mapping event")
            self._limit(
                "event_chars", len(_canonical(dict(raw)).decode("utf-8")),
                "max_event_chars",
            )
            self.events.append(_validate_event(raw, len(self.events) + 1))

    def execute(self) -> Tuple[str, Optional[Dict[str, Any]]]:
        current: Optional[str] = self.plan["start"]
        while current is not None:
            self._check_time()
            self.counters["steps"] += 1
            if self.counters["steps"] > self.limits["max_steps"]:
                raise _RuntimeBlocked("LIMIT_REACHED", "max_steps reached", limit="max_steps")
            node = self.nodes[current]
            self.visits[current] = self.visits.get(current, 0) + 1
            if self.visits[current] > self.limits["max_visits_per_node"]:
                raise _RuntimeBlocked(
                    "LIMIT_REACHED", "max_visits_per_node reached",
                    limit="max_visits_per_node", node_id=current,
                )
            node_type = node["type"]
            next_id = node.get("next")
            if node_type == "say":
                self._say(current, node["text"], "script")
            elif node_type == "generate":
                proposal = self._model_proposal(node)
                action = proposal["action"]
                model_call = len(self.model_calls)
                if action == "say":
                    self._say(current, proposal["text"], "model_proposal", model_call)
                elif action == "dtmf":
                    _require_capability(self.capabilities, "send_dtmf")
                    self._limit("turns", 1, "max_turns")
                    self.session.send_dtmf(proposal["digits"])
                    self._external_action(current, "dtmf", digits=proposal["digits"], source="model_proposal", model_call=model_call)
                elif action == "silence":
                    _require_capability(self.capabilities, "silence")
                    self.session.silence(proposal["duration_ms"])
                    self._external_action(current, "silence", duration_ms=proposal["duration_ms"], source="model_proposal", model_call=model_call)
                else:
                    _require_capability(self.capabilities, "hangup")
                    self.session.hangup(proposal["reason"])
                    self._external_action(current, "hangup", reason=proposal["reason"], source="model_proposal", model_call=model_call)
                    self._check_time()
                    return "HUNG_UP", None
            elif node_type == "listen":
                matched = self._receive_until(node, node.get("until"))
                if not matched:
                    next_id = node.get("on_timeout", next_id)
            elif node_type == "wait":
                if node["duration_ms"] > self.limits["max_wait_ms"]:
                    raise _RuntimeBlocked("LIMIT_REACHED", "max_wait_ms reached", limit="max_wait_ms")
                _require_capability(self.capabilities, "wait")
                self.session.wait(node["duration_ms"])
                self._external_action(current, "wait", duration_ms=node["duration_ms"])
            elif node_type == "dtmf":
                _require_capability(self.capabilities, "send_dtmf")
                self._limit("turns", 1, "max_turns")
                self.session.send_dtmf(node["digits"])
                self._external_action(current, "dtmf", digits=node["digits"], source="script")
            elif node_type == "silence":
                if node["duration_ms"] > self.limits["max_wait_ms"]:
                    raise _RuntimeBlocked("LIMIT_REACHED", "max_wait_ms reached", limit="max_wait_ms")
                _require_capability(self.capabilities, "silence")
                self.session.silence(node["duration_ms"])
                self._external_action(current, "silence", duration_ms=node["duration_ms"], source="script")
            elif node_type == "impairment":
                _require_capability(self.capabilities, "impairment")
                self.session.set_impairment(node["profile"])
                self._external_action(current, "impairment", profile=node["profile"])
            elif node_type == "expect":
                if not _history_matches(node["when"], self.events, self.state):
                    next_id = node.get("on_miss")
                    if next_id is None:
                        raise _RuntimeBlocked("EXPECTATION_MISSED", f"expect node {current!r} did not match")
            elif node_type == "set_state":
                target = self.state
                parts = node["key"].split(".")
                for part in parts[:-1]:
                    child = target.setdefault(part, {})
                    if not isinstance(child, dict):
                        raise _RuntimeBlocked("ACTOR_STATE_CONFLICT", f"actor state path {node['key']!r} conflicts")
                    target = child
                target[parts[-1]] = node["value"]
            elif node_type == "branch":
                next_id = node.get("default")
                for case in node["cases"]:
                    if _history_matches(case["when"], self.events, self.state):
                        next_id = case["next"]
                        break
                if next_id is None:
                    raise _RuntimeBlocked("BRANCH_UNMATCHED", f"branch node {current!r} has no match or default")
            elif node_type == "repeat_bounded":
                count = self.repeats.get(current, 0)
                if count < node["max_iterations"]:
                    self.repeats[current] = count + 1
                    next_id = node["target"]
            elif node_type == "transfer_expect":
                capability = self.capabilities.get("observe_transfer", UNSUPPORTED)
                if capability != SUPPORTED:
                    raise _RuntimeBlocked(
                        "CAPABILITY_" + capability,
                        f"transfer observation is {capability.lower()}",
                        operation="observe_transfer", capability_state=capability,
                    )
                matched = self._receive_until(node, {"event": "transfer", "status": "completed"})
                if not matched:
                    next_id = node.get("on_miss")
                    if next_id is None:
                        raise _RuntimeBlocked("TRANSFER_UNOBSERVED", "completed transfer was not observed")
            elif node_type == "hangup":
                _require_capability(self.capabilities, "hangup")
                self.session.hangup(node["reason"])
                self._external_action(current, "hangup", reason=node["reason"], source="script")
                self._check_time()
                return "HUNG_UP", None
            self._check_time()
            current = next_id
        return "COMPLETED", None


def _write_result_package(output: Path, result: Dict[str, Any]) -> Dict[str, Any]:
    unsigned = dict(result)
    unsigned.pop("result_id", None)
    result["result_id"] = _sha(_canonical(unsigned))
    _exclusive_write_bytes(output / "caller-result.json", _canonical(result) + b"\n")
    files: Dict[str, Dict[str, Any]] = {}
    entries = 0
    for path in sorted(output.rglob("*")):
        entries += 1
        if entries > _MAX_PACKAGE_ENTRIES:
            raise ValueError("caller package exceeds its entry limit")
        if path.is_symlink():
            raise ValueError("caller package cannot contain symbolic links")
        if not path.is_file() or path.name == "package-manifest.json":
            continue
        rel = path.relative_to(output).as_posix()
        data = _read_regular_bytes_no_follow(
            path, max_bytes=_MAX_PACKAGE_ARTIFACT_BYTES
        )
        files[rel] = {"bytes": len(data), "sha256": _sha(data)}
    manifest = {"schema": PACKAGE_SCHEMA, "result_id": result["result_id"], "files": files}
    manifest["package_id"] = _sha(_canonical(manifest))
    _exclusive_write_bytes(
        output / "package-manifest.json", _canonical(manifest) + b"\n"
    )
    return manifest


def verify_package(output_dir: str) -> Dict[str, Any]:
    root = Path(os.path.abspath(output_dir))
    errors: List[Dict[str, Any]] = []
    try:
        root_info = os.lstat(root)
    except OSError as exc:
        return {
            "ok": False,
            "errors": [{"code": "PACKAGE_ROOT_UNREADABLE", "message": str(exc)}],
        }
    if stat.S_ISLNK(root_info.st_mode) or not stat.S_ISDIR(root_info.st_mode):
        return {"ok": False, "errors": [{"code": "PACKAGE_ROOT_INVALID"}]}
    manifest_path = root / "package-manifest.json"
    if manifest_path.is_symlink():
        return {"ok": False, "errors": [{"code": "MANIFEST_SYMLINK"}]}
    try:
        manifest = json.loads(
            _read_regular_bytes_no_follow(
                manifest_path, max_bytes=_MAX_PACKAGE_JSON_BYTES
            ).decode("utf-8"),
            parse_constant=lambda value: (_ for _ in ()).throw(ValueError(value)),
        )
    except (OSError, ValueError) as exc:
        return {"ok": False, "errors": [{"code": "MANIFEST_UNREADABLE", "message": str(exc)}]}
    if not isinstance(manifest, dict) or manifest.get("schema") != PACKAGE_SCHEMA:
        return {"ok": False, "errors": [{"code": "MANIFEST_SCHEMA"}]}
    claimed_package = manifest.get("package_id")
    package_unsigned = dict(manifest)
    package_unsigned.pop("package_id", None)
    try:
        if claimed_package != _sha(_canonical(package_unsigned)):
            errors.append({"code": "PACKAGE_ID_MISMATCH"})
    except ValueError as exc:
        errors.append({"code": "MANIFEST_INVALID", "message": str(exc)})
    files = manifest.get("files")
    if not isinstance(files, dict) or len(files) > _MAX_PACKAGE_ENTRIES:
        return {"ok": False, "errors": errors + [{"code": "FILES_INVALID"}]}
    observed = set()
    entries = 0
    for path in root.rglob("*"):
        entries += 1
        if entries > _MAX_PACKAGE_ENTRIES:
            errors.append({"code": "PACKAGE_ENTRY_LIMIT"})
            break
        if path.is_symlink():
            errors.append(
                {
                    "code": "UNEXPECTED_SYMLINK",
                    "path": path.relative_to(root).as_posix(),
                }
            )
        elif path.is_file() and path.name != "package-manifest.json":
            observed.add(path.relative_to(root).as_posix())
    expected = set()
    for rel in files:
        if (
            not isinstance(rel, str) or not rel or rel.startswith("/") or "\\" in rel
            or any(part in {"", ".", ".."} for part in rel.split("/"))
        ):
            errors.append({"code": "FILE_PATH_INVALID", "path": repr(rel)})
        else:
            expected.add(rel)
    for rel in sorted(expected | observed):
        path = root / rel
        try:
            resolved = path.resolve()
            resolved.relative_to(root)
        except (OSError, ValueError):
            errors.append({"code": "PATH_ESCAPE", "path": rel})
            continue
        if rel not in expected:
            errors.append({"code": "UNEXPECTED_FILE", "path": rel})
            continue
        if rel not in observed or path.is_symlink():
            errors.append({"code": "FILE_MISSING_OR_SYMLINK", "path": rel})
            continue
        try:
            data = _read_regular_bytes_no_follow(
                path, max_bytes=_MAX_PACKAGE_ARTIFACT_BYTES
            )
        except (OSError, ValueError) as exc:
            errors.append({
                "code": "FILE_UNREADABLE_OR_CHANGED", "path": rel,
                "message": str(exc),
            })
            continue
        row = files[rel]
        if not isinstance(row, dict) or row.get("bytes") != len(data) or row.get("sha256") != _sha(data):
            errors.append({"code": "FILE_DIGEST_MISMATCH", "path": rel})
    try:
        result = json.loads(
            _read_regular_bytes_no_follow(
                root / "caller-result.json", max_bytes=_MAX_PACKAGE_JSON_BYTES
            ).decode("utf-8"),
            parse_constant=lambda value: (_ for _ in ()).throw(ValueError(value)),
        )
        unsigned = dict(result)
        claimed_result = unsigned.pop("result_id", None)
        if result.get("schema") != RESULT_SCHEMA or claimed_result != _sha(_canonical(unsigned)):
            errors.append({"code": "RESULT_ID_MISMATCH"})
        if manifest.get("result_id") != claimed_result:
            errors.append({"code": "RESULT_MANIFEST_MISMATCH"})
    except (OSError, ValueError, AttributeError) as exc:
        errors.append({"code": "RESULT_UNREADABLE", "message": str(exc)})
    return {"ok": not errors, "package_id": claimed_package, "errors": errors}


def _read_bound_source(
    source: Path, rel: Any, expected_sha256: Any, required_prefix: str
) -> bytes:
    if (
        not isinstance(rel, str) or not rel or rel.startswith("/") or "\\" in rel
        or any(part in {"", ".", ".."} for part in rel.split("/"))
    ):
        raise _RuntimeBlocked("FROZEN_ARTIFACT_INVALID", "frozen artifact path is unsafe")
    if not rel.startswith(required_prefix + "/"):
        raise _RuntimeBlocked(
            "FROZEN_ARTIFACT_INVALID",
            f"frozen artifact must be inside {required_prefix}/",
        )
    path = source / rel
    try:
        path.resolve().relative_to(source)
    except (OSError, ValueError) as exc:
        raise _RuntimeBlocked("FROZEN_ARTIFACT_INVALID", "frozen artifact path escapes the package") from exc
    if path.is_symlink() or not path.is_file():
        raise _RuntimeBlocked("FROZEN_ARTIFACT_INVALID", f"frozen artifact {rel!r} is unavailable")
    try:
        data = _read_regular_bytes_no_follow(
            path, max_bytes=_MAX_PACKAGE_ARTIFACT_BYTES
        )
    except (OSError, ValueError) as exc:
        raise _RuntimeBlocked(
            "FROZEN_ARTIFACT_INVALID", f"frozen artifact {rel!r} is unavailable"
        ) from exc
    if not isinstance(expected_sha256, str) or _sha(data) != expected_sha256:
        raise _RuntimeBlocked("FROZEN_ARTIFACT_MISMATCH", f"frozen artifact {rel!r} changed")
    return data


def _new_result(plan: Mapping[str, Any], created_at: str, capabilities: Mapping[str, str]) -> Dict[str, Any]:
    return {
        "schema": RESULT_SCHEMA, "plan_id": plan["id"], "mode": plan["mode"],
        "created_at": created_at, "status": "RUNNING", "exit_code": 1,
        "capabilities": dict(capabilities), "actions": [], "events": [], "model_calls": [],
        "actor_state": {}, "limits": plan.get("limits", {}), "counters": {},
        "authority": {
            "caller_model": "proposal_only", "actor_state": "scenario_input",
            "outcome": "not_evaluated", "verdict": "not_produced",
        },
    }


def _replay_frozen(
    plan: Mapping[str, Any], session: CallerSession, output: Path, created_at: str
) -> Dict[str, Any]:
    source = Path(os.path.abspath(plan["frozen_package"]))
    verification = verify_package(str(source))
    try:
        capabilities = _validate_capabilities(session.capabilities())
    except _RuntimeBlocked as exc:
        result = _new_result(plan, created_at, {})
        result.update({
            "status": "BLOCKED", "error": {
                "code": exc.code, "message": str(exc), "details": exc.details,
            },
        })
        return result
    except Exception as exc:
        result = _new_result(plan, created_at, {})
        result.update({
            "status": "ERROR",
            "error": _external_error("SESSION_OR_ADAPTER_ERROR", exc),
        })
        return result
    result = _new_result(plan, created_at, capabilities)
    result["source_package_id"] = verification.get("package_id")
    if not verification["ok"]:
        result.update({
            "status": "BLOCKED", "error": {
                "code": "FROZEN_PACKAGE_INVALID", "message": "source caller package failed verification",
                "details": {"errors": verification["errors"]},
            },
        })
        return result
    try:
        source_result = json.loads(
            _read_regular_bytes_no_follow(
                source / "caller-result.json", max_bytes=_MAX_PACKAGE_JSON_BYTES
            ).decode("utf-8")
        )
        if not isinstance(source_result, dict) or source_result.get("status") not in {"COMPLETED", "HUNG_UP"}:
            raise _RuntimeBlocked(
                "FROZEN_RESULT_INVALID", "source caller run must be completed before replay"
            )
        originals = source_result.get("actions")
        if not isinstance(originals, list):
            raise _RuntimeBlocked("FROZEN_RESULT_INVALID", "source actions must be a list")
        if len(originals) > plan["limits"]["max_steps"]:
            raise _RuntimeBlocked(
                "LIMIT_REACHED", "max_steps reached during frozen replay",
                limit="max_steps", observed=len(originals), maximum=plan["limits"]["max_steps"],
            )
    except _RuntimeBlocked as exc:
        result.update({"status": "BLOCKED", "error": {"code": exc.code, "message": str(exc), "details": exc.details}})
        return result
    except (OSError, ValueError, AttributeError) as exc:
        result.update({"status": "BLOCKED", "error": {"code": "FROZEN_RESULT_INVALID", "message": str(exc), "details": {}}})
        return result
    audio_bytes = 0
    text_chars = 0
    turns = 0
    for original in originals:
        try:
            if not isinstance(original, dict):
                raise _RuntimeBlocked("FROZEN_ACTION_INVALID", "frozen action must be a mapping")
            action = original.get("action")
            replayed: Dict[str, Any] = {
                "sequence": len(result["actions"]) + 1,
                "node_id": original.get("node_id"), "action": action, "source": "frozen_replay",
                "source_sequence": original.get("sequence"),
            }
            if action == "say":
                if original.get("delivery") == "audio":
                    _require_capability(capabilities, "send_audio")
                    rel = original["audio_path"]
                    pcm = _read_bound_source(
                        source, rel, original.get("pcm_sha256"), "artifacts/audio"
                    )
                    if len(pcm) % 2:
                        raise _RuntimeBlocked("FROZEN_ACTION_INVALID", "frozen PCM must have an even byte length")
                    rate = original.get("sample_rate_hz")
                    if isinstance(rate, bool) or not isinstance(rate, int) or not 1 <= rate <= 192_000:
                        raise _RuntimeBlocked("FROZEN_ACTION_INVALID", "frozen sample rate is invalid")
                    text_data = _read_bound_source(
                        source, original.get("text_path"), original.get("text_sha256"),
                        "artifacts/text",
                    )
                    text = text_data.decode("utf-8")
                    audio_bytes += len(pcm)
                    text_chars += len(text)
                    turns += 1
                    if audio_bytes > plan["limits"]["max_audio_bytes"]:
                        raise _RuntimeBlocked("LIMIT_REACHED", "max_audio_bytes reached", limit="max_audio_bytes")
                    if text_chars > plan["limits"]["max_text_chars"] or turns > plan["limits"]["max_turns"]:
                        raise _RuntimeBlocked("LIMIT_REACHED", "frozen caller text/turn limit reached")
                    destination = output / f"artifacts/audio/{len(result['actions']) + 1:04d}.pcm"
                    destination.parent.mkdir(parents=True, exist_ok=True)
                    _exclusive_write_bytes(destination, pcm)
                    text_destination = output / f"artifacts/text/{len(result['actions']) + 1:04d}.txt"
                    text_destination.parent.mkdir(parents=True, exist_ok=True)
                    _exclusive_write_bytes(text_destination, text_data)
                    session.send_audio(pcm, rate, {"pcm_sha256": original["pcm_sha256"], "source": "frozen_replay"})
                    replayed.update({
                        "delivery": "audio", "audio_path": destination.relative_to(output).as_posix(),
                        "pcm_sha256": original["pcm_sha256"], "audio_bytes": len(pcm),
                        "sample_rate_hz": rate, "tts": original.get("tts"),
                        "text_path": text_destination.relative_to(output).as_posix(),
                        "text_sha256": original["text_sha256"],
                    })
                else:
                    _require_capability(capabilities, "send_text")
                    rel = original["text_path"]
                    data = _read_bound_source(
                        source, rel, original.get("text_sha256"), "artifacts/text"
                    )
                    text = data.decode("utf-8")
                    text_chars += len(text)
                    turns += 1
                    if text_chars > plan["limits"]["max_text_chars"] or turns > plan["limits"]["max_turns"]:
                        raise _RuntimeBlocked("LIMIT_REACHED", "frozen caller text/turn limit reached")
                    destination = output / f"artifacts/text/{len(result['actions']) + 1:04d}.txt"
                    destination.parent.mkdir(parents=True, exist_ok=True)
                    _exclusive_write_bytes(destination, data)
                    session.send_text(text, {"text_sha256": original["text_sha256"], "source": "frozen_replay"})
                    replayed.update({
                        "delivery": "text", "text_path": destination.relative_to(output).as_posix(),
                        "text_sha256": original["text_sha256"],
                    })
            elif action == "dtmf":
                _require_capability(capabilities, "send_dtmf")
                if not isinstance(original.get("digits"), str) or not _DTMF_RE.fullmatch(original["digits"]):
                    raise _RuntimeBlocked("FROZEN_ACTION_INVALID", "frozen DTMF digits are invalid")
                turns += 1
                if turns > plan["limits"]["max_turns"]:
                    raise _RuntimeBlocked("LIMIT_REACHED", "max_turns reached", limit="max_turns")
                session.send_dtmf(original["digits"])
                replayed["digits"] = original["digits"]
            elif action in {"wait", "silence"}:
                _require_capability(capabilities, action)
                duration = original.get("duration_ms")
                if isinstance(duration, bool) or not isinstance(duration, int) or not 0 <= duration <= plan["limits"]["max_wait_ms"]:
                    raise _RuntimeBlocked("LIMIT_REACHED", "frozen wait exceeds max_wait_ms", limit="max_wait_ms")
                getattr(session, action)(original["duration_ms"])
                replayed["duration_ms"] = original["duration_ms"]
            elif action == "impairment":
                _require_capability(capabilities, "impairment")
                if not isinstance(original.get("profile"), dict) or not original["profile"]:
                    raise _RuntimeBlocked("FROZEN_ACTION_INVALID", "frozen impairment profile is invalid")
                session.set_impairment(original["profile"])
                replayed["profile"] = original["profile"]
            elif action == "hangup":
                _require_capability(capabilities, "hangup")
                if not isinstance(original.get("reason"), str) or not original["reason"]:
                    raise _RuntimeBlocked("FROZEN_ACTION_INVALID", "frozen hangup reason is invalid")
                session.hangup(original["reason"])
                replayed["reason"] = original["reason"]
            else:
                raise _RuntimeBlocked("FROZEN_ACTION_INVALID", f"cannot replay action {action!r}")
        except (KeyError, UnicodeDecodeError) as exc:
            blocked = _RuntimeBlocked("FROZEN_ACTION_INVALID", f"invalid frozen action: {exc}")
            result.update({
                "status": "BLOCKED", "actions": result["actions"],
                "error": {"code": blocked.code, "message": str(blocked), "details": {}},
            })
            return result
        except _RuntimeBlocked as exc:
            result.update({
                "status": "BLOCKED", "actions": result["actions"],
                "error": {"code": exc.code, "message": str(exc), "details": exc.details},
            })
            return result
        except Exception as exc:
            result.update({
                "status": "ERROR", "actions": result["actions"],
                "error": _external_error("SESSION_OR_ADAPTER_ERROR", exc),
            })
            return result
        result["actions"].append(replayed)
    final_status = "HUNG_UP" if result["actions"] and result["actions"][-1]["action"] == "hangup" else "COMPLETED"
    result.update({
        "status": final_status, "exit_code": 0, "frozen_action_count": len(result["actions"]),
        "model_calls": [], "events": [], "actor_state": {},
        "counters": {
            "actions": len(result["actions"]), "turns": turns,
            "text_chars": text_chars, "audio_bytes": audio_bytes,
        },
    })
    return result


def run_caller(
    plan_value: Mapping[str, Any],
    session: CallerSession,
    output_dir: str,
    *,
    model: Optional[CallerModel] = None,
    tts: Optional[CallerTTS] = None,
    clock: Callable[[], float] = time.monotonic,
    created_at: Optional[str] = None,
) -> CallerRun:
    """Execute a caller plan and write a content-addressed local package.

    Plan validation errors raise ``ValueError`` before any session operation.
    Runtime limits, capability gaps, invalid model output, and adapter errors
    produce a nonzero, verifiable result instead of a fabricated pass.
    """

    plan = validate_plan(dict(plan_value))
    if plan["mode"] == "generative" and model is None:
        raise ValueError("generative mode requires a caller model")
    output = _empty_output_root(output_dir)
    timestamp = created_at or time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    _exclusive_write_bytes(output / "caller-plan.json", _canonical(plan) + b"\n")
    if plan["mode"] == "frozen_replay":
        result = _replay_frozen(plan, session, output, timestamp)
    else:
        try:
            executor = _Executor(plan, session, output, model, tts, clock, timestamp)
        except _RuntimeBlocked as exc:
            result = _new_result(plan, timestamp, {})
            result.update({
                "status": "BLOCKED", "error": {
                    "code": exc.code, "message": str(exc), "details": exc.details,
                },
            })
        except Exception as exc:
            result = _new_result(plan, timestamp, {})
            result.update({
                "status": "ERROR",
                "error": _external_error("SESSION_OR_ADAPTER_ERROR", exc),
            })
        else:
            result = _new_result(plan, timestamp, executor.capabilities)
            try:
                status, _ = executor.execute()
                executor.drain_session_events()
                result.update({"status": status, "exit_code": 0})
            except _RuntimeInputError as exc:
                result.update({
                    "status": "ERROR", "exit_code": 1,
                    "error": {
                        "code": exc.code, "message": str(exc),
                        "details": exc.details,
                    },
                })
            except _RuntimeBlocked as exc:
                result.update({
                    "status": "BLOCKED", "error": {
                        "code": exc.code, "message": str(exc), "details": exc.details,
                    },
                })
            except Exception as exc:  # adapter boundary: preserve evidence, never call it a pass
                result.update({
                    "status": "ERROR",
                    "error": _external_error("SESSION_OR_ADAPTER_ERROR", exc),
                })
            elapsed = max(0, int(round((clock() - executor.started) * 1_000)))
            result.update({
                "actions": executor.actions, "events": executor.events,
                "model_calls": executor.model_calls, "actor_state": executor.state,
                "counters": {**executor.counters, "elapsed_ms": elapsed},
                "visits": executor.visits, "repeat_counts": executor.repeats,
            })
    evidence = getattr(session, "evidence", None)
    if callable(evidence):
        try:
            boundary = evidence()
            if not isinstance(boundary, dict):
                raise TypeError("session evidence must be a mapping")
            _canonical(boundary)
            result["session_boundary"] = boundary
        except Exception as exc:
            result.update({
                "status": "ERROR",
                "exit_code": 1,
                "error": _external_error("SESSION_EVIDENCE_INVALID", exc),
            })
    else:
        result["session_boundary"] = {
            "availability": UNOBSERVABLE,
            "reason": "the caller session did not expose command receipts",
        }
    _write_result_package(output, result)
    verification = verify_package(str(output))
    return CallerRun(str(output), result, verification)


__all__ = [
    "PLAN_SCHEMA", "RESULT_SCHEMA", "PACKAGE_SCHEMA", "SUPPORTED", "UNSUPPORTED",
    "UNOBSERVABLE", "CAPABILITY_STATES", "MODES", "NODE_TYPES", "CallerSession",
    "CallerModel", "CallerTTS", "OllamaCallerModel", "CallerRun", "validate_plan", "load_plan",
    "run_caller", "verify_package", "MAX_TRIGGER_REGEX_CHARS",
    "MAX_TRIGGER_SEARCH_CHARS", "AGENT_SPEECH_ONSET_STATUS",
    "agent_speech_started_event", "agent_speech_onset_trigger", "reactive_barge_in_plan",
]
