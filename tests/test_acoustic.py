"""``hotato.acoustic``: per-channel acoustic health metrics -- deterministic
signal measures over the same decoded audio the scorer reads.

Pins the properties that make the block trustworthy:

* SNR ESTIMATE IS A MEASUREMENT -- on synthesized sine+noise fixtures built at
  a KNOWN ratio, the reported snr_db lands within tolerance of the analytic
  value, per channel, and orders two channels of different known SNR correctly;
* the null paths are stated, never fabricated: a silent channel reports
  ``snr_db: null`` with the no-speech reason, an always-active channel with
  the no-noise-pool reason;
* percent-silence, the energy-burst rate (bursts of acoustic energy, never
  words), clipping fraction, and per-channel duration match the constructed
  fixture;
* determinism: the same file yields the byte-identical block on a re-run, and
  the block always serializes as strict finite JSON;
* surfacing: ``hotato investigate`` carries the block on the AUDIO path (state
  file included) and the transcript path omits it entirely (no audio to
  measure); the single-recording HTML report renders the acoustic table while
  a suite page stays free of it.
"""

from __future__ import annotations

import json
import math
import random
import struct
import wave

import pytest

from hotato import acoustic as A
from hotato import investigate as I
from hotato import report as R
from hotato.errors import safe_json_dumps

SR = 16000
DUR = 8.0
# Three 1.0 s energy bursts in an 8.0 s clip.
BURSTS = [(1.0, 2.0), (3.5, 4.5), (6.0, 7.0)]


def _in_burst(t: float) -> bool:
    return any(a <= t < b for a, b in BURSTS)


def _sine_noise_channel(amp_sine: float, amp_noise: float, freq: float,
                        seed: int, dur: float = DUR):
    """Uniform noise at ``amp_noise`` across the whole clip, plus a sine at
    ``amp_sine`` inside the burst windows: a channel whose speech-vs-noise
    ratio is known analytically."""
    rng = random.Random(seed)
    n = int(SR * dur)
    out = []
    for i in range(n):
        t = i / SR
        x = rng.uniform(-amp_noise, amp_noise)
        if _in_burst(t):
            x += amp_sine * math.sin(2 * math.pi * freq * i / SR)
        out.append(x)
    return out


def _write_stereo(path: str, left, right) -> None:
    n = min(len(left), len(right))
    with wave.open(path, "wb") as wf:
        wf.setnchannels(2)
        wf.setsampwidth(2)
        wf.setframerate(SR)
        frames = bytearray()
        for c, a in zip(left[:n], right[:n]):
            frames += struct.pack(
                "<hh", int(max(-1.0, min(1.0, c)) * 32767),
                int(max(-1.0, min(1.0, a)) * 32767),
            )
        wf.writeframes(bytes(frames))


