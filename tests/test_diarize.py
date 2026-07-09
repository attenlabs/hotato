"""The opt-in, quality-gated mono-scorability front-end (speaker diarization).

Exercises the whole seam with a dependency-free STUB diarizer (no model, token,
or network), so every test runs offline. It pins the honesty invariants that make
this path shippable:

  1. DEFAULT byte-identical -- with no --mono/--diarize, this code is never
     reached and a mono file stays rejected exactly as before.
  2. NO SILENT FALLBACK -- a missing extra / unknown backend raises a clean
     BackendUnavailable; raw mono is never scored and passed off as separated.
  3. THE CONFIDENCE GATE IS REAL -- high scores (not indicative); low scores but
     is stamped indicative_only and NO SLA gate fires; refuse is not scorable
     (exit 2). Never a confident verdict on low-confidence separation.
  4. ECHO N/A -- on the diarized-mono path (two slices of one mic) echo/crosstalk
     is marked not-applicable and the echo gate cannot fire.

Plus the pipeline itself: stub end-to-end on a summed-to-mono file (a synthetic
one always, a vendored AMI clip when checked out), masked reconstruction, and the
caller/agent assignment proposal + override.
"""

import math
import os
import struct
import wave

import pytest

import hotato  # noqa: F401  -- importing registers the real diarizer factories
from hotato import core
from hotato import diarize as D
from hotato._engine.audio import frame_rms
from hotato._engine.score import ScoreConfig, score_channels
from hotato._engine.vad import BackendUnavailable, energy_vad

SR = 16000


# --- deterministic synthetic fixtures ---------------------------------------

def _channels(caller_segments, agent_segments, *, duration_sec=6.0, sr=SR,
              caller_amp=0.35, agent_amp=0.35):
    """Two float channels: caller a 220 Hz sine in its segments, agent a 330 Hz
    sine in its segments, exact silence outside. Deterministic to the sample."""
    n = int(duration_sec * sr)

    def _on(segments, t):
        return any(start <= t < end for start, end in segments)

    c = [caller_amp * math.sin(2 * math.pi * 220.0 * i / sr) if _on(caller_segments, i / sr) else 0.0
         for i in range(n)]
    a = [agent_amp * math.sin(2 * math.pi * 330.0 * i / sr) if _on(agent_segments, i / sr) else 0.0
         for i in range(n)]
    return c, a


def _write_mono_sum(path, c, a, *, sr=SR):
    """Sum two channels to a single-channel PCM WAV (the mixed-mono case)."""
    n = min(len(c), len(a))
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(sr)
        frames = bytearray()
        for i in range(n):
            v = c[i] + a[i]
            v = max(-1.0, min(1.0, v))
            frames += struct.pack("<h", int(round(v * 32767)))
        w.writeframes(bytes(frames))
    return str(path)


def _truth_timelines(c, a, *, sr=SR, cfg=None):
    """The two ground-truth per-frame VAD timelines from the ORIGINAL channels --
    a PERFECT diarizer stand-in (each party already isolated), on the reference
    hop grid."""
    if cfg is None:
        cfg = ScoreConfig()
    rc, hop = frame_rms(c, sr, cfg.frame_ms, cfg.hop_ms)
    ra, _ = frame_rms(a, sr, cfg.frame_ms, cfg.hop_ms)
    va = energy_vad(rc, hop, cfg.caller_vad).active
    vb = energy_vad(ra, hop, cfg.agent_vad).active
    return {D.SPEAKER_A: va, D.SPEAKER_B: vb}


@pytest.fixture
def stub_diarizer():
    """Register a stub diarizer for the duration of a test, then restore the real
    factories (registered by importing hotato) so tests stay isolated. Yields a
    ``register(name, timelines, **kw)`` helper."""
    saved_factories = dict(D._DIARIZER_FACTORIES)
    saved_cache = dict(D._DIARIZER_CACHE)

    def _register(name, timelines=None, **kw):
        D.register_diarizer_backend(name, D.build_stub_backend(timelines, **kw))

    try:
        yield _register
    finally:
        D._DIARIZER_FACTORIES.clear()
        D._DIARIZER_FACTORIES.update(saved_factories)
        D._DIARIZER_CACHE.clear()
        D._DIARIZER_CACHE.update(saved_cache)


