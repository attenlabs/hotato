"""Process-isolated, evidence-preserving load execution for caller programs.

This module schedules :mod:`hotato.caller` plans under closed-concurrency and
open-arrival workloads.  Each started invocation owns one independently
verifiable caller package.  The aggregate result keeps scheduling, caller
termination, package verification, and target-delivery evidence in separate
lanes; it never turns them into a blended quality score.

The verifier is deliberately offline.  It performs bounded, no-follow reads,
verifies every child caller package, rebinds each child to its stage/index, and
recomputes metrics, SLOs, status, and exit code without constructing a session,
model, or TTS adapter.
"""

from __future__ import annotations

import hashlib
import json
import math
import multiprocessing
import os
import shutil
import signal
import stat
import tempfile
import time
import urllib.parse
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Dict, List, Mapping, Optional, Sequence, Tuple

from . import caller

PLAN_SCHEMA = "hotato.caller-load-plan.v1"
RESULT_SCHEMA = "hotato.caller-load-result.v1"
PACKAGE_SCHEMA = "hotato.caller-load-package.v1"
CONTEXT_SCHEMA = "hotato.caller-load-context.v1"

_MAX_CALLS = 100_000
_MAX_CONCURRENCY = 1_000
_MAX_STAGES = 100
_MAX_JSON_BYTES = 64 * 1024 * 1024
_MAX_FILE_BYTES = 256 * 1024 * 1024
_MAX_PACKAGE_FILES = 1_000_000
_EVIDENCE_STATES = ("PRESENT", "MISSING", "UNSUPPORTED", "UNOBSERVABLE")
_ENDPOINT_STATES = ("MATCHED", "MISSING", "MISMATCH", "NOT_REQUIRED")
_TERMINAL_STATUSES = ("COMPLETED", "HUNG_UP", "BLOCKED", "ERROR")
DELIVERED_AUDIO_EVENT = "hotato.delivered-audio.v1"
_DELIVERY_AUTHORITIES = {
    "target_boundary", "target_participant_reported", "carrier_boundary",
}
REMOTE_ACKNOWLEDGEMENT = (
    "I_ACCEPT_REMOTE_CALL_SIDE_EFFECTS_AND_UNOBSERVABLE_EXTERNAL_COST"
)


@dataclass(frozen=True)
class CallerLoadRun:
    output_dir: str
    result: Dict[str, Any]
    verification: Dict[str, Any]

    @property
    def exit_code(self) -> int:
        return int(self.result["exit_code"])


class CallerLoadError(RuntimeError):
    """The workload could not be prepared or safely persisted."""


@dataclass
class _Active:
    process: multiprocessing.Process
    context: Dict[str, Any]
    child_plan: Dict[str, Any]
    working_dir: Path
    final_dir: Path
    stage_started: float
    scheduled_offset_ms: float
    scheduling_delay_ms: float
    start_monotonic: float


def _canonical(value: Any) -> bytes:
    try:
        return json.dumps(
            value, sort_keys=True, separators=(",", ":"), ensure_ascii=False,
            allow_nan=False,
        ).encode("utf-8")
    except (TypeError, ValueError) as exc:
        raise ValueError("caller-load values must be finite JSON values") from exc


def _sha(data: bytes) -> str:
    return "sha256:" + hashlib.sha256(data).hexdigest()


def _integer(value: Any, name: str, low: int, high: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not low <= value <= high:
        raise ValueError(f"{name} must be an integer in [{low}, {high}]")
    return value


def _number(value: Any, name: str, low: float, high: float) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{name} must be numeric")
    result = float(value)
    if not math.isfinite(result) or not low <= result <= high:
        raise ValueError(f"{name} must be in [{low}, {high}]")
    return result


def _identifier(value: Any, name: str, maximum: int = 128) -> str:
    if (
        not isinstance(value, str) or not value or len(value) > maximum
        or any(character not in "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789._-" for character in value)
        or not value[0].isalnum()
    ):
        raise ValueError(f"{name} must be a safe identifier of at most {maximum} characters")
    return value


def _read_regular(path: Path, maximum: int = _MAX_FILE_BYTES) -> bytes:
    """Bounded, race-resistant read of a regular non-symlink file."""

    raw = os.fspath(path)
    before = os.lstat(raw)
    if stat.S_ISLNK(before.st_mode) or not stat.S_ISREG(before.st_mode):
        raise ValueError(f"{raw!r} must be a regular non-symlink file")
    if before.st_size > maximum:
        raise ValueError(f"{raw!r} exceeds the {maximum}-byte read limit")
    flags = os.O_RDONLY | getattr(os, "O_BINARY", 0)
    flags |= getattr(os, "O_NONBLOCK", 0) | getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(raw, flags)
    try:
        opened = os.fstat(descriptor)
        if (
            not stat.S_ISREG(opened.st_mode)
            or (opened.st_dev, opened.st_ino) != (before.st_dev, before.st_ino)
        ):
            raise ValueError(f"{raw!r} changed while it was opened")
        if opened.st_size > maximum:
            raise ValueError(f"{raw!r} exceeds the {maximum}-byte read limit")
        chunks: List[bytes] = []
        remaining = maximum + 1
        while remaining:
            chunk = os.read(descriptor, min(1024 * 1024, remaining))
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
        value = b"".join(chunks)
        if len(value) > maximum:
            raise ValueError(f"{raw!r} exceeds the {maximum}-byte read limit")
        return value
    finally:
        os.close(descriptor)


def _exclusive_write(path: Path, data: bytes) -> None:
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
    root = Path(os.path.abspath(output_dir))
    if os.path.lexists(root):
        info = os.lstat(root)
        if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode):
            raise ValueError(
                "caller-load output directory must be a non-symlink directory"
            )
    else:
        root.mkdir(parents=True, mode=0o700)
    if any(root.iterdir()):
        raise ValueError("caller-load output directory must be empty")
    return root


def _load_json(path: Path, maximum: int = _MAX_JSON_BYTES) -> Any:
    return json.loads(
        _read_regular(path, maximum).decode("utf-8"),
        parse_constant=lambda value: (_ for _ in ()).throw(ValueError(value)),
    )


def _safe_relative(value: Any) -> bool:
    return (
        isinstance(value, str) and bool(value) and not value.startswith("/")
        and "\\" not in value
        and all(part not in {"", ".", ".."} for part in value.split("/"))
    )


def normalize_remote_endpoint(value: Any) -> str:
    """Normalize one credential-free remote WebSocket sidecar endpoint."""

    if not isinstance(value, str) or not value or len(value) > 4096:
        raise ValueError("remote endpoint must be a bounded non-empty wss:// URL")
    try:
        parsed = urllib.parse.urlsplit(value)
        port = parsed.port
    except ValueError as exc:
        raise ValueError("remote endpoint is invalid") from exc
    if (
        parsed.scheme.lower() != "wss"
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
    ):
        raise ValueError(
            "remote endpoint must be credential-free wss:// with no query or fragment"
        )
    path = parsed.path or "/"
    if any(ord(character) < 0x21 or ord(character) > 0x7E for character in path):
        raise ValueError("remote endpoint path must be printable ASCII")
    try:
        host = parsed.hostname.encode("idna").decode("ascii").lower()
    except UnicodeError as exc:
        raise ValueError("remote endpoint hostname is invalid") from exc
    if port is not None and not 1 <= port <= 65_535:
        raise ValueError("remote endpoint port is invalid")
    display = f"[{host}]" if ":" in host else host
    authority = display if port in {None, 443} else f"{display}:{port}"
    return f"wss://{authority}{path}"


def _percentile(values: Sequence[float], percentile: float) -> Optional[float]:
    if not values:
        return None
    ordered = sorted(float(value) for value in values)
    rank = max(0, min(len(ordered) - 1, int(math.ceil(percentile * len(ordered))) - 1))
    return round(ordered[rank], 6)


