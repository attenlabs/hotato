#!/usr/bin/env python3
"""Build llms-full.txt: README + every docs/*.md + METHODOLOGY.md + the
machine-facing schemas, concatenated with file-boundary headers, for a
single-fetch agent context dump.

Order: the curated sequence in llms.txt's Links section first (README.md,
docs/WHY.md, METHODOLOGY.md, docs/BAD-CALL-TO-CI.md, ...), then any remaining
docs/*.md file on disk that Links does not name, sorted alphabetically for
determinism, then the counterexample proof schemas, then envelope.v1.json last.

Stdlib only, deterministic: the same source files always produce the same
bytes (this is asserted by tests/test_llms_docs.py, which rebuilds to a temp
path and diffs against the committed llms-full.txt).

Usage:
    python3 scripts/build_llms_full.py             # writes llms-full.txt
    python3 scripts/build_llms_full.py --check      # verify it is up to date
    python3 scripts/build_llms_full.py --out PATH   # write elsewhere
"""

from __future__ import annotations

import argparse
import re
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
LLMS_TXT = REPO_ROOT / "llms.txt"
DEFAULT_OUT = REPO_ROOT / "llms-full.txt"
SCHEMA_PATHS = (
    REPO_ROOT / "src" / "hotato" / "schema" / "counterexample-oracle.v1.json",
    REPO_ROOT / "src" / "hotato" / "schema" / "reduction-certificate.v1.json",
    REPO_ROOT / "src" / "hotato" / "schema" / "counterexample.v1.json",
    # Keep the original envelope schema last for stable consumer expectations.
    REPO_ROOT / "src" / "hotato" / "schema" / "envelope.v1.json",
)

HEADER_RULE = "=" * 80

# Matches a bare relative path to a markdown doc this build cares about:
# README.md, METHODOLOGY.md, or anything under docs/*.md. Deliberately does
# NOT match CONTRIBUTING.md, CITATION.cff, or LICENSE: those are referenced
# from llms.txt's Links section but are not part of the "README + docs/*.md +
# METHODOLOGY.md" content set this file concatenates.
_DOC_REF_RE = re.compile(r"\b(README\.md|METHODOLOGY\.md|docs/[A-Za-z0-9_.\-]+\.md)\b")


def _tracked_docs_md() -> list[str]:
    """docs/*.md that are actually part of the shipped repo, sorted.

    Inside a git checkout: `git ls-files`, NOT a raw filesystem glob. A
    checkout can carry local-only files (excluded internal artifacts) that
    exist on one machine and not another;
    globbing the filesystem would bake a machine-specific file list into
    llms-full.txt and make the build non-reproducible across checkouts.

    Outside a git checkout (an extracted sdist tree has no .git at all, e.g.
    CI's sdist-guard job): fall back to a plain filesystem glob. There is no
    gitignore concept to consult there, and the directory already reflects
    exactly what MANIFEST.in decided to ship from a clean checkout, so the
    glob is safe.
    """
    try:
        out = subprocess.run(
            ["git", "ls-files", "docs/*.md"],
            cwd=REPO_ROOT, capture_output=True, text=True, check=True,
        )
        tracked = [line for line in out.stdout.splitlines() if line]
    except (subprocess.CalledProcessError, FileNotFoundError):
        tracked = [f"docs/{p.name}" for p in (REPO_ROOT / "docs").glob("*.md")]
    # Top-level docs/*.md only. Nested docs (e.g. docs/releases/ release notes)
    # are changelog-ish meta that would accumulate in the agent corpus forever;
    # excluding them also keeps the git path and the non-recursive glob fallback
    # identical, so llms-full.txt stays reproducible across checkout and sdist.
    return sorted(f for f in tracked if f.count("/") == 1)


def _ordered_doc_paths() -> list[Path]:
    """The file list, in order: llms.txt's Links-section order first, then any
    remaining TRACKED docs/*.md not named there, alphabetically."""
    links_text = LLMS_TXT.read_text(encoding="utf-8")
    # Only look at the Links section onward, so a stray path mentioned earlier
    # in the file (inside a code block, say) cannot change the order.
    marker = "## Links"
    idx = links_text.find(marker)
    section = links_text[idx:] if idx != -1 else links_text

    seen: list[str] = []
    for m in _DOC_REF_RE.finditer(section):
        rel = m.group(1)
        if rel not in seen:
            seen.append(rel)

    for rel in _tracked_docs_md():
        if rel not in seen:
            seen.append(rel)

    return [REPO_ROOT / rel for rel in seen]


def build() -> str:
    parts: list[str] = []

    for path in _ordered_doc_paths():
        rel = path.relative_to(REPO_ROOT).as_posix()
        text = path.read_text(encoding="utf-8")
        parts.append(f"{HEADER_RULE}\nFILE: {rel}\n{HEADER_RULE}\n\n{text}")

    for schema_path in SCHEMA_PATHS:
        schema_rel = schema_path.relative_to(REPO_ROOT).as_posix()
        schema_text = schema_path.read_text(encoding="utf-8")
        parts.append(f"{HEADER_RULE}\nFILE: {schema_rel}\n{HEADER_RULE}\n\n{schema_text}")

    return "\n\n".join(parts).rstrip("\n") + "\n"


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--out", type=Path, default=DEFAULT_OUT, help="output path")
    ap.add_argument(
        "--check",
        action="store_true",
        help="verify --out is already up to date; exit 1 if it would change",
    )
    args = ap.parse_args(argv)

    content = build()

    if args.check:
        current = args.out.read_text(encoding="utf-8") if args.out.exists() else None
        if current != content:
            print(f"{args.out} is stale; run scripts/build_llms_full.py", file=sys.stderr)
            return 1
        print(f"{args.out} is up to date.")
        return 0

    args.out.write_text(content, encoding="utf-8")
    print(f"wrote {args.out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