def _write_mono(path: str, samples) -> None:
    with wave.open(path, "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(SR)
        frames = bytearray()
        for x in samples:
            frames += struct.pack("<h", int(max(-1.0, min(1.0, x)) * 32767))
        wf.writeframes(bytes(frames))


def _expected_snr_db(amp_sine: float, amp_noise: float) -> float:
    """Analytic SNR of the fixture: during a burst the frame carries the sine
    PLUS the noise (rms**2 = As**2/2 + An**2/3); the noise floor is the
    uniform noise alone (rms = An/sqrt(3))."""
    speech_rms = math.sqrt(amp_sine ** 2 / 2.0 + amp_noise ** 2 / 3.0)
    noise_rms = amp_noise / math.sqrt(3.0)
    return 20.0 * math.log10(speech_rms / noise_rms)


@pytest.fixture()
def known_snr_wav(tmp_path):
    """ch0 ~= 41.8 dB SNR, ch1 ~= 29.0 dB SNR, same burst layout."""
    path = str(tmp_path / "known-snr.wav")
    ch0 = _sine_noise_channel(0.3, 0.003, 440.0, seed=7)
    ch1 = _sine_noise_channel(0.3, 0.013, 330.0, seed=11)
    _write_stereo(path, ch0, ch1)
    return path


# --- SNR estimate on known-ratio fixtures ----------------------------------

def test_acoustic_snr_matches_known_ratio_per_channel(known_snr_wav):
    block = A.acoustic_report(known_snr_wav)
    ch0, ch1 = block["channels"]

    # Tolerance covers the VAD hangover pulling a few noise-level frames into
    # the speech pool, burst-edge frames, and 16-bit quantization.
    assert ch0["snr_db"] == pytest.approx(
        _expected_snr_db(0.3, 0.003), abs=2.5)
    assert ch1["snr_db"] == pytest.approx(
        _expected_snr_db(0.3, 0.013), abs=2.5)
    # The two known ratios differ by ~12.7 dB; the estimates keep the order
    # and a wide margin of that separation.
    assert ch0["snr_db"] - ch1["snr_db"] > 8.0
    assert ch0["snr_null_reason"] is None
    assert ch1["snr_null_reason"] is None


def test_acoustic_roles_follow_the_channel_map(known_snr_wav):
    block = A.acoustic_report(known_snr_wav)
    assert [c["role"] for c in block["channels"]] == ["caller", "agent"]
    assert [c["channel"] for c in block["channels"]] == [0, 1]
    swapped = A.acoustic_report(known_snr_wav, caller_channel=1,
                                agent_channel=0)
    assert [c["role"] for c in swapped["channels"]] == ["agent", "caller"]


# --- percent silence, energy-burst rate, duration --------------------------

def test_acoustic_percent_silence_and_energy_burst_rate(known_snr_wav):
    block = A.acoustic_report(known_snr_wav)
    for ch in block["channels"]:
        assert ch["duration_sec"] == pytest.approx(8.0, abs=0.001)
        # 3 s of bursts in 8 s: active ~= 3 s + VAD hangover + frame edges,
        # so silence sits in a band around ~55%.
        assert 45.0 <= ch["percent_silence"] <= 65.0
        # 3 sustained energy bursts in 8 s = 22.5 bursts per minute. The
        # count is of ENERGY bursts (the fixture's sine windows), a rate the
        # block deliberately never calls words.
        assert ch["energy_bursts"] == 3
        assert ch["energy_burst_rate_per_min"] == pytest.approx(22.5, abs=0.1)


# --- clipping fraction against a constructed clipped burst -----------------

def test_acoustic_clipping_fraction_matches_constructed_clipping(tmp_path):
    path = str(tmp_path / "clipped.wav")
    n = int(SR * DUR)
    hot = []
    for i in range(n):
        t = i / SR
        x = 1.2 * math.sin(2 * math.pi * 330.0 * i / SR) if 1.0 <= t < 3.0 else 0.0
        hot.append(x)
    clean = _sine_noise_channel(0.3, 0.003, 440.0, seed=3)
    _write_stereo(path, hot, clean)

    # The expected fraction, counted over the SAME 16-bit quantization the
    # writer applies and the decoder reads back (q / 32768.0).
    clipped = sum(
        1 for x in hot
        if abs(int(max(-1.0, min(1.0, x)) * 32767)) / 32768.0 >= 0.99
    )
    expected = clipped / n
    assert expected > 0.05  # the fixture genuinely clips

    block = A.acoustic_report(path)
    assert block["channels"][0]["clipping_fraction"] == pytest.approx(
        expected, abs=2e-4)
    assert block["channels"][1]["clipping_fraction"] == 0.0


# --- null paths: stated reasons, never fabricated numbers ------------------

def test_acoustic_silent_channel_reports_no_speech_reason(tmp_path):
    path = str(tmp_path / "half-silent.wav")
    silent = [0.0] * int(SR * DUR)
    talker = _sine_noise_channel(0.3, 0.003, 440.0, seed=5)
    _write_stereo(path, silent, talker)
    block = A.acoustic_report(path)
    ch0 = block["channels"][0]
    assert ch0["snr_db"] is None
    assert ch0["snr_null_reason"] == A.SNR_NO_SPEECH_REASON
    assert ch0["percent_silence"] == 100.0
    assert ch0["energy_bursts"] == 0


def test_acoustic_never_quiet_channel_reports_no_noise_pool_reason(tmp_path):
    path = str(tmp_path / "wall.wav")
    n = int(SR * DUR)
    wall = [0.3 * math.sin(2 * math.pi * 220.0 * i / SR) for i in range(n)]
    talker = _sine_noise_channel(0.3, 0.003, 440.0, seed=9)
    _write_stereo(path, wall, talker)
    block = A.acoustic_report(path)
    ch0 = block["channels"][0]
    assert ch0["snr_db"] is None
    assert ch0["snr_null_reason"] == A.SNR_NO_NOISE_REASON
    assert ch0["percent_silence"] == 0.0


def test_acoustic_mono_file_measures_but_carries_no_role(tmp_path):
    path = str(tmp_path / "mono.wav")
    _write_mono(path, _sine_noise_channel(0.3, 0.003, 440.0, seed=13))
    block = A.acoustic_report(path)
    assert len(block["channels"]) == 1
    ch = block["channels"][0]
    # A mixed/mono channel is nobody's channel: measured, never role-labeled.
    assert ch["role"] is None
    assert ch["snr_db"] is not None


# --- determinism + strict finite JSON --------------------------------------

def test_acoustic_block_is_deterministic_and_finite_json(known_snr_wav):
    b1 = A.acoustic_report(known_snr_wav)
    b2 = A.acoustic_report(known_snr_wav)
    assert json.dumps(b1, sort_keys=True) == json.dumps(b2, sort_keys=True)
    # safe_json_dumps refuses NaN/Infinity: every value is finite or null.
    safe_json_dumps(b1)
    assert b1["tool"] == "hotato"
    assert b1["kind"] == "acoustic"
    assert b1["schema_version"] == "1"


# --- surfacing: investigate (audio path only) ------------------------------

def test_acoustic_investigate_audio_path_carries_the_block(known_snr_wav,
                                                           tmp_path):
    state = str(tmp_path / "state.json")
    result, code = I.run_investigate(known_snr_wav, state_path=state)
    assert code == 0
    block = result["acoustic"]
    assert block["kind"] == "acoustic"
    assert [c["role"] for c in block["channels"]] == ["caller", "agent"]
    # persisted alongside trust in the state file
    st = json.loads(open(state, encoding="utf-8").read())
    assert st["acoustic"] == block
    # and surfaced in the human report, labeled as signal measures
    text = I.render_text(result)
    assert "acoustic health (signal measures, not speech content):" in text
    assert "energy bursts" in text


def test_acoustic_investigate_transcript_path_omits_the_block(tmp_path):
    tx = tmp_path / "chat.json"
    tx.write_text(json.dumps({"segments": [
        {"role": "agent", "start": 0.0, "end": 1.5},
        {"role": "caller", "start": 1.0, "end": 2.5},
        {"role": "agent", "start": 6.0, "end": 7.0},
    ]}), encoding="utf-8")
    state = str(tmp_path / "state.json")
    result, code = I.run_investigate_transcript(str(tx), state_path=state)
    assert code == 0
    # No audio was measured, so no acoustic block exists anywhere: omitted,
    # never a null placeholder.
    assert "acoustic" not in result
    st = json.loads(open(state, encoding="utf-8").read())
    assert "acoustic" not in st
    assert "acoustic health" not in I.render_text(result)


# --- surfacing: the HTML report table --------------------------------------

def test_acoustic_html_report_renders_the_table(known_snr_wav):
    html, env = R.build_report_html(stereo=known_snr_wav)
    assert "Acoustic health (signal measures)" in html
    assert "energy bursts (/min)" in html
    assert "caller (ch 0)" in html
    assert "agent (ch 1)" in html
    # the page-wide no-accuracy-score invariant holds: no percent sign
    # anywhere, so the share metrics render as fractions
    assert "%" not in html
    assert "fraction of frames" in html
    # the envelope itself is untouched: renderer-only surfacing
    assert "acoustic" not in env["events"][0]


def test_acoustic_html_report_split_files_render_the_table(tmp_path):
    caller = str(tmp_path / "caller.wav")
    agent = str(tmp_path / "agent.wav")
    _write_mono(caller, _sine_noise_channel(0.3, 0.003, 440.0, seed=21))
    _write_mono(agent, _sine_noise_channel(0.3, 0.003, 330.0, seed=22))
    html, _ = R.build_report_html(caller=caller, agent=agent)
    assert "Acoustic health (signal measures)" in html
    assert "caller (ch 0)" in html
    assert "agent (ch 1)" in html


def test_acoustic_suite_report_stays_free_of_the_table():
    html, _ = R.build_report_html(suite="barge-in")
    assert "Acoustic health" not in html
    assert "actab" not in html


def test_acoustic_split_report_block_shapes_roles():
    # acoustic_report_split maps file 0 -> caller, file 1 -> agent, one block.
    import os
    import tempfile
    with tempfile.TemporaryDirectory() as td:
        caller = os.path.join(td, "c.wav")
        agent = os.path.join(td, "a.wav")
        _write_mono(caller, _sine_noise_channel(0.3, 0.003, 440.0, seed=31))
        _write_mono(agent, _sine_noise_channel(0.3, 0.003, 330.0, seed=32))
        block = A.acoustic_report_split(caller, agent)
    assert [c["role"] for c in block["channels"]] == ["caller", "agent"]
    assert [c["channel"] for c in block["channels"]] == [0, 1]
    assert block["source"] == "c.wav + a.wav"
