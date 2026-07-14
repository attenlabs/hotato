"""Echo / agent-self-interruption detection.

The blind spot this closes: a stop the agent makes because it HEARD ITS OWN TTS
bleed back on the caller channel is, to the timing tracks alone, indistinguishable
from a real yield (energy is energy). These tests pin the additive cross-channel
coherence signal that flags it:

* a synthetic WAV whose caller channel is a delayed, attenuated copy of the agent
  channel yields ``signals.echo.echo_suspected = true``, an
  ``echo_correlated_activity`` scan candidate, and a loud diagnose warning;
* a clean two-speaker WAV yields ``echo_suspected = false``;
* ``--echo-gate`` (opt-in) holds a bleed-induced yield out of the verdict
  (not_scorable) while the DEFAULT run leaves the verdict untouched.

Everything is deterministic and stdlib-only; the detector lives entirely in
hotato's own layer (``hotato.echo``), never in the vendored engine.
"""

import math
import struct
import wave
from importlib import resources

import pytest

from hotato import cli
from hotato._engine import read_wav
from hotato.core import run_single, run_suite
from hotato.diagnose import diagnose_envelope, echo_warnings
from hotato.diagnose import render_text as diagnose_text
from hotato.echo import echo_block_from_samples, echo_signal
from hotato.scan import scan_recording

SR = 16000


def _write_stereo(path, caller, agent, sr=SR):
    """Two-channel 16-bit PCM WAV: caller on channel 0, agent on channel 1."""
    n = min(len(caller), len(agent))
    frames = bytearray()
    for i in range(n):
        c = int(max(-1.0, min(1.0, caller[i])) * 32767)
        a = int(max(-1.0, min(1.0, agent[i])) * 32767)
        frames += struct.pack("<hh", c, a)
    with wave.open(str(path), "wb") as w:
        w.setnchannels(2)
        w.setsampwidth(2)
        w.setframerate(sr)
        w.writeframes(bytes(frames))
    return str(path)


def _tone(n, seg, freq, sr=SR, amp=0.5):
    out = [0.0] * n
    a, b = int(seg[0] * sr), int(seg[1] * sr)
    for i in range(a, min(b, n)):
        out[i] = amp * math.sin(2 * math.pi * freq * i / sr)
    return out


def _bleed_wav(path, dur=4.0, agent_seg=(0.0, 2.0), delay_sec=0.12, gain=0.35, sr=SR):
    """Agent talks; the caller channel carries ONLY a delayed, attenuated copy of
    the agent's own audio (echo bleed). The agent then goes quiet while the echo
    tail is still present, so the scorer sees a caller-nearby stop -> a yield."""
    n = int(dur * sr)
    agent = _tone(n, agent_seg, 330.0, sr)
    delay = int(delay_sec * sr)
    caller = [0.0] * n
    for i in range(n):
        j = i - delay
        if 0 <= j < n:
            caller[i] = gain * agent[j]
    return _write_stereo(path, caller, agent, sr)


def _clean_wav(path, dur=4.0, sr=SR):
    """A real two-speaker barge-in on INDEPENDENT voices: the agent talks
    0.0-1.8s, the caller interrupts 1.5-2.5s on a different pitch, and the agent
    yields. Nothing on the caller channel is a copy of the agent channel."""
    n = int(dur * sr)
    agent = _tone(n, (0.0, 1.8), 330.0, sr)
    caller = _tone(n, (1.5, 2.5), 220.0, sr)
    return _write_stereo(path, caller, agent, sr)


def _bundled(sid):
    return str(resources.files("hotato").joinpath("data", "audio", sid + ".example.wav"))


# --- the detector on the bundled fixtures ----------------------------------

def test_bundled_echo_bleed_is_the_positive_case():
    """07-echo-bleed is the intended positive: its caller channel IS the agent's
    own audio, so coherence is ~1.0 at the rendered 0.12 s echo delay."""
    s = read_wav(_bundled("07-echo-bleed"))
    blk = echo_block_from_samples(s.get(0), s.get(1), s.sample_rate)
    assert blk["echo_suspected"] is True
    assert blk["coherence"] >= 0.9
    assert blk["lag_sec"] == pytest.approx(0.12, abs=0.03)


@pytest.mark.parametrize("sid", [
    "01-hard-interruption", "02-backchannel-mhm", "03-filler-start",
    "04-correction", "05-telephony-8khz", "06-double-talk",
    "08-rapid-turn-taking",
])
def test_bundled_clean_fixtures_are_not_echo(sid):
    s = read_wav(_bundled(sid))
    blk = echo_block_from_samples(s.get(0), s.get(1), s.sample_rate)
    assert blk["echo_suspected"] is False, (sid, blk)


def test_echo_signal_silent_channel_is_not_echo():
    # A silent caller envelope has no energy -> undefined cosine -> never echo.
    blk = echo_signal([0.0] * 200, [0.3] * 200, 0.01)
    assert blk == {"coherence": 0.0, "lag_sec": 0.0, "echo_suspected": False}


# --- signals.echo is additive on every scored event ------------------------

