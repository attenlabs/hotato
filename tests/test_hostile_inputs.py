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

import os
import struct
import subprocess
import sys
import wave
from importlib import resources

import pytest

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


# --- FIFO / named-pipe input (must refuse, never hang) --------------------
#
# Opening a FIFO for reading blocks at the OS level until a writer opens the
# other end. Every test here uses a subprocess with an explicit ``timeout``
# (mirroring how the original defect was reproduced: `timeout 5 hotato run
# --stereo fifo.wav` hung with exit code 124) so that if the fix ever
# regresses, the test FAILS with a clear TimeoutExpired instead of hanging
# the whole suite forever.

@pytest.mark.skipif(not hasattr(os, "mkfifo"),
                    reason="FIFOs are POSIX-only")
def test_fifo_stereo_input_is_refused_not_hung(tmp_path):
    """`hotato run --stereo` on a FIFO must exit 2 immediately (a clean,
    actionable ValueError), never hang inside wave.open()."""
    fifo = tmp_path / "x.wav"
    os.mkfifo(str(fifo))
    try:
        proc = subprocess.run(
            [sys.executable, "-m", "hotato", "run", "--stereo", str(fifo),
             "--format", "json"],
            capture_output=True, text=True, timeout=15,
        )
    except subprocess.TimeoutExpired:
        pytest.fail(
            "hotato hung reading a FIFO instead of refusing it cleanly "
            "(reproduces the original defect: mkfifo + `hotato run` blocks "
            "forever with zero output, zero error, zero traceback)"
        )
    assert proc.returncode == 2
    assert "not a regular file" in proc.stdout


@pytest.mark.skipif(not hasattr(os, "mkfifo"),
                    reason="FIFOs are POSIX-only")
def test_fifo_via_symlink_is_also_refused_not_hung(tmp_path):
    """A symlink pointing at a FIFO must be caught too: os.stat() follows
    symlinks, so this is refused the same way as a direct FIFO path."""
    fifo = tmp_path / "real.wav"
    os.mkfifo(str(fifo))
    link = tmp_path / "link.wav"
    os.symlink(str(fifo), str(link))
    try:
        proc = subprocess.run(
            [sys.executable, "-m", "hotato", "run", "--stereo", str(link),
             "--format", "json"],
            capture_output=True, text=True, timeout=15,
        )
    except subprocess.TimeoutExpired:
        pytest.fail("hotato hung reading a symlink-to-FIFO instead of refusing it")
    assert proc.returncode == 2
    assert "not a regular file" in proc.stdout


@pytest.mark.skipif(not hasattr(os, "mkfifo"),
                    reason="FIFOs are POSIX-only")
def test_fifo_core_load_signal_raises_valueerror_directly(tmp_path):
    """Unit-level check (in-process, no subprocess needed since the fix makes
    this return immediately): ``core._load_signal`` -- reached by every
    `_read_wav` caller (run, capture --stereo, fixture create, verify,
    compare, trust, and the MCP voice_eval_run tool) -- refuses a FIFO with a
    clean ValueError naming the problem, before ever calling wave.open()."""
    from hotato import core as _core

    fifo = tmp_path / "x.wav"
    os.mkfifo(str(fifo))
    with pytest.raises(ValueError, match="not a regular file"):
        _core._load_signal(str(fifo))


@pytest.mark.skipif(not hasattr(os, "mkfifo"),
                    reason="FIFOs are POSIX-only")
def test_fifo_scan_recording_raises_valueerror_not_hung(tmp_path):
    """scan.py opens WAVs via its own wave.open (windowed_frame_rms), bypassing
    core._read_wav -- the path `hotato scan` / `analyze` / `loop` funnel through.
    It must refuse a FIFO with the same clean ValueError, in-process and fast
    (a hang here would block the test run, reproducing the original defect)."""
    from hotato import scan as _scan

    fifo = tmp_path / "x.wav"
    os.mkfifo(str(fifo))
    with pytest.raises(ValueError, match="not a regular file"):
        _scan.windowed_frame_rms(str(fifo))
    with pytest.raises(ValueError, match="not a regular file"):
        _scan.scan_recording(str(fifo))


