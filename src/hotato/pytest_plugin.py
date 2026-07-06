"""Pytest integration: a ``hotato_score`` fixture and an opt-in suite gate.

Loaded automatically via the ``pytest11`` entry point when hotato is installed
(or explicitly with ``-p hotato.pytest_plugin``). Inert unless used: it adds
one fixture and three flags and nothing else, so a plain pytest run behaves
identically.

Fixture, for asserting on real measurements inside your own tests:

    def test_call_yields(hotato_score):
        env = hotato_score(stereo="call.wav", expect="yield")
        assert env["summary"]["regression"] is False
        assert env["events"][0]["verdict"]["seconds_to_yield"] < 1.0

Session gate, for CI:

    pytest --hotato-suite

runs the bundled battery after your tests and fails the session (exit 1) on a
regression, printing the failing events and their fix classes. Point
``--hotato-suite-scenarios`` / ``--hotato-suite-audio`` at your own labelled
set to gate on it instead of the bundled fixtures.

Depends on nothing beyond pytest itself; the scoring core stays stdlib-only.
"""

from __future__ import annotations

import pytest

_SUITE_DEFAULT = "barge-in"


def pytest_addoption(parser):
    group = parser.getgroup("hotato", "hotato turn-taking eval")
    group.addoption(
        "--hotato-suite",
        action="store",
        nargs="?",
        const=_SUITE_DEFAULT,
        default=None,
        dest="hotato_suite",
        metavar="SUITE",
        help="after the test session, run the hotato battery and fail the "
             f"session (exit 1) on a regression (default suite: {_SUITE_DEFAULT!r})",
    )
    group.addoption(
        "--hotato-suite-scenarios",
        action="store",
        default=None,
        dest="hotato_suite_scenarios",
        metavar="DIR",
        help="directory of scenario JSON labels for --hotato-suite "
             "(defaults to the bundled battery)",
    )
    group.addoption(
        "--hotato-suite-audio",
        action="store",
        default=None,
        dest="hotato_suite_audio",
        metavar="DIR",
        help="directory of scenario audio for --hotato-suite "
             "(defaults to the bundled fixtures)",
    )


@pytest.fixture
def hotato_score():
    """Score a recording (or a battery) and return the hotato JSON envelope.

    Same inputs as the CLI: ``hotato_score(stereo="call.wav", expect="yield")``
    or ``hotato_score(caller="c.wav", agent="a.wav")`` or
    ``hotato_score(suite="barge-in")``. Assert on the returned envelope; this
    helper never asserts for you, so the test states its own expectation.
    """
    from .core import run_single, run_suite

    def _score(suite=None, **kwargs):
        if suite:
            return run_suite(suite=suite, **kwargs)
        return run_single(**kwargs)

    return _score


def pytest_terminal_summary(terminalreporter, exitstatus, config):
    suite = config.getoption("hotato_suite")
    if not suite:
        return
    from .core import run_suite

    tr = terminalreporter
    tr.section(f"hotato suite: {suite}")
    try:
        env = run_suite(
            suite=suite,
            scenarios_dir=config.getoption("hotato_suite_scenarios"),
            audio_dir=config.getoption("hotato_suite_audio"),
        )
    except (ValueError, FileNotFoundError) as exc:
        tr.write_line(f"hotato: could not run the suite: {exc}")
        _fail_session(tr, config)
        return
    s = env["summary"]
    tr.write_line(f"{s['passed']} of {s['events']} events pass "
                  f"(failed={s['failed']})")
    for e in env["events"]:
        v = e["verdict"]
        if not v["passed"]:
            fx = e.get("fix") or {}
            fc = f" fix[{fx.get('fix_class')}]" if fx else ""
            tr.write_line(f"FAIL {e['event_id']}:{fc} "
                          f"{'; '.join(v.get('reasons') or [])}")
    if s["failed"]:
        tr.write_line("hotato: regression detected; failing the session.")
        _fail_session(tr, config)


def _fail_session(terminalreporter, config) -> None:
    """Mark the session failed (exit 1). ``wrap_session`` returns
    ``session.exitstatus`` after ``pytest_sessionfinish`` (which drives this
    summary hook), so setting it here sticks."""
    session = getattr(terminalreporter, "_session", None)
    # Escalate a clean (or empty) session to 1; never mask a worse status
    # (2 interrupted / 3 internal error / 4 usage error).
    if session is not None and session.exitstatus in (0, 1, 5):
        session.exitstatus = 1
