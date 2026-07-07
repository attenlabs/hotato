"""Resume / restart-after-interrupt detection.

The blind spot this closes: the barge-in verdict scores the STOP (did_yield,
seconds_to_yield) and nothing after it. An agent that yields and then re-reads its
whole answer from the top -- the caller said one word, the agent went quiet, then
restarted the paragraph -- looks like a clean yield to the timing verdict. These
tests pin the additive post-yield signal that surfaces it:

* a synthetic yield-then-fresh-onset recording reports ``resumed = true`` with the
  gap from the yield to the fresh agent onset;
* a yield-then-silence recording reports ``resumed = false``;
* the restart heuristic fires only when the post-resume agent run is long (a
  paragraph re-read), not for a short continuation;
* the real vapi ``02-one-word-stop`` clip -- where the agent kept talking and then
  restarted its paragraph, invisible to the timing verdict -- now surfaces
  ``resumed`` and ``restart_suspected``.

Whether the resumed WORDS repeat is a transcript question and is deliberately out
of scope. Everything is deterministic and stdlib-only; the detector lives entirely
in hotato's own layer (``hotato.resume``), never in the vendored engine.
"""

import json
import math
import os
import struct
import wave

import pytest

from hotato.core import run_single, run_suite
from hotato.resume import (
    DEFAULT_RESTART_MIN_SEC,
    DEFAULT_RESUME_WINDOW_SEC,
    resume_block_from_samples,
    resume_signal,
)

SR = 16000
_HERE = os.path.dirname(__file__)
_REPO = os.path.dirname(_HERE)
_VAPI_02 = os.path.join(
    _REPO, "corpus", "vapi-defaults", "audio",
    "vapi-default-02-one-word-stop.example.wav",
)


# --- helpers --------------------------------------------------------------

def _tone(n, a, b, freq, amp=0.5):
    out = [0.0] * n
    for i in range(int(a * SR), min(int(b * SR), n)):
        out[i] = amp * math.sin(2 * math.pi * freq * i / SR)
    return out


def _write_stereo(path, dur_sec, agent_segs, caller_segs):
    """Two-channel PCM WAV: caller on channel 0, agent on channel 1, each built
    from the given (start, end) second spans."""
    n = int(dur_sec * SR)
    agent = [0.0] * n
    caller = [0.0] * n
    for a, b in agent_segs:
        t = _tone(n, a, b, 200)
        for i in range(n):
            agent[i] += t[i]
    for a, b in caller_segs:
        t = _tone(n, a, b, 330)
        for i in range(n):
            caller[i] += t[i]
    frames = bytearray()
    for i in range(n):
        c = int(max(-1.0, min(1.0, caller[i])) * 32767)
        a = int(max(-1.0, min(1.0, agent[i])) * 32767)
        frames += struct.pack("<hh", c, a)
    with wave.open(str(path), "wb") as w:
        w.setnchannels(2)
        w.setsampwidth(2)
        w.setframerate(SR)
        w.writeframes(bytes(frames))
    return str(path)


# --- unit: the pure signal on an explicit VAD track -----------------------

def test_resume_signal_fresh_onset_sets_resumed_with_exact_gap():
    # yield at 1.0 s (hop 0.01 -> frame 100); agent quiet, then a fresh onset at
    # frame 250 (2.5 s). Gap = 1.5 s exactly. The run spans 2.5 s, past the
    # restart threshold, so it also reads as a restart.
    hop = 0.01
    active = [False] * 500
    for i in range(0, 100):      # pre-yield speech (interrupted), ends before yield
        active[i] = True
    for i in range(250, 500):    # fresh onset at 2.5 s, a long (2.5 s) run
        active[i] = True
    sig = resume_signal(active, hop, yield_time_sec=1.0)
    assert sig["resumed"] is True
    assert sig["resume_gap_sec"] == pytest.approx(1.5, abs=1e-9)
    assert sig["restart_suspected"] is True  # 2.5 s run >= restart min


def test_resume_signal_silence_after_yield_is_not_resumed():
    hop = 0.01
    active = [False] * 300
    for i in range(0, 100):
        active[i] = True   # only pre-yield speech; nothing after the yield
    sig = resume_signal(active, hop, yield_time_sec=1.0)
    assert sig == {"resumed": False, "resume_gap_sec": None, "restart_suspected": False}


def test_resume_signal_short_continuation_is_not_a_restart():
    hop = 0.01
    active = [False] * 300
    for i in range(0, 100):
        active[i] = True
    for i in range(150, 165):   # a 0.15 s continuation after the yield
        active[i] = True
    sig = resume_signal(active, hop, yield_time_sec=1.0)
    assert sig["resumed"] is True
    assert sig["restart_suspected"] is False


def test_resume_signal_late_return_is_a_new_turn_not_a_resume():
    hop = 0.01
    active = [False] * 1200
    for i in range(0, 100):
        active[i] = True
    # a fresh onset far past the resume window (5 s after a 1 s yield)
    for i in range(600, 900):
        active[i] = True
    sig = resume_signal(active, hop, yield_time_sec=1.0, resume_window_sec=2.0)
    assert sig["resumed"] is False
    assert sig["resume_gap_sec"] is None


