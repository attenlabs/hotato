"""R-01 (finish): the run-gate must refuse genuine cross-channel LEAKAGE, but must
NOT refuse a legitimate short BARGE-IN, and must treat a suspected channel SWAP as
a NON-FATAL caveat (score, don't refuse).

Wave-2 wired ``hotato run`` / the MCP ``run`` tool through trust's K6 channel gate
but hard-refused two things it should not have. (1) A LEGITIMATE barge-in: the
whole-clip echo COHERENCE (a cosine of the two RMS ENVELOPES) reads high for ANY
two channels whose active windows overlap -- including two INDEPENDENT distinct
speakers in genuine simultaneous speech -- so ``crosstalk_verdict_suspected`` fired
on real barge-ins. (2) A legitimate CALLER-DOMINANT recording: the possible-swap
heuristic fires whenever the caller simply talks more, but hotato does ADDRESSEE
detection, not speaker-ID, so a SWAP cannot be reliably told from timing. Refusing
either is unacceptable.

The barge-in fix corroborates the envelope-coherence trigger with the sample-level
cross-correlation of the RAW waveforms (``trust._waveform_copy_corr``): a genuine
leak is one channel carrying a delayed COPY of the other's waveform, so the
waveforms correlate (~1.0); two distinct speakers do not (~0). The VERDICT-level
coherence bar now fires ONLY when the waveforms also correlate. The swap fix makes
a suspected swap a non-fatal ``channel_mapping_caveat`` on a STILL-SCORING event
instead of a refusal.

Pinned here (deterministic synthetic PCM; no corpus, no optional extra):
  * a clean barge-in (two distinct speakers, overlapping) SCORES (exit 0/1), not refuse;
  * a channel SWAP (caller-dominant) SCORES with a caveat -- it does NOT refuse -- CLI and core;
  * a genuine delayed-copy leak still refuses via the coherence path;
  * the leak refusal is CONSISTENT across run (scan mode) and contract (contract mode);
  * a clean non-overlap run is byte-identical to the pre-gate engine;
  * the MCP ``run`` tool scores a swap (with caveat) and a barge-in, refuses a leak,
    and honors --confirm-channels.
"""

import json
import math
import struct
import wave

from hotato import cli, core
from hotato import contract as contract_mod
from hotato import mcp_server
from hotato import trust as trust_mod


# --- deterministic synthetic fixtures ---------------------------------------

def _write_stereo(path, caller_segments, agent_segments, *, duration_sec=3.0,
                  sr=16000, caller_amp=0.35, agent_amp=0.35,
                  caller_hz=220.0, agent_hz=330.0):
    """Two-channel PCM WAV: caller (channel 0) and agent (channel 1) are DISTINCT
    pure tones inside their active segments, exact digital silence outside. Two
    different frequencies = two independent speakers, not a copy of each other."""
    n = int(duration_sec * sr)

    def _on(segments, t):
        return any(start <= t < end for start, end in segments)

    frames = bytearray()
    for i in range(n):
        t = i / sr
        c = int(caller_amp * 32767 * math.sin(2 * math.pi * caller_hz * i / sr)) if _on(caller_segments, t) else 0
        a = int(agent_amp * 32767 * math.sin(2 * math.pi * agent_hz * i / sr)) if _on(agent_segments, t) else 0
        frames += struct.pack("<hh", c, a)
    with wave.open(str(path), "wb") as w:
        w.setnchannels(2)
        w.setsampwidth(2)
        w.setframerate(sr)
        w.writeframes(bytes(frames))
    return str(path)


def _write_raw_stereo(path, caller, agent, *, sr=16000):
    n = len(caller)
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


def _tone(n, seg, hz, *, sr=16000, amp=0.35):
    s, e = seg
    return [amp * math.sin(2 * math.pi * hz * i / sr) if s <= i / sr < e else 0.0
            for i in range(n)]


