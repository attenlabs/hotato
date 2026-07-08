"""M0 correctness + determinism gates for the bundled battery and envelope.

These run with zero external files and zero third-party deps: they exercise the
package exactly as a fresh `uvx` install would.
"""

import json

import pytest

from hotato.core import LIMITS, run_single, run_suite

REQUIRED_TOP_LEVEL = {
    "tool", "schema_version", "mode", "stack", "offline", "engine",
    "limits", "summary", "events", "fix_map", "funnel", "exit_code",
}


def _bundled_stereo_path(scenario_id):
    from importlib import resources
    return str(
        resources.files("hotato").joinpath(
            "data", "audio", scenario_id + ".example.wav"
        )
    )


def test_bundled_suite_all_pass():
    env = run_suite(suite="barge-in")
    assert env["summary"]["events"] == 8
    assert env["summary"]["failed"] == 0
    assert env["summary"]["passed"] == 8
    assert env["summary"]["regression"] is False
    assert env["exit_code"] == 0


def test_envelope_schema_shape():
    env = run_suite(suite="barge-in")
    assert REQUIRED_TOP_LEVEL.issubset(env.keys())
    assert env["tool"] == "hotato"
    assert env["schema_version"] == "1"
    assert env["offline"] is True
    for e in env["events"]:
        assert {"event_id", "expected_yield", "verdict", "measurements"}.issubset(e.keys())


def test_hard_negatives_hold():
    """02-backchannel and 07-echo-bleed are should_not_yield: the agent must NOT
    yield, and the event must still PASS. This is the proof the scorer measures
    real floor-taking, not any overlap."""
    env = run_suite(suite="barge-in")
    by_id = {e["scenario_id"]: e for e in env["events"]}
    for sid in ("02-backchannel-mhm", "07-echo-bleed"):
        e = by_id[sid]
        assert e["expected_yield"] is False, sid
        assert e["verdict"]["did_yield"] is False, sid
        assert e["verdict"]["passed"] is True, sid


def test_accuracy_claim_is_null():
    """The honesty invariant: no accuracy percentage is ever claimed."""
    assert LIMITS["accuracy_claim"] is None
    env = run_suite(suite="barge-in")
    assert env["limits"]["accuracy_claim"] is None


def test_determinism_two_runs_byte_identical():
    a = json.dumps(run_suite(suite="barge-in"), sort_keys=True)
    b = json.dumps(run_suite(suite="barge-in"), sort_keys=True)
    assert a == b


def test_numpy_and_stdlib_decode_agree():
    """The optional numpy WAV-decode path must produce the same result as the
    pure-stdlib path, so a published number never depends on whether numpy is
    installed."""
    from hotato._engine import audio as A

    with_numpy = json.dumps(run_suite(suite="barge-in"), sort_keys=True)
    saved = A._np
    A._np = None  # force the stdlib decode path
    try:
        without_numpy = json.dumps(run_suite(suite="barge-in"), sort_keys=True)
    finally:
        A._np = saved
    assert with_numpy == without_numpy


def test_numpy_and_stdlib_agree_on_fuzzed_random_wavs(tmp_path):
    """Defect (round 3): the bundled suite happens to round the same way on its
    fixed fixtures, which gives false confidence that the numpy and stdlib RMS
    paths are identical everywhere. numpy's np.mean uses pairwise summation while
    the stdlib path accumulates sequentially, so they can disagree in the last
    bit. This fuzzes many random 2-channel WAVs (varied amplitudes down to the
    16-bit quantization floor) and asserts the FULL exposed envelope -- every
    surfaced, rounded number and the verdict -- is byte-identical with numpy on
    vs forced off. If a future precision change ever breaks the rounded-output
    invariant at a threshold boundary, this catches it instead of the fixed
    suite silently masking it."""
    import random

    from hotato._engine import audio as A
    from hotato._engine.audio import write_wav

    saved = A._np
    if saved is None:
        pytest.skip("numpy not installed; nothing to compare against")

    sr = 16000
    rng = random.Random(20260708)
    paths = []
    for k in range(40):
        n = rng.randint(12000, 40000)
        amp = rng.choice([1.0, 0.5, 0.05, 1.0 / 32768.0, 3.0 / 32768.0])
        caller = [amp * rng.uniform(-1, 1) for _ in range(n)]
        agent = [amp * rng.uniform(-1, 1) for _ in range(n)]
        p = tmp_path / f"fuzz-{k}.wav"
        write_wav(str(p), sr, [caller, agent])
        paths.append(str(p))

    def _envs():
        out = []
        for p in paths:
            for expect in ("yield", "hold"):
                env = run_single(stereo=p, expect=expect, onset_sec=0.3)
                out.append(json.dumps(env, sort_keys=True))
        return out

    with_numpy = _envs()
    try:
        A._np = None  # force the pure-stdlib decode + RMS path
        without_numpy = _envs()
    finally:
        A._np = saved
    assert without_numpy == with_numpy


def test_regression_sets_exit_code_and_fix():
    """A failing verdict must flip exit_code to 1, mark regression, and attach a
    fix. Score a real interruption fixture but demand an impossible latency."""
    env = run_single(
        stereo=_bundled_stereo_path("01-hard-interruption"),
        onset_sec=None,
        expect="yield",
        stack="livekit",
        max_time_to_yield_sec=0.0,  # impossible -> must fail
    )
    assert env["exit_code"] == 1
    assert env["summary"]["regression"] is True
    assert len(env["fix_map"]) >= 1
    assert env["fix_map"][0]["fix_class"] in ("config", "engagement-control")


def test_mono_stereo_file_rejected():
    """A single-channel file passed as --stereo must raise, never silently
    mis-score as if it had separated channels."""
    import wave
    import struct
    import tempfile
    import os

    tmp = tempfile.NamedTemporaryFile(suffix=".wav", delete=False)
    tmp.close()
    try:
        with wave.open(tmp.name, "wb") as w:
            w.setnchannels(1)
            w.setsampwidth(2)
            w.setframerate(16000)
            w.writeframes(struct.pack("<" + "h" * 1600, *([0] * 1600)))
        with pytest.raises(ValueError):
            run_single(stereo=tmp.name, expect="yield")
    finally:
        os.unlink(tmp.name)
