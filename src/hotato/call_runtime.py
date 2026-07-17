"""Normalized contracts for call control, media sessions, and call evidence.

Hotato does not implement SIP, RTP, or WebRTC in this module.  It defines the
boundary that a telephony controller or a media sidecar must satisfy and the
append-only event format used to preserve what the boundary observed.  A
provider lifecycle receipt and delivered-media evidence are separate facts.
Neither substitutes for the other.
"""

from __future__ import annotations

import hashlib
import ipaddress
import os
import re
import shutil
import signal
import socket
import stat
import subprocess
import threading
import xml.parsers.expat as expat
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import (
    Any,
    Callable,
    Dict,
    Iterable,
    List,
    Mapping,
    Optional,
    Protocol,
    Sequence,
    Tuple,
    runtime_checkable,
)

from .manifest import canonical_json

CALL_EVENT_SCHEMA = "hotato.call-event.v1"
CALL_EVENT_MAX_BYTES = 1024 * 1024
SIDECAR_OUTPUT_MAX_BYTES = 1024 * 1024
_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/-]{0,255}$")
_SHA256_RE = re.compile(r"^sha256:[0-9a-f]{64}$")
_TARGET_RE = re.compile(r"^(?:[A-Za-z0-9]|\[)[A-Za-z0-9.:[\]_-]{0,253}$")
_EXECUTABLE_RE = re.compile(r"^[A-Za-z0-9._+/-]{1,4096}$")
SIPP_REMOTE_ACKNOWLEDGEMENT = (
    "I_ACCEPT_REMOTE_SIP_SIDE_EFFECTS_AND_UNOBSERVABLE_EXTERNAL_COST"
)
SIPP_TRUSTED_SCENARIO_ACKNOWLEDGEMENT = (
    "I_ACCEPT_SIPP_SCENARIO_HOST_COMMAND_AND_FILE_ACCESS_WITHOUT_OS_SANDBOX"
)
SIPP_SCENARIO_POLICY = "hotato.sipp-scenario-policy.v1"
SIPP_SCENARIO_MAX_ELEMENTS = 100_000
SIPP_SCENARIO_MAX_DEPTH = 128
_SIPP_EXTERNAL_KEYWORD_RE = re.compile(
    r"\[(?:file\b|field\d+(?:-\d+)?\b[^\]]*\bfile\s*=)", re.IGNORECASE
)
_SIPP_PATH_VALUE_RE = re.compile(
    r"^(?:[A-Za-z]:[\\/]|[/\\]{1,2}|\.{1,2}[\\/]|~[\\/]|(?:file|https?|ftp)://)",
    re.IGNORECASE,
)
_SIPP_EXTERNAL_ATTRIBUTE_NAMES = frozenset({
    "command",
    "href",
    "path",
    "play_pcap_audio",
    "play_pcap_video",
    "rtp_stream",
    "src",
    "uri",
})
_SIPP_DENIED_SAFE_ELEMENTS = frozenset({"exec"})
_TRUST = frozenset({
    "measured",
    "provider_reported",
    "sidecar_reported",
    "derived",
    "model_reported",
    "operator_attested",
    "unverified",
})


class RuntimeContractError(ValueError):
    """A call-runtime input or evidence chain violated its fixed contract."""


class CapabilityUnavailable(RuntimeError):
    """An operation is unsupported or its result cannot be observed."""


class SidecarError(RuntimeError):
    """A guarded sidecar invocation could not produce a usable receipt."""


class CapabilityState(str, Enum):
    """Three-state support result; unknown never collapses into failure."""

    SUPPORTED = "SUPPORTED"
    UNSUPPORTED = "UNSUPPORTED"
    UNOBSERVABLE = "UNOBSERVABLE"


@dataclass(frozen=True)
class Capability:
    state: CapabilityState
    reason: str
    authority: str = "implementation"

    def to_dict(self) -> Dict[str, str]:
        return {
            "state": self.state.value,
            "reason": self.reason,
            "authority": self.authority,
        }


def capability(state: CapabilityState, reason: str, *, authority: str = "implementation") -> Capability:
    if not isinstance(state, CapabilityState):
        raise TypeError("state must be a CapabilityState")
    if not isinstance(reason, str) or not reason.strip() or len(reason) > 1000:
        raise RuntimeContractError("capability reason must be a bounded non-empty string")
    if not isinstance(authority, str) or not authority.strip() or len(authority) > 100:
        raise RuntimeContractError("capability authority must be a bounded non-empty string")
    return Capability(state, reason.strip(), authority.strip())


@runtime_checkable
class CallController(Protocol):
    """Lifecycle control only; implementations need not carry call media."""

    def capabilities(self, provider: str) -> Mapping[str, Capability]: ...
    def create(self, spec: Any) -> Any: ...
    def get(self, provider: str, call_id: str) -> Any: ...
    def wait(self, handle: Any, *, timeout_seconds: float = 600, poll_seconds: float = 2.0, sleeper: Callable[[float], None]) -> Any: ...
    def cancel(self, handle: Any) -> Any: ...
    def export(self, handle: Any, output_dir: str) -> str: ...
    def cleanup(self, handle: Any, export_path: Optional[str] = None) -> Mapping[str, Any]: ...