# --- 1. stub pipeline end-to-end (summed-to-mono) ---------------------------

def test_stub_pipeline_scores_a_mono_file_end_to_end(tmp_path, stub_diarizer):
    # A brief caller interjection over an agent holding the floor: expect='hold'.
    c, a = _channels(caller_segments=[(3.0, 3.7)], agent_segments=[(0.2, 5.8)])
    mp = _write_mono_sum(tmp_path / "mono.wav", c, a)
    stub_diarizer("pyannote", _truth_timelines(c, a), embedding_margin=0.6)

    env = core.run_single(mono=mp, diarize=True, expect="hold")
    assert env["mode"] == "single"
    assert env["exit_code"] == 0
    ev = env["events"][0]
    # Scorable (the `scorable` key only appears on not-scorable events).
    assert ev.get("scorable") is not False
    # A real verdict, always tagged reconstructed-from-mono, never dual-channel.
    assert env["diarization"]["source"] == "diarized-mono"
    assert ev["diarization"]["confidence_tier"] == "high"
    assert ev["verdict"]["did_yield"] in (True, False)  # a real verdict exists
    # Provenance + license log carried in the envelope.
    assert env["diarization"]["model"]
    assert "pyannote-audio" in env["diarization"]["licenses"]


def test_high_tier_is_not_indicative(tmp_path, stub_diarizer):
    c, a = _channels(caller_segments=[(3.0, 3.7)], agent_segments=[(0.2, 5.8)])
    mp = _write_mono_sum(tmp_path / "mono.wav", c, a)
    stub_diarizer("pyannote", _truth_timelines(c, a), embedding_margin=0.6)
    env = core.run_single(mono=mp, diarize=True, expect="hold")
    ev = env["events"][0]
    assert ev["diarization"]["confidence_tier"] == "high"
    assert "indicative_only" not in ev  # high tier is a confident verdict


# --- 2. no silent fallback --------------------------------------------------

def test_unknown_backend_raises_backend_unavailable():
    with pytest.raises(BackendUnavailable) as ei:
        D.resolve_diarizer("no-such-backend")
    assert "not registered" in str(ei.value).lower()


def _pyannote_installed():
    try:
        import pyannote.audio  # noqa: F401
        return True
    except Exception:
        return False


def test_missing_extra_raises_clean_backend_unavailable_no_fallback(tmp_path):
    """With the real pyannote factory registered (importing hotato) and the
    [diarize] extra absent, a diarized run raises BackendUnavailable -- never a
    silent fallback that scores raw mono."""
    if _pyannote_installed():
        pytest.skip("pyannote.audio is installed here; the missing-extra path is not exercisable")
    c, a = _channels(caller_segments=[(3.0, 3.7)], agent_segments=[(0.2, 5.8)])
    mp = _write_mono_sum(tmp_path / "mono.wav", c, a)
    with pytest.raises(BackendUnavailable) as ei:
        core.run_single(mono=mp, diarize=True, diarizer="pyannote")
    msg = str(ei.value).lower()
    assert "diarize" in msg and ("extra" in msg or "install" in msg)


def test_mono_without_diarize_is_a_clean_usage_error(tmp_path):
    """--mono alone (no --diarize) is a clean usage error, never a raw-mono score."""
    c, a = _channels(caller_segments=[(3.0, 3.7)], agent_segments=[(0.2, 5.8)])
    mp = _write_mono_sum(tmp_path / "mono.wav", c, a)
    with pytest.raises(ValueError, match="requires --diarize"):
        core.run_single(mono=mp, diarize=False)


