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

import glob
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


# --- Static distribution-surface lockstep -------------------------------------
# Files an agent, a citation tool, or the MCP registry reads directly, none of
# which the version-consistency tests above ever parsed. Each is read straight
# from disk (no import, no CLI) and compared to pyproject.toml, so a missed bump
# in any single surface reddens this file. (This is exactly the class of drift
# that shipped: llms.txt still said "Version 0.5.0" at the 0.9.0 release.)


def _llms_txt_version():
    """The 'Version X.Y.Z' line in llms.txt -- the machine-readable index an
    agent reads to learn the tool version without running it."""
    with open(os.path.join(ROOT, "llms.txt"), encoding="utf-8") as fh:
        text = fh.read()
    m = re.search(r"(?m)^>?\s*Version\s+(\S+)", text)
    assert m, "no 'Version X.Y.Z' line found in llms.txt"
    return m.group(1).rstrip(".")


def _server_json():
    with open(os.path.join(ROOT, "server.json"), encoding="utf-8") as fh:
        return json.load(fh)


def _citation_cff_version():
    """The top-level `version:` key in CITATION.cff. Parsed with a regex to
    keep this test stdlib-only (no PyYAML dependency); the file's `version:`
    lives at column 0, distinct from any nested key."""
    with open(os.path.join(ROOT, "CITATION.cff"), encoding="utf-8") as fh:
        text = fh.read()
    m = re.search(r"(?m)^version:\s*(\S+)", text)
    assert m, "no top-level 'version:' key found in CITATION.cff"
    return m.group(1).strip("\"'")


def test_llms_txt_version_matches_pyproject():
    assert _llms_txt_version() == _pyproject_version(), (
        "llms.txt's 'Version X.Y.Z' line is out of lockstep with pyproject.toml "
        "-- agents read this file for the tool version. Bump it "
        "(see docs/RELEASE-CHECKLIST.md)."
    )


def test_server_json_top_level_version_matches_pyproject():
    assert _server_json()["version"] == _pyproject_version(), (
        "server.json top-level 'version' is out of lockstep with pyproject.toml "
        "-- this is what the MCP registry publishes. Bump it."
    )


def test_server_json_package_versions_match_pyproject():
    pv = _pyproject_version()
    packages = _server_json().get("packages", [])
    assert packages, "server.json has no packages[] to check"
    for pkg in packages:
        assert pkg.get("version") == pv, (
            f"server.json package {pkg.get('identifier')!r} version "
            f"{pkg.get('version')!r} != pyproject.toml {pv!r}. Bump it."
        )


def test_citation_cff_version_matches_pyproject():
    assert _citation_cff_version() == _pyproject_version(), (
        "CITATION.cff 'version:' is out of lockstep with pyproject.toml -- this "
        "is what citation tooling reports. Bump it (and date-released)."
    )


# --- Prose version pins in docs/*.md ------------------------------------------
# Human-facing docs that pin a concrete "hotato X.Y.Z" version drift silently:
# docs/TRUST-GALLERY.md still said "hotato 0.5.0" at the 0.10.0 release because
# no test ever compared a prose version claim to the packaged version.

_DOCS_VERSION_RE = re.compile(r"hotato\s+v?(\d+\.\d+\.\d+)")


def _docs_version_claims():
    """Every ``hotato X.Y.Z`` version pin in docs/*.md, as
    (relative_path, line_number, version) tuples."""
    claims = []
    for path in sorted(glob.glob(os.path.join(ROOT, "docs", "*.md"))):
        with open(path, encoding="utf-8") as fh:
            for lineno, line in enumerate(fh, 1):
                for m in _DOCS_VERSION_RE.finditer(line):
                    claims.append((os.path.relpath(path, ROOT), lineno, m.group(1)))
    return claims


def test_docs_hotato_version_claims_match_pyproject():
    """Any 'hotato X.Y.Z' version claim in docs/*.md must equal the packaged
    version, so a doc's prose version pin can't drift the way TRUST-GALLERY.md
    did (it said 0.5.0 while the package was 0.10.0)."""
    pv = _pyproject_version()
    stale = [c for c in _docs_version_claims() if c[2] != pv]
    assert not stale, (
        f"docs/*.md pin a 'hotato X.Y.Z' version that disagrees with "
        f"pyproject.toml ({pv}) -- update each prose version claim: {stale}"
    )


# --- docs/CI.md copy-paste ADOPTION examples ----------------------------------
# The CI.md "root Action" section teaches full-SHA pinning and shows the exact
# version a user should adopt in three copy-paste forms: the `git ls-remote
# refs/tags/vX.Y.Z` resolve command, the `# vX.Y.Z` comment on the
# `attenlabs/hotato@<sha>` pin, and the `hotato==X.Y.Z` pip example. Those three
# must all name the CURRENT release, or the doc ships a stale example (it carried
# v1.5.1 and 1.3.3 while the package was 1.5.2). The "available ... from release
# vX.Y.Z onward" AVAILABILITY-FLOOR sentence is a distinct, historical first-ship
# fact (the release that first shipped action.yml) and is deliberately NOT matched
# by these adoption-example patterns.
_CI_MD = os.path.join(ROOT, "docs", "CI.md")
_CI_ADOPTION_PATTERNS = {
    "git ls-remote refs/tags pin": re.compile(r"refs/tags/v(\d+\.\d+\.\d+)"),
    "attenlabs/hotato@<sha> # vX.Y.Z comment": re.compile(
        r"attenlabs/hotato@\S+\s+#\s*v(\d+\.\d+\.\d+)"
    ),
    "hotato==X.Y.Z pip example": re.compile(r"hotato==(\d+\.\d+\.\d+)"),
}


def test_ci_md_adoption_example_pins_match_pyproject():
    if not os.path.exists(_CI_MD):
        return  # doc not present in this tree
    with open(_CI_MD, encoding="utf-8") as fh:
        text = fh.read()
    pv = _pyproject_version()
    stale = []
    for label, rx in _CI_ADOPTION_PATTERNS.items():
        for found in rx.findall(text):
            if found != pv:
                stale.append(f"{label}: found {found}, expected {pv}")
    assert not stale, (
        "docs/CI.md adoption examples must name the current release "
        f"({pv}); reconcile these stale example pins: {stale}"
    )