@runtime_checkable
class ConversationSession(Protocol):
    """Duplex media/signalling boundary implemented by an external runtime."""

    def capabilities(self) -> Mapping[str, Capability]: ...
    def connect(self) -> None: ...
    def events(self) -> Iterable[Mapping[str, Any]]: ...
    def send_audio(self, pcm_s16le: bytes, *, sample_rate_hz: int, channels: int = 1) -> None: ...
    def send_dtmf(self, digits: str) -> None: ...
    def hold(self, enabled: bool) -> None: ...
    def transfer(self, destination: str, *, warm: bool = False) -> None: ...
    def hangup(self) -> None: ...
    def close(self) -> None: ...


def require_capability(source: Mapping[str, Capability], name: str) -> None:
    """Refuse an operation unless its contract explicitly says SUPPORTED."""

    entry = source.get(name)
    if entry is None:
        raise CapabilityUnavailable(f"capability {name!r} was not declared")
    if not isinstance(entry, Capability):
        raise RuntimeContractError(f"capability {name!r} is not a Capability record")
    if entry.state is not CapabilityState.SUPPORTED:
        raise CapabilityUnavailable(f"capability {name!r} is {entry.state.value}: {entry.reason}")


def _bounded_id(value: Any, label: str) -> str:
    if not isinstance(value, str) or not _ID_RE.fullmatch(value):
        raise RuntimeContractError(f"{label} must match {_ID_RE.pattern}")
    return value


def _canonical_bytes(value: Any) -> bytes:
    try:
        encoded = canonical_json(value).encode("utf-8")
    except (TypeError, ValueError) as exc:
        raise RuntimeContractError("call event must contain canonical JSON values") from exc
    if len(encoded) > CALL_EVENT_MAX_BYTES:
        raise RuntimeContractError("call event exceeds 1 MiB")
    return encoded


def canonical_event_hash(event: Mapping[str, Any]) -> str:
    """Return the hash of an event body, excluding only its claimed hash."""

    if not isinstance(event, Mapping):
        raise RuntimeContractError("call event must be a mapping")
    body = dict(event)
    body.pop("event_hash", None)
    return "sha256:" + hashlib.sha256(_canonical_bytes(body)).hexdigest()


_EVENT_REQUIRED = frozenset({
    "schema", "event_id", "run_id", "call_id", "leg_id", "sequence",
    "source", "kind", "observed_monotonic_ns", "source_timestamp",
    "trace_id", "payload", "raw_sha256", "trust", "previous_event_hash",
})
_EVENT_ALLOWED = _EVENT_REQUIRED | {"event_hash"}


def normalize_call_event(value: Any, previous: Optional[Mapping[str, Any]] = None) -> Dict[str, Any]:
    """Validate, chain, and hash one normalized call event.

    The first event has sequence zero and ``previous_event_hash=null``.  Each
    later event increments sequence by one and names the preceding event hash.
    Supplying a claimed ``event_hash`` verifies it instead of trusting it.
    """

    if not isinstance(value, Mapping):
        raise RuntimeContractError("call event must be a mapping")
    unknown = sorted(set(value) - _EVENT_ALLOWED)
    missing = sorted(_EVENT_REQUIRED - set(value))
    if unknown:
        raise RuntimeContractError("call event contains unknown field(s): " + ", ".join(unknown))
    if missing:
        raise RuntimeContractError("call event is missing field(s): " + ", ".join(missing))
    event = dict(value)
    if event.get("schema") != CALL_EVENT_SCHEMA:
        raise RuntimeContractError(f"call event schema must be {CALL_EVENT_SCHEMA!r}")
    for name in ("event_id", "run_id", "call_id", "leg_id", "source", "kind"):
        event[name] = _bounded_id(event.get(name), name)
    sequence = event.get("sequence")
    if isinstance(sequence, bool) or not isinstance(sequence, int) or sequence < 0:
        raise RuntimeContractError("sequence must be a non-negative integer")
    monotonic = event.get("observed_monotonic_ns")
    if isinstance(monotonic, bool) or not isinstance(monotonic, int) or monotonic < 0:
        raise RuntimeContractError("observed_monotonic_ns must be a non-negative integer")
    for optional in ("source_timestamp", "trace_id"):
        item = event.get(optional)
        if item is not None and (not isinstance(item, str) or not item or len(item) > 512):
            raise RuntimeContractError(f"{optional} must be null or a bounded non-empty string")
    if not isinstance(event.get("payload"), dict):
        raise RuntimeContractError("payload must be a mapping")
    raw_sha = event.get("raw_sha256")
    if not isinstance(raw_sha, str) or not _SHA256_RE.fullmatch(raw_sha):
        raise RuntimeContractError("raw_sha256 must be a sha256:<64 lowercase hex> digest")
    if event.get("trust") not in _TRUST:
        raise RuntimeContractError("trust is not a recognized evidence authority")

    if previous is None:
        if sequence != 0 or event.get("previous_event_hash") is not None:
            raise RuntimeContractError("the first event must have sequence 0 and no previous_event_hash")
    else:
        prior = normalize_call_event(previous, None) if previous.get("sequence") == 0 else dict(previous)
        prior_hash = prior.get("event_hash")
        if not isinstance(prior_hash, str) or prior_hash != canonical_event_hash(prior):
            raise RuntimeContractError("preceding event_hash does not match its canonical event body")
        if sequence != int(prior.get("sequence", -2)) + 1:
            raise RuntimeContractError("call event sequence is not contiguous")
        if event.get("run_id") != prior.get("run_id") or event.get("call_id") != prior.get("call_id"):
            raise RuntimeContractError("a call event chain cannot change run_id or call_id")
        if event.get("previous_event_hash") != prior_hash:
            raise RuntimeContractError("previous_event_hash does not match the preceding event")
        if monotonic < int(prior.get("observed_monotonic_ns", 0)):
            raise RuntimeContractError("observed_monotonic_ns moved backwards")

    expected = canonical_event_hash(event)
    claimed = event.get("event_hash")
    if claimed is not None and claimed != expected:
        raise RuntimeContractError("event_hash does not match the canonical event body")
    event["event_hash"] = expected
    _canonical_bytes(event)
    return event