def test_hosted_backend_refuses_without_egress_opt_in(tmp_path, stub_diarizer):
    """The hosted backend uploads audio; it is refused unless egress is opted in
    (checked before any audio would be sent)."""
    c, a = _channels(caller_segments=[(3.0, 3.7)], agent_segments=[(0.2, 5.8)])
    mp = _write_mono_sum(tmp_path / "mono.wav", c, a)
    stub_diarizer("pyannoteai", _truth_timelines(c, a), embedding_margin=0.6)
    with pytest.raises(BackendUnavailable, match="egress"):
        core.run_single(mono=mp, diarize=True, diarizer="pyannoteai")
    # ...and it goes through once egress is opted in.
    env = core.run_single(mono=mp, diarize=True, diarizer="pyannoteai",
                          egress_opt_in=True, expect="hold")
    assert env["events"][0]["diarization"]["backend"] == "pyannoteai"


# --- 3. the confidence gate is real -----------------------------------------

def test_low_tier_scores_indicative_and_suppresses_sla_gate(tmp_path, stub_diarizer):
    """A low tier (here: a modest embedding margin) scores but is stamped
    indicative_only, and NO pass/fail SLA gate may fire on it -- while the SAME
    file at high tier DOES fail the same bound. That contrast proves the gate."""
    # A yield scenario: agent holds then yields to the caller (talk-over > 0.1s).
    c, a = _channels(caller_segments=[(3.0, 4.2)], agent_segments=[(0.2, 3.5)])
    mp = _write_mono_sum(tmp_path / "mono.wav", c, a)
    truth = _truth_timelines(c, a)

    # HIGH tier: a tight max-talk-over bound APPLIES and fails.
    stub_diarizer("pyannote", truth, embedding_margin=0.6)
    hi = core.run_single(mono=mp, diarize=True, expect="yield", max_talk_over_sec=0.1)
    assert hi["events"][0]["diarization"]["confidence_tier"] == "high"
    assert hi["summary"]["failed"] == 1  # SLA bound applied on high

    # LOW tier (modest margin): the SAME bound is SUPPRESSED, verdict is indicative.
    stub_diarizer("pyannote", truth, embedding_margin=0.3)
    lo = core.run_single(mono=mp, diarize=True, expect="yield", max_talk_over_sec=0.1)
    ev = lo["events"][0]
    assert ev["diarization"]["confidence_tier"] == "low"
    assert ev["indicative_only"] is True
    assert lo["summary"]["failed"] == 0  # no SLA gate fires on low


def test_refuse_one_speaker_is_not_scorable_exit_2(tmp_path, stub_diarizer):
    c, a = _channels(caller_segments=[(3.0, 3.7)], agent_segments=[(0.2, 5.8)])
    mp = _write_mono_sum(tmp_path / "mono.wav", c, a)
    truth = _truth_timelines(c, a)
    # Only one speaker detected -> cannot be two clean parties -> refuse.
    stub_diarizer("pyannote", {D.SPEAKER_A: truth[D.SPEAKER_A]})
    env = core.run_single(mono=mp, diarize=True, expect="hold")
    ev = env["events"][0]
    assert ev["scorable"] is False
    assert "1 speaker" in ev["not_scorable_reason"] or "not 2" in ev["not_scorable_reason"]
    assert ev["diarization"]["confidence_tier"] == "refuse"
    # A single not-scorable run maps to the CLI's exit-2 unusable-input convention.
    assert core.process_exit_code(env) == 2


def test_refuse_three_speakers_is_not_scorable(tmp_path, stub_diarizer):
    c, a = _channels(caller_segments=[(3.0, 3.7)], agent_segments=[(0.2, 5.8)])
    mp = _write_mono_sum(tmp_path / "mono.wav", c, a)
    truth = _truth_timelines(c, a)
    third = [False] * len(truth[D.SPEAKER_A])
    for k in range(10, 40):
        third[k] = True
    stub_diarizer("pyannote", {D.SPEAKER_A: truth[D.SPEAKER_A],
                               D.SPEAKER_B: truth[D.SPEAKER_B],
                               "SPEAKER_02": third})
    env = core.run_single(mono=mp, diarize=True, expect="hold")
    assert env["events"][0]["scorable"] is False
    assert "3" in env["events"][0]["not_scorable_reason"]


