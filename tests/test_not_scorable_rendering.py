"""Text rendering + process exit for not-scorable events, from an external
release review.

The envelope semantics (scorable: false, not_scorable_reason, summary counts,
process_exit_code mapping) are pinned in test_not_scorable.py. This file pins
what the USER SEES and what the PROCESS RETURNS across every CLI text path:

  * a not-scorable event renders as `[NOT SCORABLE] <id>` with its reason on
    the next line, never as `[FAIL]`;
  * the summary line gains `, not_scorable=N` when N > 0;
  * a single run whose every event is not scorable prints
    `process_exit_code=2` (never the misleading envelope `exit_code=0`) and
    the process exits 2, for run, doctor, and capture alike;
  * fully-scorable runs keep the exact `exit_code=` trailing line, so the
    documented terminal output stays valid;
  * JSON outputs are byte-untouched: the envelope keeps its schema-frozen
    exit_code 0|1 and carries scorable: false.
"""

import json
import math
import struct
import wave

import pytest

from hotato import cli


# --- deterministic synthetic fixture (same shape as test_not_scorable.py) ---

def _write_stereo(path, caller_segments, agent_segments, duration_sec=3.0, sr=16000):
    """Two-channel PCM WAV: caller on channel 0, agent on channel 1."""
    n = int(duration_sec * sr)

    def _on(segments, t):
        return any(start <= t < end for start, end in segments)

    frames = bytearray()
    for i in range(n):
        t = i / sr
        c = int(0.35 * 32767 * math.sin(2 * math.pi * 220.0 * i / sr)) if _on(caller_segments, t) else 0
        a = int(0.35 * 32767 * math.sin(2 * math.pi * 330.0 * i / sr)) if _on(agent_segments, t) else 0
        frames += struct.pack("<hh", c, a)
    with wave.open(str(path), "wb") as w:
        w.setnchannels(2)
        w.setsampwidth(2)
        w.setframerate(sr)
        w.writeframes(bytes(frames))
    return str(path)


@pytest.fixture()
def silent_caller_wav(tmp_path):
    # The agent talks; the caller channel never does -> not scorable.
    return _write_stereo(tmp_path / "silent-caller.wav", [], [(0.2, 2.8)])


def _assert_not_scorable_text(out):
    assert "[NOT SCORABLE]" in out
    assert "reason:" in out
    assert "caller speech" in out
    assert "[FAIL]" not in out
    assert "[PASS]" not in out
    assert "process_exit_code=2" in out
    # the trailing line never prints the misleading envelope exit_code
    assert "\n  exit_code=" not in out


# --- run ---------------------------------------------------------------------

def test_run_text_not_scorable(silent_caller_wav, capsys):
    rc = cli.main(["run", "--stereo", silent_caller_wav, "--format", "text"])
    assert rc == 2
    out = capsys.readouterr().out
    _assert_not_scorable_text(out)
    assert "0/1 events pass  (failed=0, not_scorable=1)" in out


def test_run_json_envelope_unchanged(silent_caller_wav, capsys):
    rc = cli.main(["run", "--stereo", silent_caller_wav, "--format", "json"])
    assert rc == 2  # process exit still maps to 2
    env = json.loads(capsys.readouterr().out)
    # the envelope stays schema-frozen: exit_code 0|1, scorable: false present
    assert env["exit_code"] == 0
    assert env["events"][0]["scorable"] is False
    assert "caller speech" in env["events"][0]["not_scorable_reason"]
    assert env["summary"]["not_scorable"] == 1


# --- doctor ------------------------------------------------------------------

def test_doctor_text_not_scorable(silent_caller_wav, tmp_path, capsys):
    rc = cli.main([
        "doctor", "--stereo", silent_caller_wav,
        "--no-open", "--out", str(tmp_path / "report.html"),
    ])
    assert rc == 2
    out = capsys.readouterr().out
    _assert_not_scorable_text(out)


# --- capture (every --stereo path) -------------------------------------------

@pytest.mark.parametrize("stack", ["vapi", "retell", "twilio"])
def test_capture_stereo_not_scorable(stack, silent_caller_wav, capsys):
    rc = cli.main(["capture", "--stack", stack, "--stereo", silent_caller_wav])
    assert rc == 2
    out = capsys.readouterr().out
    _assert_not_scorable_text(out)


def test_capture_json_envelope_unchanged(silent_caller_wav, capsys):
    rc = cli.main([
        "capture", "--stack", "vapi", "--stereo", silent_caller_wav,
        "--format", "json",
    ])
    assert rc == 2
    env = json.loads(capsys.readouterr().out)
    assert env["exit_code"] == 0
    assert env["events"][0]["scorable"] is False


# --- fully-scorable runs keep the documented exit_code= line ------------------

def _scorable_wav(tmp_path):
    # agent holds the floor, caller barges in at 1.0s, agent yields at 1.5s
    return _write_stereo(tmp_path / "scorable.wav", [(1.0, 2.0)], [(0.0, 1.5)])


def test_run_text_scorable_keeps_exit_code_line(tmp_path, capsys):
    rc = cli.main(["run", "--stereo", _scorable_wav(tmp_path), "--format", "text"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "[PASS]" in out
    assert "exit_code=0" in out
    assert "process_exit_code" not in out
    assert "not_scorable" not in out


def test_capture_demo_keeps_exit_code_line(capsys):
    rc = cli.main(["capture", "--stack", "vapi", "--demo"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "exit_code=0" in out
    assert "process_exit_code" not in out
