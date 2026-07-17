"""Evidence-preserving load, stress, and recovery execution.

The scheduler supports both closed concurrency and open arrival-rate workloads.
It records coordinated-omission signals (scheduled starts, scheduling delay,
dropped starts, and generator saturation) and publishes one independently
verifiable child package per started call.  Provider lifecycle completion is
reported separately from delivered-media and outcome evidence.
"""

from __future__ import annotations

import concurrent.futures
import hashlib
import json
import math
import os
import shutil
import stat
import tempfile
import threading
import time
from dataclasses import dataclass
from html import escape
from typing import Any, Dict, Iterable, List, Mapping, Optional

from .errors import safe_json_dumps
from .telephony import SUCCESS_STATUSES, TelephonyClient, validate_spec

_MAX_CALLS = 100_000
_MAX_CONCURRENCY = 1_000
_PHASES = frozenset({"warmup", "ramp", "stress", "spike", "soak", "recovery"})
_MODELS = frozenset({"closed", "open"})
_EVIDENCE_STATES = frozenset({"PRESENT", "MISSING", "UNSUPPORTED", "UNOBSERVABLE"})
_MAX_SUMMARY_BYTES = 8 * 1024 * 1024
_MAX_OBSERVATIONS_BYTES = 512 * 1024 * 1024
_MAX_OBSERVATION_BYTES = 1024 * 1024
_MAX_CHILD_ARTIFACT_BYTES = 16 * 1024 * 1024
_LOAD_EVIDENCE_SCHEMA = "hotato.load-evidence.v1"
_LOAD_CALL_PACKAGE_SCHEMA = "hotato.load-call-package.v2"
_LOAD_VERIFICATION_PLAN_SCHEMA = "hotato.load-verification-plan.v2"
_EVIDENCE_LANES = ("delivered_audio", "tool_trace", "backend_state")
_EVIDENCE_AUTHORITIES = frozenset({
    "measured",
    "signed_attestation",
    "target_reported",
    "provider_reported",
    "sidecar_reported",
    "unverified",
})
_EXECUTION_CLAIM_AUTHORITIES = frozenset({"measured", "signed_attestation"})


@dataclass(frozen=True)
class LoadResult:
    output_dir: str
    summary: Dict[str, Any]
    verification: Dict[str, Any]

    @property
    def exit_code(self) -> int:
        return int(self.summary["exit_code"])


class LoadError(RuntimeError):
    """The bounded load execution could not be completed safely."""


def _read_regular_bytes(path: str, maximum: int) -> bytes:
    """Read a bounded regular file without FIFO or symlink-swap ambiguity."""
    before = os.lstat(path)
    if stat.S_ISLNK(before.st_mode) or not stat.S_ISREG(before.st_mode):
        raise ValueError(f"{path!r} must be a regular non-symlink file")
    if before.st_size > maximum:
        raise ValueError(f"{path!r} exceeds its read limit")
    flags = os.O_RDONLY | getattr(os, "O_BINARY", 0)
    flags |= getattr(os, "O_NONBLOCK", 0) | getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(path, flags)
    try:
        opened = os.fstat(descriptor)
        if (
            not stat.S_ISREG(opened.st_mode)
            or (opened.st_dev, opened.st_ino) != (before.st_dev, before.st_ino)
        ):
            raise ValueError(f"{path!r} changed while it was opened")
        chunks = []
        total = 0
        while total <= maximum:
            chunk = os.read(descriptor, min(65_536, maximum + 1 - total))
            if not chunk:
                break
            chunks.append(chunk)
            total += len(chunk)
        raw = b"".join(chunks)
        if len(raw) > maximum:
            raise ValueError(f"{path!r} exceeds its read limit")
        return raw
    finally:
        os.close(descriptor)


def _bounded_directory_names(path: str, maximum: int) -> set[str]:
    if os.path.islink(path) or not os.path.isdir(path):
        raise ValueError(f"{path!r} must be a directory and cannot be a symlink")
    names = set()
    with os.scandir(path) as entries:
        for entry in entries:
            if len(names) >= maximum:
                raise ValueError(f"{path!r} exceeds its entry limit")
            names.add(entry.name)
    return names


def _number(value: Any, name: str, low: float, high: float) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{name} must be numeric")
    result = float(value)
    if not math.isfinite(result) or not low <= result <= high:
        raise ValueError(f"{name} must be in [{low}, {high}]")
    return result


