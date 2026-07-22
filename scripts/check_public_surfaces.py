#!/usr/bin/env python3
"""Release-blocking public-surface reconciliation gate.

A publisher's success signal ("uploaded to PyPI", "Pages deploy green") does NOT
prove the public surfaces actually SERVE the shipped version: two external audits
both reported cache-stale public numbers (a CDN kept serving an older
``llms.txt`` / landing page / package summary). This gate answers, mechanically
and from a clean network, one question: **are the public surfaces reconciled to
the shipped version?** -- so a release is never declared done on a publisher
success signal alone.

Two check groups:

  A. LOCAL CROSS-SOURCE CONSISTENCY (no network, always runs). Every in-repo
     surface that names the version must agree: pyproject ``version`` ==
     ``hotato.__version__`` == the git tag ``vX.Y.Z`` (when HEAD is tagged or the
     tag exists) == the README action pin (``attenlabs/hotato@vX.Y.Z`` and
     ``hotato-version: X.Y.Z``) == the top ``## [X.Y.Z]`` CHANGELOG entry ==
     ``llms.txt``'s ``Version X.Y.Z``. This is the part CI runs with no egress.

  B. PUBLIC SURFACE RECONCILIATION (network; gated behind ``--online`` so a
     no-egress CI job skips it cleanly). PyPI's per-version JSON, the site's
     ``release.json``, the CDN-served ``llms.txt`` and landing page are fetched
     with a crawler UA + ``Cache-Control: no-cache`` and asserted to carry the
     CURRENT version and positioning markers (never the retired overclaim, never
     the stale product-identity ``<title>``).

Usage:
    python3 scripts/check_public_surfaces.py                 # group A only
    python3 scripts/check_public_surfaces.py --online        # A + B
    python3 scripts/check_public_surfaces.py --version 1.2.3 # override the version

Exit 0 = reconciled (or group B cleanly skipped); exit 1 = a mismatch / a public
surface is stale or unreachable (fail closed); exit 2 = usage/env error.
Stdlib-only. Group A does no network; group B uses only ``urllib.request`` and
parses every response JSON in-process (downloaded bytes are never piped anywhere).
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
import urllib.error
import urllib.request
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent

EXIT_OK = 0
EXIT_FAIL = 1
EXIT_ENV = 2

# A crawler UA + no-cache so a CDN cannot serve us a cached (stale) copy: this is
# exactly the surface an external auditor / search crawler sees.
_CRAWLER_UA = (
    "Mozilla/5.0 (compatible; Googlebot/2.1; +http://www.google.com/bot.html)"
)
_NO_CACHE_HEADERS = {
    "User-Agent": _CRAWLER_UA,
    "Cache-Control": "no-cache",
    "Pragma": "no-cache",
}

_PYPI_URL = "https://pypi.org/pypi/hotato/{version}/json"
_RELEASE_JSON_URL = "https://hotato.dev/release.json"
_LLMS_URL = "https://hotato.dev/llms.txt"
_SITE_URL = "https://hotato.dev/"

# Positioning markers the public surfaces MUST carry (the current identity) and
# MUST NOT carry (retired overclaim / stale product identity). Kept in lockstep
# with the 1.15.1 public-copy correction (CHANGELOG) and the operator-set
# positioning "Local-first testing and observability for AI agents".
_RETIRED_OVERCLAIM = "everything you use a hosted platform for"
_REQUIRED_PYPI_SUMMARY = "testing and observability for ai agents"
_REQUIRED_SITE_MARKER = "Local-first testing and observability for AI agents"
_STALE_TITLE_MARKER = "regression testing for voice agents"


# --------------------------------------------------------------------------- #
# Version helpers
# --------------------------------------------------------------------------- #
def _normalize(v: str | None) -> str | None:
    """Strip a leading ``v`` and surrounding whitespace so ``v1.2.3`` and
    ``1.2.3`` compare equal."""
    if v is None:
        return None
    v = v.strip()
    return v[1:] if v.startswith("v") else v


# --------------------------------------------------------------------------- #
# Group A source readers. Each takes the repo root and returns the version it
# reports (normalized, no ``v``) or None if the surface does not name one. They
# read files directly so a test can point them at a fixture repo layout (or
# monkeypatch them) without any network or git.
# --------------------------------------------------------------------------- #
def read_pyproject_version(root: Path) -> str | None:
    text = (root / "pyproject.toml").read_text(encoding="utf-8")
    m = re.search(r'(?m)^version\s*=\s*"([^"]+)"', text)
    return _normalize(m.group(1)) if m else None


def read_init_version(root: Path) -> str | None:
    text = (root / "src" / "hotato" / "__init__.py").read_text(encoding="utf-8")
    m = re.search(r'(?m)^__version__\s*=\s*"([^"]+)"', text)
    return _normalize(m.group(1)) if m else None


def read_readme_action_pin(root: Path) -> str | None:
    """The ``attenlabs/hotato@vX.Y.Z`` action pin in the README's CI snippet."""
    text = (root / "README.md").read_text(encoding="utf-8")
    m = re.search(r"attenlabs/hotato@v(\d+\.\d+\.\d+)", text)
    return _normalize(m.group(1)) if m else None


