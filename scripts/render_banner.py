#!/usr/bin/env python3
"""Kept for older instructions: the README banner and the social card are now
rendered by ``scripts/render_brand_art.py`` from outlined type (no browser).

    python3 scripts/render_banner.py --fonts DIR   # same as render_brand_art.py

``puppeteer_dir`` stays here because other render scripts in the repo
(``corpus/vapi-defaults/render_funnel.py``) import it to find a puppeteer
install: ``$HOTATO_PUPPETEER_DIR`` first, then each of ``PUPPETEER_DIRS``.
"""

from __future__ import annotations

import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

PUPPETEER_DIRS = (
    "/tmp/node_modules/puppeteer",
    os.path.join(ROOT, "node_modules", "puppeteer"),
    os.path.expanduser("~/node_modules/puppeteer"),
)


def puppeteer_dir() -> str | None:
    """The first puppeteer install that exists, or None."""
    env = os.environ.get("HOTATO_PUPPETEER_DIR")
    for candidate in ((env,) if env else ()) + PUPPETEER_DIRS:
        if candidate and os.path.isdir(candidate):
            return candidate
    return None


def main() -> int:
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    import render_brand_art

    return render_brand_art.main()


if __name__ == "__main__":
    raise SystemExit(main())
