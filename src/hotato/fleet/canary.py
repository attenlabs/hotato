"""Approval-gated canary + tested rollback for Fleet.

A controlled fixture trial and a canary on real traffic are SEPARATE evidence
classes: real traffic adds uncontrolled caller/network/carrier/device variation
(plan §10). This module evaluates an explicit approval policy against a trial
result, produces a canary plan (bounded traffic %, expiry, auto-rollback
trigger) WITHOUT routing any traffic itself, and performs a rollback through the
adapter with a durable receipt. Full automatic deployment stays disabled: this
release recommends and, at most, prepares an approval request.
"""
from __future__ import annotations

import hashlib
import json
from typing import List, Optional

from .. import evidence as _evidence


def approval_policy(*, agent_id: str, parameter_family: str,
                    within_documented_bounds: bool = True,
                    require_full_battery: bool = True,
                    require_all_high_stakes: bool = True,
                    min_canary_calls: int = 50,
                    forbid_input_health_degradation: bool = True,
                    auto_rollback_trigger: str = "any high-stakes regression",
                    max_traffic_pct: float = 5.0,
                    expires_at: Optional[str] = None) -> dict:
    """An explicit, inspectable approval policy. Every field is a gate a variant
    must clear before it may be routed any real traffic."""
    return {
        "schema_version": "1",
        "agent_id": agent_id,
        "parameter_family": parameter_family,
        "within_documented_bounds": within_documented_bounds,
        "require_full_battery": require_full_battery,
        "require_all_high_stakes": require_all_high_stakes,
        "min_canary_calls": min_canary_calls,
        "forbid_input_health_degradation": forbid_input_health_degradation,
        "auto_rollback_trigger": auto_rollback_trigger,
        "max_traffic_pct": max_traffic_pct,
        "expires_at": expires_at,
    }


def evaluate_gate(policy: dict, *, trial_verdict: str, evidence_tier: int,
                  full_battery_ran: bool, high_stakes_all_pass: bool,
                  input_health_degraded: bool, parameter_family: str,
                  within_bounds: bool) -> dict:
    """Decide whether a variant is ELIGIBLE for a canary. Returns
    {eligible, reasons}. A single failed gate makes it ineligible regardless of
    average improvement (plan §9.5 hard gates)."""
    reasons = []
    if trial_verdict != "improved":
        reasons.append(f"trial verdict is {trial_verdict!r}, not 'improved'")
    if evidence_tier < _evidence.TIER_PAIRED:
        reasons.append(f"evidence tier {evidence_tier} below paired ({_evidence.TIER_PAIRED})")
    if parameter_family != policy["parameter_family"]:
        reasons.append(f"parameter family {parameter_family!r} not permitted by policy")
    if policy.get("within_documented_bounds") and not within_bounds:
        reasons.append("change is outside documented bounds")
    if policy.get("require_full_battery") and not full_battery_ran:
        reasons.append("full fresh-recapture battery was not run")
    if policy.get("require_all_high_stakes") and not high_stakes_all_pass:
        reasons.append("not all high-stakes contracts passed")
    if policy.get("forbid_input_health_degradation") and input_health_degraded:
        reasons.append("input health degraded")
    return {"eligible": not reasons, "reasons": reasons}


def canary_plan(policy: dict, *, variant_id: str) -> dict:
    """The bounded observation plan for an ELIGIBLE variant. This is a PLAN, not
    an action: no traffic is routed here (routing requires a connected stack and
    an explicit operator approval token)."""
    return {
        "schema_version": "1",
        "variant_id": variant_id,
        "agent_id": policy["agent_id"],
        "max_traffic_pct": policy["max_traffic_pct"],
        "min_canary_calls": policy["min_canary_calls"],
        "auto_rollback_trigger": policy["auto_rollback_trigger"],
        "expires_at": policy.get("expires_at"),
        "evidence_class": "observational",   # SEPARATE from the fixture trial
        "requires_operator_approval_token": True,
        "routes_traffic": False,
    }


def observe(calls: List[dict]) -> dict:
    """Summarize canary calls as OBSERVATIONAL evidence (never merged into the
    controlled fixture-trial axis)."""
    n = len(calls)
    regressions = [c for c in calls if c.get("high_stakes_regression")]
    return {"schema_version": "1", "evidence_class": "observational",
            "calls": n, "high_stakes_regressions": len(regressions),
            "rollback_recommended": bool(regressions),
            "note": "observational: uncontrolled real-traffic variation; not a controlled proof."}


_RECEIPT_KINDS = ("clone", "canary", "rollback")


def deployment_receipt(kind: str, *, agent_id: str, variant_id: Optional[str] = None,
                       config_hash: Optional[str] = None,
                       prior_revision: Optional[int] = None,
                       detail: Optional[dict] = None) -> dict:
    """Build a canonical, hashable clone/canary/rollback receipt so the FleetAPI
    can persist deployment records uniformly (schema/deployment_receipt.v1.json).

    Pure: it computes a ``receipt_digest`` (sha256 over the canonical JSON of the
    receipt fields, before that key is inserted) but routes NO traffic and reads
    no clock — a caller who wants a timestamp puts it inside ``detail``."""
    if kind not in _RECEIPT_KINDS:
        raise ValueError(f"kind must be one of {_RECEIPT_KINDS}, got {kind!r}")
    body = {"schema_version": "1", "kind": kind, "agent_id": agent_id,
            "variant_id": variant_id, "config_hash": config_hash,
            "prior_revision": prior_revision, "detail": detail}
    body["receipt_digest"] = hashlib.sha256(
        json.dumps(body, sort_keys=True, separators=(",", ":")).encode("utf-8")).hexdigest()
    return body


def rollback(adapter, *, ref, revision, reason: str, actor: str, at: float) -> dict:
    """Restore a prior revision/routing through the adapter and emit a durable,
    hashable rollback receipt."""
    result = adapter.rollback(ref, revision)
    body = {"schema_version": "1", "kind": "rollback_receipt", "ref": ref,
            "restored_revision": revision, "reason": reason, "actor": actor,
            "at": at, "adapter_result": result}
    body["receipt_digest"] = hashlib.sha256(
        json.dumps(body, sort_keys=True, separators=(",", ":")).encode("utf-8")).hexdigest()
    return body


__all__ = ["approval_policy", "evaluate_gate", "canary_plan", "observe",
           "deployment_receipt", "rollback"]
