"""Package copy-lint (``scripts/copy_lint.py``): the shipped README, PyPI
summary, MCP/server descriptions, llms.txt, docs/*.md, CHANGELOG's
[Unreleased] section, and the report/card renderers that emit user-facing
claim text carry no unqualified overclaim phrase from the words-to-reserve
table (GPT design audit P0.1). See ``scripts/copy_lint.py`` for the exact
list and the "unqualified" rule.
"""

import importlib.util
import os

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _load_copy_lint():
    spec = importlib.util.spec_from_file_location(
        "copy_lint", os.path.join(ROOT, "scripts", "copy_lint.py")
    )
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_package_copy_is_clean():
    copy_lint = _load_copy_lint()
    hits = copy_lint.run()
    assert not hits, "unqualified overclaim phrase(s) in shipped copy:\n" + "\n".join(hits)


def test_copy_lint_scans_a_real_amount_of_content():
    # A guard against the lint silently scanning nothing (e.g. a path typo
    # in _PY_TARGETS / _FLAT_TARGETS) and passing "clean" vacuously.
    copy_lint = _load_copy_lint()
    targets = copy_lint.collect_targets()
    assert len(targets) >= 20
    total_chars = sum(len(text) for _, text in targets)
    assert total_chars > 50_000


def test_copy_lint_catches_an_unqualified_banned_phrase():
    copy_lint = _load_copy_lint()
    hits = copy_lint._scan_text(
        "synthetic", "This release ships a verified fix for the barge-in bug.")
    assert hits and "verified fix" in hits[0]


def test_copy_lint_does_not_flag_a_negated_or_quoted_mention():
    copy_lint = _load_copy_lint()
    # The exact style docs/CARDS.md and src/hotato/card.py use: naming the
    # banned phrase to say it is never rendered.
    hits = copy_lint._scan_text(
        "synthetic",
        'The card reads "PAIRED EVIDENCE IMPROVED", never "fix verified".')
    assert hits == []


def test_copy_lint_scans_only_the_unreleased_changelog_section():
    # A historical changelog entry (e.g. the 0.6.0 card description quoting
    # its own original "FIX VERIFIED WITHOUT BREAKING BACKCHANNELS" render)
    # is exempt; only [Unreleased], which is about to ship, is linted.
    copy_lint = _load_copy_lint()
    text = (
        "## [Unreleased]\n"
        "### Added\n"
        "- nothing overclaimed here.\n"
        "## [0.1.0] - 2020-01-01\n"
        "### Added\n"
        "- a historical entry that says fix verified without qualifying it.\n"
    )
    section = copy_lint._unreleased_section(text)
    assert "fix verified" not in section
    assert "nothing overclaimed" in section
