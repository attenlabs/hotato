#!/usr/bin/env python3
"""Render the repository's brand art from outlined type.

    .github/assets/hotato-banner.svg        README banner, light (also the PyPI image)
    .github/assets/hotato-banner-dark.svg   README banner, dark (GitHub dark mode)
    .github/social-preview.svg              GitHub social card source, 1280x640
    .github/social-preview.png              the card GitHub serves, 1280x640

Every letter is a path, outlined here from the font files with fontTools, so
the art renders the same in a browser, on GitHub, on PyPI and in librsvg: the
old banner was box-drawing characters in SVG <text>, which drew differently in
every monospace face it fell back to. Nothing in the art carries a version
number, so a release never makes it stale.

The fonts are only needed to regenerate. Both are SIL Open Font License,
from Google Fonts, and are the faces hotato.dev self-hosts:

    Hanken Grotesk     (variable, any of .ttf / .woff2; instanced at 700 and 500)
    Spline Sans Mono   (variable; instanced at 500)

    python3 scripts/render_brand_art.py --fonts DIR

DIR holds one file whose name contains "anken" and one containing "pline".
Needs ``pip install fonttools brotli`` (brotli only to read .woff2). The PNG is
rasterized with ``rsvg-convert`` (librsvg); pass --no-png to skip it.
"""

from __future__ import annotations

import argparse
import glob
import io
import os
import shutil
import subprocess
import sys

from fontTools.pens.svgPathPen import SVGPathPen
from fontTools.pens.transformPen import TransformPen
from fontTools.ttLib import TTFont
from fontTools.varLib import instancer

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(HERE)
GITHUB = os.path.join(REPO, ".github")

sys.path.insert(0, os.path.join(REPO, "src"))
from hotato import theme  # noqa: E402  (the one place hotato's colours live)

# The light banner (PyPI, GitHub light mode) uses the print neutrals; the
# dark one uses theme.INK / theme.MUTED.
LIGHT_INK = theme.PAPER_INK
LIGHT_MUTED = theme.PAPER_INK_2
POTATO = "#dcac5f"
POTATO_LINE = "#241a10"
POTATO_DOT = "#7d531f"

TAGLINE = "Simulate, monitor and gate voice agents in CI."


def _num(v: float) -> str:
    s = f"{v:.1f}"
    return s[:-2] if s.endswith(".0") else s


class Face:
    """One static instance of a variable font, able to set a line as paths."""

    def __init__(self, path: str, wght: int):
        font = TTFont(path)
        if "fvar" in font:
            font = instancer.instantiateVariableFont(font, {"wght": wght})
            buf = io.BytesIO()
            font.flavor = None
            font.save(buf)
            buf.seek(0)
            font = TTFont(buf)
        self.font = font
        self.upm = font["head"].unitsPerEm
        self.cmap = font.getBestCmap()
        self.gs = font.getGlyphSet()
        self.hmtx = font["hmtx"]

    def width(self, text: str, size: float, track: float = 0.0) -> float:
        adv = sum(self.hmtx[self.cmap[ord(c)]][0] for c in text)
        return adv * size / self.upm + track * size * (len(text) - 1)

    def path(self, text: str, x: float, baseline: float, size: float,
             track: float = 0.0) -> str:
        """SVG path data for `text`, left edge at x, on `baseline`."""
        s = size / self.upm
        out = []
        for c in text:
            g = self.cmap[ord(c)]
            pen = SVGPathPen(self.gs, ntos=_num)
            self.gs[g].draw(TransformPen(pen, (s, 0, 0, -s, x, baseline)))
            d = pen.getCommands()
            if d:
                out.append(d)
            x += self.hmtx[g][0] * s + track * size
        return "".join(out)


def potato(x: float, y: float, size: float, line: str = POTATO_LINE) -> str:
    """The hotato mark (the site's nav mark), scaled into a size-px square."""
    k = size / 40
    return (
        f'<g transform="translate({_num(x)} {_num(y)}) scale({k:.4f})">'
        f'<path d="M15 9q-3-3 0-6" fill="none" stroke="{theme.EMBER}" stroke-width="2" stroke-linecap="round"/>'
        f'<path d="M22 8q3-3 0-6" fill="none" stroke="{theme.EMBER_GLOW}" stroke-width="2" stroke-linecap="round"/>'
        f'<path d="M8 24c-1.5-8 4-14 13-14.5 10-.5 12 6 12 12 0 7-5.5 12-13.5 12C12 33.5 9 30 8 24Z" '
        f'fill="{POTATO}" stroke="{line}" stroke-width="2"/>'
        f'<ellipse cx="15" cy="19" rx="1.3" ry="1" fill="{POTATO_DOT}"/>'
        f'<ellipse cx="27" cy="26" rx="1.3" ry="1" fill="{POTATO_DOT}"/>'
        f'<path d="M15 22h4M23 22h4M18 28h6" stroke="{POTATO_LINE}" stroke-width="2.1" stroke-linecap="round"/>'
        f'</g>')


