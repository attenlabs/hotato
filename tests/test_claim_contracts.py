"""Claim contracts: the honesty backstop that replaces a phrase blacklist with an
evidence-language TABLE (plan Mission "Copy overclaims the lint misses").

Every public claim phrase declares, in ``src/hotato/data/evidence_language.json``,
the machine evidence dimension that must support it and the highest evidence tier it
may assert (``max_tier``). This pins the contract from the renderer's side:

  * For the WEAKEST evidence at each tier, the card renderer never emits a phrase the
    table rates above the tier the evidence vector actually earns -- no renderer
    exceeds its evidence.
  * card.py fails CLOSED (``ClaimContractError``) if a headline would over-claim.
  * The trust recommendation strings are the governed phrases the table names.
  * "safe to scan" no longer appears in the package source (it was renamed to the
    honest "eligible for scan"); the only residue is in forbidden files this lane may
    not edit, which are reported for the main thread.

The card contract is exercised on the flagship over-claim path (a ``hotato verify``
rollup): the most authoritative-looking artifact must never render from the weakest
evidence.
"""

import json
import re
from pathlib import Path

import pytest

from hotato import card as _card
from hotato import evidence as _evidence
from hotato import trust as _trust

ROOT = Path(__file__).resolve().parent.parent
TABLE_PATH = ROOT / "src" / "hotato" / "data" / "evidence_language.json"
CLAIMS = json.loads(TABLE_PATH.read_text(encoding="utf-8"))["claims"]


# --- evidence vectors at each exact tier ------------------------------------
# A full-strength (ATTESTED) vector, then a single dimension knocked down so the
# lattice caps the tier at a chosen level. Each vector is self-checked below.

_STRONG = {
    "score_integrity": "recomputed",
    "audio_identity": "recomputed",
    "policy_integrity": "signed",
    "fixture_set_integrity": "manifest_complete",
    "input_health": "clean",
    "channel_mapping": "confirmed",
    "label_authority": "human",
    "pairing_integrity": "contract_bound",
    "capture_origin": "runner_attested",
    "opposite_risk_guard": "present_passing",
}


def _vec(**overrides):
    v = dict(_STRONG)
    v.update(overrides)
    return v


# The WEAKEST-earning knock-down per target tier: the strongest vector that must
# still NOT earn any phrase above `tier`.
_VEC_AT_TIER = {
    0: _vec(audio_identity="same_pcm"),        # before==after samples -> refuse tier
    1: _vec(score_integrity="envelope_only"),  # trusted a stored verdict -> asserted
    2: _vec(input_health="caution"),           # a caution input caps at measured
    3: _vec(capture_origin="operator_asserted"),  # fresh call on the operator's word
    4: _vec(),                                  # full strength -> attested
}


def _verify_doc(evidence):
    return {
        "tool": "hotato", "kind": "verify", "schema_version": "1",
        "claim": {"supported": True, "statement": "synthetic"},
        "regression_axis": {"now_pass": 2, "used_to_fail": 3},
        "hold_axis": {"hold_guards": 2, "still_pass": 2, "regressed": 0},
        "evidence": evidence,
    }


def _rendered_title(svg: str) -> str:
    m = re.search(r'<title id="card-title">(.*?)</title>', svg)
    assert m, "card SVG has no card-title"
    return m.group(1)


_CARD_CLAIMS = {p: meta for p, meta in CLAIMS.items()
                if str(meta.get("surface", "")).startswith("card")}


# --- the vectors hit the exact tiers they claim to -------------------------

@pytest.mark.parametrize("tier", sorted(_VEC_AT_TIER))
def test_knockdown_vectors_hit_their_intended_tier(tier):
    vec = _VEC_AT_TIER[tier]
    assert _evidence.evidence_tier(vec, _evidence.REQUIRED_FOR_PAIRED_PROOF) == tier


# --- core contract: no renderer exceeds the evidence ------------------------

