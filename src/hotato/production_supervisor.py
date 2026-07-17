"""Bounded maintenance loop for the local production evidence plane.

The HTTP gateway commits evidence.  This supervisor performs the separate,
repeatable housekeeping steps that turn ended sessions into finalized evidence,
evaluate persisted alert states, and enforce a declared local retention window.
It never scores a call, invents an assertion, exports a regression candidate, or
sends a notification.  Those remain explicit review and egress boundaries.
"""

from __future__ import annotations

import json
import threading
import time
from dataclasses import dataclass
from typing import Any, Dict, Mapping, Optional, Sequence, Tuple

from .production import (
    EVIDENCE_LANES,
    ProductionStore,
    _read_regular_bytes_no_follow,
)

POLICY_SCHEMA = "hotato.production-maintenance.v1"
_ALERT_CONDITIONS = frozenset({
    "degraded",
    "missing_audio",
    "missing_tool_evidence",
    "incomplete_evidence",
    "conflict",
    "out_of_order",
    "unsequenced",
})


@dataclass(frozen=True)
class MaintenancePolicy:
    interval_seconds: float
    quiescence_seconds: float
    required_lanes: Tuple[str, ...]
    alert_rules: Tuple[Dict[str, str], ...]
    retention_seconds: Optional[float]

    def public(self) -> Dict[str, Any]:
        return {
            "schema": POLICY_SCHEMA,
            "interval_seconds": self.interval_seconds,
            "quiescence_seconds": self.quiescence_seconds,
            "required_lanes": list(self.required_lanes),
            "alert_rules": [dict(rule) for rule in self.alert_rules],
            "retention_seconds": self.retention_seconds,
        }


def _finite_number(
    value: Any, label: str, *, minimum: float, maximum: float
) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{label} must be a finite number")
    result = float(value)
    if result != result or result in (float("inf"), float("-inf")):
        raise ValueError(f"{label} must be a finite number")
    if not minimum <= result <= maximum:
        raise ValueError(f"{label} must be in [{minimum:g}, {maximum:g}]")
    return result


def validate_policy(value: Any) -> MaintenancePolicy:
    if not isinstance(value, Mapping):
        raise ValueError("production maintenance policy must be a mapping")
    allowed = {
        "schema",
        "interval_seconds",
        "quiescence_seconds",
        "required_lanes",
        "alert_rules",
        "retention_seconds",
    }
    unknown = sorted(set(value) - allowed)
    if unknown:
        raise ValueError(
            "production maintenance policy contains unknown field(s): "
            + ", ".join(unknown)
        )
    if value.get("schema") != POLICY_SCHEMA:
        raise ValueError(f"production maintenance schema must be {POLICY_SCHEMA!r}")
    interval = _finite_number(
        value.get("interval_seconds", 30),
        "interval_seconds",
        minimum=0.1,
        maximum=86_400,
    )
    quiescence = _finite_number(
        value.get("quiescence_seconds", 30),
        "quiescence_seconds",
        minimum=0,
        maximum=86_400,
    )
    lanes = value.get("required_lanes", list(EVIDENCE_LANES))
    if (
        not isinstance(lanes, list)
        or not lanes
        or len(set(lanes)) != len(lanes)
        or any(lane not in EVIDENCE_LANES for lane in lanes)
    ):
        raise ValueError(
            "required_lanes must be a non-empty unique subset of evidence lanes"
        )
    raw_rules = value.get("alert_rules", [])
    if not isinstance(raw_rules, list) or len(raw_rules) > 100:
        raise ValueError("alert_rules must be a list of at most 100 rules")
    rules = []
    seen_ids = set()
    for rule in raw_rules:
        if (
            not isinstance(rule, dict)
            or set(rule) != {"id", "condition"}
            or not isinstance(rule["id"], str)
            or not rule["id"]
            or len(rule["id"]) > 200
            or rule["condition"] not in _ALERT_CONDITIONS
            or rule["id"] in seen_ids
        ):
            raise ValueError(
                "each alert rule needs a unique bounded id and supported condition"
            )
        seen_ids.add(rule["id"])
        rules.append({"id": rule["id"], "condition": rule["condition"]})
    retention_value = value.get("retention_seconds")
    retention = (
        None
        if retention_value is None
        else _finite_number(
            retention_value,
            "retention_seconds",
            minimum=0,
            maximum=10 * 365 * 24 * 60 * 60,
        )
    )
    return MaintenancePolicy(
        interval,
        quiescence,
        tuple(lanes),
        tuple(rules),
        retention,
    )


