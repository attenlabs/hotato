"""``hotato trust`` headline honesty: a verdict-changing warning must NEVER leave
the recommendation reading "safe to scan".

The trust headline used to lie by omission: a low signal level, a possible channel
swap, or a cross-channel leak below the fixed -40 dB bar each appended a warning yet
the recommendation still said "safe to scan". These pin the fix:

  * a verdict-changing warning (leakage, low signal, possible swap) forces the
    "scan with caution: <reason>" headline and the explicit ``input_health`` state;
  * an informational-only condition (a clean call) stays "safe to scan" / "clean";
  * the dynamic leakage rule cautions EARLIER than the fixed -40 dB ratio bar when
    the leaked copy would actually cross the receiving channel's VAD gate;
  * ``input_health`` is a 3-state field: clean / caution / not_scorable.

All fixtures are deterministic synthetic stereo WAVs (pure sines inside their active
segments, exact digital silence outside), built with the stdlib ``wave`` module, so
every render is byte-identical and the thresholds are hit on purpose.
"""

import math
import struct
import wave

from hotato.trust import (
    CAUTION_RECOMMENDATION,
    SAFE_RECOMMENDATION,
    LEAKAGE_WARN_DB,
    trust_report,
)

SR = 16000


# --- deterministic synthetic fixtures ---------------------------------------

def _write_stereo(path, caller_segments, agent_segments, *, duration_sec=6.0,
                  caller_amp=0.35, agent_amp=0.35, sr=SR):
    """Two-channel PCM WAV: caller on channel 0, agent on channel 1. Each channel is
    a pure sine inside its active segments and exact digital silence outside."""
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


def _tone(n, seg, freq, *, amp, sr=SR):
    out = [0.0] * n
    a, b = int(seg[0] * sr), int(seg[1] * sr)
    for i in range(a, min(b, n)):
        out[i] = amp * math.sin(2 * math.pi * freq * i / sr)
    return out


def _write_bleed(path, gain, *, agent_amp=0.35, sr=SR):
    """Agent talks in two turns; the caller genuinely interjects once; and the caller
    channel additionally carries a delayed, attenuated COPY of the agent (echo bleed
    at ``gain``). A louder ``agent_amp`` raises the copy's ABSOLUTE level without
    changing its ratio to the source -- the regime the dynamic gate rule catches."""
    dur = 8.0
    n = int(dur * sr)
    delay = int(0.12 * sr)
    agent = [0.0] * n
    for seg in [(0.2, 3.4), (5.5, 7.8)]:
        t = _tone(n, seg, 330.0, amp=agent_amp, sr=sr)
        for i in range(n):
            agent[i] += t[i]
    caller = _tone(n, (3.0, 4.0), 220.0, amp=0.35, sr=sr)
    for i in range(n):
        j = i - delay
        if 0 <= j < n:
            caller[i] += gain * agent[j]
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


def _has_verdict_changing_warning(report):
    """A verdict-changing warning is present exactly when the input-health state is
    'caution' (leakage, low signal, or a possible swap) -- the invariant under test:
    that state and 'safe to scan' are mutually exclusive."""
    return report["input_health"] == "caution"


# --- (a) a clean call: clean + safe -----------------------------------------

def test_clean_call_is_clean_and_safe(tmp_path):
    # Caller interjects briefly; agent holds the floor: the usual, correctly mapped,
    # normal-level pattern -- no verdict-changing warning at all.
    p = _write_stereo(tmp_path / "clean.wav",
                      caller_segments=[(3.0, 3.7)],
                      agent_segments=[(0.2, 5.8)])
    r = trust_report(p)
    assert r["scorable"] is True
    assert r["input_health"] == "clean"
    assert r["recommendation"] == SAFE_RECOMMENDATION
    assert not _has_verdict_changing_warning(r)
    assert r["exit_code"] == 0


# --- (b) a low-signal call: caution -----------------------------------------

def test_low_signal_call_is_caution_not_safe(tmp_path):
    # Both channels captured very quietly (peak well below the low-signal bar):
    # timing may be under-measured downstream, so the headline must caution.
    p = _write_stereo(tmp_path / "quiet.wav",
                      caller_segments=[(3.0, 3.7)],
                      agent_segments=[(0.2, 5.8)],
                      caller_amp=0.02, agent_amp=0.02)
    r = trust_report(p)
    assert r["scorable"] is True                       # still scorable...
    assert r["input_health"] == "caution"              # ...but NOT clean
    assert r["recommendation"].startswith(CAUTION_RECOMMENDATION)
    assert r["recommendation"] != SAFE_RECOMMENDATION
    assert "signal level very low" in r["recommendation"]
    assert any("signal level very low" in w for w in r["warnings"])
    assert r["exit_code"] == 0                          # disclosed, never rescored


# --- (c) a channel-swap-suspected call: caution -----------------------------

def test_channel_swap_call_is_caution_not_safe(tmp_path):
    # The long, dominant speaker is on channel 0 (mapped as caller) and the brief
    # interjector on channel 1 (mapped as agent): the reverse of the usual pattern,
    # so the swap heuristic fires -- a verdict-changing warning.
    p = _write_stereo(tmp_path / "swapped.wav",
                      caller_segments=[(0.2, 5.8)],
                      agent_segments=[(2.0, 2.5)])
    r = trust_report(p)
    assert r["channels"]["possible_swap"] is True
    assert r["scorable"] is True
    assert r["input_health"] == "caution"
    assert r["recommendation"].startswith(CAUTION_RECOMMENDATION)
    assert r["recommendation"] != SAFE_RECOMMENDATION
    assert "reversed" in r["recommendation"]
    assert r["exit_code"] == 0