def validate_plan(value: Any) -> Dict[str, Any]:
    """Validate and normalize a credential-free caller workload plan."""

    if not isinstance(value, dict):
        raise ValueError("caller-load plan must be a mapping")
    allowed = {"schema", "id", "caller_plan", "stages", "safety", "slos", "metadata"}
    unknown = sorted(set(value) - allowed)
    if unknown:
        raise ValueError("caller-load plan has unknown fields: " + ", ".join(unknown))
    if value.get("schema") != PLAN_SCHEMA:
        raise ValueError(f"caller-load plan schema must be {PLAN_SCHEMA!r}")
    plan_id = _identifier(value.get("id"), "id")
    raw_caller_plan = value.get("caller_plan")
    if not isinstance(raw_caller_plan, dict):
        raise ValueError("caller_plan must be a mapping")
    normalized_caller = caller.validate_plan(raw_caller_plan)
    if normalized_caller["mode"] == "frozen_replay":
        # Frozen package paths are machine-local input authority and cannot be
        # made portable inside an independently verifiable workload contract.
        raise ValueError("caller-load does not accept frozen_replay caller plans")
    if "caller_load" in normalized_caller.get("metadata", {}):
        raise ValueError("caller_plan.metadata.caller_load is reserved")

    metadata = value.get("metadata", {})
    if not isinstance(metadata, dict):
        raise ValueError("metadata must be a mapping")
    metadata = json.loads(_canonical(metadata))

    raw_stages = value.get("stages")
    if not isinstance(raw_stages, list) or not 1 <= len(raw_stages) <= _MAX_STAGES:
        raise ValueError(f"stages must contain 1..{_MAX_STAGES} entries")
    stages: List[Dict[str, Any]] = []
    names = set()
    total = 0
    peak = 0
    for index, raw in enumerate(raw_stages):
        if not isinstance(raw, dict):
            raise ValueError(f"stages[{index}] must be a mapping")
        stage_id = _identifier(raw.get("id"), f"stages[{index}].id", 100)
        if stage_id in names:
            raise ValueError("stage ids must be unique")
        names.add(stage_id)
        model = raw.get("model")
        if model == "closed":
            if set(raw) != {"id", "model", "concurrency", "calls"}:
                raise ValueError(f"closed stages[{index}] requires id, model, concurrency, and calls")
            concurrency = _integer(raw["concurrency"], f"stages[{index}].concurrency", 1, _MAX_CONCURRENCY)
            calls = _integer(raw["calls"], f"stages[{index}].calls", 1, _MAX_CALLS)
            row = {
                "id": stage_id, "model": "closed", "concurrency": concurrency,
                "calls": calls,
            }
            peak = max(peak, concurrency)
        elif model == "open":
            expected = {"id", "model", "arrival_rate_per_second", "duration_seconds", "max_in_flight"}
            if set(raw) not in {frozenset(expected), frozenset(expected | {"calls"})}:
                raise ValueError(
                    f"open stages[{index}] requires id, model, arrival_rate_per_second, "
                    "duration_seconds, and max_in_flight"
                )
            rate = _number(raw["arrival_rate_per_second"], f"stages[{index}].arrival_rate_per_second", 0.001, 100_000)
            duration = _number(raw["duration_seconds"], f"stages[{index}].duration_seconds", 0.001, 604_800)
            max_in_flight = _integer(raw["max_in_flight"], f"stages[{index}].max_in_flight", 1, _MAX_CONCURRENCY)
            calls = int(math.ceil(rate * duration - 1e-12))
            calls = max(1, calls)
            if "calls" in raw and raw["calls"] != calls:
                raise ValueError(f"stages[{index}].calls does not match the open-arrival schedule")
            row = {
                "id": stage_id, "model": "open",
                "arrival_rate_per_second": rate, "duration_seconds": duration,
                "max_in_flight": max_in_flight, "calls": calls,
            }
            peak = max(peak, max_in_flight)
        else:
            raise ValueError(f"stages[{index}].model must be 'closed' or 'open'")
        total += calls
        if total > _MAX_CALLS:
            raise ValueError(f"caller-load plan exceeds {_MAX_CALLS} scheduled calls")
        stages.append(row)

    raw_safety = value.get("safety", {})
    allowed_safety = {
        "max_calls", "max_concurrency", "max_call_duration_ms", "max_start_delay_ms",
        "max_cost_per_call_microusd", "max_cost_microusd", "stop_file",
        "remote_execution",
    }
    if not isinstance(raw_safety, dict) or set(raw_safety) - allowed_safety:
        raise ValueError("safety contains unknown fields")
    max_calls = _integer(raw_safety.get("max_calls", total), "safety.max_calls", 1, _MAX_CALLS)
    max_concurrency = _integer(raw_safety.get("max_concurrency", peak), "safety.max_concurrency", 1, _MAX_CONCURRENCY)
    max_duration = _integer(
        raw_safety.get("max_call_duration_ms", normalized_caller["limits"]["max_duration_ms"]),
        "safety.max_call_duration_ms", 1, 86_400_000,
    )
    max_start_delay = _integer(raw_safety.get("max_start_delay_ms", 1_000), "safety.max_start_delay_ms", 0, 3_600_000)
    per_call_cost = _integer(
        raw_safety.get("max_cost_per_call_microusd", normalized_caller["limits"]["max_cost_microusd"]),
        "safety.max_cost_per_call_microusd", 0, 10**12,
    )
    total_cost = _integer(raw_safety.get("max_cost_microusd", total * per_call_cost), "safety.max_cost_microusd", 0, 10**15)
    stop_file = raw_safety.get("stop_file")
    if stop_file is not None and (not isinstance(stop_file, str) or not stop_file or len(stop_file) > 4096):
        raise ValueError("safety.stop_file must be a non-empty path of at most 4096 characters")
    if total > max_calls:
        raise ValueError("scheduled calls exceed safety.max_calls")
    if peak > max_concurrency:
        raise ValueError("stage concurrency exceeds safety.max_concurrency")
    if max_duration > normalized_caller["limits"]["max_duration_ms"]:
        raise ValueError("safety.max_call_duration_ms cannot exceed caller_plan.limits.max_duration_ms")
    if per_call_cost > normalized_caller["limits"]["max_cost_microusd"]:
        raise ValueError("safety.max_cost_per_call_microusd cannot exceed caller plan cost ceiling")
    if total * per_call_cost > total_cost:
        raise ValueError("worst-case caller cost exceeds safety.max_cost_microusd")

    remote_value = raw_safety.get("remote_execution")
    remote_execution: Optional[Dict[str, Any]] = None
    if remote_value is not None:
        if not isinstance(remote_value, dict) or set(remote_value) != {
            "endpoint", "max_calls", "external_cost_state", "acknowledgement",
        }:
            raise ValueError(
                "safety.remote_execution requires endpoint, max_calls, "
                "external_cost_state, and acknowledgement"
            )
        remote_calls = _integer(
            remote_value.get("max_calls"),
            "safety.remote_execution.max_calls",
            1,
            _MAX_CALLS,
        )
        if total > remote_calls:
            raise ValueError(
                "scheduled calls exceed safety.remote_execution.max_calls"
            )
        if remote_value.get("external_cost_state") != "UNOBSERVABLE":
            raise ValueError(
                "safety.remote_execution.external_cost_state must be UNOBSERVABLE"
            )
        if remote_value.get("acknowledgement") != REMOTE_ACKNOWLEDGEMENT:
            raise ValueError(
                "safety.remote_execution acknowledgement does not match the fixed phrase"
            )
        remote_execution = {
            "endpoint": normalize_remote_endpoint(remote_value.get("endpoint")),
            "max_calls": remote_calls,
            "external_cost_state": "UNOBSERVABLE",
            "acknowledgement": REMOTE_ACKNOWLEDGEMENT,
        }

    raw_slos = value.get("slos", {})
    allowed_slos = {
        "max_dropped_start_rate", "min_completion_rate", "max_blocked_error_rate",
        "min_child_verification_rate", "min_evidence_complete_rate",
        "max_p95_scheduling_delay_ms", "max_p95_duration_ms",
    }
    if not isinstance(raw_slos, dict) or set(raw_slos) - allowed_slos:
        raise ValueError("slos contains unknown fields")
    slos: Dict[str, float] = {}
    for name, raw in raw_slos.items():
        high = 1.0 if name.endswith("_rate") else 86_400_000.0
        slos[name] = _number(raw, "slos." + name, 0.0, high)

    return {
        "schema": PLAN_SCHEMA, "id": plan_id, "caller_plan": normalized_caller,
        "stages": stages,
        "safety": {
            "max_calls": max_calls, "max_concurrency": max_concurrency,
            "max_call_duration_ms": max_duration, "max_start_delay_ms": max_start_delay,
            "max_cost_per_call_microusd": per_call_cost,
            "max_cost_microusd": total_cost, "stop_file": stop_file,
            "remote_execution": remote_execution,
        },
        "slos": slos, "metadata": metadata,
    }