class AppendOnlyCallLog:
    """In-memory append surface whose snapshot is a verified hash chain."""

    def __init__(self, events: Optional[Iterable[Mapping[str, Any]]] = None) -> None:
        self._events: List[Dict[str, Any]] = []
        for event in events or ():
            self.append(event)

    def append(self, event: Mapping[str, Any]) -> Dict[str, Any]:
        normalized = normalize_call_event(event, self._events[-1] if self._events else None)
        if any(item["event_id"] == normalized["event_id"] for item in self._events):
            raise RuntimeContractError("event_id is already present in this append-only log")
        self._events.append(normalized)
        return dict(normalized)

    def snapshot(self) -> Tuple[Dict[str, Any], ...]:
        return tuple(dict(event) for event in self._events)

    def verify(self) -> str:
        previous: Optional[Dict[str, Any]] = None
        for event in self._events:
            previous = normalize_call_event(event, previous)
        return previous["event_hash"] if previous else "sha256:" + hashlib.sha256(b"").hexdigest()


@dataclass(frozen=True)
class SidecarContract:
    """Declarative boundary for a separately operated media/signalling service."""

    kind: str
    endpoint: str
    capabilities: Mapping[str, Capability]
    evidence_required: Tuple[str, ...]


def livekit_sip_contract(endpoint: str) -> SidecarContract:
    """Describe a LiveKit SIP sidecar; this function opens no socket."""

    endpoint = _endpoint(endpoint)
    pending = lambda operation: capability(  # noqa: E731 - compact fixed matrix
        CapabilityState.UNOBSERVABLE,
        f"{operation} depends on the configured LiveKit/SIP deployment and becomes supported only after session evidence is returned",
        authority="sidecar_contract",
    )
    return SidecarContract(
        "livekit-sip", endpoint,
        {name: pending(name) for name in ("media", "dtmf", "hold", "cold_transfer", "warm_transfer")},
        ("room_events", "participant_events", "delivered_audio_sha256", "sip_status"),
    )


def pipecat_media_contract(endpoint: str) -> SidecarContract:
    """Describe a Pipecat media sidecar; transport stays outside Hotato core."""

    endpoint = _endpoint(endpoint)
    pending = capability(
        CapabilityState.UNOBSERVABLE,
        "media transport depends on the configured Pipecat pipeline and requires delivered-audio evidence",
        authority="sidecar_contract",
    )
    return SidecarContract(
        "pipecat", endpoint,
        {"media": pending, "dtmf": capability(CapabilityState.UNSUPPORTED, "the generic Pipecat media contract does not define DTMF", authority="sidecar_contract"), "hold": capability(CapabilityState.UNSUPPORTED, "the generic Pipecat media contract does not define hold", authority="sidecar_contract"), "cold_transfer": capability(CapabilityState.UNSUPPORTED, "the generic Pipecat media contract does not define transfer", authority="sidecar_contract"), "warm_transfer": capability(CapabilityState.UNSUPPORTED, "the generic Pipecat media contract does not define transfer", authority="sidecar_contract")},
        ("pipeline_events", "delivered_audio_sha256"),
    )


def _endpoint(value: Any) -> str:
    if not isinstance(value, str) or not value.strip() or len(value) > 4096:
        raise RuntimeContractError("sidecar endpoint must be a bounded non-empty string")
    if any(char in value for char in ("\x00", "\r", "\n")):
        raise RuntimeContractError("sidecar endpoint contains a control character")
    return value.strip()