def _pure_echo_stereo(path, *, atten_db=-3.0, delay_sec=0.12, sr=16000,
                      duration_sec=8.0):
    """The genuine-leak fixture the COHERENCE path (not the copy-ratio path) must
    catch: the caller channel is ENTIRELY a delayed, attenuated COPY of the agent
    waveform. At -3 dB the copy is LOUDER than the leakage detector's -6 dB
    attenuation gate, so ``leakage_db`` stays None and ONLY the whole-clip
    coherence path can refuse it -- exactly the path the waveform corroboration
    guards. Its raw waveforms correlate (~1.0), so the corroboration passes and
    the verdict is refused."""
    n = int(duration_sec * sr)
    g = 10 ** (atten_db / 20.0)
    delay = int(delay_sec * sr)
    agent = [0.0] * n
    for seg in [(0.2, 3.4), (5.5, 7.8)]:
        t = _tone(n, seg, 330.0, sr=sr)
        for i in range(n):
            agent[i] += t[i]
    caller = [0.0] * n
    for i in range(n):
        j = i - delay
        if 0 <= j < n:
            caller[i] = g * agent[j]
    return _write_raw_stereo(path, caller, agent, sr=sr)


# The exact "clean barge-in" fixture wave-2 regressed on: caller (220 Hz) barges
# in 1.0-2.0s over the agent (330 Hz) holding 0.0-1.5s. Two DISTINCT speakers,
# cleanly separated onto their channels, overlapping 1.0-1.5s. scorable, NOT a
# swap, yet high envelope coherence -- a legitimate barge-in.
def _clean_barge_in(tmp_path):
    return _write_stereo(tmp_path / "barge.wav",
                         caller_segments=[(1.0, 2.0)],
                         agent_segments=[(0.0, 1.5)],
                         duration_sec=3.0)


def _swap_fixture(tmp_path):
    # Long dominant speaker mapped as caller (channel 0), brief interjector as
    # agent -- the reverse of the usual agent-dominant pattern -> swap heuristic.
    return _write_stereo(tmp_path / "swapped.wav",
                         caller_segments=[(0.2, 5.8)],
                         agent_segments=[(1.0, 2.0)],
                         duration_sec=6.0)


def _clean_nonoverlap(tmp_path):
    return _write_stereo(tmp_path / "clean.wav",
                         caller_segments=[(3.0, 3.7)],
                         agent_segments=[(0.2, 5.8)],
                         duration_sec=6.0)


# --- the blocker: a legitimate barge-in must SCORE, not refuse ---------------

def test_clean_barge_in_is_verdict_eligible(tmp_path):
    """The distinct-speaker barge-in is scorable, NOT a swap, and its envelope
    coherence is at/above the scan verdict bar (so the pre-fix gate refused it) --
    yet it is now verdict-ELIGIBLE, because its raw waveforms do not correlate."""
    p = _clean_barge_in(tmp_path)
    r = trust_mod.trust_report(p, mode=trust_mod.VERDICT_MODE_SCAN)
    assert r["scorable"] is True
    assert r["channels"]["possible_swap"] is False
    # The regression trigger: envelope coherence reaches the verdict bar.
    assert r["crosstalk_risk"]["coherence"] >= trust_mod.VERDICT_COHERENCE_THRESHOLD[
        trust_mod.VERDICT_MODE_SCAN]
    # Fixed: two independent speakers are not leakage, so the verdict is eligible.
    assert r["verdict_eligible"] is True
    assert r["verdict_ineligible_reason"] is None


def test_clean_barge_in_run_scores_not_refuses(tmp_path):
    """`hotato run` (gate enabled) SCORES the barge-in -- exit reflects the real
    pass/fail, never a not-scorable refusal (exit 2)."""
    p = _clean_barge_in(tmp_path)
    env = core.run_single(stereo=p, expect="yield", onset_sec=1.2,
                          gate_verdict_eligibility=True)
    event = env["events"][0]
    assert event.get("scorable") is not False
    assert "not_scorable_reason" not in event
    assert core.process_exit_code(env) != 2


