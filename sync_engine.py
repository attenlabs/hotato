#!/usr/bin/env python3
"""Single-source the scoring engine, and guard against drift.

The scorer that produces every number this tool reports is the MIT
``barge_scoring`` engine. To keep the reproducibility promise honest, the copy
vendored inside this package (``src/hotato/_engine``) must be
*byte-identical* to its upstream source -- if the two ever diverge, published
numbers silently disagree and the whole open-vs-closed trust argument dies.

This script has two modes:

    python sync_engine.py           # copy upstream -> vendored (_engine)
    python sync_engine.py --check   # fail (exit 1) if they differ  [CI drift gate]

The upstream source is resolved from, in order:
  1. $HOTATO_ENGINE_SRC  (a path to the upstream ``barge_scoring`` dir)
  2. the fleet checkout at ~/pmf-program/wave1/barge-in-bench/openrepo/barge_scoring

When no upstream source is present (e.g. a fresh public clone where the vendored
copy is itself canonical), ``--check`` skips cleanly with exit 0: there is
nothing to drift against.
"""

from __future__ import annotations

import os
import sys

# The full engine surface. Every one of these is kept byte-identical to upstream.
ENGINE_FILES = ["__init__.py", "audio.py", "vad.py", "score.py", "batch.py", "__main__.py"]

_HERE = os.path.dirname(os.path.abspath(__file__))
VENDORED_DIR = os.path.join(_HERE, "src", "hotato", "_engine")

_FLEET_DEFAULT = os.path.expanduser(
    "~/pmf-program/wave1/barge-in-bench/openrepo/barge_scoring"
)


def resolve_source() -> str | None:
    """Return the upstream ``barge_scoring`` dir, or None if not available here."""
    env = os.environ.get("HOTATO_ENGINE_SRC")
    for cand in (env, _FLEET_DEFAULT):
        if cand and os.path.isdir(cand):
            return cand
    return None


def _read(path: str) -> bytes:
    with open(path, "rb") as fh:
        return fh.read()


def check(source: str) -> list[str]:
    """Return the list of engine files that differ between source and vendored."""
    drift = []
    for name in ENGINE_FILES:
        src = os.path.join(source, name)
        dst = os.path.join(VENDORED_DIR, name)
        if not os.path.exists(src):
            drift.append(f"{name} (missing upstream)")
            continue
        if not os.path.exists(dst) or _read(src) != _read(dst):
            drift.append(name)
    return drift


def sync(source: str) -> list[str]:
    """Copy every engine file from source into the vendored dir. Return changed."""
    os.makedirs(VENDORED_DIR, exist_ok=True)
    changed = []
    for name in ENGINE_FILES:
        src = os.path.join(source, name)
        if not os.path.exists(src):
            raise SystemExit(f"upstream is missing {name}: {src}")
        dst = os.path.join(VENDORED_DIR, name)
        data = _read(src)
        if not os.path.exists(dst) or _read(dst) != data:
            with open(dst, "wb") as fh:
                fh.write(data)
            changed.append(name)
    return changed


def main(argv=None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    check_only = "--check" in argv

    source = resolve_source()
    if source is None:
        msg = (
            "no upstream barge_scoring source found "
            "(set HOTATO_ENGINE_SRC); the vendored _engine is canonical."
        )
        if check_only:
            print(f"sync_engine: {msg} Skipping drift check.")
            return 0
        raise SystemExit(f"sync_engine: {msg}")

    if check_only:
        drift = check(source)
        if drift:
            print("sync_engine: DRIFT DETECTED between upstream and vendored _engine:")
            for name in drift:
                print(f"  - {name}")
            print("Run `python sync_engine.py` to re-sync.")
            return 1
        print(f"sync_engine: vendored _engine is byte-identical to {source}")
        return 0

    changed = sync(source)
    if changed:
        print("sync_engine: updated vendored _engine from upstream:")
        for name in changed:
            print(f"  - {name}")
    else:
        print("sync_engine: vendored _engine already up to date.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
