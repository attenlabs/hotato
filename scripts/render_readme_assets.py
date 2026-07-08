#!/usr/bin/env python3
"""Render the README demo-report assets.

Writes two files under docs/assets/:

- ``hotato-demo-report.html``: the self-contained HTML report for the packaged
  demo battery of two REAL recorded failing calls, with the exact scored audio
  embedded under each timeline (the same thing ``hotato demo`` renders).
- ``hotato-demo-report.png``: a 1200 px wide screenshot of that page, cropped
  from the top of the page to the first fix card, so it shows the failing
  summary, at least one per-event timeline, and at least one fix card.

Rendering uses puppeteer from /tmp/node_modules via ``node -e`` when present,
and falls back to playwright if that is importable. The crop is verified by
measuring, inside the rendered page, that the summary, a timeline SVG, and a
fix card all sit inside the cropped region; the script exits nonzero if any of
them do not.

Run from the repo root (or anywhere):

    python3 scripts/render_readme_assets.py
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SRC = os.path.join(ROOT, "src")
if SRC not in sys.path:
    sys.path.insert(0, SRC)

ASSETS = os.path.join(ROOT, "docs", "assets")
HTML_PATH = os.path.join(ASSETS, "hotato-demo-report.html")
PNG_PATH = os.path.join(ASSETS, "hotato-demo-report.png")

PUPPETEER_DIR = "/tmp/node_modules/puppeteer"
PNG_WIDTH = 1200
CROP_PAD_PX = 16
MIN_PNG_BYTES = 10 * 1024

# Everything the crop must contain, measured inside the rendered page itself.
# .summary is the failing suite summary bar, .card .tl svg is the first
# per-event timeline, .card .fix is the first fix card.
_NODE_SCRIPT = """
const puppeteer = require(%(puppeteer)s);
(async () => {
  const browser = await puppeteer.launch({args: ['--no-sandbox', '--disable-dev-shm-usage']});
  const page = await browser.newPage();
  await page.setViewport({width: %(width)d, height: 800, deviceScaleFactor: 1});
  await page.goto('file://' + %(html)s, {waitUntil: 'networkidle0'});
  const boxes = await page.evaluate(() => {
    const box = (el) => {
      if (!el) return null;
      const r = el.getBoundingClientRect();
      return {top: r.top + window.scrollY, bottom: r.bottom + window.scrollY,
              left: r.left + window.scrollX, right: r.right + window.scrollX};
    };
    return {
      summary: box(document.querySelector('.summary')),
      timeline: box(document.querySelector('.card .tl svg')),
      fix: box(document.querySelector('.card .fix')),
    };
  });
  for (const k of ['summary', 'timeline', 'fix']) {
    if (!boxes[k]) { console.error('missing element in page: ' + k); process.exit(3); }
  }
  const cropHeight = Math.ceil(Math.max(
    boxes.summary.bottom, boxes.timeline.bottom, boxes.fix.bottom)) + %(pad)d;
  await page.screenshot({path: %(png)s,
    clip: {x: 0, y: 0, width: %(width)d, height: cropHeight}});
  console.log(JSON.stringify({crop: {width: %(width)d, height: cropHeight}, boxes: boxes}));
  await browser.close();
})().catch((e) => { console.error(e && e.stack || String(e)); process.exit(2); });
"""


def build_html() -> None:
    from hotato import report as _report
    from hotato.core import SUITE_ID
    from importlib import resources

    demo_root = resources.files("hotato").joinpath("data", "demo", "failing")
    os.makedirs(ASSETS, exist_ok=True)
    env = _report.write_report(
        HTML_PATH,
        fmt="html",
        suite=SUITE_ID,
        stack="generic",
        scenarios_dir=str(demo_root.joinpath("scenarios")),
        audio_dir=str(demo_root.joinpath("audio")),
        embed_audio=True,
    )
    if env["summary"]["failed"] != env["summary"]["events"]:
        raise SystemExit(
            "demo battery did not fail on every event; the asset must show the "
            f"intentionally failing report (summary: {env['summary']})"
        )
    print(f"wrote {HTML_PATH} ({os.path.getsize(HTML_PATH)} bytes)")


def _verify(measured: dict) -> None:
    """Assert, from the in-page measurements, that every required element is
    inside the crop. Exits via SystemExit on any miss."""
    crop = measured["crop"]
    if crop["width"] != PNG_WIDTH:
        raise SystemExit(f"crop width {crop['width']} != {PNG_WIDTH}")
    for name in ("summary", "timeline", "fix"):
        b = measured["boxes"][name]
        inside = (b["top"] >= 0 and b["bottom"] <= crop["height"]
                  and b["left"] >= 0 and b["right"] <= crop["width"])
        if not inside:
            raise SystemExit(f"{name} is outside the crop: {b} vs {crop}")
        print(f"  in crop: {name} top={b['top']:.0f} bottom={b['bottom']:.0f}")
    if not os.path.exists(PNG_PATH):
        raise SystemExit(f"{PNG_PATH} was not written")
    size = os.path.getsize(PNG_PATH)
    if size < MIN_PNG_BYTES:
        raise SystemExit(f"{PNG_PATH} is implausibly small ({size} bytes)")
    print(f"wrote {PNG_PATH} ({size} bytes, {crop['width']}x{crop['height']})")


def render_png_puppeteer() -> bool:
    node = shutil.which("node")
    if not node or not os.path.isdir(PUPPETEER_DIR):
        return False
    script = _NODE_SCRIPT % {
        "puppeteer": json.dumps(PUPPETEER_DIR),
        "html": json.dumps(HTML_PATH),
        "png": json.dumps(PNG_PATH),
        "width": PNG_WIDTH,
        "pad": CROP_PAD_PX,
    }
    proc = subprocess.run([node, "-e", script], capture_output=True, text=True,
                          timeout=180)
    if proc.returncode != 0:
        print(f"puppeteer render failed:\n{proc.stderr}", file=sys.stderr)
        return False
    _verify(json.loads(proc.stdout.strip().splitlines()[-1]))
    return True


def render_png_playwright() -> bool:
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        return False
    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page(viewport={"width": PNG_WIDTH, "height": 800})
        page.goto("file://" + HTML_PATH)
        boxes = page.evaluate(
            """() => {
                const box = (el) => {
                  if (!el) return null;
                  const r = el.getBoundingClientRect();
                  return {top: r.top + window.scrollY, bottom: r.bottom + window.scrollY,
                          left: r.left + window.scrollX, right: r.right + window.scrollX};
                };
                return {summary: box(document.querySelector('.summary')),
                        timeline: box(document.querySelector('.card .tl svg')),
                        fix: box(document.querySelector('.card .fix'))};
            }"""
        )
        if not all(boxes.get(k) for k in ("summary", "timeline", "fix")):
            raise SystemExit(f"missing element in page: {boxes}")
        height = int(max(b["bottom"] for b in boxes.values())) + CROP_PAD_PX
        page.screenshot(path=PNG_PATH,
                        clip={"x": 0, "y": 0, "width": PNG_WIDTH, "height": height})
        browser.close()
    _verify({"crop": {"width": PNG_WIDTH, "height": height}, "boxes": boxes})
    return True


def main() -> int:
    build_html()
    if render_png_puppeteer():
        return 0
    if render_png_playwright():
        return 0
    print(
        "no renderer available. Install one of:\n"
        "  npm install --prefix /tmp puppeteer      # then rerun this script\n"
        "  pip install playwright && playwright install chromium\n"
        f"then rerun: python3 {os.path.relpath(__file__, ROOT)}",
        file=sys.stderr,
    )
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
