"""Durable, local production evidence ingestion and regression promotion.

This module is deliberately an operational *evidence plane*, not a hosted
analytics claim.  Its invariants are small enough to audit:

* authenticate the exact request bytes before JSON parsing;
* commit a canonical/redacted event to SQLite before acknowledging it;
* deduplicate equal event identities and refuse conflicting identities;
* retain late/out-of-order arrival as evidence instead of silently sorting it;
* keep availability and execution authority separate for every evidence lane;
* expose only bounded-label Prometheus metrics;
* promote a session into a content-addressed, offline-verifiable regression
  candidate without requiring access to the live database.

The gateway is suitable for one self-hosted Hotato process.  It does not claim
carrier or distributed-streaming scale.  Deployments needing a replicated
ingress should put a durable collector/queue in front of this process and keep
this module as the evidence assembler.
"""

from __future__ import annotations

import datetime as _datetime
import hashlib
import hmac
import ipaddress
import json
import math
import os
import re
import shutil
import socket
import sqlite3
import stat
import tempfile
import threading
import time
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence, Tuple

from .errors import rename_no_replace, safe_json_dumps

__all__ = [
    "EVENT_TYPES",
    "EVIDENCE_LANES",
    "ProductionError",
    "EventConflict",
    "ProductionStore",
    "ProductionGateway",
    "validate_event",
    "normalize_otlp_json",
    "verify_regression_candidate",
]

EVENT_TYPES = frozenset(
    {
        "session.started",
        "session.ended",
        "participant.joined",
        "participant.left",
        "transcript.segment",
        "media.asset.available",
        "transport.sample",
        "turn.started",
        "turn.ended",
        "model.operation",
        "tool.requested",
        "tool.result",
        "state.snapshot",
        "capture.completed",
        "evaluation.completed",
        "incident.created",
    }
)
SESSION_STATES = frozenset(
    {"OPEN", "QUIESCENT", "COMPLETE", "DEGRADED", "EXPIRED", "DELETED"}
)
EVIDENCE_LANES = (
    "participant_audio",
    "transcript",
    "model_trace",
    "tool_calls",
    "backend_state",
)
_LANE_EVENT = {
    "media.asset.available": "participant_audio",
    "transcript.segment": "transcript",
    "model.operation": "model_trace",
    "tool.result": "tool_calls",
    "state.snapshot": "backend_state",
}
_AUTHORITY_RANK = {
    "submitted": 0,
    "adapter_reported": 1,
    "provider_export": 2,
    "signed_attestation": 3,
    "measured": 4,
}
_MAX_EVENT_BYTES = 8 * 1024 * 1024
_MAX_OTLP_EVENTS = 100_000
_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/-]{0,499}$")
_TRACEPARENT = re.compile(r"^00-[0-9a-f]{32}-[0-9a-f]{16}-[0-9a-f]{2}$")


class ProductionError(RuntimeError):
    """Base production-plane error."""


class EventConflict(ProductionError):
    """The same ``(source, id)`` arrived with different canonical bytes."""


def _canonical(value: Any) -> bytes:
    return (
        safe_json_dumps(
            value, sort_keys=True, separators=(",", ":"), ensure_ascii=False
        )
        + "\n"
    ).encode("utf-8")


def _sha(value: bytes) -> str:
    return "sha256:" + hashlib.sha256(value).hexdigest()


def _strict_json_loads(raw: Any) -> Any:
    """Decode JSON without accepting duplicate keys or non-finite numbers."""

    def object_from_pairs(pairs: List[Tuple[str, Any]]) -> Dict[str, Any]:
        output: Dict[str, Any] = {}
        for key, value in pairs:
            if key in output:
                raise ValueError(f"duplicate JSON object key: {key!r}")
            output[key] = value
        return output

    return json.loads(
        raw,
        object_pairs_hook=object_from_pairs,
        parse_constant=lambda value: (_ for _ in ()).throw(
            ValueError(f"non-finite JSON number: {value}")
        ),
    )


_MAX_REGRESSION_MANIFEST_BYTES = 64 * 1024 * 1024
_MAX_REGRESSION_ARTIFACT_BYTES = 1024 * 1024 * 1024