def banner(bold: Face, mid: Face, ink: str, muted: str) -> str:
    W, H = 760, 168
    mark = 120
    word_size, word_track = 96, -0.035
    tag_size = 24
    x0 = mark + 20
    word = bold.path("hotato", x0, 96, word_size, word_track)
    tag = mid.path(TAGLINE, x0 + 4, 140, tag_size)
    tag_w = mid.width(TAGLINE, tag_size) + x0 + 4
    word_w = bold.width("hotato", word_size, word_track) + x0
    W = int(max(tag_w, word_w) + 8)
    return (
        f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {W} {H}" width="{W}" height="{H}" '
        f'role="img" aria-label="hotato: {TAGLINE}">'
        f'{potato(0, 24, mark)}'
        f'<path d="{word}" fill="{ink}"/>'
        f'<path d="{tag}" fill="{muted}"/>'
        f'</svg>\n')


def social(bold: Face, mid: Face, mono: Face) -> str:
    W, H = 1280, 640
    pad = 88
    ink, muted = theme.INK, theme.MUTED
    parts = [
        f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {W} {H}" width="{W}" height="{H}">',
        '<defs><radialGradient id="g" cx="85%" cy="0%" r="70%">'
        f'<stop offset="0" stop-color="{theme.EMBER}" stop-opacity=".22"/>'
        f'<stop offset="1" stop-color="{theme.EMBER}" stop-opacity="0"/></radialGradient></defs>',
        f'<rect width="{W}" height="{H}" fill="{theme.GROUND}"/>',
        f'<rect width="{W}" height="{H}" fill="url(#g)"/>',
        potato(pad - 6, 70, 96),
        f'<path d="{bold.path("hotato", pad + 104, 146, 84, -0.035)}" fill="{ink}"/>',
        f'<path d="{bold.path("Simulate, monitor and gate", pad, 262, 60, -0.03)}" fill="{ink}"/>',
        f'<path d="{bold.path("voice agents in CI.", pad, 334, 60, -0.03)}" fill="{ink}"/>',
    ]
    # a terminal strip: the tool's own output for its bundled demo call, after
    # `hotato pin` has saved the incident as a contract (as on hotato.dev)
    top, left, right, bottom = 386, pad, W - pad, 556
    parts.append(f'<rect x="{left}" y="{top}" width="{right - left}" height="{bottom - top}" rx="16" '
                 f'fill="{theme.SURFACE}" stroke="{theme.BORDER}" stroke-width="2"/>')
    fs, lh, tx = 24, 40, left + 32
    y = top + 50
    line1 = [("$ ", theme.EMBER), ("hotato check --demo", ink)]
    line2 = [("[CRITICAL] BARGE-IN", theme.RED), ("  t=1.99s  overlap=2.51s", ink)]
    line3 = [("$ ", theme.EMBER), ("hotato prove --contracts contracts", ink),
             ("  overall ", muted), ("FAIL (exit 1)", theme.RED)]
    for line in (line1, line2, line3):
        x = tx
        for text, colour in line:
            d = mono.path(text, x, y, fs)
            if d:
                parts.append(f'<path d="{d}" fill="{colour}"/>')
            x += mono.width(text, fs)
        y += lh
    foot = "pip install hotato   ·   hotato.dev   ·   MIT"
    parts.append(f'<path d="{mid.path(foot, pad, 604, 24)}" fill="{muted}"/>')
    parts.append("</svg>\n")
    return "".join(parts)


def find_font(d: str, needle: str) -> str:
    hits = sorted(p for p in glob.glob(os.path.join(d, "*"))
                  if needle in os.path.basename(p).lower()
                  and p.lower().endswith((".ttf", ".otf", ".woff2", ".woff")))
    if not hits:
        raise SystemExit(f"no font file with {needle!r} in its name under {d}")
    return hits[0]


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--fonts", required=True, help="directory with the Hanken Grotesk and Spline Sans Mono files")
    ap.add_argument("--no-png", action="store_true", help="write the SVGs only")
    args = ap.parse_args()

    hanken = find_font(args.fonts, "anken")
    spline = find_font(args.fonts, "pline")
    bold, mid, mono = Face(hanken, 700), Face(hanken, 500), Face(spline, 500)

    out = {
        os.path.join(GITHUB, "assets", "hotato-banner.svg"): banner(bold, mid, LIGHT_INK, LIGHT_MUTED),
        os.path.join(GITHUB, "assets", "hotato-banner-dark.svg"): banner(bold, mid, theme.INK, theme.MUTED),
        os.path.join(GITHUB, "social-preview.svg"): social(bold, mid, mono),
    }
    for path, svg in out.items():
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(svg)
        print(f"wrote {os.path.relpath(path, REPO)} ({len(svg)} bytes)")

    if not args.no_png:
        rsvg = shutil.which("rsvg-convert")
        if not rsvg:
            raise SystemExit("rsvg-convert not found; install librsvg or pass --no-png")
        png = os.path.join(GITHUB, "social-preview.png")
        subprocess.run([rsvg, "-w", "1280", "-h", "640", "-o", png,
                        os.path.join(GITHUB, "social-preview.svg")], check=True)
        print(f"wrote {os.path.relpath(png, REPO)} (1280x640)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