def read_readme_hotato_version(root: Path) -> str | None:
    """The ``hotato-version: X.Y.Z`` input in the README's CI snippet."""
    text = (root / "README.md").read_text(encoding="utf-8")
    m = re.search(r"(?m)^\s*hotato-version:\s*v?(\d+\.\d+\.\d+)", text)
    return _normalize(m.group(1)) if m else None


def read_changelog_top(root: Path) -> str | None:
    """The first real ``## [X.Y.Z]`` release entry, skipping ``[Unreleased]``."""
    text = (root / "CHANGELOG.md").read_text(encoding="utf-8")
    for m in re.finditer(r"(?m)^##\s*\[([^\]]+)\]", text):
        tag = m.group(1).strip()
        if tag.lower() == "unreleased":
            continue
        vm = re.match(r"v?(\d+\.\d+\.\d+)", tag)
        return _normalize(vm.group(1)) if vm else None
    return None


def read_llms_version(root: Path) -> str | None:
    text = (root / "llms.txt").read_text(encoding="utf-8")
    m = re.search(r"(?m)^\s*>?\s*Version\s+v?(\d+\.\d+\.\d+)", text)
    return _normalize(m.group(1)) if m else None


def read_git_tag(root: Path, version: str) -> str | None:
    """The git tag source.

    Returns the tag at HEAD (normalized, no ``v``) when HEAD is exactly tagged,
    else ``vX.Y.Z`` when that tag exists in the repo, else None (no relevant tag
    -- e.g. a pre-tag release commit, or not a git checkout). None is NOT a
    mismatch: a version can be reconciled before it is tagged.
    """
    exact = subprocess.run(
        ["git", "-C", str(root), "describe", "--tags", "--exact-match", "HEAD"],
        capture_output=True,
    )
    if exact.returncode == 0:
        tag = exact.stdout.decode("utf-8", "replace").strip()
        if tag:
            return _normalize(tag)
    exists = subprocess.run(
        ["git", "-C", str(root), "tag", "-l", f"v{version}"],
        capture_output=True,
    )
    if exists.returncode == 0 and exists.stdout.decode("utf-8", "replace").strip():
        return version
    return None


# Ordered (label, reader) pairs for the file-backed sources. Git is handled
# separately because it needs the expected version to resolve its tag.
_LOCAL_SOURCES = [
    ("pyproject.toml version", read_pyproject_version),
    ("hotato.__version__", read_init_version),
    ("README action pin (attenlabs/hotato@vX.Y.Z)", read_readme_action_pin),
    ("README hotato-version input", read_readme_hotato_version),
    ("CHANGELOG top entry", read_changelog_top),
    ("llms.txt Version", read_llms_version),
]