# --- the core invariant: a verdict-changing warning is never "safe to scan" --

def test_headline_never_safe_when_verdict_changing_warning_present(tmp_path):
    fixtures = {
        # a clean control (no verdict-changing warning) -> safe + clean
        "clean": _write_stereo(tmp_path / "clean.wav",
                               caller_segments=[(3.0, 3.7)],
                               agent_segments=[(0.2, 5.8)]),
        # low signal
        "low": _write_stereo(tmp_path / "low.wav",
                             caller_segments=[(3.0, 3.7)],
                             agent_segments=[(0.2, 5.8)],
                             caller_amp=0.02, agent_amp=0.02),
        # possible swap
        "swap": _write_stereo(tmp_path / "swap.wav",
                              caller_segments=[(0.2, 5.8)],
                              agent_segments=[(2.0, 2.5)]),
        # loud cross-channel leakage (above the fixed -40 dB bar)
        "leak": _write_bleed(tmp_path / "leak.wav", gain=0.03),
    }
    for name, p in fixtures.items():
        r = trust_report(p)
        if _has_verdict_changing_warning(r):
            assert r["recommendation"] != SAFE_RECOMMENDATION, name
            assert r["recommendation"].startswith(CAUTION_RECOMMENDATION), name
        else:
            # the clean control: no verdict-changing warning -> safe + clean
            assert name == "clean"
            assert r["recommendation"] == SAFE_RECOMMENDATION
            assert r["input_health"] == "clean"
    # sanity: the three defect fixtures each cautioned
    for name in ("low", "swap", "leak"):
        assert trust_report(fixtures[name])["input_health"] == "caution"


# --- leakage: fixed -40 dB bar still cautions -------------------------------

def test_loud_leakage_cautions_via_fixed_bar(tmp_path):
    p = _write_bleed(tmp_path / "loud-leak.wav", gain=0.03)
    r = trust_report(p)
    ct = r["crosstalk_risk"]
    assert ct["leakage_db"] >= LEAKAGE_WARN_DB          # the fixed bar fires
    assert ct["suspected"] is True
    assert r["input_health"] == "caution"
    assert r["recommendation"].startswith(CAUTION_RECOMMENDATION)


# --- dynamic leakage: cautions EARLIER than the fixed -40 dB bar (rank 5b) ---

def test_dynamic_leakage_cautions_earlier_than_fixed_bar(tmp_path):
    # A LOUD source (agent) with a faint-RATIO leak (~ -45 dB, below the -40 dB bar):
    # the fixed rule would say "safe", but the leaked copy's ABSOLUTE level still
    # clears the receiving channel's VAD gate for a sustained run, so the dynamic
    # rule cautions -- catching a verdict-corrupting leak the fixed bar misses.
    p = _write_bleed(tmp_path / "loud-src-faint-ratio.wav", gain=0.0056, agent_amp=0.7)
    r = trust_report(p)
    ct = r["crosstalk_risk"]
    assert ct["leakage_db"] is not None
    assert ct["leakage_db"] < LEAKAGE_WARN_DB           # BELOW the fixed bar...
    assert ct["suspected"] is True                      # ...yet flagged (dynamic rule)
    assert r["input_health"] == "caution"
    assert r["recommendation"].startswith(CAUTION_RECOMMENDATION)
    assert r["recommendation"] != SAFE_RECOMMENDATION


def test_below_report_faint_leak_stays_clean(tmp_path):
    # A bleed too faint to be reliably ESTIMATED (below LEAKAGE_REPORT_DB): no
    # consistent copy is reported, so the mask test never runs and nothing is
    # fabricated as a warning. The don't-cry-wolf guard: caution is only ever ADDED
    # to a reported, mask-altering leak, never invented for a copy we cannot measure.
    p = _write_bleed(tmp_path / "veryfaint.wav", gain=0.004)
    r = trust_report(p)
    ct = r["crosstalk_risk"]
    assert ct["leakage_db"] is None
    assert ct["suspected"] is False
    assert r["input_health"] == "clean"
    assert r["recommendation"] == SAFE_RECOMMENDATION


# --- multiple verdict-changing warnings compose one caution headline --------

def test_combined_caution_reasons_are_composed(tmp_path):
    # Quiet AND swapped: both verdict-changing warnings must appear in the single
    # "scan with caution: ..." headline.
    p = _write_stereo(tmp_path / "quiet-swapped.wav",
                      caller_segments=[(0.2, 5.8)],
                      agent_segments=[(2.0, 2.5)],
                      caller_amp=0.02, agent_amp=0.02)
    r = trust_report(p)
    assert r["input_health"] == "caution"
    assert r["recommendation"].startswith(CAUTION_RECOMMENDATION)
    assert "signal level very low" in r["recommendation"]
    assert "reversed" in r["recommendation"]


# --- input_health = not_scorable when a gate fails --------------------------

def test_input_health_not_scorable_when_gate_fails(tmp_path):
    # The caller channel never speaks: not scorable -> input_health reflects it.
    p = _write_stereo(tmp_path / "silent-caller.wav",
                      caller_segments=[],
                      agent_segments=[(0.2, 5.8)])
    r = trust_report(p)
    assert r["scorable"] is False
    assert r["input_health"] == "not_scorable"
    assert r["recommendation"].startswith("NOT SCORABLE:")
    assert r["exit_code"] == 2
