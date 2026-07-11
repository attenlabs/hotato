"""Evidence vector: the honest, machine-readable backbone for what an artifact
actually proves.

This module is part of the zero-dependency Evidence Kernel. It never decides a
verdict and never touches audio; it only *classifies the strength* of an
already-produced result and maps that classification to the single public tier
a renderer (card, report, CLI, JSON, site) is allowed to claim.

The design follows one rule (the evidence lattice): the public tier of an
artifact is the WEAKEST tier permitted by any required dimension. Uncertainty
in one dimension can only pull the claim DOWN; no downstream renderer may raise
it. Dimensions are never averaged into a single confidence percentage -- a
minimum over an inspectable vector is more honest than a blended number.

Everything here is additive and stdlib-only. A consumer that predates a
dimension simply does not read it; a missing dimension is treated as its
UNKNOWN state, never as proof.
"""
from __future__ import annotations

from typing import Dict, Iterable, Optional, Tuple

SCHEMA_VERSION = "1"

# --- public tiers (ascending strength) -------------------------------------
# Deliberately NOT called "Level 0-4": the fix ladder already owns "Level N"
# (diagnose=0 .. apply=4). These are a separate, orthogonal axis about EVIDENCE
# strength, not about how far along the fix ladder an action sits.
TIER_NONE = 0        # no usable evidence for a positive claim
TIER_ASSERTED = 1    # operator/envelope-asserted; not machine-verified from audio
TIER_MEASURED = 2    # recomputed from audio, one recording, input clean
TIER_PAIRED = 3      # manifest-bound before/after, both sides recomputed under one pinned policy
TIER_ATTESTED = 4    # paired + signed policy/contract + capture-origin runner-attested

TIER_LABEL = {
    TIER_NONE: "no evidence",
    TIER_ASSERTED: "asserted (not verified from audio)",
    TIER_MEASURED: "measured from audio",
    # PAIRED is a real before/after recompute, but the RECAPTURE origin is only
    # the operator's word -- not a machine-verified fresh capture. Only ATTESTED
    # earns the "fresh-recapture" language.
    TIER_PAIRED: "paired before/after (recapture operator-asserted)",
    TIER_ATTESTED: "attested paired fresh-recapture evidence",
}

# Short, renderer-facing headline per tier. This is the TIER-ONLY default;
# renderers that carry the full vector should call ``headline_for(tier, vector)``
# so a PAIRED result never borrows the fresh-recapture green it did not earn and
# a missing opposite-risk guard is disclosed on the headline itself.
TIER_HEADLINE = {
    TIER_NONE: "NO EVIDENCE",
    TIER_ASSERTED: "ASSERTED (UNVERIFIED)",
    TIER_MEASURED: "MEASURED FROM AUDIO",
    TIER_PAIRED: "PAIRED IMPROVED (RECAPTURE OPERATOR-ASSERTED)",
    TIER_ATTESTED: "PAIRED FRESH-RECAPTURE IMPROVED",
}