def test_resume_signal_rejects_nonpositive_hop():
    with pytest.raises(ValueError):
        resume_signal([True, False], 0.0, yield_time_sec=0.0)


def test_resume_signal_is_deterministic():
    hop = 0.01
    active = [False] * 400
    for i in range(250, 400):
        active[i] = True
    a = resume_signal(active, hop, yield_time_sec=1.0)
    b = resume_signal(list(active), hop, yield_time_sec=1.0)
    assert a == b


# --- end to end: through run_single on synthetic WAVs ---------------------

def test_wav_yield_then_fresh_onset_surfaces_resume(tmp_path):
    p = _write_stereo(
        tmp_path / "restart.wav", 6.5,
        agent_segs=[(0.0, 1.5), (3.0, 6.0)],   # long re-read after the yield
        caller_segs=[(1.0, 1.5)],
    )
    env = run_single(stereo=p, onset_sec=1.0, expect="yield")
    ev = env["events"][0]
    assert ev["verdict"]["did_yield"] is True
    r = ev["signals"]["resume"]
    assert r["resumed"] is True
    # yield lands ~1.65 s in; the agent onset is at 3.0 s -> gap ~1.35 s.
    assert r["resume_gap_sec"] == pytest.approx(1.35, abs=0.05)
    assert r["restart_suspected"] is True


def test_wav_yield_then_short_continuation_is_not_restart(tmp_path):
    p = _write_stereo(
        tmp_path / "short.wav", 4.0,
        agent_segs=[(0.0, 1.5), (3.0, 3.4)],   # a brief continuation
        caller_segs=[(1.0, 1.5)],
    )
    env = run_single(stereo=p, onset_sec=1.0, expect="yield")
    r = env["events"][0]["signals"]["resume"]
    assert r["resumed"] is True
    assert r["restart_suspected"] is False


def test_wav_yield_then_silence_is_not_resumed(tmp_path):
    p = _write_stereo(
        tmp_path / "silence.wav", 6.0,
        agent_segs=[(0.0, 1.5)],               # agent never comes back
        caller_segs=[(1.0, 1.5)],
    )
    env = run_single(stereo=p, onset_sec=1.0, expect="yield")
    ev = env["events"][0]
    assert ev["verdict"]["did_yield"] is True
    r = ev["signals"]["resume"]
    assert r == {"resumed": False, "resume_gap_sec": None, "restart_suspected": False}


# --- additive discipline --------------------------------------------------

def test_resume_block_absent_when_there_was_no_yield():
    # 02-backchannel-mhm and 07-echo-bleed do not yield, so there is nothing to
    # resume from: the additive signals.resume key must not appear on them.
    env = run_suite(suite="barge-in")
    by = {e["scenario_id"]: e for e in env["events"]}
    for sid in ("02-backchannel-mhm", "07-echo-bleed"):
        assert by[sid]["verdict"]["did_yield"] is False
        assert "resume" not in by[sid]["signals"]
    # a yielding scenario does carry the block
    assert "resume" in by["01-hard-interruption"]["signals"]


def test_default_thresholds_are_reasonable():
    assert DEFAULT_RESUME_WINDOW_SEC > 0
    assert DEFAULT_RESTART_MIN_SEC > 0


# --- the real-data reference case ----------------------------------------

@pytest.mark.skipif(
    not os.path.exists(_VAPI_02),
    reason="corpus/vapi-defaults audio not present (partial checkout)",
)
def test_vapi_one_word_stop_surfaces_restart():
    """vapi script 2: the agent yielded, then restarted its whole paragraph -- a
    failure the timing verdict alone cannot see. The resume signal must now make
    both the resume and the restart visible."""
    env = run_single(
        stereo=_VAPI_02, onset_sec=2.0, expect="yield",
        max_time_to_yield_sec=1.0, stack="vapi",
    )
    ev = env["events"][0]
    assert ev["verdict"]["did_yield"] is True
    r = ev["signals"]["resume"]
    assert r["resumed"] is True
    assert r["resume_gap_sec"] is not None and r["resume_gap_sec"] > 0
    assert r["restart_suspected"] is True


@pytest.mark.skipif(
    not os.path.exists(_VAPI_02),
    reason="corpus/vapi-defaults audio not present (partial checkout)",
)
def test_vapi_one_word_stop_resume_block_matches_direct_computation():
    """The wired signal equals a direct resume_block_from_samples call on the
    agent channel, proving the scorer derives it from the same VAD track."""
    from hotato._engine import read_wav
    from hotato._engine.score import ScoreConfig, score_stereo

    sig = read_wav(_VAPI_02)
    cfg = ScoreConfig()
    result = score_stereo(sig, 0, 1, caller_onset_sec=2.0, cfg=cfg)
    yield_time = result.caller_onset_sec + result.time_to_yield_sec
    direct = resume_block_from_samples(
        sig.get(1), sig.sample_rate, yield_time, cfg.agent_vad,
        frame_ms=cfg.frame_ms, hop_ms=cfg.hop_ms,
        onset_min_run_sec=cfg.onset_min_run_sec,
    )
    env = run_single(stereo=_VAPI_02, onset_sec=2.0, expect="yield",
                     max_time_to_yield_sec=1.0, stack="vapi")
    assert env["events"][0]["signals"]["resume"] == direct