def load_plan(path: str) -> Dict[str, Any]:
    return validate_plan(_load_json(Path(path)))


def _child_context(plan: Mapping[str, Any], plan_sha: str, stage: Mapping[str, Any], index: int) -> Dict[str, Any]:
    identity = {
        "workload_plan_id": plan["id"], "workload_plan_sha256": plan_sha,
        "stage_id": stage["id"], "stage_model": stage["model"],
        "invocation_index": index,
    }
    child_id = hashlib.sha256(_canonical(identity)).hexdigest()
    remote = plan["safety"]["remote_execution"]
    expected_boundary = {
        "transport": "websocket_sidecar" if remote is not None else "operator_supplied_session_factory",
        "endpoint_sha256": (
            _sha(remote["endpoint"].encode("utf-8")) if remote is not None else None
        ),
    }
    return {
        "schema": CONTEXT_SCHEMA, "child_id": child_id, **identity,
        "expected_session_boundary": expected_boundary,
    }


def _caller_plan_for_child(plan: Mapping[str, Any], context: Mapping[str, Any]) -> Dict[str, Any]:
    result = json.loads(_canonical(plan["caller_plan"]))
    base_id = result["id"][:48]
    result["id"] = f"{base_id}--{context['child_id'][:16]}"
    metadata = dict(result.get("metadata", {}))
    if "caller_load" in metadata:
        raise ValueError("caller_plan.metadata.caller_load is reserved")
    metadata["caller_load"] = dict(context)
    result["metadata"] = metadata
    result["limits"]["max_duration_ms"] = min(
        result["limits"]["max_duration_ms"], plan["safety"]["max_call_duration_ms"]
    )
    result["limits"]["max_cost_microusd"] = min(
        result["limits"]["max_cost_microusd"], plan["safety"]["max_cost_per_call_microusd"]
    )
    return caller.validate_plan(result)


class _FailureSession:
    def __init__(self, code: str):
        self.code = code

    def capabilities(self) -> Mapping[str, str]:
        raise RuntimeError(self.code)

    def evidence(self) -> Dict[str, Any]:
        return {"availability": caller.UNOBSERVABLE, "reason": self.code}


class _FailureModel:
    def propose(self, request: Mapping[str, Any]) -> Mapping[str, Any]:
        raise RuntimeError("CALLER_LOAD_FACTORY_ERROR")


def _write_failure_package(child_plan: Mapping[str, Any], output_dir: Path, code: str) -> None:
    model = _FailureModel() if child_plan["mode"] == "generative" else None
    caller.run_caller(child_plan, _FailureSession(code), str(output_dir), model=model)


def _worker(
    child_plan: Mapping[str, Any], context: Mapping[str, Any], working_dir: str,
    session_factory: Callable[[Mapping[str, Any]], caller.CallerSession],
    model_factory: Optional[Callable[[Mapping[str, Any]], caller.CallerModel]],
    tts_factory: Optional[Callable[[Mapping[str, Any]], caller.CallerTTS]],
) -> None:
    output = Path(working_dir)
    session: Optional[caller.CallerSession] = None
    model: Optional[caller.CallerModel] = None
    tts: Optional[caller.CallerTTS] = None

    def terminate_worker(signum: int, frame: Any) -> None:
        # Best-effort remote teardown precedes the supervisor's later hard
        # kill.  Stop local subprocess adapters first so a timed-out child
        # cannot orphan Piper after this process exits.
        if tts is not None:
            abort = getattr(tts, "abort", None)
            if callable(abort):
                try:
                    abort()
                except BaseException:
                    pass
            close_tts = getattr(tts, "close", None)
            if callable(close_tts):
                try:
                    close_tts()
                except BaseException:
                    pass
        if session is not None:
            try:
                session.hangup("caller_load_supervisor_timeout")
            except BaseException:
                pass
            close = getattr(session, "close", None)
            if callable(close):
                try:
                    close()
                except BaseException:
                    pass
        os._exit(124)

    if hasattr(signal, "SIGTERM"):
        signal.signal(signal.SIGTERM, terminate_worker)
    try:
        session = session_factory(dict(context))
        model = model_factory(dict(context)) if model_factory is not None else None
        tts = tts_factory(dict(context)) if tts_factory is not None else None
        caller.run_caller(child_plan, session, str(output), model=model, tts=tts)
    except BaseException:  # process boundary: emit a verified, secret-free failure package
        if output.exists():
            shutil.rmtree(str(output), ignore_errors=True)
        output.mkdir(parents=True, exist_ok=True)
        _write_failure_package(child_plan, output, "CALLER_LOAD_FACTORY_OR_WORKER_ERROR")
    finally:
        for resource in (tts, model, session):
            if resource is None:
                continue
            close = getattr(resource, "close", None)
            if callable(close):
                try:
                    close()
                except BaseException:
                    pass


def _valid_digest(value: Any) -> bool:
    return (
        isinstance(value, str)
        and len(value) == 71
        and value.startswith("sha256:")
        and all(character in "0123456789abcdef" for character in value[7:])
    )


def _valid_delivery_claim(
    claim: Mapping[str, Any], *, context: Mapping[str, Any],
    submitted: set, from_event: bool,
) -> bool:
    required = {
        "authority", "submitted_sha256", "delivered_sha256",
        "workload_child_id", "workload_plan_sha256",
    }
    if from_event:
        required |= {"kind", "custom_type", "sequence", "event_sha256"}
        allowed = required | {"source_sequence"}
        if set(claim) not in {frozenset(required), frozenset(allowed)} or claim.get("kind") != "custom":
            return False
        if claim.get("custom_type") != DELIVERED_AUDIO_EVENT:
            return False
        unsigned = dict(claim)
        claimed_event = unsigned.pop("event_sha256", None)
        if claimed_event != _sha(_canonical(unsigned)):
            return False
        sequence = claim.get("sequence")
        if isinstance(sequence, bool) or not isinstance(sequence, int) or sequence < 1:
            return False
    else:
        required |= {"schema", "state"}
        if set(claim) != required:
            return False
        if claim.get("schema") != DELIVERED_AUDIO_EVENT or claim.get("state") != "PRESENT":
            return False
    return (
        claim.get("authority") in _DELIVERY_AUTHORITIES
        and _valid_digest(claim.get("submitted_sha256"))
        and _valid_digest(claim.get("delivered_sha256"))
        and claim.get("submitted_sha256") in submitted
        and claim.get("workload_child_id") == context.get("child_id")
        and claim.get("workload_plan_sha256") == context.get("workload_plan_sha256")
    )


