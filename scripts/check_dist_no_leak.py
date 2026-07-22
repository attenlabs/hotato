#!/usr/bin/env python3
"""Fail-closed pre-publish leak scan of the built distribution artifacts.

``.gitignore`` keeps a file out of git, NOT out of a built sdist or wheel:
setuptools assembles the sdist from ``MANIFEST.in`` directives against the
WORKING TREE, independent of git, so a gitignored internal / crown-jewel /
secret file sitting on disk can still ship to PyPI. This gate is the third
independent layer (after the committed ``.gitignore`` and the ``MANIFEST.in``
excludes): it inspects the ALREADY-BUILT artifacts in ``dist/`` and refuses,
fail-closed, if anything that should not ship is inside them.

Two checks:

  1. TRACKED-ONLY (sdist): every member must be a git-tracked repository file
     or an allowlisted build-generated file (``PKG-INFO``, ``*.egg-info/*``,
     ``setup.cfg``). Any other member is an untracked leak. This is the
     root-cause check: it catches the SAA class and any future untracked leak,
     because a gitignored file is by definition not tracked.
  2. NO FORBIDDEN PATTERNS (sdist AND wheel): no member path may match an
     internal / crown-jewel / secret pattern. The wheel is not a git-tree
     mirror, so the pattern check is its primary guard.

Usage:
    python3 scripts/check_dist_no_leak.py            # scan every archive in dist/
    python3 scripts/check_dist_no_leak.py dist/*.tar.gz dist/*.whl

Exit 0 = clean; exit 1 = a leak was found (fail closed); exit 2 = usage/env error.
Stdlib-only, no network, reads archive listings only (never executes a member).
"""

from __future__ import annotations

import glob
import os
import re
import subprocess
import sys
import tarfile
import zipfile
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent

EXIT_OK = 0
EXIT_LEAK = 1
EXIT_ENV = 2

# Build-generated files that legitimately appear in an sdist but are not git
# tracked. Everything else in an sdist must be a tracked repo file.
_ALLOWLIST_SUFFIXES = ("/PKG-INFO", "/setup.cfg")
_ALLOWLIST_EXACT = ("PKG-INFO", "setup.cfg")


def _is_allowlisted_generated(rel: str) -> bool:
    if rel in _ALLOWLIST_EXACT or rel.endswith(_ALLOWLIST_SUFFIXES):
        return True
    # setuptools writes the *.egg-info/ metadata directory into the sdist.
    return ".egg-info/" in rel or rel.endswith(".egg-info")


# Internal / crown-jewel / secret path patterns that must NEVER ship in a public
# artifact. Matched case-insensitively against the full member path (with the
# top-level ``name-version/`` prefix stripped). Keep in lockstep with the
# fleet packaging-leak LAW (2026-07-22).
_FORBIDDEN = [
    (re.compile(r"(^|/)saa[-_]", re.I), "SAA crown-jewel artifact"),
    (re.compile(r"test_saa", re.I), "SAA test"),
    (re.compile(r"(^|/)HANDOFF", re.I), "internal handoff note"),
    # A real env secret (.env, .env.local, .env.production) but NOT a public
    # template (.env.example / .sample / .template / .dist / .schema).
    (re.compile(r"(^|/)\.env(\.(?!example|sample|template|dist|schema)|$)", re.I),
     "environment/secret file"),
    (re.compile(r"credential", re.I), "credentials"),
    (re.compile(r"(^|/)secrets?\.(json|ya?ml|txt|env|ini)$", re.I), "secrets file"),
    (re.compile(r"\.pem$", re.I), "private key/cert (.pem)"),
    (re.compile(r"(^|/)id_rsa", re.I), "ssh private key"),
    (re.compile(r"(^|/)\.claude/", re.I), "agent-harness config"),
    (re.compile(r"north-star", re.I), "internal north-star canon"),
    (re.compile(r"holocron", re.I), "internal brain host"),
    (re.compile(r"\.fleet-lane", re.I), "internal fleet lane"),
]


def _git_tracked() -> set[str]:
    res = subprocess.run(
        ["git", "-C", str(REPO_ROOT), "ls-files"], capture_output=True
    )
    if res.returncode != 0:
        print("error: `git ls-files` failed; run the scan inside the repo checkout",
              file=sys.stderr)
        raise SystemExit(EXIT_ENV)
    return {
        line for line in res.stdout.decode("utf-8", "replace").splitlines() if line
    }


def _strip_prefix(member: str) -> str:
    """Drop the leading ``name-version/`` component an sdist/wheel wraps its
    files in, so the remainder is a repo-relative path."""
    parts = member.split("/", 1)
    return parts[1] if len(parts) == 2 else member


def _sdist_members(path: str) -> list[str]:
    with tarfile.open(path, "r:gz") as tf:
        return [m.name for m in tf.getmembers() if m.isfile()]


def _wheel_members(path: str) -> list[str]:
    with zipfile.ZipFile(path) as zf:
        return [n for n in zf.namelist() if not n.endswith("/")]


def _scan_forbidden(rel: str) -> str | None:
    for pat, why in _FORBIDDEN:
        if pat.search(rel):
            return why
    return None


def _check_archive(path: str, tracked: set[str]) -> list[str]:
    """Return a list of human-readable leak findings for one archive."""
    findings: list[str] = []
    is_sdist = path.endswith((".tar.gz", ".tgz"))
    members = _sdist_members(path) if is_sdist else _wheel_members(path)
    for member in members:
        rel = _strip_prefix(member)
        # (2) forbidden-pattern check runs on every artifact.
        why = _scan_forbidden(rel)
        if why is not None:
            findings.append(f"FORBIDDEN [{why}]: {member}")
            continue
        # (1) tracked-only check runs on the sdist (a repo-tree mirror). A wheel
        # is a built package and is not expected to mirror git, so it gets the
        # forbidden-pattern guard only.
        if is_sdist and rel not in tracked and not _is_allowlisted_generated(rel):
            findings.append(f"UNTRACKED (not in git, not build-generated): {member}")
    return findings


def main(argv: list[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    if args:
        archives = args
    else:
        archives = sorted(
            glob.glob(str(REPO_ROOT / "dist" / "*.tar.gz"))
            + glob.glob(str(REPO_ROOT / "dist" / "*.whl"))
        )
    if not archives:
        print("error: no dist archives found (build the sdist/wheel first, e.g. "
              "`python -m build`)", file=sys.stderr)
        return EXIT_ENV

    tracked = _git_tracked()
    total_leaks = 0
    for path in archives:
        if not os.path.exists(path):
            print(f"error: {path} does not exist", file=sys.stderr)
            return EXIT_ENV
        findings = _check_archive(path, tracked)
        name = os.path.basename(path)
        if findings:
            total_leaks += len(findings)
            print(f"LEAK in {name}:")
            for f in findings:
                print(f"  {f}")
        else:
            print(f"clean: {name}")

    if total_leaks:
        print(f"\nREFUSE: {total_leaks} leaked file(s) in the distribution. "
              "A gitignored file is NOT excluded from a built artifact; add a "
              "MANIFEST exclude and re-check. See the 2026-07-22 packaging-leak law.",
              file=sys.stderr)
        return EXIT_LEAK
    print(f"\nOK: {len(archives)} archive(s) scanned, no leaked files.")
    return EXIT_OK


if __name__ == "__main__":
    sys.exit(main())
