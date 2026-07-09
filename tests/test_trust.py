"""``hotato trust``: the input-health ("trust doctor") check.

Pinned here, on deterministic synthetic WAVs (built the same way as
test_not_scorable.py so every render is byte-identical) and, when present, the
real dual-channel corpus at ~/Projects/hotato-recordings/data:

  * a clean two-channel call is "safe to scan" and exits 0;
  * a mono file, a silent caller (or agent) channel, and two identical channels
    are each "NOT SCORABLE" with the specific reason AND the next step, and exit
    2 (the CLI's unusable-input convention);
  * swapped channels raise the possible-swap flag but stay scorable;
  * a hot recording raises the clipping warning without changing scorability;
  * the JSON shape is stable and agent-parseable;
  * NO turn-taking verdict word (yield/hold/pass/fail) ever appears in the
    output: this command reports input health only.
"""

import json
import math
import os
import struct
import wave

import pytest

from hotato import cli
from hotato import trust as trust_mod
from hotato.trust import (
    MIN_ACTIVITY_SEC,
    NEXT_STEP_CHANNEL_MAP,
    NEXT_STEP_DUAL_CHANNEL,
    SAFE_RECOMMENDATION,
    trust_report,
)


# --- deterministic synthetic fixtures ---------------------------------------

def _write_stereo(path, caller_segments, agent_segments, *, duration_sec=6.0,
                  sr=16000, caller_amp=0.35, agent_amp=0.35):
    """Two-channel PCM WAV: caller on channel 0, agent on channel 1. Each channel
    is a pure sine inside its active segments and exact digital silence outside,
    so every render is byte-identical everywhere."""
    n = int(duration_sec * sr)

    def _on(segments, t):
        return any(start <= t < end for start, end in segments)

    frames = bytearray()
    for i in range(n):
        t = i / sr
        c = int(caller_amp * 32767 * math.sin(2 * math.pi * 220.0 * i / sr)) if _on(caller_segments, t) else 0
        a = int(agent_amp * 32767 * math.sin(2 * math.pi * 330.0 * i / sr)) if _on(agent_segments, t) else 0
        frames += struct.pack("<hh", c, a)
    with wave.open(str(path), "wb") as w:
        w.setnchannels(2)
        w.setsampwidth(2)
        w.setframerate(sr)
        w.writeframes(bytes(frames))
    return str(path)


def _write_mono(path, segments, *, duration_sec=6.0, sr=16000):
    """A single-channel PCM WAV (the malformed 'export mixed a mono file' case)."""
    n = int(duration_sec * sr)

    def _on(t):
        return any(start <= t < end for start, end in segments)

    frames = bytearray()
    for i in range(n):
        t = i / sr
        v = int(0.35 * 32767 * math.sin(2 * math.pi * 220.0 * i / sr)) if _on(t) else 0
        frames += struct.pack("<h", v)
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(sr)
        w.writeframes(bytes(frames))
    return str(path)


def _write_duplicated_mono(path, segments, *, duration_sec=6.0, sr=16000):
    """A two-channel WAV whose two channels carry the IDENTICAL signal (a mono
    recording duplicated into stereo): decodable, two channels, but not separated."""
    n = int(duration_sec * sr)

    def _on(t):
        return any(start <= t < end for start, end in segments)

    frames = bytearray()
    for i in range(n):
        t = i / sr
        v = int(0.35 * 32767 * math.sin(2 * math.pi * 220.0 * i / sr)) if _on(t) else 0
        frames += struct.pack("<hh", v, v)
    with wave.open(str(path), "wb") as w:
        w.setnchannels(2)
        w.setsampwidth(2)
        w.setframerate(sr)
        w.writeframes(bytes(frames))
    return str(path)


# --- clean dual-channel: safe to scan ---------------------------------------

def test_clean_dual_channel_is_safe_to_scan(tmp_path):
    # Caller interjects briefly (0.6s), agent holds the floor (long): the usual,
    # correctly mapped pattern.
    p = _write_stereo(tmp_path / "clean.wav",
                      caller_segments=[(3.0, 3.7)],
                      agent_segments=[(0.2, 5.8)])
    r = trust_report(p)
    assert r["scorable"] is True
    assert r["recommendation"] == SAFE_RECOMMENDATION
    assert r["not_scorable_reason"] is None
    assert r["next_step"] is None
    assert r["exit_code"] == 0
    assert r["channels"]["possible_swap"] is False
    sc = r["scorability"]
    assert sc == {"separated_tracks": True,
                  "enough_caller_activity": True,
                  "enough_agent_activity": True}


