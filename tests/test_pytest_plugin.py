"""P9: the pytest integration. The `hotato_score` fixture returns the real
envelope for assertions, `--hotato-suite` gates the session (exit 1 on a real
regression), and the plugin is inert when unused: a plain pytest run behaves
identically with the plugin loaded.

The plugin is loaded explicitly with -p (the pytest11 entry point takes over
once hotato is pip-installed; the module is identical either way).
"""

from importlib import resources

pytest_plugins = ["pytester"]

# Block the pytest11 entry point by name, then load the module explicitly.
# Identical behavior whether hotato is pip-installed (entry point present,
# blocked, module loaded once) or run from the dev tree (no entry point,
# module loaded once). Avoids pluggy double-registration either way.
_PLUGIN = ("-p", "no:hotato", "-p", "hotato.pytest_plugin")

def _bundled(sid):
    return str(
        resources.files("hotato").joinpath("data", "audio", sid + ".example.wav")
    )


def test_fixture_scores_a_recording(pytester):
    wav = _bundled("01-hard-interruption")
    pytester.makepyfile(f"""
        def test_call_yields(hotato_score):
            env = hotato_score(stereo={wav!r}, expect="yield")
            assert env["tool"] == "hotato"
            assert env["summary"]["regression"] is False
            v = env["events"][0]["verdict"]
            assert v["did_yield"] is True
            assert 0.0 < v["seconds_to_yield"] < 1.0
    """)
    result = pytester.runpytest(*_PLUGIN)
    result.assert_outcomes(passed=1)


def test_fixture_scores_the_bundled_battery(pytester):
    pytester.makepyfile("""
        def test_battery(hotato_score):
            env = hotato_score(suite="barge-in")
            assert env["summary"]["events"] == 8
            assert env["summary"]["failed"] == 0
    """)
    result = pytester.runpytest(*_PLUGIN)
    result.assert_outcomes(passed=1)


def test_fixture_lets_the_test_assert_a_regression(pytester):
    wav = _bundled("01-hard-interruption")
    pytester.makepyfile(f"""
        def test_too_slow(hotato_score):
            env = hotato_score(stereo={wav!r}, expect="yield",
                               max_time_to_yield_sec=0.0)
            # the impossible bound makes this a REAL failing verdict
            assert env["summary"]["regression"] is True
            assert env["exit_code"] == 1
    """)
    result = pytester.runpytest(*_PLUGIN)
    result.assert_outcomes(passed=1)


def test_plain_run_with_plugin_loaded_is_untouched(pytester):
    pytester.makepyfile("""
        def test_nothing_to_do_with_hotato():
            assert 1 + 1 == 2
    """)
    result = pytester.runpytest(*_PLUGIN)
    result.assert_outcomes(passed=1)
    assert result.ret == 0
    # no hotato output of any kind without the flag
    assert "hotato suite" not in result.stdout.str()


def test_hotato_suite_flag_passes_on_green_battery(pytester):
    pytester.makepyfile("""
        def test_trivial():
            assert True
    """)
    result = pytester.runpytest(*_PLUGIN, "--hotato-suite")
    assert result.ret == 0
    result.stdout.fnmatch_lines(["*hotato suite: barge-in*",
                                 "*8 of 8 events pass*"])


def test_hotato_suite_flag_fails_session_on_insufficient_coverage(pytester, tmp_path):
    # An empty audio dir makes every battery scenario NOT-SCORABLE (a missing
    # audio file is an input problem, never a measured miss). The gate must still
    # fail the session (exit 1) -- scoring nothing is a setup failure -- but
    # HONESTLY, as insufficient coverage, never as a fabricated "regression".
    empty = tmp_path / "no-audio"
    empty.mkdir()
    pytester.makepyfile("""
        def test_trivial():
            assert True
    """)
    result = pytester.runpytest(*_PLUGIN, "--hotato-suite",
                                "--hotato-suite-audio", str(empty))
    assert result.ret == 1
    result.stdout.fnmatch_lines(["*no scorable events*"])
    # a missing file must NOT be reported as a regression
    assert not any("regression detected" in ln
                   for ln in result.stdout.lines)
    # the tests themselves still passed; the gate is what failed the session
    result.assert_outcomes(passed=1)


def test_hotato_suite_unknown_suite_fails_session(pytester):
    pytester.makepyfile("""
        def test_trivial():
            assert True
    """)
    result = pytester.runpytest(*_PLUGIN, "--hotato-suite", "not-a-suite")
    assert result.ret == 1
    result.stdout.fnmatch_lines(["*could not run the suite*"])
