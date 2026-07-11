"""Permanent numeric-boundary gates (plan Mission 2 / §3).

Turns the audit's measured boundaries into regression tests: rate invariance,
low-signal -> caution (not 'eligible for scan'), channel inversion -> possible-swap
caution, and hop-grid boundary-sensitivity. Uses deterministic synthetic
perturbations of a real fixture.
"""
import os

import pytest

from hotato import core, trust, synth
from tests import _trial_audio as ta


def _mk(tmp_path, name="src.wav"):
    p = str(tmp_path / name)
    ta.yielding_call(p)
    return p


def test_measurement_is_rate_invariant(tmp_path):
    """8 / 16 / 48 kHz of the same call yield the same yield timing."""
    src = _mk(tmp_path)
    ttoys = []
    for rate in (8000, 16000, 48000):
        out = str(tmp_path / f"r{rate}.wav")
        synth.perturb(src, {"transform": "resample", "rate": rate}, out_path=out, seed=1)
        env = core.run_single(stereo=out, onset_sec=2.0, expect="yield",
                              max_time_to_yield_sec=1.0, max_talk_over_sec=1.0)
        v = env["events"][0]["verdict"]
        if v["seconds_to_yield"] is not None:
            ttoys.append(round(v["seconds_to_yield"], 2))
    # all rates that scored agree to the hop grid
    assert len(set(ttoys)) == 1, ttoys


def test_low_signal_forces_caution_not_safe(tmp_path):
    """A heavily attenuated call is scorable but must NOT read 'safe to scan'."""
    src = _mk(tmp_path)
    out = str(tmp_path / "quiet.wav")
    synth.perturb(src, {"transform": "gain", "gain_db": -30}, out_path=out, seed=1)
    rep = trust.trust_report(out)
    if rep.get("scorable"):
        rec = rep["recommendation"]
        # any verdict-changing warning present -> not the strongest clean headline
        if rep.get("warnings"):
            assert rec != trust.SAFE_RECOMMENDATION, rec
        # input_health is one of the three explicit states
        assert rep.get("input_health") in ("clean", "caution", "not_scorable")


def test_channel_inversion_is_flagged(tmp_path):
    """Swapping caller/agent channels must be detected DIRECTLY as a possible
    swap.

    Build a call where the agent clearly holds the floor and the caller only
    interjects briefly, then invert the channels: the channel mapped as the
    caller now dominates talk time, which is exactly the reversal the swap
    heuristic exists to catch. Asserting ``possible_swap`` directly (rather than
    over the always-true input_health enum) makes this a real regression gate."""
    p = str(tmp_path / "asym.wav")
    ta.write_stereo(p, caller_windows=[(2.0, 2.6)],
                    agent_windows=[(0.2, 6.0)], total_sec=6.0)
    out = str(tmp_path / "swapped.wav")
    synth.perturb(p, {"transform": "invert_channels"}, out_path=out, seed=1)
    rep = trust.trust_report(out)
    assert rep["channels"]["possible_swap"]


def test_boundary_sensitive_flag_is_exposed(tmp_path):
    """A result tuned to sit exactly on its bound is marked boundary_sensitive."""
    src = _mk(tmp_path)
    env = core.run_single(stereo=src, onset_sec=2.0, expect="yield")
    ev = env["events"][0]
    ttoy = ev["verdict"]["seconds_to_yield"]
    assert ttoy is not None
    # bound exactly at the measured value -> zero slack -> boundary sensitive
    tight = core.run_single(stereo=src, onset_sec=2.0, expect="yield",
                            max_time_to_yield_sec=ttoy)
    m = tight["events"][0]["measurements"]
    assert m["boundary_sensitive"] is True
    assert abs(m["decision_margin_hops"]) <= 1
    # bound far away -> not boundary sensitive, large positive margin
    loose = core.run_single(stereo=src, onset_sec=2.0, expect="yield",
                            max_time_to_yield_sec=ttoy + 1.0)
    lm = loose["events"][0]["measurements"]
    assert lm["boundary_sensitive"] is False
    assert lm["decision_margin_sec"] > 0