@dataclass(frozen=True)
class SippRunSpec:
    target: str
    scenario_path: str
    calls: int = 1
    rate_per_second: int = 1
    timeout_seconds: int = 60
    executable: str = "sipp"
    allow_remote: bool = False
    remote_ip_allowlist: Tuple[str, ...] = ()
    remote_acknowledgement: Optional[str] = None
    max_remote_calls: Optional[int] = None
    trusted_scenario_acknowledgement: Optional[str] = None


def validate_sipp_spec(value: Any) -> SippRunSpec:
    if isinstance(value, SippRunSpec):
        spec = value
    elif isinstance(value, Mapping):
        allowed = {
            "target", "scenario_path", "calls", "rate_per_second",
            "timeout_seconds", "executable", "allow_remote",
            "remote_ip_allowlist", "remote_acknowledgement", "max_remote_calls",
            "trusted_scenario_acknowledgement",
        }
        unknown = sorted(set(value) - allowed)
        if unknown:
            raise RuntimeContractError("SIPp spec contains unknown field(s): " + ", ".join(unknown))
        spec = SippRunSpec(**dict(value))
    else:
        raise RuntimeContractError("SIPp spec must be a mapping or SippRunSpec")
    if not isinstance(spec.target, str) or not _TARGET_RE.fullmatch(spec.target) or spec.target.startswith("-"):
        raise RuntimeContractError("SIPp target must be a hostname or address with optional port")
    scenario = Path(spec.scenario_path)
    if not scenario.is_file() or scenario.is_symlink():
        raise RuntimeContractError("SIPp scenario_path must name a regular, non-symlink file")
    if scenario.stat().st_size > 4 * 1024 * 1024:
        raise RuntimeContractError("SIPp scenario exceeds 4 MiB")
    if isinstance(spec.calls, bool) or not isinstance(spec.calls, int) or not 1 <= spec.calls <= 100000:
        raise RuntimeContractError("SIPp calls must be an integer in [1, 100000]")
    if isinstance(spec.rate_per_second, bool) or not isinstance(spec.rate_per_second, int) or not 1 <= spec.rate_per_second <= 10000:
        raise RuntimeContractError("SIPp rate_per_second must be an integer in [1, 10000]")
    if isinstance(spec.timeout_seconds, bool) or not isinstance(spec.timeout_seconds, int) or not 1 <= spec.timeout_seconds <= 86400:
        raise RuntimeContractError("SIPp timeout_seconds must be an integer in [1, 86400]")
    if not isinstance(spec.executable, str) or not _EXECUTABLE_RE.fullmatch(spec.executable) or ".." in Path(spec.executable).parts:
        raise RuntimeContractError("SIPp executable is not a safe path or command name")
    if os.path.basename(spec.executable).lower() not in {"sipp", "sipp.exe"}:
        raise RuntimeContractError("SIPp executable basename must be sipp or sipp.exe")
    if not isinstance(spec.allow_remote, bool):
        raise RuntimeContractError("SIPp allow_remote must be boolean")
    if not isinstance(spec.remote_ip_allowlist, (list, tuple)) or len(spec.remote_ip_allowlist) > 64:
        raise RuntimeContractError("SIPp remote_ip_allowlist must contain at most 64 addresses")
    addresses = []
    for item in spec.remote_ip_allowlist:
        if not isinstance(item, str):
            raise RuntimeContractError("SIPp remote_ip_allowlist entries must be IP addresses")
        try:
            addresses.append(str(ipaddress.ip_address(item)))
        except ValueError as exc:
            raise RuntimeContractError("SIPp remote_ip_allowlist entry is invalid") from exc
    if len(set(addresses)) != len(addresses):
        raise RuntimeContractError("SIPp remote_ip_allowlist contains duplicates")
    if spec.allow_remote:
        if spec.remote_acknowledgement != SIPP_REMOTE_ACKNOWLEDGEMENT:
            raise RuntimeContractError("SIPp remote acknowledgement does not match the fixed phrase")
        if (
            isinstance(spec.max_remote_calls, bool)
            or not isinstance(spec.max_remote_calls, int)
            or not 1 <= spec.max_remote_calls <= 100000
            or spec.calls > spec.max_remote_calls
        ):
            raise RuntimeContractError("SIPp calls exceed max_remote_calls")
        if not addresses:
            raise RuntimeContractError("SIPp remote_ip_allowlist is required for remote execution")
    elif addresses or spec.remote_acknowledgement is not None or spec.max_remote_calls is not None:
        raise RuntimeContractError("SIPp remote safety fields require allow_remote=true")
    if spec.trusted_scenario_acknowledgement not in (
        None,
        SIPP_TRUSTED_SCENARIO_ACKNOWLEDGEMENT,
    ):
        raise RuntimeContractError(
            "SIPp trusted-scenario acknowledgement does not match the fixed phrase"
        )
    _validate_sipp_scenario_xml(
        _read_regular_bytes_no_follow(scenario, 4 * 1024 * 1024),
        trusted=bool(spec.trusted_scenario_acknowledgement),
    )
    return SippRunSpec(
        target=spec.target,
        scenario_path=spec.scenario_path,
        calls=spec.calls,
        rate_per_second=spec.rate_per_second,
        timeout_seconds=spec.timeout_seconds,
        executable=spec.executable,
        allow_remote=spec.allow_remote,
        remote_ip_allowlist=tuple(addresses),
        remote_acknowledgement=spec.remote_acknowledgement,
        max_remote_calls=spec.max_remote_calls,
        trusted_scenario_acknowledgement=spec.trusted_scenario_acknowledgement,
    )


