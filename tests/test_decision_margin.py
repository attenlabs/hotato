"""Boundary-sensitivity / decision-margin gates (plan Mission 2).

These exercise the ADDITIVE per-event exposure of the onset frame the scorer
snapped to and the signed distance from the binding pass/fail threshold, so a
near-miss (within one frame hop of flipping) reads differently from a result
sitting comfortably inside the limit.

Deterministic: every case scores a real stereo WAV written by the shared
tests._trial_audio helpers, and thresholds are derived from the measured value
so the assertions hold regardless of exact frame counts.
"""
from __future__ import annotations

from hotato.core import run_single, run_suite
from hotato._engine.score import evaluate, score_stereo
from hotato._engine.audio import read_wav

from tests._trial_audio import yielding_call


def _score_yielding(path):
    """Write a clean yield call and score it. Returns the ScoreResult."""
    yielding_call(path, onset=2.0, total=6.0)
    sig = read_wav(str(path))
    return score_stereo(sig, caller_channel=0, agent_channel=1, caller_onset_sec=2.0)


# --- onset quantization -----------------------------------------------------

def test_onset_effective_equals_frame_index_times_hop(tmp_path):
    """The quantized onset actually used is exactly onset_frame_index * hop_sec."""
    result = _score_yielding(tmp_path / "yield.wav")
    assert result.onset_frame_index is not None
    assert result.onset_effective_sec == result.onset_frame_index * result.hop_sec
    # onset was supplied, so it is echoed back verbatim.
    assert result.onset_requested_sec == 2.0
    # this call yields, so a real yield frame is exposed.
    assert result.yield_frame_index is not None
    assert result.did_yield is True


def test_onset_requested_null_when_autodetected(tmp_path):
    """When no onset is supplied it is auto-detected; onset_requested_sec is null."""
    yielding_call(tmp_path / "auto.wav", onset=2.0, total=6.0)
    sig = read_wav(str(tmp_path / "auto.wav"))
    result = score_stereo(sig, caller_channel=0, agent_channel=1, caller_onset_sec=None)
    assert result.onset_requested_sec is None
    # the effective onset is still a real quantized frame time.
    assert result.onset_effective_sec == result.onset_frame_index * result.hop_sec


# --- decision margin --------------------------------------------------------

def test_comfortably_inside_limit_not_boundary_sensitive(tmp_path):
    """A generous bound leaves a large positive margin and is NOT boundary-sensitive."""
    result = _score_yielding(tmp_path / "comfortable.wav")
    ttoy = result.time_to_yield_sec
    assert ttoy is not None
    # A full second of slack past the measured yield -> ~100 hops of margin.
    verdict = evaluate(result, {"yield": True, "max_time_to_yield_sec": ttoy + 1.0})
    assert verdict.passed is True
    assert verdict.decision_margin_sec is not None
    assert verdict.decision_margin_sec > 0.5          # large positive slack
    assert verdict.decision_margin_hops is not None
    assert verdict.decision_margin_hops > 1
    assert verdict.boundary_sensitive is False


def test_within_one_hop_is_boundary_sensitive(tmp_path):
    """A bound tuned to the measured value leaves ~zero slack -> boundary-sensitive."""
    result = _score_yielding(tmp_path / "edge.wav")
    ttoy = result.time_to_yield_sec
    assert ttoy is not None
    # Bound == measured yield time: still passes (not strictly greater), but the
    # margin is 0.0s -> 0 hops -> within one hop of flipping.
    verdict = evaluate(result, {"yield": True, "max_time_to_yield_sec": ttoy})
    assert verdict.passed is True
    assert verdict.decision_margin_sec == 0.0
    assert verdict.decision_margin_hops == 0
    assert verdict.boundary_sensitive is True


def test_within_one_hop_via_talk_over_bound(tmp_path):
    """The talk_over bound is a binding constraint too; one hop of slack flips it."""
    result = _score_yielding(tmp_path / "over.wav")
    hop = result.hop_sec
    over = result.talk_over_sec
    # one hop of slack over the measured talk-over -> boundary-sensitive.
    verdict = evaluate(result, {"yield": True, "max_talk_over_sec": over + hop})
    assert verdict.passed is True
    assert verdict.decision_margin_hops == 1
    assert verdict.boundary_sensitive is True
    # two hops of slack -> no longer boundary-sensitive.
    verdict2 = evaluate(result, {"yield": True, "max_talk_over_sec": over + 2 * hop})
    assert verdict2.decision_margin_hops == 2
    assert verdict2.boundary_sensitive is False