def _integer(value: Any, name: str, low: int, high: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not low <= value <= high:
        raise ValueError(f"{name} must be an integer in [{low}, {high}]")
    return value


def validate_plan(value: Any, base_dir: str = ".") -> Dict[str, Any]:
    """Validate and normalize v1 closed plans and the v2 workload contract."""
    if not isinstance(value, dict):
        raise ValueError("load plan must be a mapping")
    schema = value.get("schema")
    if schema not in ("hotato.load-plan.v1", "hotato.load-plan.v2"):
        raise ValueError("load plan schema must be hotato.load-plan.v1 or hotato.load-plan.v2")
    allowed_v1 = {"schema", "id", "call", "stages", "terminal_timeout_seconds", "poll_seconds", "slos"}
    allowed_v2 = allowed_v1 | {"safety", "faults"}
    unknown = sorted(set(value) - (allowed_v1 if schema.endswith("v1") else allowed_v2))
    if unknown:
        raise ValueError("load plan contains unknown field(s): " + ", ".join(unknown))
    plan_id = value.get("id")
    if not isinstance(plan_id, str) or not plan_id or len(plan_id) > 200:
        raise ValueError("load plan id must be 1..200 characters")
    call = value.get("call")
    call_spec = validate_spec(call)
    # Store one canonical semantic representation.  Defaults and absent
    # optional values must not make two equivalent executions hash as different
    # workload contracts.
    normalized_call = {
        "schema": "hotato.telephony-call.v1",
        "id": call_spec.id,
        "provider": call_spec.provider,
        "to": call_spec.to,
        "from": call_spec.from_,
        "agent_id": call_spec.agent_id,
        "phone_number_id": call_spec.phone_number_id,
        "twiml_url": call_spec.twiml_url,
        "callback_url": call_spec.callback_url,
        "timeout_seconds": call_spec.timeout_seconds,
        "record": call_spec.record,
        "metadata": dict(call_spec.metadata),
    }
    stages = value.get("stages")
    if not isinstance(stages, list) or not stages or len(stages) > 100:
        raise ValueError("stages must contain 1..100 rows")

    normalized: List[Dict[str, Any]] = []
    total = 0
    for index, raw in enumerate(stages):
        if not isinstance(raw, dict):
            raise ValueError(f"stages[{index}] must be a mapping")
        if schema.endswith("v1"):
            if set(raw) != {"concurrency", "calls"}:
                raise ValueError(f"stages[{index}] must contain concurrency and calls")
            row = {
                "name": f"stage-{index}", "phase": "stress", "model": "closed",
                "concurrency": _integer(raw["concurrency"], f"stages[{index}].concurrency", 1, _MAX_CONCURRENCY),
                "calls": _integer(raw["calls"], f"stages[{index}].calls", 1, _MAX_CALLS),
            }
        else:
            common = {"name", "phase", "model"}
            if not common <= set(raw):
                raise ValueError(f"stages[{index}] requires name, phase, and model")
            if set(raw) - common - {"concurrency", "calls", "arrival_rate_per_second", "duration_seconds", "max_in_flight"}:
                raise ValueError(f"stages[{index}] contains unknown fields")
            if not isinstance(raw["name"], str) or not raw["name"] or len(raw["name"]) > 100:
                raise ValueError(f"stages[{index}].name must be 1..100 characters")
            if raw["phase"] not in _PHASES:
                raise ValueError(f"stages[{index}].phase is unsupported")
            if raw["model"] not in _MODELS:
                raise ValueError(f"stages[{index}].model is unsupported")
            row = {"name": raw["name"], "phase": raw["phase"], "model": raw["model"]}
            if raw["model"] == "closed":
                if set(raw) != common | {"concurrency", "calls"}:
                    raise ValueError(f"closed stages[{index}] requires only concurrency and calls")
                row.update({
                    "concurrency": _integer(raw["concurrency"], f"stages[{index}].concurrency", 1, _MAX_CONCURRENCY),
                    "calls": _integer(raw["calls"], f"stages[{index}].calls", 1, _MAX_CALLS),
                })
            else:
                if set(raw) != common | {"arrival_rate_per_second", "duration_seconds", "max_in_flight"}:
                    raise ValueError(f"open stages[{index}] requires arrival_rate_per_second, duration_seconds, and max_in_flight")
                rate = _number(raw["arrival_rate_per_second"], f"stages[{index}].arrival_rate_per_second", 0.001, 100_000)
                duration = _number(raw["duration_seconds"], f"stages[{index}].duration_seconds", 0.001, 604_800)
                row.update({
                    "arrival_rate_per_second": rate,
                    "duration_seconds": duration,
                    "max_in_flight": _integer(raw["max_in_flight"], f"stages[{index}].max_in_flight", 1, _MAX_CONCURRENCY),
                    "calls": int(math.ceil(rate * duration)),
                })
        total += row["calls"]
        if total > _MAX_CALLS:
            raise ValueError(f"load plan exceeds {_MAX_CALLS} scheduled calls")
        normalized.append(row)

    terminal = _number(value.get("terminal_timeout_seconds", 600), "terminal_timeout_seconds", 0.01, 86_400)
    poll = _number(value.get("poll_seconds", 2.0), "poll_seconds", 0.001, 300)
    allowed_slos = {
        "max_create_error_rate", "max_incomplete_rate", "min_lifecycle_completion_rate",
        "min_evidence_complete_rate", "max_p95_terminal_seconds", "max_dropped_start_rate",
        "max_p95_scheduling_delay_seconds", "max_recovery_seconds", "min_completion_rate",
    }
    slos = value.get("slos", {})
    if not isinstance(slos, dict) or set(slos) - allowed_slos:
        raise ValueError("slos uses unknown fields")
    normalized_slos = {
        ("min_lifecycle_completion_rate" if name == "min_completion_rate" else name):
        _number(raw, f"slos.{name}", 0, 10**12)
        for name, raw in slos.items()
    }

    raw_safety = value.get("safety", {})
    if not isinstance(raw_safety, dict) or set(raw_safety) - {
        "max_calls", "estimated_cost_per_call_usd", "max_estimated_cost_usd",
        "allowed_destinations", "stop_file",
    }:
        raise ValueError("safety uses unknown fields")
    max_calls = _integer(raw_safety.get("max_calls", total), "safety.max_calls", 1, _MAX_CALLS)
    per_call = _number(raw_safety.get("estimated_cost_per_call_usd", 0), "safety.estimated_cost_per_call_usd", 0, 1_000_000)
    max_cost = _number(raw_safety.get("max_estimated_cost_usd", 0), "safety.max_estimated_cost_usd", 0, 10**12)
    destinations = raw_safety.get("allowed_destinations", [])
    if not isinstance(destinations, list) or len(destinations) > 1_000 or any(not isinstance(x, str) or not x for x in destinations):
        raise ValueError("safety.allowed_destinations must be a list of non-empty strings")
    stop_file = raw_safety.get("stop_file")
    if stop_file is not None:
        if not isinstance(stop_file, str) or not stop_file:
            raise ValueError("safety.stop_file must be a non-empty path")
        stop_file = os.path.abspath(os.path.join(base_dir, stop_file))
    if total > max_calls:
        raise ValueError("scheduled calls exceed safety.max_calls")
    estimated = total * per_call
    if max_cost <= 0 and estimated > 0:
        raise ValueError("a positive estimated cost requires safety.max_estimated_cost_usd")
    if max_cost > 0 and estimated > max_cost + 1e-12:
        raise ValueError("estimated plan cost exceeds safety.max_estimated_cost_usd")
    destination = call_spec.to
    if destinations and not any(destination.startswith(prefix) for prefix in destinations):
        raise ValueError("call destination is outside safety.allowed_destinations")
    if call_spec.provider != "local":
        # Legacy v1 did not carry billable-run authority.  Preserve it for the
        # hermetic local fixture only; a provider call must use v2 and declare
        # every spend/destination gate instead of inheriting zero-valued
        # defaults that make unknown spend look free.
        if schema.endswith("v1"):
            raise ValueError(
                "remote load plans require hotato.load-plan.v2 billable safety gates"
            )
        required_billable = {
            "estimated_cost_per_call_usd",
            "max_estimated_cost_usd",
            "allowed_destinations",
        }
        missing_billable = sorted(required_billable - set(raw_safety))
        if missing_billable:
            raise ValueError(
                "remote load safety must explicitly declare: "
                + ", ".join(missing_billable)
            )
        if per_call <= 0:
            raise ValueError(
                "remote load safety.estimated_cost_per_call_usd must be positive"
            )
        if max_cost <= 0:
            raise ValueError(
                "remote load safety.max_estimated_cost_usd must be positive"
            )
        if not destinations:
            raise ValueError(
                "remote load safety.allowed_destinations must be non-empty"
            )

    faults = value.get("faults", [])
    if not isinstance(faults, list) or len(faults) > 1_000:
        raise ValueError("faults must be a list with at most 1000 rows")
    normalized_faults = []
    names = {row["name"] for row in normalized}
    for index, fault in enumerate(faults):
        required = {"stage", "after_call", "kind", "duration_calls"}
        if not isinstance(fault, dict) or set(fault) != required:
            raise ValueError(f"faults[{index}] must contain stage, after_call, kind, duration_calls")
        if fault["stage"] not in names:
            raise ValueError(f"faults[{index}].stage does not name a stage")
        if not isinstance(fault["kind"], str) or not fault["kind"] or len(fault["kind"]) > 100:
            raise ValueError(f"faults[{index}].kind must be a bounded string")
        normalized_faults.append({
            "stage": fault["stage"],
            "after_call": _integer(fault["after_call"], f"faults[{index}].after_call", 0, _MAX_CALLS),
            "kind": fault["kind"],
            "duration_calls": _integer(fault["duration_calls"], f"faults[{index}].duration_calls", 1, _MAX_CALLS),
        })

    return {
        "schema": "hotato.load-plan.v2", "id": plan_id, "call": normalized_call,
        "stages": normalized, "terminal_timeout_seconds": terminal, "poll_seconds": poll,
        "slos": normalized_slos, "safety": {
            "max_calls": max_calls, "estimated_cost_per_call_usd": per_call,
            "max_estimated_cost_usd": max_cost, "estimated_plan_cost_usd": round(estimated, 9),
            "allowed_destinations": list(destinations), "stop_file": stop_file,
        }, "faults": normalized_faults,
    }


def load_plan(path: str) -> Dict[str, Any]:
    try:
        value = json.loads(_read_regular_bytes(path, _MAX_SUMMARY_BYTES))
    except UnicodeDecodeError as exc:
        raise ValueError("load plan must be UTF-8 JSON") from exc
    return validate_plan(value, os.path.dirname(os.path.abspath(path)))


def _canonical(value: Any) -> bytes:
    return (safe_json_dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False) + "\n").encode()


def _sha(raw: bytes) -> str:
    return "sha256:" + hashlib.sha256(raw).hexdigest()


def _digest_string(value: Any) -> bool:
    return (
        isinstance(value, str)
        and len(value) == 71
        and value.startswith("sha256:")
        and all(character in "0123456789abcdef" for character in value[7:])
    )


def _call_id_hash(provider: str, call_id: str) -> str:
    """Pseudonymize a call id inside its provider namespace."""

    return hashlib.sha256((provider + "\x00" + call_id).encode("utf-8")).hexdigest()


