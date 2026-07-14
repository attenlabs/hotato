"""Boundary-sensitivity / decision-margin gates (plan Mission 2).

Every event exposes, in its ADDITIVE measurements block, the onset the caller
requested, the quantized onset/yield frame the scorer landed on, and the signed
distance from the binding pass/fail bound. A near-miss (within one hop of
flipping) reads differently from a result comfortably inside the limit.

These assert through the PUBLIC envelope (core.run_single), because the fields
are derived in hotato's own layer -- the vendored _engine stays byte-identical
to upstream. Deterministic: thresholds are derived from the measured value.
"""
from __future__ import annotations

from hotato.core import run_single
from tests._trial_audio import write_stereo, yielding_call


def _measure(path, **kw):
    yielding_call(str(path), onset=2.0, total=6.0)
    env = run_single(stereo=str(path), onset_sec=2.0, expect="yield", **kw)
    return env["events"][0]["measurements"]


def test_onset_effective_equals_frame_index_times_hop(tmp_path):
    m = _measure(tmp_path / "y.wav", max_time_to_yield_sec=1.0)
    assert m["onset_frame_index"] is not None
    assert abs(m["onset_effective_sec"] - m["onset_frame_index"] * m["hop_sec"]) < 1e-9
    assert m["onset_requested_sec"] == 2.0          # supplied -> echoed
    assert m["yield_frame_index"] is not None        # this call yields


def test_onset_requested_null_when_autodetected(tmp_path):
    p = tmp_path / "auto.wav"
    yielding_call(str(p), onset=2.0, total=6.0)
    env = run_single(stereo=str(p), expect="yield", max_time_to_yield_sec=1.0)  # no onset_sec
    m = env["events"][0]["measurements"]
    assert m["onset_requested_sec"] is None          # auto-detected
    assert m["onset_frame_index"] is not None         # still a real quantized onset


def test_comfortable_bound_is_not_boundary_sensitive(tmp_path):
    m0 = _measure(tmp_path / "a.wav")
    ttoy = m0.get("caller_onset_sec") is not None and None
    # score once to learn the measured time-to-yield, then set a far bound
    base = _measure(tmp_path / "b.wav", max_time_to_yield_sec=5.0)
    yielded = base  # yields well within 5s
    m = _measure(tmp_path / "c.wav", max_time_to_yield_sec=5.0)
    assert m["boundary_sensitive"] is False
    assert m["decision_margin_sec"] > 0


def test_bound_on_the_measured_value_is_boundary_sensitive(tmp_path):
    base = _measure(tmp_path / "d.wav", max_time_to_yield_sec=1.0)
    env = run_single(stereo=str(tmp_path / "d.wav"), onset_sec=2.0, expect="yield")
    ttoy = env["events"][0]["verdict"]["seconds_to_yield"]
    m = run_single(stereo=str(tmp_path / "d.wav"), onset_sec=2.0, expect="yield",
                   max_time_to_yield_sec=ttoy)["events"][0]["measurements"]
    assert m["boundary_sensitive"] is True
    assert abs(m["decision_margin_hops"]) <= 1
    assert abs(m["decision_margin_sec"]) < 1e-9      # zero slack


def test_talk_over_bound_one_hop_is_boundary_sensitive(tmp_path):
    p = tmp_path / "e.wav"
    yielding_call(str(p), onset=2.0, total=6.0)
    env = run_single(stereo=str(p), onset_sec=2.0, expect="yield")
    talk = env["events"][0]["verdict"]["talk_over_sec"]
    hop = env["events"][0]["measurements"]["hop_sec"]
    m = run_single(stereo=str(p), onset_sec=2.0, expect="yield",
                   max_talk_over_sec=talk + hop)["events"][0]["measurements"]
    assert m["boundary_sensitive"] is True
    assert m["decision_margin_hops"] == 1


def test_tightest_binding_constraint_wins(tmp_path):
    p = tmp_path / "f.wav"
    yielding_call(str(p), onset=2.0, total=6.0)
    env = run_single(stereo=str(p), onset_sec=2.0, expect="yield")
    ttoy = env["events"][0]["verdict"]["seconds_to_yield"]
    talk = env["events"][0]["verdict"]["talk_over_sec"]
    # loose ttoy bound, tight talk-over bound -> talk-over margin is the tightest
    m = run_single(stereo=str(p), onset_sec=2.0, expect="yield",
                   max_time_to_yield_sec=ttoy + 1.0,
                   max_talk_over_sec=talk + 0.0)["events"][0]["measurements"]
    assert abs(m["decision_margin_sec"]) < 1e-9      # the zero-slack talk-over bound wins


def test_no_numeric_bound_margin_null(tmp_path):
    m = _measure(tmp_path / "g.wav")                 # no max_* bounds
    assert m["decision_margin_sec"] is None
    assert m["decision_margin_hops"] is None
    assert m["boundary_sensitive"] is False


def test_not_scorable_event_defaults_boundary_fields(tmp_path):
    # a silent caller channel -> not scorable -> boundary fields default null/false
    p = tmp_path / "silent.wav"
    write_stereo(str(p), caller_windows=[], agent_windows=[(0.2, 5.0)], total_sec=6.0)
    env = run_single(stereo=str(p), expect="yield")
    m = env["events"][0]["measurements"]
    assert m["onset_frame_index"] is None
    assert m["decision_margin_sec"] is None
    assert m["boundary_sensitive"] is False