def load_policy(path: str) -> MaintenancePolicy:
    raw = _read_regular_bytes_no_follow(path, max_bytes=1024 * 1024)
    try:
        value = json.loads(raw)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError("production maintenance policy is not valid JSON") from exc
    return validate_policy(value)


class ProductionSupervisor:
    """Run one serialized maintenance cycle at a bounded fixed interval."""

    def __init__(
        self,
        store: ProductionStore,
        policy: MaintenancePolicy,
        *,
        clock=time.time,
        autostart: bool = True,
    ) -> None:
        if not isinstance(policy, MaintenancePolicy):
            raise TypeError("policy must be a MaintenancePolicy")
        self.store = store
        self.policy = policy
        self.clock = clock
        self._stop = threading.Event()
        self._cycle_lock = threading.Lock()
        self._state_lock = threading.Lock()
        self._last: Dict[str, Any] = {
            "schema": "hotato.production-maintenance-status.v1",
            "state": "STARTING" if autostart else "IDLE",
            "cycles": 0,
            "last_started_at": None,
            "last_completed_at": None,
            "last_error": None,
            "last_result": None,
        }
        self.thread = threading.Thread(
            target=self._loop,
            name="hotato-production-maintenance",
            daemon=True,
        )
        if autostart:
            self.thread.start()

    def run_once(self) -> Dict[str, Any]:
        if not self._cycle_lock.acquire(blocking=False):
            raise RuntimeError("a production maintenance cycle is already running")
        started = float(self.clock())
        with self._state_lock:
            self._last["state"] = "RUNNING"
            self._last["last_started_at"] = started
        try:
            finalized = self.store.finalize(
                quiescence_seconds=self.policy.quiescence_seconds,
                required_lanes=self.policy.required_lanes,
            )
            transitions = self.store.evaluate_alerts(self.policy.alert_rules)
            deleted = (
                []
                if self.policy.retention_seconds is None
                else self.store.enforce_retention(
                    retention_seconds=self.policy.retention_seconds
                )
            )
            result = {
                "schema": "hotato.production-maintenance-cycle.v1",
                "started_at": started,
                "completed_at": float(self.clock()),
                "finalized_count": len(finalized),
                "finalized": finalized,
                "alert_transition_count": len(transitions),
                "alert_transitions": transitions,
                "retention_deletion_count": len(deleted),
                "retention_deletions": deleted,
            }
            with self._state_lock:
                self._last.update({
                    "state": "IDLE",
                    "cycles": int(self._last["cycles"]) + 1,
                    "last_completed_at": result["completed_at"],
                    "last_error": None,
                    "last_result": result,
                })
            return result
        except Exception as exc:
            with self._state_lock:
                self._last.update({
                    "state": "ERROR",
                    "cycles": int(self._last["cycles"]) + 1,
                    "last_completed_at": float(self.clock()),
                    "last_error": {
                        "type": type(exc).__name__,
                        "message": str(exc)[:1000],
                    },
                })
            raise
        finally:
            self._cycle_lock.release()

    def status(self) -> Dict[str, Any]:
        with self._state_lock:
            return json.loads(json.dumps(self._last, allow_nan=False))

    def _loop(self) -> None:
        while not self._stop.is_set():
            try:
                self.run_once()
            except Exception:
                # Status retains the bounded error.  The next interval retries
                # because a transient SQLite/disk failure must not kill the
                # maintenance plane silently.
                pass
            self._stop.wait(self.policy.interval_seconds)

    def close(self, timeout: float = 10.0) -> None:
        self._stop.set()
        if self.thread.is_alive():
            self.thread.join(timeout=timeout)
        if self.thread.is_alive():
            raise RuntimeError("production maintenance thread did not stop")
        with self._state_lock:
            if self._last["state"] not in {"ERROR", "RUNNING"}:
                self._last["state"] = "STOPPED"


__all__: Sequence[str] = (
    "MaintenancePolicy",
    "POLICY_SCHEMA",
    "ProductionSupervisor",
    "load_policy",
    "validate_policy",
)
