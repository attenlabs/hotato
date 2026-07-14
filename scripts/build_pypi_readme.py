#!/usr/bin/env python3
"""Build README.pypi.md: the PyPI long-description variant of README.md.

PyPI renders the long description but resolves relative links against
https://pypi.org/project/hotato/, so every repo-relative link in README.md
(docs/, corpus/, examples/, LICENSE, ...) 404s there. This rewrites each
repo-relative Markdown link and image target to an absolute URL on GitHub so
the PyPI page's links resolve, while README.md itself keeps idiomatic relative
links that work in-tree on GitHub.

Deterministic and stdlib-only: the same README.md always produces the same
bytes (asserted by tests/test_pypi_readme.py, which rebuilds to a temp path and
diffs against the committed README.pypi.md). Run with --check to verify the
committed file is current without rewriting it.

Usage:
    python scripts/build_pypi_readme.py            # write README.pypi.md
    python scripts/build_pypi_readme.py --check     # exit 1 if stale
"""
import os
import re
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(HERE)
SRC = os.path.join(REPO, "README.md")
OUT = os.path.join(REPO, "README.pypi.md")

RAW_BASE = "https://raw.githubusercontent.com/attenlabs/hotato/main/"
BLOB_BASE = "https://github.com/attenlabs/hotato/blob/main/"
# GitHub renders these inline via /blob/; raw bytes are better for these.
RAW_EXT = (".txt", ".sh", ".png", ".jpg", ".jpeg", ".gif", ".svg")

def _strip_dot_slash(t):
    return t[2:] if t.startswith('./') else t


_LINK = re.compile(r'(!?\[[^\]]*\])\(([^)]+)\)')


def _is_repo_relative(target: str) -> bool:
    t = target.strip()
    if t.startswith(("http://", "https://", "mailto:", "#", "//", "data:")):
        return False
    return True


def _absolutize(target: str) -> str:
    # split off an anchor / query so it is preserved
    frag = ""
    for sep in ("#", "?"):
        if sep in target:
            i = target.index(sep)
            frag = target[i:] + frag if sep == "#" else target[i:]
            target = target[:i]
    path = _strip_dot_slash(target)
    base = RAW_BASE if path.lower().endswith(RAW_EXT) else BLOB_BASE
    return base + path + frag


def rewrite(text: str) -> str:
    def repl(m):
        label, target = m.group(1), m.group(2)
        if not _is_repo_relative(target):
            return m.group(0)
        # images (![...]) must use raw bytes to render on PyPI
        if label.startswith("!"):
            path = target.strip().lstrip("./")
            return f"{label}({RAW_BASE}{path})"
        return f"{label}({_absolutize(target)})"
    text = _LINK.sub(repl, text)

    # HTML attributes too (badges are already absolute; a relative <img src>/
    # <a href> — e.g. the banner SVG — would 404 on PyPI).
    def _attr(m):
        pre, target, post = m.group(1), m.group(2), m.group(3)
        if not _is_repo_relative(target):
            return m.group(0)
        base = RAW_BASE if target.lower().endswith(RAW_EXT) else BLOB_BASE
        return pre + base + _strip_dot_slash(target) + post
    text = re.sub(r'(<(?:img|a|source)\b[^>]*?\b(?:src|href|srcset)=")([^"]+)(")', _attr, text)
    return text


def build() -> str:
    with open(SRC, encoding="utf-8") as fh:
        return rewrite(fh.read())


def main() -> int:
    built = build()
    if "--check" in sys.argv:
        try:
            with open(OUT, encoding="utf-8") as fh:
                current = fh.read()
        except FileNotFoundError:
            current = None
        if current != built:
            sys.stderr.write(
                "README.pypi.md is stale -- run: python scripts/build_pypi_readme.py\n")
            return 1
        return 0
    with open(OUT, "w", encoding="utf-8") as fh:
        fh.write(built)
    print(f"wrote {OUT}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
