"""Positioning lockstep: the product's promise line must agree across every surface.

The TOTAL-QA congruence audit (2026-07-13) found the canonical description split
across surfaces. test_version_lockstep guards version NUMBERS; this guards the
POSITIONING COPY so they can never drift apart again.

The invariant (2026-07-24 front-door repositioning): every user-facing surface
leads with the promise "Find what broke in your agent calls. Pin it so it never
ships again." (local call forensics and regression guards for AI agents), and
NONE of them still leads with a retired product definition (turn-taking eval,
flight recorder, conversation QA for voice agents, regression testing for voice
agents, the vague "AI engineering platform", or the 2026-07-22 "local-first
testing and observability" identity). The turn-taking wedge survives as a
capability/example, not as the product's identity.
"""
import pathlib

ROOT = pathlib.Path(__file__).resolve().parent.parent

# (surface path, the text region to check). Each region MUST carry the promise
# and MUST NOT reintroduce a retired product definition.
_SURFACES = [
    ("README.md", 4000),
    ("llms.txt", 2000),
    ("server.json", 1200),
    ("pyproject.toml", 4000),
    ("CITATION.cff", 1200),
    ("src/hotato/__init__.py", 1200),
    ("src/hotato/cli.py", 4200),
]

# The canonical promise line every surface must carry.
_POSITIONING = "find what broke in your agent calls. pin it so it never ships again."

# Retired product DEFINITIONS that must not survive as the lead identity. These
# are exact product-definition phrases, not the individual words -- "voice",
# "conversation", and "turn-taking" all still appear legitimately as the wedge.
_RETIRED = [
    "the open turn-taking eval for voice agents",
    "flight recorder for production voice agents",
    "Offline turn-taking analysis and regression evidence for dual-channel",
    "conversation QA for voice agents",
    "regression testing for voice agents",
    "AI engineering platform",
    "local-first testing and observability for AI agents",
]


def _head(rel, n):
    return (ROOT / rel).read_text(encoding="utf-8")[:n]


def test_every_surface_says_the_promise():
    missing = []
    for rel, _n in _SURFACES:
        full = (ROOT / rel).read_text(encoding="utf-8").lower()
        if _POSITIONING not in full:
            missing.append(rel)
    assert not missing, (
        "these surfaces no longer carry the promise 'Find what broke in your "
        "agent calls. Pin it so it never ships again.' (positioning drift): "
        f"{missing}")


def test_no_surface_leads_with_a_retired_product_definition():
    hits = []
    for rel, _n in _SURFACES:
        full = (ROOT / rel).read_text(encoding="utf-8")
        for phrase in _RETIRED:
            if phrase in full:
                hits.append(f"{rel}: {phrase!r}")
    assert not hits, (
        "a surface still leads with a retired product definition; "
        "reposition it to the promise 'Find what broke in your agent calls. "
        f"Pin it so it never ships again.': {hits}")


def test_the_five_dimensions_are_named_consistently():
    # the canonical dimension vocabulary appears wherever scoring is described
    for rel, n in [("README.md", 4000), ("llms.txt", 2000),
                   ("src/hotato/__init__.py", 1200)]:
        head = _head(rel, n).lower()
        for dim in ("outcome", "policy", "conversation", "speech", "reliability"):
            assert dim in head, f"{rel} omits the '{dim}' dimension from its lead"
