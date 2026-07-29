#!/usr/bin/env python3
"""Rasterize the two committed hero HTMLs in .github/ to their PNGs.

    .github/banner.html       -> .github/banner.png          (the README hero)
    .github/social-card.html  -> .github/social-preview.png  (the GitHub social card)

Both are 1200x630 (the standard social-preview size) and both carry their own
palette in a ``:root`` block that mirrors ``src/hotato/theme.py``. The taglines
in the HTML are canonical, verbatim from the README's bold pitch and the
banner/GIF alt text; this script only rasterizes them.

Rendering uses puppeteer via ``node -e`` when a puppeteer install can be found,
and falls back to playwright if that is importable. Puppeteer is looked up, in
order, in ``$HOTATO_PUPPETEER_DIR``, then in each of ``PUPPETEER_DIRS`` below,
so a plain ``npm install puppeteer`` in the home directory or the repo is
enough. Run from anywhere:

    python3 scripts/render_banner.py                 # both
    python3 scripts/render_banner.py --only banner   # one of banner|social
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
GITHUB_DIR = os.path.join(ROOT, ".github")

# name -> (source HTML, output PNG)
TARGETS = {
    "banner": ("banner.html", "banner.png"),
    "social": ("social-card.html", "social-preview.png"),
}

PUPPETEER_DIRS = (
    "/tmp/node_modules/puppeteer",
    os.path.join(ROOT, "node_modules", "puppeteer"),
    os.path.expanduser("~/node_modules/puppeteer"),
)
WIDTH, HEIGHT = 1200, 630
MIN_PNG_BYTES = 10 * 1024

_NODE_SCRIPT = """
const puppeteer = require(%(puppeteer)s);
(async () => {
  const browser = await puppeteer.launch({args: ['--no-sandbox', '--disable-dev-shm-usage']});
  const page = await browser.newPage();
  await page.setViewport({width: %(w)d, height: %(h)d, deviceScaleFactor: 2});
  await page.goto('file://' + %(html)s, {waitUntil: 'networkidle0'});
  await page.screenshot({path: %(png)s, clip: {x: 0, y: 0, width: %(w)d, height: %(h)d}});
  await browser.close();
})().catch((e) => { console.error(e && e.stack || String(e)); process.exit(2); });
"""


def puppeteer_dir() -> str | None:
    """The first puppeteer install that exists, or None."""
    env = os.environ.get("HOTATO_PUPPETEER_DIR")
    for candidate in ((env,) if env else ()) + PUPPETEER_DIRS:
        if candidate and os.path.isdir(candidate):
            return candidate
    return None


def _check(png_path: str) -> None:
    if not os.path.exists(png_path):
        raise SystemExit(f"{png_path} was not written")
    size = os.path.getsize(png_path)
    if size < MIN_PNG_BYTES:
        raise SystemExit(f"{png_path} is implausibly small ({size} bytes)")
    print(f"wrote {png_path} ({size} bytes, {WIDTH}x{HEIGHT} @2x)")


def render_puppeteer(html_path: str, png_path: str) -> bool:
    node = shutil.which("node")
    pdir = puppeteer_dir()
    if not node or not pdir:
        return False
    script = _NODE_SCRIPT % {
        "puppeteer": json.dumps(pdir),
        "html": json.dumps(html_path),
        "png": json.dumps(png_path),
        "w": WIDTH, "h": HEIGHT,
    }
    proc = subprocess.run([node, "-e", script], capture_output=True, text=True, timeout=180)
    if proc.returncode != 0:
        print(f"puppeteer render failed:\n{proc.stderr}", file=sys.stderr)
        return False
    _check(png_path)
    return True


def render_playwright(html_path: str, png_path: str) -> bool:
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        return False
    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page(viewport={"width": WIDTH, "height": HEIGHT},
                                device_scale_factor=2)
        page.goto("file://" + html_path)
        page.screenshot(path=png_path,
                        clip={"x": 0, "y": 0, "width": WIDTH, "height": HEIGHT})
        browser.close()
    _check(png_path)
    return True


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--only", choices=sorted(TARGETS), default=None,
                    help="render just one of the two heroes (default: both)")
    args = ap.parse_args()

    names = [args.only] if args.only else list(TARGETS)
    for name in names:
        html_name, png_name = TARGETS[name]
        html_path = os.path.join(GITHUB_DIR, html_name)
        png_path = os.path.join(GITHUB_DIR, png_name)
        if not os.path.exists(html_path):
            raise SystemExit(f"{html_path} is missing")
        if render_puppeteer(html_path, png_path):
            continue
        if render_playwright(html_path, png_path):
            continue
        print(
            "no renderer available. Install one of:\n"
            "  npm install puppeteer   # then rerun, or set HOTATO_PUPPETEER_DIR\n"
            "  pip install playwright && playwright install chromium",
            file=sys.stderr,
        )
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