def test_tightest_binding_constraint_wins(tmp_path):
    """With two bounds applying, the smallest slack (closest to flipping) is kept."""
    result = _score_yielding(tmp_path / "both.wav")
    hop = result.hop_sec
    ttoy = result.time_to_yield_sec
    over = result.talk_over_sec
    # ttoy bound is loose (1s slack); talk_over bound is tight (1 hop slack).
    verdict = evaluate(
        result,
        {
            "yield": True,
            "max_time_to_yield_sec": ttoy + 1.0,
            "max_talk_over_sec": over + hop,
        },
    )
    # the tight talk_over margin (~1 hop) must be the one reported.
    assert verdict.decision_margin_hops == 1
    assert verdict.boundary_sensitive is True


def test_no_numeric_bound_margin_null(tmp_path):
    """Pure yield expectation (no numeric bound) -> null margin, not sensitive."""
    result = _score_yielding(tmp_path / "pure.wav")
    verdict = evaluate(result, {"yield": True})
    assert verdict.passed is True
    assert verdict.decision_margin_sec is None
    assert verdict.decision_margin_hops is None
    assert verdict.boundary_sensitive is False


# --- envelope wiring (core.py measurements block) ---------------------------

def test_measurements_block_carries_boundary_keys(tmp_path):
    """run_single surfaces the new keys in the event's measurements block."""
    path = tmp_path / "single.wav"
    yielding_call(path, onset=2.0, total=6.0)
    env = run_single(
        stereo=str(path),
        onset_sec=2.0,
        expect="yield",
        max_time_to_yield_sec=3.0,
    )
    m = env["events"][0]["measurements"]
    for key in (
        "onset_requested_sec",
        "onset_frame_index",
        "onset_effective_sec",
        "yield_frame_index",
        "decision_margin_sec",
        "decision_margin_hops",
        "boundary_sensitive",
    ):
        assert key in m, f"missing measurement key {key!r}"
    # the equality that lets a reader re-derive the quantized onset holds through
    # the envelope too.
    assert m["onset_effective_sec"] == m["onset_frame_index"] * m["hop_sec"]
    assert m["onset_requested_sec"] == 2.0
    # generous 3s bound -> comfortably inside, not boundary-sensitive.
    assert m["boundary_sensitive"] is False
    assert m["decision_margin_sec"] > 0.5


def test_single_edge_case_flags_boundary_sensitive(tmp_path):
    """Tuning the bound to the measured yield makes the envelope flag it sensitive."""
    path = tmp_path / "single_edge.wav"
    yielding_call(path, onset=2.0, total=6.0)
    # measure the yield first with the same default config the CLI uses.
    sig = read_wav(str(path))
    result = score_stereo(sig, caller_channel=0, agent_channel=1, caller_onset_sec=2.0)
    env = run_single(
        stereo=str(path),
        onset_sec=2.0,
        expect="yield",
        max_time_to_yield_sec=result.time_to_yield_sec,
    )
    m = env["events"][0]["measurements"]
    assert m["decision_margin_hops"] == 0
    assert m["boundary_sensitive"] is True
    assert env["events"][0]["verdict"]["passed"] is True


# --- placeholder defaults (not-scorable event) ------------------------------

def test_missing_audio_placeholder_defaults_null(tmp_path):
    """The missing-audio placeholder defaults every boundary key to null/false."""
    scen_dir = tmp_path / "scenarios"
    audio_dir = tmp_path / "audio"
    scen_dir.mkdir()
    audio_dir.mkdir()  # deliberately empty -> the referenced wav is absent
    import json

    (scen_dir / "gap.json").write_text(
        json.dumps({"id": "gap", "title": "no audio", "expected": {"yield": True}})
    )
    env = run_suite(
        suite="barge-in",
        scenarios_dir=str(scen_dir),
        audio_dir=str(audio_dir),
    )
    evt = env["events"][0]
    assert evt.get("scorable") is False
    m = evt["measurements"]
    assert m["onset_requested_sec"] is None
    assert m["onset_frame_index"] is None
    assert m["onset_effective_sec"] is None
    assert m["yield_frame_index"] is None
    assert m["decision_margin_sec"] is None
    assert m["decision_margin_hops"] is None
    assert m["boundary_sensitive"] is False