def _evidence_state(result: Mapping[str, Any], context: Mapping[str, Any]) -> str:
    """Recompute complete outgoing-audio delivery from child-owned evidence."""

    submitted = {
        row.get("pcm_sha256")
        for row in result.get("actions", [])
        if isinstance(row, dict) and _valid_digest(row.get("pcm_sha256"))
    }
    boundary = result.get("session_boundary")
    boundary = boundary if isinstance(boundary, dict) else {}
    candidates: List[Tuple[Mapping[str, Any], bool]] = []
    malformed_delivery_claim = False
    boundary_claim = boundary.get("delivery_evidence")
    if isinstance(boundary_claim, dict):
        if boundary_claim.get("state") == "PRESENT":
            candidates.append((boundary_claim, False))
    elif boundary_claim is not None:
        malformed_delivery_claim = True
    for event in result.get("events", []):
        if not isinstance(event, dict):
            continue
        if event.get("custom_type") == DELIVERED_AUDIO_EVENT:
            candidates.append((event, True))

    covered = set()
    for claim, from_event in candidates:
        if not _valid_delivery_claim(
            claim, context=context, submitted=submitted, from_event=from_event
        ):
            malformed_delivery_claim = True
            continue
        covered.add(claim["submitted_sha256"])
    if submitted and not malformed_delivery_claim and covered == submitted:
        return "PRESENT"
    if candidates or malformed_delivery_claim:
        # A malformed/partial PRESENT claim never upgrades its state.
        return "MISSING"
    explicit = boundary.get("delivery_evidence_state")
    if explicit in _EVIDENCE_STATES:
        return "MISSING" if explicit == "PRESENT" else str(explicit)
    if isinstance(boundary_claim, dict):
        state = boundary_claim.get("state")
        if state in _EVIDENCE_STATES:
            return "MISSING" if state == "PRESENT" else str(state)
    availability = boundary.get("availability")
    if availability in {caller.UNSUPPORTED, caller.UNOBSERVABLE}:
        return str(availability)
    if submitted:
        # Audio was emitted but the session made no explicit capability-state
        # declaration and supplied no target/carrier receipt.
        return "MISSING"
    return "UNOBSERVABLE"


def _session_endpoint_state(
    result: Mapping[str, Any], context: Mapping[str, Any]
) -> str:
    expected_boundary = context.get("expected_session_boundary")
    expected = (
        expected_boundary.get("endpoint_sha256")
        if isinstance(expected_boundary, dict) else None
    )
    if expected is None:
        return "NOT_REQUIRED"
    boundary = result.get("session_boundary")
    observed = (
        boundary.get("connected_endpoint_sha256")
        if isinstance(boundary, dict) else None
    )
    if not _valid_digest(observed):
        return "MISSING"
    return "MATCHED" if observed == expected else "MISMATCH"


def _actual_cost(result: Mapping[str, Any]) -> int:
    counters = result.get("counters")
    value = counters.get("cost_microusd", 0) if isinstance(counters, dict) else 0
    return value if isinstance(value, int) and not isinstance(value, bool) and value >= 0 else 0


def _parse_child(path: Path) -> Tuple[Dict[str, Any], Dict[str, Any]]:
    verification = caller.verify_package(str(path))
    result: Dict[str, Any] = {}
    if verification.get("ok"):
        loaded = _load_json(path / "caller-result.json")
        if isinstance(loaded, dict):
            result = loaded
    return result, verification


def _observation_from_child(active: _Active, *, timeout: bool, worker_exit_code: Optional[int]) -> Dict[str, Any]:
    result, verification = _parse_child(active.final_dir)
    status = result.get("status") if result.get("status") in _TERMINAL_STATUSES else "ERROR"
    elapsed = max(0.0, (time.monotonic() - active.start_monotonic) * 1000.0)
    return {
        "stage_id": active.context["stage_id"],
        "stage_model": active.context["stage_model"],
        "invocation_index": active.context["invocation_index"],
        "child_id": active.context["child_id"],
        "disposition": "STARTED",
        "scheduled_offset_ms": round(active.scheduled_offset_ms, 6),
        "scheduling_delay_ms": round(active.scheduling_delay_ms, 6),
        "duration_ms": round(elapsed, 6),
        "supervisor_timeout": bool(timeout),
        "worker_exit_code": worker_exit_code,
        "package_path": f"children/{active.context['child_id']}",
        "child_package_id": verification.get("package_id"),
        "child_package_verified": bool(verification.get("ok")),
        "caller_status": status,
        "caller_exit_code": result.get("exit_code") if isinstance(result.get("exit_code"), int) else 1,
        "delivery_evidence_state": _evidence_state(result, active.context),
        "session_endpoint_state": _session_endpoint_state(result, active.context),
        "actual_cost_microusd": _actual_cost(result),
    }


def _drop_observation(
    context: Mapping[str, Any], scheduled_offset_ms: float, delay_ms: float, reason: str,
) -> Dict[str, Any]:
    return {
        "stage_id": context["stage_id"], "stage_model": context["stage_model"],
        "invocation_index": context["invocation_index"], "child_id": context["child_id"],
        "disposition": "DROPPED", "drop_reason": reason,
        "scheduled_offset_ms": round(scheduled_offset_ms, 6),
        "scheduling_delay_ms": round(max(0.0, delay_ms), 6),
    }


def _stopped_observation(context: Mapping[str, Any], scheduled_offset_ms: float) -> Dict[str, Any]:
    return {
        "stage_id": context["stage_id"], "stage_model": context["stage_model"],
        "invocation_index": context["invocation_index"], "child_id": context["child_id"],
        "disposition": "STOPPED", "scheduled_offset_ms": round(scheduled_offset_ms, 6),
        "scheduling_delay_ms": 0.0,
    }


def _process_context() -> multiprocessing.context.BaseContext:
    methods = multiprocessing.get_all_start_methods()
    # Fork supports closures commonly used by local test harnesses.  Spawn is
    # the portable fallback and requires factories to be pickleable.
    return multiprocessing.get_context("fork" if "fork" in methods else "spawn")


def _start_child(
    mp: multiprocessing.context.BaseContext,
    plan: Mapping[str, Any], plan_sha: str, stage: Mapping[str, Any], index: int,
    output: Path, work_root: Path, stage_started: float, scheduled_offset_ms: float,
    session_factory: Callable[[Mapping[str, Any]], caller.CallerSession],
    model_factory: Optional[Callable[[Mapping[str, Any]], caller.CallerModel]],
    tts_factory: Optional[Callable[[Mapping[str, Any]], caller.CallerTTS]],
) -> _Active:
    context = _child_context(plan, plan_sha, stage, index)
    child_plan = _caller_plan_for_child(plan, context)
    final = output / "children" / context["child_id"]
    working = work_root / context["child_id"]
    now = time.monotonic()
    delay = max(0.0, (now - stage_started) * 1000.0 - scheduled_offset_ms)
    process = mp.Process(
        target=_worker,
        args=(child_plan, context, str(working), session_factory, model_factory, tts_factory),
        name="hotato-caller-" + context["child_id"][:12],
    )
    process.daemon = False
    process.start()
    return _Active(
        process=process, context=context, child_plan=child_plan,
        working_dir=working, final_dir=final, stage_started=stage_started,
        scheduled_offset_ms=scheduled_offset_ms, scheduling_delay_ms=delay,
        start_monotonic=now,
    )