def test_clean_dual_channel_cli_exit_zero(tmp_path, capsys):
    p = _write_stereo(tmp_path / "clean.wav",
                      caller_segments=[(3.0, 3.7)],
                      agent_segments=[(0.2, 5.8)])
    code = cli.main(["trust", "--stereo", p])
    out = capsys.readouterr().out
    assert code == 0
    assert SAFE_RECOMMENDATION in out


# --- mono: not scorable ------------------------------------------------------

def test_mono_is_not_scorable(tmp_path):
    p = _write_mono(tmp_path / "mono.wav", segments=[(0.2, 5.8)])
    r = trust_report(p)
    assert r["scorable"] is False
    assert r["recording"]["channels"] == 1
    assert "single channel" in r["not_scorable_reason"]
    assert r["next_step"] == NEXT_STEP_DUAL_CHANNEL
    assert r["recommendation"].startswith("NOT SCORABLE:")
    assert r["exit_code"] == 2
    assert r["scorability"]["separated_tracks"] is False


def test_mono_cli_exit_two(tmp_path):
    p = _write_mono(tmp_path / "mono.wav", segments=[(0.2, 5.8)])
    assert cli.main(["trust", "--stereo", p]) == 2


# --- silent caller channel: not scorable, with the exact reason + next step --

def test_silent_caller_channel_is_not_scorable(tmp_path):
    # The agent talks; the caller channel never does.
    p = _write_stereo(tmp_path / "silent-caller.wav",
                      caller_segments=[],
                      agent_segments=[(0.2, 5.8)])
    r = trust_report(p)
    assert r["scorable"] is False
    assert r["not_scorable_reason"] == "caller channel has no detected speech"
    assert r["next_step"] == NEXT_STEP_CHANNEL_MAP
    assert r["recommendation"] == (
        "NOT SCORABLE: caller channel has no detected speech"
    )
    assert r["exit_code"] == 2
    assert r["scorability"]["enough_caller_activity"] is False
    assert r["channels"]["caller"]["has_speech"] is False


def test_silent_agent_channel_is_not_scorable(tmp_path):
    p = _write_stereo(tmp_path / "silent-agent.wav",
                      caller_segments=[(1.0, 3.0)],
                      agent_segments=[])
    r = trust_report(p)
    assert r["scorable"] is False
    assert r["not_scorable_reason"] == "agent channel has no detected speech"
    assert r["next_step"] == NEXT_STEP_CHANNEL_MAP
    assert r["exit_code"] == 2


# --- identical channels: not separated, not scorable ------------------------

def test_identical_channels_are_not_scorable(tmp_path):
    p = _write_duplicated_mono(tmp_path / "dup.wav", segments=[(0.2, 5.8)])
    r = trust_report(p)
    assert r["scorable"] is False
    assert r["scorability"]["separated_tracks"] is False
    assert "same signal" in r["not_scorable_reason"]
    assert r["next_step"] == NEXT_STEP_DUAL_CHANNEL
    assert r["exit_code"] == 2


# --- swapped channels: flagged, but still scorable --------------------------

def test_swapped_channels_are_flagged(tmp_path):
    # The LONG, dominant speaker is on channel 0 (mapped as the caller) and the
    # brief interjector on channel 1 (mapped as the agent): the reverse of the
    # usual agent-dominant pattern, so the swap heuristic should fire.
    p = _write_stereo(tmp_path / "swapped.wav",
                      caller_segments=[(0.2, 5.8)],
                      agent_segments=[(2.0, 2.5)])
    r = trust_report(p)
    assert r["channels"]["possible_swap"] is True
    assert r["channels"]["swap_reason"]
    # A swap is a WARNING about the mapping, not an input defect: both channels
    # carry speech, so the recording is still scorable.
    assert r["scorable"] is True
    assert r["exit_code"] == 0
    assert any("reversed" in w for w in r["warnings"])


# --- clipping: warned, scorability unchanged --------------------------------

def test_clipping_is_warned_without_blocking_scan(tmp_path):
    # Caller recorded at full scale (a hot capture); agent normal.
    p = _write_stereo(tmp_path / "hot.wav",
                      caller_segments=[(3.0, 3.7)],
                      agent_segments=[(0.2, 5.8)],
                      caller_amp=1.0)
    r = trust_report(p)
    clip = r["recording"]["clipping"]["caller"]
    assert clip["clipped"] is True
    assert clip["clipped_fraction"] > 0.0
    assert any("clipping" in w for w in r["warnings"])
    # Clipping does not, by itself, make a recording unscorable.
    assert r["scorable"] is True
    assert r["exit_code"] == 0


# --- JSON shape is stable and agent-parseable -------------------------------