def test_refuse_near_silent_speaker_is_not_scorable(tmp_path, stub_diarizer):
    c, a = _channels(caller_segments=[(3.0, 3.7)], agent_segments=[(0.2, 5.8)])
    mp = _write_mono_sum(tmp_path / "mono.wav", c, a)
    truth = _truth_timelines(c, a)
    # Second "speaker" barely speaks -> spurious split of one party -> refuse.
    n = len(truth[D.SPEAKER_A])
    tiny = [False] * n
    tiny[5] = True
    stub_diarizer("pyannote", {D.SPEAKER_A: truth[D.SPEAKER_A], D.SPEAKER_B: tiny})
    env = core.run_single(mono=mp, diarize=True, expect="hold")
    ev = env["events"][0]
    assert ev["scorable"] is False
    assert ev["diarization"]["confidence_tier"] == "refuse"


def test_separation_confidence_tiers_unit():
    """The gate itself, on crafted results: high / low / refuse without any I/O."""
    hop = 0.01
    n = 600
    a = [i < 300 for i in range(n)]      # speaker A first half
    b = [i >= 300 for i in range(n)]     # speaker B second half (no overlap)
    dur = {D.SPEAKER_A: 3.0, D.SPEAKER_B: 3.0}

    def _res(margin=None, posterior=1.0, overlap=None, active=None):
        act = active or {D.SPEAKER_A: a, D.SPEAKER_B: b}
        return D.DiarizationResult(
            speaker_active=act, hop_sec=hop,
            posterior=[posterior] * n,
            overlap=overlap if overlap is not None else [False] * n,
            label_duration={l: sum(v) * hop for l, v in act.items()},
            embedding_margin=margin, model="stub", model_version="0",
        )

    smap = {"caller": D.SPEAKER_A, "agent": D.SPEAKER_B, "balanced": False}
    high = D.separation_confidence(_res(margin=0.6), smap)
    assert high["confidence_tier"] == "high" and high["indicative_only"] is False
    low = D.separation_confidence(_res(margin=0.3), smap)
    assert low["confidence_tier"] == "low" and low["indicative_only"] is True
    # one speaker -> refuse
    refuse = D.separation_confidence(
        _res(active={D.SPEAKER_A: a}), {"caller": D.SPEAKER_A, "agent": D.SPEAKER_B})
    assert refuse["confidence_tier"] == "refuse"


# --- 4. echo is N/A on the diarized-mono path -------------------------------

def test_echo_is_marked_not_applicable_on_diarized_path(tmp_path, stub_diarizer):
    c, a = _channels(caller_segments=[(3.0, 3.7)], agent_segments=[(0.2, 5.8)])
    mp = _write_mono_sum(tmp_path / "mono.wav", c, a)
    stub_diarizer("pyannote", _truth_timelines(c, a), embedding_margin=0.6)
    env = core.run_single(mono=mp, diarize=True, expect="hold")
    echo = env["events"][0]["signals"]["echo"]
    assert echo["applicable"] is False
    assert echo["echo_suspected"] is False
    assert echo["coherence"] is None


def test_echo_gate_cannot_fire_on_diarized_path(tmp_path, stub_diarizer):
    """Even with an overlap-heavy reconstruction (correlated tracks would read as
    echo on two mics), the echo gate never holds a yield out here: echo is N/A."""
    c, a = _channels(caller_segments=[(3.0, 4.2)], agent_segments=[(0.2, 3.5)])
    mp = _write_mono_sum(tmp_path / "mono.wav", c, a)
    stub_diarizer("pyannote", _truth_timelines(c, a), embedding_margin=0.6)
    env = core.run_single(mono=mp, diarize=True, expect="yield")
    ev = env["events"][0]
    # The verdict was not held out with an echo reason.
    reason_text = " ".join(ev["verdict"].get("reasons", [])).lower()
    assert "echo" not in reason_text


# --- 5. caller/agent assignment + override ----------------------------------