def _finish_child(active: _Active, maximum_ms: int) -> Optional[Dict[str, Any]]:
    elapsed_ms = (time.monotonic() - active.start_monotonic) * 1000.0
    if active.process.is_alive() and elapsed_ms <= maximum_ms:
        return None
    timeout = active.process.is_alive()
    if timeout:
        active.process.terminate()
        active.process.join(timeout=2.0)
        if active.process.is_alive():
            kill = getattr(active.process, "kill", None)
            if callable(kill):
                kill()
            active.process.join(timeout=2.0)
    else:
        active.process.join(timeout=0.1)
    worker_exit = active.process.exitcode
    if not timeout and active.working_dir.is_dir() and caller.verify_package(str(active.working_dir)).get("ok"):
        os.replace(str(active.working_dir), str(active.final_dir))
    else:
        if active.working_dir.exists():
            shutil.rmtree(str(active.working_dir), ignore_errors=True)
        active.final_dir.mkdir(parents=True, exist_ok=False)
        _write_failure_package(
            active.child_plan, active.final_dir,
            "CALLER_LOAD_TIMEOUT" if timeout else "CALLER_LOAD_WORKER_FAILED",
        )
    return _observation_from_child(active, timeout=timeout, worker_exit_code=worker_exit)


def _reap(active: List[_Active], observations: List[Dict[str, Any]], maximum_ms: int) -> None:
    remaining: List[_Active] = []
    for item in active:
        observation = _finish_child(item, maximum_ms)
        if observation is None:
            remaining.append(item)
        else:
            observations.append(observation)
    active[:] = remaining


def _stop_requested(plan: Mapping[str, Any], base_dir: Path) -> bool:
    value = plan["safety"].get("stop_file")
    if value is None:
        return False
    path = Path(value)
    if not path.is_absolute():
        path = base_dir / path
    return path.exists()


def _execute_stage(
    mp: multiprocessing.context.BaseContext,
    plan: Mapping[str, Any], plan_sha: str, stage: Mapping[str, Any],
    output: Path, work_root: Path, base_dir: Path,
    session_factory: Callable[[Mapping[str, Any]], caller.CallerSession],
    model_factory: Optional[Callable[[Mapping[str, Any]], caller.CallerModel]],
    tts_factory: Optional[Callable[[Mapping[str, Any]], caller.CallerTTS]],
) -> List[Dict[str, Any]]:
    observations: List[Dict[str, Any]] = []
    active: List[_Active] = []
    started = time.monotonic()
    maximum_ms = int(plan["safety"]["max_call_duration_ms"])
    max_delay = float(plan["safety"]["max_start_delay_ms"])
    stopped = False

    for index in range(int(stage["calls"])):
        if stage["model"] == "open":
            offset_ms = index * 1000.0 / float(stage["arrival_rate_per_second"])
            while True:
                _reap(active, observations, maximum_ms)
                remaining = offset_ms / 1000.0 - (time.monotonic() - started)
                if remaining <= 0:
                    break
                time.sleep(min(0.01, remaining))
            capacity = int(stage["max_in_flight"])
        else:
            offset_ms = max(0.0, (time.monotonic() - started) * 1000.0)
            capacity = int(stage["concurrency"])
            while len(active) >= capacity:
                _reap(active, observations, maximum_ms)
                if len(active) >= capacity:
                    time.sleep(0.002)

        context = _child_context(plan, plan_sha, stage, index)
        if stopped or _stop_requested(plan, base_dir):
            stopped = True
            observations.append(_stopped_observation(context, offset_ms))
            continue

        _reap(active, observations, maximum_ms)
        delay_ms = max(0.0, (time.monotonic() - started) * 1000.0 - offset_ms)
        if stage["model"] == "open" and len(active) >= capacity:
            observations.append(_drop_observation(context, offset_ms, delay_ms, "MAX_IN_FLIGHT"))
            continue
        if stage["model"] == "open" and delay_ms > max_delay:
            observations.append(_drop_observation(context, offset_ms, delay_ms, "START_DELAY"))
            continue
        try:
            active.append(_start_child(
                mp, plan, plan_sha, stage, index, output, work_root, started,
                offset_ms, session_factory, model_factory, tts_factory,
            ))
        except Exception:
            # A failed process start is still a started invocation contract: a
            # verified child package records the supervisor boundary failure.
            child_plan = _caller_plan_for_child(plan, context)
            final = output / "children" / context["child_id"]
            final.mkdir(parents=True, exist_ok=False)
            _write_failure_package(child_plan, final, "CALLER_LOAD_PROCESS_START_FAILED")
            synthetic = _Active(
                process=None,  # type: ignore[arg-type]
                context=context, child_plan=child_plan, working_dir=work_root,
                final_dir=final, stage_started=started,
                scheduled_offset_ms=offset_ms, scheduling_delay_ms=delay_ms,
                start_monotonic=time.monotonic(),
            )
            observations.append(_observation_from_child(
                synthetic, timeout=False, worker_exit_code=None
            ))

    while active:
        _reap(active, observations, maximum_ms)
        if active:
            time.sleep(0.002)
    observations.sort(key=lambda row: int(row["invocation_index"]))
    return observations


def _metric_block(rows: Sequence[Mapping[str, Any]]) -> Dict[str, Any]:
    scheduled = len(rows)
    started = [row for row in rows if row.get("disposition") == "STARTED"]
    dropped = [row for row in rows if row.get("disposition") == "DROPPED"]
    stopped = [row for row in rows if row.get("disposition") == "STOPPED"]
    statuses = {status: sum(row.get("caller_status") == status for row in started) for status in _TERMINAL_STATUSES}
    evidence = {state: sum(row.get("delivery_evidence_state") == state for row in started) for state in _EVIDENCE_STATES}
    endpoints = {
        state: sum(row.get("session_endpoint_state") == state for row in started)
        for state in _ENDPOINT_STATES
    }
    delays = [float(row["scheduling_delay_ms"]) for row in rows]
    durations = [float(row["duration_ms"]) for row in started]
    verified = sum(row.get("child_package_verified") is True for row in started)
    costs = sum(int(row.get("actual_cost_microusd", 0)) for row in started)
    denominator = len(started)
    return {
        "scheduled": scheduled, "started": denominator, "dropped_starts": len(dropped),
        "stopped_before_start": len(stopped),
        "caller_status": {
            "completed": statuses["COMPLETED"], "hung_up": statuses["HUNG_UP"],
            "blocked": statuses["BLOCKED"], "error": statuses["ERROR"],
        },
        "child_verification": {"verified": verified, "unverified": denominator - verified},
        "delivery_evidence": {state.lower(): evidence[state] for state in _EVIDENCE_STATES},
        "session_endpoint_binding": {
            "matched": endpoints["MATCHED"], "missing": endpoints["MISSING"],
            "mismatch": endpoints["MISMATCH"],
            "not_required": endpoints["NOT_REQUIRED"],
            "required": endpoints["MATCHED"] + endpoints["MISSING"] + endpoints["MISMATCH"],
        },
        "rates": {
            "dropped_start_rate": round(len(dropped) / scheduled, 9) if scheduled else 0.0,
            "completion_rate": round((statuses["COMPLETED"] + statuses["HUNG_UP"]) / denominator, 9) if denominator else 0.0,
            "blocked_error_rate": round((statuses["BLOCKED"] + statuses["ERROR"]) / denominator, 9) if denominator else 0.0,
            "child_verification_rate": round(verified / denominator, 9) if denominator else 0.0,
            "evidence_complete_rate": round(evidence["PRESENT"] / denominator, 9) if denominator else 0.0,
            "session_endpoint_match_rate": (
                round(
                    endpoints["MATCHED"] /
                    (endpoints["MATCHED"] + endpoints["MISSING"] + endpoints["MISMATCH"]),
                    9,
                )
                if endpoints["MATCHED"] + endpoints["MISSING"] + endpoints["MISMATCH"]
                else None
            ),
        },
        "scheduling_delay_ms": {
            "p50": _percentile(delays, 0.50), "p95": _percentile(delays, 0.95),
            "max": round(max(delays), 6) if delays else None,
        },
        "duration_ms": {
            "p50": _percentile(durations, 0.50), "p95": _percentile(durations, 0.95),
            "max": round(max(durations), 6) if durations else None,
        },
        "actual_cost_microusd": costs,
    }


