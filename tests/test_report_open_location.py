"""Auto-opened reports must land where a confined browser can read them.

Regression guard: `hotato doctor` and `hotato demo` used to write their HTML
report into the system temp dir and then open file:///tmp/...; on Ubuntu the
default browser is a snap that cannot read /tmp, so the report opened to
"file not found" (and webbrowser.open still reported success, so no fallback
fired). Reports now default to the working directory, and _try_open stages any
browser-unreachable path under a non-hidden $HOME directory before opening.
"""

import os
import sys
import tempfile

import pytest

from hotato import cli


def _stub_browser(monkeypatch):
    """Never launch a real browser in tests; capture what would be opened."""
    opened = {}

    def fake_open(url):
        opened["url"] = url
        return True

    import webbrowser

    monkeypatch.setattr(webbrowser, "open", fake_open)
    return opened


def test_doctor_default_report_is_written_to_cwd(tmp_path, monkeypatch):
    # Old code wrote to a fixed gettempdir()/hotato-report.html regardless of
    # cwd; the fix writes into the working directory instead.
    _stub_browser(monkeypatch)
    monkeypatch.chdir(tmp_path)
    rc = cli.main(["doctor"])
    assert rc == 0
    assert (tmp_path / "hotato-report.html").is_file()


def test_browser_readable_target_passes_through_nonhidden_home_paths(tmp_path, monkeypatch):
    if not sys.platform.startswith("linux"):
        pytest.skip("staging only applies on linux")
    home = tmp_path / "home"
    (home / "proj").mkdir(parents=True)
    monkeypatch.setenv("HOME", str(home))
    report = home / "proj" / "hotato-report.html"
    report.write_text("<h1>ok</h1>")
    assert cli._browser_readable_target(str(report)) == os.path.abspath(str(report))


def test_browser_readable_target_stages_tempdir_paths_under_home(tmp_path, monkeypatch):
    if not sys.platform.startswith("linux"):
        pytest.skip("staging only applies on linux")
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    src = os.path.join(tempfile.gettempdir(), "hotato-report-stage-probe.html")
    with open(src, "w") as fh:
        fh.write("<h1>marker</h1>")
    try:
        target = cli._browser_readable_target(src)
        assert target != src
        real_home = os.path.realpath(str(home))
        assert os.path.realpath(target).startswith(real_home + os.sep)
        rel = os.path.relpath(os.path.realpath(target), real_home)
        assert not rel.split(os.sep)[0].startswith(".")  # readable: non-hidden
        with open(target) as fh:
            assert fh.read() == "<h1>marker</h1>"
    finally:
        os.remove(src)


def test_browser_readable_target_stages_hidden_home_paths(tmp_path, monkeypatch):
    # A snap browser also cannot read a top-level hidden dir like ~/.cache.
    if not sys.platform.startswith("linux"):
        pytest.skip("staging only applies on linux")
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    hidden = home / ".cache" / "hotato" / "r.html"
    hidden.parent.mkdir(parents=True)
    hidden.write_text("<h1>hid</h1>")
    target = cli._browser_readable_target(str(hidden))
    assert target != str(hidden)
    rel = os.path.relpath(os.path.realpath(target), os.path.realpath(str(home)))
    assert not rel.split(os.sep)[0].startswith(".")