def collect_local_sources(root: Path, version: str) -> list[tuple[str, str | None]]:
    """Return an ordered list of (source label, reported version) for group A,
    including the git-tag source resolved against ``version``."""
    rows: list[tuple[str, str | None]] = []
    for label, reader in _LOCAL_SOURCES:
        try:
            rows.append((label, reader(root)))
        except FileNotFoundError:
            rows.append((label, None))
    try:
        rows.append(("git tag vX.Y.Z", read_git_tag(root, version)))
    except Exception:  # noqa: BLE001 - git absence must never crash group A
        rows.append(("git tag vX.Y.Z", None))
    return rows


def run_group_a(root: Path, version: str) -> tuple[bool, list[str]]:
    """Assert every in-repo source that names a version equals ``version``.

    A source that reports None (does not name a version, or the git tag does not
    exist yet) is skipped, not failed. Returns (ok, printable table lines).
    """
    rows = collect_local_sources(root, version)
    lines: list[str] = []
    ok = True
    width = max(len(label) for label, _ in rows)
    for label, found in rows:
        if found is None:
            status = "n/a"
        elif found == version:
            status = "OK"
        else:
            status = "MISMATCH"
            ok = False
        shown = found if found is not None else "(not present)"
        lines.append(f"  {label.ljust(width)}  {shown:<12} {status}")
    return ok, lines


# --------------------------------------------------------------------------- #
# Group B fetch + parse helpers. The parse helpers take already-decoded data so
# they are unit-testable on a fixture without any network.
# --------------------------------------------------------------------------- #
class SurfaceError(Exception):
    """A public surface could not be fetched or failed its assertion. Carries a
    single-line human message naming the failing URL; never a traceback."""


def _fetch_bytes(url: str, timeout: float = 15.0) -> bytes:
    req = urllib.request.Request(url, headers=_NO_CACHE_HEADERS)  # noqa: S310 - https only
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:  # noqa: S310
            return resp.read()
    except urllib.error.HTTPError as exc:
        raise SurfaceError(f"{url} -> HTTP {exc.code} {exc.reason}") from exc
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        reason = getattr(exc, "reason", exc)
        raise SurfaceError(f"{url} -> unreachable ({reason})") from exc


def _fetch_json(url: str) -> dict:
    raw = _fetch_bytes(url)
    try:
        doc = json.loads(raw.decode("utf-8"))
    except (ValueError, UnicodeDecodeError) as exc:
        raise SurfaceError(f"{url} -> response was not valid JSON ({exc})") from exc
    if not isinstance(doc, dict):
        raise SurfaceError(f"{url} -> JSON root was not an object")
    return doc


def _fetch_text(url: str) -> str:
    return _fetch_bytes(url).decode("utf-8", "replace")


def check_pypi_doc(doc: dict, version: str) -> list[str]:
    """Assert PyPI's per-version JSON reconciles to ``version``. Returns a list
    of failure strings (empty == OK). Pure; unit-tested on a fixture JSON."""
    problems: list[str] = []
    info = doc.get("info") or {}
    got = info.get("version")
    if got != version:
        problems.append(f"info.version is {got!r}, expected {version!r}")
    urls = doc.get("urls")
    if not urls:
        problems.append("doc.urls is empty (no release files for this version)")
    summary = (info.get("summary") or "")
    low = summary.lower()
    if _RETIRED_OVERCLAIM in low:
        problems.append(
            f"summary still contains the retired overclaim {_RETIRED_OVERCLAIM!r}"
        )
    if _REQUIRED_PYPI_SUMMARY not in low:
        problems.append(
            f"summary is missing the required phrase {_REQUIRED_PYPI_SUMMARY!r}"
        )
    return problems


def check_release_json(doc: dict, version: str) -> list[str]:
    got = doc.get("version")
    if got != version:
        return [f"version is {got!r}, expected {version!r}"]
    return []


def check_llms_text(text: str, version: str) -> list[str]:
    if f"Version {version}" not in text:
        return [f"does not contain {'Version ' + version!r}"]
    return []