def _sipp_destination(spec: SippRunSpec) -> Tuple[str, str, bool]:
    """Resolve once, enforce remote policy, and return an IP-bound target."""

    target = spec.target
    port = 5060
    if target.startswith("["):
        closing = target.find("]")
        if closing < 0:
            raise RuntimeContractError("SIPp IPv6 target must use brackets")
        host = target[1:closing]
        suffix = target[closing + 1:]
        if suffix:
            if not suffix.startswith(":") or not suffix[1:].isdigit():
                raise RuntimeContractError("SIPp target port is invalid")
            port = int(suffix[1:])
    elif target.count(":") == 1:
        host, raw_port = target.rsplit(":", 1)
        if not raw_port.isdigit():
            raise RuntimeContractError("SIPp target port is invalid")
        port = int(raw_port)
    elif ":" in target:
        raise RuntimeContractError("SIPp IPv6 target must use brackets")
    else:
        host = target
    if not host or not 1 <= port <= 65_535:
        raise RuntimeContractError("SIPp target host or port is invalid")
    try:
        rows = socket.getaddrinfo(host, port, type=socket.SOCK_DGRAM)
        resolved = sorted({str(ipaddress.ip_address(row[4][0])) for row in rows})
    except (OSError, ValueError) as exc:
        raise RuntimeContractError("SIPp target could not be resolved") from exc
    if not resolved:
        raise RuntimeContractError("SIPp target resolved to no addresses")
    remote = [item for item in resolved if not ipaddress.ip_address(item).is_loopback]
    if remote:
        if not spec.allow_remote:
            raise RuntimeContractError(
                "SIPp remote target is default-deny; declare allow_remote and its safety fields"
            )
        allowed = set(spec.remote_ip_allowlist)
        candidates = [item for item in remote if item in allowed]
        if not candidates:
            raise RuntimeContractError("SIPp resolved remote target is outside remote_ip_allowlist")
        selected = candidates[0]
    else:
        selected = resolved[0]
    display = f"[{selected}]" if ":" in selected else selected
    return f"{display}:{port}", selected, bool(remote)


def _run_bounded(argv: Sequence[str], cwd: str, timeout: int, env: Mapping[str, str]) -> Tuple[int, bytes, bytes]:
    """Run without a shell and retain at most one MiB per output stream."""

    process = subprocess.Popen(
        list(argv), cwd=cwd, env=dict(env), stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, shell=False,
        start_new_session=True,
    )
    buffers = [bytearray(), bytearray()]
    totals = [0, 0]

    def drain(stream: Any, index: int) -> None:
        while True:
            chunk = stream.read(65536)
            if not chunk:
                break
            totals[index] += len(chunk)
            remaining = SIDECAR_OUTPUT_MAX_BYTES - len(buffers[index])
            if remaining > 0:
                buffers[index].extend(chunk[:remaining])

    threads = [
        threading.Thread(target=drain, args=(process.stdout, 0), daemon=True),
        threading.Thread(target=drain, args=(process.stderr, 1), daemon=True),
    ]
    for thread in threads:
        thread.start()
    try:
        code = process.wait(timeout=timeout)
    except subprocess.TimeoutExpired as exc:
        if os.name == "posix":
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
        else:
            process.kill()
        process.wait()
        raise SidecarError(f"SIPp exceeded the {timeout}-second timeout") from exc
    finally:
        for thread in threads:
            thread.join(timeout=5)
    if any(total > SIDECAR_OUTPUT_MAX_BYTES for total in totals):
        raise SidecarError("SIPp output exceeded the 1 MiB per-stream evidence limit")
    return int(code), bytes(buffers[0]), bytes(buffers[1])


def _exclusive_write(path: Path, data: bytes, *, mode: int = 0o600) -> None:
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    descriptor = os.open(str(path), flags, mode)
    opened = os.fstat(descriptor)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            if hasattr(os, "fchmod"):
                os.fchmod(handle.fileno(), mode)
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
    except BaseException:
        try:
            current = os.lstat(path)
            if (current.st_dev, current.st_ino) == (opened.st_dev, opened.st_ino):
                os.unlink(str(path))
        except OSError:
            pass
        raise