def _workload_binding(plan: Mapping[str, Any]) -> Dict[str, Any]:
    """Return the privacy-safe identity of the complete normalized workload."""

    return {
        "provider": plan["call"]["provider"],
        "call_spec_sha256": _sha(_canonical(plan["call"])),
        "normalized_plan_sha256": _sha(_canonical(plan)),
        "terminal_timeout_seconds": plan["terminal_timeout_seconds"],
        "poll_seconds": plan["poll_seconds"],
    }


def _normalize_evidence_export(
    value: Any, *, provider: str, call_id_sha256: str
) -> Dict[str, Any]:
    """Validate one evidence-adapter result before it can affect an SLO.

    ``PRESENT`` is a content-identity statement, not a boolean.  Every present
    lane therefore needs a digest and an explicit authority.  The eligibility
    bit is derived from that authority and must agree with it.  The envelope is
    bound to the provider-namespaced call pseudonym so evidence from another
    child cannot be swapped into this result.
    """

    required = {"schema", "provider", "call_id_sha256", "lanes"}
    if not isinstance(value, Mapping) or set(value) != required:
        raise LoadError("evidence adapter returned an invalid envelope")
    if value.get("schema") != _LOAD_EVIDENCE_SCHEMA:
        raise LoadError("evidence adapter schema is unsupported")
    if value.get("provider") != provider:
        raise LoadError("evidence adapter provider binding does not match the call")
    if value.get("call_id_sha256") != call_id_sha256:
        raise LoadError("evidence adapter call binding does not match the call")
    lanes = value.get("lanes")
    if not isinstance(lanes, Mapping) or set(lanes) != set(_EVIDENCE_LANES):
        raise LoadError("evidence adapter must declare every evidence lane")
    normalized: Dict[str, Dict[str, Any]] = {}
    for name in _EVIDENCE_LANES:
        lane = lanes[name]
        if not isinstance(lane, Mapping) or set(lane) != {
            "state",
            "authority",
            "sha256",
            "eligible_for_execution_claim",
        }:
            raise LoadError(f"evidence lane {name!r} has an invalid shape")
        state = lane.get("state")
        authority = lane.get("authority")
        digest = lane.get("sha256")
        eligible = lane.get("eligible_for_execution_claim")
        if state not in _EVIDENCE_STATES:
            raise LoadError(f"evidence lane {name!r} has an invalid state")
        if authority not in _EVIDENCE_AUTHORITIES:
            raise LoadError(f"evidence lane {name!r} has an invalid authority")
        expected_eligible = (
            state == "PRESENT" and authority in _EXECUTION_CLAIM_AUTHORITIES
        )
        if not isinstance(eligible, bool) or eligible is not expected_eligible:
            raise LoadError(
                f"evidence lane {name!r} execution-claim eligibility contradicts its authority"
            )
        if state == "PRESENT":
            if not _digest_string(digest):
                raise LoadError(
                    f"evidence lane {name!r} PRESENT state requires a sha256 digest"
                )
        elif digest is not None:
            raise LoadError(
                f"evidence lane {name!r} cannot carry a digest unless it is PRESENT"
            )
        normalized[name] = {
            "state": state,
            "authority": authority,
            "sha256": digest,
            "eligible_for_execution_claim": eligible,
        }
    return {
        "schema": _LOAD_EVIDENCE_SCHEMA,
        "provider": provider,
        "call_id_sha256": call_id_sha256,
        "lanes": normalized,
    }


def _execution_evidence_complete(
    evidence: Mapping[str, Any],
    provider_export: Optional[Mapping[str, Any]],
) -> bool:
    """Require presence *and* execution-claim authority for every lane."""

    if evidence.get("call_lifecycle") != "PRESENT" or not isinstance(
        provider_export, Mapping
    ):
        return False
    lanes = provider_export.get("lanes")
    if not isinstance(lanes, Mapping) or set(lanes) != set(_EVIDENCE_LANES):
        return False
    return all(
        isinstance(lanes[name], Mapping)
        and lanes[name].get("state") == "PRESENT"
        and lanes[name].get("eligible_for_execution_claim") is True
        for name in _EVIDENCE_LANES
    )


def _percentile(values: Iterable[float], q: float) -> Optional[float]:
    ordered = sorted(values)
    if not ordered:
        return None
    rank = (len(ordered) - 1) * q
    left, right = int(math.floor(rank)), int(math.ceil(rank))
    result = ordered[left] if left == right else ordered[left] + (ordered[right] - ordered[left]) * (rank - left)
    return round(result, 9)


def _rate(numerator: int, denominator: int) -> float:
    return 0.0 if not denominator else round(numerator / denominator, 9)


def _metrics(rows: List[Dict[str, Any]]) -> Dict[str, Any]:
    scheduled = len(rows)
    started = [row for row in rows if row["start_status"] == "started"]
    created = [row for row in started if row["create_status"] == "created"]
    terminal = [row for row in created if row["terminal_status"] not in (None, "ERROR")]
    completed = [row for row in terminal if row["terminal_status"] in SUCCESS_STATUSES]
    evidence = [row for row in started if row.get("evidence_complete") is True]
    dropped = [row for row in rows if row["start_status"] == "dropped"]
    delays = [float(row["scheduling_delay_seconds"]) for row in started]
    terminal_lat = [float(row["terminal_seconds"]) for row in terminal if row["terminal_seconds"] is not None]
    create_lat = [float(row["create_seconds"]) for row in created]
    return {
        "scheduled": scheduled, "started": len(started), "dropped_starts": len(dropped),
        "created": len(created), "terminal_observed": len(terminal),
        "lifecycle_completed": len(completed), "evidence_complete": len(evidence),
        "dropped_start_rate": _rate(len(dropped), scheduled),
        "create_error_rate": _rate(len(started) - len(created), len(started)),
        "incomplete_rate": _rate(len(created) - len(terminal), len(created)),
        "lifecycle_completion_rate": _rate(len(completed), len(started)),
        "evidence_complete_rate": _rate(len(evidence), len(started)),
        "scheduling_delay_seconds": {"p50": _percentile(delays, .5), "p95": _percentile(delays, .95), "max": round(max(delays), 9) if delays else None},
        "create_seconds": {"p50": _percentile(create_lat, .5), "p95": _percentile(create_lat, .95), "max": round(max(create_lat), 9) if create_lat else None},
        "terminal_seconds": {"p50": _percentile(terminal_lat, .5), "p95": _percentile(terminal_lat, .95), "max": round(max(terminal_lat), 9) if terminal_lat else None},
    }


def _recovery(rows: List[Dict[str, Any]], faults: List[Dict[str, Any]]) -> Dict[str, Any]:
    measurements = []
    for fault in faults:
        candidates = sorted((row for row in rows if row["stage_name"] == fault["stage"]), key=lambda row: row["index"])
        end_index = fault["after_call"] + fault["duration_calls"]
        after = [row for row in candidates if row["index"] >= end_index and row.get("terminal_status") in SUCCESS_STATUSES]
        recovery = None
        if after:
            end_rows = [row for row in candidates if row["index"] == end_index - 1]
            origin = end_rows[0]["finished_monotonic"] if end_rows and end_rows[0].get("finished_monotonic") is not None else after[0]["scheduled_monotonic"]
            recovery = round(max(0.0, after[0]["finished_monotonic"] - origin), 9)
        measurements.append({"stage": fault["stage"], "kind": fault["kind"], "fault_end_call": end_index, "recovery_seconds": recovery})
    values = [row["recovery_seconds"] for row in measurements if row["recovery_seconds"] is not None]
    return {"measurements": measurements, "max_seconds": max(values) if values else None}


