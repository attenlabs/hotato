"""Optional label-suggestion review ASSISTANT (plan section 12).

A heuristic aid, NEVER a contract authority. It emits ``yield | hold | abstain``
with visible supporting/contradicting observations and abstains on ANY
uncertainty (low signal, unresolved channel mapping, crosstalk, boundary-
sensitive timing, ambiguous shape, unsupported locale). A HUMAN label is always
required to promote a candidate into a contract; this only speeds review. Its
output is not, and must never be presented as, a Hotato scorer accuracy claim.

Zero-dependency (stdlib only). A future ``[label-suggest]`` extra could swap the
heuristic for a calibrated local model behind the SAME abstaining contract.
"""
from __future__ import annotations

from typing import Optional

MODEL_ID = "hotato-heuristic-suggest"
MODEL_HASH = "builtin-v1"
FEATURE_VERSION = "1"
SUPPORTED_LOCALES = ("und", "en", "en-us", "en-gb")


def suggest(measured: dict, *, input_health: Optional[str] = None,
            channel_mapping: Optional[str] = None, locale: str = "und") -> dict:
    """Return a suggestion object for one candidate's measured shape. Abstains on
    any uncertainty; a returned suggestion is advisory only."""
    measured = measured or {}
    comp = measured.get("components") or {}
    reasons = []
    if input_health and input_health not in ("clean", None):
        reasons.append(f"input health is '{input_health}', not clean")
    if channel_mapping in ("suspect", "unknown"):
        reasons.append("caller/agent channel mapping is unresolved")
    if measured.get("boundary_sensitive") or comp.get("boundary_sensitive"):
        reasons.append("timing is boundary-sensitive (within one hop of the policy)")
    if locale and locale.lower() not in SUPPORTED_LOCALES:
        reasons.append(f"locale '{locale}' is not supported by the heuristic")

    overlap = _num(measured.get("overlap_sec")) or _num(comp.get("severity")) or 0.0
    supporting, contradicting = [], []
    suggestion, confidence = "abstain", 0.0
    if not reasons:
        if overlap >= 0.4:
            suggestion, confidence = "yield", 0.55
            supporting.append(f"caller overlaps the agent for {overlap:.2f}s (interruption-like)")
            contradicting.append("timing alone cannot tell a real interruption from a long backchannel")
        elif 0.0 < overlap < 0.2:
            suggestion, confidence = "hold", 0.50
            supporting.append("caller activity is short (backchannel-like)")
            contradicting.append("a short overlap can still be a real, terse interruption")
        else:
            reasons.append("overlap is ambiguous (neither clearly interruption nor backchannel)")
            suggestion, confidence = "abstain", 0.0

    return {
        "suggestion": suggestion,
        "confidence": round(confidence, 3),
        "model_id": MODEL_ID,
        "model_hash": MODEL_HASH,
        "feature_version": FEATURE_VERSION,
        "locale": locale,
        "supporting_observations": supporting,
        "contradicting_observations": contradicting,
        "reason_for_abstention": "; ".join(reasons) or None,
        "authority": ("advisory only: a human label is required to promote a "
                      "contract; this is not a Hotato scorer accuracy claim"),
    }


def _num(x):
    try:
        return float(x)
    except (TypeError, ValueError):
        return None


__all__ = ["suggest", "MODEL_ID", "MODEL_HASH", "FEATURE_VERSION", "SUPPORTED_LOCALES"]
