#!/usr/bin/env python3
"""Canonical clean-tree release build: sdist + wheel from `git archive HEAD`.

Every release artifact is built from the COMMITTED tree, never the working
tree. The script exports `git archive HEAD` into a fresh temporary directory
and runs the build there, so an untracked or gitignored file (a generated
corpus render, `examples/reference-agent/.out/`, a local scratch doc) cannot
enter the artifacts -- the 1.6.1/1.6.2 sdists shipped ~1,300 generated
working-tree files because they were built in place. The tag-faithfulness
test in tests/test_release_supply_chain.py holds the same invariant in CI.

Determinism: the process umask is forced to 022 so artifact bytes do not
depend on the builder's umask (0644 vs 0664 external attrs), the archive is
extracted with tarfile (which applies git's normalized 0644/0755 modes
directly), and SOURCE_DATE_EPOCH is taken from the HEAD commit -- the same
pinning .github/actions/build-python-dist/action.yml applies in CI.

The sdist, wheel, and a SHA256SUMS file land in ./dist.

Usage:
    python3 scripts/build_release.py
"""

from __future__ import annotations

import hashlib
import io
import os
import shutil
import subprocess
import sys
import tarfile
import tempfile
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
DIST = REPO_ROOT / "dist"


def _git(*args: str) -> str:
    res = subprocess.run(
        ["git", "-C", str(REPO_ROOT), *args], capture_output=True, check=True
    )
    return res.stdout.decode("utf-8", "replace").strip()


def main() -> int:
    try:
        import build  # noqa: F401  (availability check for `python -m build`)
    except ImportError:
        print("error: the `build` package is required: pip install build", file=sys.stderr)
        return 1
    try:
        head = _git("rev-parse", "--verify", "HEAD")
    except (subprocess.CalledProcessError, FileNotFoundError):
        print("error: not a git checkout; the release build exports `git archive HEAD`",
              file=sys.stderr)
        return 1

    os.umask(0o022)
    epoch = _git("log", "-1", "--pretty=%ct")

    with tempfile.TemporaryDirectory(prefix="hotato-release-") as tmp:
        src = Path(tmp) / "src"
        src.mkdir()
        archive = subprocess.run(
            ["git", "-C", str(REPO_ROOT), "archive", "--format=tar", "HEAD"],
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

        DIST.mkdir(exist_ok=True)
        lines = []
        for artifact in sorted(out.iterdir()):
            dest = DIST / artifact.name
            shutil.copyfile(artifact, dest)
            os.chmod(dest, 0o644)
            digest = hashlib.sha256(dest.read_bytes()).hexdigest()
            lines.append(f"{digest}  {artifact.name}")
        (DIST / "SHA256SUMS").write_text("\n".join(lines) + "\n", encoding="utf-8")

    print(f"built from {head} (SOURCE_DATE_EPOCH={epoch}) into {DIST}")
    print("\n".join(lines))
    return 0


if __name__ == "__main__":
    sys.exit(main())