def _evaluate_slos(metrics: Dict[str, Any], recovery: Dict[str, Any], slos: Dict[str, float]) -> List[Dict[str, Any]]:
    mapping = {
        "max_create_error_rate": (metrics["create_error_rate"], "max"),
        "max_incomplete_rate": (metrics["incomplete_rate"], "max"),
        "min_lifecycle_completion_rate": (metrics["lifecycle_completion_rate"], "min"),
        "min_evidence_complete_rate": (metrics["evidence_complete_rate"], "min"),
        "max_p95_terminal_seconds": (metrics["terminal_seconds"]["p95"], "max"),
        "max_dropped_start_rate": (metrics["dropped_start_rate"], "max"),
        "max_p95_scheduling_delay_seconds": (metrics["scheduling_delay_seconds"]["p95"], "max"),
        "max_recovery_seconds": (recovery["max_seconds"], "max"),
    }
    rows = []
    for name, threshold in sorted(slos.items()):
        observed, direction = mapping[name]
        status = "INCONCLUSIVE" if observed is None else ("PASS" if (observed <= threshold if direction == "max" else observed >= threshold) else "FAIL")
        rows.append({"id": name, "observed": observed, "operator": direction, "threshold": threshold, "status": status})
    return rows


def _conclusion(slos: List[Dict[str, Any]]) -> tuple[str, int]:
    """Derive the only permitted process conclusion from recomputed SLO rows."""
    if not slos or any(row["status"] == "INCONCLUSIVE" for row in slos):
        return "INCONCLUSIVE", 2
    if any(row["status"] == "FAIL" for row in slos):
        return "FAIL", 1
    return "PASS", 0


def _public_safety(plan: Mapping[str, Any]) -> Dict[str, Any]:
    """Publish cost guardrails without leaking destinations or local paths."""
    safety = plan["safety"]
    return {
        "max_calls": safety["max_calls"],
        "estimated_cost_per_call_usd": safety["estimated_cost_per_call_usd"],
        "max_estimated_cost_usd": safety["max_estimated_cost_usd"],
        "estimated_plan_cost_usd": safety["estimated_plan_cost_usd"],
        "allowed_destination_count": len(safety["allowed_destinations"]),
        "stop_file_configured": safety["stop_file"] is not None,
    }


def _verification_plan(plan: Mapping[str, Any]) -> Dict[str, Any]:
    """Return the non-secret execution contract required to recompute a verdict."""
    return {
        "schema": _LOAD_VERIFICATION_PLAN_SCHEMA,
        "plan_id": plan["id"],
        "workload": _workload_binding(plan),
        "stages": json.loads(json.dumps(plan["stages"])),
        "faults": json.loads(json.dumps(plan["faults"])),
        "slos": dict(plan["slos"]),
        "safety": _public_safety(plan),
    }


def _validate_verification_plan(value: Any) -> Dict[str, Any]:
    """Strictly validate the portable subset before using it as verifier input."""
    if not isinstance(value, dict) or set(value) != {
        "schema", "plan_id", "workload", "stages", "faults", "slos", "safety",
    }:
        raise ValueError("verification plan has an invalid shape")
    if value.get("schema") != _LOAD_VERIFICATION_PLAN_SCHEMA:
        raise ValueError("verification plan schema is unsupported")
    if not isinstance(value.get("plan_id"), str) or not value["plan_id"]:
        raise ValueError("verification plan id is invalid")
    workload = value.get("workload")
    if not isinstance(workload, dict) or set(workload) != {
        "provider",
        "call_spec_sha256",
        "normalized_plan_sha256",
        "terminal_timeout_seconds",
        "poll_seconds",
    }:
        raise ValueError("verification plan workload binding is invalid")
    if workload.get("provider") not in {"local", "twilio", "vapi", "retell"}:
        raise ValueError("verification plan provider is invalid")
    if not _digest_string(workload.get("call_spec_sha256")) or not _digest_string(
        workload.get("normalized_plan_sha256")
    ):
        raise ValueError("verification plan workload digest is invalid")
    _number(
        workload.get("terminal_timeout_seconds"),
        "verification.workload.terminal_timeout_seconds",
        0.01,
        86_400,
    )
    _number(
        workload.get("poll_seconds"),
        "verification.workload.poll_seconds",
        0.001,
        300,
    )
    stages = value.get("stages")
    if not isinstance(stages, list) or not stages or len(stages) > 100:
        raise ValueError("verification plan stages are invalid")
    seen = set()
    total = 0
    for index, stage in enumerate(stages):
        if not isinstance(stage, dict):
            raise ValueError("verification plan stage is invalid")
        common = {"name", "phase", "model", "calls"}
        expected = common | ({"concurrency"} if stage.get("model") == "closed" else {
            "arrival_rate_per_second", "duration_seconds", "max_in_flight",
        })
        if set(stage) != expected:
            raise ValueError("verification plan stage shape is invalid")
        if (
            not isinstance(stage["name"], str) or not stage["name"]
            or stage["name"] in seen or stage["phase"] not in _PHASES
            or stage["model"] not in _MODELS
        ):
            raise ValueError("verification plan stage identity is invalid")
        seen.add(stage["name"])
        _integer(stage["calls"], f"verification.stages[{index}].calls", 1, _MAX_CALLS)
        total += stage["calls"]
    if total > _MAX_CALLS:
        raise ValueError("verification plan exceeds the call ceiling")
    faults = value.get("faults")
    if not isinstance(faults, list) or len(faults) > 1_000:
        raise ValueError("verification plan faults are invalid")
    for fault in faults:
        if (
            not isinstance(fault, dict)
            or set(fault) != {"stage", "after_call", "kind", "duration_calls"}
            or fault.get("stage") not in seen
            or not isinstance(fault.get("kind"), str)
            or not fault["kind"]
        ):
            raise ValueError("verification plan fault is invalid")
        _integer(fault["after_call"], "verification.fault.after_call", 0, _MAX_CALLS)
        _integer(fault["duration_calls"], "verification.fault.duration_calls", 1, _MAX_CALLS)
    slos = value.get("slos")
    if not isinstance(slos, dict) or set(slos) - {
        "max_create_error_rate", "max_incomplete_rate", "min_lifecycle_completion_rate",
        "min_evidence_complete_rate", "max_p95_terminal_seconds", "max_dropped_start_rate",
        "max_p95_scheduling_delay_seconds", "max_recovery_seconds",
    }:
        raise ValueError("verification plan SLOs are invalid")
    for name, threshold in slos.items():
        _number(threshold, f"verification.slos.{name}", 0, 10**12)
    safety = value.get("safety")
    if not isinstance(safety, dict) or set(safety) != {
        "max_calls", "estimated_cost_per_call_usd", "max_estimated_cost_usd",
        "estimated_plan_cost_usd", "allowed_destination_count", "stop_file_configured",
    }:
        raise ValueError("verification plan safety summary is invalid")
    if not isinstance(safety["stop_file_configured"], bool):
        raise ValueError("verification plan stop-file flag is invalid")
    _integer(safety["max_calls"], "verification.safety.max_calls", 1, _MAX_CALLS)
    _integer(safety["allowed_destination_count"], "verification.safety.allowed_destination_count", 0, 1_000)
    for name in ("estimated_cost_per_call_usd", "max_estimated_cost_usd", "estimated_plan_cost_usd"):
        _number(safety[name], f"verification.safety.{name}", 0, 10**12)
    return value


def _finite(value: Any, *, nullable: bool = False, nonnegative: bool = False) -> bool:
    if value is None:
        return nullable
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return False
    number = float(value)
    return math.isfinite(number) and (not nonnegative or number >= 0)


