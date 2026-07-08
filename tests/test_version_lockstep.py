"""Version lockstep: every surface that self-reports the tool version must
agree with pyproject.toml.

Regression guard for the 0.4.0 release defect: the published wheel's
``hotato describe`` / ``hotato --version`` said "hotato 0.3.1" because
``src/hotato/__init__.py``'s ``__version__`` literal was missed by the
release bump, and the existing version-consistency tests only compared
server.json / CITATION.cff to pyproject -- nothing ever read
``hotato.__version__`` against an independent source (test_describe_cli
compared the manifest to ``__version__`` itself, which is circular and
agrees even when both are wrong).

pyproject.toml is the independent source of truth here: it is what names
the wheel/sdist version, it ships in the sdist, and the other lockstep
tests already anchor on it.
"""

import json
import os
import re

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _pyproject_version():
    with open(os.path.join(ROOT, "pyproject.toml"), encoding="utf-8") as fh:
        pyproject = fh.read()
    m = re.search(r'(?m)^version\s*=\s*"([^"]+)"', pyproject)
    assert m, "no version in pyproject.toml"
    return m.group(1)


def test_dunder_version_matches_pyproject():
    """THE shipped bug: __init__.py's literal drifted from pyproject."""
    from hotato import __version__

    assert __version__ == _pyproject_version(), (
        "src/hotato/__init__.py __version__ is out of lockstep with "
        "pyproject.toml -- this is exactly how 0.4.0 shipped a wheel whose "
        "describe/--version said 0.3.1. Bump both (see docs/RELEASE-CHECKLIST.md)."
    )


def test_describe_manifest_version_matches_pyproject(capsys):
    """End to end through the CLI: the describe manifest an agent reads must
    carry the packaged version, compared against pyproject (not against
    hotato.__version__, which would be circular)."""
    from hotato import cli

    code = cli.main(["describe", "--format", "json"])
    assert code == 0
    manifest = json.loads(capsys.readouterr().out)
    assert manifest["version"] == _pyproject_version()


def test_describe_text_banner_matches_pyproject(capsys):
    from hotato import cli

    code = cli.main(["describe", "--format", "text"])
    assert code == 0
    out = capsys.readouterr().out
    assert out.startswith(f"hotato {_pyproject_version()} ")


def test_installed_dist_metadata_matches_pyproject():
    """The installed distribution's metadata (what `pip show hotato` and the
    wheel filename say) must agree with the source tree being tested. Skips
    when hotato is not installed at all (bare source-tree run); fails when an
    installed copy disagrees -- that means the venv is stale (re-run
    `pip install -e .`) or the bump missed pyproject."""
    import pytest

    try:
        from importlib.metadata import PackageNotFoundError, version
    except ImportError:  # pragma: no cover - py3.9+ always has it
        pytest.skip("importlib.metadata unavailable")
    try:
        dist_version = version("hotato")
    except PackageNotFoundError:
        pytest.skip("hotato is not installed in this environment")
    assert dist_version == _pyproject_version()