def test_floor_dominance_proposes_dominant_as_agent(stub_diarizer):
    hop = 0.01
    n = 600
    # A (short) vs B (long) -> B dominant -> B proposed as agent.
    a = [200 <= i < 260 for i in range(n)]       # 0.6s
    b = [10 <= i < 580 for i in range(n)]         # 5.7s
    res = D.DiarizationResult(
        speaker_active={D.SPEAKER_A: a, D.SPEAKER_B: b}, hop_sec=hop,
        label_duration={D.SPEAKER_A: sum(a) * hop, D.SPEAKER_B: sum(b) * hop},
    )
    smap = D.assign_speakers(res)
    assert smap["basis"] == "floor-dominance"
    assert smap["agent"] == D.SPEAKER_B
    assert smap["caller"] == D.SPEAKER_A
    assert smap["balanced"] is False


def test_user_override_skips_the_heuristic(stub_diarizer):
    hop = 0.01
    n = 600
    a = [i < 300 for i in range(n)]
    b = [i >= 300 for i in range(n)]
    res = D.DiarizationResult(
        speaker_active={D.SPEAKER_A: a, D.SPEAKER_B: b}, hop_sec=hop,
        label_duration={D.SPEAKER_A: 3.0, D.SPEAKER_B: 3.0},
    )
    smap = D.assign_speakers(res, caller_speaker=D.SPEAKER_B, agent_speaker=D.SPEAKER_A)
    assert smap["basis"] == "user"
    assert smap["caller"] == D.SPEAKER_B and smap["agent"] == D.SPEAKER_A
    assert smap["balanced"] is False


def test_balanced_floor_time_downgrades_to_indicative(tmp_path, stub_diarizer):
    """Near-equal floor times -> ambiguous mapping (basis first-speaker, balanced)
    -> the verdict is indicative, never a confident coin-flip."""
    # Both speakers roughly equal talk time.
    c, a = _channels(caller_segments=[(1.0, 3.0)], agent_segments=[(3.2, 5.2)])
    mp = _write_mono_sum(tmp_path / "mono.wav", c, a)
    stub_diarizer("pyannote", _truth_timelines(c, a), embedding_margin=0.6)
    env = core.run_single(mono=mp, diarize=True, expect="hold")
    ev = env["events"][0]
    smap = ev["diarization"]["speaker_map"]
    assert smap["balanced"] is True
    assert ev["diarization"]["confidence_tier"] == "low"
    assert ev["indicative_only"] is True


def test_override_flag_flows_through_run_single(tmp_path, stub_diarizer):
    c, a = _channels(caller_segments=[(3.0, 3.7)], agent_segments=[(0.2, 5.8)])
    mp = _write_mono_sum(tmp_path / "mono.wav", c, a)
    stub_diarizer("pyannote", _truth_timelines(c, a), embedding_margin=0.6)
    env = core.run_single(mono=mp, diarize=True, expect="hold",
                          caller_speaker=D.SPEAKER_A, agent_speaker=D.SPEAKER_B)
    smap = env["events"][0]["diarization"]["speaker_map"]
    assert smap["basis"] == "user"
    assert smap["caller"] == D.SPEAKER_A and smap["agent"] == D.SPEAKER_B


# --- 6. masked reconstruction shape -----------------------------------------