def _observation_problem(row: Mapping[str, Any]) -> Optional[str]:
    """Return a reason when an observation cannot safely drive recomputation."""
    required = {
        "schema", "stage_index", "stage_name", "phase", "model", "index",
        "scheduled_monotonic", "started_monotonic", "finished_monotonic",
        "scheduling_delay_seconds", "start_status", "drop_reason", "create_status",
        "call_id_hash", "create_seconds", "terminal_status", "terminal_seconds",
        "error_type", "fault", "evidence", "evidence_complete", "child",
    }
    if set(row) != required:
        return "shape"
    if not _finite(row["scheduled_monotonic"]):
        return "scheduled-time"
    if not isinstance(row["evidence_complete"], bool):
        return "evidence-complete"
    if row["start_status"] == "dropped":
        if (
            row["drop_reason"] not in {"stop_file", "generator_saturated"}
            or any(row[name] is not None for name in (
                "started_monotonic", "finished_monotonic", "scheduling_delay_seconds",
                "create_status", "call_id_hash", "create_seconds", "terminal_status",
                "terminal_seconds", "error_type", "fault", "child",
            ))
            or row["evidence"] != {}
            or row["evidence_complete"] is not False
        ):
            return "dropped-contract"
        return None
    if row["start_status"] != "started":
        return "start-status"
    if (
        row["drop_reason"] is not None
        or not _finite(row["started_monotonic"])
        or not _finite(row["finished_monotonic"])
        or not _finite(row["scheduling_delay_seconds"], nonnegative=True)
        or row["create_status"] not in {"created", "ERROR"}
        or not _finite(row["create_seconds"], nullable=True, nonnegative=True)
        or not _finite(row["terminal_seconds"], nullable=True, nonnegative=True)
        or not isinstance(row["evidence"], dict)
        or set(row["evidence"]) != {"call_lifecycle", "delivered_audio", "tool_trace", "backend_state"}
        or any(state not in _EVIDENCE_STATES for state in row["evidence"].values())
        or not isinstance(row["child"], dict)
    ):
        return "started-contract"
    if row["create_status"] == "created":
        if (
            not isinstance(row["call_id_hash"], str)
            or len(row["call_id_hash"]) != 64
            or any(char not in "0123456789abcdef" for char in row["call_id_hash"])
            or row["create_seconds"] is None
            or not isinstance(row["terminal_status"], str)
            or not row["terminal_status"]
            or len(row["terminal_status"]) > 100
        ):
            return "created-contract"
    elif row["call_id_hash"] is not None or row["create_seconds"] is not None:
        return "create-error-contract"
    if row["error_type"] is not None and (
        not isinstance(row["error_type"], str) or len(row["error_type"]) > 200
    ):
        return "error-type"
    if row["fault"] is not None and (
        not isinstance(row["fault"], dict)
        or set(row["fault"]) != {"kind", "scheduled"}
        or not isinstance(row["fault"].get("kind"), str)
        or len(row["fault"].get("kind", "")) > 100
        or row["fault"].get("scheduled") is not True
    ):
        return "fault"
    return None


def _summary_problem(summary: Mapping[str, Any]) -> Optional[str]:
    required = {
        "schema", "plan_id", "workload_models", "automatic_create_retries",
        "provider_completion_is_quality_pass", "metrics", "stages", "recovery",
        "workload_plan_sha256", "call_spec_sha256", "safety", "slos", "status",
        "exit_code", "artifacts", "result_id",
    }
    if set(summary) != required:
        return "shape"
    artifacts = summary.get("artifacts")
    if (
        summary.get("schema") != "hotato.load-result.v2"
        or not isinstance(summary.get("plan_id"), str)
        or not summary["plan_id"]
        or len(summary["plan_id"]) > 200
        or not isinstance(summary.get("workload_models"), list)
        or not isinstance(summary.get("metrics"), dict)
        or not isinstance(summary.get("stages"), list)
        or not isinstance(summary.get("recovery"), dict)
        or not _digest_string(summary.get("workload_plan_sha256"))
        or not _digest_string(summary.get("call_spec_sha256"))
        or not isinstance(summary.get("safety"), dict)
        or not isinstance(summary.get("slos"), list)
        or summary.get("status") not in {"PASS", "FAIL", "INCONCLUSIVE"}
        or summary.get("exit_code") not in {0, 1, 2}
        or not isinstance(artifacts, dict)
        or set(artifacts) != {
            "observations_sha256", "report_sha256", "verification_plan_sha256",
        }
        or any(not _digest_string(value) for value in artifacts.values())
        or not _digest_string(summary.get("result_id"))
    ):
        return "contract"
    return None


def _write_child(
    root: str,
    row: Dict[str, Any],
    create_receipt: Optional[Mapping[str, Any]],
    terminal_receipt: Optional[Mapping[str, Any]],
    provider_export: Optional[Mapping[str, Any]],
    *,
    provider: str,
    workload_plan_sha256: str,
    call_spec_sha256: str,
) -> Dict[str, Any]:
    child_name = f"{row['stage_index']:03d}-{row['index']:06d}"
    child = os.path.join(root, "calls", child_name)
    os.makedirs(child, mode=0o700)
    artifacts: Dict[str, str] = {}
    for name, value in (("create-receipt.json", create_receipt), ("terminal-receipt.json", terminal_receipt), ("provider-export.json", provider_export)):
        if value is None:
            continue
        raw = _canonical(value)
        with open(os.path.join(child, name), "wb") as handle:  # open-ok: private staging directory
            handle.write(raw)
        artifacts[name] = _sha(raw)
    evidence = dict(row["evidence"])
    manifest = {
        "schema": _LOAD_CALL_PACKAGE_SCHEMA,
        "stage": row["stage_name"],
        "index": row["index"],
        "provider": provider,
        "call_id_sha256": row["call_id_hash"],
        "workload_plan_sha256": workload_plan_sha256,
        "call_spec_sha256": call_spec_sha256,
        "lifecycle_status": row["terminal_status"], "evidence": evidence,
        "evidence_complete": _execution_evidence_complete(
            evidence, provider_export
        ),
        "artifacts": artifacts,
    }
    unsigned = _canonical(manifest)
    manifest["package_id"] = _sha(unsigned)
    raw = _canonical(manifest)
    with open(os.path.join(child, "manifest.json"), "wb") as handle:  # open-ok: private staging directory
        handle.write(raw)
    return {"path": f"calls/{child_name}", "package_id": manifest["package_id"], "evidence_complete": manifest["evidence_complete"]}