def test_signals_echo_present_on_single_and_suite():
    env = run_single(stereo=_bundled("01-hard-interruption"), expect="yield")
    echo = env["events"][0]["signals"]["echo"]
    assert set(echo) == {"coherence", "lag_sec", "echo_suspected"}

    suite = run_suite(suite="barge-in")
    by = {e["scenario_id"]: e for e in suite["events"]}
    assert by["07-echo-bleed"]["signals"]["echo"]["echo_suspected"] is True
    for sid, e in by.items():
        if sid != "07-echo-bleed":
            assert e["signals"]["echo"]["echo_suspected"] is False, sid


def test_synthetic_bleed_flags_echo_and_clean_does_not(tmp_path):
    bleed = _bleed_wav(tmp_path / "bleed.wav")
    clean = _clean_wav(tmp_path / "clean.wav")

    b = run_single(stereo=bleed, onset_sec=0.5, expect="yield")["events"][0]
    assert b["verdict"]["did_yield"] is True
    assert b["signals"]["echo"]["echo_suspected"] is True

    c = run_single(stereo=clean, onset_sec=1.5, expect="yield")["events"][0]
    assert c["signals"]["echo"]["echo_suspected"] is False


# --- the scan candidate -----------------------------------------------------

def test_scan_flags_echo_correlated_activity_on_bleed(tmp_path):
    bleed = _bleed_wav(tmp_path / "bleed.wav")
    res = scan_recording(bleed)
    echo = [c for c in res["candidates"] if c["kind"] == "echo_correlated_activity"]
    assert echo, "the echo-bleed run must surface an echo_correlated_activity candidate"
    c = echo[0]
    # honours the fixed candidate contract (same four keys as every other kind)
    assert set(c) == {"t_sec", "kind", "durations", "agent_reaction"}
    assert c["agent_reaction"]["echo_suspected"] is True
    assert c["agent_reaction"]["coherence"] >= 0.9
    assert c["durations"]["lag_sec"] == pytest.approx(0.12, abs=0.03)


def test_scan_clean_two_speaker_has_no_echo_candidate(tmp_path):
    clean = _clean_wav(tmp_path / "clean.wav")
    res = scan_recording(clean)
    kinds = {c["kind"] for c in res["candidates"]}
    assert "echo_correlated_activity" not in kinds


def test_scan_text_output_warns_on_echo(tmp_path, capsys):
    bleed = _bleed_wav(tmp_path / "bleed.wav")
    assert cli.main(["scan", "--stereo", bleed]) == 0
    out = capsys.readouterr().out
    assert "echo_correlated_activity" in out
    assert "WARNING" in out and "hearing itself" in out


# --- the diagnose warning ---------------------------------------------------

def test_diagnose_warns_on_echo_suspected_yield(tmp_path):
    bleed = _bleed_wav(tmp_path / "bleed.wav")
    env = run_single(stereo=bleed, onset_sec=0.5, expect="yield")
    diag = diagnose_envelope(env, source="bleed.wav")
    warns = diag["echo_warnings"]
    assert len(warns) == 1
    assert warns[0]["coherence"] >= 0.9
    text = diagnose_text(diag)
    assert "WARNING echo-suspected yield" in text
    assert "hearing its own audio bleed" in text


def test_echo_warnings_ignores_non_yields():
    # 07-echo-bleed does NOT yield (the agent correctly holds), so despite
    # echo_suspected=true it is not a spurious-yield warning.
    env = run_suite(suite="barge-in")
    warns = echo_warnings(env["events"])
    assert warns == []


# --- the opt-in echo gate ---------------------------------------------------

def test_echo_gate_flips_bleed_yield_to_not_scorable_only_when_asked(tmp_path):
    bleed = _bleed_wav(tmp_path / "bleed.wav")

    # DEFAULT run: the verdict is untouched; the yield stands, echo is reported.
    default = run_single(stereo=bleed, onset_sec=0.5, expect="yield")["events"][0]
    assert default.get("scorable") is not False
    assert default["verdict"]["did_yield"] is True
    assert default["signals"]["echo"]["echo_suspected"] is True

    # OPT-IN gate: the same bleed-induced yield is held out of the verdict.
    gated = run_single(
        stereo=bleed, onset_sec=0.5, expect="yield", echo_gate=True
    )["events"][0]
    assert gated["scorable"] is False
    assert "echo coherence" in gated["not_scorable_reason"]


def test_echo_gate_does_not_touch_clean_audio(tmp_path):
    clean = _clean_wav(tmp_path / "clean.wav")
    gated = run_single(
        stereo=clean, onset_sec=1.5, expect="yield", echo_gate=True
    )["events"][0]
    # clean audio is never echo-gated: the verdict is whatever it was.
    assert gated.get("scorable") is not False


def test_echo_gate_cli_flag_is_accepted(tmp_path):
    bleed = _bleed_wav(tmp_path / "bleed.wav")
    code = cli.main([
        "run", "--stereo", bleed, "--onset", "0.5", "--expect", "yield",
        "--echo-gate", "--format", "json",
    ])
    # a not-scorable single run maps to the CLI's exit-2 unusable-input code
    assert code == 2
