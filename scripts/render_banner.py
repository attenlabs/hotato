#!/usr/bin/env python3
"""Render .github/banner.png from .github/banner.html (the README hero).

A 1200x630 PNG (standard social-preview size). The tagline in the HTML is the
ONE canonical line, verbatim from the README's bold pitch and the banner/GIF
alt text; this script only rasterizes it.

Uses puppeteer from /tmp/node_modules via ``node -e`` when present, and falls
back to playwright if importable. Run from anywhere:

    python3 scripts/render_banner.py
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
HTML_PATH = os.path.join(ROOT, ".github", "banner.html")
PNG_PATH = os.path.join(ROOT, ".github", "banner.png")
PUPPETEER_DIR = "/tmp/node_modules/puppeteer"
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


def _check() -> None:
    if not os.path.exists(PNG_PATH):
        raise SystemExit(f"{PNG_PATH} was not written")
    size = os.path.getsize(PNG_PATH)
    if size < MIN_PNG_BYTES:
        raise SystemExit(f"{PNG_PATH} is implausibly small ({size} bytes)")
    print(f"wrote {PNG_PATH} ({size} bytes, {WIDTH}x{HEIGHT} @2x)")


def render_puppeteer() -> bool:
    node = shutil.which("node")
    if not node or not os.path.isdir(PUPPETEER_DIR):
        return False
    script = _NODE_SCRIPT % {
        "puppeteer": json.dumps(PUPPETEER_DIR),
        "html": json.dumps(HTML_PATH),
        "png": json.dumps(PNG_PATH),
        "w": WIDTH, "h": HEIGHT,
    }
    proc = subprocess.run([node, "-e", script], capture_output=True, text=True, timeout=180)
    if proc.returncode != 0:
        print(f"puppeteer render failed:\n{proc.stderr}", file=sys.stderr)
        return False
    _check()
    return True


def render_playwright() -> bool:
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        return False
    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page(viewport={"width": WIDTH, "height": HEIGHT},
                                device_scale_factor=2)
        page.goto("file://" + HTML_PATH)
        page.screenshot(path=PNG_PATH,
                        clip={"x": 0, "y": 0, "width": WIDTH, "height": HEIGHT})
        browser.close()
    _check()
    return True


def main() -> int:
    if render_puppeteer():
        return 0
    if render_playwright():
        return 0
    print("no renderer available (need /tmp/node_modules/puppeteer or playwright)",
          file=sys.stderr)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