def _verify_child(
    root: str,
    reference: Mapping[str, Any],
    row: Mapping[str, Any],
    workload: Mapping[str, Any],
) -> List[str]:
    problems = []
    if set(reference) != {"path", "package_id", "evidence_complete"}:
        return [f"row:{row.get('stage_index')}:{row.get('index')}:child-reference"]
    expected_path = f"calls/{row.get('stage_index', -1):03d}-{row.get('index', -1):06d}"
    if reference.get("path") != expected_path:
        return [f"row:{row.get('stage_index')}:{row.get('index')}:child-path"]
    path = os.path.join(root, expected_path)
    try:
        manifest = json.loads(
            _read_regular_bytes(
                os.path.join(path, "manifest.json"), _MAX_SUMMARY_BYTES
            )
        )
    except (OSError, ValueError, TypeError, UnicodeDecodeError, json.JSONDecodeError):
        return [str(reference.get("path", "")) + ":manifest"]
    if not isinstance(manifest, dict) or set(manifest) != {
        "schema", "stage", "index", "provider", "call_id_sha256",
        "workload_plan_sha256", "call_spec_sha256", "lifecycle_status",
        "evidence", "evidence_complete", "artifacts", "package_id",
    } or manifest.get("schema") != _LOAD_CALL_PACKAGE_SCHEMA or not isinstance(
        manifest.get("evidence"), dict
    ):
        return [str(reference.get("path", "")) + ":manifest-contract"]
    claimed = manifest.pop("package_id", None)
    try:
        expected_package_id = _sha(_canonical(manifest))
    except (TypeError, ValueError):
        expected_package_id = None
    if claimed != expected_package_id or claimed != reference.get("package_id"):
        problems.append(str(reference.get("path", "")) + ":package_id")
    if manifest.get("stage") != row.get("stage_name") or manifest.get("index") != row.get("index"):
        problems.append(str(reference.get("path", "")) + ":observation-binding")
    if manifest.get("call_id_sha256") != row.get("call_id_hash"):
        problems.append(str(reference.get("path", "")) + ":call-binding")
    if manifest.get("provider") != workload.get("provider"):
        problems.append(str(reference.get("path", "")) + ":provider-binding")
    if manifest.get("workload_plan_sha256") != workload.get(
        "normalized_plan_sha256"
    ):
        problems.append(str(reference.get("path", "")) + ":workload-binding")
    if manifest.get("call_spec_sha256") != workload.get("call_spec_sha256"):
        problems.append(str(reference.get("path", "")) + ":call-spec-binding")
    if manifest.get("evidence") != row.get("evidence"):
        problems.append(str(reference.get("path", "")) + ":evidence-binding")
    if (
        manifest.get("evidence_complete") != row.get("evidence_complete")
        or reference.get("evidence_complete") != row.get("evidence_complete")
    ):
        problems.append(str(reference.get("path", "")) + ":completeness-binding")
    artifacts = manifest.get("artifacts", {})
    if not isinstance(artifacts, dict) or set(artifacts) - {
        "create-receipt.json", "terminal-receipt.json", "provider-export.json",
    }:
        problems.append(str(reference.get("path", "")) + ":artifacts")
        artifacts = {}
    try:
        observed_child_names = _bounded_directory_names(path, 8)
    except (OSError, ValueError):
        observed_child_names = set()
        problems.append(str(reference.get("path", "")) + ":layout")
    expected_child_names = {"manifest.json", *artifacts}
    for name in sorted(observed_child_names ^ expected_child_names):
        problems.append(
            str(reference.get("path", ""))
            + (":unexpected:" if name in observed_child_names else ":missing:")
            + name
        )
    for name, digest in artifacts.items():
        try:
            observed = _sha(
                _read_regular_bytes(
                    os.path.join(path, name), _MAX_CHILD_ARTIFACT_BYTES
                )
            )
        except (OSError, ValueError):
            observed = None
        if observed != digest:
            problems.append(str(reference.get("path", "")) + ":" + name)
    if any(value not in _EVIDENCE_STATES for value in manifest.get("evidence", {}).values()):
        problems.append(str(reference.get("path", "")) + ":evidence-state")
    expected_evidence = {
        "call_lifecycle": (
            "PRESENT" if "terminal-receipt.json" in artifacts else "MISSING"
        ),
        **{name: "UNOBSERVABLE" for name in _EVIDENCE_LANES},
    }
    normalized_export: Optional[Dict[str, Any]] = None
    if "provider-export.json" in artifacts:
        try:
            export_value = json.loads(
                _read_regular_bytes(
                    os.path.join(path, "provider-export.json"),
                    _MAX_CHILD_ARTIFACT_BYTES,
                ),
                parse_constant=lambda item: (_ for _ in ()).throw(ValueError(item)),
            )
            normalized_export = _normalize_evidence_export(
                export_value,
                provider=str(workload.get("provider")),
                call_id_sha256=str(manifest.get("call_id_sha256")),
            )
            for name in _EVIDENCE_LANES:
                expected_evidence[name] = normalized_export["lanes"][name]["state"]
        except (OSError, ValueError, TypeError, LoadError):
            problems.append(str(reference.get("path", "")) + ":evidence-export")
    if manifest.get("evidence") != expected_evidence:
        problems.append(str(reference.get("path", "")) + ":evidence-recompute")
    expected_complete = _execution_evidence_complete(
        expected_evidence, normalized_export
    )
    if manifest.get("evidence_complete") is not expected_complete:
        problems.append(str(reference.get("path", "")) + ":evidence-completeness")
    return problems


def _html(summary: Dict[str, Any]) -> bytes:
    rows = "".join(
        f"<tr><td>{escape(stage['name'])}</td><td>{stage['model']}</td><td>{stage['metrics']['scheduled']}</td><td>{stage['metrics']['lifecycle_completion_rate']:.3f}</td><td>{stage['metrics']['evidence_complete_rate']:.3f}</td><td>{stage['metrics']['dropped_start_rate']:.3f}</td></tr>"
        for stage in summary["stages"]
    )
    slos = "".join(f"<tr><td>{escape(row['id'])}</td><td>{escape(str(row['observed']))}</td><td>{row['operator']} {row['threshold']}</td><td class={row['status']}>{row['status']}</td></tr>" for row in summary["slos"])
    return f"""<!doctype html><html><head><meta charset=utf-8><meta http-equiv=Content-Security-Policy content="default-src 'none'; style-src 'unsafe-inline'"><meta name=viewport content="width=device-width"><title>Hotato load result</title><style>body{{font:14px ui-monospace,monospace;max-width:1100px;margin:40px auto;background:#0b0d10;color:#e7edf4}}table{{width:100%;border-collapse:collapse}}td,th{{padding:10px;border-bottom:1px solid #28303a;text-align:left}}.PASS{{color:#66d9a5}}.FAIL{{color:#ff6b6b}}.INCONCLUSIVE{{color:#ffd166}}</style></head><body><h1>Load and recovery · {escape(summary['plan_id'])}</h1><p>Lifecycle completion and evidence completeness are separate.</p><table><tr><th>Stage</th><th>Model</th><th>Scheduled</th><th>Lifecycle</th><th>Evidence</th><th>Dropped</th></tr>{rows}</table><h2>SLOs</h2><table>{slos or '<tr><td>No SLOs declared; result is inconclusive.</td></tr>'}</table></body></html>""".encode()


