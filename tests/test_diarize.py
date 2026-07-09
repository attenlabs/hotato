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


# --- 9. pyannote 3.x/4.x output shape + embedding-margin edge cases ---------

def test_unpack_pipeline_output_handles_4x_diarize_output_and_3x_shapes():
    """4.x's ``pipeline(...)`` call returns a ``DiarizeOutput`` object (not
    iterable, no ``.labels()``) exposing ``.speaker_diarization`` /
    ``.speaker_embeddings``; 3.x returns a bare ``(annotation, embeddings)``
    tuple, or a bare annotation with no embeddings. All three must unpack to
    the same ``(annotation, embeddings)`` shape, without importing pyannote."""

    class _FakeDiarizeOutput:  # a 4.x-style DiarizeOutput stand-in
        def __init__(self, diarization, embeddings):
            self.speaker_diarization = diarization
            self.speaker_embeddings = embeddings

    annotation_4x, embeddings_4x = object(), object()
    a, e = D._unpack_pipeline_output(_FakeDiarizeOutput(annotation_4x, embeddings_4x))
    assert a is annotation_4x and e is embeddings_4x

    annotation_3x, embeddings_3x = object(), object()
    a, e = D._unpack_pipeline_output((annotation_3x, embeddings_3x))
    assert a is annotation_3x and e is embeddings_3x

    annotation_plain = object()
    a, e = D._unpack_pipeline_output(annotation_plain)
    assert a is annotation_plain and e is None


def test_embedding_margin_zero_vector_is_no_signal_not_fabricated_neutral():
    """A degenerate (zero-norm) centroid -- pyannote returns one when it could
    not reliably estimate a speaker's embedding -- must read as "no margin
    available" (None), the same as a missing embeddings array, not a
    fabricated cos=0 (margin 0.5) that the gate would read as good
    separation."""
    zero = [0.0] * 8
    real = [1.0] + [0.0] * 7
    assert D._embedding_margin([zero, real]) is None
    assert D._embedding_margin([real, zero]) is None
    assert D._embedding_margin(None) is None


def test_embedding_margin_direction_and_scale():
    """Sanity on the formula itself: orthogonal centroids read as maximally
    ambiguous (0.5), identical centroids read as not separated at all (0.0),
    opposite centroids read as maximally separated (1.0)."""
    m = D._embedding_margin([[1.0, 0.0], [0.0, 1.0]])
    assert m == pytest.approx(0.5)
    m = D._embedding_margin([[1.0, 2.0, 3.0], [1.0, 2.0, 3.0]])
    assert m == pytest.approx(0.0)
    m = D._embedding_margin([[1.0, 0.0], [-1.0, 0.0]])
    assert m == pytest.approx(1.0)


# --- 10. yield-boundary confidence (signal 7; DIARIZE-BENCHMARK-2026-07-09) --
#
# The benchmark showed the six diarization-quality signals are anti-correlated
# with verdict correctness: `high` reproduced the dual-channel did_yield verdict
# LESS often than `low`, because every disagreement was a MISSED sub-second yield
# the quality signals cannot see. Signal 7 measures how much the yield verdict
# depends on sub-second boundary placement and BARS the fragile zone from `high`.

_YB_N = 600
_YB_HOP = 0.01


def _band(lo, hi, n=_YB_N):
    return [lo <= i < hi for i in range(n)]


def _union(*bands):
    return [any(b[i] for b in bands) for i in range(len(bands[0]))]


def _yb_result(caller, agent, *, margin=0.6, posterior=1.0):
    """A DiarizationResult from two crafted per-frame timelines (caller=A,
    agent=B), with the OTHER six signals held green so any demotion is signal 7."""
    active = {D.SPEAKER_A: caller, D.SPEAKER_B: agent}
    labels = sorted(active)
    over = [sum(1 for l in labels if active[l][i]) >= 2 for i in range(_YB_N)]
    return D.DiarizationResult(
        speaker_active=active, hop_sec=_YB_HOP,
        posterior=[posterior] * _YB_N, overlap=over,
        label_duration={l: round(sum(v) * _YB_HOP, 6) for l, v in active.items()},
        embedding_margin=margin, model="stub", model_version="0",
    )


_YB_SMAP = {"caller": D.SPEAKER_A, "agent": D.SPEAKER_B, "balanced": False}


def test_perturb_timeline_dilate_and_erode():
    """The perturbation primitive, tested directly: dilation grows each active run
    by k frames per side (gaps shrink); erosion trims it (gaps grow) and drops runs
    shorter than 2k+1, never manufacturing an interior hole in a solid run."""
    tl = [False, False, True, True, True, False, False]
    assert D._perturb_timeline(tl, 1) == [False, True, True, True, True, True, False]
    assert D._perturb_timeline(tl, -1) == [False, False, False, True, False, False, False]
    assert D._perturb_timeline(tl, 0) == tl
    # a solid block only trims at its ends under erosion -- no interior gap created
    solid = [True] * 10
    assert D._perturb_timeline(solid, -2) == [False, False] + [True] * 6 + [False, False]