def _read_regular_bytes_no_follow(path: Path, maximum: int) -> bytes:
    """Bind a bounded scenario read to the inode checked before open."""

    before = os.lstat(path)
    if stat.S_ISLNK(before.st_mode) or not stat.S_ISREG(before.st_mode):
        raise RuntimeContractError(
            "SIPp scenario_path must name a regular, non-symlink file"
        )
    if before.st_size > maximum:
        raise RuntimeContractError("SIPp scenario exceeds 4 MiB")
    flags = os.O_RDONLY | getattr(os, "O_BINARY", 0)
    flags |= getattr(os, "O_NONBLOCK", 0) | getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(path, flags)
    try:
        opened = os.fstat(descriptor)
        if (
            not stat.S_ISREG(opened.st_mode)
            or (opened.st_dev, opened.st_ino) != (before.st_dev, before.st_ino)
        ):
            raise RuntimeContractError(
                "SIPp scenario changed while it was being opened"
            )
        chunks: List[bytes] = []
        total = 0
        while total <= maximum:
            chunk = os.read(descriptor, min(65_536, maximum + 1 - total))
            if not chunk:
                break
            chunks.append(chunk)
            total += len(chunk)
        raw = b"".join(chunks)
        if len(raw) > maximum:
            raise RuntimeContractError("SIPp scenario exceeds 4 MiB")
        return raw
    finally:
        os.close(descriptor)


def _xml_local_name(name: str) -> str:
    """Return the local part of an Expat namespace-expanded XML name."""

    return name.rsplit("}", 1)[-1]


def _validate_sipp_scenario_xml(raw: bytes, *, trusted: bool) -> None:
    """Parse one bounded SIPp scenario without resolving DTDs or entities.

    SIPp scenarios are executable input.  The default profile therefore
    rejects every ``exec`` action and every documented scenario-level host
    file reference.  The fixed trusted-scenario acknowledgement disables only
    those feature checks; it never enables DTDs, entity declarations, external
    entity resolution, processing instructions, or unbounded XML structure.
    """

    if not isinstance(raw, bytes) or not raw:
        raise RuntimeContractError("SIPp scenario XML must be non-empty bytes")

    depth = 0
    elements = 0
    root_name: Optional[str] = None
    text_tail = ""

    def reject_doctype(*_args: Any) -> None:
        raise RuntimeContractError("SIPp scenario XML cannot contain a DTD")

    def reject_entity(*_args: Any) -> None:
        raise RuntimeContractError(
            "SIPp scenario XML cannot declare or resolve entities"
        )

    def reject_processing_instruction(*_args: Any) -> None:
        raise RuntimeContractError(
            "SIPp scenario XML cannot contain processing instructions"
        )

    def start_element(name: str, attributes: Mapping[str, str]) -> None:
        nonlocal depth, elements, root_name
        depth += 1
        elements += 1
        if depth > SIPP_SCENARIO_MAX_DEPTH:
            raise RuntimeContractError(
                f"SIPp scenario XML exceeds depth {SIPP_SCENARIO_MAX_DEPTH}"
            )
        if elements > SIPP_SCENARIO_MAX_ELEMENTS:
            raise RuntimeContractError(
                f"SIPp scenario XML exceeds {SIPP_SCENARIO_MAX_ELEMENTS} elements"
            )
        local_name = _xml_local_name(name).lower()
        if elements == 1:
            root_name = local_name
            if root_name != "scenario":
                raise RuntimeContractError(
                    "SIPp scenario XML root element must be scenario"
                )
        # Destination redirection can bypass the target that Hotato resolved,
        # allowlisted, and bound into the receipt.  It is therefore forbidden
        # in every profile, including trusted host-command/file mode.
        if local_name == "setdest":
            raise RuntimeContractError(
                "SIPp setdest actions are denied by static destination policy"
            )
        if not trusted and local_name in _SIPP_DENIED_SAFE_ELEMENTS:
            raise RuntimeContractError(
                f"SIPp {local_name} actions are denied by the default safe "
                "scenario profile"
            )
        if trusted:
            return
        for raw_name, value in attributes.items():
            attribute = _xml_local_name(raw_name).lower()
            is_file_attribute = (
                attribute in {"file", "filename"}
                or attribute.startswith("file_")
                or attribute.endswith("_file")
            )
            if attribute in _SIPP_EXTERNAL_ATTRIBUTE_NAMES or is_file_attribute:
                raise RuntimeContractError(
                    "SIPp external command/file attributes are denied by the "
                    "default safe scenario profile"
                )
            if _SIPP_EXTERNAL_KEYWORD_RE.search(value) or _SIPP_PATH_VALUE_RE.search(
                value.strip()
            ):
                raise RuntimeContractError(
                    "SIPp host file/path references are denied by the default "
                    "safe scenario profile"
                )

    def end_element(_name: str) -> None:
        nonlocal depth
        depth -= 1
        if depth < 0:
            raise RuntimeContractError("SIPp scenario XML structure is invalid")

    def character_data(value: str) -> None:
        nonlocal text_tail
        if trusted or not value:
            return
        window = text_tail + value
        if _SIPP_EXTERNAL_KEYWORD_RE.search(window):
            raise RuntimeContractError(
                "SIPp scenario host-file keywords are denied by the default "
                "safe scenario profile"
            )
        text_tail = window[-512:]

    parser = expat.ParserCreate(namespace_separator="}")
    parser.buffer_text = True
    parser.StartDoctypeDeclHandler = reject_doctype
    parser.EntityDeclHandler = reject_entity
    parser.UnparsedEntityDeclHandler = reject_entity
    parser.ExternalEntityRefHandler = reject_entity
    parser.ProcessingInstructionHandler = reject_processing_instruction
    parser.StartElementHandler = start_element
    parser.EndElementHandler = end_element
    parser.CharacterDataHandler = character_data
    if hasattr(parser, "SetParamEntityParsing"):
        parser.SetParamEntityParsing(expat.XML_PARAM_ENTITY_PARSING_NEVER)
    try:
        for offset in range(0, len(raw), 65_536):
            parser.Parse(raw[offset:offset + 65_536], False)
        parser.Parse(b"", True)
    except RuntimeContractError:
        raise
    except expat.ExpatError as exc:
        raise RuntimeContractError("SIPp scenario XML is not well formed") from exc
    if elements == 0 or root_name != "scenario" or depth != 0:
        raise RuntimeContractError("SIPp scenario XML structure is invalid")


