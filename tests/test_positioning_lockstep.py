"""Positioning lockstep: the product's one-liner must agree across every surface.

The TOTAL-QA congruence audit (2026-07-13) found the canonical description split
four ways at one released version -- pyproject/CITATION said "conversation QA"
while README/CLI/__init__/llms.txt/server.json still said "turn-taking eval".
test_version_lockstep guards version NUMBERS; this guards the POSITIONING COPY so
they can never drift apart again. The invariant: every user-facing surface calls
hotato a "conversation QA" / "conversation-QA" system for voice agents, and NONE
of them still leads with the retired narrow "turn-taking eval / flight recorder /
offline turn-taking analysis" framing.
"""
import pathlib

ROOT = pathlib.Path(__file__).resolve().parent.parent

# (surface path, the text region to check). Each region MUST say conversation-QA
# and MUST NOT contain a retired narrow lead-in.
_SURFACES = [
    ("README.md", 4000),
    ("llms.txt", 2000),
    ("server.json", 1200),
    ("pyproject.toml", 4000),
    ("CITATION.cff", 1200),
    ("src/hotato/__init__.py", 1200),
    ("src/hotato/cli.py", 4200),
]

# retired narrow lead-ins that must not survive as the product definition
_RETIRED = [
    "the open turn-taking eval for voice agents",
    "flight recorder for production voice agents",
    "Offline turn-taking analysis and regression evidence for dual-channel",
]


def _head(rel, n):
    return (ROOT / rel).read_text(encoding="utf-8")[:n]


def test_every_surface_says_conversation_qa():
    missing = []
    for rel, _n in _SURFACES:
        full = (ROOT / rel).read_text(encoding="utf-8").lower()
        if "conversation qa" not in full and "conversation-qa" not in full:
            missing.append(rel)
    assert not missing, (
        "these surfaces no longer call hotato a conversation-QA system "
        f"(positioning drift): {missing}")


def test_no_surface_leads_with_the_retired_narrow_framing():
    hits = []
    for rel, _n in _SURFACES:
        full = (ROOT / rel).read_text(encoding="utf-8")
        for phrase in _RETIRED:
            if phrase in full:
                hits.append(f"{rel}: {phrase!r}")
    assert not hits, (
        "a surface still leads with the retired narrow product definition; "
        f"reposition it to conversation QA: {hits}")


def test_the_five_dimensions_are_named_consistently():
    # the canonical dimension vocabulary appears wherever the platform is described
    for rel, n in [("README.md", 4000), ("llms.txt", 2000),
                   ("src/hotato/__init__.py", 1200)]:
        head = _head(rel, n).lower()
        for dim in ("outcome", "policy", "conversation", "speech", "reliability"):
            assert dim in head, f"{rel} omits the '{dim}' dimension from its lead"
