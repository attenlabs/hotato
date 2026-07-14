"""Optional, NON-REFERENCE mono-scorability front-end (speaker diarization).

Hotato's gold reference is a two-channel recording: the caller on one channel,
the agent on the other, each channel one party, no separation needed. That path,
and every published/golden/bundled number, is untouched by this module. This
module widens COVERAGE, not the reference: it turns a single mixed (mono)
recording into two per-speaker activity timelines via an off-the-shelf diarizer,
reconstructs two masked tracks, and feeds the EXISTING ``_engine.score_channels``
so a mono call becomes scorable -- opt-in, quality-gated, and honestly labeled.

Design, mirroring the neural-VAD seam (``neural.py`` + ``_engine.vad``):

  * a pluggable DIARIZER-BACKEND SEAM (a name -> factory registry, resolved
    lazily on first use) so no model is imported unless a diarized run is
    actually requested. The energy reference path never reaches this code.
  * one ``DiarizationResult`` contract every backend maps onto (two per-frame
    activity timelines on the 10 ms hop, a per-frame posterior track, an overlap
    track, per-label durations, and -- when the backend exposes them -- speaker
    embeddings for a cluster-separation margin), so the confidence gate and the
    reconstruction are backend-agnostic.
  * THREE real backends behind the seam -- ``pyannote`` (local, CPU-viable,
    offline, richest confidence signals), ``sortformer`` (NVIDIA NeMo, local,
    GPU-leaning, best self-hostable on telephone), ``pyannoteai`` (hosted, best
    absolute, egress opt-in) -- each import-guarded so an absent dependency
    raises a clean ``BackendUnavailable``, NEVER a silent fallback that would
    score raw mono and pass it off as separated.
  * a hermetic STUB backend (``build_stub_backend``) for tests: deterministic,
    derives timelines from a provided truth or a simple energy split, needs no
    model / token / network, so the whole pipeline is exercisable offline.

Honesty invariants this module enforces (asserted in tests/test_diarize.py):
  1. Default (no ``--mono``/``--diarize``) is byte-identical: this code is never
     reached, and a mono file stays rejected exactly as today.
  2. No silent fallback: a missing extra / token / model raises
     ``BackendUnavailable`` and the run exits non-zero; raw mono is never scored
     and presented as a diarized result.
  3. The confidence gate is real (``separation_confidence``): high tier scores
     and stamps ``indicative_only=false``; low tier scores but stamps
     ``indicative_only=true`` and suppresses SLA/CI gating; the refuse tier
     returns ``scorable=false`` (exit 2). A confident verdict is never emitted on
     low-confidence separation.
  4. On the diarized-mono path the two reconstructed tracks are slices of ONE
     physical microphone, so ``signals.echo`` / crosstalk coherence is
     definitionally invalid and is marked N/A (``echo_na_block``); the echo gate
     never fires here.

Reconstruction mechanic (Option A, zero ``_engine`` edit; spec 5.3/5.4): mask the
mono by each speaker's active frames -- keep the original samples where that
speaker is active, hard-zero elsewhere -- into two equal-length tracks, and re-run
the EXISTING energy path via ``score_channels``. During overlap both tracks carry
the mixed mono, so ``talk_over`` reconstructs. A de-risk spike (with a PERFECT
diarizer) showed this re-VAD-on-mono systematically INFLATES sub-second talk_over
by ~0.1-0.36 s and can bridge a short backchannel gap (flipping a did_yield), an
error INTRINSIC to single-channel masking that no diarizer quality removes. That
is precisely why the confidence gate treats elevated overlap and short-yield /
sub-second cases as the fragile zone (-> ``low``, indicative only, no SLA gate),
and why direct timeline injection (the spec's deferred Option B, no second VAD)
is the recommended follow-up. Under either option the honest error budget is
governed by the gate here; nothing on this path is presented as equal to a true
dual-channel measurement.

Diarization model licenses (logged per the direction's FTO note; pin in
docs/DIARIZE.md + THIRD-PARTY-LICENSES): pyannote-audio MIT (code);
speaker-diarization-community-1 weights CC-BY-4.0 (attribution required);
segmentation-3.0 MIT; wespeaker embedding CC-BY-4.0; torch BSD-3-Clause;
torchaudio BSD-2-Clause; nemo-toolkit Apache-2.0; Sortformer STREAMING v2
CC-BY-4.0 (the offline v1 is CC-BY-NC -> NON-COMMERCIAL, never ship it);
pyannoteAI SDK/hosted terms (verify before enabling egress).
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Callable, Dict, List, Optional, Sequence

from ._engine.audio import frame_rms
from ._engine.score import ScoreConfig
from ._engine.vad import BackendUnavailable, energy_vad, first_active_sec

__all__ = [
    "DiarizationResult",
    "DiarizedMono",
    "register_diarizer_backend",
    "clear_diarizer_backends",
    "resolve_diarizer",
    "diarize_mono",
    "reconstruct_tracks",
    "separation_confidence",
    "assign_speakers",
    "prepare_diarized_mono",
    "echo_na_block",
    "build_stub_backend",
    "build_pyannote_backend",
    "build_sortformer_backend",
    "build_pyannoteai_backend",
    "DIARIZER_BACKENDS",
    "SEPARATION_MODEL_LICENSES",
]

# Anonymous speaker labels a backend assigns before caller/agent mapping. The
# pyannote/NeMo convention is SPEAKER_00 / SPEAKER_01; the stub follows it so the
# assignment heuristic (below) is exercised the same way in tests and in prod.
SPEAKER_A = "SPEAKER_00"
SPEAKER_B = "SPEAKER_01"

# License provenance carried into the score envelope / docs (FTO note). Purely
# informational; the seam works with any subset of backends installed.
SEPARATION_MODEL_LICENSES = {
    "pyannote-audio": "MIT (code)",
    "speaker-diarization-community-1": "CC-BY-4.0 (weights; attribution required)",
    "segmentation-3.0": "MIT (weights)",
    "wespeaker-voxceleb-resnet34-LM": "CC-BY-4.0 (weights)",
    "nemo-toolkit": "Apache-2.0 (code)",
    "diar_streaming_sortformer_4spk-v2": "CC-BY-4.0 (weights; v1 offline is CC-BY-NC, never ship)",
    "pyannoteAI-precision-2": "hosted / proprietary terms (verify before egress)",
}

# The three shipped backend names, in the seam's preference order. The DEFAULT is
# chosen by the downstream benchmark, not pre-assumed (spec 8); pyannote is the
# accessible local default candidate but is NOT best on telephone -- a user who
# needs best-in-class picks sortformer (local/GPU) or pyannoteai (hosted).
DIARIZER_BACKENDS = ("pyannote", "sortformer", "pyannoteai")


# --------------------------------------------------------------------------- #
# The one contract every backend maps onto.
# --------------------------------------------------------------------------- #

@dataclass
class DiarizationResult:
    """The backend-agnostic diarization contract.

    ``speaker_active`` maps each anonymous speaker label to a per-frame activity
    timeline (``list[bool]``) on the SAME 10 ms hop grid the energy VAD uses, so
    the reconstruction and the confidence gate never touch a backend-specific
    shape. ``hop_sec`` is that grid's spacing.

    Confidence-gate inputs (spec 7), all optional so an EEND backend that does not
    expose them still yields a valid result:
      * ``posterior``     per-frame segmentation confidence in [0, 1] (an
                          UNCALIBRATED relative signal, never a certified
                          probability), aligned to the hop grid.
      * ``overlap``       per-frame both-speakers-active track (>= 2 active).
      * ``label_duration``  total active seconds per speaker label.
      * ``embedding_margin``  cosine separation between the two speaker centroids
                          in [0, 1] (a pyannote-only bonus; ``None`` for EEND
                          backends like Sortformer or when unavailable).
    ``model`` / ``model_version`` are provenance stamped into the envelope.
    """

    speaker_active: Dict[str, List[bool]]
    hop_sec: float
    posterior: List[float] = field(default_factory=list)
    overlap: List[bool] = field(default_factory=list)
    label_duration: Dict[str, float] = field(default_factory=dict)
    embedding_margin: Optional[float] = None
    model: str = "unknown"
    model_version: str = "unknown"

    @property
    def labels(self) -> List[str]:
        """Speaker labels, sorted for a deterministic order."""
        return sorted(self.speaker_active)

    @property
    def num_speakers(self) -> int:
        return len(self.speaker_active)


# A backend is a callable:
#   backend(mono_samples, sample_rate, hop_sec, num_speakers) -> DiarizationResult
# It is produced by a zero-arg FACTORY (so importing/loading the model happens
# lazily, inside the factory, on first real use -- never at registration).
BackendFn = Callable[[Sequence[float], int, float, int], DiarizationResult]
FactoryFn = Callable[[], BackendFn]

_DIARIZER_FACTORIES: Dict[str, FactoryFn] = {}
_DIARIZER_CACHE: Dict[str, BackendFn] = {}


def register_diarizer_backend(name: str, factory: FactoryFn) -> None:
    """Register a named diarizer backend factory (or a test stub).

    ``factory`` is a zero-arg callable returning the backend function. It is
    invoked lazily, once, on the first ``diarize_mono`` for ``name``; loading the
    model MUST happen inside it (never at registration) so registering costs
    nothing and the energy path stays dependency-free. The factory should raise
    ``BackendUnavailable`` when its optional extra / token / model is absent."""
    _DIARIZER_FACTORIES[name] = factory
    _DIARIZER_CACHE.pop(name, None)


def clear_diarizer_backends() -> None:
    """Unregister every diarizer backend (a clean state for tests)."""
    _DIARIZER_FACTORIES.clear()
    _DIARIZER_CACHE.clear()


def resolve_diarizer(name: str) -> BackendFn:
    """Resolve a named backend to its callable, lazily and cached.

    Raises ``BackendUnavailable`` when the name is unknown (listing the ones that
    ARE registered) or when the factory itself cannot load its dependency. NEVER
    substitutes a different backend: a mis-selected or unavailable diarizer is a
    hard, explicit error so a mono verdict never quietly changes provenance."""
    if name not in _DIARIZER_FACTORIES:
        known = ", ".join(sorted(_DIARIZER_FACTORIES)) or "(none registered)"
        raise BackendUnavailable(
            f"diarizer backend {name!r} is not registered; available: {known}. "
            "Install the matching optional extra (for the default: "
            "pip install 'hotato[diarize]') or register one with "
            "register_diarizer_backend(). The dual-channel path is the reference "
            "and is never substituted for a mono guess."
        )
    if name not in _DIARIZER_CACHE:
        _DIARIZER_CACHE[name] = _DIARIZER_FACTORIES[name]()  # may raise BackendUnavailable
    return _DIARIZER_CACHE[name]


def _hop_samples(sample_rate: int, cfg: ScoreConfig) -> int:
    """The integer hop, matching ``frame_rms`` exactly so timelines, masks, and
    the re-VAD grid all line up frame-for-frame."""
    return max(1, int(round(sample_rate * cfg.hop_ms / 1000.0)))


def diarize_mono(
    mono_samples: Sequence[float],
    sample_rate: int,
    *,
    backend: str = "pyannote",
    num_speakers: int = 2,
    cfg: Optional[ScoreConfig] = None,
) -> DiarizationResult:
    """Diarize one mono recording with the named backend into a
    ``DiarizationResult`` on the reference hop grid. Thin: it resolves the backend
    (lazy, no-fallback) and calls it; all model work is inside the backend."""
    if cfg is None:
        cfg = ScoreConfig()
    hop = _hop_samples(sample_rate, cfg) / sample_rate
    fn = resolve_diarizer(backend)
    return fn(mono_samples, sample_rate, hop, num_speakers)


# --------------------------------------------------------------------------- #
# Reconstruction (Option A): two masked tracks -> the EXISTING score path.
# --------------------------------------------------------------------------- #

def reconstruct_tracks(
    mono_samples: Sequence[float],
    result: DiarizationResult,
    caller_label: str,
    agent_label: str,
    *,
    sample_rate: int,
    cfg: Optional[ScoreConfig] = None,
):
    """Mask the mono into two equal-length caller/agent tracks (spec 5.4).

    For each speaker, keep the ORIGINAL mono samples inside that speaker's active
    frames and hard-zero everywhere else. During overlap both tracks carry the
    mixed mono in that region, so both re-VAD active and ``talk_over``
    reconstructs. Hard-zeroing is safe for the energy VAD: zeroed frames sit at
    the -120 dBFS floor while speech sits well above the -60 dBFS gate, so
    re-detection reproduces the diarized activity to within a hop.

    Returns ``(caller_track, agent_track)`` as two equal-length ``list[float]``
    (or ndarray, matching the input) -- the exact two-mono contract
    ``score_channels`` consumes."""
    if cfg is None:
        cfg = ScoreConfig()
    hop = _hop_samples(sample_rate, cfg)
    caller_active = result.speaker_active.get(caller_label, [])
    agent_active = result.speaker_active.get(agent_label, [])

    # numpy fast path when the samples already arrived as an ndarray (the engine's
    # optional acceleration); identical values either way.
    try:
        from ._engine.audio import _np
    except Exception:  # pragma: no cover
        _np = None
    if _np is not None and isinstance(mono_samples, _np.ndarray):
        n = mono_samples.shape[0]
        n_frames = (n + hop - 1) // hop if n else 0
        cmask = _np.zeros(n_frames, dtype=bool)
        amask = _np.zeros(n_frames, dtype=bool)
        for k in range(min(n_frames, len(caller_active))):
            cmask[k] = bool(caller_active[k])
        for k in range(min(n_frames, len(agent_active))):
            amask[k] = bool(agent_active[k])
        # expand each frame mask to the sample grid, truncated to n
        csamp = _np.repeat(cmask, hop)[:n]
        asamp = _np.repeat(amask, hop)[:n]
        return mono_samples * csamp, mono_samples * asamp

    n = len(mono_samples)
    caller_track = [0.0] * n
    agent_track = [0.0] * n
    for i in range(n):
        f = i // hop
        if f < len(caller_active) and caller_active[f]:
            caller_track[i] = mono_samples[i]
        if f < len(agent_active) and agent_active[f]:
            agent_track[i] = mono_samples[i]
    return caller_track, agent_track


# --------------------------------------------------------------------------- #
# Caller/agent assignment (never a silent guess; spec 6).
# --------------------------------------------------------------------------- #

# Reuse trust's dominance band so the proposal here and the possible_swap flag in
# `hotato trust` speak the same heuristic. An agent usually holds the floor
# longer, so the higher-talk-time speaker is proposed as the agent.
SWAP_DOMINANCE_RATIO = 1.5
SWAP_ABS_MARGIN_SEC = 1.0


def _first_active_frame(active: List[bool]) -> int:
    for i, a in enumerate(active):
        if a:
            return i
    return len(active)


def assign_speakers(
    result: DiarizationResult,
    caller_speaker: Optional[str] = None,
    agent_speaker: Optional[str] = None,
) -> dict:
    """Propose (or accept an override for) the caller/agent -> speaker mapping.

    Never a silent guess:
      * If both ``--caller-speaker`` / ``--agent-speaker`` are given, no heuristic
        runs; ``basis`` is ``"user"`` and ``balanced`` is False.
      * Otherwise reuse trust's floor-dominance band: the higher-talk-time speaker
        is proposed as the agent, the other as the caller. When the two floor
        times are near-equal (within the dominance band) the mapping is ambiguous
        -- broken by who-speaks-first (an inbound agent typically greets first) --
        and flagged ``balanced: True`` so the confidence gate downgrades it to
        indicative rather than emitting a confident verdict on a coin-flip.

    Returns ``{caller, agent, basis, balanced, confidence}``; ``basis`` is one of
    ``user`` / ``floor-dominance`` / ``first-speaker``."""
    labels = result.labels
    # Overrides win outright and skip every heuristic.
    if caller_speaker is not None and agent_speaker is not None:
        return {
            "caller": caller_speaker,
            "agent": agent_speaker,
            "basis": "user",
            "balanced": False,
            "confidence": 1.0,
        }
    if len(labels) < 2:
        # Degenerate; the gate refuses on speaker_count anyway. Map defensively.
        only = labels[0] if labels else SPEAKER_A
        return {
            "caller": only,
            "agent": only,
            "basis": "degenerate",
            "balanced": True,
            "confidence": 0.0,
        }

    # Take the two most active labels (the gate refuses > 2 separately).
    durs = result.label_duration
    ranked = sorted(labels, key=lambda l: durs.get(l, 0.0), reverse=True)
    a_label, b_label = ranked[0], ranked[1]
    a_dur = durs.get(a_label, 0.0)
    b_dur = durs.get(b_label, 0.0)

    dominant = (
        a_dur > SWAP_DOMINANCE_RATIO * b_dur
        and (a_dur - b_dur) >= SWAP_ABS_MARGIN_SEC
    )
    if dominant:
        # Clear floor dominance: the dominant speaker is the agent.
        return {
            "caller": b_label,
            "agent": a_label,
            "basis": "floor-dominance",
            "balanced": False,
            "confidence": round(min(1.0, (a_dur - b_dur) / max(a_dur, 1e-9)), 3),
        }

    # Balanced floor time -> break by who speaks first (agent greets first), and
    # flag it balanced so the gate keeps us indicative, not confident.
    fa = _first_active_frame(result.speaker_active.get(a_label, []))
    fb = _first_active_frame(result.speaker_active.get(b_label, []))
    if fa <= fb:
        agent, caller = a_label, b_label
    else:
        agent, caller = b_label, a_label
    return {
        "caller": caller,
        "agent": agent,
        "basis": "first-speaker",
        "balanced": True,
        "confidence": 0.3,
    }


# --------------------------------------------------------------------------- #
# The runtime confidence gate (the honesty core; spec 7).
# --------------------------------------------------------------------------- #
#
# Aggregate DER is a corpus statistic; this gate is PER-FILE, at runtime, with no
# ground truth. Thresholds are provisional and UNCALIBRATED -- they are pinned by
# the spec-8 downstream verdict-agreement benchmark, not asserted as accuracy.
# They are exposed as constants so they are inspectable and tunable.

MIN_SPEAKER_ACTIVITY_SEC = 0.30      # each speaker needs this much detected speech
POSTERIOR_REFUSE = 0.40              # below this the model is guessing -> refuse
POSTERIOR_HIGH = 0.75               # at/above this the segmentation is confident
OVERLAP_RATIO_HIGH_MAX = 0.30       # more overlap than this -> only indicative
OVERLAP_RATIO_REFUSE = 0.60         # extreme overlap -> not separable -> refuse
EMBED_MARGIN_REFUSE = 0.20          # centroids this close -> voices too similar
EMBED_MARGIN_HIGH = 0.45            # at/above this the two voices are well apart
# NOTE (DIARIZE-BENCHMARK-2026-07-09): measured against a real pyannote
# community-1 backend over the AMI corpus, embedding margin clustered tightly
# (~0.43-0.52) regardless of downstream verdict correctness -- uninformative
# on that set, not a demonstrated computation defect. Thresholds are left as
# measured here; recalibration is deferred to the gate-recalibration stage.
CHURN_HIGH_MAX = 2.0                # speaker flips/sec above this -> only indicative
CHURN_REFUSE = 6.0                  # jitter this high -> unreliable -> refuse
SHORT_TURN_SEC = 0.30               # a turn shorter than this counts toward churn

# --- signal 7: yield-boundary confidence (DIARIZE-BENCHMARK-2026-07-09) -------
# The six signals above measure DIARIZATION quality (two clean, well-separated,
# stable speakers?). The real-backend benchmark showed that is necessary but NOT
# sufficient: on a pyannote community-1 run over AMI summed to mono, the gate was
# ANTI-correlated with verdict correctness -- the `high` tier reproduced the
# dual-channel did_yield verdict LESS often (38%) than `low` (75%). Every
# disagreement was a MISSED yield (never a phantom), concentrated in short-yield /
# backchannel / sub-second talk-over cases, and present even at DER 0.000: the
# verdict turns on a sub-250 ms agent-quiet gap that DER's 0.25 s collar and all
# six quality signals forgive. Diarization can be pristine while the verdict is
# wrong. This 7th signal measures the quantity that actually decides the verdict:
# how much did_yield depends on sub-second boundary placement. It replays the
# engine's yield logic straight over the diarization timelines (no model calls, no
# reconstruction), perturbs the speaker boundaries by +/- a quarter second, and
# asks whether the verdict survives. A yield whose triggering agent-quiet gap only
# barely clears the hangover, or that rests on a backchannel-length caller run, or
# that flips under a 250 ms boundary nudge, is in the exact fragile zone the
# benchmark identified: it can NEVER be `high` (it drops to `low`, indicative).
# `high` now REQUIRES a boundary-robust verdict, so honest `high` coverage shrinks
# a lot on real material -- that is the point.
YIELD_BOUNDARY_PERTURB_SEC = 0.25   # the +/- boundary nudge the verdict must survive
YIELD_MIN_CALLER_FLOOR_SEC = 0.50   # a yield resting on a briefer caller run is
                                    # backchannel-grade -> fragile, never high
YIELD_NEAR_WINDOW_SEC = 0.50        # +/- window around the yield for local overlap (reported)


def _clamp01(x: float) -> float:
    if x < 0.0:
        return 0.0
    if x > 1.0:
        return 1.0
    return x


def _overlap_ratio(result: DiarizationResult) -> float:
    """Fraction of SPEECH frames (any speaker active) where >= 2 speakers overlap."""
    labels = result.labels
    n = max((len(result.speaker_active.get(l, [])) for l in labels), default=0)
    if n == 0:
        return 0.0
    speech = 0
    over = 0
    for i in range(n):
        active = sum(
            1 for l in labels
            if i < len(result.speaker_active[l]) and result.speaker_active[l][i]
        )
        if active >= 1:
            speech += 1
        if active >= 2:
            over += 1
    return over / speech if speech else 0.0


def _segment_churn_per_sec(result: DiarizationResult) -> float:
    """Very-short turns per second across both timelines: a jittery diarization
    that flips speakers on sub-``SHORT_TURN_SEC`` runs is an unstable timeline."""
    hop = result.hop_sec or 0.01
    short_frames = max(1, int(round(SHORT_TURN_SEC / hop)))
    short_turns = 0
    total_sec = 0.0
    for l in result.labels:
        active = result.speaker_active.get(l, [])
        total_sec = max(total_sec, len(active) * hop)
        run = 0
        for a in active:
            if a:
                run += 1
            else:
                if 0 < run < short_frames:
                    short_turns += 1
                run = 0
        if 0 < run < short_frames:
            short_turns += 1
    return short_turns / total_sec if total_sec > 0 else 0.0


def _mean_posterior(result: DiarizationResult) -> Optional[float]:
    post = result.posterior
    if not post:
        return None
    # Mean over frames where SOME speaker is active (the frames the decision is
    # about), falling back to the whole track if no activity is present.
    labels = result.labels
    vals = []
    for i, p in enumerate(post):
        any_active = any(
            i < len(result.speaker_active[l]) and result.speaker_active[l][i]
            for l in labels
        )
        if any_active:
            vals.append(p)
    if not vals:
        vals = list(post)
    return sum(vals) / len(vals)


# --------------------------------------------------------------------------- #
# Signal 7 helpers: replay the yield decision on the timelines, then perturb it.
# --------------------------------------------------------------------------- #

def _perturb_timeline(active: List[bool], k: int) -> List[bool]:
    """Dilate (``k`` > 0) or erode (``k`` < 0) a boolean timeline by ``|k|`` frames
    on EACH side -- a deterministic model of the diarizer having placed every
    speech boundary up to ``|k|`` frames off.

    Dilation grows each active run outward (activity spreads, gaps shrink);
    erosion trims each active run inward (gaps grow) and drops runs shorter than
    ``2|k|+1``. Erosion never manufactures an interior hole in a SOLID run -- it
    only widens gaps that already exist -- so a hold whose floor-holder is
    continuously active stays a hold, while a hold with a sub-threshold agent
    blip near the caller's floor can flip (it should: that blip may be a real
    yield the mono path bridged). O(n*|k|), no allocation beyond the output."""
    n = len(active)
    if k == 0 or n == 0:
        return [bool(x) for x in active]
    out = [False] * n
    if k > 0:
        for i in range(n):
            if active[i]:
                lo = i - k if i - k > 0 else 0
                hi = i + k + 1 if i + k + 1 < n else n
                for d in range(lo, hi):
                    out[d] = True
    else:
        r = -k
        for i in range(n):
            if i - r < 0 or i + r >= n:
                continue  # window runs off the edge -> cannot confirm -> False
            if all(active[d] for d in range(i - r, i + r + 1)):
                out[i] = True
    return out


def _run_len_intersecting(active: List[bool], lo: int, hi: int) -> int:
    """Length (frames) of the longest contiguous active run intersecting the
    half-open window ``[lo, hi)`` -- used to size the caller's floor supporting a
    yield (a backchannel is a very short such run)."""
    n = len(active)
    best = 0
    i = 0
    while i < n:
        if active[i]:
            j = i
            while j < n and active[j]:
                j += 1
            if i < hi and j > lo:
                best = max(best, j - i)
            i = j
        else:
            i += 1
    return best


def _timeline_yield(caller_active, agent_active, hop: float, cfg: ScoreConfig) -> dict:
    """Replay the engine's did_yield decision (``_engine.score.score_channels``
    step 2) directly over two boolean activity timelines -- the diarizer's own
    output -- with NO re-VAD and NO model call.

    Faithful to the engine: caller onset via ``first_active_sec``; a yield is the
    first agent-quiet run of at least ``yield_hangover_sec`` at/after onset within
    ``max_search_sec``, with the caller holding the floor within
    ``caller_proximity_sec``. Returns ``did_yield`` plus the decision internals the
    gate needs: ``yield_idx``, the FULL triggering agent-quiet ``gap_frames``, the
    ``yield_frames`` threshold, and the search ``grace`` window."""
    n = min(len(caller_active), len(agent_active))
    yield_frames = max(1, int(round(cfg.yield_hangover_sec / hop)))
    grace = max(1, int(round(cfg.caller_proximity_sec / hop)))
    out = {
        "did_yield": False,
        "yield_idx": None,
        "onset_idx": 0,
        "gap_frames": 0,
        "yield_frames": yield_frames,
        "grace": grace,
    }
    if n == 0:
        return out

    onset = first_active_sec(caller_active, hop, min_run_sec=cfg.onset_min_run_sec)
    onset_idx = int(round(onset / hop)) if onset >= 0 else 0
    onset_idx = max(0, min(onset_idx, n - 1))
    out["onset_idx"] = onset_idx

    search_end = min(n, onset_idx + int(round(cfg.max_search_sec / hop)))
    i = onset_idx
    while i < search_end:
        if not agent_active[i]:
            run = 0
            j = i
            while j < n and not agent_active[j]:
                run += 1
                if run >= yield_frames:
                    break
                j += 1
            if run >= yield_frames:
                lo = max(0, i - grace)
                hi = min(len(caller_active), i + grace)
                if any(caller_active[k] for k in range(lo, hi)):
                    # Extend past the hangover break to the full agent-quiet gap so
                    # the gate can size the decision's margin.
                    full = i
                    while full < n and not agent_active[full]:
                        full += 1
                    out["did_yield"] = True
                    out["yield_idx"] = i
                    out["gap_frames"] = full - i
                    return out
            i = j + 1
        else:
            i += 1
    return out


def _yield_boundary_confidence(result: DiarizationResult, speaker_map: dict,
                               cfg: ScoreConfig) -> dict:
    """The 7th signal: how much the did_yield verdict depends on sub-second
    boundary placement, computed straight off the diarization timelines.

    Reads the caller/agent timelines the ``speaker_map`` names, replays the engine
    yield decision on them, then re-runs that decision on the four sign-corners of
    a +/- ``YIELD_BOUNDARY_PERTURB_SEC`` boundary perturbation of BOTH speakers
    (dilate/erode). The verdict is FRAGILE when any corner flips ``did_yield``
    (``boundary_perturb_flip``) or when the yield rests on a caller run shorter
    than ``YIELD_MIN_CALLER_FLOOR_SEC`` (``backchannel_yield``). A fragile verdict
    can never be ``high``. ``robust`` is the high-tier gate; ``score`` in [0, 1]
    folds into ``separation_confidence`` (1.0 for a boundary-robust verdict,
    graded down by the yield's gap margin, capped low when fragile)."""
    caller = result.speaker_active.get(speaker_map.get("caller"), [])
    agent = result.speaker_active.get(speaker_map.get("agent"), [])
    hop = result.hop_sec or 0.01
    P = max(1, int(round(YIELD_BOUNDARY_PERTURB_SEC / hop)))

    base = _timeline_yield(caller, agent, hop, cfg)
    did_yield = base["did_yield"]

    # Perturb both boundaries by +/- P and see whether the verdict survives. The
    # agent gap drives a yield->hold flip (dilating the agent closes the gap, the
    # benchmark's exact missed-yield direction); eroding it opens a sub-threshold
    # gap into a hold->yield flip. Sampling the four sign-corners covers both.
    perturb_flip = False
    for ka, kc in ((P, P), (P, -P), (-P, P), (-P, -P)):
        pa = _perturb_timeline(agent, ka)
        pc = _perturb_timeline(caller, kc)
        if _timeline_yield(pc, pa, hop, cfg)["did_yield"] != did_yield:
            perturb_flip = True
            break

    gap_frames = base["gap_frames"]
    yield_frames = base["yield_frames"]
    grace = base["grace"]
    yidx = base["yield_idx"]

    trigger_gap_sec = round(gap_frames * hop, 3) if did_yield else None
    gap_margin_sec = round((gap_frames - yield_frames) * hop, 3) if did_yield else None

    caller_floor_sec = None
    backchannel = False
    yield_overlap_frac = 0.0
    if did_yield and yidx is not None:
        floor_frames = _run_len_intersecting(caller, yidx - grace, yidx + grace)
        caller_floor_sec = round(floor_frames * hop, 3)
        backchannel = floor_frames * hop < YIELD_MIN_CALLER_FLOOR_SEC
        # local overlap in a window around the yield point (reported, not gating:
        # pre-yield barge-in overlap is expected on a clean yield).
        w = max(1, int(round(YIELD_NEAR_WINDOW_SEC / hop)))
        n = min(len(caller), len(agent))
        lo = max(0, yidx - w)
        hi = min(n, yidx + w)
        both = sum(1 for k in range(lo, hi) if caller[k] and agent[k])
        yield_overlap_frac = round(both / (hi - lo), 3) if hi > lo else 0.0

    robust = (not perturb_flip) and (not backchannel)

    # Confidence contribution: a boundary-robust hold is neutral (1.0); a yield is
    # graded by how far its gap clears the hangover (full credit at >= P margin);
    # any fragility caps it low so `low`-tier fragile verdicts read as such.
    if did_yield:
        score = _clamp01((gap_frames - yield_frames) / P)
    else:
        score = 1.0
    if perturb_flip or backchannel:
        score = min(score, 0.25)

    return {
        "did_yield": did_yield,
        "trigger_gap_sec": trigger_gap_sec,
        "gap_margin_sec": gap_margin_sec,
        "caller_floor_sec": caller_floor_sec,
        "backchannel_yield": backchannel,
        "boundary_perturb_flip": perturb_flip,
        "yield_overlap_frac": yield_overlap_frac,
        "robust": robust,
        "score": round(score, 3),
    }


def _yield_low_reason(yb: dict, cfg: ScoreConfig) -> Optional[str]:
    """Plain-language reason a fragile-yield clip is `low`, guarding against the
    None fields of a hold-side flip."""
    ms = int(round(YIELD_BOUNDARY_PERTURB_SEC * 1000))
    if yb.get("backchannel_yield"):
        floor = yb.get("caller_floor_sec")
        return (
            "the yield rests on a backchannel-length caller interjection"
            + (f" ({floor:.2f}s)" if floor is not None else "")
            + "; a short yield reconstructed from one channel is only indicative"
        )
    if yb.get("boundary_perturb_flip"):
        if yb.get("did_yield"):
            gap = yb.get("trigger_gap_sec")
            return (
                "the did_yield verdict flips under a "
                f"+/-{ms}ms boundary shift"
                + (f" (agent-quiet gap only {gap:.2f}s vs a "
                   f"{cfg.yield_hangover_sec:.2f}s threshold)" if gap is not None else "")
                + "; sub-second timing from one channel is only indicative here"
            )
        return (
            f"a +/-{ms}ms boundary shift would turn this hold into a yield; the "
            "verdict sits on a sub-second agent-quiet gap that one channel cannot "
            "resolve, so it is only indicative"
        )
    return None


def separation_confidence(
    result: DiarizationResult,
    speaker_map: dict,
    *,
    backend: str = "pyannote",
    cfg: Optional[ScoreConfig] = None,
) -> dict:
    """Score one diarized-mono file's separability and assign a tier (spec 7).

    Seven signals -> ``separation_confidence`` in [0, 1] -> one of three tiers.
    Six measure DIARIZATION quality (speaker count, both-active, posterior,
    embedding margin, overlap, churn); the 7th (yield-boundary confidence,
    ``_yield_boundary_confidence``) measures how much the did_yield VERDICT depends
    on sub-second boundary placement -- the quantity the benchmark showed the other
    six are blind to. A boundary-fragile / backchannel / short-yield verdict can
    never be ``high``.
      * ``high``   -- score normally, labeled diarized-mono, ``indicative_only``
                      false. A real verdict, always tagged reconstructed-from-mono
                      (never presented as dual-channel), AND boundary-robust.
      * ``low``    -- score, but ``indicative_only`` true: the verdict is
                      "indicative only"; no pass/fail SLA gate fires on it.
      * ``refuse`` -- ``scorable`` false, a reason naming the failed signal, exit
                      2 -- exactly like today's mono rejection.

    Never a confident verdict on low-confidence separation. Returns the
    ``scorability.separation`` sub-block."""
    if cfg is None:
        cfg = ScoreConfig()
    labels = result.labels
    caller_label = speaker_map.get("caller")
    agent_label = speaker_map.get("agent")
    caller_sec = result.label_duration.get(caller_label, 0.0)
    agent_sec = result.label_duration.get(agent_label, 0.0)
    mean_post = _mean_posterior(result)
    margin = result.embedding_margin
    overlap_ratio = _overlap_ratio(result)
    churn = _segment_churn_per_sec(result)

    signals = {
        "speaker_count": result.num_speakers,
        "speaker_count_ok": result.num_speakers == 2,
        "caller_activity_sec": round(caller_sec, 3),
        "agent_activity_sec": round(agent_sec, 3),
        "both_speakers_active": (
            caller_sec >= MIN_SPEAKER_ACTIVITY_SEC
            and agent_sec >= MIN_SPEAKER_ACTIVITY_SEC
        ),
        "mean_posterior": round(mean_post, 3) if mean_post is not None else None,
        "embedding_margin": round(margin, 3) if margin is not None else None,
        "overlap_ratio": round(overlap_ratio, 3),
        "segment_churn_per_sec": round(churn, 3),
    }

    # --- refuse conditions (first match wins, most fundamental first) ----------
    reason = None
    if result.num_speakers != 2:
        reason = (
            f"detected {result.num_speakers} speaker(s), not 2, so the mix is not "
            "two clean parties (1 = could not separate; 3+ = background voices or "
            "mis-clustering)"
        )
    elif not signals["both_speakers_active"]:
        reason = (
            f"one speaker has too little detected speech "
            f"(caller {caller_sec:.2f}s, agent {agent_sec:.2f}s; need at least "
            f"{MIN_SPEAKER_ACTIVITY_SEC}s each) -- likely a single party split in two"
        )
    elif overlap_ratio >= OVERLAP_RATIO_REFUSE:
        reason = (
            f"overlap ratio {overlap_ratio:.2f} is extreme -- heavy crosstalk or "
            "the diarizer collapsing turns, so talk-over cannot be attributed from "
            "one channel"
        )
    elif churn >= CHURN_REFUSE:
        reason = (
            f"segmentation churn {churn:.2f} short turns/sec is too high -- the "
            "timeline is unstable, so the timing signals would be noise"
        )
    elif mean_post is not None and mean_post < POSTERIOR_REFUSE:
        reason = (
            f"mean segmentation posterior {mean_post:.2f} is near chance -- the "
            "model is unsure who is speaking, so boundaries are unreliable"
        )
    elif margin is not None and margin < EMBED_MARGIN_REFUSE:
        reason = (
            f"speaker-embedding separation margin {margin:.2f} is too small -- the "
            "two voices are too similar to separate confidently"
        )

    if reason is not None:
        signals_out = dict(signals)
        return {
            "backend": backend,
            "signals": signals_out,
            "separation_confidence": 0.0,
            "confidence_tier": "refuse",
            "indicative_only": True,
            "reason": reason,
        }

    # --- high vs low (all refuse gates cleared) --------------------------------
    # A bounded, interpretable blend of the sub-scores for reporting, plus a
    # strict "all-green" test for the high tier. Both use the same thresholds.
    def _sub(value, low, high):
        if value is None:
            return 1.0  # signal absent (e.g. margin on an EEND backend) -> neutral
        if high == low:
            return 1.0 if value >= high else 0.0
        return _clamp01((value - low) / (high - low))

    post_score = _sub(mean_post, POSTERIOR_REFUSE, POSTERIOR_HIGH)
    margin_score = _sub(margin, EMBED_MARGIN_REFUSE, EMBED_MARGIN_HIGH)
    overlap_score = _clamp01(
        (OVERLAP_RATIO_REFUSE - overlap_ratio)
        / (OVERLAP_RATIO_REFUSE - OVERLAP_RATIO_HIGH_MAX)
    )
    churn_score = _clamp01((CHURN_REFUSE - churn) / (CHURN_REFUSE - CHURN_HIGH_MAX))

    # Signal 7: yield-boundary confidence. Computed here (after the structural
    # refuse gates so the caller/agent timelines are two real speakers) straight
    # off the diarization timelines -- no model call, no reconstruction.
    yb = _yield_boundary_confidence(result, speaker_map, cfg)
    signals["yield_boundary"] = {
        "did_yield": yb["did_yield"],
        "trigger_gap_sec": yb["trigger_gap_sec"],
        "gap_margin_sec": yb["gap_margin_sec"],
        "caller_floor_sec": yb["caller_floor_sec"],
        "backchannel_yield": yb["backchannel_yield"],
        "boundary_perturb_flip": yb["boundary_perturb_flip"],
        "yield_overlap_frac": yb["yield_overlap_frac"],
        "robust": yb["robust"],
    }

    confidence = (
        post_score * margin_score * overlap_score * churn_score * yb["score"]
    )

    all_green = (
        (mean_post is None or mean_post >= POSTERIOR_HIGH)
        and overlap_ratio <= OVERLAP_RATIO_HIGH_MAX
        and churn <= CHURN_HIGH_MAX
        and (margin is None or margin >= EMBED_MARGIN_HIGH)
        and not speaker_map.get("balanced", False)
        and yb["robust"]  # the did_yield verdict must survive a +/-250ms boundary nudge
    )
    tier = "high" if all_green else "low"

    low_reason = None
    if tier == "low":
        # The yield-boundary fragility is surfaced FIRST: it is the benchmark's
        # decisive failure (every disagreement was a missed sub-second yield).
        yr = _yield_low_reason(yb, cfg)
        if yr is not None:
            low_reason = yr
        elif speaker_map.get("balanced", False):
            low_reason = (
                "caller/agent mapping is ambiguous (balanced floor time); confirm "
                "which speaker is the agent with --caller-speaker/--agent-speaker"
            )
        elif overlap_ratio > OVERLAP_RATIO_HIGH_MAX:
            low_reason = (
                f"overlap ratio {overlap_ratio:.2f} is elevated; sub-second "
                "talk-over from a single channel is only indicative"
            )
        elif mean_post is not None and mean_post < POSTERIOR_HIGH:
            low_reason = (
                f"mean segmentation posterior {mean_post:.2f} is moderate; treat "
                "boundaries as indicative"
            )
        elif margin is not None and margin < EMBED_MARGIN_HIGH:
            low_reason = (
                f"speaker-embedding margin {margin:.2f} is modest; attribution is "
                "only indicative"
            )
        elif churn > CHURN_HIGH_MAX:
            low_reason = (
                f"segmentation churn {churn:.2f}/sec is elevated; timing is only "
                "indicative"
            )
        else:
            low_reason = "reconstructed from a single channel; treat as indicative"

    return {
        "backend": backend,
        "signals": signals,
        "separation_confidence": round(confidence, 3),
        "confidence_tier": tier,
        "indicative_only": tier != "high",
        "reason": low_reason,
    }


# --------------------------------------------------------------------------- #
# Echo N/A on the diarized-mono path (spec 5.4).
# --------------------------------------------------------------------------- #

def echo_na_block() -> dict:
    """The ``signals.echo`` block for the diarized-mono path: definitionally N/A.

    The two reconstructed tracks are slices of ONE physical microphone, so
    cross-channel echo/crosstalk coherence carries no echo information (it is
    trivially high in overlap). It is marked not-applicable and, because
    ``echo_suspected`` is False, the ``--echo-gate`` can never fire on this
    path."""
    return {
        "applicable": False,
        "reason": (
            "single physical channel: cross-channel echo/crosstalk is not defined "
            "on a diarized-mono reconstruction (both tracks are slices of one "
            "microphone); the echo gate does not apply here"
        ),
        "coherence": None,
        "lag_sec": None,
        "echo_suspected": False,
    }


# --------------------------------------------------------------------------- #
# The end-to-end preparation core.run_single hands off to.
# --------------------------------------------------------------------------- #

@dataclass
class DiarizedMono:
    """What ``core.run_single``'s ``--mono --diarize`` branch needs to finish the
    envelope with the EXISTING scoring/echo/resume machinery.

    On the score path (``high``/``low``): ``caller_track`` / ``agent_track`` are
    two equal-length masked arrays ready for ``score_channels``; ``indicative_only``
    is True on ``low``. On the refuse path: the tracks are ``None`` and
    ``not_scorable_reason`` is set (-> ``scorable: false``, exit 2)."""

    caller_track: object
    agent_track: object
    separation: dict
    speaker_map: dict
    tier: str
    indicative_only: bool
    provenance: dict
    not_scorable_reason: Optional[str]


def prepare_diarized_mono(
    mono_samples: Sequence[float],
    sample_rate: int,
    *,
    backend: str = "pyannote",
    num_speakers: int = 2,
    caller_speaker: Optional[str] = None,
    agent_speaker: Optional[str] = None,
    egress_opt_in: bool = False,
    cfg: Optional[ScoreConfig] = None,
) -> DiarizedMono:
    """Diarize -> assign -> gate -> reconstruct, returning everything the envelope
    needs. Raises ``BackendUnavailable`` (never a fallback) if the backend cannot
    load. The hosted backend additionally refuses without ``egress_opt_in``."""
    if cfg is None:
        cfg = ScoreConfig()
    fn = resolve_diarizer(backend)
    # The hosted backend is the only one that leaves the machine; gate it on an
    # explicit egress opt-in, checked BEFORE any audio is sent.
    if backend == "pyannoteai" and not egress_opt_in:
        raise BackendUnavailable(
            "the 'pyannoteai' backend is HOSTED: it uploads your audio to "
            "pyannote.ai. This breaks hotato's no-egress default, so it is refused "
            "unless you pass --egress-opt-in (CLI) / egress_opt_in=True. Use "
            "'pyannote' (local, offline) or 'sortformer' (local) to keep audio on "
            "this machine."
        )
    result = fn(mono_samples, sample_rate, _hop_samples(sample_rate, cfg) / sample_rate, num_speakers)

    speaker_map = assign_speakers(result, caller_speaker, agent_speaker)
    sep = separation_confidence(result, speaker_map, backend=backend, cfg=cfg)
    tier = sep["confidence_tier"]

    provenance = {
        "source": "diarized-mono",
        "backend": backend,
        "model": result.model,
        "model_version": result.model_version,
        "num_speakers": result.num_speakers,
        "speaker_map": speaker_map,
        "separation_confidence": sep["separation_confidence"],
        "confidence_tier": tier,
        "overlap_ratio": sep["signals"]["overlap_ratio"],
        "licenses": SEPARATION_MODEL_LICENSES,
        "note": (
            "reconstructed from a single channel via speaker diarization; the "
            "dual-channel recording is the gold reference and this is never "
            "equivalent to it for sub-second talk-over attribution"
        ),
    }

    if tier == "refuse":
        return DiarizedMono(
            caller_track=None,
            agent_track=None,
            separation=sep,
            speaker_map=speaker_map,
            tier=tier,
            indicative_only=True,
            provenance=provenance,
            not_scorable_reason=sep["reason"],
        )

    caller_track, agent_track = reconstruct_tracks(
        mono_samples,
        result,
        speaker_map["caller"],
        speaker_map["agent"],
        sample_rate=sample_rate,
        cfg=cfg,
    )
    return DiarizedMono(
        caller_track=caller_track,
        agent_track=agent_track,
        separation=sep,
        speaker_map=speaker_map,
        tier=tier,
        indicative_only=sep["indicative_only"],
        provenance=provenance,
        not_scorable_reason=None,
    )


# --------------------------------------------------------------------------- #
# The hermetic STUB backend (tests only).
# --------------------------------------------------------------------------- #

def build_stub_backend(
    timelines: Optional[Dict[str, List[bool]]] = None,
    *,
    posterior=None,
    embedding_margin: Optional[float] = None,
    overlap: Optional[List[bool]] = None,
    model: str = "stub",
    model_version: str = "0",
) -> FactoryFn:
    """A dependency-free, deterministic diarizer for tests.

    Two modes:
      * TRUTH mode -- pass ``timelines`` (label -> per-frame ``list[bool]``), e.g.
        the two ground-truth per-channel VAD tracks of an AMI recording summed to
        mono. The stub returns exactly those, so any pipeline error is the
        reconstruction/gate itself, not diarizer error. ``posterior`` (a scalar or
        a per-frame list), ``embedding_margin``, and ``overlap`` let a test steer
        each confidence tier.
      * ENERGY-SPLIT mode -- ``timelines=None``: run the reference energy VAD on
        the mono and assign contiguous active RUNS to the two speakers
        alternately. Crude (no model), but a valid two-timeline structure so the
        seam is exercisable with no truth on hand.

    Returns a zero-arg factory (matching the seam), so registration is free."""

    def _factory() -> BackendFn:
        def _backend(mono_samples, sample_rate, hop_sec, num_speakers) -> DiarizationResult:
            hop = max(1, int(round(hop_sec * sample_rate)))
            n = len(mono_samples)
            n_frames = (n + hop - 1) // hop if n else 0

            if timelines is not None:
                active = {
                    label: _fit(list(track), n_frames)
                    for label, track in timelines.items()
                }
            else:
                active = _energy_split(mono_samples, sample_rate, hop, n_frames)

            # per-label duration
            label_dur = {
                label: round(sum(track) * hop_sec, 6)
                for label, track in active.items()
            }
            # overlap track
            if overlap is not None:
                over = _fit(list(overlap), n_frames)
            else:
                labels = sorted(active)
                over = [
                    sum(1 for l in labels if active[l][i]) >= 2
                    for i in range(n_frames)
                ]
            # posterior track
            if posterior is None:
                post = [1.0] * n_frames  # a clean, confident stub default
            elif isinstance(posterior, (int, float)):
                post = [float(posterior)] * n_frames
            else:
                post = _fit_floats(list(posterior), n_frames)

            return DiarizationResult(
                speaker_active=active,
                hop_sec=hop_sec,
                posterior=post,
                overlap=over,
                label_duration=label_dur,
                embedding_margin=embedding_margin,
                model=model,
                model_version=model_version,
            )

        return _backend

    return _factory


def _fit(track: List[bool], n: int) -> List[bool]:
    if len(track) >= n:
        return [bool(x) for x in track[:n]]
    return [bool(x) for x in track] + [False] * (n - len(track))


def _fit_floats(track: List[float], n: int) -> List[float]:
    if len(track) >= n:
        return [float(x) for x in track[:n]]
    tail = track[-1] if track else 0.0
    return [float(x) for x in track] + [float(tail)] * (n - len(track))


def _energy_split(mono_samples, sample_rate, hop, n_frames) -> Dict[str, List[bool]]:
    """Deterministic no-model split: energy-VAD the mono, then hand contiguous
    active runs to the two speakers alternately. Crude on purpose."""
    rms, hop_sec = frame_rms(mono_samples, sample_rate, 20.0, hop / sample_rate * 1000.0)
    active = energy_vad(rms, hop_sec).active
    a = [False] * len(active)
    b = [False] * len(active)
    turn = 0
    i = 0
    while i < len(active):
        if active[i]:
            j = i
            while j < len(active) and active[j]:
                j += 1
            target = a if turn % 2 == 0 else b
            for k in range(i, j):
                target[k] = True
            turn += 1
            i = j
        else:
            i += 1
    return {SPEAKER_A: _fit(a, n_frames), SPEAKER_B: _fit(b, n_frames)}


# --------------------------------------------------------------------------- #
# The three REAL backends (import-guarded; absent deps -> BackendUnavailable).
# --------------------------------------------------------------------------- #

def build_pyannote_backend() -> BackendFn:
    """Factory for the pyannote.audio backend (``speaker-diarization-community-1``,
    local, CPU-viable, offline once the gated weights are cached).

    Requires the ``[diarize]`` extra (pyannote.audio, torch, torchaudio, numpy)
    plus a Hugging Face token with the gated model conditions accepted; the
    weights download once under ``HF_HOME`` and inference then runs offline. An
    absent extra / token / model raises ``BackendUnavailable`` -- never a silent
    fallback to scoring raw mono."""
    try:
        import os

        import numpy as np  # noqa: F401
        import torch  # noqa: F401
        from pyannote.audio import Pipeline
    except Exception as exc:
        raise BackendUnavailable(
            "the 'pyannote' diarizer requires the optional extra: "
            "pip install 'hotato[diarize]' (missing dependency: "
            f"{exc}). It also needs a Hugging Face token with the "
            "speaker-diarization-community-1 conditions accepted. The dual-channel "
            "path stays the reference; nothing falls back to a raw-mono guess."
        ) from exc

    token = (
        os.environ.get("HUGGINGFACE_TOKEN")
        or os.environ.get("HF_TOKEN")
        or os.environ.get("HUGGING_FACE_HUB_TOKEN")
    )
    model_id = os.environ.get(
        "HOTATO_DIARIZE_MODEL", "pyannote/speaker-diarization-community-1"
    )
    try:
        try:
            pipeline = Pipeline.from_pretrained(model_id, token=token)
        except TypeError:
            # pyannote.audio 4.0.7's Pipeline.from_pretrained renamed the
            # kwarg from `use_auth_token` to `token` and DROPPED the old name
            # outright (a hard TypeError, not a deprecation warning) -- try
            # both so either release loads cleanly, same pattern as the
            # return_embeddings= fallback below.
            pipeline = Pipeline.from_pretrained(model_id, use_auth_token=token)
    except Exception as exc:
        raise BackendUnavailable(
            "the '[diarize]' extra is installed but the pyannote pipeline "
            f"{model_id!r} could not be loaded. The weights are GATED on Hugging "
            "Face: accept the model conditions and set HUGGINGFACE_TOKEN (or "
            "HF_TOKEN). After the one-time download it runs offline. "
            f"(underlying: {exc})"
        ) from exc

    def _backend(mono_samples, sample_rate, hop_sec, num_speakers) -> DiarizationResult:
        import numpy as np

        wav = np.asarray(mono_samples, dtype=np.float32)
        n = wav.shape[0]
        n_frames = (n + int(round(hop_sec * sample_rate)) - 1) // int(round(hop_sec * sample_rate)) if n else 0
        tensor = {"waveform": _as_2d(wav), "sample_rate": int(sample_rate)}
        try:
            output = pipeline(tensor, num_speakers=num_speakers, return_embeddings=True)
        except TypeError:
            # A pyannote build that rejects return_embeddings= outright.
            output = pipeline(tensor, num_speakers=num_speakers)
        annotation, embeddings = _unpack_pipeline_output(output)

        labels = list(annotation.labels())
        active = {l: [False] * n_frames for l in labels}
        for segment, _, label in annotation.itertracks(yield_label=True):
            lo = max(0, int(segment.start / hop_sec))
            hi = min(n_frames, int(math.ceil(segment.end / hop_sec)))
            track = active.setdefault(label, [False] * n_frames)
            for k in range(lo, hi):
                track[k] = True
        label_dur = {
            l: round(annotation.label_duration(l), 6) for l in annotation.labels()
        }
        over = [
            sum(1 for l in active if active[l][i]) >= 2 for i in range(n_frames)
        ]
        margin = _embedding_margin(embeddings)
        return DiarizationResult(
            speaker_active=active,
            hop_sec=hop_sec,
            posterior=[1.0] * n_frames,  # frame posteriors read via a separate
                                         # Inference pass in a fuller build; the
                                         # margin + overlap + activity signals gate
                                         # this backend today.
            overlap=over,
            label_duration=label_dur,
            embedding_margin=margin,
            model=model_id,
            model_version="community-1",
        )

    return _backend


def _as_2d(wav):
    import numpy as np

    arr = np.asarray(wav, dtype=np.float32)
    if arr.ndim == 1:
        arr = arr.reshape(1, -1)
    try:
        import torch

        return torch.from_numpy(arr)
    except Exception:  # pragma: no cover - torch present with the extra
        return arr


def _unpack_pipeline_output(output):
    """Unpack a pyannote ``pipeline(...)`` call across the 3.x/4.x return-shape
    split into ``(annotation, embeddings)``.

    pyannote.audio 3.x returns a bare ``Annotation`` (or, with
    ``return_embeddings=True``, a ``(Annotation, embeddings)`` tuple). 4.x
    (>=4.0) instead returns one ``DiarizeOutput`` object -- not a tuple, not
    iterable, no ``.labels()`` -- carrying the same information as
    ``.speaker_diarization`` / ``.speaker_embeddings``. Unpacking that object
    as a 3.x tuple raises ``TypeError``, and calling ``.labels()`` on it
    directly raises ``AttributeError``; branch on the attribute rather than a
    version check so both shapes land on the same pair."""
    if hasattr(output, "speaker_diarization"):
        # pyannote.audio 4.x DiarizeOutput.
        return output.speaker_diarization, getattr(output, "speaker_embeddings", None)
    if isinstance(output, tuple):
        # pyannote.audio 3.x with return_embeddings=True.
        return output
    # pyannote.audio 3.x plain Annotation (no embeddings returned).
    return output, None


def _embedding_margin(embeddings) -> Optional[float]:
    """Cosine separation (in [0, 1]) between the two speaker centroids, derived
    from pyannote's ``return_embeddings`` output. ``None`` when unavailable,
    when there are not exactly two centroids, or when a centroid is a
    degenerate (zero-norm / non-finite) vector -- pyannote returns one when it
    could not reliably estimate a speaker's embedding (near-silent split,
    extraction failure). Dividing by a zero norm used to fall back to a
    fabricated ``cos = 0`` (margin 0.5, read by the gate as adequate
    separation) instead of signalling "no margin available", the same way a
    missing embeddings array already does; a degenerate centroid now returns
    ``None`` too.

    Benchmarked (DIARIZE-BENCHMARK-2026-07-09): even past that fix, margin
    measured on a real pyannote community-1 run over the AMI corpus clustered
    tightly (~0.43-0.52) regardless of downstream verdict correctness and did
    not reliably separate correct from incorrect gate tiers -- uninformative
    on that set, not demonstrably defective. Redesign is deferred to the
    gate-recalibration stage, not tuned here."""
    if embeddings is None:
        return None
    try:
        rows = [[float(x) for x in row] for row in embeddings]
    except (TypeError, ValueError):
        return None
    if len(rows) < 2 or not rows[0] or len(rows[0]) != len(rows[1]):
        return None
    a, b = rows[0], rows[1]
    norm_a = math.sqrt(sum(x * x for x in a))
    norm_b = math.sqrt(sum(x * x for x in b))
    if not (math.isfinite(norm_a) and math.isfinite(norm_b)) or norm_a == 0.0 or norm_b == 0.0:
        return None  # degenerate centroid: no reliable margin, not cos=0
    cos = sum(x * y for x, y in zip(a, b)) / (norm_a * norm_b)
    if not math.isfinite(cos):
        return None
    # cosine distance normalized to [0, 1]
    return max(0.0, min(1.0, (1.0 - cos) / 2.0))


def build_sortformer_backend() -> BackendFn:
    """Factory for the NVIDIA Sortformer backend
    (``diar_streaming_sortformer_4spk-v2``, CC-BY-4.0, best self-hostable on
    telephone). Requires the ``[diarize-sortformer]`` extra (nemo-toolkit, torch)
    and effectively a GPU. An EEND model: it exposes per-frame per-speaker
    sigmoid posteriors but no clustering margin (``embedding_margin`` stays
    ``None``). Absent deps -> ``BackendUnavailable``, never a fallback."""
    try:
        import numpy as np  # noqa: F401
        import torch  # noqa: F401
        from nemo.collections.asr.models import SortformerEncLabelModel
    except Exception as exc:
        raise BackendUnavailable(
            "the 'sortformer' diarizer requires the optional extra: "
            "pip install 'hotato[diarize-sortformer]' (missing dependency: "
            f"{exc}). It is GPU-leaning and downloads the CC-BY-4.0 streaming v2 "
            "checkpoint from NGC (the offline v1 is CC-BY-NC and must not be "
            "shipped). Nothing falls back to a raw-mono guess."
        ) from exc

    import os

    ckpt = os.environ.get(
        "HOTATO_SORTFORMER_MODEL", "nvidia/diar_streaming_sortformer_4spk-v2"
    )
    try:
        model = SortformerEncLabelModel.from_pretrained(ckpt)
        model.eval()
    except Exception as exc:
        raise BackendUnavailable(
            "the '[diarize-sortformer]' extra is installed but the Sortformer "
            f"checkpoint {ckpt!r} could not be loaded (network / NGC access / GPU). "
            f"(underlying: {exc})"
        ) from exc

    def _backend(mono_samples, sample_rate, hop_sec, num_speakers) -> DiarizationResult:
        # NeMo's streaming diarizer returns per-frame per-speaker activity; the
        # exact tensor plumbing is deployment-specific (audio path vs in-memory).
        # This adapter is import-guarded and license-correct; wiring the tensor
        # I/O for a given NeMo release is left to the deploy that installs the
        # extra. Kept explicit rather than approximated so no fabricated timeline
        # is ever produced.
        raise BackendUnavailable(
            "the sortformer backend is installed but its per-release tensor I/O is "
            "not wired in this build; use --diarizer pyannote (local) meanwhile. "
            "This is a clean unavailable, never a raw-mono fallback."
        )

    return _backend


def build_pyannoteai_backend() -> BackendFn:
    """Factory for the pyannoteAI HOSTED backend (precision-2, best absolute).
    Requires the ``[diarize-hosted]`` extra and an API key, and UPLOADS audio to
    pyannote.ai -- so it is gated behind an explicit egress opt-in in
    ``prepare_diarized_mono`` and prints an audio-leaves-this-machine notice.
    Absent deps / key -> ``BackendUnavailable``, never a fallback."""
    try:
        import os

        from pyannoteai.sdk import Client  # noqa: F401  # type: ignore
    except Exception as exc:
        raise BackendUnavailable(
            "the 'pyannoteai' hosted diarizer requires the optional extra: "
            "pip install 'hotato[diarize-hosted]' (missing dependency: "
            f"{exc}). It is HOSTED (audio leaves this machine) and needs "
            "PYANNOTEAI_API_KEY. Prefer 'pyannote'/'sortformer' to stay local."
        ) from exc

    key = os.environ.get("PYANNOTEAI_API_KEY")
    if not key:
        raise BackendUnavailable(
            "the '[diarize-hosted]' extra is installed but PYANNOTEAI_API_KEY is "
            "not set. This backend uploads audio to pyannote.ai; set the key and "
            "pass --egress-opt-in, or use a local backend."
        )

    def _backend(mono_samples, sample_rate, hop_sec, num_speakers) -> DiarizationResult:
        raise BackendUnavailable(
            "the pyannoteai hosted backend is installed but its upload/transport "
            "is not wired in this build; use --diarizer pyannote (local) meanwhile. "
            "A clean unavailable, never a raw-mono fallback."
        )

    return _backend