def test_yield_boundary_flips_on_short_gap_not_on_wide_gap():
    """The signal's core: a yield triggered by a barely-above-threshold agent-quiet
    gap flips did_yield under a +/-250ms boundary nudge (fragile); the same yield
    with a wide gap does not (robust). Perturbation logic exercised end to end."""
    cfg = ScoreConfig()
    # short gap: agent quiet for 22 frames (0.22s), just over the 0.20s hangover
    short = D._yield_boundary_confidence(
        _yb_result(_band(280, 360), _union(_band(0, 300), _band(322, 600))),
        _YB_SMAP, cfg)
    assert short["did_yield"] is True
    assert short["boundary_perturb_flip"] is True
    assert short["robust"] is False
    # wide gap: agent quiet for 180 frames (1.8s) -> survives the nudge
    wide = D._yield_boundary_confidence(
        _yb_result(_band(280, 470), _union(_band(0, 300), _band(480, 600))),
        _YB_SMAP, cfg)
    assert wide["did_yield"] is True
    assert wide["boundary_perturb_flip"] is False
    assert wide["robust"] is True


def test_fragile_short_yield_forced_out_of_high():
    """A short-yield clip whose six quality signals are ALL green (2 speakers,
    both active, high posterior, wide embedding margin, low overlap, no churn) is
    still demoted out of `high` purely by the boundary-fragility of its verdict."""
    sep = D.separation_confidence(
        _yb_result(_band(280, 360), _union(_band(0, 300), _band(322, 600))),
        _YB_SMAP)
    sg = sep["signals"]
    # every OTHER signal is green -> without signal 7 this would be `high`
    assert sg["speaker_count_ok"] is True and sg["both_speakers_active"] is True
    assert sg["embedding_margin"] >= D.EMBED_MARGIN_HIGH
    assert sg["overlap_ratio"] <= D.OVERLAP_RATIO_HIGH_MAX
    assert sg["mean_posterior"] >= D.POSTERIOR_HIGH
    # signal 7 catches the sub-second yield and forces low
    assert sg["yield_boundary"]["boundary_perturb_flip"] is True
    assert sg["yield_boundary"]["robust"] is False
    assert sep["confidence_tier"] == "low"
    assert sep["indicative_only"] is True
    assert "boundary" in sep["reason"]


def test_backchannel_yield_forced_out_of_high():
    """A yield resting on a backchannel-length caller interjection (0.3s) cannot be
    high even when the agent-quiet gap itself is wide -- a short yield reconstructed
    from one channel is only indicative."""
    sep = D.separation_confidence(
        _yb_result(_band(300, 330), _union(_band(0, 300), _band(500, 600))),
        _YB_SMAP)
    yb = sep["signals"]["yield_boundary"]
    assert yb["did_yield"] is True
    assert yb["backchannel_yield"] is True
    assert yb["caller_floor_sec"] < D.YIELD_MIN_CALLER_FLOOR_SEC
    assert sep["confidence_tier"] == "low"
    assert "backchannel" in sep["reason"]


def test_robust_wide_margin_yield_stays_high():
    """The point of shrinking `high`, not eliminating it: a genuinely robust yield
    -- a wide agent-quiet gap, a caller floor well above backchannel length, no
    boundary flip, all six quality signals green -- earns and keeps `high`."""
    sep = D.separation_confidence(
        _yb_result(_band(280, 470), _union(_band(0, 300), _band(480, 600))),
        _YB_SMAP)
    yb = sep["signals"]["yield_boundary"]
    assert yb["did_yield"] is True
    assert yb["robust"] is True
    assert yb["boundary_perturb_flip"] is False
    assert yb["backchannel_yield"] is False
    assert sep["confidence_tier"] == "high"
    assert sep["indicative_only"] is False


def test_robust_hold_is_not_penalized_by_signal_7():
    """A clean HOLD (the agent never goes quiet during the caller's floor) is
    boundary-robust and stays high: signal 7 only bites the fragile yield zone,
    it does not punish a confident hold."""
    sep = D.separation_confidence(
        _yb_result(_band(300, 370), _band(20, 580)), _YB_SMAP)  # agent solid throughout
    yb = sep["signals"]["yield_boundary"]
    assert yb["did_yield"] is False
    assert yb["boundary_perturb_flip"] is False
    assert yb["robust"] is True
    assert sep["confidence_tier"] == "high"


def test_timeline_yield_replica_matches_engine_did_yield():
    """The signal replays the engine's did_yield over the diarization timelines
    rather than re-scoring; that replica must agree with the real
    ``score_channels`` did_yield on the same activity, for both a hold and a
    clean yield -- otherwise the fragility measure is about the wrong decision."""
    cfg = ScoreConfig()
    # A clean yield and a clean hold as synthetic channels, then compare the
    # engine's verdict on the channels to the replica on their VAD timelines.
    for segs, label in (
        (([(3.0, 4.2)], [(0.2, 3.5)]), "yield"),   # agent yields at 3.5
        (([(3.0, 3.7)], [(0.2, 5.8)]), "hold"),    # agent holds throughout
    ):
        c, a = _channels(caller_segments=segs[0], agent_segments=segs[1])
        engine = score_channels(c, a, SR, cfg=cfg).did_yield
        tl = _truth_timelines(c, a, cfg=cfg)
        replica = D._timeline_yield(tl[D.SPEAKER_A], tl[D.SPEAKER_B],
                                    cfg.hop_ms / 1000.0, cfg)["did_yield"]
        assert replica == engine, f"{label}: replica {replica} != engine {engine}"
