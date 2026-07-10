#!/usr/bin/env python3
"""Package copy-lint: fail when a shipped public-copy surface contains an
UNQUALIFIED overclaim phrase from the words-to-reserve table (GPT design
audit, ``hotato-launch/GPT-DESIGN-AUDIT-2026-07-10.md`` P0.1 and the
words-to-reserve section). The site already had this pass; this is the
package-side equivalent so 0.9.0 does not ship claim language the site was
just corrected to drop.

"Unqualified" mirrors the convention ``tests/test_llms_docs.py`` already uses
for the uvx footgun check: a banned phrase is fine on a line that plainly
marks it as an example of what NOT to say (a "never say X" / "not X" /
"no X yet" caution), and a violation only when it reads as this project's own
unqualified claim.

Deliberately NOT banned here: "safe to scan" -- the literal, accurate
``trust.recommendation`` machine string, unchanged this pass. Renaming that
field is a larger, separate fast-follow (GPT design audit P0.6), not a claim
this lint should punish while the code still emits it correctly.

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


def _scan_text(label: str, text: str) -> list[str]:
    hits = []
    for lineno, line in enumerate(text.splitlines(), 1):
        low = line.lower()
        for phrase in BANNED_PHRASES:
            if phrase in low and not _QUALIFIER_RE.search(line):
                hits.append(
                    f"{label}:{lineno}: unqualified {phrase!r}: {line.strip()}")
    return hits


def run() -> list[str]:
    hits: list[str] = []
    for label, text in collect_targets():
        hits.extend(_scan_text(label, text))
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