def _prepare_private_sqlite_file(path: str) -> Tuple[int, Tuple[int, int]]:
    """Create/open the SQLite main file privately and hold an identity guard.

    SQLite inherits the main database's mode for WAL/SHM sidecars.  Creating
    the file before connecting prevents a normal ``0022`` umask from making
    production evidence world-readable.  The held descriptor and post-connect
    identity check also close the common pathname-swap window.
    """

    parent = os.path.dirname(path) or "."
    os.makedirs(parent, exist_ok=True)
    flags = os.O_RDWR | getattr(os, "O_BINARY", 0)
    flags |= getattr(os, "O_NONBLOCK", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        before = os.lstat(path)
    except FileNotFoundError:
        descriptor = os.open(path, flags | os.O_CREAT | os.O_EXCL, 0o600)
        created = True
        before = os.fstat(descriptor)
    else:
        created = False
        if not stat.S_ISREG(before.st_mode):
            raise ProductionError("production database must be a regular non-symlink file")
        descriptor = os.open(path, flags)
    try:
        opened = os.fstat(descriptor)
        if (
            not stat.S_ISREG(opened.st_mode)
            or (opened.st_dev, opened.st_ino) != (before.st_dev, before.st_ino)
        ):
            raise ProductionError("production database changed while it was opened")
        if getattr(opened, "st_nlink", 1) != 1:
            raise ProductionError("production database must not have hard links")
        if os.name == "posix":
            os.fchmod(descriptor, 0o600)
        os.fsync(descriptor)
        if created and os.name != "nt":
            _fsync_directory(parent)
        return descriptor, (opened.st_dev, opened.st_ino)
    except BaseException:
        os.close(descriptor)
        raise


def _read_regular_bytes_no_follow(path: str, *, max_bytes: int) -> bytes:
    """Read a bounded regular file without a pathname-swap/FIFO window.

    Candidate directories are portable, untrusted inputs.  A pre-open
    ``lstat`` alone is insufficient because the path can be replaced before a
    later builtin ``open``.  Nonblocking/no-follow flags prevent FIFO hangs
    and symlink traversal; ``fstat`` then proves that the opened descriptor is
    the same regular inode inspected before the open.
    """

    if isinstance(max_bytes, bool) or not isinstance(max_bytes, int) or max_bytes < 0:
        raise ValueError("max_bytes must be a non-negative integer")
    before = os.lstat(path)
    if not stat.S_ISREG(before.st_mode):
        raise ValueError(f"{path!r} must be a regular file")
    if before.st_size > max_bytes:
        raise ValueError(f"{path!r} exceeds the {max_bytes}-byte read limit")

    flags = os.O_RDONLY
    flags |= getattr(os, "O_BINARY", 0)
    flags |= getattr(os, "O_NONBLOCK", 0)
    flags |= getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(path, flags)
    try:
        opened = os.fstat(descriptor)
        if not stat.S_ISREG(opened.st_mode):
            raise ValueError(f"{path!r} must be a regular file")
        if (before.st_dev, before.st_ino) != (opened.st_dev, opened.st_ino):
            raise ValueError(f"{path!r} changed while it was being opened")
        if opened.st_size > max_bytes:
            raise ValueError(f"{path!r} exceeds the {max_bytes}-byte read limit")
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
            raise ValueError(f"{path!r} exceeds the {max_bytes}-byte read limit")
        return value
    finally:
        os.close(descriptor)


def _fsync_directory(path: str) -> None:
    # Directory-entry durability is a POSIX affordance: os.open of a DIRECTORY
    # raises PermissionError on Windows (the CRT open cannot take a directory),
    # and NTFS metadata durability is handled by the volume. Guard inside the
    # helper so every call site is covered.
    if os.name == "nt":
        return
    descriptor = os.open(path, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _utc_rfc3339_from_nanos(value: Any) -> str:
    try:
        nanos = int(value)
    except (TypeError, ValueError):
        raise ValueError("OTLP timestamp must be integer nanoseconds")
    if nanos < 0:
        raise ValueError("OTLP timestamp must be non-negative")
    seconds, remainder = divmod(nanos, 1_000_000_000)
    try:
        moment = _datetime.datetime.fromtimestamp(
            seconds, tz=_datetime.timezone.utc
        )
    except (OverflowError, OSError, ValueError) as exc:
        raise ValueError("OTLP timestamp is outside the supported datetime range") from exc
    return moment.strftime("%Y-%m-%dT%H:%M:%S") + ".%09dZ" % remainder


# Fractional seconds, anchored to an hh:mm:ss field so nothing else in the
# string can match.
_RFC3339_FRACTION = re.compile(r"(\d{2}:\d{2}:\d{2})\.(\d+)")


def _six_digit_fraction(match: "re.Match[str]") -> str:
    # Exactly six digits: truncated beyond microseconds (the 3.11+
    # ``fromisoformat`` semantics) and zero-padded below.  Length never
    # affects 3.11+ acceptance, so 3.11+ behavior is unchanged.
    return match.group(1) + "." + match.group(2)[:6].ljust(6, "0")


def _parse_rfc3339(value: str) -> _datetime.datetime:
    """Parse an RFC3339 timestamp identically on every supported interpreter.

    ``datetime.fromisoformat`` on Python 3.11+ accepts fractional seconds of
    any length (truncating beyond microseconds), but Python 3.9/3.10 demand
    exactly three or six digits -- and OTLP emitters produce nanosecond
    fractions such as ``1970-01-01T00:00:01.000000000Z``.  Normalize the two
    strict-RFC3339 shapes older interpreters cannot read -- a trailing ``Z``
    and a fractional-seconds run that is not exactly six digits -- then
    delegate, so 3.9/3.10 accept exactly the RFC3339 strings 3.11+ accepts
    and every other candidate string is judged by ``fromisoformat`` unchanged.
    """

    normalized = value[:-1] + "+00:00" if value.endswith("Z") else value
    normalized = _RFC3339_FRACTION.sub(_six_digit_fraction, normalized, count=1)
    return _datetime.datetime.fromisoformat(normalized)


def _validate_timestamp(value: Any) -> str:
    if not isinstance(value, str) or not value:
        raise ValueError("time must be a non-empty RFC3339 timestamp")
    try:
        parsed = _parse_rfc3339(value)
    except ValueError:
        raise ValueError("time must be an RFC3339 timestamp")
    if parsed.tzinfo is None:
        raise ValueError("time must include a timezone")
    return value


def _validate_json_tree(value: Any) -> None:
    """Bound nesting/node count and refuse non-JSON Python values."""

    stack: List[Tuple[Any, int]] = [(value, 0)]
    nodes = 0
    while stack:
        item, depth = stack.pop()
        nodes += 1
        if nodes > 200_000:
            raise ValueError("event data exceeds 200000 JSON nodes")
        if depth > 64:
            raise ValueError("event data exceeds maximum nesting depth 64")
        if isinstance(item, dict):
            if any(not isinstance(key, str) for key in item):
                raise ValueError("event data object keys must be strings")
            stack.extend((child, depth + 1) for child in item.values())
        elif isinstance(item, list):
            stack.extend((child, depth + 1) for child in item)
        elif item is None or isinstance(item, (str, bool, int)):
            continue
        elif isinstance(item, float) and math.isfinite(item):
            continue
        else:
            raise ValueError("event data must contain only finite JSON values")


def validate_event(value: Any) -> Dict[str, Any]:
    """Validate and canonicalize one CloudEvents-shaped Hotato event.

    ``authority`` is deliberately required.  Authentication proves who sent
    bytes to the gateway; it does not upgrade provider- or adapter-reported
    execution evidence into an independently measured fact.
    """

    if not isinstance(value, dict):
        raise ValueError("production event must be a mapping")
    required = {
        "specversion",
        "id",
        "source",
        "type",
        "subject",
        "time",
        "data",
        "authority",
    }
    allowed = required | {
        "datacontenttype",
        "dataschema",
        "traceparent",
        "sequence",
    }
    missing = sorted(required - set(value))
    unknown = sorted(set(value) - allowed)
    if missing:
        raise ValueError("production event missing: " + ", ".join(missing))
    if unknown:
        raise ValueError("production event unknown fields: " + ", ".join(unknown))
    if value["specversion"] != "1.0":
        raise ValueError("specversion must be 1.0")
    for name in ("id", "source", "subject"):
        if not isinstance(value[name], str) or not _ID.fullmatch(value[name]):
            raise ValueError(f"{name} must be a bounded event identifier")
    if value["type"] not in EVENT_TYPES:
        raise ValueError("unsupported production event type")
    _validate_timestamp(value["time"])
    if not isinstance(value["data"], dict):
        raise ValueError("data must be a mapping")
    _validate_json_tree(value["data"])
    availability = value["data"].get("availability")
    if availability is not None and availability not in (
        "available",
        "unavailable",
        "unsupported",
    ):
        raise ValueError(
            "data.availability must be available, unavailable, or unsupported"
        )
    authority = value["authority"]
    if not isinstance(authority, dict) or set(authority) != {
        "kind",
        "eligible_for_execution_claim",
    }:
        raise ValueError(
            "authority must contain kind and eligible_for_execution_claim"
        )
    if authority["kind"] not in _AUTHORITY_RANK:
        raise ValueError("unsupported authority kind")
    eligible = authority["kind"] in ("measured", "signed_attestation")
    if authority["eligible_for_execution_claim"] is not eligible:
        raise ValueError("execution-claim eligibility contradicts authority kind")
    sequence = value.get("sequence")
    if sequence is not None and (
        isinstance(sequence, bool) or not isinstance(sequence, int) or sequence < 0
    ):
        raise ValueError("sequence must be an integer >= 0")
    traceparent = value.get("traceparent")
    if traceparent is not None and (
        not isinstance(traceparent, str) or not _TRACEPARENT.fullmatch(traceparent)
    ):
        raise ValueError("traceparent must use W3C version 00 syntax")
    raw = _canonical(value)
    if len(raw) > _MAX_EVENT_BYTES:
        raise ValueError("production event exceeds 8 MiB")
    return json.loads(raw.decode("utf-8"))


def _is_nonnegative_number(value: Any) -> bool:
    return (
        not isinstance(value, bool)
        and isinstance(value, (int, float))
        and math.isfinite(value)
        and value >= 0
    )


def _is_nonnegative_integer(value: Any) -> bool:
    return not isinstance(value, bool) and isinstance(value, int) and value >= 0


def _is_confidence(value: Any) -> bool:
    return _is_nonnegative_number(value) and value <= 1


def _is_boolean(value: Any) -> bool:
    return isinstance(value, bool)


def _is_availability(value: Any) -> bool:
    return value in ("available", "unavailable", "unsupported")


def _is_sha256(value: Any) -> bool:
    return isinstance(value, str) and bool(
        re.fullmatch(r"(?:sha256:)?[0-9a-f]{64}", value)
    )


def _is_span_id(value: Any) -> bool:
    return value is None or (
        isinstance(value, str) and bool(re.fullmatch(r"[0-9a-f]{16}", value))
    )


def _is_unix_nanos(value: Any) -> bool:
    if _is_nonnegative_integer(value):
        return True
    return (
        isinstance(value, str)
        and 1 <= len(value) <= 32
        and value.isascii()
        and value.isdigit()
    )


def _is_exit_code(value: Any) -> bool:
    return (
        not isinstance(value, bool)
        and isinstance(value, int)
        and -255 <= value <= 255
    )


def _enum_validator(*allowed: str):
    values = frozenset(allowed)

    def validate(value: Any) -> bool:
        return isinstance(value, str) and value in values

    return validate


_MEDIA_TYPE = _enum_validator(
    "audio/flac",
    "audio/mpeg",
    "audio/ogg",
    "audio/wav",
    "audio/webm",
    "audio/x-wav",
)
_AUDIO_CODEC = _enum_validator(
    "alaw",
    "flac",
    "g711_alaw",
    "g711_ulaw",
    "linear16",
    "mulaw",
    "ogg",
    "opus",
    "pcm16",
    "pcm_s16le",
    "wav",
)
_SAMPLE_FORMAT = _enum_validator(
    "alaw", "float32", "mulaw", "pcm_f32le", "pcm_s16le", "s16le"
)
_RESULT_STATUS = _enum_validator(
    "cancelled",
    "complete",
    "completed",
    "error",
    "failed",
    "pass",
    "passed",
    "pending",
    "running",
    "success",
    "succeeded",
    "timeout",
)
_INCIDENT_SEVERITY = _enum_validator("critical", "high", "medium", "low")


# Default-deny persistence policy.  The event envelope, session evidence lanes,
# and original-payload digest retain identity and monitoring structure.  Only
# fields below have sufficiently narrow value types to be safe and useful in a
# portable regression candidate.  A familiar-looking field on another event
# type is not implicitly trusted.
_SAFE_DATA_FIELDS_BY_EVENT_TYPE: Dict[str, Dict[str, Any]] = {
    event_type: {"availability": _is_availability} for event_type in EVENT_TYPES
}

for _event_type in ("media.asset.available", "capture.completed"):
    _SAFE_DATA_FIELDS_BY_EVENT_TYPE[_event_type].update(
        {
            "audio_sha256": _is_sha256,
            "byte_count": _is_nonnegative_integer,
            "bytes": _is_nonnegative_integer,
            "channels": _is_nonnegative_integer,
            "codec": _AUDIO_CODEC,
            "content_sha256": _is_sha256,
            "delivered_audio_sha256": _is_sha256,
            "duration_ms": _is_nonnegative_number,
            "frame_count": _is_nonnegative_integer,
            "media_type": _MEDIA_TYPE,
            "pcm_sha256": _is_sha256,
            "sample_format": _SAMPLE_FORMAT,
            "sample_rate_hz": _is_nonnegative_integer,
            "sample_width_bytes": _is_nonnegative_integer,
            "sha256": _is_sha256,
        }
    )

for _event_type in ("session.ended", "turn.started", "turn.ended"):
    _SAFE_DATA_FIELDS_BY_EVENT_TYPE[_event_type].update(
        {
            "duration_ms": _is_nonnegative_number,
            "overlap_ms": _is_nonnegative_number,
            "yield_latency_ms": _is_nonnegative_number,
        }
    )

_SAFE_DATA_FIELDS_BY_EVENT_TYPE["transcript.segment"].update(
    {
        "confidence": _is_confidence,
        "duration_ms": _is_nonnegative_number,
        "end_ms": _is_nonnegative_number,
        "final": _is_boolean,
        "start_ms": _is_nonnegative_number,
    }
)

for _event_type in ("model.operation", "tool.requested", "tool.result"):
    _SAFE_DATA_FIELDS_BY_EVENT_TYPE[_event_type].update(
        {
            "attempt": _is_nonnegative_integer,
            "duration_ms": _is_nonnegative_number,
            "input_tokens": _is_nonnegative_integer,
            "latency_ms": _is_nonnegative_number,
            "output_tokens": _is_nonnegative_integer,
            "retry_count": _is_nonnegative_integer,
            "status": _RESULT_STATUS,
            "status_code": _is_nonnegative_integer,
            "success": _is_boolean,
        }
    )

_SAFE_DATA_FIELDS_BY_EVENT_TYPE["state.snapshot"].update(
    {
        "content_sha256": _is_sha256,
        "sha256": _is_sha256,
        "version": _is_nonnegative_integer,
    }
)

_SAFE_DATA_FIELDS_BY_EVENT_TYPE["transport.sample"].update(
    {
        "byte_count": _is_nonnegative_integer,
        "bytes": _is_nonnegative_integer,
        "duration_ms": _is_nonnegative_number,
        "end_time_unix_nano": _is_unix_nanos,
        "latency_ms": _is_nonnegative_number,
        "parent_span_id": _is_span_id,
        "retry_count": _is_nonnegative_integer,
        "span_id": _is_span_id,
        "start_time_unix_nano": _is_unix_nanos,
        "status_code": _is_nonnegative_integer,
    }
)

_SAFE_DATA_FIELDS_BY_EVENT_TYPE["evaluation.completed"].update(
    {
        "duration_ms": _is_nonnegative_number,
        "exit_code": _is_exit_code,
        "passed": _is_boolean,
        "status": _RESULT_STATUS,
    }
)

_SAFE_DATA_FIELDS_BY_EVENT_TYPE["incident.created"].update(
    {
        "severity": _INCIDENT_SEVERITY,
        "status": _RESULT_STATUS,
    }
)


def _redaction_descriptor(value: Any) -> Dict[str, Any]:
    raw = _canonical(value)
    return {
        "redacted": True,
        "byte_count": len(raw),
    }


def _redact(value: Mapping[str, Any], *, event_type: str) -> Dict[str, Any]:
    """Return the default-deny persisted projection for one event payload.

    Unknown scalars and containers are both reduced as a whole to a canonical
    byte count and digest.  No recursive walk can accidentally expose a new
    nested key.  Allowlisted fields still have strict validators; a value that
    does not match its safe structural type is reduced to the same descriptor.
    """

    safe_fields = _SAFE_DATA_FIELDS_BY_EVENT_TYPE.get(event_type, {})
    output: Dict[str, Any] = {}
    for key, item in value.items():
        validator = safe_fields.get(key)
        output[key] = item if validator and validator(item) else _redaction_descriptor(item)
    return output


def _empty_evidence() -> Dict[str, Dict[str, Any]]:
    return {
        lane: {
            "availability": "missing",
            "authority": "unavailable",
            "eligible_for_execution_claim": False,
            "event_ids": [],
        }
        for lane in EVIDENCE_LANES
    }


def _attribute_value(value: Any) -> Any:
    """Decode the JSON representation of one OTLP AnyValue."""

    if not isinstance(value, dict):
        raise ValueError("OTLP attribute value must be an AnyValue object")
    supported = (
        "stringValue",
        "boolValue",
        "intValue",
        "doubleValue",
        "bytesValue",
        "arrayValue",
        "kvlistValue",
    )
    present = [key for key in supported if key in value]
    if len(present) != 1 or set(value) != set(present):
        raise ValueError("OTLP AnyValue must contain exactly one supported value")
    key = present[0]
    result = value[key]
    if key == "stringValue" or key == "bytesValue":
        if not isinstance(result, str):
            raise ValueError(f"OTLP {key} must be a string")
        return result
    if key == "boolValue":
        if not isinstance(result, bool):
            raise ValueError("OTLP boolValue must be a boolean")
        return result
    if key == "intValue":
        if isinstance(result, bool) or not isinstance(result, (str, int)):
            raise ValueError("OTLP intValue must be an integer string")
        try:
            return int(result)
        except ValueError as exc:
            raise ValueError("OTLP intValue must be an integer string") from exc
    if key == "doubleValue":
        if isinstance(result, bool) or not isinstance(result, (int, float)):
            raise ValueError("OTLP doubleValue must be a finite number")
        if not math.isfinite(float(result)):
            raise ValueError("OTLP doubleValue must be a finite number")
        return result
    if key == "arrayValue":
        if not isinstance(result, dict) or set(result) - {"values"}:
            raise ValueError("OTLP arrayValue must contain a values array")
        values = result.get("values", [])
        if not isinstance(values, list):
            raise ValueError("OTLP arrayValue.values must be a list")
        return [_attribute_value(item) for item in values]
    if not isinstance(result, dict) or set(result) - {"values"}:
        raise ValueError("OTLP kvlistValue must contain a values array")
    values = result.get("values", [])
    return _otlp_attributes(values)


def _otlp_attributes(values: Any) -> Dict[str, Any]:
    if not isinstance(values, list):
        raise ValueError("OTLP attributes must be a list")
    output: Dict[str, Any] = {}
    for item in values:
        if (
            not isinstance(item, dict)
            or set(item) != {"key", "value"}
            or not isinstance(item.get("key"), str)
            or not item["key"]
        ):
            raise ValueError("each OTLP attribute must contain key and value")
        if item["key"] in output:
            raise ValueError(f"duplicate OTLP attribute: {item['key']!r}")
        output[item["key"]] = _attribute_value(item["value"])
    return output


def normalize_otlp_json(
    payload: Any,
    *,
    source: str,
    authority_kind: str = "adapter_reported",
) -> List[Dict[str, Any]]:
    """Normalize OTLP/HTTP JSON trace spans into production events.

    This is a bounded trace correlation bridge, not a complete OTLP receiver.
    It accepts the standard ``resourceSpans/scopeSpans/spans`` JSON shape.
    Each span must carry one of ``hotato.session_id``, ``session.id``,
    ``conversation.id``, or ``call.id``.  A producer can set
    ``hotato.event_type`` to a supported event type; otherwise the span remains
    a ``transport.sample``.  Unknown OTLP fields stay outside the authority
    decision.
    """

    if authority_kind not in _AUTHORITY_RANK:
        raise ValueError("unsupported authority kind")
    if not isinstance(source, str) or not _ID.fullmatch(source):
        raise ValueError("source must be a bounded event identifier")
    if not isinstance(payload, dict) or not isinstance(
        payload.get("resourceSpans"), list
    ):
        raise ValueError("OTLP payload must contain resourceSpans")
    eligible = authority_kind in ("measured", "signed_attestation")
    output: List[Dict[str, Any]] = []
    for resource_group in payload["resourceSpans"]:
        if not isinstance(resource_group, dict):
            raise ValueError("each OTLP resourceSpans entry must be an object")
        resource = resource_group.get("resource", {})
        if not isinstance(resource, dict):
            raise ValueError("OTLP resource must be an object")
        resource_attributes = _otlp_attributes(resource.get("attributes", []))
        scope_spans = resource_group.get("scopeSpans", [])
        if not isinstance(scope_spans, list):
            raise ValueError("OTLP scopeSpans must be a list")
        for scope_group in scope_spans:
            if not isinstance(scope_group, dict):
                raise ValueError("each OTLP scopeSpans entry must be an object")
            scope = scope_group.get("scope", {})
            if not isinstance(scope, dict):
                raise ValueError("OTLP scope must be an object")
            spans = scope_group.get("spans", [])
            if not isinstance(spans, list):
                raise ValueError("OTLP spans must be a list")
            for span in spans:
                if len(output) >= _MAX_OTLP_EVENTS:
                    raise ValueError("OTLP payload exceeds 100000 spans")
                if not isinstance(span, dict):
                    raise ValueError("each OTLP span must be an object")
                attributes = dict(resource_attributes)
                attributes.update(_otlp_attributes(span.get("attributes", [])))
                subject = next(
                    (
                        attributes[key]
                        for key in (
                            "hotato.session_id",
                            "session.id",
                            "conversation.id",
                            "call.id",
                        )
                        if isinstance(attributes.get(key), str)
                        and _ID.fullmatch(attributes[key])
                    ),
                    None,
                )
                if subject is None:
                    raise ValueError("each OTLP span must carry a bounded session id")
                trace_id = str(span.get("traceId", "")).lower()
                span_id = str(span.get("spanId", "")).lower()
                if not re.fullmatch(r"[0-9a-f]{32}", trace_id) or not re.fullmatch(
                    r"[0-9a-f]{16}", span_id
                ):
                    raise ValueError("OTLP span traceId/spanId must be lowercase hex")
                event_type = attributes.get("hotato.event_type", "transport.sample")
                if event_type not in EVENT_TYPES:
                    event_type = "transport.sample"
                sequence = attributes.get("hotato.sequence")
                if isinstance(sequence, str) and sequence.isdigit():
                    sequence = int(sequence)
                data = {
                    "span_name": str(span.get("name", ""))[:500],
                    "span_id": span_id,
                    "parent_span_id": span.get("parentSpanId") or None,
                    "start_time_unix_nano": str(span.get("startTimeUnixNano", "")),
                    "end_time_unix_nano": str(span.get("endTimeUnixNano", "")),
                    "status": span.get("status", {}),
                    "attributes": attributes,
                    "resource": resource_attributes,
                    "scope": {
                        "name": scope.get("name"),
                        "version": scope.get("version"),
                    },
                }
                availability = attributes.get("hotato.evidence.availability")
                if availability in ("available", "unavailable", "unsupported"):
                    data["availability"] = availability
                event: Dict[str, Any] = {
                    "specversion": "1.0",
                    "id": f"otel-{trace_id}-{span_id}",
                    "source": source,
                    "type": event_type,
                    "subject": subject,
                    "time": _utc_rfc3339_from_nanos(span.get("startTimeUnixNano")),
                    "datacontenttype": "application/json",
                    "traceparent": f"00-{trace_id}-{span_id}-01",
                    "data": data,
                    "authority": {
                        "kind": authority_kind,
                        "eligible_for_execution_claim": eligible,
                    },
                }
                if isinstance(sequence, int) and not isinstance(sequence, bool):
                    event["sequence"] = sequence
                output.append(validate_event(event))
    return output


@dataclass(frozen=True)
class AuditVerification:
    valid: bool
    entries: int
    first_invalid_sequence: Optional[int]
    head_sha256: str


class ProductionStore:
    """A single-process durable event store backed by SQLite WAL."""

    def __init__(self, path: str, *, clock=time.time) -> None:
        self.path = os.path.abspath(path)
        self.clock = clock
        self._lock = threading.RLock()
        guard, expected_identity = _prepare_private_sqlite_file(self.path)
        try:
            self.db = sqlite3.connect(
                self.path, check_same_thread=False, timeout=30, isolation_level=None
            )
            observed = os.stat(self.path, follow_symlinks=False)
            if (observed.st_dev, observed.st_ino) != expected_identity:
                self.db.close()
                raise ProductionError(
                    "production database changed while SQLite was opening it"
                )
        finally:
            os.close(guard)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute("PRAGMA synchronous=FULL")
        self.db.execute("PRAGMA secure_delete=ON")
        self.db.execute("PRAGMA foreign_keys=ON")
        self.db.execute("PRAGMA busy_timeout=30000")
        self._schema()
        self._verify_schema_layout()

    def _schema(self) -> None:
        with self._lock:
            self.db.executescript(
                """
                BEGIN IMMEDIATE;
                CREATE TABLE IF NOT EXISTS metadata(
                  key TEXT PRIMARY KEY,
                  value TEXT NOT NULL
                );
                INSERT OR IGNORE INTO metadata(key,value)
                  VALUES('production_schema_version','1');
                CREATE TABLE IF NOT EXISTS events(
                  source TEXT NOT NULL,
                  event_id TEXT NOT NULL,
                  subject TEXT NOT NULL,
                  type TEXT NOT NULL,
                  source_time TEXT NOT NULL,
                  received REAL NOT NULL,
                  sequence INTEGER,
                  traceparent TEXT,
                  payload_sha256 TEXT NOT NULL,
                  stored_sha256 TEXT NOT NULL,
                  redacted INTEGER NOT NULL,
                  payload_json TEXT NOT NULL,
                  PRIMARY KEY(source,event_id)
                );
                CREATE INDEX IF NOT EXISTS events_subject_received
                  ON events(subject,received,source,event_id);
                CREATE TABLE IF NOT EXISTS conflicts(
                  id INTEGER PRIMARY KEY AUTOINCREMENT,
                  source TEXT NOT NULL,
                  event_id TEXT NOT NULL,
                  subject TEXT NOT NULL,
                  first_sha256 TEXT NOT NULL,
                  second_sha256 TEXT NOT NULL,
                  received REAL NOT NULL
                );
                CREATE TABLE IF NOT EXISTS sessions(
                  subject TEXT PRIMARY KEY,
                  state TEXT NOT NULL,
                  started REAL,
                  ended REAL,
                  last_event REAL NOT NULL,
                  finalized_at REAL,
                  event_count INTEGER NOT NULL DEFAULT 0,
                  duplicate_count INTEGER NOT NULL DEFAULT 0,
                  conflict_count INTEGER NOT NULL DEFAULT 0,
                  out_of_order_count INTEGER NOT NULL DEFAULT 0,
                  unsequenced_count INTEGER NOT NULL DEFAULT 0,
                  highest_sequence INTEGER,
                  evidence_json TEXT NOT NULL,
                  required_evidence_json TEXT,
                  finalization_reason TEXT
                );
                CREATE TABLE IF NOT EXISTS sequence_cursors(
                  subject TEXT NOT NULL,
                  source TEXT NOT NULL,
                  highest_sequence INTEGER NOT NULL,
                  PRIMARY KEY(subject,source)
                );
                CREATE TABLE IF NOT EXISTS alerts(
                  id TEXT PRIMARY KEY,
                  subject TEXT NOT NULL,
                  rule_id TEXT NOT NULL,
                  state TEXT NOT NULL,
                  opened REAL NOT NULL,
                  updated REAL NOT NULL,
                  generation INTEGER NOT NULL DEFAULT 1,
                  observed_json TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS alert_transitions(
                  sequence INTEGER PRIMARY KEY AUTOINCREMENT,
                  alert_id TEXT NOT NULL,
                  at REAL NOT NULL,
                  previous_state TEXT,
                  state TEXT NOT NULL,
                  observed_json TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS deletion_receipts(
                  id TEXT PRIMARY KEY,
                  subject_sha256 TEXT NOT NULL,
                  deleted_at REAL NOT NULL,
                  receipt_json TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS counters(
                  name TEXT PRIMARY KEY,
                  value INTEGER NOT NULL
                );
                INSERT OR IGNORE INTO counters(name,value) VALUES
                  ('events',0),('duplicates',0),('conflicts',0),('out_of_order',0);
                CREATE TABLE IF NOT EXISTS audit(
                  sequence INTEGER PRIMARY KEY AUTOINCREMENT,
                  at REAL NOT NULL,
                  action TEXT NOT NULL,
                  target TEXT NOT NULL,
                  before_sha256 TEXT,
                  after_sha256 TEXT,
                  result TEXT NOT NULL,
                  previous_sha256 TEXT NOT NULL,
                  chain_sha256 TEXT NOT NULL
                );
                COMMIT;
                """
            )

    def _verify_schema_layout(self) -> None:
        required = {
            "events": {
                "source", "event_id", "subject", "type", "source_time",
                "received", "sequence", "traceparent", "payload_sha256",
                "stored_sha256", "redacted", "payload_json",
            },
            "sessions": {
                "subject", "state", "started", "ended", "last_event",
                "finalized_at", "event_count", "duplicate_count",
                "conflict_count", "out_of_order_count", "unsequenced_count",
                "highest_sequence", "evidence_json", "required_evidence_json",
                "finalization_reason",
            },
            "audit": {
                "sequence", "at", "action", "target", "before_sha256",
                "after_sha256", "result", "previous_sha256", "chain_sha256",
            },
        }
        for table, expected in required.items():
            observed = {
                row[1] for row in self.db.execute(f"PRAGMA table_info({table})")
            }
            missing = sorted(expected - observed)
            if missing:
                self.db.close()
                raise ProductionError(
                    "incompatible production database schema; back up the "
                    f"database and migrate before opening it (table {table} "
                    f"missing: {', '.join(missing)})"
                )

    def _begin(self) -> None:
        self.db.execute("BEGIN IMMEDIATE")

    def _commit(self) -> None:
        self.db.execute("COMMIT")

    def _rollback(self) -> None:
        if self.db.in_transaction:
            self.db.execute("ROLLBACK")

    def _audit(
        self,
        action: str,
        target: str,
        before: Optional[str],
        after: Optional[str],
        result: str,
    ) -> None:
        row = self.db.execute(
            "SELECT chain_sha256 FROM audit ORDER BY sequence DESC LIMIT 1"
        ).fetchone()
        previous = row[0] if row else "sha256:" + "0" * 64
        # Keep the raw provider subject or event identity out of the audit row.
        # This deterministic digest is an integrity identifier, not an
        # anonymity guarantee for low-entropy input.
        target_digest = _sha(target.encode("utf-8"))
        body = {
            "at": round(float(self.clock()), 6),
            "action": action,
            "target": target_digest,
            "before_sha256": before,
            "after_sha256": after,
            "result": result,
            "previous": previous,
        }
        chain = _sha(_canonical(body))
        self.db.execute(
            "INSERT INTO audit(at,action,target,before_sha256,after_sha256,result,previous_sha256,chain_sha256) VALUES(?,?,?,?,?,?,?,?)",
            (
                body["at"],
                action,
                target_digest,
                before,
                after,
                result,
                previous,
                chain,
            ),
        )
        self.db.execute(
            "INSERT INTO metadata(key,value) VALUES('audit_head_sha256',?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (chain,),
        )
        self.db.execute(
            "INSERT INTO metadata(key,value) VALUES('audit_entry_count','1') ON CONFLICT(key) DO UPDATE SET value=CAST(value AS INTEGER)+1"
        )

    def _increment_counter(self, name: str) -> None:
        self.db.execute(
            "UPDATE counters SET value=value+1 WHERE name=?", (name,)
        )

    def ingest(
        self, event_value: Any, *, redact_payloads: bool = True
    ) -> Dict[str, Any]:
        """Persist one event and return only after the FULL transaction commits."""

        prepared = self._prepare_event(event_value, redact_payloads=redact_payloads)
        with self._lock:
            self._begin()
            try:
                result = self._ingest_prepared(prepared)
                self._commit()
            except BaseException:
                self._rollback()
                raise
        if result["status"] == "conflict":
            raise EventConflict("same event identity arrived with different bytes")
        return result

    def _prepare_event(
        self, event_value: Any, *, redact_payloads: bool
    ) -> Tuple[Dict[str, Any], str, str, bytes, float, bool]:
        event = validate_event(event_value)
        original = _canonical(event)
        digest = _sha(original)
        stored = dict(event)
        if redact_payloads:
            stored["data"] = _redact(stored["data"], event_type=event["type"])
        stored_bytes = _canonical(stored)
        stored_digest = _sha(stored_bytes)
        now = float(self.clock())
        return event, digest, stored_digest, stored_bytes, now, redact_payloads

    def _ingest_prepared(
        self, prepared: Tuple[Dict[str, Any], str, str, bytes, float, bool]
    ) -> Dict[str, Any]:
        event, digest, stored_digest, stored_bytes, now, redacted = prepared
        key = f"{event['source']}#{event['id']}"
        existing = self.db.execute(
            "SELECT payload_sha256,subject FROM events WHERE source=? AND event_id=?",
            (event["source"], event["id"]),
        ).fetchone()
        if existing:
            if existing["payload_sha256"] == digest:
                self.db.execute(
                    "UPDATE sessions SET duplicate_count=duplicate_count+1 WHERE subject=?",
                    (existing["subject"],),
                )
                self._audit("event.duplicate", key, digest, digest, "deduplicated")
                self._increment_counter("duplicates")
                return {
                    "status": "duplicate",
                    "sha256": digest,
                    "subject": existing["subject"],
                    "durability": "committed",
                }
            self.db.execute(
                "INSERT INTO conflicts(source,event_id,subject,first_sha256,second_sha256,received) VALUES(?,?,?,?,?,?)",
                (
                    event["source"],
                    event["id"],
                    existing["subject"],
                    existing["payload_sha256"],
                    digest,
                    now,
                ),
            )
            conflicted_event = dict(event)
            conflicted_event["subject"] = existing["subject"]
            self._touch_session(conflicted_event, now, conflict=True)
            self._audit(
                "event.conflict",
                key,
                existing["payload_sha256"],
                digest,
                "refused",
            )
            self._increment_counter("conflicts")
            return {
                "status": "conflict",
                "sha256": digest,
                "subject": existing["subject"],
                "durability": "committed",
            }
        arrival = self._touch_session(event, now, conflict=False)
        self.db.execute(
            "INSERT INTO events(source,event_id,subject,type,source_time,received,sequence,traceparent,payload_sha256,stored_sha256,redacted,payload_json) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                event["source"],
                event["id"],
                event["subject"],
                event["type"],
                event["time"],
                now,
                event.get("sequence"),
                event.get("traceparent"),
                digest,
                stored_digest,
                int(redacted),
                stored_bytes.decode("utf-8"),
            ),
        )
        self._audit("event.ingest", key, None, digest, arrival)
        self._increment_counter("events")
        if arrival == "out_of_order":
            self._increment_counter("out_of_order")
        return {
            "status": arrival,
            "sha256": digest,
            "stored_sha256": stored_digest,
            "subject": event["subject"],
            "durability": "committed",
        }

    def ingest_many(
        self, events: Sequence[Any], *, redact_payloads: bool = True
    ) -> List[Dict[str, Any]]:
        """Atomically commit a bounded event batch.

        Identity conflicts are durable per-event results; the existing event is
        never overwritten.  Any storage failure rolls the whole batch back.
        """

        if len(events) > _MAX_OTLP_EVENTS:
            raise ValueError("event batch exceeds 100000 entries")
        prepared = [
            self._prepare_event(value, redact_payloads=redact_payloads)
            for value in events
        ]
        with self._lock:
            self._begin()
            try:
                results = [self._ingest_prepared(item) for item in prepared]
                self._commit()
            except BaseException:
                self._rollback()
                raise
        return results

    def ingest_otlp(
        self,
        payload: Any,
        *,
        source: str,
        authority_kind: str = "adapter_reported",
        redact_payloads: bool = True,
    ) -> List[Dict[str, Any]]:
        events = normalize_otlp_json(
            payload, source=source, authority_kind=authority_kind
        )
        return self.ingest_many(events, redact_payloads=redact_payloads)

    def _touch_session(
        self, event: Mapping[str, Any], now: float, *, conflict: bool
    ) -> str:
        subject = str(event["subject"])
        event_type = str(event["type"])
        row = self.db.execute(
            "SELECT * FROM sessions WHERE subject=?", (subject,)
        ).fetchone()
        evidence = json.loads(row["evidence_json"]) if row else _empty_evidence()
        state = row["state"] if row else "OPEN"
        started = row["started"] if row else None
        ended = row["ended"] if row else None
        finalized_at = row["finalized_at"] if row else None
        finalization_reason = row["finalization_reason"] if row else None
        sequence = event.get("sequence")
        highest = row["highest_sequence"] if row else None
        out_of_order = False
        if conflict:
            unsequenced = row["unsequenced_count"] if row else 0
        elif sequence is None:
            unsequenced = (row["unsequenced_count"] if row else 0) + 1
        else:
            unsequenced = row["unsequenced_count"] if row else 0
            cursor = self.db.execute(
                "SELECT highest_sequence FROM sequence_cursors WHERE subject=? AND source=?",
                (subject, event["source"]),
            ).fetchone()
            source_highest = cursor[0] if cursor else None
            if source_highest is not None and sequence <= source_highest:
                out_of_order = True
            new_source_highest = (
                sequence if source_highest is None else max(source_highest, sequence)
            )
            self.db.execute(
                "INSERT INTO sequence_cursors(subject,source,highest_sequence) VALUES(?,?,?) ON CONFLICT(subject,source) DO UPDATE SET highest_sequence=excluded.highest_sequence",
                (subject, event["source"], new_source_highest),
            )
            highest = sequence if highest is None else max(highest, sequence)
        out_count = (row["out_of_order_count"] if row else 0) + int(out_of_order)

        if event_type == "session.started" and not conflict:
            started = now
        if event_type == "session.ended" and not conflict:
            ended = now
            state = "QUIESCENT"
        lane = _LANE_EVENT.get(event_type)
        if lane and not conflict:
            item = evidence[lane]
            availability = event["data"].get("availability", "available")
            authority = event["authority"]["kind"]
            # The latest source observation owns current lane availability.
            # Every contributing event id remains listed, so a downgrade is
            # visible rather than erasing the earlier observation.
            item["availability"] = availability
            if availability == "available":
                if _AUTHORITY_RANK[authority] >= _AUTHORITY_RANK.get(
                    item.get("authority"), -1
                ):
                    item["authority"] = authority
                    item["eligible_for_execution_claim"] = event["authority"][
                        "eligible_for_execution_claim"
                    ]
            else:
                item["authority"] = "unavailable"
                item["eligible_for_execution_claim"] = False
            item["event_ids"] = sorted(
                set(item.get("event_ids", [])) | {str(event["id"])}
            )
        if conflict:
            state = "DEGRADED"
            finalization_reason = "event_identity_conflict"
        elif finalized_at is not None:
            state = "DEGRADED"
            finalization_reason = "late_event_after_finalization"
        count = (row["event_count"] if row else 0) + (0 if conflict else 1)
        conflicts = (row["conflict_count"] if row else 0) + int(conflict)
        duplicates = row["duplicate_count"] if row else 0
        self.db.execute(
            """
            INSERT INTO sessions(
              subject,state,started,ended,last_event,finalized_at,event_count,
              duplicate_count,conflict_count,out_of_order_count,
              unsequenced_count,highest_sequence,evidence_json,
              required_evidence_json,finalization_reason
            ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            ON CONFLICT(subject) DO UPDATE SET
              state=excluded.state,started=excluded.started,ended=excluded.ended,
              last_event=excluded.last_event,finalized_at=excluded.finalized_at,
              event_count=excluded.event_count,
              duplicate_count=excluded.duplicate_count,
              conflict_count=excluded.conflict_count,
              out_of_order_count=excluded.out_of_order_count,
              unsequenced_count=excluded.unsequenced_count,
              highest_sequence=excluded.highest_sequence,
              evidence_json=excluded.evidence_json,
              required_evidence_json=excluded.required_evidence_json,
              finalization_reason=excluded.finalization_reason
            """,
            (
                subject,
                state,
                started,
                ended,
                now,
                finalized_at,
                count,
                duplicates,
                conflicts,
                out_count,
                unsequenced,
                highest,
                safe_json_dumps(evidence, sort_keys=True),
                row["required_evidence_json"] if row else None,
                finalization_reason,
            ),
        )
        return "out_of_order" if out_of_order else "stored"

    def finalize(
        self,
        *,
        quiescence_seconds: float = 30,
        now: Optional[float] = None,
        required_lanes: Sequence[str] = EVIDENCE_LANES,
    ) -> List[Dict[str, Any]]:
        """Finalize ended sessions after a declared quiescence window."""

        if quiescence_seconds < 0:
            raise ValueError("quiescence_seconds must be >= 0")
        required = tuple(required_lanes)
        if not required or len(set(required)) != len(required) or any(
            lane not in EVIDENCE_LANES for lane in required
        ):
            raise ValueError(
                "required_lanes must be a non-empty unique subset of EVIDENCE_LANES"
            )
        now_value = float(self.clock() if now is None else now)
        output: List[Dict[str, Any]] = []
        with self._lock:
            self._begin()
            try:
                rows = self.db.execute(
                    "SELECT * FROM sessions WHERE finalized_at IS NULL AND state IN ('OPEN','QUIESCENT','DEGRADED') ORDER BY subject"
                ).fetchall()
                finalized_subjects: List[str] = []
                for row in rows:
                    if row["ended"] is None or now_value - row["last_event"] < quiescence_seconds:
                        continue
                    evidence = json.loads(row["evidence_json"])
                    lifecycle_counts = {
                        item["type"]: item["count"]
                        for item in self.db.execute(
                            "SELECT type,COUNT(*) AS count FROM events WHERE subject=? AND type IN ('session.started','session.ended') GROUP BY type",
                            (row["subject"],),
                        ).fetchall()
                    }
                    lifecycle_ambiguous = (
                        lifecycle_counts.get("session.started", 0) != 1
                        or lifecycle_counts.get("session.ended", 0) != 1
                    )
                    availability_missing = any(
                        evidence[lane]["availability"] != "available"
                        for lane in required
                    )
                    degraded = bool(
                        row["conflict_count"]
                        or row["out_of_order_count"]
                        or row["unsequenced_count"]
                        or lifecycle_ambiguous
                        or availability_missing
                    )
                    state = "DEGRADED" if degraded else "COMPLETE"
                    reason = (
                        "evidence_incomplete_or_ambiguous"
                        if degraded
                        else "session_end_plus_quiescence"
                    )
                    self.db.execute(
                        "UPDATE sessions SET state=?,finalized_at=?,required_evidence_json=?,finalization_reason=? WHERE subject=?",
                        (
                            state,
                            now_value,
                            safe_json_dumps(list(required), sort_keys=True),
                            reason,
                            row["subject"],
                        ),
                    )
                    self._audit(
                        "session.finalize", row["subject"], row["state"], state, reason
                    )
                    finalized_subjects.append(row["subject"])
                self._commit()
                output = [self.manifest(subject) for subject in finalized_subjects]
            except BaseException:
                self._rollback()
                raise
        return output

    def manifest(self, subject: str) -> Dict[str, Any]:
        with self._lock:
            row = self.db.execute(
                "SELECT * FROM sessions WHERE subject=?", (subject,)
            ).fetchone()
            if not row:
                raise KeyError(subject)
            events = self.db.execute(
                "SELECT stored_sha256,redacted FROM events WHERE subject=? ORDER BY received,source,event_id",
                (subject,),
            ).fetchall()
            lifecycle_counts = {
                item["type"]: item["count"]
                for item in self.db.execute(
                    "SELECT type,COUNT(*) AS count FROM events WHERE subject=? AND type IN ('session.started','session.ended') GROUP BY type",
                    (subject,),
                ).fetchall()
            }
        event_digest = _sha(_canonical([item[0] for item in events]))
        redacted_count = sum(int(item[1]) for item in events)
        if not events or redacted_count == len(events):
            payload_storage = "redacted"
        elif redacted_count == 0:
            payload_storage = "unredacted"
        else:
            payload_storage = "mixed"
        return {
            "schema": "hotato.production-session.v1",
            "session_id": subject,
            "status": row["state"],
            "event_count": row["event_count"],
            "duplicate_count": row["duplicate_count"],
            "conflict_count": row["conflict_count"],
            "out_of_order_count": row["out_of_order_count"],
            "unsequenced_count": row["unsequenced_count"],
            "highest_sequence": row["highest_sequence"],
            "lifecycle": {
                "session_started_events": lifecycle_counts.get("session.started", 0),
                "session_ended_events": lifecycle_counts.get("session.ended", 0),
                "unambiguous": (
                    lifecycle_counts.get("session.started", 0) == 1
                    and lifecycle_counts.get("session.ended", 0) == 1
                ),
            },
            "evidence": json.loads(row["evidence_json"]),
            "required_evidence_lanes": (
                json.loads(row["required_evidence_json"])
                if row["required_evidence_json"]
                else list(EVIDENCE_LANES)
            ),
            "payload_storage": payload_storage,
            "stored_event_log_sha256": event_digest,
            "finalized_at": row["finalized_at"],
            "finalization_reason": row["finalization_reason"],
        }

    def evaluate_alerts(
        self, rules: Iterable[Dict[str, Any]]
    ) -> List[Dict[str, Any]]:
        """Evaluate bounded built-in rules and persist state transitions."""

        supported = {
            "degraded",
            "missing_audio",
            "missing_tool_evidence",
            "incomplete_evidence",
            "conflict",
            "out_of_order",
            "unsequenced",
        }
        normalized_rules: List[Dict[str, str]] = []
        for rule in rules:
            if (
                not isinstance(rule, dict)
                or set(rule) != {"id", "condition"}
                or not isinstance(rule["id"], str)
                or not _ID.fullmatch(rule["id"])
                or rule["condition"] not in supported
            ):
                raise ValueError(
                    "alert rule must contain a bounded id and supported condition"
                )
            normalized_rules.append(rule)
        changes: List[Dict[str, Any]] = []
        now = float(self.clock())
        with self._lock:
            sessions = self.db.execute(
                "SELECT * FROM sessions WHERE state!='DELETED' ORDER BY subject"
            ).fetchall()
            self._begin()
            try:
                for rule in normalized_rules:
                    for session in sessions:
                        evidence = json.loads(session["evidence_json"])
                        condition = rule["condition"]
                        if condition == "degraded":
                            active = session["state"] == "DEGRADED"
                        elif condition == "missing_audio":
                            active = evidence["participant_audio"]["availability"] != "available"
                        elif condition == "missing_tool_evidence":
                            active = evidence["tool_calls"]["availability"] != "available"
                        elif condition == "incomplete_evidence":
                            active = any(
                                item["availability"] != "available"
                                for item in evidence.values()
                            )
                        elif condition == "conflict":
                            active = session["conflict_count"] > 0
                        elif condition == "out_of_order":
                            active = session["out_of_order_count"] > 0
                        else:
                            active = session["unsequenced_count"] > 0
                        alert_id = hashlib.sha256(
                            f"{rule['id']}:{session['subject']}".encode("utf-8")
                        ).hexdigest()
                        existing = self.db.execute(
                            "SELECT state,opened,generation FROM alerts WHERE id=?",
                            (alert_id,),
                        ).fetchone()
                        new_state = "FIRING" if active else "RESOLVED"
                        if not active and existing is None:
                            continue
                        if existing and existing["state"] == new_state:
                            continue
                        generation = int(existing["generation"]) if existing else 1
                        opened = float(existing["opened"]) if existing else now
                        if existing and new_state == "FIRING":
                            generation += 1
                            opened = now
                        observed = safe_json_dumps(
                            {"condition": condition}, sort_keys=True
                        )
                        self.db.execute(
                            """
                            INSERT INTO alerts(id,subject,rule_id,state,opened,updated,generation,observed_json)
                            VALUES(?,?,?,?,?,?,?,?)
                            ON CONFLICT(id) DO UPDATE SET
                              state=excluded.state,opened=excluded.opened,
                              updated=excluded.updated,generation=excluded.generation,
                              observed_json=excluded.observed_json
                            """,
                            (
                                alert_id,
                                session["subject"],
                                rule["id"],
                                new_state,
                                opened,
                                now,
                                generation,
                                observed,
                            ),
                        )
                        previous = existing["state"] if existing else None
                        self.db.execute(
                            "INSERT INTO alert_transitions(alert_id,at,previous_state,state,observed_json) VALUES(?,?,?,?,?)",
                            (alert_id, now, previous, new_state, observed),
                        )
                        self._audit(
                            "alert.transition",
                            alert_id,
                            previous,
                            new_state,
                            "stored",
                        )
                        changes.append(
                            {
                                "alert_id": alert_id,
                                "subject": session["subject"],
                                "rule_id": rule["id"],
                                "state": new_state,
                                "generation": generation,
                            }
                        )
                self._commit()
            except BaseException:
                self._rollback()
                raise
        return changes

    def metrics(self) -> str:
        """Return Prometheus text with only enumerated, bounded labels."""

        with self._lock:
            states = {
                row[0]: row[1]
                for row in self.db.execute(
                    "SELECT state,COUNT(*) FROM sessions GROUP BY state"
                )
            }
            counters = {
                row[0]: row[1]
                for row in self.db.execute("SELECT name,value FROM counters")
            }
            alerts = {
                row[0]: row[1]
                for row in self.db.execute(
                    "SELECT state,COUNT(*) FROM alerts GROUP BY state"
                )
            }
        lines = [
            "# HELP hotato_production_events_total Events committed to the local store.",
            "# TYPE hotato_production_events_total counter",
            f"hotato_production_events_total {counters.get('events', 0)}",
            "# HELP hotato_production_duplicates_total Idempotent duplicate deliveries observed.",
            "# TYPE hotato_production_duplicates_total counter",
            f"hotato_production_duplicates_total {counters.get('duplicates', 0)}",
            "# HELP hotato_production_conflicts_total Conflicting event identities refused.",
            "# TYPE hotato_production_conflicts_total counter",
            f"hotato_production_conflicts_total {counters.get('conflicts', 0)}",
            "# HELP hotato_production_out_of_order_total Source-scoped ordering anomalies retained.",
            "# TYPE hotato_production_out_of_order_total counter",
            f"hotato_production_out_of_order_total {counters.get('out_of_order', 0)}",
            "# HELP hotato_production_sessions Current sessions by enumerated state.",
            "# TYPE hotato_production_sessions gauge",
        ]
        for state in sorted(SESSION_STATES):
            lines.append(
                f'hotato_production_sessions{{status="{state}"}} {states.get(state, 0)}'
            )
        lines.extend(
            [
                "# HELP hotato_production_alerts Current alerts by enumerated state.",
                "# TYPE hotato_production_alerts gauge",
                f'hotato_production_alerts{{state="FIRING"}} {alerts.get("FIRING", 0)}',
                f'hotato_production_alerts{{state="RESOLVED"}} {alerts.get("RESOLVED", 0)}',
            ]
        )
        return "\n".join(lines) + "\n"

    def verify_audit_chain(self) -> Dict[str, Any]:
        previous = "sha256:" + "0" * 64
        first_invalid: Optional[int] = None
        entries = 0
        with self._lock:
            rows = self.db.execute("SELECT * FROM audit ORDER BY sequence").fetchall()
            checkpoint_rows = {
                row[0]: row[1]
                for row in self.db.execute(
                    "SELECT key,value FROM metadata WHERE key IN ('audit_head_sha256','audit_entry_count')"
                )
            }
        for row in rows:
            entries += 1
            body = {
                "at": row["at"],
                "action": row["action"],
                "target": row["target"],
                "before_sha256": row["before_sha256"],
                "after_sha256": row["after_sha256"],
                "result": row["result"],
                "previous": previous,
            }
            expected = _sha(_canonical(body))
            if (
                row["previous_sha256"] != previous
                or row["chain_sha256"] != expected
            ) and first_invalid is None:
                first_invalid = row["sequence"]
            previous = row["chain_sha256"]
        checkpoint_head = checkpoint_rows.get(
            "audit_head_sha256", "sha256:" + "0" * 64
        )
        try:
            checkpoint_count = int(checkpoint_rows.get("audit_entry_count", "0"))
        except ValueError:
            checkpoint_count = -1
        checkpoint_matches = previous == checkpoint_head and entries == checkpoint_count
        return {
            "schema": "hotato.production-audit-verification.v1",
            "valid": first_invalid is None and checkpoint_matches,
            "entries": entries,
            "first_invalid_sequence": first_invalid,
            "head_sha256": previous,
            "checkpoint_matches": checkpoint_matches,
        }

    def export_regression_candidate(self, subject: str, output_dir: str) -> Dict[str, Any]:
        """Export one session into an atomic, offline-verifiable directory."""

        manifest = self.manifest(subject)
        if manifest["status"] not in ("COMPLETE", "DEGRADED"):
            raise ProductionError("session must be finalized before promotion")
        target = os.path.abspath(output_dir)
        if os.path.exists(target):
            raise FileExistsError(target)
        parent = os.path.dirname(target) or "."
        os.makedirs(parent, exist_ok=True)
        staging = tempfile.mkdtemp(prefix=".hotato-production-", dir=parent)
        try:
            with self._lock:
                rows = self.db.execute(
                    "SELECT payload_json FROM events WHERE subject=? ORDER BY received,source,event_id",
                    (subject,),
                ).fetchall()
            events_bytes = b"".join(
                _canonical(json.loads(row["payload_json"])) for row in rows
            )
            events_name = "events.jsonl"
            events_path = os.path.join(staging, events_name)
            with open(events_path, "wb") as handle:  # open-ok: owned staging dir
                handle.write(events_bytes)
                handle.flush()
                os.fsync(handle.fileno())
            candidate: Dict[str, Any] = {
                "schema": "hotato.production-regression-candidate.v1",
                "source_session": {
                    "id": subject,
                    "status": manifest["status"],
                    "manifest": manifest,
                },
                "artifacts": [
                    {
                        "path": events_name,
                        "bytes": len(events_bytes),
                        "sha256": _sha(events_bytes),
                        "media_type": "application/x-ndjson",
                    }
                ],
                "privacy": {
                    "payloads": manifest["payload_storage"],
                    "review_required_before_sharing": True,
                },
                "promotion": {
                    "status": "CANDIDATE",
                    "reason": "requires operator-authored assertions before CI gating",
                },
            }
            candidate["candidate_id"] = _sha(_canonical(candidate))
            candidate_bytes = _canonical(candidate)
            with open(
                os.path.join(staging, "candidate.json"), "wb"
            ) as handle:  # open-ok: owned staging dir
                handle.write(candidate_bytes)
                handle.flush()
                os.fsync(handle.fileno())
            verification = verify_regression_candidate(staging)
            if not verification["valid"]:
                raise ProductionError("staged regression candidate failed verification")
            rename_no_replace(staging, target)
            _fsync_directory(parent)
            staging = ""
            with self._lock:
                self._begin()
                try:
                    self._audit(
                        "session.promote",
                        subject,
                        manifest["stored_event_log_sha256"],
                        candidate["candidate_id"],
                        "exported",
                    )
                    self._commit()
                except BaseException:
                    self._rollback()
                    raise
            return {
                "status": "exported",
                "path": target,
                "candidate_id": candidate["candidate_id"],
                "verification": verification,
            }
        finally:
            if staging:
                shutil.rmtree(staging, ignore_errors=True)

    def delete_session(self, subject: str, *, reason: str = "operator_request") -> Dict[str, Any]:
        if not isinstance(reason, str) or not _ID.fullmatch(reason):
            raise ValueError("deletion reason must be a bounded identifier")
        before = self.manifest(subject)
        if before["status"] == "DELETED":
            raise ProductionError("session is already deleted")
        before_hash = _sha(_canonical(before))
        now = float(self.clock())
        receipt: Dict[str, Any] = {
            "schema": "hotato.production-deletion-receipt.v1",
            "subject_sha256": _sha(subject.encode("utf-8")),
            "before_manifest_sha256": before_hash,
            "deleted_event_count": before["event_count"],
            "deleted_at": now,
            "reason": reason,
        }
        receipt["receipt_id"] = _sha(_canonical(receipt))
        with self._lock:
            self._begin()
            try:
                alert_ids = [
                    row[0]
                    for row in self.db.execute(
                        "SELECT id FROM alerts WHERE subject=?", (subject,)
                    ).fetchall()
                ]
                self.db.execute("DELETE FROM events WHERE subject=?", (subject,))
                self.db.execute("DELETE FROM conflicts WHERE subject=?", (subject,))
                self.db.execute("DELETE FROM sequence_cursors WHERE subject=?", (subject,))
                for alert_id in alert_ids:
                    self.db.execute(
                        "DELETE FROM alert_transitions WHERE alert_id=?", (alert_id,)
                    )
                self.db.execute("DELETE FROM alerts WHERE subject=?", (subject,))
                self.db.execute("DELETE FROM sessions WHERE subject=?", (subject,))
                self.db.execute(
                    "INSERT INTO deletion_receipts(id,subject_sha256,deleted_at,receipt_json) VALUES(?,?,?,?)",
                    (
                        receipt["receipt_id"],
                        receipt["subject_sha256"],
                        now,
                        _canonical(receipt).decode("utf-8"),
                    ),
                )
                self._audit(
                    "session.delete", subject, before_hash, receipt["receipt_id"], reason
                )
                self._commit()
            except BaseException:
                self._rollback()
                raise
        return receipt

    def enforce_retention(
        self, *, retention_seconds: float, now: Optional[float] = None
    ) -> List[Dict[str, Any]]:
        if retention_seconds < 0:
            raise ValueError("retention_seconds must be >= 0")
        now_value = float(self.clock() if now is None else now)
        cutoff = now_value - retention_seconds
        with self._lock:
            subjects = [
                row[0]
                for row in self.db.execute(
                    "SELECT subject FROM sessions WHERE state IN ('COMPLETE','DEGRADED','EXPIRED') AND finalized_at IS NOT NULL AND finalized_at<=? ORDER BY subject",
                    (cutoff,),
                ).fetchall()
            ]
        return [
            self.delete_session(subject, reason="retention_policy")
            for subject in subjects
        ]

    def close(self) -> None:
        with self._lock:
            self.db.close()


def verify_regression_candidate(path: str) -> Dict[str, Any]:
    root = os.path.abspath(path)
    try:
        root_mode = os.lstat(root).st_mode
    except OSError as exc:
        return {
            "schema": "hotato.production-regression-verification.v1",
            "valid": False,
            "errors": [f"candidate directory unreadable: {exc}"],
        }
    if not stat.S_ISDIR(root_mode):
        return {
            "schema": "hotato.production-regression-verification.v1",
            "valid": False,
            "errors": ["candidate root must be a non-symlink directory"],
        }
    candidate_path = os.path.join(root, "candidate.json")
    errors: List[str] = []
    try:
        candidate_mode = os.lstat(candidate_path).st_mode
    except OSError as exc:
        return {
            "schema": "hotato.production-regression-verification.v1",
            "valid": False,
            "errors": [f"candidate.json unreadable: {exc}"],
        }
    if not stat.S_ISREG(candidate_mode):
        return {
            "schema": "hotato.production-regression-verification.v1",
            "valid": False,
            "errors": ["candidate.json must be a regular file"],
        }
    try:
        candidate = _strict_json_loads(
            _read_regular_bytes_no_follow(
                candidate_path, max_bytes=_MAX_REGRESSION_MANIFEST_BYTES
            ).decode("utf-8")
        )
    except (OSError, ValueError) as exc:
        return {
            "schema": "hotato.production-regression-verification.v1",
            "valid": False,
            "errors": [f"candidate.json unreadable: {exc}"],
        }
    if not isinstance(candidate, dict) or candidate.get("schema") != "hotato.production-regression-candidate.v1":
        errors.append("candidate schema is unsupported")
    claimed_id = candidate.get("candidate_id")
    identity_doc = dict(candidate) if isinstance(candidate, dict) else {}
    identity_doc.pop("candidate_id", None)
    if claimed_id != _sha(_canonical(identity_doc)):
        errors.append("candidate_id mismatch")
    artifacts = candidate.get("artifacts", []) if isinstance(candidate, dict) else []
    if not isinstance(artifacts, list):
        errors.append("artifacts must be a list")
        artifacts = []
    if len(artifacts) != 1:
        errors.append("candidate must declare exactly one events.jsonl artifact")
    declared_paths: List[str] = []
    event_log_digests: Optional[List[str]] = None
    for artifact in artifacts:
        if not isinstance(artifact, dict):
            errors.append("artifact entry must be a mapping")
            continue
        relative = artifact.get("path")
        if (
            not isinstance(relative, str)
            or not relative
            or os.path.isabs(relative)
            or ".." in relative.replace("\\", "/").split("/")
        ):
            errors.append("artifact path is unsafe")
            continue
        if relative in declared_paths:
            errors.append(f"duplicate artifact path: {relative}")
            continue
        declared_paths.append(relative)
        if relative != "events.jsonl":
            errors.append(f"unsupported artifact path: {relative}")
            continue
        if artifact.get("media_type") != "application/x-ndjson":
            errors.append("events.jsonl media type mismatch")
        full = os.path.abspath(os.path.join(root, relative))
        if os.path.commonpath([root, full]) != root:
            errors.append("artifact escapes candidate directory")
            continue
        try:
            artifact_mode = os.lstat(full).st_mode
        except OSError:
            errors.append(f"artifact missing: {relative}")
            continue
        if not stat.S_ISREG(artifact_mode):
            errors.append(f"artifact is not a regular file: {relative}")
            continue
        try:
            raw = _read_regular_bytes_no_follow(
                full, max_bytes=_MAX_REGRESSION_ARTIFACT_BYTES
            )
        except (OSError, ValueError) as exc:
            errors.append(f"artifact unreadable or changed: {relative}: {exc}")
            continue
        if len(raw) != artifact.get("bytes"):
            errors.append(f"artifact byte count mismatch: {relative}")
        if _sha(raw) != artifact.get("sha256"):
            errors.append(f"artifact digest mismatch: {relative}")
        if relative == "events.jsonl":
            event_log_digests = []
            for index, line in enumerate(raw.splitlines(), 1):
                try:
                    item = _strict_json_loads(line)
                except (ValueError, UnicodeDecodeError):
                    errors.append(f"events.jsonl line {index} is invalid JSON")
                    continue
                canonical_line = _canonical(item)
                if canonical_line.rstrip(b"\n") != line:
                    errors.append(f"events.jsonl line {index} is not canonical")
                event_log_digests.append(_sha(canonical_line))
    source_session = candidate.get("source_session", {}) if isinstance(candidate, dict) else {}
    source_manifest = (
        source_session.get("manifest", {})
        if isinstance(source_session, dict)
        else {}
    )
    if event_log_digests is None:
        errors.append("events.jsonl artifact is required")
    elif isinstance(source_manifest, dict):
        if source_manifest.get("event_count") != len(event_log_digests):
            errors.append("source manifest event_count mismatch")
        if source_manifest.get("stored_event_log_sha256") != _sha(
            _canonical(event_log_digests)
        ):
            errors.append("source manifest event log digest mismatch")
    else:
        errors.append("source session manifest must be a mapping")

    # A verified directory is safe to copy as a unit only when every entry is
    # covered.  Otherwise a valid manifest can camouflage an unlisted secret,
    # socket, or symlink that a reviewer later archives and shares.
    try:
        names: List[str] = []
        with os.scandir(root) as entries:
            for entry in entries:
                names.append(entry.name)
                if len(names) > 2:
                    break
        observed = set(names)
        expected = {"candidate.json", "events.jsonl"}
        if observed != expected:
            unexpected = sorted(observed - expected)
            missing = sorted(expected - observed)
            if unexpected:
                errors.append("unlisted candidate entries: " + ", ".join(unexpected))
            if missing:
                errors.append("missing candidate entries: " + ", ".join(missing))
        if len(names) > 2:
            errors.append("candidate directory contains more than two entries")
        for name in names[:2]:
            entry_mode = os.lstat(os.path.join(root, name)).st_mode
            if not stat.S_ISREG(entry_mode):
                errors.append(f"candidate entry is not a regular file: {name}")
    except OSError as exc:
        errors.append(f"candidate directory could not be enumerated: {exc}")
    return {
        "schema": "hotato.production-regression-verification.v1",
        "valid": not errors,
        "candidate_id": claimed_id,
        "artifacts_verified": len(artifacts),
        "errors": errors,
    }


def _verify_bearer(header: str, token: str) -> bool:
    return (
        bool(token)
        and header.lower().startswith("bearer ")
        and hmac.compare_digest(header[7:].strip().encode(), token.encode())
    )


def _verify_hmac(
    raw: bytes,
    *,
    timestamp: str,
    signature: str,
    secret: str,
    now: float,
    max_skew_seconds: int,
) -> bool:
    if (
        not secret
        or not re.fullmatch(r"[0-9]{1,20}", timestamp)
        or not re.fullmatch(r"v1=[0-9a-fA-F]{64}", signature)
    ):
        return False
    try:
        moment = int(timestamp)
    except ValueError:
        return False
    if abs(now - moment) > max_skew_seconds:
        return False
    expected = hmac.new(
        secret.encode("utf-8"), timestamp.encode("ascii") + b"." + raw, hashlib.sha256
    ).hexdigest()
    return hmac.compare_digest(signature[3:].lower(), expected)


class _BoundedThreadingHTTPServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, *args: Any, max_workers: int, **kwargs: Any) -> None:
        if max_workers < 1:
            raise ValueError("max_workers must be >= 1")
        self._capacity = threading.BoundedSemaphore(max_workers)
        super().__init__(*args, **kwargs)

    def process_request(self, request: socket.socket, client_address: Any) -> None:
        if not self._capacity.acquire(blocking=False):
            try:
                request.sendall(
                    b"HTTP/1.1 503 Service Unavailable\r\nConnection: close\r\nContent-Type: application/json\r\nContent-Length: 25\r\nRetry-After: 1\r\n\r\n{\"error\":\"backpressure\"}\n"
                )
            finally:
                self.shutdown_request(request)
            return
        try:
            super().process_request(request, client_address)
        except BaseException:
            self._capacity.release()
            raise

    def process_request_thread(self, request: socket.socket, client_address: Any) -> None:
        try:
            super().process_request_thread(request, client_address)
        finally:
            self._capacity.release()