def test_json_shape(tmp_path, capsys):
    p = _write_stereo(tmp_path / "clean.wav",
                      caller_segments=[(3.0, 3.7)],
                      agent_segments=[(0.2, 5.8)])
    code = cli.main(["trust", "--stereo", p, "--format", "json"])
    assert code == 0
    d = json.loads(capsys.readouterr().out)
    assert d["tool"] == "hotato"
    assert d["kind"] == "input-health"
    assert d["schema_version"] == "1"
    assert set(d["recording"]) >= {"sample_rate", "duration_sec", "channels",
                                   "clipping", "leading_silence_sec"}
    assert set(d["scorability"]) == {"separated_tracks",
                                     "enough_caller_activity",
                                     "enough_agent_activity"}
    assert set(d["crosstalk_risk"]) == {"coherence", "lag_sec", "suspected"}
    assert set(d["channels"]) == {"caller", "agent", "possible_swap",
                                  "swap_reason"}
    for role in ("caller", "agent"):
        assert set(d["channels"][role]) == {"channel", "active_sec",
                                            "first_speech_sec", "has_speech",
                                            "enough_activity"}
    assert d["exit_code"] == 0
    assert d["scorable"] is True


def test_json_not_scorable_shape_carries_reason_and_next_step(tmp_path, capsys):
    p = _write_stereo(tmp_path / "silent-caller.wav",
                      caller_segments=[],
                      agent_segments=[(0.2, 5.8)])
    code = cli.main(["trust", "--stereo", p, "--format", "json"])
    assert code == 2
    d = json.loads(capsys.readouterr().out)
    assert d["scorable"] is False
    assert d["not_scorable_reason"] == "caller channel has no detected speech"
    assert d["next_step"] == NEXT_STEP_CHANNEL_MAP
    assert d["exit_code"] == 2


# --- honesty guardrail: never a turn-taking verdict -------------------------

def test_output_never_emits_a_turn_taking_verdict(tmp_path, capsys):
    p = _write_stereo(tmp_path / "clean.wav",
                      caller_segments=[(3.0, 3.7)],
                      agent_segments=[(0.2, 5.8)])
    cli.main(["trust", "--stereo", p])
    text = capsys.readouterr().out.lower()
    cli.main(["trust", "--stereo", p, "--format", "json"])
    js = capsys.readouterr().out.lower()
    # These are turn-taking VERDICT words; this command must never render one.
    for banned in ("did_yield", "yielded", "pass ", "[pass]", "[fail]",
                   "talk_over", "seconds_to_yield"):
        assert banned not in text, f"{banned!r} leaked into trust text output"
        assert banned not in js, f"{banned!r} leaked into trust json output"


# --- usage errors are the CLI's exit-2 usage contract, not a report ---------

def test_bad_channel_flag_is_a_usage_error(tmp_path):
    p = _write_stereo(tmp_path / "clean.wav",
                      caller_segments=[(3.0, 3.7)],
                      agent_segments=[(0.2, 5.8)])
    # channel 5 does not exist in a 2-channel file: exit 2 (usage error).
    assert cli.main(["trust", "--stereo", p, "--agent-channel", "5"]) == 2


def test_missing_file_is_a_usage_error(tmp_path):
    assert cli.main(["trust", "--stereo", str(tmp_path / "nope.wav")]) == 2


# --- real dual-channel corpus (when checked out) ----------------------------

_REAL_DIR = os.path.expanduser("~/Projects/hotato-recordings/data")


def _real_wavs():
    if not os.path.isdir(_REAL_DIR):
        return []
    return sorted(
        os.path.join(_REAL_DIR, f)
        for f in os.listdir(_REAL_DIR)
        if f.endswith(".wav")
    )


@pytest.mark.skipif(not _real_wavs(),
                    reason="real recordings not checked out at ~/Projects/hotato-recordings/data")
@pytest.mark.parametrize("wav", _real_wavs(), ids=lambda p: os.path.basename(p))
def test_real_dual_channel_recordings_are_scorable(wav):
    r = trust_report(wav)
    assert r["recording"]["channels"] == 2
    # Every committed corpus call is a real dual-channel recording with both
    # parties audible, so it must be safe to scan.
    assert r["scorable"] is True, (
        f"{os.path.basename(wav)} unexpectedly not scorable: "
        f"{r['not_scorable_reason']}"
    )
    assert r["exit_code"] == 0
    # Corpus convention: caller on channel 0, agent on channel 1, agent holds the
    # floor longer -> the swap heuristic must not fire on a correctly mapped call.
    assert r["channels"]["possible_swap"] is False