@pytest.mark.parametrize("tier", sorted(_VEC_AT_TIER))
def test_card_renderer_never_exceeds_evidence(tier):
    """For the weakest evidence at each tier, the verify card renders no card claim
    phrase the table rates ABOVE that tier, and the rendered headline itself is within
    the evidence the vector earned."""
    vec = _VEC_AT_TIER[tier]
    classification = _evidence.classify(vec, _evidence.REQUIRED_FOR_PAIRED_PROOF)
    cls_tier = classification["tier"]
    assert cls_tier == tier

    svg = _card._render_verify(_verify_doc(classification))

    # No governed CARD claim phrase above the earned tier may appear anywhere.
    for phrase, meta in _CARD_CLAIMS.items():
        if meta["max_tier"] > cls_tier:
            assert phrase not in svg, (
                f"tier-{cls_tier} evidence rendered the over-tier claim {phrase!r} "
                f"(max_tier {meta['max_tier']})")

    # The rendered headline is a governed phrase and does not exceed the evidence.
    headline = _rendered_title(svg)
    max_tier = _card._claim_max_tier(headline)
    assert max_tier is not None, f"headline {headline!r} is not in the contract"
    assert max_tier <= cls_tier, (
        f"headline {headline!r} asserts tier {max_tier} above evidence tier "
        f"{cls_tier}")


def test_weakest_vector_that_should_not_earn_each_phrase(tier=None):
    """For EACH card claim phrase, the weakest evidence that should NOT earn it (one
    tier below its max_tier) renders a card that does not contain the phrase."""
    for phrase, meta in _CARD_CLAIMS.items():
        mt = meta["max_tier"]
        if mt == 0:
            continue  # nothing is weaker than NO EVIDENCE
        vec = _VEC_AT_TIER[mt - 1]
        cls = _evidence.classify(vec, _evidence.REQUIRED_FOR_PAIRED_PROOF)
        svg = _card._render_verify(_verify_doc(cls))
        assert phrase not in svg, (
            f"phrase {phrase!r} (max_tier {mt}) was rendered from tier-{mt - 1} "
            "evidence that must not earn it")


# --- the legitimate strong claim DOES render (contract is not vacuous) ------

def test_attested_evidence_still_earns_the_green_claim():
    cls = _evidence.classify(_VEC_AT_TIER[4], _evidence.REQUIRED_FOR_PAIRED_PROOF)
    assert cls["tier"] == _evidence.TIER_ATTESTED
    svg = _card._render_verify(_verify_doc(cls))
    assert "PAIRED FRESH-RECAPTURE" in svg
    assert _card._C["green"] in svg


# --- card.py fails CLOSED on an over-claim ----------------------------------

def test_over_claim_headline_is_refused():
    # A headline stronger than the evidence must raise, never ship.
    with pytest.raises(_card.ClaimContractError):
        _card._assert_claim_within_evidence(
            "PAIRED FRESH-RECAPTURE IMPROVED", _evidence.TIER_MEASURED)


def test_unknown_claim_phrase_is_refused():
    with pytest.raises(_card.ClaimContractError):
        _card._assert_claim_within_evidence(
            "TOTALLY VERIFIED SUPER PROOF", _evidence.TIER_ATTESTED)


def test_headline_within_evidence_passes_for_canonical_phrases():
    # Every canonical card phrase validates at exactly its own tier.
    for phrase, meta in _CARD_CLAIMS.items():
        _card._assert_claim_within_evidence(phrase, meta["max_tier"])


# --- the trust recommendation strings are the governed phrases --------------

def test_trust_recommendation_strings_are_governed_claims():
    assert _trust.SAFE_RECOMMENDATION == "eligible for scan"
    assert _trust.CAUTION_RECOMMENDATION == "scan with caution"
    assert _trust.SAFE_RECOMMENDATION in CLAIMS
    assert _trust.CAUTION_RECOMMENDATION in CLAIMS
    # "eligible" (clean input) may back a stronger downstream tier than a caution.
    assert (CLAIMS[_trust.SAFE_RECOMMENDATION]["max_tier"]
            > CLAIMS[_trust.CAUTION_RECOMMENDATION]["max_tier"])


# --- "safe to scan" is gone from the package source -------------------------

def test_safe_to_scan_absent_from_package_source():
    """The overclaiming "safe to scan" recommendation was renamed to "eligible for
    scan". It must not appear in any source file this lane owns or may edit. The only
    permitted residue is in forbidden files (cli.py) this lane must not touch; those
    are reported separately to the main thread, so they are allow-listed here rather
    than silently ignored."""
    src = ROOT / "src" / "hotato"
    # Forbidden files this lane may not edit; occurrences reported for the main thread.
    forbidden = {"cli.py"}
    unexpected = {}
    for p in src.rglob("*.py"):
        text = p.read_text(encoding="utf-8")
        if "safe to scan" in text:
            rel = p.relative_to(src).as_posix()
            if p.name not in forbidden:
                lines = [i for i, ln in enumerate(text.splitlines(), 1)
                         if "safe to scan" in ln]
                unexpected[rel] = lines
    assert not unexpected, (
        "\"safe to scan\" leaked into non-forbidden source: " + str(unexpected))
