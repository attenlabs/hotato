"""M4 regression guard: the frozen bundled 8 must score identically to today.

The M4 example fixtures are ADDITIVE and live OUTSIDE the shipped package. This
guard pins the three barge-in signals (did_yield, seconds_to_yield, talk_over) for
every one of the 8 bundled scenarios, so any accidental perturbation of the bundled
audio, the vendored engine, or the default config is caught -- independently of the
signal-bus mirror check in test_signals.py. The bundled suite stays exactly 8 and
these numbers must never move.
"""

from hotato.core import run_suite

# did_yield, seconds_to_yield, talk_over_sec -- captured today, under the default
# ScoreConfig, for the frozen bundled battery.
FROZEN_8 = {
    "01-hard-interruption": (True, 0.5, 0.5),
    "02-backchannel-mhm": (False, None, 1.57),
    "03-filler-start": (True, 0.65, 0.56),
    "04-correction": (True, 0.5, 0.5),
    "05-telephony-8khz": (True, 0.5, 0.5),
    "06-double-talk": (True, 1.05, 1.05),
    "07-echo-bleed": (False, None, 3.0),
    "08-rapid-turn-taking": (True, 0.5, 0.5),
}


def test_bundled_suite_is_exactly_eight_and_all_pass():
    env = run_suite(suite="barge-in")
    assert env["summary"]["events"] == 8
    assert env["summary"]["passed"] == 8
    assert env["summary"]["failed"] == 0
    assert env["funnel"] is None            # the frozen suite must not trip the funnel


def test_frozen_barge_in_signals_unchanged():
    env = run_suite(suite="barge-in")
    by = {e["scenario_id"]: e for e in env["events"]}
    assert set(by) == set(FROZEN_8), "the bundled scenario set changed"
    for sid, (did_yield, ttoy, talk_over) in FROZEN_8.items():
        v = by[sid]["verdict"]
        assert v["did_yield"] == did_yield, sid
        assert v["seconds_to_yield"] == ttoy, sid
        assert v["talk_over_sec"] == talk_over, sid