def _metrics(plan: Mapping[str, Any], observations: Sequence[Mapping[str, Any]]) -> Tuple[Dict[str, Any], List[Dict[str, Any]]]:
    aggregate = _metric_block(observations)
    stages = []
    for stage in plan["stages"]:
        rows = [row for row in observations if row.get("stage_id") == stage["id"]]
        stages.append({"id": stage["id"], "model": stage["model"], "metrics": _metric_block(rows)})
    return aggregate, stages


def _evaluate_slos(plan: Mapping[str, Any], metrics: Mapping[str, Any]) -> List[Dict[str, Any]]:
    rates = metrics["rates"]
    lookup = {
        "max_dropped_start_rate": rates["dropped_start_rate"],
        "min_completion_rate": rates["completion_rate"],
        "max_blocked_error_rate": rates["blocked_error_rate"],
        "min_child_verification_rate": rates["child_verification_rate"],
        "min_evidence_complete_rate": rates["evidence_complete_rate"],
        "max_p95_scheduling_delay_ms": metrics["scheduling_delay_ms"]["p95"],
        "max_p95_duration_ms": metrics["duration_ms"]["p95"],
    }
    rows = []
    for name in sorted(plan["slos"]):
        threshold = plan["slos"][name]
        observed = lookup[name]
        if observed is None:
            passed = False
            reason = "NO_OBSERVATIONS"
        elif name.startswith("max_"):
            passed = observed <= threshold
            reason = None
        else:
            passed = observed >= threshold
            reason = None
        row = {"name": name, "threshold": threshold, "observed": observed, "passed": passed}
        if reason is not None:
            row["reason"] = reason
        rows.append(row)
    return rows


def _status(
    plan: Mapping[str, Any], metrics: Mapping[str, Any], slos: Sequence[Mapping[str, Any]],
) -> Tuple[str, int]:
    if metrics["stopped_before_start"]:
        return "INCONCLUSIVE", 2
    if metrics["actual_cost_microusd"] > plan["safety"]["max_cost_microusd"]:
        return "FAIL", 1
    endpoint = metrics["session_endpoint_binding"]
    if plan["safety"]["remote_execution"] is not None and (
        endpoint["required"] != metrics["started"]
        or endpoint["matched"] != metrics["started"]
    ):
        # An unknown/mismatched remote endpoint is an execution-boundary gap,
        # not evidence of agent quality failure.
        return "INCONCLUSIVE", 2
    if not plan["slos"]:
        return "INCONCLUSIVE", 2
    if any(row.get("passed") is not True for row in slos):
        return "FAIL", 1
    return "PASS", 0


def _inventory(root: Path) -> Dict[str, Dict[str, Any]]:
    files: Dict[str, Dict[str, Any]] = {}
    for path in sorted(root.rglob("*")):
        if path.is_symlink():
            raise CallerLoadError(f"refusing symbolic link in output: {path}")
        if not path.is_file() or path == root / "package-manifest.json":
            continue
        if len(files) >= _MAX_PACKAGE_FILES:
            raise CallerLoadError("caller-load output exceeds file count limit")
        data = _read_regular(path)
        rel = path.relative_to(root).as_posix()
        files[rel] = {"bytes": len(data), "sha256": _sha(data)}
    return files


def _write_manifest(root: Path, result_id: str) -> Dict[str, Any]:
    manifest: Dict[str, Any] = {
        "schema": PACKAGE_SCHEMA, "result_id": result_id, "files": _inventory(root),
    }
    manifest["package_id"] = _sha(_canonical(manifest))
    _exclusive_write(root / "package-manifest.json", _canonical(manifest) + b"\n")
    return manifest


def _execution_boundary(plan: Mapping[str, Any]) -> Dict[str, Any]:
    remote = plan["safety"]["remote_execution"]
    if remote is not None:
        return {
            "transport": "websocket_sidecar",
            "network": "remote",
            "plan_declared_endpoint_sha256": _sha(remote["endpoint"].encode("utf-8")),
            "runtime_configured_endpoint_sha256": _sha(remote["endpoint"].encode("utf-8")),
            "external_provider_cost_state": "UNOBSERVABLE",
            "external_provider_cost_microusd": None,
        }
    return {
        "transport": "operator_supplied_session_factory",
        "network": "local_declared",
        "plan_declared_endpoint_sha256": None,
        "runtime_configured_endpoint_sha256": None,
        "external_provider_cost_state": "UNOBSERVABLE",
        "external_provider_cost_microusd": None,
    }


def run_caller_load(
    plan_value: Mapping[str, Any], output_dir: str,
    session_factory: Callable[[Mapping[str, Any]], caller.CallerSession],
    *, model_factory: Optional[Callable[[Mapping[str, Any]], caller.CallerModel]] = None,
    tts_factory: Optional[Callable[[Mapping[str, Any]], caller.CallerTTS]] = None,
    base_dir: str = ".", created_at: Optional[str] = None,
    remote_endpoint: Optional[str] = None,
    execution_scope: Optional[str] = None,
) -> CallerLoadRun:
    """Run a bounded workload and produce an offline-verifiable package."""

    if not callable(session_factory):
        raise ValueError("session_factory must be callable")
    if model_factory is not None and not callable(model_factory):
        raise ValueError("model_factory must be callable")
    if tts_factory is not None and not callable(tts_factory):
        raise ValueError("tts_factory must be callable")
    plan = validate_plan(dict(plan_value))
    if plan["caller_plan"]["mode"] == "generative" and model_factory is None:
        raise ValueError("generative caller plans require model_factory")
    if execution_scope not in {"local", "remote"}:
        raise ValueError("execution_scope must be explicitly 'local' or 'remote'")
    declared_remote = plan["safety"]["remote_execution"]
    if execution_scope == "remote" and remote_endpoint is None:
        raise ValueError("remote execution_scope requires remote_endpoint")
    if execution_scope == "local" and remote_endpoint is not None:
        raise ValueError("local execution_scope cannot supply remote_endpoint")
    if execution_scope == "local" and declared_remote is not None:
        raise ValueError(
            "remote_execution is declared for a local execution_scope"
        )
    if execution_scope == "remote" and declared_remote is None:
        raise ValueError(
            "remote caller load requires safety.remote_execution in the workload plan"
        )
    if remote_endpoint is not None:
        normalized_remote = normalize_remote_endpoint(remote_endpoint)
        if normalized_remote != declared_remote["endpoint"]:
            raise ValueError("remote runtime endpoint is outside the plan allowlist")
    execution_boundary = _execution_boundary(plan)
    output = _empty_output_root(output_dir)
    (output / "children").mkdir()
    plan_bytes = _canonical(plan) + b"\n"
    _exclusive_write(output / "workload-plan.json", plan_bytes)
    plan_sha = _sha(_canonical(plan))
    base = Path(base_dir).resolve()
    timestamp = created_at or time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    observations: List[Dict[str, Any]] = []
    mp = _process_context()
    # Keep worker staging on the output filesystem so a verified child can be
    # published with one atomic directory rename (no cross-device copy gap).
    with tempfile.TemporaryDirectory(
        prefix=".hotato-caller-load-", dir=str(output.parent)
    ) as temporary:
        work_root = Path(temporary)
        for stage in plan["stages"]:
            observations.extend(_execute_stage(
                mp, plan, plan_sha, stage, output, work_root, base,
                session_factory, model_factory, tts_factory,
            ))
    observations.sort(key=lambda row: (
        next(index for index, stage in enumerate(plan["stages"]) if stage["id"] == row["stage_id"]),
        int(row["invocation_index"]),
    ))
    observation_bytes = b"".join(_canonical(row) + b"\n" for row in observations)
    _exclusive_write(output / "observations.jsonl", observation_bytes)
    metrics, stages = _metrics(plan, observations)
    slos = _evaluate_slos(plan, metrics)
    status, exit_code = _status(plan, metrics, slos)
    result: Dict[str, Any] = {
        "schema": RESULT_SCHEMA, "plan_id": plan["id"], "created_at": timestamp,
        "workload_plan_sha256": plan_sha,
        "semantics": {
            "caller_completion_is_agent_quality_pass": False,
            "delivery_evidence_is_caller_completion": False,
            "actual_cost_microusd_scope": "caller_model_reported_only",
            "blended_score": None,
        },
        "execution_boundary": execution_boundary,
        "metrics": metrics, "stages": stages, "slos": slos,
        "safety": {
            **plan["safety"],
            "actual_cost_microusd": metrics["actual_cost_microusd"],
            "cost_bound_respected": metrics["actual_cost_microusd"] <= plan["safety"]["max_cost_microusd"],
            "cost_bound_scope": "caller_model_reported_only",
        },
        "artifacts": {
            "workload_plan": {"path": "workload-plan.json", "sha256": _sha(plan_bytes), "bytes": len(plan_bytes)},
            "observations": {"path": "observations.jsonl", "sha256": _sha(observation_bytes), "bytes": len(observation_bytes)},
            "children_root": "children",
        },
        "status": status, "exit_code": exit_code,
    }
    result["result_id"] = _sha(_canonical(result))
    _exclusive_write(output / "result.json", _canonical(result) + b"\n")
    _write_manifest(output, result["result_id"])
    verification = verify_caller_load(str(output))
    if not verification["ok"]:
        raise CallerLoadError(
            "published caller-load package failed self-verification: "
            + ", ".join(verification["mismatches"])
        )
    return CallerLoadRun(str(output), result, verification)