def run(plan_path: str, output_dir: str, *, client: Optional[TelephonyClient] = None, clock=time.monotonic, sleeper=time.sleep) -> LoadResult:
    plan = load_plan(plan_path)
    client = client or TelephonyClient()
    verification_plan = _verification_plan(plan)
    workload = verification_plan["workload"]
    output_dir = os.path.abspath(output_dir)
    if os.path.lexists(output_dir):
        raise ValueError(f"output directory already exists: {output_dir!r}")
    parent = os.path.dirname(output_dir)
    os.makedirs(parent, exist_ok=True)
    stage_root = tempfile.mkdtemp(prefix=".hotato-load-", dir=parent)
    rows: List[Dict[str, Any]] = []
    rows_lock = threading.Lock()
    faults_by_stage: Dict[str, List[Dict[str, Any]]] = {}
    for fault in plan["faults"]:
        faults_by_stage.setdefault(fault["stage"], []).append(fault)

    def fault_for(stage_name: str, index: int) -> Optional[Dict[str, Any]]:
        for fault in faults_by_stage.get(stage_name, []):
            if fault["after_call"] <= index < fault["after_call"] + fault["duration_calls"]:
                return fault
        return None

    def stopped() -> bool:
        stop_file = plan["safety"]["stop_file"]
        return bool(stop_file and os.path.exists(stop_file))

    def dropped(stage_index: int, stage: Dict[str, Any], index: int, scheduled: float, reason: str) -> None:
        rows.append({
            "schema": "hotato.load-observation.v2", "stage_index": stage_index,
            "stage_name": stage["name"], "phase": stage["phase"], "model": stage["model"],
            "index": index, "scheduled_monotonic": scheduled, "started_monotonic": None,
            "finished_monotonic": None, "scheduling_delay_seconds": None,
            "start_status": "dropped", "drop_reason": reason, "create_status": None,
            "call_id_hash": None, "create_seconds": None, "terminal_status": None,
            "terminal_seconds": None, "error_type": None, "fault": None,
            "evidence": {}, "evidence_complete": False, "child": None,
        })

    def one(stage_index: int, stage: Dict[str, Any], index: int, scheduled: float) -> None:
        started = clock()
        row: Dict[str, Any] = {
            "schema": "hotato.load-observation.v2", "stage_index": stage_index,
            "stage_name": stage["name"], "phase": stage["phase"], "model": stage["model"],
            "index": index, "scheduled_monotonic": round(scheduled, 9),
            "started_monotonic": round(started, 9), "finished_monotonic": None,
            "scheduling_delay_seconds": round(max(0.0, started - scheduled), 9),
            "start_status": "started", "drop_reason": None, "create_status": "ERROR",
            "call_id_hash": None, "create_seconds": None, "terminal_status": None,
            "terminal_seconds": None, "error_type": None, "fault": None,
            "evidence": {"call_lifecycle": "MISSING", "delivered_audio": "UNOBSERVABLE", "tool_trace": "UNOBSERVABLE", "backend_state": "UNOBSERVABLE"},
            "evidence_complete": False, "child": None,
        }
        create_receipt = terminal_receipt = provider_export = None
        try:
            call_doc = json.loads(json.dumps(plan["call"]))
            call_doc["id"] = f"{plan['id']}-s{stage_index}-c{index}"
            fault = fault_for(stage["name"], index)
            if fault:
                row["fault"] = {"kind": fault["kind"], "scheduled": True}
                injector = getattr(client, "inject_fault", None)
                if injector is None:
                    raise LoadError("configured fault is unsupported by the call controller")
                injector(fault["kind"], call_doc)
            handle = client.create(call_doc)
            if (
                getattr(handle, "provider", None) != plan["call"]["provider"]
                or not isinstance(getattr(handle, "call_id", None), str)
                or not handle.call_id
            ):
                raise LoadError("call controller returned an unbound create handle")
            created = clock()
            row["create_status"] = "created"
            row["create_seconds"] = round(created - started, 9)
            row["call_id_hash"] = _call_id_hash(handle.provider, handle.call_id)
            create_receipt = handle.receipt
            final = client.wait(handle, timeout_seconds=plan["terminal_timeout_seconds"], poll_seconds=plan["poll_seconds"])
            if (
                getattr(final, "provider", None) != handle.provider
                or getattr(final, "call_id", None) != handle.call_id
            ):
                raise LoadError("call controller changed provider or call identity while waiting")
            row["terminal_status"] = final.normalized_status
            row["terminal_seconds"] = round(clock() - started, 9)
            terminal_receipt = final.receipt
            row["evidence"]["call_lifecycle"] = "PRESENT"
            # A lifecycle controller's portable ``export`` does not establish
            # delivered media or task outcome.  Only a separate evidence
            # provider may promote those lanes.
            evidence_provider = getattr(client, "evidence", None)
            if evidence_provider is not None:
                exported = evidence_provider(final)
                provider_export = _normalize_evidence_export(
                    exported,
                    provider=handle.provider,
                    call_id_sha256=row["call_id_hash"],
                )
                for key in _EVIDENCE_LANES:
                    row["evidence"][key] = provider_export["lanes"][key]["state"]
        except Exception as exc:
            row["error_type"] = type(exc).__name__
            row["terminal_status"] = "ERROR"
        finally:
            row["finished_monotonic"] = round(clock(), 9)
            child = _write_child(
                stage_root,
                row,
                create_receipt,
                terminal_receipt,
                provider_export,
                provider=workload["provider"],
                workload_plan_sha256=workload["normalized_plan_sha256"],
                call_spec_sha256=workload["call_spec_sha256"],
            )
            row["child"] = child
            row["evidence_complete"] = child["evidence_complete"]
            with rows_lock:
                rows.append(row)

    try:
        for stage_index, stage in enumerate(plan["stages"]):
            stage_start = clock()
            if stage["model"] == "closed":
                with concurrent.futures.ThreadPoolExecutor(max_workers=stage["concurrency"], thread_name_prefix="hotato-load") as pool:
                    active: set = set()
                    next_index = 0
                    while next_index < stage["calls"] or active:
                        if stopped() and next_index < stage["calls"]:
                            with rows_lock:
                                while next_index < stage["calls"]:
                                    dropped(stage_index, stage, next_index, clock(), "stop_file")
                                    next_index += 1
                        while next_index < stage["calls"] and len(active) < stage["concurrency"]:
                            active.add(pool.submit(one, stage_index, stage, next_index, clock()))
                            next_index += 1
                        if active:
                            done, active = concurrent.futures.wait(active, return_when=concurrent.futures.FIRST_COMPLETED)
                            for future in done:
                                future.result()
            else:
                interval = 1.0 / stage["arrival_rate_per_second"]
                with concurrent.futures.ThreadPoolExecutor(max_workers=stage["max_in_flight"], thread_name_prefix="hotato-arrival") as pool:
                    active: set = set()
                    for index in range(stage["calls"]):
                        due = stage_start + index * interval
                        delay = due - clock()
                        if delay > 0:
                            sleeper(delay)
                        done = {future for future in active if future.done()}
                        active -= done
                        for future in done:
                            future.result()
                        with rows_lock:
                            if stopped():
                                dropped(stage_index, stage, index, due, "stop_file")
                            elif len(active) >= stage["max_in_flight"]:
                                dropped(stage_index, stage, index, due, "generator_saturated")
                            else:
                                active.add(pool.submit(one, stage_index, stage, index, due))
                    for future in concurrent.futures.as_completed(active):
                        future.result()

        rows.sort(key=lambda row: (row["stage_index"], row["index"]))
        stage_rows = []
        for index, stage in enumerate(plan["stages"]):
            stage_rows.append({"name": stage["name"], "phase": stage["phase"], "model": stage["model"], "metrics": _metrics([row for row in rows if row["stage_index"] == index])})
        metrics = _metrics(rows)
        recovery = _recovery(rows, plan["faults"])
        slos = _evaluate_slos(metrics, recovery, plan["slos"])
        status, exit_code = _conclusion(slos)
        summary: Dict[str, Any] = {
            "schema": "hotato.load-result.v2", "plan_id": plan["id"],
            "workload_plan_sha256": workload["normalized_plan_sha256"],
            "call_spec_sha256": workload["call_spec_sha256"],
            "workload_models": sorted({stage["model"] for stage in plan["stages"]}),
            "automatic_create_retries": 0, "provider_completion_is_quality_pass": False,
            "metrics": metrics, "stages": stage_rows, "recovery": recovery,
            "safety": verification_plan["safety"], "slos": slos, "status": status,
            "exit_code": exit_code,
        }
        observations = b"".join(_canonical(row) for row in rows)
        verification_plan_bytes = _canonical(verification_plan)
        with open(os.path.join(stage_root, "verification-plan.json"), "wb") as handle:  # open-ok: private staging
            handle.write(verification_plan_bytes)
        with open(os.path.join(stage_root, "observations.jsonl"), "wb") as handle:  # open-ok: private staging
            handle.write(observations)
        report = _html(summary)
        with open(os.path.join(stage_root, "report.html"), "wb") as handle:  # open-ok: private staging
            handle.write(report)
        summary["artifacts"] = {
            "observations_sha256": _sha(observations),
            "report_sha256": _sha(report),
            "verification_plan_sha256": _sha(verification_plan_bytes),
        }
        summary["result_id"] = _sha(_canonical(summary))
        with open(os.path.join(stage_root, "summary.json"), "wb") as handle:  # open-ok: private staging
            handle.write(_canonical(summary))
        os.replace(stage_root, output_dir)
        verification = verify(output_dir)
        if not verification["ok"]:
            raise RuntimeError("published load result did not verify")
        return LoadResult(output_dir, summary, verification)
    except BaseException:
        shutil.rmtree(stage_root, ignore_errors=True)
        raise