def _stage_sipp_executable(spec: SippRunSpec, root: Path) -> Tuple[str, Dict[str, Any]]:
    resolved = shutil.which(spec.executable)
    if resolved is None:
        raise RuntimeContractError("SIPp executable was not found")
    source = Path(os.path.realpath(resolved))
    try:
        raw = _read_regular_bytes_no_follow(source, 512 * 1024 * 1024)
    except (OSError, RuntimeContractError) as exc:
        raise RuntimeContractError("SIPp executable is not a bounded regular file") from exc
    staged = root / ("sipp-bin" + source.suffix)
    _exclusive_write(staged, raw, mode=0o700)
    return os.fspath(staged), {
        "state": "PRESENT",
        "sha256": "sha256:" + hashlib.sha256(raw).hexdigest(),
        "bytes": len(raw),
        "staged": True,
        "version": "UNOBSERVABLE",
    }


class SippSubprocessAdapter:
    """SIPp adapter with static scenario policy; it is not an OS sandbox."""

    def __init__(self, runner: Callable[[Sequence[str], str, int, Mapping[str, str]], Tuple[int, bytes, bytes]] = _run_bounded) -> None:
        self._runner = runner

    def capabilities(self) -> Mapping[str, Capability]:
        return {
            "sip_scenario": capability(
                CapabilityState.SUPPORTED,
                "runs a bounded SIPp XML scenario; the default static profile "
                "denies host command/file features, while trusted mode has no OS sandbox",
            ),
            "media": capability(CapabilityState.UNOBSERVABLE, "configured RTP playback requires a SIPp scenario and delivered-media evidence at the target"),
            "dtmf": capability(CapabilityState.UNOBSERVABLE, "DTMF requires scenario-level signalling evidence"),
            "hold": capability(CapabilityState.UNOBSERVABLE, "hold requires scenario-level signalling evidence"),
            "cold_transfer": capability(CapabilityState.UNOBSERVABLE, "transfer requires scenario-level signalling evidence"),
            "warm_transfer": capability(CapabilityState.UNSUPPORTED, "the generic SIPp adapter does not orchestrate a second live transfer leg"),
        }

    def run(self, value: Any, output_dir: str) -> Dict[str, Any]:
        spec = validate_sipp_spec(value)
        bound_target, resolved_ip, is_remote = _sipp_destination(spec)
        root = Path(output_dir)
        if root.exists() and (not root.is_dir() or root.is_symlink()):
            raise RuntimeContractError("SIPp output_dir must be a directory and cannot be a symlink")
        root.mkdir(parents=True, exist_ok=True, mode=0o700)
        scenario_raw = _read_regular_bytes_no_follow(
            Path(spec.scenario_path), 4 * 1024 * 1024
        )
        trusted_scenario = bool(spec.trusted_scenario_acknowledgement)
        _validate_sipp_scenario_xml(scenario_raw, trusted=trusted_scenario)
        scenario_sha256 = "sha256:" + hashlib.sha256(scenario_raw).hexdigest()
        # SIPp receives an immutable, private staged copy.  It never reopens the
        # caller-controlled pathname after validation, eliminating a swap-and-
        # restore window during the subprocess execution.
        staged_scenario = root / "scenario.xml"
        _exclusive_write(staged_scenario, scenario_raw)
        if self._runner is _run_bounded:
            executable, executable_identity = _stage_sipp_executable(spec, root)
        else:
            executable = spec.executable
            executable_identity = {
                "state": "UNOBSERVABLE",
                "sha256": None,
                "bytes": None,
                "staged": False,
                "version": "UNOBSERVABLE",
                "reason": "runner was injected; executable identity was not observed",
            }
        argv = (
            executable, bound_target, "-sf", str(staged_scenario.resolve()), "-m", str(spec.calls),
            "-r", str(spec.rate_per_second), "-nostdin",
        )
        environment = {key: os.environ[key] for key in ("PATH", "HOME", "LANG", "LC_ALL") if key in os.environ}
        code, stdout, stderr = self._runner(argv, str(root.resolve()), spec.timeout_seconds, environment)
        if not isinstance(code, int) or not isinstance(stdout, bytes) or not isinstance(stderr, bytes):
            raise SidecarError("SIPp runner returned an invalid result contract")
        if len(stdout) > SIDECAR_OUTPUT_MAX_BYTES or len(stderr) > SIDECAR_OUTPUT_MAX_BYTES:
            raise SidecarError("SIPp output exceeded the 1 MiB per-stream evidence limit")
        observed_scenario_sha256 = "sha256:" + hashlib.sha256(
            _read_regular_bytes_no_follow(staged_scenario, 4 * 1024 * 1024)
        ).hexdigest()
        if observed_scenario_sha256 != scenario_sha256:
            raise SidecarError("SIPp scenario changed during execution")
        if executable_identity["state"] == "PRESENT":
            try:
                observed_executable = _read_regular_bytes_no_follow(
                    Path(executable), 512 * 1024 * 1024
                )
            except (OSError, RuntimeContractError) as exc:
                raise SidecarError("staged SIPp executable became unavailable") from exc
            if (
                "sha256:" + hashlib.sha256(observed_executable).hexdigest()
                != executable_identity["sha256"]
            ):
                raise SidecarError("staged SIPp executable changed during execution")
        _exclusive_write(root / "sipp.stdout", stdout)
        _exclusive_write(root / "sipp.stderr", stderr)
        receipt: Dict[str, Any] = {
            "schema": "hotato.sipp-run.v1",
            "status": "PASS" if code == 0 else "FAIL",
            "exit_code": code,
            "executable": os.path.basename(spec.executable),
            "executable_identity": executable_identity,
            "target_sha256": "sha256:" + hashlib.sha256(spec.target.encode("utf-8")).hexdigest(),
            "resolved_destination": bound_target,
            "resolved_ip": resolved_ip,
            "network_scope": "remote" if is_remote else "loopback",
            "remote_authorization": {
                "allowed": bool(is_remote and spec.allow_remote),
                "max_remote_calls": spec.max_remote_calls if is_remote else None,
                "external_cost_state": "UNOBSERVABLE",
                "external_cost_microusd": None,
            },
            "scenario_policy": {
                "schema": SIPP_SCENARIO_POLICY,
                "profile": (
                    "trusted_host_access" if trusted_scenario else "safe_default"
                ),
                "authority": (
                    "operator_attested"
                    if trusted_scenario
                    else "hotato_static_validation"
                ),
                "command_and_host_file_features": (
                    "UNRESTRICTED"
                    if trusted_scenario
                    else "DENIED_BY_STATIC_POLICY"
                ),
                "destination_redirection": "DENIED_BY_STATIC_POLICY",
                "dtd_and_entity_resolution": "DENIED",
                "os_process_sandbox": "ABSENT",
            },
            "scenario_sha256": scenario_sha256,
            "calls": spec.calls,
            "rate_per_second": spec.rate_per_second,
            "stdout_sha256": "sha256:" + hashlib.sha256(stdout).hexdigest(),
            "stderr_sha256": "sha256:" + hashlib.sha256(stderr).hexdigest(),
            "authority": "sipp_process_reported",
        }
        receipt["receipt_id"] = "sha256:" + hashlib.sha256(_canonical_bytes(receipt)).hexdigest()
        _exclusive_write(root / "sipp.receipt.json", (canonical_json(receipt) + "\n").encode("utf-8"))
        return receipt


__all__ = [
    "AppendOnlyCallLog", "CALL_EVENT_SCHEMA", "CallController", "Capability",
    "CapabilityState", "CapabilityUnavailable", "ConversationSession",
    "RuntimeContractError", "SidecarContract", "SidecarError", "SippRunSpec",
    "SIPP_REMOTE_ACKNOWLEDGEMENT", "SIPP_TRUSTED_SCENARIO_ACKNOWLEDGEMENT",
    "SippSubprocessAdapter", "canonical_event_hash", "capability",
    "livekit_sip_contract", "normalize_call_event", "pipecat_media_contract",
    "require_capability", "validate_sipp_spec",
]
