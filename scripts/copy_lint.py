#!/usr/bin/env python3
"""Package copy-lint: fail when a shipped public-copy surface either (a) contains
an UNQUALIFIED overclaim phrase from the words-to-reserve table, or (b) uses an
evidence-tier CLAIM phrase together with an unqualified escalator that pushes the
claim above the tier that phrase is allowed to assert (the claim-language
contract, ``src/hotato/data/evidence_language.json``). The site already had the
phrase pass; this is the package-side equivalent, now backed by the same
evidence-language table that ``card.py`` renders and validates from, so the
phrasing and its evidence bar have one source of truth.

"Unqualified" mirrors the convention ``tests/test_llms_docs.py`` already uses
for the uvx footgun check: a banned phrase is fine on a line that plainly
marks it as an example of what NOT to say (a "never say X" / "not X" /
"no X yet" caution), and a violation only when it reads as this project's own
unqualified claim.

Note: the trust recommendation string was renamed "safe to scan" -> "eligible
for scan" (an input being scorable is eligibility, not a safety guarantee); both
"eligible for scan" and "scan with caution" are governed claim phrases in the
evidence-language contract.

CHANGELOG.md is scanned ONLY inside its ``[Unreleased]`` section: a released
version's changelog entry is a historical record of what shipped at the time
and is exempt, matching the audit's own PUBLIC-COPY LINT governance note
("Historical changelogs/quotes exempt").

Usage:
    python3 scripts/copy_lint.py           # exit 1 and print every hit
    python3 scripts/copy_lint.py --check   # identical; kept for CLI symmetry
                                            # with scripts/build_llms_full.py
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent

# The exact overclaim phrases this pass removed from the package (README,
# pyproject/server.json/MCP summary, card renderer, docs, CHANGELOG). Matched
# case-insensitively as a plain substring of each line.
BANNED_PHRASES = [
    "verified fix",
    "fix verified",
    "proves the fix",
    "proves a fix",
    "bug cannot come back",
    "every push replays",
    "same on any machine",
    "a red build means the audio changed",
    "every failure points at a concrete fix",
    "private by construction",
    "keep it from coming back",
]

# A line containing a banned phrase is exempt when it plainly negates or
# quotes the phrase as a bad example rather than asserting it. Deliberately
# lexical, not quote-based: most of the scanned files are Python source,
# where a bare quote character is present on nearly every line (it is part
# of the string literal syntax) and would exempt everything.
_QUALIFIER_RE = re.compile(
    r"\bnever\b|\bnot\b|n't\b|\bno\b|\bavoid\b|\binstead of\b|\bcannot\b|"
    r"\bdoes not\b|\bbanned\b|\breserve\b|\boverclaim\b",
    re.IGNORECASE,
)

# Escalator tokens per evidence tier (matching hotato.evidence's 0-4 ladder): the
# marketing-over-claim decorators that assert the strongest tier. A shipped surface
# that uses a claim phrase whose ``max_tier`` is BELOW this and then, in the text not
# already covered by a governed phrase, an unqualified escalator, is dressing a weak
# claim as a verified/fresh-recapture proof -- the audit's flagship over-claim (weak
# evidence rendered as a positive "verified" card). Kept deliberately to the tier-4
# marketing words (not the honest internal descriptors like "runner-attested", which
# appear in explanatory prose): tokens are matched as case-insensitive substrings and
# the "*fix" ones overlap BANNED_PHRASES on purpose, backstopping the phrase list too.
_TIER_ESCALATORS = {
    4: ("fresh-recapture", "fresh recapture", "verified fix", "fix verified",
        "proven fix", "guaranteed fix"),
}


def _load_evidence_language() -> dict:
    """The claim-language contract: phrase -> {machine_field, max_tier, ...}.
    Read from the shipped package data so copy_lint and card.py share one table."""
    path = REPO_ROOT / "src" / "hotato" / "data" / "evidence_language.json"
    return json.loads(path.read_text(encoding="utf-8")).get("claims", {})

# Package source files whose user-facing strings (report/card/refusal text)
# are generated artifacts in the audit's sense, plus the flagship copy
# surfaces (README, pyproject summary, MCP/server descriptions, llms.txt).
_PY_TARGETS = (
    "src/hotato/mcp_server.py",
    "src/hotato/card.py",
    "src/hotato/contract.py",
    "src/hotato/fix_trial.py",
    "src/hotato/verify.py",
    "src/hotato/trust.py",
    "src/hotato/start.py",
)
_FLAT_TARGETS = (
    "README.md",
    "METHODOLOGY.md",
    "llms.txt",
    "pyproject.toml",
    "server.json",
)


def _tracked_top_level_docs() -> list[Path]:
    """Top-level docs/*.md, the same set scripts/build_llms_full.py ships
    into llms-full.txt (git ls-files in a checkout; filesystem glob outside
    one, e.g. an extracted sdist)."""
    try:
        out = subprocess.run(
            ["git", "ls-files", "docs/*.md"],
            cwd=REPO_ROOT, capture_output=True, text=True, check=True,
        )
        names = [line for line in out.stdout.splitlines() if line]
    except (subprocess.CalledProcessError, FileNotFoundError):
        names = [f"docs/{p.name}" for p in (REPO_ROOT / "docs").glob("*.md")]
    return sorted(REPO_ROOT / n for n in names if n.count("/") == 1)


def _unreleased_section(changelog_text: str) -> str:
    m = re.search(r"(?ms)^## \[Unreleased\]\n(.*?)(?=^## \[)", changelog_text)
    return m.group(1) if m else ""


def collect_targets() -> list[tuple[str, str]]:
    """(label, text) pairs for every shipped public-copy surface this lint
    covers. Reads straight off disk; nothing is cached or re-derived."""
    targets: list[tuple[str, str]] = []

    for rel in _FLAT_TARGETS + _PY_TARGETS:
        p = REPO_ROOT / rel
        if p.exists():
            targets.append((rel, p.read_text(encoding="utf-8")))

    for p in _tracked_top_level_docs():
        targets.append(
            (p.relative_to(REPO_ROOT).as_posix(), p.read_text(encoding="utf-8")))

    changelog = REPO_ROOT / "CHANGELOG.md"
    if changelog.exists():
        text = changelog.read_text(encoding="utf-8")
        targets.append(
            ("CHANGELOG.md [Unreleased]", _unreleased_section(text)))

    return targets


# Em / en dashes are banned in prose copy (operator style law 2026-07-15): a
# clean sentence uses a period, comma, colon, or parentheses instead. Matched as
# literal glyphs and as their HTML entities; hyphen-minus in compound words is
# left alone. Applied only to prose surfaces (markdown / toml / txt / json), not
# to Python source, where a stray glyph in a range or comment is out of scope.
_DASH_TOKENS = ("—", "–", "&mdash;", "&ndash;")


def _scan_dashes(label: str, text: str) -> list[str]:
    hits = []
    for lineno, line in enumerate(text.splitlines(), 1):
        for tok in _DASH_TOKENS:
            if tok in line:
                name = {
                    "—": "em dash", "–": "en dash",
                    "&mdash;": "&mdash;", "&ndash;": "&ndash;",
                }[tok]
                hits.append(
                    f"{label}:{lineno}: banned {name} (use a period, comma, "
                    f"colon, or parentheses): {line.strip()}")
    return hits


def _scan_text(label: str, text: str) -> list[str]:
    hits = []
    for lineno, line in enumerate(text.splitlines(), 1):
        low = line.lower()
        for phrase in BANNED_PHRASES:
            if phrase in low and not _QUALIFIER_RE.search(line):
                hits.append(
                    f"{label}:{lineno}: unqualified {phrase!r}: {line.strip()}")
    return hits


def _scan_evidence_claims(label: str, text: str, claims: dict) -> list[str]:
    """Flag a shipped line that uses an evidence-tier CLAIM phrase and then, in the
    text NOT already covered by any governed claim phrase, an unqualified escalator
    for a tier ABOVE the strongest claim the line legitimately makes -- i.e. the
    surface decorates a claim above its ``max_tier``.

    Present canonical claim phrases are masked out first, so a line that renders or
    lists several governed phrases (e.g. a conditional ``"ATTESTED PAIRED" if ... else
    "PAIRED (OPERATOR-ASSERTED)"``, or a docs tier table) is fine: each phrase is
    individually governed. Only an escalator in the LEFTOVER text -- a bare "verified
    fix" / "fresh-recapture" decorating a weaker claim -- trips it. Negated / example
    lines (``_QUALIFIER_RE``) are exempt, exactly like the banned-phrase check."""
    hits = []
    lowered = {phrase.lower(): meta["max_tier"] for phrase, meta in claims.items()}
    for lineno, line in enumerate(text.splitlines(), 1):
        low = line.lower()
        if _QUALIFIER_RE.search(line):
            continue
        present = [(p, t) for p, t in lowered.items() if p in low]
        if not present:
            continue
        ceiling = max(t for _, t in present)
        # Mask every governed claim phrase out of the line; an escalator that is
        # merely a substring of a legitimately-present phrase is thus not counted.
        residual = low
        for phrase, _ in present:
            residual = residual.replace(phrase, " ")
        for tier, tokens in _TIER_ESCALATORS.items():
            if tier <= ceiling:
                continue
            for tok in tokens:
                if tok in residual:
                    hits.append(
                        f"{label}:{lineno}: claim (max tier {ceiling}) escalated "
                        f"by {tok!r} (tier {tier}): {line.strip()}")
    return hits


def run() -> list[str]:
    claims = _load_evidence_language()
    hits: list[str] = []
    for label, text in collect_targets():
        hits.extend(_scan_text(label, text))
        hits.extend(_scan_evidence_claims(label, text, claims))
        if not label.endswith(".py"):
            hits.extend(_scan_dashes(label, text))
    return hits


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument(
        "--check", action="store_true",
        help="identical to the default; kept for naming symmetry with "
             "scripts/build_llms_full.py's --check",
    )
    ap.parse_args(argv)

    hits = run()
    if hits:
        print("copy-lint: unqualified overclaim phrase(s) found:",
              file=sys.stderr)
        for h in hits:
            print(f"  {h}", file=sys.stderr)
        return 1
    print("copy-lint: clean.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
