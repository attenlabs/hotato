"""Optional LiveKit transport for Hotato's bounded caller programs.

This module is deliberately outside the zero-dependency core.  It joins a
LiveKit room as a participant, publishes caller PCM through a local audio
track, consumes remote PCM/transcription events, and can publish SIP DTMF.

The adapter distinguishes three different facts:

* ``sdk_playout_complete`` means the local LiveKit ``AudioSource`` accepted the
  frames and drained its queue;
* ``received_audio_frame`` is PCM observed at this caller participant's input
  boundary; and
* ``delivered_audio_receipt`` is a digest reported by a cooperating target
  participant.  It is target-reported evidence, never silently upgraded to a
  measured carrier or PSTN fact.

The LiveKit SDK is imported only when the concrete driver is constructed.
The synchronous facade exists because ``hotato.caller.CallerSession`` is a
synchronous protocol; the SDK remains on one private asyncio loop thread.
"""

from __future__ import annotations

import asyncio
import concurrent.futures
import hashlib
import importlib
import ipaddress
import json
import os
import queue
import threading
import time
from dataclasses import dataclass
from typing import Any, Callable, Dict, Mapping, Optional, Protocol
from urllib.parse import urlsplit

from .caller import SUPPORTED, UNOBSERVABLE, UNSUPPORTED

EVIDENCE_SCHEMA = "hotato.livekit-evidence.v1"
CONTROL_SCHEMA = "hotato.livekit-control.v1"

_MAX_CONTROL_BYTES = 64 * 1024
_MAX_PCM_BYTES = 64 * 1024 * 1024
_MAX_EVENT_CHARS = 200_000
_DIGEST_PREFIX = "sha256:"
_DTMF_CODES = {
    **{str(value): value for value in range(10)},
    "*": 10,
    "#": 11,
    "A": 12,
    "B": 13,
    "C": 14,
    "D": 15,
}


class LiveKitSessionError(RuntimeError):
    """The LiveKit caller session could not preserve its contract."""


class LiveKitSDKUnavailable(LiveKitSessionError):
    """The optional LiveKit RTC SDK is absent or incomplete."""


class LiveKitCapabilityError(LiveKitSessionError):
    """The requested operation is explicitly unsupported by this adapter."""


class LiveKitTransportDriver(Protocol):
    """Narrow synchronous seam implemented by :class:`LiveKitRTCDriver`."""

    def connect(
        self,
        *,
        url: str,
        token: str,
        target_identity: str,
        sample_rate_hz: int,
        receive_sample_rate_hz: int,
        audio_queue_ms: int,
        max_remote_tracks: int,
        event_sink: Callable[[Mapping[str, Any]], None],
        timeout_seconds: float,
    ) -> None: ...

    def publish_audio(
        self,
        pcm_s16le: bytes,
        *,
        sample_rate_hz: int,
        frame_duration_ms: int,
        timeout_seconds: float,
    ) -> None: ...

    def publish_data(
        self,
        payload: bytes,
        *,
        topic: str,
        destination_identity: str,
        timeout_seconds: float,
    ) -> None: ...

    def publish_dtmf(self, digits: str, *, timeout_seconds: float) -> None: ...

    def close(self, *, timeout_seconds: float) -> None: ...


def _canonical_bytes(value: Any, *, maximum: int = _MAX_CONTROL_BYTES) -> bytes:
    try:
        encoded = json.dumps(
            value,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
            allow_nan=False,
        ).encode("utf-8")
    except (TypeError, ValueError) as exc:
        raise ValueError("LiveKit control data must contain finite JSON values") from exc
    if len(encoded) > maximum:
        raise ValueError(f"LiveKit control data exceeds {maximum} bytes")
    return encoded


def _sha256(value: bytes) -> str:
    return _DIGEST_PREFIX + hashlib.sha256(value).hexdigest()


def _bounded_text(value: Any, label: str, maximum: int = 512) -> str:
    if not isinstance(value, str) or not value or len(value) > maximum:
        raise ValueError(f"{label} must be a bounded non-empty string")
    if any(character in value for character in ("\x00", "\r", "\n")):
        raise ValueError(f"{label} contains a control character")
    return value


def _validate_url(value: Any, allow_remote: bool) -> str:
    value = _bounded_text(value, "LiveKit URL", 4096)
    parsed = urlsplit(value)
    if parsed.scheme not in {"ws", "wss"} or not parsed.hostname:
        raise ValueError("LiveKit URL must use ws:// or wss:// and include a host")
    if parsed.username is not None or parsed.password is not None:
        raise ValueError("LiveKit URL must not contain credentials")
    if parsed.query or parsed.fragment:
        raise ValueError("LiveKit URL must not contain a query or fragment")
    host = parsed.hostname
    loopback = host.lower() == "localhost"
    try:
        loopback = loopback or ipaddress.ip_address(host).is_loopback
    except ValueError:
        pass
    if not loopback and not allow_remote:
        raise ValueError("remote LiveKit egress requires allow_remote=True")
    if not loopback and parsed.scheme != "wss":
        raise ValueError("remote LiveKit connections require wss://")
    return value