def _observed_files(root: Path) -> Tuple[set, List[str]]:
    files = set()
    errors = []
    count = 0
    for path in root.rglob("*"):
        count += 1
        if count > _MAX_PACKAGE_FILES * 2:
            errors.append("output:entry-limit")
            break
        if path.is_symlink():
            errors.append("symlink:" + path.relative_to(root).as_posix())
        elif path.is_file():
            files.add(path.relative_to(root).as_posix())
    return files, errors


def _manifest_checks(root: Path, manifest: Mapping[str, Any]) -> List[str]:
    mismatches: List[str] = []
    if manifest.get("schema") != PACKAGE_SCHEMA:
        return ["manifest:schema"]
    unsigned = dict(manifest)
    claimed = unsigned.pop("package_id", None)
    if claimed != _sha(_canonical(unsigned)):
        mismatches.append("manifest:package-id")
    rows = manifest.get("files")
    if not isinstance(rows, dict) or len(rows) > _MAX_PACKAGE_FILES:
        return mismatches + ["manifest:files"]
    observed, errors = _observed_files(root)
    mismatches.extend(errors)
    observed.discard("package-manifest.json")
    expected = set(rows)
    if expected != observed:
        mismatches.append("manifest:file-set")
    for rel in sorted(expected & observed):
        if not _safe_relative(rel):
            mismatches.append("manifest:path:" + repr(rel))
            continue
        row = rows[rel]
        if not isinstance(row, dict) or set(row) != {"bytes", "sha256"}:
            mismatches.append("manifest:file-row:" + rel)
            continue
        try:
            data = _read_regular(root / rel)
        except (OSError, ValueError):
            mismatches.append("manifest:unreadable:" + rel)
            continue
        if row.get("bytes") != len(data) or row.get("sha256") != _sha(data):
            mismatches.append("manifest:digest:" + rel)
    return mismatches


def _parse_observations(path: Path) -> List[Dict[str, Any]]:
    raw = _read_regular(path, _MAX_FILE_BYTES)
    rows: List[Dict[str, Any]] = []
    for index, line in enumerate(raw.splitlines()):
        if not line.strip():
            raise ValueError(f"blank observations line {index + 1}")
        value = json.loads(
            line.decode("utf-8"),
            parse_constant=lambda item: (_ for _ in ()).throw(ValueError(item)),
        )
        if not isinstance(value, dict):
            raise ValueError(f"observation {index + 1} is not a mapping")
        rows.append(value)
        if len(rows) > _MAX_CALLS:
            raise ValueError("observations exceed call limit")
    return rows


def _expected_layout(plan: Mapping[str, Any], rows: Sequence[Mapping[str, Any]]) -> set:
    expected = {"workload-plan.json", "observations.jsonl", "result.json", "package-manifest.json"}
    for row in rows:
        if row.get("disposition") != "STARTED":
            continue
        rel = row.get("package_path")
        if _safe_relative(rel):
            expected.add(str(rel) + "/package-manifest.json")
            # Child-owned files are taken from the verified child manifest, not
            # from aggregate observations.
    return expected


def _semantic_rows(
    root: Path, plan: Mapping[str, Any], plan_sha: str, rows: Sequence[Mapping[str, Any]],
) -> Tuple[List[Dict[str, Any]], List[str]]:
    mismatches: List[str] = []
    expected_keys = []
    stage_map = {stage["id"]: stage for stage in plan["stages"]}
    for stage in plan["stages"]:
        expected_keys.extend((stage["id"], index) for index in range(int(stage["calls"])))
    observed_keys = [(row.get("stage_id"), row.get("invocation_index")) for row in rows]
    if observed_keys != expected_keys:
        mismatches.append("observations:stage-index-sequence")
    normalized: List[Dict[str, Any]] = []
    for position, row in enumerate(rows):
        stage_id = row.get("stage_id")
        index = row.get("invocation_index")
        if stage_id not in stage_map or isinstance(index, bool) or not isinstance(index, int):
            mismatches.append(f"observation:{position}:identity")
            continue
        stage = stage_map[stage_id]
        context = _child_context(plan, plan_sha, stage, index)
        if row.get("child_id") != context["child_id"] or row.get("stage_model") != stage["model"]:
            mismatches.append(f"observation:{position}:child-binding")
        disposition = row.get("disposition")
        if disposition not in {"STARTED", "DROPPED", "STOPPED"}:
            mismatches.append(f"observation:{position}:disposition")
            continue
        common = {
            "stage_id", "stage_model", "invocation_index", "child_id", "disposition",
            "scheduled_offset_ms", "scheduling_delay_ms",
        }
        expected_fields = common
        if disposition == "STARTED":
            expected_fields = common | {
                "duration_ms", "supervisor_timeout", "worker_exit_code", "package_path",
                "child_package_id", "child_package_verified", "caller_status",
                "caller_exit_code", "delivery_evidence_state",
                "session_endpoint_state", "actual_cost_microusd",
            }
        elif disposition == "DROPPED":
            expected_fields = common | {"drop_reason"}
        if set(row) != expected_fields:
            mismatches.append(f"observation:{position}:fields")
        for timing_name in ("scheduled_offset_ms", "scheduling_delay_ms"):
            timing = row.get(timing_name)
            if (
                isinstance(timing, bool) or not isinstance(timing, (int, float))
                or not math.isfinite(float(timing)) or timing < 0
            ):
                mismatches.append(f"observation:{position}:{timing_name}")
        if stage["model"] == "open":
            expected_offset = round(index * 1000.0 / float(stage["arrival_rate_per_second"]), 6)
            if row.get("scheduled_offset_ms") != expected_offset:
                mismatches.append(f"observation:{position}:scheduled-offset")
        if disposition != "STARTED":
            if disposition == "DROPPED" and (
                stage["model"] != "open" or row.get("drop_reason") not in {"MAX_IN_FLIGHT", "START_DELAY"}
            ):
                mismatches.append(f"observation:{position}:drop-reason")
            normalized.append(dict(row))
            continue
        expected_path = "children/" + context["child_id"]
        if row.get("package_path") != expected_path:
            mismatches.append(f"observation:{position}:package-path")
            continue
        duration = row.get("duration_ms")
        if (
            isinstance(duration, bool) or not isinstance(duration, (int, float))
            or not math.isfinite(float(duration)) or duration < 0
        ):
            mismatches.append(f"observation:{position}:duration_ms")
        if not isinstance(row.get("supervisor_timeout"), bool):
            mismatches.append(f"observation:{position}:supervisor_timeout")
        worker_exit = row.get("worker_exit_code")
        if worker_exit is not None and (isinstance(worker_exit, bool) or not isinstance(worker_exit, int)):
            mismatches.append(f"observation:{position}:worker_exit_code")
        child_root = root / expected_path
        verification = caller.verify_package(str(child_root))
        if not verification.get("ok"):
            mismatches.append(f"child:{context['child_id']}:verification")
            continue
        try:
            child_plan = _load_json(child_root / "caller-plan.json")
            child_result = _load_json(child_root / "caller-result.json")
        except (OSError, ValueError):
            mismatches.append(f"child:{context['child_id']}:unreadable")
            continue
        expected_child_plan = _caller_plan_for_child(plan, context)
        if child_plan != expected_child_plan:
            mismatches.append(f"child:{context['child_id']}:plan-binding")
        expected_fields = {
            "child_package_id": verification.get("package_id"),
            "child_package_verified": True,
            "caller_status": child_result.get("status"),
            "caller_exit_code": child_result.get("exit_code"),
            "delivery_evidence_state": _evidence_state(child_result, context),
            "session_endpoint_state": _session_endpoint_state(child_result, context),
            "actual_cost_microusd": _actual_cost(child_result),
        }
        for key, expected in expected_fields.items():
            if row.get(key) != expected:
                mismatches.append(f"observation:{position}:{key}")
        normalized.append(dict(row))
    return normalized, mismatches