def test_clean_barge_in_cli_scores(tmp_path, capsys):
    """CLI end-to-end: the barge-in renders a real verdict, not [NOT SCORABLE]."""
    p = _clean_barge_in(tmp_path)
    rc = cli.main(["run", "--stereo", p, "--expect", "yield", "--onset", "1.2",
                   "--format", "text"])
    out = capsys.readouterr().out
    assert rc != 2
    assert "[NOT SCORABLE]" not in out
    assert "process_exit_code=2" not in out


def test_barge_in_would_refuse_without_waveform_corroboration(tmp_path):
    """Mechanism, fail-pre-fix / pass-post-fix at the unit level: for a coherence
    at/above the bar with a clean leakage dict, the OLD contract (no waveform
    measurement) refuses (True), while supplying the barge-in's low waveform
    correlation flips it to eligible (False). The genuine-copy correlation (>= the
    bar) still refuses."""
    leakage_clean = {"leakage_db": None, "leakage_alters_mask": False,
                     "leakage_crosses_gate": False}
    coh = 0.82  # the barge-in's coherence, above the scan bar
    # Pre-fix behavior is preserved when no measurement is supplied.
    assert trust_mod.crosstalk_verdict_suspected(
        coh, leakage_clean, mode=trust_mod.VERDICT_MODE_SCAN) is True
    # A distinct-speaker overlap (waveforms uncorrelated) is NOT refused.
    assert trust_mod.crosstalk_verdict_suspected(
        coh, leakage_clean, mode=trust_mod.VERDICT_MODE_SCAN,
        waveform_corr=0.02) is False
    # A genuine delayed copy (waveforms correlate) IS still refused.
    assert trust_mod.crosstalk_verdict_suspected(
        coh, leakage_clean, mode=trust_mod.VERDICT_MODE_SCAN,
        waveform_corr=0.98) is True


# --- the discriminator itself -----------------------------------------------