def _positive_int(value: Any, label: str, low: int, high: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not low <= value <= high:
        raise ValueError(f"{label} must be in [{low}, {high}]")
    return value


def _positive_number(value: Any, label: str, high: float) -> float:
    if (
        isinstance(value, bool)
        or not isinstance(value, (int, float))
        or not 0 < float(value) <= high
    ):
        raise ValueError(f"{label} must be in (0, {high}]")
    return float(value)


def _identity(value: Any, label: str) -> str:
    value = _bounded_text(value, label, 256)
    if any(ord(character) < 32 or ord(character) == 127 for character in value):
        raise ValueError(f"{label} contains a control character")
    return value


def _topic(value: Any, label: str) -> str:
    value = _bounded_text(value, label, 256)
    if any(ord(character) < 33 or ord(character) > 126 for character in value):
        raise ValueError(f"{label} must contain printable ASCII without spaces")
    return value


@dataclass(frozen=True)
class LiveKitEvidenceState:
    """Truthful evidence availability; this is separate from media control."""

    outgoing_audio_submission: str
    outgoing_audio_delivery: str
    incoming_audio_observation: str
    target_tool_result: str
    target_state_snapshot: str

    def to_dict(self) -> Dict[str, str]:
        return {
            "outgoing_audio_submission": self.outgoing_audio_submission,
            "outgoing_audio_delivery": self.outgoing_audio_delivery,
            "incoming_audio_observation": self.incoming_audio_observation,
            "target_tool_result": self.target_tool_result,
            "target_state_snapshot": self.target_state_snapshot,
        }


class LiveKitCallerSession:
    """Synchronous Hotato caller session backed by a LiveKit room participant.

    ``token`` should be a short-lived, least-privilege room token.  It is passed
    directly to the SDK driver during construction and is not retained on this
    facade or copied into events.  The underlying LiveKit SDK necessarily holds
    connection credentials for the lifetime of its room connection.
    """

    def __init__(
        self,
        url: str,
        token: str,
        *,
        target_identity: str,
        allow_remote: bool = False,
        sample_rate_hz: int = 48_000,
        receive_sample_rate_hz: int = 48_000,
        frame_duration_ms: int = 20,
        audio_queue_ms: int = 1_000,
        connect_timeout_seconds: float = 15.0,
        operation_timeout_seconds: float = 30.0,
        close_timeout_seconds: float = 10.0,
        max_audio_bytes_per_send: int = 16 * 1024 * 1024,
        max_total_audio_bytes: int = 256 * 1024 * 1024,
        max_audio_duration_ms: int = 120_000,
        max_audio_submissions: int = 1_024,
        max_events: int = 1_024,
        max_remote_tracks: int = 4,
        evidence_topic: Optional[str] = None,
        control_topic: Optional[str] = "hotato.control.v1",
        session_id: Optional[str] = None,
        driver: Optional[LiveKitTransportDriver] = None,
    ) -> None:
        self._url = _validate_url(url, allow_remote)
        token = _bounded_text(token, "LiveKit token", 16_384)
        self._target_identity = _identity(target_identity, "target_identity")
        self._sample_rate_hz = _positive_int(sample_rate_hz, "sample_rate_hz", 8_000, 192_000)
        self._receive_sample_rate_hz = _positive_int(
            receive_sample_rate_hz, "receive_sample_rate_hz", 8_000, 192_000
        )
        self._frame_duration_ms = _positive_int(
            frame_duration_ms, "frame_duration_ms", 5, 100
        )
        self._audio_queue_ms = _positive_int(audio_queue_ms, "audio_queue_ms", 100, 10_000)
        self._connect_timeout = _positive_number(
            connect_timeout_seconds, "connect_timeout_seconds", 300
        )
        self._operation_timeout = _positive_number(
            operation_timeout_seconds, "operation_timeout_seconds", 300
        )
        self._close_timeout = _positive_number(
            close_timeout_seconds, "close_timeout_seconds", 60
        )
        self._max_audio_bytes_per_send = _positive_int(
            max_audio_bytes_per_send, "max_audio_bytes_per_send", 2, _MAX_PCM_BYTES
        )
        self._max_total_audio_bytes = _positive_int(
            max_total_audio_bytes,
            "max_total_audio_bytes",
            self._max_audio_bytes_per_send,
            4 * 1024 * 1024 * 1024,
        )
        self._max_audio_duration_ms = _positive_int(
            max_audio_duration_ms, "max_audio_duration_ms", 1, 300_000
        )
        self._max_audio_submissions = _positive_int(
            max_audio_submissions, "max_audio_submissions", 1, 100_000
        )
        max_events = _positive_int(max_events, "max_events", 1, 100_000)
        self._max_remote_tracks = _positive_int(
            max_remote_tracks, "max_remote_tracks", 1, 64
        )
        self._evidence_topic = _topic(evidence_topic, "evidence_topic") if evidence_topic else None
        self._control_topic = _topic(control_topic, "control_topic") if control_topic else None
        self.session_id = session_id or hashlib.sha256(os.urandom(32)).hexdigest()
        self.session_id = _identity(self.session_id, "session_id")

        self._events: "queue.Queue[Dict[str, Any]]" = queue.Queue(maxsize=max_events)
        self._state_lock = threading.Lock()
        self._operation_lock = threading.Lock()
        self._closed = False
        self._fatal: Optional[LiveKitSessionError] = None
        self._submission_sequence = 0
        self._evidence_sequence = 0
        self._total_audio_bytes = 0
        self._delivery_receipts = 0
        self._announced_submission_digests: Dict[int, str] = {}
        self._successful_submissions: "set[int]" = set()
        self._receipted_submissions: "set[int]" = set()
        self._incoming_hasher = hashlib.sha256()
        self._incoming_audio_bytes = 0
        self._incoming_audio_frames = 0
        self._target_evidence_kinds: "set[str]" = set()
        self._driver = driver or LiveKitRTCDriver()
        try:
            self._driver.connect(
                url=self._url,
                token=token,
                target_identity=self._target_identity,
                sample_rate_hz=self._sample_rate_hz,
                receive_sample_rate_hz=self._receive_sample_rate_hz,
                audio_queue_ms=self._audio_queue_ms,
                max_remote_tracks=self._max_remote_tracks,
                event_sink=self._on_transport_event,
                timeout_seconds=self._connect_timeout,
            )
            if self._control_topic:
                self._publish_control(
                    {
                        "schema": CONTROL_SCHEMA,
                        "type": "session_started",
                        "session_id": self.session_id,
                        "caller": "hotato",
                        "evidence_topic": self._evidence_topic,
                    }
                )
        except BaseException:
            self._closed = True
            try:
                self._driver.close(timeout_seconds=self._close_timeout)
            except BaseException:
                pass
            raise

    def __repr__(self) -> str:
        parsed = urlsplit(self._url)
        return (
            f"LiveKitCallerSession(host={parsed.hostname!r}, "
            f"target_identity={self._target_identity!r}, closed={self._closed!r})"
        )

    def capabilities(self) -> Mapping[str, str]:
        return {
            "send_text": UNSUPPORTED,
            "send_audio": SUPPORTED,
            "receive": SUPPORTED,
            "send_dtmf": SUPPORTED,
            "wait": SUPPORTED,
            "silence": SUPPORTED,
            "impairment": UNSUPPORTED,
            "observe_transfer": SUPPORTED if self._evidence_topic else UNOBSERVABLE,
            "hangup": SUPPORTED,
        }

    def evidence_capabilities(self) -> Mapping[str, str]:
        with self._state_lock:
            has_submission = bool(self._successful_submissions)
            has_delivery = bool(self._delivery_receipts)
            has_incoming = bool(self._incoming_audio_frames)
            target_kinds = set(self._target_evidence_kinds)
        state = LiveKitEvidenceState(
            outgoing_audio_submission=(
                SUPPORTED if has_submission else UNOBSERVABLE
            ),
            outgoing_audio_delivery=(
                SUPPORTED if has_delivery else UNOBSERVABLE
            ),
            incoming_audio_observation=(
                SUPPORTED if has_incoming else UNOBSERVABLE
            ),
            target_tool_result=(
                SUPPORTED if "tool_result" in target_kinds else UNOBSERVABLE
            ),
            target_state_snapshot=(
                SUPPORTED
                if "state_snapshot" in target_kinds
                else UNOBSERVABLE
            ),
        )
        return state.to_dict()

    def media_summary(self) -> Mapping[str, Any]:
        """Return digest-only media counters; no credentials or raw PCM."""

        with self._state_lock:
            return {
                "session_id": self.session_id,
                "outgoing_submission_attempts": self._submission_sequence,
                "outgoing_successful_submissions": len(self._successful_submissions),
                "outgoing_pcm_bytes": self._total_audio_bytes,
                "target_delivery_receipts": self._delivery_receipts,
                "incoming_audio_frames": self._incoming_audio_frames,
                "incoming_pcm_bytes": self._incoming_audio_bytes,
                "incoming_stream_sha256": (
                    _DIGEST_PREFIX + self._incoming_hasher.hexdigest()
                ),
                "incoming_authority": "local_livekit_receiver",
                "outgoing_delivery_authority": (
                    "target_participant_reported"
                    if self._delivery_receipts
                    else UNOBSERVABLE
                ),
            }

    def evidence(self) -> Dict[str, Any]:
        """Return the caller-engine boundary record for this session.

        This record is intentionally digest-only.  Local SDK submission,
        locally received media, and target-reported delivery remain separate
        authorities; absence is represented as ``UNOBSERVABLE``.
        """

        return {
            "schema": "hotato.livekit-session-boundary.v1",
            "availability": "available",
            "adapter": "livekit-python-rtc",
            "authority": {
                "outgoing_submission": "local_livekit_sdk",
                "incoming_observation": "local_livekit_receiver",
                "outgoing_delivery": "target_participant_reported",
                "carrier_or_pstn_delivery": UNOBSERVABLE,
            },
            "capabilities": dict(self.evidence_capabilities()),
            "media": dict(self.media_summary()),
            "limitation": (
                "SDK playout completion is not target, SIP, PSTN, or carrier "
                "delivery; target delivery requires a valid session-bound receipt"
            ),
        }

    def send_text(self, text: str, metadata: Mapping[str, Any]) -> None:
        del text, metadata
        raise LiveKitCapabilityError(
            "send_text is unsupported: LiveKit data messages are not spoken caller audio; provide a TTS adapter"
        )

    def send_audio(
        self,
        pcm_s16le: bytes,
        sample_rate_hz: int,
        metadata: Mapping[str, Any],
    ) -> None:
        if not self._operation_lock.acquire(blocking=False):
            raise LiveKitSessionError("concurrent LiveKit media operations are refused")
        try:
            self._send_audio_serial(pcm_s16le, sample_rate_hz, metadata)
        finally:
            self._operation_lock.release()

    def _send_audio_serial(
        self,
        pcm_s16le: bytes,
        sample_rate_hz: int,
        metadata: Mapping[str, Any],
    ) -> None:
        self._ensure_open()
        if not isinstance(pcm_s16le, bytes) or not pcm_s16le or len(pcm_s16le) % 2:
            raise ValueError("caller audio must be non-empty even-length PCM16LE bytes")
        if len(pcm_s16le) > self._max_audio_bytes_per_send:
            raise ValueError("caller audio exceeds max_audio_bytes_per_send")
        if sample_rate_hz != self._sample_rate_hz:
            raise ValueError(
                "caller PCM sample rate differs from the configured LiveKit source; resample explicitly"
            )
        if not isinstance(metadata, Mapping):
            raise ValueError("caller audio metadata must be a mapping")
        metadata_bytes = _canonical_bytes(dict(metadata))
        del metadata_bytes
        duration_ms = len(pcm_s16le) * 1_000 // (2 * sample_rate_hz)
        if duration_ms > self._max_audio_duration_ms:
            raise ValueError("caller audio exceeds max_audio_duration_ms")
        digest = _sha256(pcm_s16le)
        claimed = metadata.get("pcm_sha256")
        if claimed is not None and claimed != digest:
            raise ValueError("caller audio metadata pcm_sha256 does not match the supplied bytes")

        with self._state_lock:
            if self._submission_sequence >= self._max_audio_submissions:
                raise LiveKitSessionError("caller audio exceeds the session submission ceiling")
            if self._total_audio_bytes + len(pcm_s16le) > self._max_total_audio_bytes:
                raise LiveKitSessionError("caller audio exceeds the session byte ceiling")
            self._total_audio_bytes += len(pcm_s16le)
            self._submission_sequence += 1
            sequence = self._submission_sequence
            self._announced_submission_digests[sequence] = digest

        timeout = min(600.0, self._operation_timeout + duration_ms / 1_000.0)
        try:
            if self._control_topic:
                self._publish_control(
                    {
                        "schema": CONTROL_SCHEMA,
                        "type": "audio_submission",
                        "session_id": self.session_id,
                        "sequence": sequence,
                        "pcm_sha256": digest,
                        "bytes": len(pcm_s16le),
                        "sample_rate_hz": sample_rate_hz,
                        "channels": 1,
                    }
                )
            self._driver.publish_audio(
                pcm_s16le,
                sample_rate_hz=sample_rate_hz,
                frame_duration_ms=self._frame_duration_ms,
                timeout_seconds=timeout,
            )
        except BaseException:
            with self._state_lock:
                self._total_audio_bytes -= len(pcm_s16le)
            raise
        with self._state_lock:
            self._successful_submissions.add(sequence)
        self._emit(
            {
                "kind": "custom",
                "event": "audio_submitted",
                "submission_sequence": sequence,
                "pcm_sha256": digest,
                "bytes": len(pcm_s16le),
                "sample_rate_hz": sample_rate_hz,
                "channels": 1,
                "submission_status": "sdk_playout_complete",
                "target_delivery": UNOBSERVABLE,
                "authority": "local_livekit_sdk",
            }
        )

    def receive(self, timeout_ms: int) -> Optional[Mapping[str, Any]]:
        self._ensure_open(allow_fatal=False)
        timeout_ms = _positive_int(timeout_ms, "receive timeout_ms", 1, 300_000)
        fatal = self._fatal
        if fatal is not None:
            raise fatal
        try:
            return dict(self._events.get(timeout=timeout_ms / 1_000.0))
        except queue.Empty:
            if self._fatal is not None:
                raise self._fatal
            return None

    def send_dtmf(self, digits: str) -> None:
        if not self._operation_lock.acquire(blocking=False):
            raise LiveKitSessionError("concurrent LiveKit media operations are refused")
        try:
            self._send_dtmf_serial(digits)
        finally:
            self._operation_lock.release()

    def _send_dtmf_serial(self, digits: str) -> None:
        self._ensure_open()
        if (
            not isinstance(digits, str)
            or not digits
            or len(digits) > 64
            or any(character.upper() not in _DTMF_CODES for character in digits)
        ):
            raise ValueError("DTMF digits are invalid")
        self._driver.publish_dtmf(
            digits.upper(),
            timeout_seconds=min(300.0, self._operation_timeout + len(digits) * 0.1),
        )
        self._emit(
            {
                "kind": "custom",
                "event": "dtmf_submitted",
                "digits": digits.upper(),
                "authority": "local_livekit_sdk",
                "target_delivery": UNOBSERVABLE,
            }
        )

    def wait(self, duration_ms: int) -> None:
        self._sleep(duration_ms)

    def silence(self, duration_ms: int) -> None:
        self._sleep(duration_ms)

    def _sleep(self, duration_ms: int) -> None:
        self._ensure_open()
        duration_ms = _positive_int(duration_ms, "duration_ms", 0, 300_000)
        time.sleep(duration_ms / 1_000.0)

    def set_impairment(self, profile: Mapping[str, Any]) -> None:
        del profile
        raise LiveKitCapabilityError(
            "media impairment is unsupported in the direct LiveKit adapter; use an evidenced media sidecar"
        )

    def hangup(self, reason: str) -> None:
        _bounded_text(reason, "hangup reason", 500)
        self.close()

    def close(self) -> None:
        with self._state_lock:
            if self._closed:
                return
            self._closed = True
        self._driver.close(timeout_seconds=self._close_timeout)

    def abort(self) -> None:
        self.close()

    def __enter__(self) -> "LiveKitCallerSession":
        return self

    def __exit__(self, _exc_type: Any, _exc: Any, _tb: Any) -> None:
        self.close()

    def _ensure_open(self, *, allow_fatal: bool = True) -> None:
        if self._closed:
            raise LiveKitSessionError("LiveKit caller session is closed")
        if allow_fatal and self._fatal is not None:
            raise self._fatal

    def _publish_control(self, value: Mapping[str, Any]) -> None:
        if not self._control_topic:
            return
        self._driver.publish_data(
            _canonical_bytes(dict(value)),
            topic=self._control_topic,
            destination_identity=self._target_identity,
            timeout_seconds=self._operation_timeout,
        )

    def _emit(self, event: Mapping[str, Any]) -> None:
        try:
            encoded = _canonical_bytes(dict(event), maximum=_MAX_EVENT_CHARS)
            normalized = json.loads(encoded.decode("utf-8"))
        except (TypeError, ValueError) as exc:
            self._set_fatal("LiveKit produced an invalid or oversized caller event", exc)
            return
        try:
            self._events.put_nowait(normalized)
        except queue.Full as exc:
            self._set_fatal("LiveKit caller event queue overflowed; evidence was not silently dropped", exc)

    def _set_fatal(self, message: str, cause: BaseException) -> None:
        del cause
        with self._state_lock:
            if self._fatal is None:
                self._fatal = LiveKitSessionError(message)

    def _on_transport_event(self, raw: Mapping[str, Any]) -> None:
        if self._closed:
            return
        if not isinstance(raw, Mapping):
            self._set_fatal("LiveKit driver emitted a non-mapping event", TypeError())
            return
        event_type = raw.get("transport_event")
        if event_type == "audio_frame":
            self._handle_audio_frame(raw)
        elif event_type == "transcription":
            self._handle_transcription(raw)
        elif event_type == "data":
            self._handle_data(raw)
        elif event_type == "dtmf":
            if not self._from_target(raw):
                return
            self._emit(
                {
                    "kind": "dtmf",
                    "digits": str(raw.get("digit", "")),
                    "participant_identity": raw.get("participant_identity"),
                    "authority": "livekit_room_event",
                }
            )
        elif event_type == "lifecycle":
            self._emit(
                {
                    "kind": "lifecycle",
                    "status": str(raw.get("status", "unknown"))[:128],
                    "participant_identity": raw.get("participant_identity"),
                    "authority": "livekit_room_event",
                }
            )
        elif event_type == "track_rejected":
            self._emit(
                {
                    "kind": "custom",
                    "event": "remote_track_rejected",
                    "reason": str(raw.get("reason", "resource_limit"))[:256],
                    "authority": "local_livekit_adapter",
                }
            )
        else:
            self._set_fatal("LiveKit driver emitted an unknown event type", ValueError())

    def _from_target(self, raw: Mapping[str, Any]) -> bool:
        return raw.get("participant_identity") == self._target_identity

    def _handle_audio_frame(self, raw: Mapping[str, Any]) -> None:
        if not self._from_target(raw):
            return
        pcm = raw.get("pcm_s16le")
        if not isinstance(pcm, bytes) or not pcm or len(pcm) % 2:
            self._set_fatal("LiveKit driver emitted invalid PCM", ValueError())
            return
        if len(pcm) > self._receive_sample_rate_hz * 2:
            self._set_fatal("LiveKit driver emitted an audio frame longer than one second", ValueError())
            return
        with self._state_lock:
            self._incoming_hasher.update(pcm)
            self._incoming_audio_bytes += len(pcm)
            self._incoming_audio_frames += 1
            frame_sequence = self._incoming_audio_frames
            stream_bytes = self._incoming_audio_bytes
            stream_digest = _DIGEST_PREFIX + self._incoming_hasher.hexdigest()
        self._emit(
            {
                "kind": "custom",
                "event": "received_audio_frame",
                "pcm_sha256": _sha256(pcm),
                "bytes": len(pcm),
                "frame_sequence": frame_sequence,
                "stream_bytes": stream_bytes,
                "stream_sha256": stream_digest,
                "sample_rate_hz": raw.get("sample_rate_hz"),
                "channels": raw.get("channels"),
                "participant_identity": self._target_identity,
                "authority": "local_livekit_receiver",
            }
        )

    def _handle_transcription(self, raw: Mapping[str, Any]) -> None:
        if not self._from_target(raw):
            return
        text = raw.get("text")
        if not isinstance(text, str) or not text or len(text) > 200_000:
            self._set_fatal("LiveKit transcription was invalid or oversized", ValueError())
            return
        self._emit(
            {
                "kind": "transcript",
                "text": text,
                "final": bool(raw.get("final", False)),
                "language": raw.get("language"),
                "participant_identity": self._target_identity,
                "authority": "livekit_transcription_event",
            }
        )

    def _handle_data(self, raw: Mapping[str, Any]) -> None:
        if not self._evidence_topic or raw.get("topic") != self._evidence_topic:
            return
        if not self._from_target(raw):
            return
        payload = raw.get("payload")
        if not isinstance(payload, bytes) or len(payload) > _MAX_CONTROL_BYTES:
            self._reject_evidence(b"", "payload_invalid")
            return
        try:
            value = json.loads(
                payload.decode("utf-8"),
                parse_constant=lambda token: (_ for _ in ()).throw(ValueError(token)),
            )
            self._accept_evidence(value, payload)
        except (UnicodeDecodeError, json.JSONDecodeError, ValueError, TypeError) as exc:
            self._reject_evidence(payload, type(exc).__name__)

    def _reject_evidence(self, payload: bytes, reason: str) -> None:
        self._emit(
            {
                "kind": "custom",
                "event": "target_evidence_rejected",
                "raw_sha256": _sha256(payload),
                "reason": reason[:128],
                "authority": "local_livekit_adapter",
            }
        )

    def _accept_evidence(self, value: Any, raw: bytes) -> None:
        if not isinstance(value, dict) or set(value) != {
            "schema", "session_id", "sequence", "kind", "payload"
        }:
            raise ValueError("evidence envelope fields are invalid")
        if value.get("schema") != EVIDENCE_SCHEMA or value.get("session_id") != self.session_id:
            raise ValueError("evidence schema or session binding is invalid")
        sequence = value.get("sequence")
        if isinstance(sequence, bool) or not isinstance(sequence, int):
            raise ValueError("evidence sequence must be an integer")
        if sequence != self._evidence_sequence + 1:
            raise ValueError("evidence sequence is not contiguous")
        kind = value.get("kind")
        if kind not in {
            "transcript", "tool_result", "state_snapshot", "transfer", "hold",
            "timing", "audio_receipt", "custom",
        }:
            raise ValueError("evidence kind is unsupported")
        payload = value.get("payload")
        if not isinstance(payload, dict):
            raise ValueError("evidence payload must be a mapping")
        normalized_payload = json.loads(_canonical_bytes(payload).decode("utf-8"))
        receipt_hash = _sha256(raw)
        if kind == "audio_receipt":
            event = self._audio_receipt(normalized_payload, receipt_hash, sequence)
        else:
            reserved = {
                "kind", "authority", "source_sequence", "receipt_sha256",
                "event_sha256", "sequence",
            }
            if reserved.intersection(normalized_payload):
                raise ValueError("evidence payload attempts to replace envelope authority fields")
            event = {
                "kind": kind,
                **normalized_payload,
                "source_sequence": sequence,
                "receipt_sha256": receipt_hash,
                "authority": "target_participant_reported",
            }
            with self._state_lock:
                self._target_evidence_kinds.add(kind)
        self._evidence_sequence = sequence
        self._emit(event)

    def _audio_receipt(
        self, payload: Mapping[str, Any], receipt_hash: str, sequence: int
    ) -> Dict[str, Any]:
        required = {
            "submission_sequence", "submitted_sha256", "delivered_sha256",
            "delivered_bytes", "sample_rate_hz", "channels", "boundary",
        }
        if set(payload) != required:
            raise ValueError("audio receipt fields are invalid")
        submission = payload.get("submission_sequence")
        delivered_bytes = payload.get("delivered_bytes")
        rate = payload.get("sample_rate_hz")
        channels = payload.get("channels")
        if (
            isinstance(submission, bool) or not isinstance(submission, int)
            or not 1 <= submission <= self._submission_sequence
        ):
            raise ValueError("audio receipt submission_sequence is invalid")
        for name in ("submitted_sha256", "delivered_sha256"):
            digest = payload.get(name)
            if (
                not isinstance(digest, str)
                or len(digest) != len(_DIGEST_PREFIX) + 64
                or not digest.startswith(_DIGEST_PREFIX)
            ):
                raise ValueError(f"audio receipt {name} is invalid")
            try:
                int(digest[len(_DIGEST_PREFIX):], 16)
            except ValueError as exc:
                raise ValueError(f"audio receipt {name} is invalid") from exc
        _positive_int(delivered_bytes, "delivered_bytes", 1, _MAX_PCM_BYTES)
        _positive_int(rate, "sample_rate_hz", 8_000, 192_000)
        _positive_int(channels, "channels", 1, 8)
        boundary = _bounded_text(payload.get("boundary"), "boundary", 256)
        with self._state_lock:
            if self._announced_submission_digests.get(submission) != payload.get("submitted_sha256"):
                raise ValueError("audio receipt does not match the announced submission digest")
            if submission in self._receipted_submissions:
                raise ValueError("audio receipt duplicates a completed submission")
            self._receipted_submissions.add(submission)
            self._delivery_receipts += 1
        return {
            "kind": "custom",
            "event": "delivered_audio_receipt",
            **dict(payload),
            "boundary": boundary,
            "source_sequence": sequence,
            "receipt_sha256": receipt_hash,
            "authority": "target_participant_reported",
        }


class LiveKitRTCDriver:
    """Concrete optional driver for the official ``livekit.rtc`` Python SDK."""

    def __init__(self, *, rtc_module: Optional[Any] = None) -> None:
        if rtc_module is None:
            try:
                rtc_module = importlib.import_module("livekit.rtc")
            except (ImportError, ModuleNotFoundError) as exc:
                raise LiveKitSDKUnavailable(
                    "LiveKit RTC support requires the optional hotato[livekit] extra"
                ) from exc
        required = {
            "Room", "AudioSource", "AudioFrame", "AudioStream", "LocalAudioTrack",
            "ParticipantTrackPermission", "TrackPublishOptions", "TrackSource", "TrackKind",
            "RoomOptions",
        }
        missing = sorted(name for name in required if not hasattr(rtc_module, name))
        if missing:
            raise LiveKitSDKUnavailable(
                "installed LiveKit RTC SDK is missing: " + ", ".join(missing)
            )
        self._rtc = rtc_module
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._thread: Optional[threading.Thread] = None
        self._room: Optional[Any] = None
        self._source: Optional[Any] = None
        self._event_sink: Optional[Callable[[Mapping[str, Any]], None]] = None
        self._target_identity = ""
        self._receive_sample_rate_hz = 48_000
        self._max_remote_tracks = 1
        self._remote_tasks: "set[asyncio.Task[Any]]" = set()
        self._remote_track_keys: "set[str]" = set()
        self._closed = False

    def connect(
        self,
        *,
        url: str,
        token: str,
        target_identity: str,
        sample_rate_hz: int,
        receive_sample_rate_hz: int,
        audio_queue_ms: int,
        max_remote_tracks: int,
        event_sink: Callable[[Mapping[str, Any]], None],
        timeout_seconds: float,
    ) -> None:
        if self._loop is not None or self._closed:
            raise LiveKitSessionError("LiveKit RTC driver cannot be connected twice")
        self._event_sink = event_sink
        self._target_identity = target_identity
        self._receive_sample_rate_hz = receive_sample_rate_hz
        self._max_remote_tracks = max_remote_tracks
        loop = asyncio.new_event_loop()
        ready = threading.Event()

        def run() -> None:
            asyncio.set_event_loop(loop)
            ready.set()
            loop.run_forever()
            pending = asyncio.all_tasks(loop)
            for task in pending:
                task.cancel()
            if pending:
                loop.run_until_complete(asyncio.gather(*pending, return_exceptions=True))
            loop.close()

        thread = threading.Thread(target=run, name="hotato-livekit-rtc", daemon=True)
        self._loop = loop
        self._thread = thread
        thread.start()
        if not ready.wait(min(timeout_seconds, 5.0)):
            self._stop_loop()
            raise LiveKitSessionError("LiveKit RTC event loop did not start")
        try:
            self._submit(
                self._connect_async(
                    url=url,
                    token=token,
                    sample_rate_hz=sample_rate_hz,
                    audio_queue_ms=audio_queue_ms,
                ),
                timeout_seconds,
                "connect",
            )
        except BaseException:
            self.close(timeout_seconds=min(timeout_seconds, 10.0))
            raise

    async def _connect_async(
        self, *, url: str, token: str, sample_rate_hz: int, audio_queue_ms: int
    ) -> None:
        room = self._rtc.Room()
        self._room = room
        self._register_room_events(room)
        try:
            await room.connect(
                url,
                token,
                options=self._rtc.RoomOptions(auto_subscribe=False),
            )
            self._scan_existing_target_tracks(room)
            permission = self._rtc.ParticipantTrackPermission(
                participant_identity=self._target_identity,
                allow_all=True,
                allowed_track_sids=[],
            )
            # Apply before publishing so another room participant cannot create
            # a subscription in the gap between track publication and policy.
            room.local_participant.set_track_subscription_permissions(
                allow_all_participants=False,
                participant_permissions=[permission],
            )
            source = self._rtc.AudioSource(
                sample_rate_hz,
                1,
                queue_size_ms=audio_queue_ms,
                loop=self._loop,
            )
            track = self._rtc.LocalAudioTrack.create_audio_track("hotato-caller", source)
            options = self._rtc.TrackPublishOptions()
            options.source = self._rtc.TrackSource.SOURCE_MICROPHONE
            await room.local_participant.publish_track(track, options)
            self._source = source
            self._sink(
                {
                    "transport_event": "lifecycle",
                    "status": "connected",
                    "participant_identity": None,
                }
            )
        except BaseException:
            try:
                await room.disconnect()
            except BaseException:
                pass
            raise

    def _register_room_events(self, room: Any) -> None:
        def register(name: str, callback: Callable[..., None]) -> None:
            room.on(name)(callback)

        def participant_connected(participant: Any) -> None:
            identity = getattr(participant, "identity", None)
            if identity == self._target_identity:
                self._sink({
                    "transport_event": "lifecycle",
                    "status": "target_connected",
                    "participant_identity": identity,
                })

        def participant_disconnected(participant: Any) -> None:
            identity = getattr(participant, "identity", None)
            if identity == self._target_identity:
                self._sink({
                    "transport_event": "lifecycle",
                    "status": "target_disconnected",
                    "participant_identity": identity,
                })

        def track_subscribed(track: Any, _publication: Any, participant: Any) -> None:
            identity = getattr(participant, "identity", None)
            if identity != self._target_identity:
                return
            if getattr(track, "kind", None) != self._rtc.TrackKind.KIND_AUDIO:
                return
            self._start_remote_audio(track, identity)

        def track_published(publication: Any, participant: Any) -> None:
            identity = getattr(participant, "identity", None)
            if identity != self._target_identity:
                return
            self._subscribe_target_publication(publication, identity)

        def transcription_received(
            segments: Any, participant: Any, _publication: Any
        ) -> None:
            identity = getattr(participant, "identity", None)
            if identity != self._target_identity:
                return
            for segment in list(segments)[:256]:
                self._sink({
                    "transport_event": "transcription",
                    "participant_identity": identity,
                    "text": getattr(segment, "text", ""),
                    "final": getattr(segment, "final", False),
                    "language": getattr(segment, "language", None),
                })

        def data_received(packet: Any) -> None:
            participant = getattr(packet, "participant", None)
            self._sink({
                "transport_event": "data",
                "participant_identity": getattr(participant, "identity", None),
                "topic": getattr(packet, "topic", ""),
                "payload": bytes(getattr(packet, "data", b"")),
            })

        def dtmf_received(value: Any) -> None:
            participant = getattr(value, "participant", None)
            self._sink({
                "transport_event": "dtmf",
                "participant_identity": getattr(participant, "identity", None),
                "digit": getattr(value, "digit", ""),
            })

        register("participant_connected", participant_connected)
        register("participant_disconnected", participant_disconnected)
        register("track_published", track_published)
        register("track_subscribed", track_subscribed)
        register("transcription_received", transcription_received)
        register("data_received", data_received)
        register("sip_dtmf_received", dtmf_received)
        register(
            "reconnecting",
            lambda: self._sink({
                "transport_event": "lifecycle", "status": "reconnecting",
                "participant_identity": None,
            }),
        )
        register(
            "reconnected",
            lambda: self._sink({
                "transport_event": "lifecycle", "status": "reconnected",
                "participant_identity": None,
            }),
        )
        register(
            "disconnected",
            lambda _reason: self._sink({
                "transport_event": "lifecycle", "status": "disconnected",
                "participant_identity": None,
            }),
        )

    def _scan_existing_target_tracks(self, room: Any) -> None:
        participants = getattr(room, "remote_participants", {})
        if not isinstance(participants, Mapping):
            return
        for participant in participants.values():
            identity = getattr(participant, "identity", None)
            if identity != self._target_identity:
                continue
            self._sink({
                "transport_event": "lifecycle",
                "status": "target_present_at_connect",
                "participant_identity": identity,
            })
            publications = getattr(participant, "track_publications", {})
            if not isinstance(publications, Mapping):
                continue
            for publication in publications.values():
                self._subscribe_target_publication(publication, identity)

    def _subscribe_target_publication(self, publication: Any, identity: str) -> None:
        """Subscribe only to target audio after connecting with auto-subscribe off."""

        if identity != self._target_identity:
            return
        track = getattr(publication, "track", None)
        kind = getattr(publication, "kind", None)
        if kind is None and track is not None:
            kind = getattr(track, "kind", None)
        if kind != self._rtc.TrackKind.KIND_AUDIO:
            return
        subscribe = getattr(publication, "set_subscribed", None)
        if not callable(subscribe):
            self._sink({
                "transport_event": "track_rejected",
                "reason": "target_publication_cannot_subscribe",
            })
            return
        subscribe(True)
        if track is not None:
            self._start_remote_audio(track, identity)

    def _start_remote_audio(self, track: Any, identity: str) -> None:
        key = str(getattr(track, "sid", "") or id(track))
        if key in self._remote_track_keys:
            return
        if len(self._remote_tasks) >= self._max_remote_tracks:
            self._sink({
                "transport_event": "track_rejected",
                "reason": "max_remote_tracks",
            })
            return
        task = asyncio.create_task(self._consume_audio(track, identity))
        self._remote_tasks.add(task)
        self._remote_track_keys.add(key)

        def completed(done: "asyncio.Task[Any]") -> None:
            self._remote_tasks.discard(done)
            self._remote_track_keys.discard(key)

        task.add_done_callback(completed)

    async def _consume_audio(self, track: Any, identity: str) -> None:
        stream = self._rtc.AudioStream(
            track,
            loop=self._loop,
            capacity=64,
            sample_rate=self._receive_sample_rate_hz,
            num_channels=1,
            frame_size_ms=20,
        )
        try:
            async for value in stream:
                frame = value.frame
                self._sink({
                    "transport_event": "audio_frame",
                    "participant_identity": identity,
                    "pcm_s16le": bytes(frame.data),
                    "sample_rate_hz": int(frame.sample_rate),
                    "channels": int(frame.num_channels),
                })
        except asyncio.CancelledError:
            raise
        except BaseException:
            self._sink({
                "transport_event": "track_rejected",
                "reason": "audio_stream_error",
            })
        finally:
            try:
                await stream.aclose()
            except BaseException:
                pass

    def publish_audio(
        self,
        pcm_s16le: bytes,
        *,
        sample_rate_hz: int,
        frame_duration_ms: int,
        timeout_seconds: float,
    ) -> None:
        self._submit(
            self._publish_audio_async(
                pcm_s16le,
                sample_rate_hz=sample_rate_hz,
                frame_duration_ms=frame_duration_ms,
            ),
            timeout_seconds,
            "publish_audio",
        )

    async def _publish_audio_async(
        self, pcm_s16le: bytes, *, sample_rate_hz: int, frame_duration_ms: int
    ) -> None:
        if self._source is None:
            raise LiveKitSessionError("LiveKit audio source is unavailable")
        frame_bytes = sample_rate_hz * 2 * frame_duration_ms // 1_000
        frame_bytes -= frame_bytes % 2
        if frame_bytes <= 0:
            raise LiveKitSessionError("LiveKit audio frame size is invalid")
        for offset in range(0, len(pcm_s16le), frame_bytes):
            chunk = pcm_s16le[offset:offset + frame_bytes]
            frame = self._rtc.AudioFrame(
                data=chunk,
                sample_rate=sample_rate_hz,
                num_channels=1,
                samples_per_channel=len(chunk) // 2,
            )
            await self._source.capture_frame(frame)
        await self._source.wait_for_playout()

    def publish_data(
        self,
        payload: bytes,
        *,
        topic: str,
        destination_identity: str,
        timeout_seconds: float,
    ) -> None:
        async def publish() -> None:
            if self._room is None:
                raise LiveKitSessionError("LiveKit room is unavailable")
            await self._room.local_participant.publish_data(
                payload,
                reliable=True,
                destination_identities=[destination_identity],
                topic=topic,
            )

        self._submit(publish(), timeout_seconds, "publish_data")

    def publish_dtmf(self, digits: str, *, timeout_seconds: float) -> None:
        async def publish() -> None:
            if self._room is None:
                raise LiveKitSessionError("LiveKit room is unavailable")
            for index, digit in enumerate(digits):
                if index:
                    await asyncio.sleep(0.05)
                await self._room.local_participant.publish_dtmf(
                    code=_DTMF_CODES[digit], digit=digit
                )

        self._submit(publish(), timeout_seconds, "publish_dtmf")

    def close(self, *, timeout_seconds: float) -> None:
        if self._closed:
            return
        self._closed = True
        loop = self._loop
        if loop is not None and loop.is_running():
            try:
                self._submit(self._close_async(), timeout_seconds, "close")
            finally:
                self._stop_loop()

    async def _close_async(self) -> None:
        tasks = list(self._remote_tasks)
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        if self._source is not None:
            await self._source.aclose()
            self._source = None
        if self._room is not None:
            await self._room.disconnect()
            self._room = None

    def _submit(self, coroutine: Any, timeout_seconds: float, operation: str) -> Any:
        loop = self._loop
        if loop is None or not loop.is_running():
            try:
                coroutine.close()
            except (AttributeError, RuntimeError):
                pass
            raise LiveKitSessionError(f"LiveKit {operation} refused because the event loop is stopped")
        future = asyncio.run_coroutine_threadsafe(coroutine, loop)
        try:
            return future.result(timeout=timeout_seconds)
        except concurrent.futures.TimeoutError as exc:
            future.cancel()
            raise LiveKitSessionError(f"LiveKit {operation} exceeded its bounded timeout") from exc
        except BaseException as exc:
            # SDK exception messages can contain endpoint details.  Expose only
            # the operation and exception type; preserve the cause in-process.
            raise LiveKitSessionError(
                f"LiveKit {operation} failed ({type(exc).__name__})"
            ) from exc

    def _sink(self, value: Mapping[str, Any]) -> None:
        if self._event_sink is not None:
            self._event_sink(value)

    def _stop_loop(self) -> None:
        loop = self._loop
        thread = self._thread
        if loop is not None and loop.is_running():
            loop.call_soon_threadsafe(loop.stop)
        if thread is not None and thread is not threading.current_thread():
            thread.join(timeout=5.0)
        self._loop = None
        self._thread = None


__all__ = [
    "CONTROL_SCHEMA",
    "EVIDENCE_SCHEMA",
    "LiveKitCallerSession",
    "LiveKitCapabilityError",
    "LiveKitEvidenceState",
    "LiveKitRTCDriver",
    "LiveKitSDKUnavailable",
    "LiveKitSessionError",
    "LiveKitTransportDriver",
]