# --- dimensions -------------------------------------------------------------
# Each dimension maps a state -> the HIGHEST tier that state is compatible with
# (a cap). The overall tier is the minimum cap across the REQUIRED dimensions.
# A state of None (dimension absent) is treated as the dimension's UNKNOWN cap.
#
# Keep these tables exhaustive and inspectable: they ARE the honesty contract.
_DIMENSIONS: Dict[str, Dict[Optional[str], int]] = {
    "input_health": {
        "clean": TIER_ATTESTED,
        "caution": TIER_MEASURED,       # a caution can never be a green paired proof
        "not_scorable": TIER_NONE,
        None: TIER_MEASURED,
    },
    "channel_mapping": {
        "confirmed": TIER_ATTESTED,
        "inferred": TIER_MEASURED,
        "suspect": TIER_ASSERTED,
        "unknown": TIER_ASSERTED,
        None: TIER_ASSERTED,
    },
    "label_authority": {
        "human": TIER_ATTESTED,
        "suggested": TIER_MEASURED,
        "none": TIER_ASSERTED,
        None: TIER_ASSERTED,
    },
    "audio_identity": {
        "recomputed": TIER_ATTESTED,    # decoded PCM matched disk and was re-scored
        "asserted": TIER_MEASURED,      # distinct audio, but identity not machine-verified
        "missing": TIER_ASSERTED,
        "same_pcm": TIER_NONE,          # before and after are the same samples: refuse
        "mismatch": TIER_NONE,          # stored digest != disk: refuse
        None: TIER_ASSERTED,
    },
    "capture_origin": {
        "runner_attested": TIER_ATTESTED,
        "operator_asserted": TIER_PAIRED,   # a fresh call, but on the operator's word
        "unknown": TIER_MEASURED,
        None: TIER_MEASURED,
    },
    "policy_integrity": {
        "signed": TIER_ATTESTED,
        "repo_pinned": TIER_PAIRED,
        "manifest_pinned": TIER_PAIRED,     # both sides scored under one pinned policy hash
        "unsigned": TIER_MEASURED,
        "changed": TIER_NONE,               # policy differs between sides: refuse
        None: TIER_MEASURED,
    },
    "fixture_set_integrity": {
        "manifest_complete": TIER_ATTESTED,
        "subset": TIER_ASSERTED,            # fixtures dropped: cannot be a paired proof
        "unknown": TIER_ASSERTED,
        None: TIER_ASSERTED,
    },
    "score_integrity": {
        "recomputed": TIER_ATTESTED,        # verdict re-derived from audio at trial time
        "envelope_only": TIER_ASSERTED,     # trusted a stored verdict.passed
        "mismatch": TIER_NONE,              # stored verdict != recomputed: refuse
        None: TIER_ASSERTED,
    },
    "pairing_integrity": {
        "contract_bound": TIER_ATTESTED,
        "id_only": TIER_MEASURED,
        "unpaired": TIER_ASSERTED,
        None: TIER_ASSERTED,
    },
    # Opposite-risk (hold) guard: a yield-directed fix that ships without a
    # previously-passing hold fixture cannot be a clean attested-green -- it may
    # have traded a false-hold for the yield. A hold guard that REGRESSED is an
    # outright refusal (the change broke the opposite risk).
    "opposite_risk_guard": {
        "present_passing": TIER_ATTESTED,   # >=1 hold guard passed before AND after
        "none": TIER_PAIRED,                # no hold guard submitted: qualify, never green-attested
        "regressed": TIER_NONE,             # a passing hold guard now fails: refuse
        None: TIER_PAIRED,
    },
    "deployment_identity": {
        "config_hash_bound": TIER_ATTESTED,
        "operator_asserted": TIER_PAIRED,
        "unknown": TIER_MEASURED,
        None: TIER_MEASURED,
    },
    "experiment_design": {
        "paired": TIER_ATTESTED,
        "canary": TIER_PAIRED,
        "observational": TIER_MEASURED,
        "none": TIER_MEASURED,
        None: TIER_MEASURED,
    },
    # Informational only: absence of independent attestation never pulls the
    # INTERNAL tier below attested; it gates the (future) public leaderboard,
    # not a private workspace proof. Kept in the vector for completeness.
    "external_attestation": {
        "independent": TIER_ATTESTED,
        "self_reported": TIER_ATTESTED,
        "none": TIER_ATTESTED,
        None: TIER_ATTESTED,
    },
}

# The dimensions that gate a paired fresh-fix proof (a `fix trial` improvement
# card). Ordered most-load-bearing first for readable "why capped" output.
REQUIRED_FOR_PAIRED_PROOF: Tuple[str, ...] = (
    "score_integrity",
    "audio_identity",
    "policy_integrity",
    "fixture_set_integrity",
    "input_health",
    "channel_mapping",
    "label_authority",
    "pairing_integrity",
    "capture_origin",
    "opposite_risk_guard",
)

# The dimensions that gate a single measured recording (a `run`/`contract`
# result on ONE call -- no before/after pairing claimed).
REQUIRED_FOR_MEASURED: Tuple[str, ...] = (
    "input_health",
    "channel_mapping",
    "score_integrity",
)


def _cap_for(dimension: str, state: Optional[str]) -> int:
    table = _DIMENSIONS.get(dimension)
    if table is None:
        # Unknown dimension name: contribute nothing (never raises the tier,
        # never crashes a renderer on a future additive dimension).
        return TIER_ATTESTED
    if state in table:
        return table[state]
    # Unrecognized state string -> treat as the dimension's UNKNOWN cap, the
    # conservative default. Never trust an unknown token as strong evidence.
    return table[None]


