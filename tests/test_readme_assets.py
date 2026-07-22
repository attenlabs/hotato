"""The committed README demo-report assets exist and are plausible.

Regenerate with: ``python3 scripts/render_readme_assets.py`` (renders the
packaged demo battery of two bundled failing calls, with embedded audio, to
docs/assets/ and screenshots it, verifying in-page that the failing summary, a
timeline, and a fix card are inside the crop).
"""

import os

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
ASSETS = os.path.join(ROOT, "docs", "assets")

# docs/assets/ is a screenshot artifact pruned from the sdist to keep it lean
# (regenerated with scripts/render_readme_assets.py, which needs a browser). In
# a source checkout the assets are present and these tests run; from an extracted
# sdist they are legitimately absent, so skip rather than error.
_HAVE_ASSETS = os.path.isdir(ASSETS)
pytestmark = pytest.mark.skipif(
    not _HAVE_ASSETS, reason="docs/assets pruned from sdist (regenerate in a checkout)"
)


def test_demo_report_png_exists_and_is_a_real_png():
    png = os.path.join(ASSETS, "hotato-demo-report.png")
    assert os.path.exists(png), (
        "missing README asset; regenerate with "
        "python3 scripts/render_readme_assets.py")
    assert os.path.getsize(png) > 10 * 1024
    with open(png, "rb") as fh:
        assert fh.read(8) == b"\x89PNG\r\n\x1a\n"


def test_demo_report_html_source_exists():
    html = os.path.join(ASSETS, "hotato-demo-report.html")
    assert os.path.exists(html)
    with open(html, encoding="utf-8") as fh:
        page = fh.read()
    assert "fd-01-missed-interruption" in page
    assert "fd-02-backchannel-yielded" in page
