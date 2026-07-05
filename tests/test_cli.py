"""M3: freeze the CLI exit-code contract and the --help honesty statement.

Exit codes: 0 = all pass (or --no-fail), 1 = a regression, 2 = usage/IO error.
These are the contract an agent and a CI job depend on, so they are pinned here.
"""

import struct
import wave
from importlib import resources

import pytest

from hotato import cli


def _bundled(sid):
    return str(
        resources.files("hotato").joinpath("data", "audio", sid + ".example.wav")
    )


def _write_wav(path, sample_rate, n_channels, n_frames):
    with wave.open(str(path), "wb") as w:
        w.setnchannels(n_channels)
        w.setsampwidth(2)
        w.setframerate(sample_rate)
        w.writeframes(struct.pack("<" + "h" * (n_frames * n_channels), *([0] * (n_frames * n_channels))))


def test_exit_0_on_passing_suite():
    assert cli.main(["run", "--suite", "barge-in", "--format", "json"]) == 0


def test_exit_1_on_regression():
    assert cli.main(["run", "--stereo", _bundled("01-hard-interruption"),
                     "--max-time-to-yield", "0.0"]) == 1


def test_no_fail_forces_exit_0_on_regression():
    assert cli.main(["run", "--stereo", _bundled("01-hard-interruption"),
                     "--max-time-to-yield", "0.0", "--no-fail"]) == 0


def test_exit_2_on_missing_file():
    assert cli.main(["run", "--stereo", "/nonexistent/definitely-not-here.wav"]) == 2


def test_exit_2_on_single_channel_as_stereo(tmp_path):
    mono = tmp_path / "mono.wav"
    _write_wav(mono, 16000, 1, 1600)
    assert cli.main(["run", "--stereo", str(mono)]) == 2


def test_exit_2_on_sample_rate_mismatch(tmp_path):
    a = tmp_path / "caller_16k.wav"
    b = tmp_path / "agent_8k.wav"
    _write_wav(a, 16000, 1, 1600)
    _write_wav(b, 8000, 1, 800)
    assert cli.main(["run", "--caller", str(a), "--agent", str(b)]) == 2


def test_exit_2_on_no_input():
    assert cli.main(["run"]) == 2


def test_help_states_offline_and_no_accuracy(capsys):
    with pytest.raises(SystemExit):
        cli.main(["run", "--help"])
    out = capsys.readouterr().out.lower()
    assert "offline" in out
    assert "no accuracy" in out or "no\naccuracy" in out


def test_top_level_help_states_no_accuracy(capsys):
    with pytest.raises(SystemExit):
        cli.main(["--help"])
    out = capsys.readouterr().out.lower()
    assert "no accuracy" in out
