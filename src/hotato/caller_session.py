"""Strict WebSocket sidecar session for the stateful caller engine.

The sidecar protocol lets a Pipecat, LiveKit, SIP, or provider-specific process
own media and signaling while Hotato owns caller policy, limits, provenance,
and evidence packaging.  A successful WebSocket handshake establishes only the
session boundary.  Delivered-media or carrier claims require events emitted by
the target boundary and stay explicit in the caller package.
"""

from __future__ import annotations

import hashlib
import json
import os
import struct
from typing import Any, Callable, Dict, Mapping, Optional
from urllib.parse import urlsplit

from .caller import CAPABILITY_STATES
from .websocket_transport import (
    WebSocketClient,
    WebSocketProtocolError,
    WebSocketTimeout,
    connect,
)

PROTOCOL_SCHEMA = "hotato.caller-session.v1"
SUBPROTOCOL = "hotato.caller.v1"
_AUDIO_MAGIC = b"HTC1"
_AUDIO_HEADER = struct.Struct("!4sI")
_MAX_JSON_BYTES = 1024 * 1024
_MAX_AUDIO_BYTES = 64 * 1024 * 1024
_MAX_PENDING_EVENTS = 512
_MAX_COMMAND_RECEIPTS = 4096
_MAX_STORED_EVIDENCE_BYTES = 16 * 1024 * 1024
_MAX_HEADERS = 64
_MAX_HEADER_BYTES = 256 * 1024
_OPERATIONS = (
    "send_text", "send_audio", "receive", "send_dtmf", "wait", "silence",
    "impairment", "observe_transfer", "hangup",
)


class CallerSessionError(RuntimeError):
    """The caller sidecar violated or could not complete its wire contract."""


def _canonical(value: Any) -> str:
    try:
        encoded = json.dumps(
            value, sort_keys=True, separators=(",", ":"), ensure_ascii=False,
            allow_nan=False,
        )
    except (TypeError, ValueError) as exc:
        raise ValueError("caller-session messages must contain finite JSON values") from exc
    if len(encoded.encode("utf-8")) > _MAX_JSON_BYTES:
        raise ValueError("caller-session control message exceeds 1 MiB")
    return encoded


def _capabilities(value: Any) -> Dict[str, str]:
    if not isinstance(value, dict) or set(value) != set(_OPERATIONS):
        raise CallerSessionError(
            "sidecar ready message must declare every caller-session operation"
        )
    output = {}
    for name, state in value.items():
        if state not in CAPABILITY_STATES:
            raise CallerSessionError(
                f"sidecar capability {name!r} must be SUPPORTED, UNSUPPORTED, or UNOBSERVABLE"
            )
        output[name] = state
    return output


def _safe_headers(headers: Optional[Mapping[str, str]]) -> Dict[str, str]:
    if headers is not None and not isinstance(headers, Mapping):
        raise ValueError("caller-session headers must be a mapping")
    if len(headers or {}) > _MAX_HEADERS:
        raise ValueError(f"caller-session headers exceed the {_MAX_HEADERS}-header limit")
    output = {}
    seen_names = set()
    total = 0
    for name, value in (headers or {}).items():
        if not isinstance(name, str) or not isinstance(value, str):
            raise ValueError("caller-session headers must be string pairs")
        if any(char in name for char in "\r\n:") or any(char in value for char in "\r\n"):
            raise ValueError("caller-session headers contain a control character")
        if not name or len(name) > 256 or "\x00" in name or "\x00" in value:
            raise ValueError("caller-session header name or value is invalid")
        normalized_name = name.lower()
        if normalized_name in seen_names:
            raise ValueError("caller-session header names must be unique case-insensitively")
        seen_names.add(normalized_name)
        total += len(name.encode("utf-8")) + len(value.encode("utf-8"))
        if total > _MAX_HEADER_BYTES:
            raise ValueError(
                f"caller-session headers exceed the {_MAX_HEADER_BYTES}-byte limit"
            )
        output[name] = value
    return output


def _normalized_endpoint_identity(endpoint: str) -> str:
    """Return the credential-free endpoint identity used for evidence binding."""

    try:
        parsed = urlsplit(endpoint)
        port = parsed.port
    except ValueError as exc:
        raise ValueError("caller-session endpoint is invalid") from exc
    scheme = parsed.scheme.lower()
    if (
        scheme not in {"ws", "wss"}
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
        or parsed.fragment
    ):
        raise ValueError("caller-session endpoint identity is invalid")
    target = parsed.path or "/"
    if parsed.query:
        target += "?" + parsed.query
    if any(ord(character) < 0x21 or ord(character) > 0x7E for character in target):
        raise ValueError("caller-session endpoint path/query must be printable ASCII")
    try:
        host = parsed.hostname.encode("idna").decode("ascii").lower()
    except UnicodeError as exc:
        raise ValueError("caller-session endpoint hostname is invalid") from exc
    default = 443 if scheme == "wss" else 80
    if port is not None and not 1 <= port <= 65_535:
        raise ValueError("caller-session endpoint port is invalid")
    display = f"[{host}]" if ":" in host else host
    authority = display if port in {None, default} else f"{display}:{port}"
    return f"{scheme}://{authority}{target}"