def evidence_tier(
    vector: Dict[str, Optional[str]],
    required: Iterable[str] = REQUIRED_FOR_PAIRED_PROOF,
) -> int:
    """The single public tier this vector is allowed to claim: the minimum cap
    across the required dimensions. Missing/unknown states pull it down."""
    required = tuple(required)
    if not required:
        return TIER_NONE
    return min(_cap_for(dim, vector.get(dim)) for dim in required)


def limiting_dimensions(
    vector: Dict[str, Optional[str]],
    required: Iterable[str] = REQUIRED_FOR_PAIRED_PROOF,
) -> list:
    """Which required dimensions hold the tier down (cap == overall tier), so a
    renderer/CLI can say *why* an artifact is not green -- most honest UX."""
    required = tuple(required)
    tier = evidence_tier(vector, required)
    out = []
    for dim in required:
        cap = _cap_for(dim, vector.get(dim))
        if cap <= tier:
            out.append({"dimension": dim, "state": vector.get(dim), "cap": cap})
    return out


def headline_for(tier: int, vector: Dict[str, Optional[str]]) -> str:
    """Renderer-facing headline that respects capture ORIGIN and the opposite-
    risk guard, so a paired result never over-claims:

    * Only a runner-attested origin (ATTESTED tier) earns the generic
      "PAIRED FRESH-RECAPTURE IMPROVED" green.
    * A PAIRED tier whose recapture origin is merely operator-asserted says so.
    * A paired improvement with NO hold guard submitted is disclosed on the
      headline itself, never rendered as a clean green.
    """
    origin = vector.get("capture_origin")
    guard = vector.get("opposite_risk_guard")
    if tier >= TIER_ATTESTED:
        head = "PAIRED FRESH-RECAPTURE IMPROVED"
    elif tier >= TIER_PAIRED:
        head = ("PAIRED IMPROVED" if origin == "runner_attested"
                else "PAIRED IMPROVED (RECAPTURE OPERATOR-ASSERTED)")
    elif tier >= TIER_MEASURED:
        return "MEASURED FROM AUDIO"
    elif tier >= TIER_ASSERTED:
        return "ASSERTED (UNVERIFIED)"
    else:
        return "NO EVIDENCE"
    if guard == "none":
        head += " -- NO HOLD GUARD SUBMITTED"
    return head


def classify(
    vector: Dict[str, Optional[str]],
    required: Iterable[str] = REQUIRED_FOR_PAIRED_PROOF,
) -> dict:
    """The full, renderer-ready evidence classification block."""
    required = tuple(required)
    tier = evidence_tier(vector, required)
    return {
        "schema_version": SCHEMA_VERSION,
        "vector": dict(vector),
        "required": list(required),
        "tier": tier,
        "tier_name": TIER_LABEL[tier],
        "headline": headline_for(tier, vector),
        "limited_by": limiting_dimensions(vector, required),
        "allows_positive_paired": tier >= TIER_PAIRED,
        "allows_positive_measured": tier >= TIER_MEASURED,
    }


def one_sentence(classification: dict) -> str:
    """One plain sentence stating what this tier proves and what it does not."""
    tier = classification["tier"]
    if tier >= TIER_ATTESTED:
        return (
            "Both recordings were recomputed from audio under one pinned, "
            "attested policy; this does not prove your change caused the "
            "result or that future calls will pass."
        )
    if tier >= TIER_PAIRED:
        return (
            "Both recordings were recomputed from audio under one pinned "
            "policy; this does not prove the recordings were independently "
            "attested, that your change caused the result, or future behavior."
        )
    if tier >= TIER_MEASURED:
        return (
            "This recording was measured from audio under the stated policy; "
            "it is not a paired before/after fresh-recapture proof."
        )
    if tier >= TIER_ASSERTED:
        return (
            "This is asserted (envelope-only) evidence: it was NOT recomputed "
            "from audio and cannot render as a verified fresh-fix proof."
        )
    return "There is no usable evidence for a positive claim here."


__all__ = [
    "SCHEMA_VERSION",
    "TIER_NONE", "TIER_ASSERTED", "TIER_MEASURED", "TIER_PAIRED", "TIER_ATTESTED",
    "TIER_LABEL", "TIER_HEADLINE",
    "REQUIRED_FOR_PAIRED_PROOF", "REQUIRED_FOR_MEASURED",
    "evidence_tier", "limiting_dimensions", "classify", "one_sentence",
    "headline_for",
]
