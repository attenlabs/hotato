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
     root-cause check: it catches any future untracked leak,
     because a gitignored file is by definition not tracked.
  2. NO FORBIDDEN PATTERNS (sdist AND wheel): no member path may match an
     internal / secret path pattern, and no member path or text body may name
     an entry of the maintainers' private ban list (scripts/private_banlist.py).
     The wheel is not a git-tree mirror, so this is its primary guard.

Usage:
    python3 scripts/check_dist_no_leak.py            # scan every archive in dist/
    python3 scripts/check_dist_no_leak.py dist/*.tar.gz dist/*.whl
    # pre-publish (release runbook): the internal-name check may not be skipped
    HOTATO_REQUIRE_PRIVATE_BANLIST=1 python3 scripts/check_dist_no_leak.py dist/*.tar.gz dist/*.whl

Exit 0 = clean; exit 1 = a leak was found (fail closed); exit 2 = usage/env
error, including a required private ban list that is absent, empty or malformed.
Stdlib-only, no network, reads archive listings only (never executes a member).
"""

from __future__ import annotations

import glob
import importlib.util
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


# Structural (non-name) secret / internal path patterns that must NEVER ship in
# a public artifact. Matched case-insensitively against the full member path
# (with the top-level ``name-version/`` prefix stripped).
_FORBIDDEN = [
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
]

# Internal names and operator identifiers are not in this repository in any
# form. They live as sha256 digests in the maintainers' private ban list (see
# scripts/private_banlist.py: $HOTATO_PRIVATE_BANLIST, default
# ~/.config/hotato/private-banlist.sha256). A member path or text body is split
# into ``[a-z0-9]+`` tokens (so ``a-b``, ``a_b`` and ``a/b`` all read as the
# pair ``a b``) and refused when a token or a run of 2-3 tokens hashes to an
# ``internal`` entry. Absent file: that check is skipped with a notice;
# HOTATO_REQUIRE_PRIVATE_BANLIST=1 turns an absent or empty file into exit 2.
def _load_banlist_module():
    name = "hotato_private_banlist"
    mod = sys.modules.get(name)
    if mod is None:
        path = Path(__file__).resolve().with_name("private_banlist.py")
        spec = importlib.util.spec_from_file_location(name, path)
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        sys.modules[name] = mod
    return mod


_BANLIST = _load_banlist_module()

# Long base64 runs (embedded audio/images) are opaque data; their random
# letter groups are not names.
_BASE64_RUN = re.compile(r"[A-Za-z0-9+/=]{64,}")
_MAX_TEXT_BYTES = 8 * 1024 * 1024


def _digest_hits(text: str) -> str | None:
    return _BANLIST.first_hit(text, _BANLIST.digests("internal"))


def _scan_text(data: bytes) -> str | None:
    if b"\0" in data[:4096] or len(data) > _MAX_TEXT_BYTES:
        return None
    return _digest_hits(_BASE64_RUN.sub(" ", data.decode("utf-8", "replace")))


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


def _sdist_members(path: str) -> dict[str, bytes]:
    out: dict[str, bytes] = {}
    with tarfile.open(path, "r:gz") as tf:
        for m in tf.getmembers():
            if m.isfile():
                fh = tf.extractfile(m)
                out[m.name] = fh.read() if fh is not None else b""
    return out


def _wheel_members(path: str) -> dict[str, bytes]:
    with zipfile.ZipFile(path) as zf:
        return {n: zf.read(n) for n in zf.namelist() if not n.endswith("/")}


def _scan_forbidden(rel: str) -> str | None:
    hit = _digest_hits(rel)
    if hit is not None:
        return hit
    for pat, why in _FORBIDDEN:
        if pat.search(rel):
            return why
    return None


def _check_archive(path: str, tracked: set[str]) -> list[str]:
    """Return a list of human-readable leak findings for one archive."""
    findings: list[str] = []
    is_sdist = path.endswith((".tar.gz", ".tgz"))
    members = _sdist_members(path) if is_sdist else _wheel_members(path)
    for member, data in members.items():
        rel = _strip_prefix(member)
        # (2) forbidden-pattern check runs on every artifact: the member path
        # and (for text members) its body.
        why = _scan_forbidden(rel) or _scan_text(data)
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

    try:
        names_on = _BANLIST.digests("internal") is not None
    except _BANLIST.BanlistUnavailable as exc:
        print(f"error: {exc}", file=sys.stderr)
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
    print(f"\nOK: {len(archives)} archive(s) scanned, no leaked files "
          f"(internal-name check: {'on' if names_on else 'SKIPPED'}).")
    return EXIT_OK


if __name__ == "__main__":
    sys.exit(main())
