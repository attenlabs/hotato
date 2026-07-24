"""docs/EVIDENCE-CONTRACT.md is the ONE statement of the four-tier evidence
policy; every surface that narrates the policy links to it instead of
restating it. This lockstep keeps the page and its referencing surfaces from
drifting apart: the page must state all four tiers, and each surface that
applies the policy must point at the page.
"""

import pathlib

ROOT = pathlib.Path(__file__).resolve().parent.parent
PAGE = ROOT / "docs" / "EVIDENCE-CONTRACT.md"

# Every surface that narrates the mono/dual-channel evidence split must link
# to the one policy page rather than growing its own divergent copy.
_REFERENCING_SURFACES = [
    "README.md",
    "docs/README.md",
    "docs/AUTOPSY.md",
    "docs/TRUST.md",
    "docs/TRUST-MATRIX.md",
]

# The four tiers, by the phrase that names each one's behavior.
_TIER_MARKERS = [
    "dual-channel audio: deterministic",
    "attributable, with declared authority",
    "symptom detection, with measured confidence",
    "refused, with the remediation",
]


def test_the_page_states_all_four_tiers():
    text = PAGE.read_text(encoding="utf-8")
    for marker in _TIER_MARKERS:
        assert marker in text, f"EVIDENCE-CONTRACT.md lost its tier: {marker!r}"


def test_every_referencing_surface_links_to_the_page():
    missing = []
    for rel in _REFERENCING_SURFACES:
        if "EVIDENCE-CONTRACT.md" not in (ROOT / rel).read_text(encoding="utf-8"):
            missing.append(rel)
    assert not missing, (
        "these surfaces narrate the evidence policy but no longer link to "
        f"docs/EVIDENCE-CONTRACT.md (the one source of truth): {missing}")


def test_the_page_keeps_the_exit_code_and_denominator_facts():
    """The two load-bearing facts the page asserts about the runtime: a
    refusal is exit 2, and only dual-channel calls enter the Voice Stability
    denominator. If either claim leaves the page, the surfaces linking here
    lose the policy they defer to."""
    text = PAGE.read_text(encoding="utf-8")
    assert "exit `2`" in text
    assert "Voice Stability denominator" in text
