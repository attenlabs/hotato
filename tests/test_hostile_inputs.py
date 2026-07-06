"""Adversarial / production-hardening regression tests.

Every case here reproduces a real defect found by feeding the CLI the kind of
hostile, malformed, or conflicting input a skeptical engineer throws at a tool
before trusting it. The contract each one pins:

  * NEVER a Python traceback. ``cli.main`` must RETURN an exit code, not raise --
    so ``main(...) == 2`` is itself proof that no exception escaped.
  * A malformed / corrupt / truncated / non-WAV / empty file, an out-of-range
    channel, a negative onset, or a conflicting flag set is a *usage* error and
    must exit 2 (never 0, never 1, never a crash).
  * A truncated file must NOT masquerade as a real low score.

The honesty invariants and the frozen golden are covered elsewhere; these tests
only guard the input surface.
"""

import struct
import wave
from importlib import resources

from hotato import cli
from hotato.core import run_suite


# --- fixture helpers ------------------------------------------------------

def _write_valid_wav(path, sample_rate=16000, n_channels=2, n_frames=1600):
    with wave.open(str(path), "wb") as w:
        w.setnchannels(n_channels)
        w.setsampwidth(2)
        w.setframerate(sample_rate)
        w.writeframes(
            struct.pack("<" + "h" * (n_frames * n_channels), *([0] * (n_frames * n_channels)))
        )
    return str(path)


def _bundled(sid):
    return str(resources.files("hotato").joinpath("data", "audio", sid + ".example.wav"))


# --- corrupt / malformed files (each must be a clean exit 2) --------------

def test_exit_2_on_empty_wav(tmp_path):
    p = tmp_path / "empty.wav"
    p.write_bytes(b"")
    assert cli.main(["run", "--stereo", str(p)]) == 2


def test_exit_2_on_non_wav_file(tmp_path):
    p = tmp_path / "nota.wav"
    p.write_text("this is plainly not a WAV file\n" * 4)
    assert cli.main(["run", "--stereo", str(p)]) == 2


def test_exit_2_on_truncated_wav(tmp_path):
    """A header that declares far more frames than the data chunk holds must be
    rejected -- never silently scored as a (bogus) regression."""
    full = tmp_path / "full.wav"
    _write_valid_wav(full, n_frames=1600)
    raw = full.read_bytes()
    trunc = tmp_path / "trunc.wav"
    trunc.write_bytes(raw[:100])  # header intact, data chunk cut off
    assert cli.main(["run", "--stereo", str(trunc)]) == 2


def test_exit_2_on_odd_byte_wav(tmp_path):
    """A data chunk whose byte length is not a whole number of samples is corrupt
    and must fail cleanly (not raise array.frombytes' ValueError as a traceback)."""
    full = tmp_path / "full.wav"
    _write_valid_wav(full, n_frames=1600)
    raw = full.read_bytes()
    odd = tmp_path / "odd.wav"
    odd.write_bytes(raw[:-1])  # drop one byte -> odd-length data chunk
    assert cli.main(["run", "--stereo", str(odd)]) == 2


# --- out-of-range / negative channel indices ------------------------------

def test_exit_2_on_out_of_range_channel():
    assert cli.main(["run", "--stereo", _bundled("01-hard-interruption"),
                     "--caller-channel", "5"]) == 2


def test_exit_2_on_negative_channel():
    assert cli.main(["run", "--stereo", _bundled("01-hard-interruption"),
                     "--caller-channel", "-1"]) == 2


def test_exit_2_on_out_of_range_agent_channel():
    assert cli.main(["run", "--stereo", _bundled("01-hard-interruption"),
                     "--agent-channel", "9"]) == 2


# --- nonsensical / conflicting flags --------------------------------------

def test_exit_2_on_negative_onset():
    assert cli.main(["run", "--stereo", _bundled("01-hard-interruption"),
                     "--onset", "-5"]) == 2


def test_exit_2_on_suite_combined_with_stereo():
    assert cli.main(["run", "--suite", "--stereo", _bundled("01-hard-interruption")]) == 2


def test_exit_2_on_suite_combined_with_caller_agent():
    assert cli.main(["run", "--suite", "--caller", _bundled("01-hard-interruption"),
                     "--agent", _bundled("01-hard-interruption")]) == 2


def test_exit_2_on_unknown_stack():
    import pytest
    # argparse rejects an out-of-choices --stack with a usage error (SystemExit 2).
    with pytest.raises(SystemExit) as exc:
        cli.main(["run", "--stereo", _bundled("01-hard-interruption"), "--stack", "notreal"])
    assert exc.value.code == 2


# --- healthy paths are unaffected -----------------------------------------

def test_unicode_and_space_path_scores_cleanly(tmp_path):
    """A path with spaces + non-ASCII characters must score, not error."""
    p = tmp_path / "café recording ñ.wav"
    _write_valid_wav(p, n_frames=1600)
    # zeros never yield, so expect 'hold' to get a clean pass (exit 0); the point
    # is that the unicode path resolves and scores without a crash.
    assert cli.main(["run", "--stereo", str(p), "--expect", "hold", "--format", "json"]) == 0


def test_valid_channel_indices_still_pass():
    assert cli.main(["run", "--stereo", _bundled("01-hard-interruption"),
                     "--caller-channel", "0", "--agent-channel", "1"]) == 0


# --- honesty invariant on real emitted envelopes --------------------------

def test_accuracy_claim_is_null_in_every_output_shape():
    """The honesty invariant, checked on real outputs (not just the schema):
    limits.accuracy_claim is null for a passing single, a failing single, and the
    battery. No output path may ever populate an accuracy percentage."""
    from hotato.core import run_single

    envs = [
        run_suite(suite="barge-in"),
        run_single(stereo=_bundled("01-hard-interruption"), expect="yield"),
        run_single(stereo=_bundled("01-hard-interruption"), expect="yield",
                   stack="livekit", max_time_to_yield_sec=0.0),  # a failing event
    ]
    for env in envs:
        assert env["limits"]["accuracy_claim"] is None


# --- determinism: numpy-present vs numpy-absent parity --------------------

def test_numpy_absent_matches_numpy_present():
    """The optional numpy accelerator must never change a single number: the
    energy reference is the same with or without it. Force the pure-Python path
    and assert the suite envelope is byte-identical to the default path."""
    import json

    from hotato._engine import audio as _audio

    baseline = json.dumps(run_suite(suite="barge-in"), sort_keys=True)
    saved = _audio._np
    try:
        _audio._np = None  # force the stdlib-only RMS path
        pure = json.dumps(run_suite(suite="barge-in"), sort_keys=True)
    finally:
        _audio._np = saved
    assert pure == baseline
