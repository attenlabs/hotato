#!/usr/bin/env python3
"""Canonical clean-tree release build: sdist + wheel from an immutable tag.

Every release artifact is built from the COMMITTED tree at a signed/annotated
release tag, never the working tree and never a HEAD that has drifted past the
tag. The script resolves the ``vX.Y.Z`` tag matching the ``pyproject.toml``
version, REFUSES (exit 2) if HEAD is not exactly at that tag object, then
exports ``git archive <tag>`` into a fresh temporary directory and runs the
build there -- so an untracked or gitignored file (a generated corpus render,
``examples/reference-agent/.out/``, a local scratch doc) cannot enter the
artifacts, and neither can a commit made after the tag was cut. The 1.6.1/1.6.2
sdists shipped ~1,300 generated working-tree files because they were built in
place. The tag-faithfulness test in tests/test_release_supply_chain.py holds
the same invariant in CI.

``dist/`` is emptied and re-created before the new artifacts land, so it
contains ONLY the current build and its SHA256SUMS -- a stale sdist/wheel from a
prior version can never linger unlisted by SHA256SUMS.

Determinism: the process umask is forced to 022 so artifact bytes do not
depend on the builder's umask (0644 vs 0664 external attrs), the archive is
extracted with tarfile (which applies git's normalized 0644/0755 modes
directly), and SOURCE_DATE_EPOCH is taken from the tag's commit -- the same
pinning .github/actions/build-python-dist/action.yml applies in CI.

The sdist, wheel, and a SHA256SUMS file land in ./dist.

Usage:
    python3 scripts/build_release.py            # build the vX.Y.Z tag matching pyproject
    python3 scripts/build_release.py --ref TAG  # build an explicit tag/ref
    python3 scripts/build_release.py --allow-drift   # dev build off HEAD (NOT for publishing)
"""

from __future__ import annotations

import argparse
import hashlib
import io
import os
import re
import shutil
import subprocess
import sys
import tarfile
import tempfile
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
DIST = REPO_ROOT / "dist"

# Exit codes (hotato convention): 0 = built, 1 = environment error,
# 2 = refuse (policy: tag missing / HEAD drifted past the tag) or usage.
EXIT_OK = 0
EXIT_ENV = 1
EXIT_REFUSE = 2


def _git(*args: str) -> str:
    res = subprocess.run(
        ["git", "-C", str(REPO_ROOT), *args], capture_output=True, check=True
    )
    return res.stdout.decode("utf-8", "replace").strip()


def _git_commit(ref: str) -> str | None:
    """Resolve ``ref`` to a commit SHA, or None if it does not exist."""
    res = subprocess.run(
        ["git", "-C", str(REPO_ROOT), "rev-parse", "--verify", "--quiet", f"{ref}^{{commit}}"],
        capture_output=True,
    )
    if res.returncode != 0:
        return None
    return res.stdout.decode("utf-8", "replace").strip() or None