def _exact_layout(root: Path, rows: Sequence[Mapping[str, Any]]) -> List[str]:
    """Reject any file not owned by the aggregate or a verified child manifest."""

    expected = {"workload-plan.json", "observations.jsonl", "result.json", "package-manifest.json"}
    for row in rows:
        if row.get("disposition") != "STARTED" or not _safe_relative(row.get("package_path")):
            continue
        prefix = str(row["package_path"])
        try:
            child_manifest = _load_json(root / prefix / "package-manifest.json")
        except (OSError, ValueError):
            continue
        child_files = child_manifest.get("files") if isinstance(child_manifest, dict) else None
        if isinstance(child_files, dict):
            expected.add(prefix + "/package-manifest.json")
            for rel in child_files:
                if _safe_relative(rel):
                    expected.add(prefix + "/" + rel)
    observed, errors = _observed_files(root)
    if observed != expected:
        errors.append("output:unexpected-or-missing-file")
    return errors


def verify_caller_load(output_dir: str) -> Dict[str, Any]:
    """Verify and recompute a caller-load package without network or models."""

    root = Path(os.path.abspath(output_dir))
    mismatches: List[str] = []
    try:
        root_info = os.lstat(root)
    except OSError:
        return {"ok": False, "package_id": None, "mismatches": ["output:invalid"]}
    if stat.S_ISLNK(root_info.st_mode) or not stat.S_ISDIR(root_info.st_mode):
        return {"ok": False, "package_id": None, "mismatches": ["output:invalid"]}
    try:
        manifest = _load_json(root / "package-manifest.json")
    except (OSError, ValueError):
        return {"ok": False, "package_id": None, "mismatches": ["manifest:unreadable"]}
    if not isinstance(manifest, dict):
        return {"ok": False, "package_id": None, "mismatches": ["manifest:invalid"]}
    try:
        mismatches.extend(_manifest_checks(root, manifest))
        plan_raw = _load_json(root / "workload-plan.json")
        plan = validate_plan(plan_raw)
        if plan != plan_raw:
            mismatches.append("workload-plan:not-normalized")
        plan_sha = _sha(_canonical(plan))
        rows = _parse_observations(root / "observations.jsonl")
        semantic_rows, row_mismatches = _semantic_rows(root, plan, plan_sha, rows)
        mismatches.extend(row_mismatches)
        mismatches.extend(_exact_layout(root, rows))
        result = _load_json(root / "result.json")
        if not isinstance(result, dict) or result.get("schema") != RESULT_SCHEMA:
            raise ValueError("result schema")
        expected_result_fields = {
            "schema", "plan_id", "created_at", "workload_plan_sha256", "semantics",
            "execution_boundary", "metrics", "stages", "slos", "safety", "artifacts", "status",
            "exit_code", "result_id",
        }
        if set(result) != expected_result_fields:
            mismatches.append("result:fields")
        unsigned = dict(result)
        result_id = unsigned.pop("result_id", None)
        if result_id != _sha(_canonical(unsigned)):
            mismatches.append("result:result-id")
        if manifest.get("result_id") != result_id:
            mismatches.append("result:manifest-binding")
        metrics, stages = _metrics(plan, semantic_rows)
        slos = _evaluate_slos(plan, metrics)
        status, exit_code = _status(plan, metrics, slos)
        expected = {
            "plan_id": plan["id"], "workload_plan_sha256": plan_sha,
            "execution_boundary": _execution_boundary(plan),
            "metrics": metrics, "stages": stages, "slos": slos,
            "status": status, "exit_code": exit_code,
        }
        for key, value in expected.items():
            if result.get(key) != value:
                mismatches.append("result:" + key)
        expected_artifacts = {
            "workload_plan": {
                "path": "workload-plan.json",
                "sha256": _sha(_read_regular(root / "workload-plan.json")),
                "bytes": len(_read_regular(root / "workload-plan.json")),
            },
            "observations": {
                "path": "observations.jsonl",
                "sha256": _sha(_read_regular(root / "observations.jsonl")),
                "bytes": len(_read_regular(root / "observations.jsonl")),
            },
            "children_root": "children",
        }
        if result.get("artifacts") != expected_artifacts:
            mismatches.append("result:artifacts")
        expected_safety = {
            **plan["safety"], "actual_cost_microusd": metrics["actual_cost_microusd"],
            "cost_bound_respected": metrics["actual_cost_microusd"] <= plan["safety"]["max_cost_microusd"],
            "cost_bound_scope": "caller_model_reported_only",
        }
        if result.get("safety") != expected_safety:
            mismatches.append("result:safety")
        expected_semantics = {
            "caller_completion_is_agent_quality_pass": False,
            "delivery_evidence_is_caller_completion": False,
            "actual_cost_microusd_scope": "caller_model_reported_only",
            "blended_score": None,
        }
        if result.get("semantics") != expected_semantics:
            mismatches.append("result:semantics")
    except (OSError, ValueError, TypeError, AttributeError) as exc:
        mismatches.append("verification:invalid:" + type(exc).__name__)
        metrics, stages, slos, status, exit_code = {}, [], [], None, None
    return {
        "ok": not mismatches, "package_id": manifest.get("package_id"),
        "mismatches": sorted(set(mismatches)),
        "recomputed": {
            "metrics": metrics, "stages": stages, "slos": slos,
            "status": status, "exit_code": exit_code,
        },
    }


__all__ = [
    "PLAN_SCHEMA", "RESULT_SCHEMA", "PACKAGE_SCHEMA", "CONTEXT_SCHEMA",
    "DELIVERED_AUDIO_EVENT", "REMOTE_ACKNOWLEDGEMENT", "CallerLoadRun",
    "CallerLoadError", "validate_plan", "load_plan", "run_caller_load",
    "verify_caller_load",
]