def test_waveform_copy_corr_separates_copy_from_independent(tmp_path):
    """``_waveform_copy_corr`` is ~0 for two independent speakers and ~1 for a
    delayed copy, with a wide margin around ``WAVEFORM_LEAKAGE_MIN_CORR``."""
    barge = _clean_barge_in(tmp_path)
    echo = _pure_echo_stereo(tmp_path / "echo.wav")
    for path, expect_low in ((barge, True), (echo, False)):
        sig = core._read_wav(path)
        n = sig.num_samples
        active = [True] * (n // 160 + 1)
        wc = trust_mod._waveform_copy_corr(
            sig.get(0), sig.get(1), sig.sample_rate, 0.12, 0.01, active, active)
        if expect_low:
            assert wc < trust_mod.WAVEFORM_LEAKAGE_MIN_CORR
            assert wc < 0.1  # independent speakers correlate near zero
        else:
            assert wc >= trust_mod.WAVEFORM_LEAKAGE_MIN_CORR
            assert wc > 0.9  # a delayed copy correlates near one


def test_waveform_copy_corr_stdlib_fallback_matches_discrimination(tmp_path, monkeypatch):
    """With numpy absent the pure-Python fallback reaches the SAME copy-vs-
    independent verdict (a copy clears the bar, distinct speakers do not), so the
    fix holds on the zero-dependency (stdlib-only) install too."""
    import hotato._engine.audio as _audio
    monkeypatch.setattr(_audio, "_np", None)
    barge = _clean_barge_in(tmp_path)
    echo = _pure_echo_stereo(tmp_path / "echo.wav")

    sb = core._read_wav(barge)
    from hotato._engine.audio import frame_rms
    from hotato._engine.vad import energy_vad
    from hotato._engine.score import ScoreConfig
    cfg = ScoreConfig()

    def _active(sig):
        rms_c, hop = frame_rms(sig.get(0), sig.sample_rate, cfg.frame_ms, cfg.hop_ms)
        rms_a, _ = frame_rms(sig.get(1), sig.sample_rate, cfg.frame_ms, cfg.hop_ms)
        return (energy_vad(rms_c, hop, cfg.caller_vad).active,
                energy_vad(rms_a, hop, cfg.agent_vad).active, hop)

    ca, aa, hop = _active(sb)
    wc_barge = trust_mod._waveform_copy_corr(
        sb.get(0), sb.get(1), sb.sample_rate, 0.5, hop, ca, aa)
    se = core._read_wav(echo)
    ce, ae, hop_e = _active(se)
    wc_echo = trust_mod._waveform_copy_corr(
        se.get(0), se.get(1), se.sample_rate, 0.12, hop_e, ce, ae)
    assert wc_barge < trust_mod.WAVEFORM_LEAKAGE_MIN_CORR
    assert wc_echo >= trust_mod.WAVEFORM_LEAKAGE_MIN_CORR


# --- genuine leak still refuses (the coherence path is preserved) ------------

def test_genuine_copy_leak_still_refuses_via_coherence_path(tmp_path):
    """A -3 dB delayed copy is above the leakage detector's attenuation gate, so
    ``leakage_db`` is None and ONLY the coherence path can refuse it. Its
    waveforms correlate, so the corroboration passes and the verdict is refused --
    proving the fix did not neuter genuine echo detection."""
    p = _pure_echo_stereo(tmp_path / "echo.wav")
    r = trust_mod.trust_report(p, mode=trust_mod.VERDICT_MODE_SCAN)
    assert r["scorable"] is True
    assert r["channels"]["possible_swap"] is False
    # The copy-ratio leakage path does NOT flag it (copy is too loud to attenuate-gate)...
    assert r["crosstalk_risk"]["leakage_db"] is None
    # ...so only the coherence path can, and it does.
    assert r["crosstalk_risk"]["coherence"] >= trust_mod.VERDICT_COHERENCE_THRESHOLD[
        trust_mod.VERDICT_MODE_SCAN]
    assert r["verdict_eligible"] is False
    env = core.run_single(stereo=p, expect="yield", onset_sec=1.0,
                          gate_verdict_eligibility=True)
    assert env["events"][0].get("scorable") is False
    assert core.process_exit_code(env) == 2


def test_genuine_leak_consistent_between_run_and_contract(tmp_path):
    """HARD REQUIREMENT: a genuinely leaky recording is handled the SAME by the
    run path (trust scan mode) and the contract path (trust contract mode + the
    contract's own ``_channel_verdict_eligible``). Both refuse the verdict."""
    p = _pure_echo_stereo(tmp_path / "echo.wav")
    run_rep = trust_mod.trust_report(p, mode=trust_mod.VERDICT_MODE_SCAN)
    contract_rep = trust_mod.trust_report(p, mode=trust_mod.VERDICT_MODE_CONTRACT)
    assert run_rep["verdict_eligible"] is False
    assert contract_rep["verdict_eligible"] is False
    # The contract consumes it through this exact function -> not eligible, same reason.
    elig, reason = contract_mod._channel_verdict_eligible(contract_rep)
    assert elig is False
    assert reason == trust_mod.VERDICT_INELIGIBLE_REASON
    # And the run command itself refuses (process exit 2), matching contract.
    env = core.run_single(stereo=p, expect="yield", onset_sec=1.0,
                          gate_verdict_eligibility=True)
    assert core.process_exit_code(env) == 2


# --- swap scores with a caveat (does NOT refuse); clean byte-identical --------

def test_channel_swap_scores_with_caveat(tmp_path, capsys):
    """HARD REQUIREMENT: a suspected SWAP is a NON-FATAL caveat, not a refusal.
    A caller-dominant (swap-suspect) run SCORES -- it does NOT exit 2 -- and
    carries a channel_mapping_caveat, CLI and core. hotato does addressee
    detection, not speaker-ID, so a swap cannot be reliably told from timing and
    must not false-refuse a legitimate caller-dominant recording."""
    p = _swap_fixture(tmp_path)
    r = trust_mod.trust_report(p)
    assert r["channels"]["possible_swap"] is True
    # The swap trips no LEAKAGE refusal (distinct speakers, uncorrelated waveforms).
    assert r["crosstalk_verdict_refused"] is False
    env = core.run_single(stereo=p, expect="yield", onset_sec=1.2,
                          gate_verdict_eligibility=True)
    assert core.process_exit_code(env) != 2
    assert env["events"][0].get("scorable") is not False
    assert env["events"][0]["channel_mapping_caveat"]["reason"] == \
        trust_mod.CHANNEL_MAPPING_CAVEAT_REASON
    rc = cli.main(["run", "--stereo", p, "--expect", "yield", "--onset", "1.2",
                   "--format", "json"])
    assert rc != 2
    event = json.loads(capsys.readouterr().out)["events"][0]
    assert event.get("scorable") is not False
    assert "channel_mapping_caveat" in event


def test_clean_nonoverlap_run_byte_identical_gated_vs_ungated(tmp_path):
    """HARD REQUIREMENT: a clean, non-overlapping recording is byte-identical
    whether or not the gate is enabled -- the gate injects nothing on an eligible
    recording."""
    p = _clean_nonoverlap(tmp_path)
    gated = core.run_single(stereo=p, expect="yield", onset_sec=3.0,
                            gate_verdict_eligibility=True)
    ungated = core.run_single(stereo=p, expect="yield", onset_sec=3.0)
    assert json.dumps(gated, sort_keys=True) == json.dumps(ungated, sort_keys=True)
    assert "scorable" not in gated["events"][0]


# --- MCP surface: the same gate closes the MCP run tool ----------------------

def test_mcp_run_swapped_scores_with_caveat(tmp_path):
    """The MCP ``run`` tool SCORES a channel-swap-suspect (caller-dominant)
    recording -- it does NOT refuse -- and its event carries the non-fatal
    channel_mapping_caveat, so the caller can confirm the mapping."""
    p = _swap_fixture(tmp_path)
    resp = mcp_server._run_tool(stereo=p, expect="yield", onset_sec=1.2)
    assert resp.get("error_code") is None
    assert "events" in resp
    assert resp["events"][0].get("scorable") is not False
    assert resp["events"][0]["channel_mapping_caveat"]["reason"] == \
        trust_mod.CHANNEL_MAPPING_CAVEAT_REASON


def test_mcp_run_confirm_channels_suppresses_caveat(tmp_path):
    """The MCP escape hatch: ``channel_map_confirmed=True`` scores the same
    swap-suspect recording with NO caveat (the caller vouched for the mapping)."""
    p = _swap_fixture(tmp_path)
    resp = mcp_server._run_tool(stereo=p, expect="yield", onset_sec=1.2,
                                channel_map_confirmed=True)
    assert resp.get("ok") is not False
    assert resp.get("error_code") is None
    assert "events" in resp
    assert "channel_mapping_caveat" not in resp["events"][0]


def test_mcp_run_barge_in_scores(tmp_path):
    """The MCP ``run`` tool SCORES a legitimate barge-in (no false refusal)."""
    p = _clean_barge_in(tmp_path)
    resp = mcp_server._run_tool(stereo=p, expect="yield", onset_sec=1.2)
    assert resp.get("error_code") is None
    assert "events" in resp
    assert resp["events"][0].get("scorable") is not False


def test_mcp_clean_core_byte_identical_to_ungated(tmp_path):
    """A clean recording's MCP envelope CORE (control keys popped) is byte-
    identical to the ungated ``run_single`` -- the MCP gate injects nothing on an
    eligible recording."""
    p = _clean_nonoverlap(tmp_path)
    resp = mcp_server._run_tool(stereo=p, expect="yield", onset_sec=3.0)
    core_env = dict(resp)
    for k in ("evidence_status", "refusal_reason", "artifact_digests",
              "pending_irreversible_action"):
        core_env.pop(k, None)
    plain = core.run_single(stereo=p, expect="yield", onset_sec=3.0)
    assert json.dumps(core_env, sort_keys=True) == json.dumps(plain, sort_keys=True)