def verify(output_dir: str) -> Dict[str, Any]:
    """Rehash children and recompute aggregate metrics and SLO conclusions."""
    root = os.path.abspath(output_dir)
    if os.path.islink(root) or not os.path.isdir(root):
        return {"ok": False, "result_id": None, "mismatches": ["output-directory:invalid"], "recomputed_metrics": {}}
    try:
        summary = json.loads(
            _read_regular_bytes(
                os.path.join(root, "summary.json"), _MAX_SUMMARY_BYTES
            )
        )
        if not isinstance(summary, dict) or _summary_problem(summary) is not None:
            raise ValueError("summary must be an object")
    except (OSError, ValueError, TypeError, UnicodeDecodeError, json.JSONDecodeError):
        return {
            "ok": False,
            "result_id": None,
            "mismatches": ["summary.json:invalid"],
            "recomputed_metrics": {},
        }
    claimed = summary.get("result_id")
    unsigned = dict(summary)
    unsigned.pop("result_id", None)
    mismatches: List[str] = []
    try:
        expected_result_id = _sha(_canonical(unsigned))
    except (TypeError, ValueError):
        expected_result_id = None
    if claimed != expected_result_id:
        mismatches.append("result_id")
    try:
        verification_plan_bytes = _read_regular_bytes(
            os.path.join(root, "verification-plan.json"), _MAX_SUMMARY_BYTES
        )
        verification_plan = _validate_verification_plan(json.loads(verification_plan_bytes))
    except (OSError, ValueError, TypeError, UnicodeDecodeError, json.JSONDecodeError):
        verification_plan_bytes = b""
        verification_plan = None
        mismatches.append("verification-plan.json:invalid")
    if _sha(verification_plan_bytes) != summary.get("artifacts", {}).get("verification_plan_sha256"):
        mismatches.append("verification-plan.json:sha256")
    try:
        observation_bytes = _read_regular_bytes(
            os.path.join(root, "observations.jsonl"), _MAX_OBSERVATIONS_BYTES
        )
        lines = [line for line in observation_bytes.splitlines() if line]
        if len(lines) > _MAX_CALLS or any(len(line) > _MAX_OBSERVATION_BYTES for line in lines):
            raise ValueError("observation count or row size exceeds verifier bounds")
        rows = [json.loads(line) for line in lines]
    except (OSError, ValueError, TypeError, UnicodeDecodeError, json.JSONDecodeError):
        rows = []
        observation_bytes = b""
        mismatches.append("observations.jsonl:invalid")
    if _sha(observation_bytes) != summary.get("artifacts", {}).get("observations_sha256"):
        mismatches.append("observations.jsonl:sha256")
    try:
        report = _read_regular_bytes(
            os.path.join(root, "report.html"), _MAX_SUMMARY_BYTES
        )
    except (OSError, ValueError):
        report = b""
    if _sha(report) != summary.get("artifacts", {}).get("report_sha256"):
        mismatches.append("report.html:sha256")
    valid_rows = []
    identities = set()
    for row in rows:
        if not isinstance(row, dict):
            mismatches.append("observation:shape")
            continue
        identity = (row.get("stage_index"), row.get("index"))
        if identity in identities:
            mismatches.append(f"row:{identity[0]}:{identity[1]}:duplicate")
            continue
        identities.add(identity)
        if verification_plan is None or not isinstance(identity[0], int) or not isinstance(identity[1], int):
            mismatches.append(f"row:{identity[0]}:{identity[1]}:identity")
            continue
        if not 0 <= identity[0] < len(verification_plan["stages"]):
            mismatches.append(f"row:{identity[0]}:{identity[1]}:stage")
            continue
        stage = verification_plan["stages"][identity[0]]
        if not 0 <= identity[1] < stage["calls"]:
            mismatches.append(f"row:{identity[0]}:{identity[1]}:index")
            continue
        if (
            row.get("schema") != "hotato.load-observation.v2"
            or row.get("stage_name") != stage["name"]
            or row.get("phase") != stage["phase"]
            or row.get("model") != stage["model"]
            or row.get("start_status") not in {"started", "dropped"}
        ):
            mismatches.append(f"row:{identity[0]}:{identity[1]}:contract")
            continue
        problem = _observation_problem(row)
        if problem is not None:
            mismatches.append(f"row:{identity[0]}:{identity[1]}:{problem}")
            continue
        valid_rows.append(row)
        if row.get("start_status") == "started":
            reference = row.get("child")
            if not isinstance(reference, dict):
                mismatches.append(f"row:{row.get('stage_index')}:{row.get('index')}:child")
            else:
                mismatches.extend(
                    _verify_child(root, reference, row, verification_plan["workload"])
                )
        elif row.get("child") is not None:
            mismatches.append(f"row:{row.get('stage_index')}:{row.get('index')}:dropped-child")
    if verification_plan is not None:
        expected_identities = {
            (stage_index, index)
            for stage_index, stage in enumerate(verification_plan["stages"])
            for index in range(stage["calls"])
        }
        for stage_index, index in sorted(expected_identities - identities):
            mismatches.append(f"row:{stage_index}:{index}:missing")
    try:
        recomputed = _metrics(valid_rows)
    except (KeyError, TypeError, ValueError, OverflowError):
        recomputed = {}
        mismatches.append("metrics:invalid-observation")
    if recomputed != summary.get("metrics"):
        mismatches.append("metrics:recompute")
    if verification_plan is not None and recomputed:
        recomputed_stages = []
        for index, stage in enumerate(verification_plan["stages"]):
            recomputed_stages.append({
                "name": stage["name"], "phase": stage["phase"], "model": stage["model"],
                "metrics": _metrics([row for row in valid_rows if row["stage_index"] == index]),
            })
        if recomputed_stages != summary.get("stages"):
            mismatches.append("stages:recompute")
        recovery = _recovery(valid_rows, verification_plan["faults"])
        if recovery != summary.get("recovery"):
            mismatches.append("recovery:recompute")
        recomputed_slos = _evaluate_slos(recomputed, recovery, verification_plan["slos"])
        if recomputed_slos != summary.get("slos"):
            mismatches.append("slos:recompute")
        status, exit_code = _conclusion(recomputed_slos)
        if summary.get("status") != status:
            mismatches.append("status:recompute")
        if summary.get("exit_code") != exit_code:
            mismatches.append("exit_code:recompute")
        if summary.get("plan_id") != verification_plan["plan_id"]:
            mismatches.append("plan_id:binding")
        workload = verification_plan["workload"]
        if summary.get("workload_plan_sha256") != workload[
            "normalized_plan_sha256"
        ]:
            mismatches.append("workload_plan_sha256:binding")
        if summary.get("call_spec_sha256") != workload["call_spec_sha256"]:
            mismatches.append("call_spec_sha256:binding")
        if summary.get("workload_models") != sorted({stage["model"] for stage in verification_plan["stages"]}):
            mismatches.append("workload_models:recompute")
        if summary.get("safety") != verification_plan["safety"]:
            mismatches.append("safety:binding")
        expected_report = _html({
            "plan_id": verification_plan["plan_id"], "stages": recomputed_stages,
            "slos": recomputed_slos,
        })
        if report != expected_report:
            mismatches.append("report.html:recompute")
    if summary.get("provider_completion_is_quality_pass") is not False:
        mismatches.append("provider_completion_is_quality_pass")
    if summary.get("automatic_create_retries") != 0:
        mismatches.append("automatic_create_retries")
    try:
        observed_top = _bounded_directory_names(root, 16)
    except (OSError, ValueError):
        observed_top = set()
        mismatches.append("output-directory:entries")
    expected_top = {"summary.json", "observations.jsonl", "report.html", "verification-plan.json", "calls"}
    for name in sorted(observed_top ^ expected_top):
        mismatches.append(("unexpected:" if name in observed_top else "missing:") + name)
    calls_path = os.path.join(root, "calls")
    expected_children = {
        f"{row['stage_index']:03d}-{row['index']:06d}"
        for row in valid_rows if row["start_status"] == "started"
    }
    try:
        if os.path.islink(calls_path) or not os.path.isdir(calls_path):
            raise OSError("calls is not a regular directory")
        observed_children = _bounded_directory_names(calls_path, _MAX_CALLS + 1)
        for name in sorted(observed_children ^ expected_children):
            mismatches.append(("calls:unexpected:" if name in observed_children else "calls:missing:") + name)
    except OSError:
        mismatches.append("calls:invalid")
    return {"ok": not mismatches, "result_id": claimed, "mismatches": mismatches, "recomputed_metrics": recomputed}