class ProductionGateway:
    """Bounded local HTTP gateway for CloudEvents and OTLP/HTTP JSON traces."""

    def __init__(
        self,
        store: ProductionStore,
        token: Optional[str] = None,
        *,
        hmac_secret: Optional[str] = None,
        host: str = "127.0.0.1",
        port: int = 0,
        max_workers: int = 16,
        max_signature_skew_seconds: int = 300,
        request_timeout_seconds: float = 30.0,
    ) -> None:
        if token is not None and len(token) < 16:
            raise ValueError("gateway token must contain at least 16 characters")
        if token is not None and any(character in token for character in "\x00\r\n"):
            raise ValueError("gateway token must contain exactly one header value")
        if hmac_secret is not None and len(hmac_secret) < 32:
            raise ValueError("gateway HMAC secret must contain at least 32 characters")
        if not token and not hmac_secret:
            raise ValueError("configure a bearer token, HMAC secret, or both")
        if (
            isinstance(max_signature_skew_seconds, bool)
            or not isinstance(max_signature_skew_seconds, int)
            or not 1 <= max_signature_skew_seconds <= 86_400
        ):
            raise ValueError("max_signature_skew_seconds must be in [1, 86400]")
        if (
            isinstance(port, bool)
            or not isinstance(port, int)
            or not 0 <= port <= 65_535
        ):
            raise ValueError("production gateway port must be in [0, 65535]")
        if (
            isinstance(request_timeout_seconds, bool)
            or not isinstance(request_timeout_seconds, (int, float))
            or not math.isfinite(float(request_timeout_seconds))
            or not 0.1 <= float(request_timeout_seconds) <= 300
        ):
            raise ValueError("request_timeout_seconds must be in [0.1, 300]")
        if not isinstance(host, str) or not host:
            raise ValueError("production gateway host must be a loopback address")
        try:
            addresses = {
                item[4][0]
                for item in socket.getaddrinfo(
                    host, port, type=socket.SOCK_STREAM
                )
            }
        except socket.gaierror as exc:
            raise ValueError("production gateway host could not be resolved") from exc
        if not addresses or any(
            not ipaddress.ip_address(address.split("%", 1)[0]).is_loopback
            for address in addresses
        ):
            raise ValueError(
                "production gateway accepts loopback binds only; terminate TLS "
                "at a local reverse proxy for remote ingestion"
            )
        gateway = self

        class Handler(BaseHTTPRequestHandler):
            server_version = "hotato-production"
            sys_version = ""

            def setup(self) -> None:
                super().setup()
                self.connection.settimeout(float(request_timeout_seconds))

            def log_message(self, *_args: Any) -> None:
                return

            def _send(
                self,
                status: int,
                body: bytes,
                content_type: str = "application/json",
            ) -> None:
                self.send_response(status)
                self.send_header("Content-Type", content_type)
                self.send_header("Content-Length", str(len(body)))
                self.send_header("Cache-Control", "no-store")
                self.end_headers()
                self.wfile.write(body)

            def _bearer_ok(self) -> bool:
                return bool(token) and _verify_bearer(
                    self.headers.get("Authorization", ""), token or ""
                )

            def _authenticated(self, raw: bytes) -> bool:
                if self._bearer_ok():
                    return True
                return bool(hmac_secret) and _verify_hmac(
                    raw,
                    timestamp=self.headers.get("X-Hotato-Timestamp", ""),
                    signature=self.headers.get("X-Hotato-Signature", ""),
                    secret=hmac_secret or "",
                    now=float(store.clock()),
                    max_skew_seconds=max_signature_skew_seconds,
                )

            def do_GET(self) -> None:
                if self.path == "/healthz":
                    self._send(200, b'{"status":"ok"}\n')
                    return
                if self.path == "/metrics" and self._bearer_ok():
                    self._send(
                        200,
                        store.metrics().encode("utf-8"),
                        "text/plain; version=0.0.4",
                    )
                    return
                self._send(401, b'{"error":"unauthorized"}\n')

            def do_POST(self) -> None:
                if self.path not in ("/v1/events", "/v1/otlp/traces", "/v1/traces"):
                    self._send(404, b'{"error":"not_found"}\n')
                    return
                try:
                    length = int(self.headers.get("Content-Length", "-1"))
                except ValueError:
                    length = -1
                if length < 0 or length > _MAX_EVENT_BYTES:
                    self._send(413, b'{"error":"invalid_size"}\n')
                    return
                try:
                    raw = self.rfile.read(length)
                except (TimeoutError, socket.timeout):
                    try:
                        self._send(408, b'{"error":"request_timeout"}\n')
                    except OSError:
                        pass
                    return
                if len(raw) != length:
                    self._send(400, b'{"error":"truncated_body"}\n')
                    return
                # Authenticate exact bytes before parsing them.
                if not self._authenticated(raw):
                    self._send(401, b'{"error":"unauthorized"}\n')
                    return
                try:
                    payload = _strict_json_loads(raw)
                    if self.path == "/v1/events":
                        result: Any = store.ingest(payload)
                    else:
                        source = self.headers.get("X-Hotato-Source", "otlp")
                        event_results = store.ingest_otlp(payload, source=source)
                        result = {
                            "status": (
                                "completed_with_conflicts"
                                if any(item["status"] == "conflict" for item in event_results)
                                else "stored"
                            ),
                            "events": event_results,
                            "durability": "committed",
                        }
                except EventConflict:
                    self._send(409, b'{"error":"event_conflict"}\n')
                    return
                except (ValueError, json.JSONDecodeError, RecursionError):
                    self._send(400, b'{"error":"invalid_event"}\n')
                    return
                except sqlite3.Error:
                    self._send(503, b'{"error":"storage_unavailable"}\n')
                    return
                # The canonical OTLP/HTTP path must return an
                # ExportTraceServiceResponse JSON object.  An empty object is
                # the successful no-partial-error response; returning Hotato's
                # richer receipt fields here would be an unknown-field
                # protobuf-JSON response for strict collectors.  The explicit
                # /v1/otlp/traces compatibility path retains the inspectable
                # Hotato commit receipt.
                if self.path == "/v1/traces":
                    rejected = sum(
                        item["status"] == "conflict" for item in event_results
                    )
                    response = (
                        b"{}\n"
                        if not rejected
                        else _canonical(
                            {
                                "partialSuccess": {
                                    "rejectedSpans": str(rejected),
                                    "errorMessage": (
                                        "Hotato refused conflicting event identities"
                                    ),
                                }
                            }
                        )
                    )
                else:
                    response = _canonical(result)
                self._send(200, response)

        self.server = _BoundedThreadingHTTPServer(
            (host, port), Handler, max_workers=max_workers
        )
        self.thread = threading.Thread(
            target=self.server.serve_forever,
            name="hotato-production-gateway",
            daemon=True,
        )
        self.thread.start()

    @property
    def address(self) -> Tuple[str, int]:
        return self.server.server_address

    def close(self) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=5)
