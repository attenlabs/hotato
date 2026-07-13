"""README.pypi.md is the PyPI long-description: README.md with repo-relative
links rewritten to absolute GitHub URLs (PyPI resolves relative links against
pypi.org, so they would 404). These tests keep it current and link-clean."""
import os
import re
import subprocess
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PYPI_README = os.path.join(ROOT, "README.pypi.md")
BUILDER = os.path.join(ROOT, "scripts", "build_pypi_readme.py")

_LINK = re.compile(r'!?\[[^\]]*\]\(([^)]+)\)')


def test_pypi_readme_is_current():
    """A stale README.pypi.md ships broken PyPI links; --check must pass."""
    r = subprocess.run([sys.executable, BUILDER, "--check"],
                       capture_output=True, text=True)
    assert r.returncode == 0, (
        "README.pypi.md is stale -- run scripts/build_pypi_readme.py\n" + r.stderr)


def test_pypi_readme_has_no_repo_relative_links():
    with open(PYPI_README, encoding="utf-8") as fh:
        text = fh.read()
    bad = []
    for target in _LINK.findall(text):
        t = target.strip()
        if t.startswith(("http://", "https://", "mailto:", "#", "//", "data:")):
            continue
        bad.append(target)
    assert not bad, f"README.pypi.md still has repo-relative links (404 on PyPI): {bad}"