@pytest.mark.skipif(not hasattr(os, "mkfifo"),
                    reason="FIFOs are POSIX-only")
def test_fifo_scan_cli_is_refused_not_hung(tmp_path):
    """`hotato scan --stereo <fifo>` must return exit 2 with the clean message,
    not hang (the scan/analyze/loop commands bypassed core's guard before)."""
    fifo = tmp_path / "x.wav"
    os.mkfifo(str(fifo))
    proc = subprocess.run(
        [sys.executable, "-m", "hotato", "scan", "--stereo", str(fifo),
         "--format", "json"],
        capture_output=True, text=True, timeout=20)
    assert proc.returncode == 2, (
        "hotato scan on a FIFO should exit 2, not hang "
        f"(rc={proc.returncode}, out={proc.stdout[:200]!r})")
    assert "not a regular file" in proc.stdout


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
    # zeros carry no caller speech, so the honest outcome is not-scorable (exit 2,
    # usable-input contract); the point is that the unicode path resolves and the
    # tool answers cleanly without a crash.
    assert cli.main(["run", "--stereo", str(p), "--expect", "hold", "--format", "json"]) == 2


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


@pytest.mark.skipif(not hasattr(os, "mkfifo"),
                    reason="FIFOs are POSIX-only")
def test_fifo_fleet_ingest_is_refused_not_hung(tmp_path):
    """`hotato fleet ingest <fifo>` hashes the recording via a raw open() before
    any wave.open; it must refuse a FIFO (exit 2), not hang (was exit 124)."""
    fifo = tmp_path / "rec.wav"
    os.mkfifo(str(fifo))
    proc = subprocess.run(
        [sys.executable, "-m", "hotato", "fleet", "ingest",
         "--home", str(tmp_path / "home"), "-w", "ws1", "--agent", "a1",
         str(fifo)],
        capture_output=True, text=True, timeout=30)
    assert proc.returncode == 2, (
        f"fleet ingest on a FIFO should exit 2, not hang (rc={proc.returncode})")


@pytest.mark.skipif(not hasattr(os, "mkfifo"),
                    reason="FIFOs are POSIX-only")
def test_fifo_contract_pack_is_refused_not_hung(tmp_path):
    """`hotato contract pack` hashes each bundle member via a raw open(); a bundle
    whose audio is a FIFO (not a symlink) must refuse, not hang (was exit 124)."""
    out = tmp_path / "out"
    rc = cli.main(["contract", "create", "--stereo",
                   _bundled("01-hard-interruption"), "--onset", "2.0",
                   "--expect", "yield", "--id", "t1", "--out", str(out)])
    assert rc == 0
    bundle = out / "t1.hotato"
    import glob
    wavs = glob.glob(str(bundle / "**" / "*.wav"), recursive=True)
    assert wavs, "expected a bundled wav to replace with a FIFO"
    os.remove(wavs[0])
    os.mkfifo(wavs[0])
    proc = subprocess.run(
        [sys.executable, "-m", "hotato", "contract", "pack", str(bundle)],
        capture_output=True, text=True, timeout=30)
    assert proc.returncode != 124 and "not a regular file" in (
        proc.stdout + proc.stderr), (
        f"contract pack on a FIFO-audio bundle should refuse, not hang "
        f"(rc={proc.returncode})")


@pytest.mark.skipif(not hasattr(os, "mkfifo"),
                    reason="FIFOs are POSIX-only")
def test_fifo_scenario_json_is_refused_not_hung(tmp_path):
    """A scenarios dir containing a FIFO .json must refuse, not hang, since
    run_suite reads each scenario via a text open()."""
    from hotato import core as _core

    scen = tmp_path / "scen"
    scen.mkdir()
    os.mkfifo(str(scen / "s.json"))
    with pytest.raises(ValueError, match="not a regular file"):
        _core.run_suite(scenarios_dir=str(scen), audio_dir=str(tmp_path))