class WebSocketCallerSession:
    """Concrete caller session backed by a strict RFC 6455 sidecar.

    The constructor completes a nonce-bound hello/ready exchange before
    exposing capabilities.  Secret headers are used only in the opening
    handshake and never copied into caller artifacts or control messages.
    """

    def __init__(
        self,
        endpoint: str,
        *,
        headers: Optional[Mapping[str, str]] = None,
        allow_remote: bool = False,
        connect_timeout: float = 10.0,
        command_timeout: float = 60.0,
        max_message_bytes: int = 16 * 1024 * 1024,
        connector: Callable[..., WebSocketClient] = connect,
        nonce: Optional[str] = None,
    ) -> None:
        if not isinstance(endpoint, str) or not endpoint:
            raise ValueError("caller-session endpoint must be non-empty")
        if isinstance(connect_timeout, bool) or not isinstance(connect_timeout, (int, float)) or not 0 < float(connect_timeout) <= 300:
            raise ValueError("connect_timeout must be in (0, 300]")
        if isinstance(command_timeout, bool) or not isinstance(command_timeout, (int, float)) or not 0 < float(command_timeout) <= 600:
            raise ValueError("command_timeout must be in (0, 600]")
        if isinstance(max_message_bytes, bool) or not isinstance(max_message_bytes, int) or not 1024 <= max_message_bytes <= _MAX_AUDIO_BYTES + 1024:
            raise ValueError("max_message_bytes is outside the supported bound")
        self._client = connector(
            endpoint, subprotocol=SUBPROTOCOL, headers=_safe_headers(headers),
            timeout=float(connect_timeout), max_message_bytes=max_message_bytes,
            allow_remote=allow_remote,
        )
        self._sequence = 0
        self._closed = False
        self._command_timeout = float(command_timeout)
        self._pending_events = []
        self._receipts = []
        self._stored_evidence_bytes = 0
        self._nonce = nonce or hashlib.sha256(os.urandom(32)).hexdigest()
        if not isinstance(self._nonce, str) or not self._nonce or len(self._nonce) > 256:
            self._client.abort()
            raise ValueError("caller-session nonce must be a bounded string")
        try:
            self._send({
                "schema": PROTOCOL_SCHEMA, "type": "hello", "nonce": self._nonce,
                "client": "hotato", "operations": list(_OPERATIONS),
            })
            ready = self._receive_json()
            if (
                ready.get("schema") != PROTOCOL_SCHEMA
                or ready.get("type") != "ready"
                or ready.get("nonce") != self._nonce
            ):
                raise CallerSessionError("sidecar did not return the nonce-bound ready message")
            unknown = set(ready) - {"schema", "type", "nonce", "capabilities", "adapter"}
            if unknown:
                raise CallerSessionError("sidecar ready message contains unknown fields")
            adapter = ready.get("adapter")
            if not isinstance(adapter, dict) or set(adapter) != {"name", "version"}:
                raise CallerSessionError("sidecar ready message requires adapter name and version")
            if any(not isinstance(adapter[key], str) or not adapter[key] or len(adapter[key]) > 500 for key in ("name", "version")):
                raise CallerSessionError("sidecar adapter identity is invalid")
            self._capabilities = _capabilities(ready.get("capabilities"))
            self.adapter = dict(adapter)
            # Assigned only after the nonce-bound application handshake has
            # completed.  This is endpoint-configuration evidence, not proof
            # of a packet reaching an agent or carrier.
            normalized_endpoint = _normalized_endpoint_identity(endpoint)
            self.connected_endpoint_sha256 = (
                "sha256:" + hashlib.sha256(normalized_endpoint.encode("utf-8")).hexdigest()
            )
        except BaseException:
            self._client.abort()
            self._closed = True
            raise

    def _send(self, value: Mapping[str, Any]) -> None:
        if self._closed:
            raise CallerSessionError("caller session is closed")
        self._client.send_text(_canonical(dict(value)))

    def _receive_json(self) -> Dict[str, Any]:
        message = self._client.receive()
        if message.kind == "close":
            self._closed = True
            raise CallerSessionError("caller sidecar closed before completing the operation")
        if message.kind != "text" or not isinstance(message.data, str):
            raise WebSocketProtocolError("caller sidecar control messages must be JSON text")
        if len(message.data.encode("utf-8")) > _MAX_JSON_BYTES:
            raise WebSocketProtocolError("caller sidecar control message exceeds 1 MiB")
        try:
            value = json.loads(message.data)
        except json.JSONDecodeError as exc:
            raise WebSocketProtocolError("caller sidecar sent invalid JSON") from exc
        if not isinstance(value, dict):
            raise WebSocketProtocolError("caller sidecar JSON message must be an object")
        return value

    def _event(self, value: Mapping[str, Any]) -> Dict[str, Any]:
        if value.get("schema") != PROTOCOL_SCHEMA or value.get("type") != "event":
            raise WebSocketProtocolError("caller sidecar returned a non-event message")
        if set(value) != {"schema", "type", "event"} or not isinstance(value["event"], dict):
            raise WebSocketProtocolError("caller sidecar event envelope is malformed")
        # Apply the same one-MiB canonical bound to the event object before it
        # enters the caller engine or a content-addressed package.
        _canonical(value)
        return dict(value["event"])

    def _reserve_evidence(self, value: Mapping[str, Any]) -> None:
        size = len(_canonical(dict(value)).encode("utf-8"))
        if self._stored_evidence_bytes + size > _MAX_STORED_EVIDENCE_BYTES:
            raise CallerSessionError(
                "caller sidecar exceeded the 16 MiB stored-evidence limit"
            )
        self._stored_evidence_bytes += size

    def _await_result(
        self, sequence: int, command: str, *, timeout_seconds: Optional[float] = None
    ) -> Dict[str, Any]:
        self._client.set_timeout(
            self._command_timeout if timeout_seconds is None else timeout_seconds
        )
        try:
            while True:
                value = self._receive_json()
                if value.get("type") == "event":
                    if len(self._pending_events) >= _MAX_PENDING_EVENTS:
                        raise CallerSessionError(
                            "caller sidecar emitted more than 512 events while a command was pending"
                        )
                    event = self._event(value)
                    self._reserve_evidence(event)
                    self._pending_events.append(event)
                    continue
                required = {
                    "schema", "type", "sequence", "command", "status", "receipt"
                }
                if (
                    set(value) != required
                    or value.get("schema") != PROTOCOL_SCHEMA
                    or value.get("type") != "command_result"
                    or value.get("sequence") != sequence
                    or value.get("command") != command
                    or value.get("status") not in {"completed", "unsupported", "error"}
                    or not isinstance(value.get("receipt"), dict)
                ):
                    raise WebSocketProtocolError(
                        "caller sidecar returned an invalid or uncorrelated command_result"
                    )
                _canonical(value)
                receipt = {
                    "sequence": sequence,
                    "command": command,
                    "status": value["status"],
                    "receipt": dict(value["receipt"]),
                }
                if len(self._receipts) >= _MAX_COMMAND_RECEIPTS:
                    raise CallerSessionError(
                        f"caller sidecar exceeded the {_MAX_COMMAND_RECEIPTS}-receipt limit"
                    )
                self._reserve_evidence(receipt)
                self._receipts.append(receipt)
                if value["status"] != "completed":
                    raise CallerSessionError(
                        f"caller sidecar {command} returned {value['status']}"
                    )
                return receipt
        except WebSocketTimeout as exc:
            raise CallerSessionError(
                f"caller sidecar did not acknowledge {command} sequence {sequence}"
            ) from exc
        finally:
            if not self._closed:
                self._client.set_timeout(None)

    def _control(
        self, kind: str, *, timeout_seconds: Optional[float] = None, **payload: Any
    ) -> Dict[str, Any]:
        self._sequence += 1
        sequence = self._sequence
        self._send({
            "schema": PROTOCOL_SCHEMA, "type": "command", "sequence": sequence,
            "command": kind, "payload": payload,
        })
        return self._await_result(
            sequence, kind, timeout_seconds=timeout_seconds
        )

    def capabilities(self) -> Mapping[str, str]:
        return dict(self._capabilities)

    def send_text(self, text: str, metadata: Mapping[str, Any]) -> None:
        if not isinstance(text, str) or not text or len(text) > 200_000:
            raise ValueError("caller text must contain 1..200000 characters")
        if not isinstance(metadata, Mapping):
            raise ValueError("caller text metadata must be a mapping")
        self._control("send_text", text=text, metadata=dict(metadata))

    def send_audio(
        self, pcm_s16le: bytes, sample_rate_hz: int, metadata: Mapping[str, Any]
    ) -> None:
        if not isinstance(pcm_s16le, bytes) or not pcm_s16le or len(pcm_s16le) % 2 or len(pcm_s16le) > _MAX_AUDIO_BYTES:
            raise ValueError("caller audio must be bounded non-empty PCM16LE bytes")
        if isinstance(sample_rate_hz, bool) or not isinstance(sample_rate_hz, int) or not 8_000 <= sample_rate_hz <= 192_000:
            raise ValueError("sample_rate_hz must be in [8000, 192000]")
        if not isinstance(metadata, Mapping):
            raise ValueError("caller audio metadata must be a mapping")
        self._sequence += 1
        sequence = self._sequence
        digest = "sha256:" + hashlib.sha256(pcm_s16le).hexdigest()
        self._send({
            "schema": PROTOCOL_SCHEMA, "type": "command", "sequence": sequence,
            "command": "send_audio", "payload": {
                "sample_rate_hz": sample_rate_hz, "channels": 1,
                "sample_format": "pcm_s16le", "bytes": len(pcm_s16le),
                "sha256": digest, "metadata": dict(metadata),
            },
        })
        self._client.send_binary(_AUDIO_HEADER.pack(_AUDIO_MAGIC, sequence) + pcm_s16le)
        self._await_result(sequence, "send_audio")

    def receive(self, timeout_ms: int) -> Optional[Mapping[str, Any]]:
        if isinstance(timeout_ms, bool) or not isinstance(timeout_ms, int) or not 1 <= timeout_ms <= 300_000:
            raise ValueError("receive timeout_ms must be in [1, 300000]")
        if self._pending_events:
            return dict(self._pending_events.pop(0))
        self._client.set_timeout(timeout_ms / 1000.0)
        try:
            value = self._receive_json()
        except WebSocketTimeout:
            return None
        finally:
            if not self._closed:
                self._client.set_timeout(None)
        return self._event(value)

    def send_dtmf(self, digits: str) -> None:
        if not isinstance(digits, str) or not digits or len(digits) > 64 or any(char not in "0123456789ABCDabcd*#" for char in digits):
            raise ValueError("DTMF digits are invalid")
        self._control("send_dtmf", digits=digits.upper())

    def wait(self, duration_ms: int) -> None:
        self._duration("wait", duration_ms)

    def silence(self, duration_ms: int) -> None:
        self._duration("silence", duration_ms)

    def _duration(self, command: str, duration_ms: int) -> None:
        if isinstance(duration_ms, bool) or not isinstance(duration_ms, int) or not 0 <= duration_ms <= 300_000:
            raise ValueError("duration_ms must be in [0, 300000]")
        self._control(
            command,
            duration_ms=duration_ms,
            timeout_seconds=min(600.0, self._command_timeout + duration_ms / 1000.0),
        )

    def set_impairment(self, profile: Mapping[str, Any]) -> None:
        if not isinstance(profile, Mapping):
            raise ValueError("impairment profile must be a mapping")
        self._control("impairment", profile=dict(profile))

    def hangup(self, reason: str) -> None:
        if not isinstance(reason, str) or not reason or len(reason) > 500:
            raise ValueError("hangup reason must contain 1..500 characters")
        try:
            self._control("hangup", reason=reason)
        finally:
            self.close()

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        self._client.close(1000, "caller session complete")

    def abort(self) -> None:
        if self._closed:
            return
        self._closed = True
        self._client.abort()

    def evidence(self) -> Dict[str, Any]:
        """Return sidecar-reported command receipts, never target-delivery proof."""

        return {
            "schema": "hotato.caller-session-evidence.v1",
            "availability": "available",
            "authority": "sidecar_reported",
            "adapter": dict(self.adapter),
            "connected_endpoint_sha256": self.connected_endpoint_sha256,
            "command_receipts": json.loads(
                json.dumps(self._receipts, allow_nan=False)
            ),
            "limitation": (
                "A completed command receipt proves the sidecar accepted the operation; "
                "target delivery requires a target-boundary event or delivered-media hash."
            ),
        }

    def drain_events(self) -> list:
        """Return and clear events already received between command results."""

        events = [dict(event) for event in self._pending_events]
        self._pending_events.clear()
        return events

    def __enter__(self) -> "WebSocketCallerSession":
        return self

    def __exit__(self, _exc_type: Any, _exc: Any, _tb: Any) -> None:
        self.close()


__all__ = [
    "PROTOCOL_SCHEMA", "SUBPROTOCOL", "CallerSessionError",
    "WebSocketCallerSession",
]