def test_reconstruct_tracks_masks_by_activity(stub_diarizer):
    """Keep the mono where a speaker is active, hard-zero elsewhere; overlap
    frames carry the mono on BOTH tracks; the two tracks are equal length."""
    cfg = ScoreConfig()
    sr = SR
    n = sr  # 1s
    mono = [0.5] * n  # constant so masking is visible
    hop = max(1, int(round(sr * cfg.hop_ms / 1000.0)))
    nf = (n + hop - 1) // hop
    ca = [False] * nf
    ag = [False] * nf
    for k in range(0, 20):
        ca[k] = True          # caller active frames 0..19
    for k in range(10, 30):
        ag[k] = True          # agent active 10..29 -> overlap 10..19
    res = D.DiarizationResult(
        speaker_active={D.SPEAKER_A: ca, D.SPEAKER_B: ag}, hop_sec=hop / sr,
        label_duration={D.SPEAKER_A: 0.2, D.SPEAKER_B: 0.2},
    )
    ct, at = D.reconstruct_tracks(mono, res, D.SPEAKER_A, D.SPEAKER_B,
                                  sample_rate=sr, cfg=cfg)
    assert len(ct) == len(at) == n
    assert ct[0] == 0.5 and at[0] == 0.0            # caller-only region
    assert ct[10 * hop] == 0.5 and at[10 * hop] == 0.5  # overlap region: both
    assert ct[25 * hop] == 0.0 and at[25 * hop] == 0.5  # agent-only region
    assert ct[40 * hop] == 0.0 and at[40 * hop] == 0.0  # silence


# --- 7. DEFAULT path byte-identical -----------------------------------------

def test_default_stereo_run_has_no_diarization_and_mono_stays_rejected(tmp_path):
    """No --mono/--diarize: the diarize code is never reached. A normal stereo run
    carries no diarization block, and a mono file passed as --stereo is rejected
    exactly as before."""
    c, a = _channels(caller_segments=[(3.0, 3.7)], agent_segments=[(0.2, 5.8)])
    # A real two-channel file scores normally, with no diarization surface.
    sp = tmp_path / "stereo.wav"
    with wave.open(str(sp), "wb") as w:
        w.setnchannels(2)
        w.setsampwidth(2)
        w.setframerate(SR)
        frames = bytearray()
        for i in range(len(c)):
            frames += struct.pack("<hh",
                                  int(round(max(-1, min(1, c[i])) * 32767)),
                                  int(round(max(-1, min(1, a[i])) * 32767)))
        w.writeframes(bytes(frames))
    env = core.run_single(stereo=str(sp), expect="hold")
    assert "diarization" not in env
    assert "diarization" not in env["events"][0]
    assert "indicative_only" not in env["events"][0]

    # A mono file passed as --stereo is still rejected, byte-identical to before.
    mp = _write_mono_sum(tmp_path / "mono.wav", c, a)
    with pytest.raises(ValueError, match="one channel"):
        core.run_single(stereo=mp)


# --- 8. a vendored AMI clip when checked out (perfect-diarizer end-to-end) ---

def _ami_fixture():
    d = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                     "corpus", "real", "audio")
    if not os.path.isdir(d):
        return None
    for name in sorted(os.listdir(d)):
        if name.startswith("ami-") and name.endswith(".example.wav"):
            return os.path.join(d, name)
    return None


@pytest.mark.skipif(_ami_fixture() is None,
                    reason="AMI dual-channel corpus not checked out at corpus/real/audio")
def test_ami_summed_to_mono_scores_with_perfect_diarizer(tmp_path, stub_diarizer):
    """The spec-8 ground-truth mechanic: a vendored AMI 2-channel clip (each party
    already on one channel = the exact truth) summed to mono, diarized by a PERFECT
    stub (the two channels' own VAD), reconstructed, and scored. Proves the
    reconstruction handles real speech, not just tones."""
    from hotato import _engine

    sig = _engine.read_wav(_ami_fixture())
    if sig.num_channels < 2:
        pytest.skip("AMI fixture is not two-channel")
    c = sig.get(0)
    a = sig.get(1)
    n = min(len(c), len(a))
    c, a = list(c[:n]), list(a[:n])
    mp = _write_mono_sum(tmp_path / "ami-mono.wav", c, a, sr=sig.sample_rate)
    truth = _truth_timelines(c, a, sr=sig.sample_rate)
    stub_diarizer("pyannote", truth, embedding_margin=0.6)
    env = core.run_single(mono=mp, diarize=True, expect="hold")
    assert env["mode"] == "single"
    assert env["diarization"]["source"] == "diarized-mono"
    # A verdict is produced (its exact value is not the invariant here).
    assert env["events"][0]["verdict"]["did_yield"] in (True, False)
