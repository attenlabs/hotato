"""``scripts/check_public_surfaces.py``: the release-blocking public-surface
reconciliation gate.

Group A (local cross-source consistency) is exercised end to end against a
TEMP fake-repo layout: every in-repo surface that names the version is written
into a tmp tree, and the gate must (a) pass when they all agree and (b) FAIL
when any single source drifts. Group B is not unit-tested against the live
network here; only its pure parsing helper (``check_pypi_doc``) is asserted on
a fixture JSON, so no test ever performs I/O against pypi.org / hotato.dev.
"""

from __future__ import annotations

import importlib.util
import os

import pytest

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_SCRIPT = os.path.join(_ROOT, "scripts", "check_public_surfaces.py")


def _load_module():
    spec = importlib.util.spec_from_file_location("check_public_surfaces", _SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


cps = _load_module()

VERSION = "1.15.1"


def _write_fake_repo(root, *, version=VERSION, overrides=None):
    """Materialize a minimal repo layout naming ``version`` across every group-A
    surface. ``overrides`` maps a surface key to a REPLACEMENT version string so
    a single source can be drifted while the rest stay in lockstep."""
    overrides = overrides or {}

    def v(key):
        return overrides.get(key, version)

    root = str(root)
    (pkg := os.path.join(root, "src", "hotato"))  # noqa: F841
    os.makedirs(os.path.join(root, "src", "hotato"), exist_ok=True)

    with open(os.path.join(root, "pyproject.toml"), "w", encoding="utf-8") as fh:
        fh.write(f'[project]\nname = "hotato"\nversion = "{v("pyproject")}"\n')

    with open(os.path.join(root, "src", "hotato", "__init__.py"), "w",
              encoding="utf-8") as fh:
        fh.write(f'__version__ = "{v("init")}"\n')

    with open(os.path.join(root, "README.md"), "w", encoding="utf-8") as fh:
        fh.write(
            "# hotato\n\n```yaml\n"
            f"      - uses: attenlabs/hotato@v{v('readme_pin')}\n"
            "        with:\n"
            "          contracts: contracts/\n"
            f"          hotato-version: {v('readme_input')}\n"
            "```\n"
        )

    with open(os.path.join(root, "CHANGELOG.md"), "w", encoding="utf-8") as fh:
        fh.write(
            "# Changelog\n\n## [Unreleased]\n\n"
            f"## [{v('changelog')}] - 2026-07-22\n\n### Changed\n- stuff\n\n"
            "## [1.6.0] - 2026-01-01\n- older\n"
        )

    with open(os.path.join(root, "llms.txt"), "w", encoding="utf-8") as fh:
        fh.write(f"# hotato\n\n> some index text\n>\n> Version {v('llms')}\n")

    return root


def test_group_a_passes_when_all_sources_agree(tmp_path):
    root = _write_fake_repo(tmp_path)
    ok, lines = cps.run_group_a(cps.Path(root), VERSION)
    assert ok, "\n".join(lines)
    # Every file-backed source resolved to the shipped version (git tag is n/a
    # in a non-git tmp tree, which is not a failure).
    assert all("MISMATCH" not in line for line in lines)


@pytest.mark.parametrize(
    "drift_key",
    ["pyproject", "init", "readme_pin", "readme_input", "changelog", "llms"],
)
def test_group_a_fails_on_single_source_drift(tmp_path, drift_key):
    """A drift in ANY single in-repo surface must fail group A -- this is the
    whole point of the gate: a publisher signal cannot hide a stale source."""
    root = _write_fake_repo(tmp_path, overrides={drift_key: "9.9.9"})
    ok, lines = cps.run_group_a(cps.Path(root), VERSION)
    assert not ok, f"expected drift in {drift_key} to fail:\n" + "\n".join(lines)
    assert any("MISMATCH" in line for line in lines)


def test_group_a_git_tag_is_na_without_repo(tmp_path):
    """The git-tag source reports n/a (not a mismatch) when the tree is not a
    git checkout, so group A is clean pre-tag."""
    root = _write_fake_repo(tmp_path)
    rows = cps.collect_local_sources(cps.Path(root), VERSION)
    git_rows = [(label, val) for label, val in rows if "git tag" in label]
    assert git_rows and git_rows[0][1] is None


def test_read_changelog_top_skips_unreleased(tmp_path):
    root = _write_fake_repo(tmp_path)
    assert cps.read_changelog_top(cps.Path(root)) == VERSION


# --------------------------------------------------------------------------- #
# Group B: only the pure parsing helper, on a fixture JSON. No network.
# --------------------------------------------------------------------------- #
def _pypi_fixture(**over):
    doc = {
        "info": {
            "version": VERSION,
            "summary": "Find what broke in your agent calls. Pin it so it "
                       "never ships again. Local call forensics and "
                       "regression guards for AI agents.",
        },
        "urls": [{"filename": "hotato-1.15.1.tar.gz"}],
    }
    doc["info"].update(over.get("info", {}))
    if "urls" in over:
        doc["urls"] = over["urls"]
    return doc


def test_check_pypi_doc_clean_fixture_passes():
    assert cps.check_pypi_doc(_pypi_fixture(), VERSION) == []


def test_check_pypi_doc_flags_version_mismatch():
    doc = _pypi_fixture(info={"version": "1.14.0"})
    problems = cps.check_pypi_doc(doc, VERSION)
    assert any("info.version" in p for p in problems)


def test_check_pypi_doc_flags_empty_urls():
    doc = _pypi_fixture(urls=[])
    problems = cps.check_pypi_doc(doc, VERSION)
    assert any("urls" in p for p in problems)


def test_check_pypi_doc_flags_retired_overclaim():
    doc = _pypi_fixture(info={
        "summary": "everything you use a hosted platform for, on your machine. "
                   "find what broke in your agent calls",
    })
    problems = cps.check_pypi_doc(doc, VERSION)
    assert any("retired overclaim" in p for p in problems)


def test_check_pypi_doc_flags_missing_required_phrase():
    doc = _pypi_fixture(info={"summary": "a local tool for voice stuff"})
    problems = cps.check_pypi_doc(doc, VERSION)
    assert any("required phrase" in p for p in problems)


def test_check_release_json_and_llms_and_site_helpers():
    assert cps.check_release_json({"version": VERSION}, VERSION) == []
    assert cps.check_release_json({"version": "1.14.0"}, VERSION) != []

    assert cps.check_llms_text(f"...\n> Version {VERSION}\n", VERSION) == []
    assert cps.check_llms_text("no version line here", VERSION) != []


def test_check_site_html_positioning_markers():
    good = ("<title>hotato</title>"
            "<h1>Hotato: call forensics and regression guards for AI agents</h1>"
            "<p>changelog: 1.6.0, 1.7.0 released earlier</p>")
    assert cps.check_site_html(good, VERSION) == []

    # Old version strings elsewhere on the page are NOT a failure.
    assert "1.6.0" in good and cps.check_site_html(good, VERSION) == []

    missing_marker = "<title>hotato</title><h1>voice tools</h1>"
    assert cps.check_site_html(missing_marker, VERSION) != []

    stale_title = ("<title>hotato -- regression testing for voice agents</title>"
                   "<h1>Hotato: call forensics and regression guards for AI agents</h1>")
    problems = cps.check_site_html(stale_title, VERSION)
    assert any("<title>" in p for p in problems)
