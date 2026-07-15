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

import hotato  # noqa: F401  -- registers the real diarizer factories
from hotato import cli
from hotato import diarize as _diarize
from hotato import trust as trust_mod
from hotato.trust import (
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


# --- K6: verdict eligibility is a NARROWER, separate gate from scorability --

def test_swapped_channels_are_candidate_eligible_but_not_verdict_eligible(tmp_path):
    # A suspected swap keeps the input CANDIDATE-eligible (scan can still surface
    # advisory candidates + audio) but must refuse a VERDICT: verdict_eligible is
    # False with the honest reason, distinct from (and narrower than) scorable.
    p = _write_stereo(tmp_path / "swapped.wav",
                      caller_segments=[(0.2, 5.8)],
                      agent_segments=[(2.0, 2.5)])
    r = trust_report(p)
    assert r["channels"]["possible_swap"] is True
    assert r["scorable"] is True
    assert r["candidate_eligible"] is True
    assert r["verdict_eligible"] is False
    assert r["verdict_ineligible_reason"] == trust_mod.VERDICT_INELIGIBLE_REASON
    assert r["exit_code"] == 0  # candidate eligibility, NOT the verdict gate


def test_confirmed_channel_mapping_flips_verdict_eligible_back_on(tmp_path):
    # A human (or authenticated provider metadata) explicit channel-map
    # confirmation overrides the swap-driven verdict refusal -- it scores
    # normally again. It never touches candidate eligibility (already True).
    p = _write_stereo(tmp_path / "swapped.wav",
                      caller_segments=[(0.2, 5.8)],
                      agent_segments=[(2.0, 2.5)])
    r = trust_report(p, channel_map_confirmed=True)
    assert r["channels"]["possible_swap"] is True
    assert r["scorable"] is True
    assert r["verdict_eligible"] is True
    assert r["verdict_ineligible_reason"] is None
    assert r["channel_map_confirmed"] is True


def test_not_scorable_input_is_never_verdict_eligible(tmp_path):
    # A confirmation cannot rescue an input that fails the not-scorable gate
    # outright: verdict_eligible is False regardless.
    p = _write_stereo(tmp_path / "silent-caller.wav",
                      caller_segments=[],
                      agent_segments=[(0.2, 5.8)])
    r = trust_report(p, channel_map_confirmed=True)
    assert r["scorable"] is False
    assert r["candidate_eligible"] is False
    assert r["verdict_eligible"] is False


def test_clean_call_is_verdict_eligible(tmp_path):
    p = _write_stereo(tmp_path / "clean.wav",
                      caller_segments=[(3.0, 3.7)],
                      agent_segments=[(0.2, 5.8)])
    r = trust_report(p)
    assert r["verdict_eligible"] is True
    assert r["verdict_ineligible_reason"] is None
    assert r["candidate_eligible"] is True
    assert r["verdict_mode"] == trust_mod.VERDICT_MODE_SCAN


def test_invalid_verdict_mode_is_rejected(tmp_path):
    p = _write_stereo(tmp_path / "clean.wav",
                      caller_segments=[(3.0, 3.7)],
                      agent_segments=[(0.2, 5.8)])
    with pytest.raises(ValueError, match="mode must be one of"):
        trust_report(p, mode="bogus")


def test_stricter_contract_mode_crosstalk_threshold_refuses_where_scan_warns():
    # A synthetic leak/coherence reading that sits UNDER every scan-level bar
    # (no mask alteration, no gate-crossing, coherence and ratio both below the
    # scan bars) but AT/ABOVE the stricter contract-mode ratio bar: scan stays
    # verdict-eligible, contract refuses. Exercises the exact mechanism
    # `contract create`/`contract verify` rely on for the higher-stakes gate.
    leakage = {"leakage_db": -43.0, "leakage_alters_mask": False,
               "leakage_crosses_gate": False}
    coherence = 0.5  # well under both DEFAULT_COHERENCE_THRESHOLD and the 0.6 contract bar
    assert coherence < trust_mod.VERDICT_COHERENCE_THRESHOLD[trust_mod.VERDICT_MODE_CONTRACT]
    assert -43.0 < trust_mod.VERDICT_LEAKAGE_DB[trust_mod.VERDICT_MODE_SCAN]  # under -40
    assert -43.0 >= trust_mod.VERDICT_LEAKAGE_DB[trust_mod.VERDICT_MODE_CONTRACT]  # at/over -46
    assert trust_mod.crosstalk_verdict_suspected(
        coherence, leakage, mode=trust_mod.VERDICT_MODE_SCAN) is False
    assert trust_mod.crosstalk_verdict_suspected(
        coherence, leakage, mode=trust_mod.VERDICT_MODE_CONTRACT) is True


def test_crosstalk_verdict_suspected_scan_mode_matches_existing_suspected_bar():
    # For mode="scan", crosstalk_verdict_suspected must read IDENTICALLY to the
    # existing crosstalk_risk.suspected computation (echo_suspected OR
    # leakage_suspected) -- the new gate never silently changes scan behavior.
    leakage_clean = {"leakage_db": None, "leakage_alters_mask": False,
                      "leakage_crosses_gate": False}
    assert trust_mod.crosstalk_verdict_suspected(
        0.5, leakage_clean, mode=trust_mod.VERDICT_MODE_SCAN) is False
    leakage_loud = {"leakage_db": -35.0, "leakage_alters_mask": False,
                     "leakage_crosses_gate": False}
    assert trust_mod.crosstalk_verdict_suspected(
        0.5, leakage_loud, mode=trust_mod.VERDICT_MODE_SCAN) is True
    assert trust_mod.crosstalk_verdict_suspected(
        0.9, leakage_clean, mode=trust_mod.VERDICT_MODE_SCAN) is True


def test_high_leakage_is_not_verdict_eligible(tmp_path):
    p = _bleed_stereo(tmp_path / "bleed.wav", gain=0.03)
    r = trust_report(p)
    assert r["scorable"] is True
    assert r["verdict_eligible"] is False
    assert r["verdict_ineligible_reason"] == trust_mod.VERDICT_INELIGIBLE_REASON


def test_confirmed_channel_mapping_does_not_clear_crosstalk_verdict(tmp_path):
    # R-05: --confirm-channels answers ONLY "are the roles reversed?" -- it must
    # never clear a crosstalk/leakage verdict refusal. A recording whose channels
    # are correctly mapped but still bleed into each other misattributes one
    # party's audio to the other, so the timing verdict stays untrustworthy. On
    # pre-fix code the `elif channel_map_confirmed` branch flipped this back to
    # eligible; this asserts confirmation cannot launder echo bleed into a trusted
    # verdict.
    p = _bleed_stereo(tmp_path / "bleed.wav", gain=0.03)
    r = trust_report(p, channel_map_confirmed=True)
    assert r["scorable"] is True
    assert r["channel_map_confirmed"] is True
    assert r["verdict_eligible"] is False
    assert r["verdict_ineligible_reason"] == trust_mod.VERDICT_INELIGIBLE_REASON


def test_confirmed_channel_mapping_does_not_clear_crosstalk_verdict_contract(tmp_path):
    # R-05, attestation path: the stricter contract mode (what `contract
    # create`/`contract verify` re-derive verdict_eligible under before signing a
    # PASS) must likewise refuse a confirmed-but-bleeding recording. Locks that
    # --confirm-channels cannot produce a signed trusted verdict over crosstalk.
    p = _bleed_stereo(tmp_path / "bleed.wav", gain=0.03)
    r = trust_report(p, channel_map_confirmed=True,
                     mode=trust_mod.VERDICT_MODE_CONTRACT)
    assert r["scorable"] is True
    assert r["verdict_eligible"] is False
    assert r["verdict_ineligible_reason"] == trust_mod.VERDICT_INELIGIBLE_REASON


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


# --- cross-channel leakage: warned + downgraded, scorability unchanged -------

def _write_raw_stereo(path, caller, agent, *, sr=16000):
    """Write two float channels (in [-1, 1]) to a 16-bit PCM stereo WAV."""
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


def _tone(n, seg, freq, *, sr=16000, amp=0.35):
    out = [0.0] * n
    a, b = int(seg[0] * sr), int(seg[1] * sr)
    for i in range(a, min(b, n)):
        out[i] = amp * math.sin(2 * math.pi * freq * i / sr)
    return out


def _bleed_stereo(path, gain, *, sr=16000):
    """Agent talks in two turns; the caller genuinely interjects once; and the
    caller channel additionally carries a delayed, attenuated COPY of the agent
    (echo bleed at ``gain``). At a loud enough gain the leaked agent audio is read
    as caller activity across the agent's whole span -- the exact input on which a
    downstream timing verdict was red-teamed into flipping."""
    dur = 8.0
    n = int(dur * sr)
    delay = int(0.12 * sr)
    agent = [0.0] * n
    for seg in [(0.2, 3.4), (5.5, 7.8)]:
        t = _tone(n, seg, 330.0, sr=sr)
        for i in range(n):
            agent[i] += t[i]
    caller = _tone(n, (3.0, 4.0), 220.0, sr=sr)
    for i in range(n):
        j = i - delay
        if 0 <= j < n:
            caller[i] += gain * agent[j]
    return _write_raw_stereo(path, caller, agent, sr=sr)


def test_high_leakage_is_not_safe_to_scan(tmp_path):
    # ~ -30 dB symmetric bleed: a loud, consistent delayed copy of the agent on
    # the caller channel. trust must stop calling this "safe to scan".
    p = _bleed_stereo(tmp_path / "bleed.wav", gain=0.03)
    r = trust_report(p)
    ct = r["crosstalk_risk"]
    assert ct["leakage_db"] is not None
    assert ct["leakage_db"] >= trust_mod.LEAKAGE_WARN_DB  # loud enough to matter
    assert ct["leakage_direction"] == "agent_into_caller"
    assert ct["suspected"] is True
    assert any("leakage" in w for w in r["warnings"])
    assert r["recommendation"].startswith(trust_mod.CAUTION_RECOMMENDATION)
    assert r["recommendation"] != SAFE_RECOMMENDATION
    # DISCLOSED, never silently rescored: scorability + exit code are unchanged.
    assert r["scorable"] is True
    assert r["exit_code"] == 0


def test_clean_dual_channel_has_no_leakage_and_stays_safe(tmp_path):
    p = _write_stereo(tmp_path / "clean.wav",
                      caller_segments=[(3.0, 3.7)],
                      agent_segments=[(0.2, 5.8)])
    r = trust_report(p)
    assert r["crosstalk_risk"]["leakage_db"] is None
    assert r["crosstalk_risk"]["suspected"] is False
    assert not any("leakage" in w for w in r["warnings"])
    assert r["recommendation"] == SAFE_RECOMMENDATION


def test_leak_below_fixed_bar_that_alters_mask_cautions(tmp_path):
    # A bleed whose RATIO (~ -46 dB) is below the fixed LEAKAGE_WARN_DB bar, yet the
    # leaked agent copy still crosses the caller channel's VAD gate and would move the
    # measured caller onset. The mask-alteration test flags it regardless of the
    # absolute/ratio dB -- this is the ~6-11 dB gap the fixed bar used to leave open,
    # where a verdict-changing leak read clean. Disclosed, never silently rescored.
    p = _bleed_stereo(tmp_path / "faint.wav", gain=0.005)
    r = trust_report(p)
    ct = r["crosstalk_risk"]
    assert ct["leakage_db"] is not None
    assert ct["leakage_db"] < trust_mod.LEAKAGE_WARN_DB   # BELOW the fixed bar...
    assert ct["suspected"] is True                        # ...yet flagged (mask altered)
    assert r["recommendation"].startswith(trust_mod.CAUTION_RECOMMENDATION)
    assert r["recommendation"] != SAFE_RECOMMENDATION
    assert r["scorable"] is True
    assert r["exit_code"] == 0


def test_below_report_faint_leak_stays_clean(tmp_path):
    # A bleed too faint to be reliably estimated at all (below LEAKAGE_REPORT_DB):
    # no consistent copy is reported, so nothing is fabricated as a warning and the
    # recording stays eligible. The mask test only ever ADDS caution to a REPORTED
    # leak; it never invents one.
    p = _bleed_stereo(tmp_path / "veryfaint.wav", gain=0.004)
    r = trust_report(p)
    ct = r["crosstalk_risk"]
    assert ct["leakage_db"] is None
    assert ct["suspected"] is False
    assert r["recommendation"] == SAFE_RECOMMENDATION


# --- low input level: warned, scorability unchanged -------------------------

def test_low_signal_level_is_warned_without_blocking_scan(tmp_path):
    # Both channels captured very quietly (peak ~ -34 dBFS): timing may be
    # underestimated downstream, so trust warns -- scorability stays unchanged.
    p = _write_stereo(tmp_path / "quiet.wav",
                      caller_segments=[(3.0, 3.7)],
                      agent_segments=[(0.2, 5.8)],
                      caller_amp=0.02, agent_amp=0.02)
    r = trust_report(p)
    assert any("signal level very low" in w for w in r["warnings"])
    assert r["scorable"] is True
    assert r["exit_code"] == 0


def test_normal_level_has_no_low_signal_warning(tmp_path):
    p = _write_stereo(tmp_path / "normal.wav",
                      caller_segments=[(3.0, 3.7)],
                      agent_segments=[(0.2, 5.8)])
    r = trust_report(p)
    assert not any("signal level very low" in w for w in r["warnings"])
    assert r["recommendation"] == SAFE_RECOMMENDATION


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
    assert set(d["crosstalk_risk"]) == {"coherence", "lag_sec", "suspected",
                                        "leakage_db", "leakage_direction"}
    # A clean call carries no cross-channel leakage copy.
    assert d["crosstalk_risk"]["leakage_db"] is None
    assert d["crosstalk_risk"]["leakage_direction"] is None
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


# --- the --diarize path: a mono file's SEPARABILITY tier (still never a verdict)

def _timeline(segments, *, n_frames=600, hop=0.01):
    return [any(s <= k * hop < e for s, e in segments) for k in range(n_frames)]


@pytest.fixture
def stub_diarizer():
    """Register a stub diarizer for the test, restore the real factories after."""
    saved_f = dict(_diarize._DIARIZER_FACTORIES)
    saved_c = dict(_diarize._DIARIZER_CACHE)

    def _register(name, timelines=None, **kw):
        _diarize.register_diarizer_backend(
            name, _diarize.build_stub_backend(timelines, **kw)
        )

    try:
        yield _register
    finally:
        _diarize._DIARIZER_FACTORIES.clear()
        _diarize._DIARIZER_FACTORIES.update(saved_f)
        _diarize._DIARIZER_CACHE.clear()
        _diarize._DIARIZER_CACHE.update(saved_c)


def test_mono_diarize_reports_high_tier_and_is_scorable(tmp_path, stub_diarizer):
    p = _write_mono(tmp_path / "mono.wav", segments=[(0.2, 5.8)])
    stub_diarizer("pyannote", {
        _diarize.SPEAKER_A: _timeline([(0.5, 2.0)]),
        _diarize.SPEAKER_B: _timeline([(2.5, 5.5)]),
    }, embedding_margin=0.6)
    r = trust_report(p, diarize=True)
    assert r["scorable"] is True
    assert r["exit_code"] == 0
    assert "separation" in r["scorability"]
    assert r["scorability"]["separation"]["confidence_tier"] == "high"
    assert r["confidence_tier"] == "high"
    assert r["indicative_only"] is False
    assert "diarized-mono" in r["recommendation"]
    # Still input-health only: NEVER a turn-taking verdict word anywhere.
    text = trust_mod.render_text(r).lower()
    for banned in ("did_yield", "yielded", "talk_over", "seconds_to_yield",
                   "pass ", "[pass]", "[fail]"):
        assert banned not in text


def test_mono_diarize_refuse_is_not_scorable_exit_two(tmp_path, stub_diarizer):
    p = _write_mono(tmp_path / "mono.wav", segments=[(0.2, 5.8)])
    # Only one speaker detected -> not two clean parties -> refuse.
    stub_diarizer("pyannote", {_diarize.SPEAKER_A: _timeline([(0.5, 5.5)])})
    r = trust_report(p, diarize=True)
    assert r["scorable"] is False
    assert r["exit_code"] == 2
    assert r["scorability"]["separation"]["confidence_tier"] == "refuse"
    assert r["not_scorable_reason"]
    assert r["next_step"]


def test_trust_default_mono_path_is_unaffected_by_diarize_support(tmp_path):
    """Without --diarize, a mono file is not scorable exactly as before -- the new
    support changes nothing on the default path (no 'separation' sub-block)."""
    p = _write_mono(tmp_path / "mono.wav", segments=[(0.2, 5.8)])
    r = trust_report(p)  # diarize defaults to False
    assert r["scorable"] is False
    assert "single channel" in r["not_scorable_reason"]
    assert "separation" not in r["scorability"]


def test_mono_diarize_cli_json_shape(tmp_path, capsys, stub_diarizer):
    p = _write_mono(tmp_path / "mono.wav", segments=[(0.2, 5.8)])
    stub_diarizer("pyannote", {
        _diarize.SPEAKER_A: _timeline([(0.5, 2.0)]),
        _diarize.SPEAKER_B: _timeline([(2.5, 5.5)]),
    }, embedding_margin=0.6)
    code = cli.main(["trust", "--stereo", p, "--diarize", "--format", "json"])
    assert code == 0
    d = json.loads(capsys.readouterr().out)
    assert d["scorability"]["separation"]["confidence_tier"] == "high"
    assert d["diarization"]["speaker_map"]["caller"]
    assert d["exit_code"] == 0


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
