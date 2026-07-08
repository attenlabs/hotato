#!/usr/bin/env python3
"""Build llms-full.txt: README + every docs/*.md + METHODOLOGY.md + the
envelope schema, concatenated with file-boundary headers, for a single-fetch
agent context dump.

Order: the curated sequence in llms.txt's Links section first (README.md,
docs/WHY.md, METHODOLOGY.md, docs/BAD-CALL-TO-CI.md, ...), then any remaining
docs/*.md file on disk that Links does not name, sorted alphabetically for
determinism, then schema/envelope.v1.json last.

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
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
LLMS_TXT = REPO_ROOT / "llms.txt"
DEFAULT_OUT = REPO_ROOT / "llms-full.txt"
SCHEMA_PATH = REPO_ROOT / "src" / "hotato" / "schema" / "envelope.v1.json"

HEADER_RULE = "=" * 80

# Matches a bare relative path to a markdown doc this build cares about:
# README.md, METHODOLOGY.md, or anything under docs/*.md. Deliberately does
# NOT match CONTRIBUTING.md, CITATION.cff, or LICENSE: those are referenced
# from llms.txt's Links section but are not part of the "README + docs/*.md +
# METHODOLOGY.md" content set this file concatenates.
_DOC_REF_RE = re.compile(r"\b(README\.md|METHODOLOGY\.md|docs/[A-Za-z0-9_.\-]+\.md)\b")


def _ordered_doc_paths() -> list[Path]:
    """The file list, in order: llms.txt's Links-section order first, then any
    remaining docs/*.md on disk not named there, alphabetically."""
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

    all_docs_md = sorted(
        f"docs/{p.name}" for p in (REPO_ROOT / "docs").glob("*.md")
    )
    for rel in all_docs_md:
        if rel not in seen:
            seen.append(rel)

    return [REPO_ROOT / rel for rel in seen]


def build() -> str:
    parts: list[str] = []

    for path in _ordered_doc_paths():
        rel = path.relative_to(REPO_ROOT).as_posix()
        text = path.read_text(encoding="utf-8")
        parts.append(f"{HEADER_RULE}\nFILE: {rel}\n{HEADER_RULE}\n\n{text}")

    schema_rel = SCHEMA_PATH.relative_to(REPO_ROOT).as_posix()
    schema_text = SCHEMA_PATH.read_text(encoding="utf-8")
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