def check_site_html(html: str, version: str) -> list[str]:
    """Assert the landing page carries the current positioning marker and does
    NOT use the stale product identity as its ``<title>``. Deliberately does
    NOT fail on an old version string appearing in the page -- a changelog
    legitimately lists past versions; only positioning markers are asserted."""
    problems: list[str] = []
    if _REQUIRED_SITE_MARKER not in html:
        problems.append(f"missing the positioning marker {_REQUIRED_SITE_MARKER!r}")
    # Only a stale <title> is a true product-identity regression. Match the
    # marker specifically inside a <title>...</title>, not anywhere on the page.
    title_m = re.search(r"<title[^>]*>(.*?)</title>", html, re.I | re.S)
    if title_m and _STALE_TITLE_MARKER.lower() in title_m.group(1).lower():
        problems.append(
            f"<title> still reads the stale product identity {_STALE_TITLE_MARKER!r}"
        )
    return problems


def run_group_b(version: str) -> tuple[bool, list[str]]:
    """Fetch and reconcile every public surface. Returns (ok, table lines). A
    network error is a clean failure line naming the URL, never a traceback."""
    checks = [
        ("PyPI /pypi/hotato/{v}/json", _PYPI_URL.format(version=version),
         lambda: check_pypi_doc(_fetch_json(_PYPI_URL.format(version=version)), version)),
        ("hotato.dev/release.json", _RELEASE_JSON_URL,
         lambda: check_release_json(_fetch_json(_RELEASE_JSON_URL), version)),
        ("hotato.dev/llms.txt (crawler UA)", _LLMS_URL,
         lambda: check_llms_text(_fetch_text(_LLMS_URL), version)),
        ("hotato.dev/ (crawler UA)", _SITE_URL,
         lambda: check_site_html(_fetch_text(_SITE_URL), version)),
    ]
    lines: list[str] = []
    ok = True
    width = max(len(label) for label, _, _ in checks)
    for label, url, run in checks:
        try:
            problems = run()
        except SurfaceError as exc:
            ok = False
            lines.append(f"  {label.ljust(width)}  UNREACHABLE  {exc}")
            continue
        if problems:
            ok = False
            lines.append(f"  {label.ljust(width)}  STALE")
            for p in problems:
                lines.append(f"  {' ' * width}    - {p}")
        else:
            lines.append(f"  {label.ljust(width)}  OK           {url}")
    return ok, lines


# --------------------------------------------------------------------------- #
def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        description="Reconcile hotato's public surfaces to the shipped version.",
    )
    ap.add_argument(
        "--version",
        help="version to reconcile to (default: read from pyproject.toml)",
    )
    ap.add_argument(
        "--online",
        action="store_true",
        help="also fetch and reconcile the live public surfaces (network)",
    )
    args = ap.parse_args(argv)

    version = _normalize(args.version)
    if version is None:
        try:
            version = read_pyproject_version(REPO_ROOT)
        except FileNotFoundError:
            print("error: pyproject.toml not found; run inside the repo checkout",
                  file=sys.stderr)
            return EXIT_ENV
        if version is None:
            print("error: could not read a version from pyproject.toml",
                  file=sys.stderr)
            return EXIT_ENV

    print(f"Reconciling public surfaces to version {version}\n")

    print("A. Local cross-source consistency (no network):")
    a_ok, a_lines = run_group_a(REPO_ROOT, version)
    for line in a_lines:
        print(line)

    print("\nB. Public surface reconciliation (network):")
    if args.online:
        b_ok, b_lines = run_group_b(version)
        for line in b_lines:
            print(line)
    else:
        b_ok = True
        print("  SKIPPED (pass --online to fetch the live surfaces)")

    print()
    if a_ok and b_ok:
        print(f"OK: public surfaces reconciled to {version}.")
        return EXIT_OK
    reason = []
    if not a_ok:
        reason.append("local sources disagree")
    if not b_ok:
        reason.append("a public surface is stale or unreachable")
    print(f"REFUSE: {'; '.join(reason)}. A release is NOT done on a publisher "
          "success signal alone -- reconcile the surfaces above and re-check.",
          file=sys.stderr)
    return EXIT_FAIL


if __name__ == "__main__":
    sys.exit(main())