def _pyproject_version() -> str:
    text = (REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8")
    try:
        import tomllib

        return tomllib.loads(text)["project"]["version"]
    except Exception:
        m = re.search(r'(?m)^\s*version\s*=\s*["\']([^"\']+)["\']', text)
        if not m:
            raise
        return m.group(1)


def _resolve_build_ref(args: argparse.Namespace) -> tuple[str, str] | int:
    """Return ``(ref, human_label)`` to build from, or an exit code on refusal.

    Default policy: build the ``vX.Y.Z`` tag matching pyproject, and REFUSE if
    HEAD is not exactly at that tag (drift) or the tag does not exist -- an
    operator must not be able to silently cut a "release" from an untagged or
    post-tag commit. ``--ref`` builds an explicit tag/ref; ``--allow-drift``
    permits a non-tag-faithful dev build off HEAD.
    """
    head = _git_commit("HEAD")
    if head is None:
        print("error: not a git checkout; the release build exports `git archive <tag>`",
              file=sys.stderr)
        return EXIT_ENV

    if args.ref:
        commit = _git_commit(args.ref)
        if commit is None:
            print(f"error: ref {args.ref!r} does not resolve to a commit", file=sys.stderr)
            return EXIT_REFUSE
        return args.ref, f"{args.ref} ({commit})"

    version = _pyproject_version()
    tag = f"v{version}"
    tag_commit = _git_commit(tag)

    if tag_commit is None:
        if args.allow_drift:
            print(f"warning: no release tag {tag!r}; building a DEV artifact off HEAD "
                  f"({head[:12]}) -- NOT tag-faithful, do not publish", file=sys.stderr)
            return "HEAD", f"HEAD {head} (untagged dev build, pyproject {version})"
        print(f"refuse: no release tag {tag!r} for pyproject version {version}. "
              f"Create the annotated tag at the release commit (git tag -s {tag}) "
              f"before building, or pass --allow-drift for a non-publishable dev build.",
              file=sys.stderr)
        return EXIT_REFUSE

    if head != tag_commit:
        if args.allow_drift:
            print(f"warning: HEAD ({head[:12]}) is not at tag {tag} ({tag_commit[:12]}); "
                  f"building off HEAD anyway -- NOT tag-faithful, do not publish",
                  file=sys.stderr)
            return "HEAD", f"HEAD {head} (drifted from {tag}, pyproject {version})"
        try:
            distance = _git("rev-list", "--count", f"{tag}..HEAD")
        except subprocess.CalledProcessError:
            distance = "?"
        print(f"refuse: HEAD ({head[:12]}) is {distance} commit(s) past tag {tag} "
              f"({tag_commit[:12]}). A release build must come from the immutable tag "
              f"object. Check out the tag (git checkout {tag}) and re-run, or pass "
              f"--allow-drift for a non-publishable dev build.", file=sys.stderr)
        return EXIT_REFUSE

    # HEAD is exactly at the tag: build from the immutable tag ref.
    return tag, f"{tag} ({tag_commit})"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Build the canonical release sdist + wheel from an immutable tag.")
    parser.add_argument("--ref", metavar="TAG",
                        help="explicit git tag/ref to build from (skips the pyproject "
                             "tag-match + drift check; the ref must exist)")
    parser.add_argument("--allow-drift", action="store_true",
                        help="permit a dev build off HEAD when it is not at the release "
                             "tag; the result is NOT tag-faithful and must not be published")
    args = parser.parse_args(argv)

    # Resolve (and enforce the tag-lock policy) BEFORE touching the build
    # toolchain, so a drift/untagged refusal is fast and independent of whether
    # `build` happens to be installed.
    resolved = _resolve_build_ref(args)
    if isinstance(resolved, int):
        return resolved
    ref, label = resolved

    try:
        import build  # noqa: F401  (availability check for `python -m build`)
    except ImportError:
        print("error: the `build` package is required: pip install build", file=sys.stderr)
        return EXIT_ENV

    os.umask(0o022)
    epoch = _git("log", "-1", "--pretty=%ct", ref)

    with tempfile.TemporaryDirectory(prefix="hotato-release-") as tmp:
        src = Path(tmp) / "src"
        src.mkdir()
        archive = subprocess.run(
            ["git", "-C", str(REPO_ROOT), "archive", "--format=tar", ref],
            capture_output=True, check=True,
        )
        with tarfile.open(fileobj=io.BytesIO(archive.stdout)) as tf:
            # "tar" filter (3.12+): path-safety checks, modes kept exactly as
            # git normalized them (0644/0755). Older Pythons extract the same
            # trusted, self-produced archive without the keyword.
            if hasattr(tarfile, "tar_filter"):
                tf.extractall(src, filter="tar")
            else:
                tf.extractall(src)

        out = Path(tmp) / "out"
        env = {**os.environ, "SOURCE_DATE_EPOCH": epoch}
        subprocess.run(
            [sys.executable, "-m", "build", "--no-isolation", "--outdir", str(out)],
            cwd=src, env=env, check=True,
        )

        # Empty dist/ so it holds ONLY this build + its SHA256SUMS -- a stale
        # sdist/wheel from a prior version must never linger unlisted.
        shutil.rmtree(DIST, ignore_errors=True)
        DIST.mkdir(parents=True)
        lines = []
        for artifact in sorted(out.iterdir()):
            dest = DIST / artifact.name
            shutil.copyfile(artifact, dest)
            os.chmod(dest, 0o644)
            digest = hashlib.sha256(dest.read_bytes()).hexdigest()
            lines.append(f"{digest}  {artifact.name}")
        (DIST / "SHA256SUMS").write_text("\n".join(lines) + "\n", encoding="utf-8")

    print(f"built from {label} (SOURCE_DATE_EPOCH={epoch}) into {DIST}")
    print("\n".join(lines))
    return EXIT_OK


if __name__ == "__main__":
    sys.exit(main())
