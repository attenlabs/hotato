"""``hotato trust``: the input-health check you run BEFORE scoring a call.

Also known as the "trust doctor": it inspects one recording and reports whether
the audio is even SCORABLE, so a bad export (a mono file, a silent channel, a
swapped channel map) is caught up front instead of producing a confident-looking
but meaningless turn-taking verdict downstream.

What it reports, all of it INPUT health and nothing about the agent's behaviour:

  per-channel activity      how much speech each channel carries (caller expected
                            on channel 0, agent on channel 1, per the corpus
                            manifest convention) and when each first speaks
  possible channel swap     a heuristic flag: if the channel mapped as the caller
                            holds the floor far longer than the channel mapped as
                            the agent, the caller/agent channels may be reversed
  sample rate + duration    the basic recording facts
  clipping                  per-channel peak level and the fraction of samples at
                            full scale (a sign the input was recorded too hot)
  leading silence           dead air before the first speech on either channel
  crosstalk risk            cross-channel echo coherence: is the caller channel
                            carrying a delayed copy of the agent's own audio?
  cross-channel leakage     the level of a consistent attenuated delayed COPY of
                            one channel found on the other; loud leakage can be
                            read as the other party's activity, which the
                            whole-clip coherence above can miss
  low signal level          a capture so quiet that turn timing can be
                            under-measured downstream (a warning, never a gate)
  scorability               separated tracks? enough caller activity? enough agent
                            activity? -- the three things a real score needs
  recommendation            "eligible for scan", or "NOT SCORABLE" with the
                            specific reason AND the next step to fix it

HONESTY, the whole point of this command: it NEVER labels intent and NEVER emits
a turn-taking verdict (no yield / hold, no pass / fail). It answers exactly one
question -- is this audio good enough to score? -- and stops there. "Eligible for
scan" says the input is scorable, NOT that the audio is problem-free: a recording
that is eligible for scan may still contain agent bugs; that is what
``hotato scan`` / ``hotato run`` are for.

Everything runs offline and reuses hotato's existing primitives: the hardened WAV
reader (``core._read_wav``), the reference framing (``_engine.audio.frame_rms``),
the energy VAD (``_engine.vad.energy_vad`` / ``first_active_sec``), and the
cross-channel echo coherence (``echo.echo_signal``). No DSP is reimplemented here.
"""

from __future__ import annotations

import math
import os
from typing import List, Optional, Tuple

from ._engine.audio import frame_rms, to_dbfs
from ._engine.score import ScoreConfig
from ._engine.vad import VADParams, energy_vad, first_active_sec
from .echo import DEFAULT_COHERENCE_THRESHOLD, echo_signal
from .errors import ChannelRangeError

__all__ = [
    "trust_report",
    "render_text",
    "NOTE",
    "SAFE_RECOMMENDATION",
    "NEXT_STEP_CHANNEL_MAP",
    "VERDICT_MODE_SCAN",
    "VERDICT_MODE_CONTRACT",
    "VERDICT_MODES",
    "VERDICT_COHERENCE_THRESHOLD",
    "VERDICT_LEAKAGE_DB",
    "VERDICT_INELIGIBLE_REASON",
    "crosstalk_verdict_suspected",
]

NOTE = (
    "input health only: whether the audio is scorable, not a judgement of the "
    "agent's turn-taking. It never labels intent and never emits a yield/hold "
    "or pass/fail verdict."
)

# A recording that clears every gate below. The wording is deliberately
# "eligible for scan", NOT a safety guarantee: clearing the scorability gates makes
# an input SCORABLE (eligible to be measured), which is a statement about the input,
# not a safety guarantee about the recording or the agent. A scorable recording
# can still carry bugs, clipping, or bleed; only the not-scorable gate is a hard
# stop. Overclaiming safety is exactly the headline honesty gap this rename fixes.
SAFE_RECOMMENDATION = "eligible for scan"

# The single fix instruction shared by every channel-level not-scorable reason
# (a silent channel, or the two channels swapped): the mapping or the export is
# wrong, and both are fixed the same way.
NEXT_STEP_CHANNEL_MAP = "verify channel mapping or export dual-channel again"
NEXT_STEP_DUAL_CHANNEL = (
    "export a dual-channel recording with the caller on one channel and the "
    "agent on the other"
)

# --- thresholds (all exposed as constants so they are inspectable) ----------

# A sustained run at least this long counts as "speech" on a channel; shorter
# blips (a click, a single frame over the gate) do not open a turn.
SPEECH_MIN_RUN_SEC = 0.10
# A channel needs at least this many seconds of detected speech to be scorable;
# below it there is effectively nothing to attribute a turn to.
MIN_ACTIVITY_SEC = 0.30
# A sample at or above this absolute level (in [0, 1]) counts as clipped; 0.99 is
# within ~0.09 dB of 16-bit full scale.
CLIP_PEAK_LIN = 0.99
# Clipping is only WARNED when at least this fraction of a channel's samples are
# at full scale, so a single stray peak is not reported as a hot recording.
CLIP_WARN_FRACTION = 0.001
# Leading silence longer than this is WARNED (a capture/trigger lag), never a
# not-scorable condition on its own.
LEADING_SILENCE_WARN_SEC = 3.0
# Possible-swap heuristic: flag a reversal only when the channel mapped as the
# caller holds the floor at least this many TIMES longer than the channel mapped
# as the agent AND by at least this ABSOLUTE margin, so ordinary variation in a
# correctly mapped call does not trip it. The usual pattern is agent-dominant
# (an assistant answering in paragraphs), so caller-dominance is the signal.
SWAP_DOMINANCE_RATIO = 1.5
SWAP_ABS_MARGIN_SEC = 1.0

# --- cross-channel leakage (bleed) estimate --------------------------------
# The whole-clip echo coherence (echo.echo_signal) is a single best-lag cosine
# over the ENTIRE envelope, so unrelated activity elsewhere in the recording
# dilutes it: symmetric bleed that demonstrably corrupts a downstream timing
# verdict can sit well under the 0.7 coherence bar and never be flagged. This
# estimate adds the missing signal by measuring the RESIDUAL level of one channel
# that is an attenuated, delayed COPY of the other -- the physical signature of
# leaked audio -- in a way that does NOT depend on the leak being quiet enough to
# leave the other channel's VAD idle (a loud leak re-triggers it, which is exactly
# the regime that breaks the verdict).
#
# A true leak is one source scaled by a single gain and delay, so its per-frame
# level ratio resid/src is the SAME (a constant negative dB) on every frame it
# appears; independent speech has no such fixed ratio. So we look, per candidate
# lag, at the distribution of the per-frame resid/src ratio over frames where the
# residual channel is active and the source was active `lag` frames earlier, and
# a tight cluster (a consistent, attenuated ratio over enough frames) is a leak.
LEAKAGE_MAX_LAG_SEC = 0.5
# A frame's ratio counts toward the cluster's "consistency" when it is within this
# many dB of the cluster median.
LEAKAGE_TOL_DB = 3.0
# At least this fraction of the qualifying frames must fall inside that band for
# the ratios to read as one scaled copy rather than independent speech. Calibrated
# so no clean dual-channel fixture in the corpus reaches it (their consistent
# copies are the digital-silence floor, far below the warn level, or independent
# speech whose consistency stays well under this).
LEAKAGE_CONSISTENCY = 0.85
# A leak is ATTENUATED: the copy must sit at least this far below the source, so
# genuine equal-level double-talk (ratio ~0 dB) is never mistaken for a leak.
LEAKAGE_ATTEN_MAX_DB = -6.0
# Only report a leakage level at or above this; fainter consistent copies are the
# framing floor of near-silent channels, not usable bleed, and stay null.
LEAKAGE_REPORT_DB = -50.0
# At or above this level the leaked audio is loud enough to be read as the other
# party's activity and corrupt a downstream timing verdict, so it is flagged
# (suspected + warning + the recommendation is downgraded off "eligible for scan").
# Calibrated to the level at which the red-team reproduced a verdict break under
# symmetric bleed (~ -40 dB); every clean dual-channel corpus fixture stays well
# below it (their loudest consistent copies sit at ~ -54 dB), so none are flagged.
LEAKAGE_WARN_DB = -40.0
# At least this many qualifying frames before a cluster is trusted at all.
LEAKAGE_MIN_FRAMES = 12
# The copy must COVER at least this fraction of the source channel's active time.
# Real bleed shadows its source: the copy appears on the other channel EVERY time
# the source speaks, so it spans nearly all of the source's active frames. A brief
# genuine overlap (two independent speakers talking at once for a moment, whose
# steady levels can momentarily hold a constant ratio) covers only that moment, so
# this rejects it. This is the gate that separates a real leak from a coincidental
# consistent ratio during a short overlap.
LEAKAGE_MIN_COVERAGE = 0.5

# --- dynamic leakage rule (secondary, catches a leak earlier than the fixed dB) --
# The fixed LEAKAGE_WARN_DB above is a RATIO (copy vs source), not aligned with the
# scorer's real failure boundary: what actually corrupts a downstream measurement is
# the leaked copy crossing the RECEIVING channel's own VAD activity gate, so its
# leaked frames get counted as that party's activity. That gate is an ABSOLUTE level
# (max(noise_floor + rel_db, abs_gate_db)), so a faint-ratio leak of a LOUD source can
# cross it while a same-ratio leak of a quiet source does not. This secondary rule
# predicts the leaked copy's absolute level (source level + the estimated bleed gain)
# and flags the leak when that prediction clears the receiver's gate for a sustained
# run -- letting a leak caution EARLIER than the -40 dB ratio bar. It only ADDS
# suspected cases; the fixed LEAKAGE_WARN_DB case is preserved unchanged.
#
# The receiver gate is built from the VAD reference defaults (imported, not hard-coded
# here) so it tracks the same rel_db / abs_gate_db the scorer's VAD uses.
_VAD_REL_DB = VADParams.rel_db          # 15.0 dB: the VAD's speech margin over the floor
_VAD_ABS_GATE_DB = VADParams.abs_gate_db  # -60.0 dBFS: the VAD's absolute activity floor
# The predicted leak must clear the receiver gate by this margin over a sustained run
# before the dynamic rule cautions. Without it, a leak that merely grazes the absolute
# gate floor (the faint, documented-safe boundary at ~ -46 dB ratio, whose loudest
# leaked frames sit ~ 2 dB above the -60 dBFS floor) would trip; the margin keeps that
# safe while still cautioning EARLIER than -40 dB for a leak that clearly clears the
# gate (e.g. a loud source's -45 dB-ratio leak, whose copy sits ~ 6 dB above it).
LEAKAGE_GATE_MARGIN_DB = 6.0

# --- low input level -------------------------------------------------------
# When even the loudest channel peaks below this, the recording is quiet enough
# that framing/threshold quantization can materially UNDER-estimate turn timing
# downstream while every scorability gate still passes. Warned (never a
# not-scorable condition on its own -- the not-scorable floor is lower and
# unchanged). Calibrated above the level where timing measurably breaks in the
# low-gain reproduction (~ -31 dBFS) and far below any normal capture (the corpus
# loudest channels sit at -4 to -1 dBFS), so a normal recording never trips it.
LOW_SIGNAL_WARN_DBFS = -30.0

CAUTION_RECOMMENDATION = "scan with caution"

# --- K6: verdict eligibility, distinct from candidate/scan eligibility ------
# ``scorable`` (aliased below as ``candidate_eligible``) says the audio is
# usable at all: separated tracks, enough activity on each side -- the bar
# ``scan`` needs to surface ADVISORY candidates + audio for human review.
# ``verdict_eligible`` is a NARROWER, separate gate: a suspected channel swap,
# or cross-channel crosstalk/leakage at or above a VERDICT threshold, refuses
# a yield/hold VERDICT (did_yield / seconds_to_yield / talk_over_sec / verdict)
# even though the input stays candidate-eligible. This is what stops a
# suspected swap or high crosstalk from silently producing a confident-looking
# verdict: candidate discovery (`scan`) never emitted one anyway, but `run` /
# `contract create` / `contract verify` do, and they must check this field
# before doing so.
VERDICT_MODE_SCAN = "scan"
VERDICT_MODE_CONTRACT = "contract"
VERDICT_MODES = (VERDICT_MODE_SCAN, VERDICT_MODE_CONTRACT)

# Whole-clip echo coherence bar used for VERDICT eligibility, by mode. "scan"
# reuses the existing crosstalk-risk bar (DEFAULT_COHERENCE_THRESHOLD, the same
# number `echo_suspected` uses); "contract" (contract create/verify, and any CI
# gate) is STRICTER -- it trips at a lower coherence -- because a false-
# confident CI pass is more costly than an advisory scan caution.
VERDICT_COHERENCE_THRESHOLD = {
    VERDICT_MODE_SCAN: DEFAULT_COHERENCE_THRESHOLD,
    VERDICT_MODE_CONTRACT: 0.6,
}
# Leak-ratio dB bar used for VERDICT eligibility, by mode. "scan" reuses the
# existing LEAKAGE_WARN_DB bar; "contract" is stricter at -46 dB -- the
# documented "faint, safe" boundary the dynamic gate-crossing rule already
# discusses above (LEAKAGE_GATE_MARGIN_DB) -- closing that gap for the
# higher-stakes contract/CI gate.
VERDICT_LEAKAGE_DB = {
    VERDICT_MODE_SCAN: LEAKAGE_WARN_DB,
    VERDICT_MODE_CONTRACT: -46.0,
}

# --- waveform corroboration for the VERDICT-level COHERENCE bar --------------
# The whole-clip echo COHERENCE (echo_signal) is a cosine of the two per-frame
# RMS ENVELOPES, so it reads high for ANY two channels whose active windows
# overlap -- including two INDEPENDENT distinct speakers in genuine simultaneous
# speech (a real barge-in), whose envelopes align even though neither channel is
# a copy of the other. That is NOT cross-channel leakage. A genuine leak is one
# source physically present on both channels: one channel carries a delayed,
# attenuated COPY of the OTHER's actual WAVEFORM, so their raw sample waveforms
# CORRELATE. Independent speakers do not: different voices/content decorrelate at
# the sample level even when their envelopes overlap. So the envelope-coherence
# trigger is corroborated with the sample-level cross-correlation of the raw
# waveforms (``_waveform_copy_corr``): the VERDICT-level coherence bar fires only
# when the waveforms ALSO correlate, which fires on real leakage/echo and NOT on
# distinct-speaker overlap. A genuinely leaky recording that the coherence bar
# would miss anyway is still caught by the copy-ratio leakage path
# (``leakage_alters_mask`` / ``leakage_crosses_gate`` / ``leakage_db``), which is
# UNCHANGED -- this only removes the false REFUSAL of a legitimate barge-in.
#
# Calibrated with a wide margin: a genuine delayed copy scores ~1.0 here, while
# every measured distinct-speaker overlap (pure tones or broadband voices) stays
# below ~0.05, so the exact bar is not sensitive.
WAVEFORM_LEAKAGE_MIN_CORR = 0.5
# Pure-Python fallback (used only when numpy is absent) bounds: the numpy path
# scans the full lag range via FFT; the fallback correlates a bounded span,
# anchored at the first both-active region, over a tight lag window around the
# envelope's best lag (which localizes the true delay to within +/- half a hop).
# Both paths separate a copy (~1.0) from independent speech (~0) by a wide
# margin; only the exact value differs, never the copy-vs-independent verdict.
_WF_LAG_PAD_HOPS = 1
_WF_MAX_SPAN_SEC = 0.75

# The reason a verdict-ineligible consumer emits in place of a real
# did_yield/seconds_to_yield/talk_over_sec/verdict. Stable wording: consumers
# (contract.py, core.py) rely on this exact string.
VERDICT_INELIGIBLE_REASON = (
    "channel mapping unconfirmed: suspected swap/crosstalk; confirm mapping "
    "or supply provider metadata"
)

# The reason the `run` / MCP run gate emits when it refuses a verdict for
# genuine CROSS-CHANNEL LEAKAGE (the principled, correlation-based signal: one
# channel carries a delayed COPY of the other, so the raw waveforms correlate).
# Distinct from VERDICT_INELIGIBLE_REASON: a leak is a physical audio defect, so
# the fix is the audio path, NOT confirming the channel mapping. Genuine leakage
# refuses the verdict (fail-closed, exit 2) on `run` and the MCP run tool.
CROSSTALK_VERDICT_REASON = (
    "cross-channel leakage detected: one channel carries a delayed copy of the "
    "other (the raw waveforms correlate), so the yield/hold verdict cannot be "
    "trusted; fix the audio path (echo cancellation / channel separation) "
    "before scoring"
)

# The non-fatal channel-mapping CAVEAT the possible-swap heuristic attaches.
# hotato does ADDRESSEE detection, NOT speaker-ID, so a channel SWAP cannot be
# reliably told from timing alone: a caller-dominant recording is an ordinary
# caller-led yield far more often than a swapped mapping. So a suspected swap
# does NOT refuse a verdict on `run` / the MCP run tool -- the recording STILL
# SCORES, and this structured caveat rides along (a machine reader gets it too)
# so the operator can confirm the mapping. --confirm-channels suppresses it.
CHANNEL_MAPPING_CAVEAT_REASON = (
    "channel mapping unconfirmed: caller-dominant timing could indicate a "
    "swapped caller/agent mapping"
)
CHANNEL_MAPPING_CAVEAT_HINT = (
    "pass --confirm-channels if the mapping is correct, or use a contract for a "
    "gated verdict"
)


def channel_mapping_caveat_block(swap_reason: str) -> dict:
    """The non-fatal channel-mapping caveat a suspected swap attaches to a
    still-scoring run: a stable structured dict (``reason`` / ``detail`` /
    ``hint``), so both the CLI text note and a machine reader carry the same
    advisory. ``detail`` is the swap heuristic's own per-role talk-time wording."""
    return {
        "reason": CHANNEL_MAPPING_CAVEAT_REASON,
        "detail": swap_reason,
        "hint": CHANNEL_MAPPING_CAVEAT_HINT,
    }


def crosstalk_verdict_suspected(coherence: float, leakage: dict, *, mode: str,
                                waveform_corr: Optional[float] = None) -> bool:
    """Is cross-channel crosstalk/leakage severe enough, at ``mode``'s VERDICT
    bar, to refuse a yield/hold verdict? ``coherence`` is the whole-clip echo
    cosine (mode-independent number); ``leakage`` is `_leakage_estimate`'s
    return dict. The dB-free, honest checks (the leak alters the receiver's
    activity mask, or crosses its VAD gate for a sustained run) are ALWAYS
    additive regardless of mode -- only the numeric coherence/leak-ratio bars
    vary by mode.

    ``waveform_corr`` (optional; the max normalized cross-correlation MAGNITUDE
    of the two RAW waveforms from ``_waveform_copy_corr``) CORROBORATES the
    envelope-coherence trigger, which alone reads high for ANY overlapping
    activity -- including two INDEPENDENT distinct speakers in genuine
    simultaneous speech (a real barge-in), which is not leakage. A genuine leak
    is one channel carrying a delayed COPY of the other's waveform, so the
    waveforms correlate; distinct speakers do not. When ``waveform_corr`` is
    supplied, the coherence trigger fires only if the waveforms ALSO correlate
    (>= ``WAVEFORM_LEAKAGE_MIN_CORR``), so a legitimate barge-in no longer refuses
    a verdict while genuine echo still does. The copy-ratio leakage paths
    (``leakage_alters_mask`` / ``leakage_crosses_gate`` / ``leakage_db``) are
    UNCHANGED -- a genuinely leaky recording the coherence bar would miss is still
    refused by them. When ``waveform_corr`` is None (no measurement supplied) the
    behavior is UNCHANGED: the coherence bar alone fires. For ``mode="scan"`` with
    no ``waveform_corr`` this is IDENTICAL to the existing
    ``crosstalk_risk.suspected`` (echo_suspected OR leakage_suspected)."""
    if mode not in VERDICT_MODES:
        raise ValueError(f"mode must be one of {VERDICT_MODES!r}; got {mode!r}.")
    if coherence >= VERDICT_COHERENCE_THRESHOLD[mode]:
        # Distinct-speaker overlap has high envelope coherence but near-zero
        # waveform correlation, so it does NOT clear this corroboration; a real
        # delayed copy (leakage/echo) does. No measurement -> unchanged behavior.
        if waveform_corr is None or waveform_corr >= WAVEFORM_LEAKAGE_MIN_CORR:
            return True
    if leakage.get("leakage_alters_mask") or leakage.get("leakage_crosses_gate"):
        return True
    db = leakage.get("leakage_db")
    return db is not None and db >= VERDICT_LEAKAGE_DB[mode]


def _waveform_copy_corr(caller, agent, sample_rate: int, env_lag_sec: float,
                        hop_sec: float, caller_active: List[bool],
                        agent_active: List[bool],
                        max_lag_sec: float = LEAKAGE_MAX_LAG_SEC) -> float:
    """Max normalized cross-correlation MAGNITUDE of the two RAW waveforms
    (``caller[i]`` against ``agent[i - lag]`` over lags 0..``max_lag``): ~1.0 when
    one channel carries an actual delayed COPY of the other's waveform (genuine
    cross-channel leakage/echo), ~0 for independent sources (two distinct
    speakers whose active windows merely overlap). This is the sample-level
    corroboration the whole-clip RMS-ENVELOPE coherence lacks -- the envelope
    cosine reads high for any overlapping activity, copy or not.

    Deterministic and network-free. numpy-accelerated (full-range FFT
    cross-correlation) when numpy is importable; otherwise a bounded pure-Python
    fallback that correlates a capped span, anchored at the first both-active
    region, over a tight lag window around ``env_lag_sec`` (the envelope's best
    lag localizes the true delay to within ~half a hop). Both paths separate a
    copy (~1.0) from independent speech (~0) by a wide margin."""
    from ._engine.audio import _np

    n = min(len(caller), len(agent))
    if n == 0:
        return 0.0
    max_lag = min(int(round(max_lag_sec * sample_rate)), n - 1)
    if max_lag < 0:
        return 0.0
    if _np is not None:
        c = _np.asarray(caller[:n], dtype=float)
        a = _np.asarray(agent[:n], dtype=float)
        nc = float(_np.sqrt(_np.dot(c, c)))
        na = float(_np.sqrt(_np.dot(a, a)))
        if nc <= 0.0 or na <= 0.0:
            return 0.0
        size = 1
        while size < 2 * n:
            size *= 2
        # corr[k] = sum_i c[i] * a[i - k] (caller lagging agent by k); zero-pad to
        # >= 2n so the negative-index wrap lands in the zero pad (no contamination).
        corr = _np.fft.irfft(_np.fft.rfft(c, size) * _np.conj(_np.fft.rfft(a, size)),
                             size)
        return float(_np.abs(corr[:max_lag + 1]).max() / (nc * na))
    return _waveform_copy_corr_py(
        caller, agent, n, sample_rate, max_lag, env_lag_sec, hop_sec,
        caller_active, agent_active,
    )


def _waveform_copy_corr_py(caller, agent, n: int, sample_rate: int, max_lag: int,
                           env_lag_sec: float, hop_sec: float,
                           caller_active: List[bool],
                           agent_active: List[bool]) -> float:
    """stdlib fallback for ``_waveform_copy_corr`` (no numpy): a bounded, windowed
    sample-level cross-correlation. Correlates at most ``_WF_MAX_SPAN_SEC`` of
    audio from the first both-active frame (a copy relationship holds throughout
    its source's activity, so a bounded slice is representative and the cost is
    independent of recording length) over lags within +/- ``_WF_LAG_PAD_HOPS``
    hops of the envelope's best lag."""
    from operator import mul

    hop_samp = max(1, int(round(hop_sec * sample_rate)))
    nf = min(len(caller_active), len(agent_active))
    f0 = next((f for f in range(nf)
               if caller_active[f] and agent_active[f]), None)
    if f0 is None:
        return 0.0
    center = int(round((env_lag_sec or 0.0) * sample_rate))
    pad = hop_samp * _WF_LAG_PAD_HOPS
    lo = max(0, center - pad)
    hi = min(max_lag, center + pad)
    if hi < lo:
        return 0.0
    start = f0 * hop_samp
    end = min(n, start + int(_WF_MAX_SPAN_SEC * sample_rate))
    c = list(caller[start:end])
    m = len(c)
    if m == 0:
        return 0.0
    nc_sq = math.fsum(x * x for x in c)
    if nc_sq <= 0.0:
        return 0.0
    nc = math.sqrt(nc_sq)
    # agent context spanning [start - hi, end): a_ctx[t] == agent[start - hi + t]
    # (left-zero-padded when start < hi), so caller[start + i] (== c[i]) pairs with
    # agent[start + i - lag] == a_ctx[hi - lag + i].
    left = start - hi
    a_ctx = list(agent[max(0, left):end])
    if left < 0:
        a_ctx = [0.0] * (-left) + a_ctx
    best = 0.0
    for lag in range(lo, hi + 1):
        base = hi - lag
        seg = a_ctx[base:base + m]
        na_sq = math.fsum(x * x for x in seg)
        if na_sq <= 0.0:
            continue
        r = abs(math.fsum(map(mul, c, seg))) / (nc * math.sqrt(na_sq))
        if r > best:
            best = r
    return best


def _peak_and_clip(samples, clip_lin: float) -> Tuple[float, float]:
    """Return ``(peak, clipped_fraction)`` for one channel's samples in [-1, 1].

    ``peak`` is the largest absolute sample; ``clipped_fraction`` is the share of
    samples at or above ``clip_lin`` in magnitude. numpy is used when the samples
    are already an ndarray (the engine's optional acceleration path); otherwise
    the peak comes from the C-level ``max``/``min`` builtins and the per-sample
    count runs only when the peak is actually near full scale, so a clean
    recording is O(1) extra work beyond the peak.
    """
    from ._engine.audio import _np

    if _np is not None and isinstance(samples, _np.ndarray):
        if samples.size == 0:
            return 0.0, 0.0
        mag = _np.abs(samples)
        peak = float(mag.max())
        clipped = int(_np.count_nonzero(mag >= clip_lin))
        return peak, clipped / samples.size

    n = len(samples)
    if n == 0:
        return 0.0, 0.0
    hi = max(samples)
    lo = min(samples)
    peak = hi if hi >= -lo else -lo
    if peak < clip_lin:
        return peak, 0.0
    clipped = 0
    for x in samples:
        if x >= clip_lin or x <= -clip_lin:
            clipped += 1
    return peak, clipped / n


def _channels_identical(a, b) -> bool:
    """True when the two channels carry the exact same signal (a mono recording
    duplicated into two channels), so the tracks are not really separated.

    Deliberately EXACT identity only: a legitimately scorable two-channel call
    with heavy echo bleed has high cross-channel coherence but is NOT identical,
    and must stay scorable (its bleed is surfaced as crosstalk risk instead).
    Cheap probe first (spread indices reject a distinct pair immediately), full
    scan only when every probe matches.
    """
    n = len(a)
    if n != len(b):
        return False
    if n == 0:
        return True
    from ._engine.audio import _np

    if _np is not None and isinstance(a, _np.ndarray) and isinstance(b, _np.ndarray):
        return bool(_np.array_equal(a, b))
    step = max(1, n // 64)
    for i in range(0, n, step):
        if a[i] != b[i]:
            return False
    for i in range(n):
        if a[i] != b[i]:
            return False
    return True


def _peak_dbfs(peak: float) -> float:
    """Peak level in dBFS, floored so a silent channel is a finite number."""
    return round(20.0 * math.log10(peak if peak > 1e-6 else 1e-6), 1)


def _channel_block(channel: int, active: List[bool], hop: float) -> dict:
    active_sec = round(sum(active) * hop, 3)
    first = first_active_sec(active, hop, SPEECH_MIN_RUN_SEC)
    has_speech = first >= 0.0
    return {
        "channel": channel,
        "active_sec": active_sec,
        "first_speech_sec": round(first, 3) if has_speech else None,
        "has_speech": has_speech,
        "enough_activity": has_speech and active_sec >= MIN_ACTIVITY_SEC,
    }


def _median(values: List[float]) -> float:
    s = sorted(values)
    n = len(s)
    return s[n // 2] if n % 2 else 0.5 * (s[n // 2 - 1] + s[n // 2])


def _leak_crosses_receiver_gate(src, act_src, lag, leak_db, hop,
                                resid_noise_floor_db) -> bool:
    """Dynamic-leakage secondary check: would the leaked copy of ``src`` cross the
    RECEIVING channel's effective VAD activity gate for a sustained run?

    ``energy_vad`` marks a receiver frame active when its level clears
    ``max(noise_floor + rel_db, abs_gate_db)``. The leaked copy's absolute level on
    the receiver is predicted from the source level ``lag`` frames earlier plus the
    estimated (negative) bleed gain ``leak_db`` -- so this isolates the LEAK's own
    contribution and never counts the receiver's genuine speech. When that predicted
    level clears the gate (plus ``LEAKAGE_GATE_MARGIN_DB``) for a run at least
    ``SPEECH_MIN_RUN_SEC`` long (the run that opens a turn downstream), the leak would
    register as receiver activity and can move a measurement, so it is suspected.

    Returns ``False`` when the receiver noise floor is unknown (the check is skipped,
    never guessed) or no run clears the gate."""
    if resid_noise_floor_db is None:
        return False
    n = min(len(src), len(act_src))
    if n == 0 or lag >= n:
        return False
    gate = max(resid_noise_floor_db + _VAD_REL_DB, _VAD_ABS_GATE_DB) + LEAKAGE_GATE_MARGIN_DB
    min_run = max(1, int(round(SPEECH_MIN_RUN_SEC / hop)))
    src_db = to_dbfs(src[:n])
    run = 0
    for i in range(lag, n):
        if act_src[i - lag] and src_db[i - lag] + leak_db >= gate:
            run += 1
            if run >= min_run:
                return True
        else:
            run = 0
    return False


def _leak_alters_receiver_mask(resid, src, other_active, leak_db, lag_sec, hop,
                               vad_params) -> bool:
    """Would REMOVING the suspected delayed-attenuated leak change the receiving
    channel's VAD activity mask in a way that could move a downstream turn-taking
    measurement? This is the honest, dB-free replacement for a fixed leakage bar:
    the caution depends on whether the leaked component actually creates (or erases)
    activity frames the scorer would read, not on the leak's absolute or ratio dB.

    It reconstructs the receiver WITHOUT the leak by subtracting the predicted leak
    energy per frame -- ``resid_wo[i] = sqrt(max(0, resid[i]^2 - (g*src[i-lag])^2))``
    with ``g = 10**(leak_db/20)`` -- then runs the SAME ``energy_vad`` on the receiver
    with and without the leak and compares three things a timing verdict reads:

      * ONSET: the first sustained active run (moves a yield/response-gap measurement);
      * total active frames (adds/removes activity attributed to the receiver);
      * TALK-OVER: frames active while the OTHER channel is also active (overlap).

    Any change means the leak is verdict-changing, so caution is forced regardless of
    dB. Deterministic and stdlib-only (reuses the reference ``energy_vad`` /
    ``first_active_sec``). Returns ``False`` when there is no leak estimate or no VAD
    params (the check is skipped, never guessed)."""
    if leak_db is None or vad_params is None:
        return False
    n = min(len(resid), len(src), len(other_active))
    if n == 0:
        return False
    lag = int(round((lag_sec or 0.0) / hop))
    g = 10 ** (leak_db / 20.0)
    resid_wo = list(resid[:n])
    for i in range(lag, n):
        leaked = g * src[i - lag]
        e = resid[i] * resid[i] - leaked * leaked
        resid_wo[i] = math.sqrt(e) if e > 0.0 else 0.0
    mask_with = energy_vad(resid[:n], hop, vad_params).active
    mask_wo = energy_vad(resid_wo, hop, vad_params).active
    m = min(len(mask_with), len(mask_wo), n)
    on_with = first_active_sec(mask_with[:m], hop, SPEECH_MIN_RUN_SEC)
    on_wo = first_active_sec(mask_wo[:m], hop, SPEECH_MIN_RUN_SEC)
    if round(on_with, 3) != round(on_wo, 3):
        return True
    if sum(mask_with[:m]) != sum(mask_wo[:m]):
        return True
    to_with = sum(1 for i in range(m) if mask_with[i] and other_active[i])
    to_wo = sum(1 for i in range(m) if mask_wo[i] and other_active[i])
    return to_with != to_wo


def _copy_leak_one_direction(resid, src, act_resid, act_src, hop,
                             resid_noise_floor_db=None):
    """Estimate a delayed, attenuated COPY of ``src`` present on the ``resid``
    channel (leakage), robust to the copy being loud enough to re-trigger the
    residual channel's own VAD.

    Over frames where the residual channel is active AND the source was active
    ``lag`` frames earlier, the per-frame level ratio ``resid/src`` (in dB) of a
    true leak clusters tightly at the (negative) bleed gain -- a leak is one
    scaled delayed copy of a single source, so every leaked frame shares the same
    ratio. Independent speech on the residual channel does not: its level relative
    to the source varies frame to frame. The lag whose ratio cluster is tightest
    is returned as ``(leak_db, consistency, coverage, lag_sec, crosses_gate)``, where
    ``consistency`` is the fraction of qualifying frames within ``LEAKAGE_TOL_DB``
    of the cluster median, ``coverage`` is the fraction of the source channel's
    active frames the copy spans (a real leak shadows its source and so spans
    nearly all of them; a brief coincidental overlap spans few), and ``crosses_gate``
    is the dynamic secondary check (``_leak_crosses_receiver_gate``): whether the
    leaked copy at the winning lag would clear the receiving channel's VAD activity
    gate for a sustained run (``False`` when ``resid_noise_floor_db`` is not given).

    Returns ``(None, None, 0.0, None, False)`` when no lag has ``LEAKAGE_MIN_FRAMES``
    qualifying frames -- an undefined estimate, never a fabricated leak."""
    n = min(len(resid), len(src), len(act_resid), len(act_src))
    if n == 0:
        return None, None, 0.0, None, False
    peak_src = max(src[:n], default=0.0)
    peak_resid = max(resid[:n], default=0.0)
    src_active = sum(1 for i in range(n) if act_src[i]) or 1
    # Per-channel energy floors so the framing floor of a near-silent stretch does
    # not enter the ratio (it would fabricate a spurious constant ratio).
    floor_src = peak_src * (10 ** (-60.0 / 20.0))
    floor_resid = peak_resid * (10 ** (-70.0 / 20.0))
    max_lag = min(max(0, int(round(LEAKAGE_MAX_LAG_SEC / hop))), max(0, n - 1))
    best = (None, None, 0.0, None)
    best_key = (-1.0, -1)
    best_lag_frames = 0
    for lag in range(0, max_lag + 1):
        ratios = []
        for i in range(lag, n):
            if not act_resid[i]:
                continue
            s = src[i - lag]
            if not act_src[i - lag] or s <= floor_src:
                continue
            r = resid[i]
            if r <= floor_resid:
                continue
            ratios.append(20.0 * math.log10(r / s))
        if len(ratios) < LEAKAGE_MIN_FRAMES:
            continue
        med = _median(ratios)
        consistency = sum(1 for x in ratios if abs(x - med) <= LEAKAGE_TOL_DB) / len(ratios)
        coverage = len(ratios) / src_active
        key = (round(consistency, 4), len(ratios))
        if key > best_key:
            best_key = key
            best_lag_frames = lag
            best = (round(med, 1), round(consistency, 3),
                    round(coverage, 3), round(lag * hop, 3))
    # Dynamic secondary check on the winning lag: does the leaked copy actually clear
    # the receiver's VAD gate for a sustained run? (Uses the UNROUNDED work above via
    # best_lag_frames + the reported median gain.)
    crosses_gate = _leak_crosses_receiver_gate(
        src, act_src, best_lag_frames, best[0], hop, resid_noise_floor_db
    ) if best[0] is not None else False
    return (*best, crosses_gate)


def _leakage_estimate(rms_c, rms_a, caller_active, agent_active, hop,
                      caller_noise_floor_db=None, agent_noise_floor_db=None,
                      caller_vad_params=None, agent_vad_params=None) -> dict:
    """Cross-channel leakage (bleed): the level of the loudest consistent,
    attenuated delayed COPY of one channel found on the other, and whether it is
    loud enough to be mistaken for the other party's activity downstream.

    Both directions are measured -- an agent copy leaking onto the caller channel
    (the dangerous direction: leaked agent audio read as caller activity) and the
    reverse. The loudest consistent copy is reported. ``leakage_suspected`` is set
    when the leak actually ALTERS the receiving channel's activity mask (the honest,
    dB-free test ``_leak_alters_receiver_mask``: removing the leak would move the
    onset, the total active frames, or the talk-over overlap the scorer reads) OR --
    kept as additive safety nets, never as the sole gate -- when the copy is at or
    above the fixed ``LEAKAGE_WARN_DB`` ratio OR would cross the RECEIVING channel's
    VAD gate for a sustained run (``crosses_gate``). A copy that does none of these
    is reported (for transparency) but not flagged; no consistent copy at all reports
    ``leakage_db = None``. The mask test closes the ~6-11 dB gap a fixed bar left:
    a verdict-changing leak below the fixed bar is now flagged because it changes the
    activity the measurement depends on, whatever its absolute dB."""
    # For agent->caller the receiver is the caller channel, so its noise floor gates
    # the dynamic check; for caller->agent the receiver is the agent channel.
    ac = _copy_leak_one_direction(rms_c, rms_a, caller_active, agent_active, hop,
                                  caller_noise_floor_db)
    ca = _copy_leak_one_direction(rms_a, rms_c, agent_active, caller_active, hop,
                                  agent_noise_floor_db)
    candidates = []
    for (db, cons, coverage, lag, crosses), direction in (
        (ac, "agent_into_caller"),
        (ca, "caller_into_agent"),
    ):
        if (db is not None and cons is not None
                and cons >= LEAKAGE_CONSISTENCY
                and coverage >= LEAKAGE_MIN_COVERAGE
                and LEAKAGE_REPORT_DB <= db <= LEAKAGE_ATTEN_MAX_DB):
            # Mask-alteration test on the RECEIVING channel for this direction: the
            # leak is verdict-changing when removing it would move the receiver's
            # activity mask (onset / total / talk-over). agent->caller receives on the
            # caller channel (other party = agent); caller->agent is the reverse.
            if direction == "agent_into_caller":
                alters = _leak_alters_receiver_mask(
                    rms_c, rms_a, agent_active, db, lag, hop, caller_vad_params)
            else:
                alters = _leak_alters_receiver_mask(
                    rms_a, rms_c, caller_active, db, lag, hop, agent_vad_params)
            candidates.append((db, cons, lag, direction, crosses, alters))
    if not candidates:
        return {"leakage_db": None, "leakage_direction": None,
                "leakage_lag_sec": None, "leakage_suspected": False,
                "leakage_alters_mask": False, "leakage_crosses_gate": False}
    # The loudest consistent copy (highest dB = most leaked energy) is the worst.
    db, cons, lag, direction, crosses, alters = max(candidates, key=lambda t: t[0])
    return {
        "leakage_db": db,
        "leakage_direction": direction,
        "leakage_lag_sec": lag,
        # PRIMARY: the leak actually alters the receiver's activity mask (dB-free).
        # The fixed -40 dB ratio bar and the gate-crossing rule are kept as additive
        # safety nets -- they only ADD suspected cases, never remove one.
        "leakage_suspected": alters or db >= LEAKAGE_WARN_DB or crosses,
        # The two dB-free/gate signals broken out separately (additive to every
        # mode's VERDICT bar in crosstalk_verdict_suspected -- see K6 above).
        "leakage_alters_mask": bool(alters),
        "leakage_crosses_gate": bool(crosses),
    }


def trust_report(
    path: str,
    *,
    caller_channel: int = 0,
    agent_channel: int = 1,
    cfg: Optional[ScoreConfig] = None,
    diarize: bool = False,
    diarizer: str = "pyannote",
    egress_opt_in: bool = False,
    mode: str = VERDICT_MODE_SCAN,
    channel_map_confirmed: bool = False,
) -> dict:
    """Inspect one recording and return the input-health report.

    Reads the WAV through the same hardened reader ``run`` uses (a corrupt,
    empty, truncated, or non-WAV file raises the usual ValueError -> the CLI's
    exit-2 usage error), then frames and VADs each channel with the reference
    engine so the activity numbers line up with what ``scan`` / ``run`` would
    see. A mono file, identical channels, or a silent required channel are NOT
    raised: they are reported as ``scorable: false`` with the reason and the
    next step, because "is this scorable?" is exactly the question asked.

    ``exit_code`` in the returned dict is 0 when the recording is eligible for
    scan and 2 when it is not, matching the CLI's unusable-input convention.

    ``candidate_eligible`` mirrors ``scorable``: the audio is usable enough for
    ``scan`` to surface advisory candidates + audio for human review.
    ``verdict_eligible`` is the NARROWER K6 gate: ``False`` (with
    ``verdict_ineligible_reason`` set) when a possible channel swap or
    crosstalk/leakage at or above ``mode``'s verdict threshold means a
    yield/hold verdict cannot be trusted, even though the input stayed
    candidate-eligible. ``mode="contract"`` applies a STRICTER crosstalk bar
    than the default ``mode="scan"`` (see ``VERDICT_COHERENCE_THRESHOLD`` /
    ``VERDICT_LEAKAGE_DB``), for contract/CI consumers where a false-confident
    pass is more costly than an advisory scan caution. ``channel_map_confirmed``
    is a HUMAN explicit confirmation (or the caller having verified authenticated
    provider metadata) that the caller/agent channel mapping is correct despite
    the heuristic flag; it flips ``verdict_eligible`` back on (never overrides
    the not-scorable gate -- an unusable input still cannot carry a verdict).
    """
    from .core import _read_wav  # hardened WAV reader; reused, never reimplemented

    if mode not in VERDICT_MODES:
        raise ValueError(f"mode must be one of {VERDICT_MODES!r}; got {mode!r}.")
    if cfg is None:
        cfg = ScoreConfig()

    signal = _read_wav(path)
    source = os.path.basename(path)
    duration = round(signal.num_samples / signal.sample_rate, 3)

    base = {
        "tool": "hotato",
        "kind": "input-health",
        "schema_version": "1",
        "source": source,
        "note": NOTE,
        "recording": {
            "sample_rate": signal.sample_rate,
            "duration_sec": duration,
            "channels": signal.num_channels,
        },
    }

    # A single-channel recording cannot separate caller from agent on its own.
    # DEFAULT (no --diarize): report it as not scorable, byte-identical to before.
    # With --diarize: run the opt-in diarizer and report whether the mono is
    # confidently SEPARABLE (a tier), still WITHOUT emitting any turn-taking
    # verdict -- trust only answers "is this scorable?", now including "is this
    # mono file confidently separable?" and hands the tier to run/scan.
    if signal.num_channels < 2:
        if diarize:
            return _diarize_trust(
                base, signal, diarizer=diarizer, egress_opt_in=egress_opt_in, cfg=cfg,
                mode=mode, channel_map_confirmed=channel_map_confirmed,
            )
        return _finalize(
            base,
            channels=None,
            crosstalk=None,
            scorability={
                "separated_tracks": False,
                "enough_caller_activity": False,
                "enough_agent_activity": False,
            },
            warnings=[],
            scorable=False,
            reason=(
                "the recording has a single channel, so the caller and the agent "
                "cannot be told apart"
            ),
            next_step=NEXT_STEP_DUAL_CHANNEL,
            verdict_eligible=False,
            mode=mode,
            channel_map_confirmed=channel_map_confirmed,
        )

    # A real two-channel file: bad channel flags are a usage error (exit 2),
    # exactly as core/scan treat them.
    for role, idx in (("caller", caller_channel), ("agent", agent_channel)):
        if idx < 0 or idx >= signal.num_channels:
            raise ChannelRangeError(
                f"--{role}-channel {idx} is out of range for a "
                f"{signal.num_channels}-channel recording "
                f"(valid channels: 0..{signal.num_channels - 1})."
            )
    if caller_channel == agent_channel:
        raise ValueError(
            f"--caller-channel and --agent-channel must be different (both are "
            f"{caller_channel}); pass distinct channels for a 2-channel recording "
            "(the caller and the agent are on separate channels)."
        )

    caller_samples = signal.get(caller_channel)
    agent_samples = signal.get(agent_channel)

    # Reference framing + VAD: identical to scan/core, so the activity numbers
    # match what the scorer would measure on the same file.
    rms_c, hop = frame_rms(caller_samples, signal.sample_rate, cfg.frame_ms, cfg.hop_ms)
    rms_a, _ = frame_rms(agent_samples, signal.sample_rate, cfg.frame_ms, cfg.hop_ms)
    caller_vad = energy_vad(rms_c, hop, cfg.caller_vad)
    agent_vad = energy_vad(rms_a, hop, cfg.agent_vad)
    caller_active = caller_vad.active
    agent_active = agent_vad.active
    n = min(len(caller_active), len(agent_active))
    caller_active, agent_active = caller_active[:n], agent_active[:n]

    caller = _channel_block(caller_channel, caller_active, hop)
    agent = _channel_block(agent_channel, agent_active, hop)

    # Clipping, per channel (peak level + full-scale fraction).
    c_peak, c_clip = _peak_and_clip(caller_samples, CLIP_PEAK_LIN)
    a_peak, a_clip = _peak_and_clip(agent_samples, CLIP_PEAK_LIN)
    base["recording"]["clipping"] = {
        "caller": _clip_block(c_peak, c_clip),
        "agent": _clip_block(a_peak, a_clip),
    }

    # Leading silence: dead air before the first speech on either channel.
    onsets = [b["first_speech_sec"] for b in (caller, agent)
              if b["first_speech_sec"] is not None]
    leading_silence = round(min(onsets), 3) if onsets else duration
    base["recording"]["leading_silence_sec"] = leading_silence

    # Possible channel swap: the channel mapped as the caller dominates talk time
    # over the channel mapped as the agent (the reverse of the usual pattern).
    swap = (
        caller["active_sec"] > SWAP_DOMINANCE_RATIO * agent["active_sec"]
        and caller["active_sec"] - agent["active_sec"] >= SWAP_ABS_MARGIN_SEC
    )
    channels = {
        "caller": caller,
        "agent": agent,
        "possible_swap": swap,
        "swap_reason": (
            f"channel {caller_channel} (mapped as caller) holds the floor "
            f"{caller['active_sec']}s vs {agent['active_sec']}s on channel "
            f"{agent_channel} (mapped as agent); an agent usually holds the "
            "floor longer, so the caller/agent channels may be reversed"
        ) if swap else None,
    }

    # Crosstalk risk: cross-channel echo coherence (caller carrying a delayed copy
    # of the agent's own audio). A warning, never a not-scorable condition.
    crosstalk = echo_signal(rms_c[:n], rms_a[:n], hop)
    # Leakage (bleed) estimate: catches the symmetric bleed the whole-clip
    # coherence dilutes away. Loud leakage is loud enough that leaked audio can be
    # read as the other party's activity and corrupt a downstream timing verdict.
    leakage = _leakage_estimate(
        rms_c[:n], rms_a[:n], caller_active, agent_active, hop,
        caller_noise_floor_db=caller_vad.noise_floor_db,
        agent_noise_floor_db=agent_vad.noise_floor_db,
        caller_vad_params=cfg.caller_vad,
        agent_vad_params=cfg.agent_vad,
    )
    crosstalk_out = {
        "coherence": crosstalk["coherence"],
        "lag_sec": crosstalk["lag_sec"],
        # suspected is set by EITHER the coherence bar OR a loud consistent leak;
        # the whole-clip cosine and the copy-ratio cluster catch different regimes.
        "suspected": crosstalk["echo_suspected"] or leakage["leakage_suspected"],
        "leakage_db": leakage["leakage_db"],
        "leakage_direction": leakage["leakage_direction"],
    }
    # K6 VERDICT-level crosstalk suspicion, at mode's (scan vs contract) bar --
    # independent of the caution/recommendation wording above, which stays on the
    # existing scan-level bars. For mode="scan" this is identical to
    # crosstalk_out["suspected"]; mode="contract" can trip where scan would not.
    #
    # The envelope coherence alone reads high for ANY overlapping activity,
    # including two INDEPENDENT distinct speakers in a genuine barge-in, which is
    # not leakage. Only when the coherence reaches this mode's bar do we pay for
    # the sample-level waveform corroboration that tells a real delayed COPY
    # (leakage/echo -> waveforms correlate) apart from distinct-speaker overlap
    # (independent waveforms), so the verdict refuses ONLY on real leakage. Below
    # the bar the coherence path cannot fire anyway, so we skip the work and the
    # report stays byte-identical for a clean call.
    waveform_corr = None
    if crosstalk["coherence"] >= VERDICT_COHERENCE_THRESHOLD[mode]:
        waveform_corr = _waveform_copy_corr(
            caller_samples, agent_samples, signal.sample_rate,
            crosstalk["lag_sec"], hop, caller_active, agent_active,
        )
    crosstalk_verdict_hit = crosstalk_verdict_suspected(
        crosstalk["coherence"], leakage, mode=mode, waveform_corr=waveform_corr,
    )

    # Scorability: the three things a real score needs.
    separated = not _channels_identical(caller_samples, agent_samples)
    scorability = {
        "separated_tracks": separated,
        "enough_caller_activity": caller["enough_activity"],
        "enough_agent_activity": agent["enough_activity"],
    }

    # Non-blocking warnings (informational; they never change scorability).
    warnings: List[str] = []
    if base["recording"]["clipping"]["caller"]["clipped"]:
        warnings.append(
            f"caller channel is clipping ({c_clip * 100:.1f}% of samples at full "
            "scale); the recording was captured too hot"
        )
    if base["recording"]["clipping"]["agent"]["clipped"]:
        warnings.append(
            f"agent channel is clipping ({a_clip * 100:.1f}% of samples at full "
            "scale); the recording was captured too hot"
        )
    if leading_silence >= LEADING_SILENCE_WARN_SEC:
        warnings.append(
            f"{leading_silence:.1f}s of leading silence before any speech; check "
            "the capture start / trigger"
        )
    if crosstalk["echo_suspected"]:
        warnings.append(
            f"crosstalk risk: cross-channel coherence {crosstalk_out['coherence']} "
            f"at {crosstalk_out['lag_sec']}s lag; the caller channel may be "
            "carrying the agent's own audio (echo bleed), so a scan may see the "
            "agent hearing itself"
        )
    # VERDICT-CHANGING warnings force the caution headline. Each appends its warning
    # AND a caution reason; the reasons are composed into one "scan with caution: ..."
    # recommendation in _finalize. A verdict-changing warning is one that could move
    # what a downstream turn-taking measurement reads: cross-channel leakage (leaked
    # audio counted as the other party), a very low signal level (timing under-
    # measured), and a possible channel swap (caller/agent reversed). Clipping and
    # leading silence stay INFORMATIONAL warnings above -- a hot capture or a late
    # trigger does not, on its own, change the measured timing -- so they do not
    # caution. Cross-channel echo coherence surfaces via crosstalk_risk.suspected and
    # only cautions when it is also leakage-suspected (handled here), unchanged.
    caution_reasons: List[str] = []
    # Cross-channel leakage: the copy-ratio cluster the whole-clip coherence can miss.
    if leakage["leakage_suspected"]:
        _dir = ("agent audio on the caller channel"
                if leakage["leakage_direction"] == "agent_into_caller"
                else "caller audio on the agent channel")
        leak_msg = (
            f"cross-channel leakage: {_dir} sits at {leakage['leakage_db']} dB, a "
            f"consistent delayed copy (lag {leakage['leakage_lag_sec']}s); leaked "
            "audio at this level can be counted as the other party's activity, so "
            "a downstream timing measurement may be wrong even though the tracks "
            "are separated"
        )
        warnings.append(leak_msg)
        caution_reasons.append(leak_msg)
    # Low input level: quiet enough that timing may be under-measured downstream
    # while every scorability gate still passes.
    loudest_peak_dbfs = max(_peak_dbfs(c_peak), _peak_dbfs(a_peak))
    if loudest_peak_dbfs < LOW_SIGNAL_WARN_DBFS:
        low_msg = (
            f"signal level very low (loudest channel peaks at "
            f"{loudest_peak_dbfs:.1f} dBFS); timing may be underestimated -- "
            "re-capture at a higher input level for an exact measurement"
        )
        warnings.append(low_msg)
        caution_reasons.append(low_msg)
    # Possible channel swap: the mapping may be reversed, which flips every per-role
    # timing measurement downstream.
    if swap:
        warnings.append(channels["swap_reason"])
        caution_reasons.append(channels["swap_reason"])
    caution: Optional[str] = (
        f"{CAUTION_RECOMMENDATION}: {'; '.join(caution_reasons)}"
        if caution_reasons else None
    )

    # Not-scorable gate, first matching reason wins. Ordered from the most
    # fundamental input defect outward.
    reason = None
    next_step = None
    if not separated:
        reason = (
            "the two channels carry the same signal (a mono recording duplicated "
            "into two channels), so caller and agent cannot be separated"
        )
        next_step = NEXT_STEP_DUAL_CHANNEL
    elif not caller["has_speech"]:
        reason = "caller channel has no detected speech"
        next_step = NEXT_STEP_CHANNEL_MAP
    elif not agent["has_speech"]:
        reason = "agent channel has no detected speech"
        next_step = NEXT_STEP_CHANNEL_MAP
    elif not caller["enough_activity"]:
        reason = (
            f"caller channel has only {caller['active_sec']}s of detected speech "
            f"(need at least {MIN_ACTIVITY_SEC}s to score)"
        )
        next_step = NEXT_STEP_CHANNEL_MAP
    elif not agent["enough_activity"]:
        reason = (
            f"agent channel has only {agent['active_sec']}s of detected speech "
            f"(need at least {MIN_ACTIVITY_SEC}s to score)"
        )
        next_step = NEXT_STEP_CHANNEL_MAP

    candidate_eligible = reason is None
    # K6: verdict eligibility is a NARROWER, separate gate from candidate
    # eligibility. A suspected swap or verdict-level crosstalk/leakage refuses a
    # verdict even though the input stays candidate-eligible (advisory scan
    # candidates + audio are still surfaced). It never overrides the not-scorable
    # gate.
    #
    # An explicit human channel-map confirmation (or the caller's own
    # authenticated provider metadata) resolves ONLY the channel-SWAP conflict --
    # confirming the mapping is correct answers exactly the "are the roles
    # reversed?" question and nothing else. It does NOT clear a crosstalk/leakage
    # verdict hit: a recording whose channels are correctly mapped but still bleed
    # into each other can misattribute one party's audio to the other, so a
    # downstream timing verdict stays untrustworthy and must remain refused.
    swap_conflict = swap and not channel_map_confirmed
    verdict_conflict = swap_conflict or crosstalk_verdict_hit
    if not candidate_eligible:
        verdict_eligible = False
        verdict_ineligible_reason = None  # not_scorable_reason already explains
    elif verdict_conflict:
        verdict_eligible = False
        verdict_ineligible_reason = VERDICT_INELIGIBLE_REASON
    else:
        verdict_eligible = True
        verdict_ineligible_reason = None

    # The two K6 signals, exposed SEPARATELY so a verdict consumer can apply the
    # right policy to each (``verdict_eligible`` above conflates them for the
    # contract/CI gate, which refuses on either). The `run` / MCP run gate keeps
    # them apart: a genuine cross-channel LEAK refuses the verdict, but a suspected
    # SWAP (unreliable from timing -- addressee detection, not speaker-ID) does not,
    # it only cautions.
    #  * ``crosstalk_verdict_refused``: the principled, correlation-based leakage
    #    signal at this mode's bar (independent of channel-map confirmation -- a
    #    physical leak is not fixed by confirming the mapping).
    #  * ``channel_mapping_caveat``: the NON-FATAL swap caveat (a structured dict
    #    or None), present only when the recording is candidate-eligible, a swap is
    #    suspected, and the mapping was not confirmed.
    channel_mapping_caveat = (
        channel_mapping_caveat_block(channels["swap_reason"])
        if (candidate_eligible and swap and not channel_map_confirmed)
        else None
    )

    return _finalize(
        base,
        channels=channels,
        crosstalk=crosstalk_out,
        scorability=scorability,
        warnings=warnings,
        scorable=candidate_eligible,
        reason=reason,
        next_step=next_step,
        caution=caution,
        verdict_eligible=verdict_eligible,
        verdict_ineligible_reason=verdict_ineligible_reason,
        mode=mode,
        channel_map_confirmed=channel_map_confirmed,
        crosstalk_verdict_refused=crosstalk_verdict_hit,
        channel_mapping_caveat=channel_mapping_caveat,
    )


def _clip_block(peak: float, clipped_fraction: float) -> dict:
    return {
        "peak": round(peak, 4),
        "peak_dbfs": _peak_dbfs(peak),
        "clipped_fraction": round(clipped_fraction, 6),
        "clipped": clipped_fraction >= CLIP_WARN_FRACTION,
    }


def _finalize(base: dict, *, channels, crosstalk, scorability, warnings,
              scorable: bool, reason, next_step, caution=None,
              verdict_eligible: bool = True, verdict_ineligible_reason=None,
              mode: str = VERDICT_MODE_SCAN, channel_map_confirmed: bool = False,
              crosstalk_verdict_refused: bool = False,
              channel_mapping_caveat=None) -> dict:
    """Attach the channel/crosstalk/scorability blocks and the recommendation,
    and set the process exit code (0 scorable, 2 not).

    ``caution`` downgrades the recommendation of a still-scorable recording OFF
    "eligible for scan" (a verdict-changing warning -- leakage, low signal, or a possible
    channel swap -- can corrupt a downstream timing measurement) WITHOUT changing
    scorability or the exit code -- ``scorable`` and the not-scorable gate are
    untouched, only the human-facing recommendation is.

    ``input_health`` is an explicit 3-state summary of that same axis, additive to
    the report: "clean" (scorable, no verdict-changing warning), "caution" (scorable
    but a verdict-changing warning is present, i.e. ``caution`` is set), or
    "not_scorable" (a gate failed). It restates in one field what a machine reader
    would otherwise have to infer from ``scorable`` + the recommendation prefix.

    ``verdict_eligible`` / ``verdict_ineligible_reason`` (K6): a NARROWER gate
    than ``scorable``, additive to the report -- see ``trust_report``'s
    docstring. ``candidate_eligible`` mirrors ``scorable`` under its K6 name.
    """
    if channels is not None:
        base["channels"] = channels
    if crosstalk is not None:
        base["crosstalk_risk"] = crosstalk
    base["scorability"] = scorability
    base["warnings"] = warnings
    base["scorable"] = scorable
    base["candidate_eligible"] = scorable
    base["verdict_eligible"] = verdict_eligible
    base["verdict_ineligible_reason"] = verdict_ineligible_reason
    # K6 (additive, separated signals -- see trust_report): the correlation-based
    # leakage refusal, and the non-fatal channel-mapping (swap) caveat.
    base["crosstalk_verdict_refused"] = bool(crosstalk_verdict_refused)
    base["channel_mapping_caveat"] = channel_mapping_caveat
    base["verdict_mode"] = mode
    base["channel_map_confirmed"] = bool(channel_map_confirmed)
    if scorable:
        base["recommendation"] = caution if caution else SAFE_RECOMMENDATION
        base["input_health"] = "caution" if caution else "clean"
        base["not_scorable_reason"] = None
        base["next_step"] = None
        base["exit_code"] = 0
    else:
        base["recommendation"] = f"NOT SCORABLE: {reason}"
        base["input_health"] = "not_scorable"
        base["not_scorable_reason"] = reason
        base["next_step"] = next_step
        base["exit_code"] = 2
    return base


def _diarize_trust(base: dict, signal, *, diarizer: str, egress_opt_in: bool,
                   cfg: ScoreConfig, mode: str = VERDICT_MODE_SCAN,
                   channel_map_confirmed: bool = False) -> dict:
    """The --diarize path for a mono file: run the opt-in diarizer and report the
    separation confidence tier, WITHOUT scoring. A refused (non-separable) file is
    not scorable (exit 2); high/low tiers are scorable-via-diarized-mono with the
    tier and ``indicative_only`` carried, so the caller knows a below-bar verdict
    is only indicative. A missing extra/token/model raises BackendUnavailable (the
    CLI's clean exit-2), never a raw-mono guess. Still never a turn-taking verdict."""
    from .diarize import prepare_diarized_mono

    dm = prepare_diarized_mono(
        signal.get(0), signal.sample_rate,
        backend=diarizer, num_speakers=2, egress_opt_in=egress_opt_in, cfg=cfg,
    )
    sep = dm.separation
    tier = dm.tier
    base["scorability"] = {"separation": sep}
    base["diarization"] = {
        "backend": diarizer,
        "speaker_map": dm.speaker_map,
        "confidence_tier": tier,
        "separation_confidence": sep["separation_confidence"],
    }
    base["warnings"] = []
    base["confidence_tier"] = tier
    base["indicative_only"] = dm.indicative_only
    if tier == "refuse":
        base["scorable"] = False
        base["recommendation"] = f"NOT SCORABLE: {dm.not_scorable_reason}"
        base["input_health"] = "not_scorable"
        base["not_scorable_reason"] = dm.not_scorable_reason
        base["next_step"] = (
            "record a dual-channel call (caller and agent on separate channels) "
            "for a confident verdict"
        )
        base["exit_code"] = 2
    else:
        label = ("high confidence" if tier == "high"
                 else "indicative only, below the confidence bar")
        base["scorable"] = True
        base["recommendation"] = (
            f"SCORABLE via diarized-mono ({label}); score with: "
            "hotato run --mono FILE --diarize"
        )
        # "clean" scorable; a below-bar tier carries indicative_only, not a
        # verdict-changing input-health warning, so the input itself reads clean.
        base["input_health"] = "clean"
        base["not_scorable_reason"] = None
        base["next_step"] = None
        base["exit_code"] = 0
    # K6: the diarized-mono path has no caller/agent channel-swap concept (its
    # own separation-confidence tier + indicative_only already carry the honest
    # confidence signal), so verdict eligibility simply mirrors scorability here.
    base["candidate_eligible"] = base["scorable"]
    base["verdict_eligible"] = base["scorable"]
    base["verdict_ineligible_reason"] = None
    # The diarized-mono path has no caller/agent channel-swap concept and does not
    # compute cross-channel leakage, so both K6 signals are inert here.
    base["crosstalk_verdict_refused"] = False
    base["channel_mapping_caveat"] = None
    base["verdict_mode"] = mode
    base["channel_map_confirmed"] = bool(channel_map_confirmed)
    return base


def render_text(report: dict) -> str:
    """A compact human summary of the input-health report."""
    rec = report["recording"]
    lines = [
        f"hotato trust: {report['source']}",
        f"  recording: {rec['duration_sec']:.1f}s, {rec['sample_rate']} Hz, "
        f"{rec['channels']} channel{'s' if rec['channels'] != 1 else ''}",
    ]
    ch = report.get("channels")
    if ch is not None:
        clip = rec.get("clipping", {})
        for role in ("caller", "agent"):
            b = ch[role]
            first = ("-" if b["first_speech_sec"] is None
                     else f"first at {b['first_speech_sec']:.2f}s")
            cl = clip.get(role, {})
            hot = (f", CLIPPING {cl['clipped_fraction'] * 100:.1f}%"
                   if cl.get("clipped") else "")
            lines.append(
                f"  {role:<6} (ch{b['channel']}): {b['active_sec']:.2f}s speech, "
                f"{first}, peak {cl.get('peak_dbfs', 0.0):.1f} dBFS{hot}"
            )
        lines.append(f"  leading silence: {rec['leading_silence_sec']:.2f}s")
        ct = report["crosstalk_risk"]
        # Tag coherence by its OWN bar, not the combined suspicion: a loud leak can
        # set crosstalk_risk.suspected while the whole-clip coherence stays low.
        coh_tag = ("HIGH" if ct["coherence"] >= DEFAULT_COHERENCE_THRESHOLD
                   else "low")
        lines.append(
            f"  crosstalk: coherence {ct['coherence']} ({coh_tag}) at "
            f"{ct['lag_sec']}s lag"
        )
        if ct.get("leakage_db") is not None:
            direction = ("agent->caller" if ct.get("leakage_direction") == "agent_into_caller"
                         else "caller->agent")
            leak_tag = "HIGH" if ct["suspected"] else "low"
            lines.append(
                f"  leakage: {ct['leakage_db']} dB ({direction}, {leak_tag})"
            )
        if ch["possible_swap"]:
            lines.append(f"  possible channel swap: {ch['swap_reason']}")
    sc = report["scorability"]
    if "separation" in sc:
        # The --diarize (mono-scorability) path: report the separation tier, not
        # the two-channel separated/activity checks.
        sep = sc["separation"]
        sg = sep.get("signals", {})
        lines.append(
            f"  separation: tier {sep['confidence_tier']} "
            f"(confidence {sep['separation_confidence']}, backend {sep['backend']}); "
            f"{sg.get('speaker_count', '?')} speakers, overlap "
            f"{sg.get('overlap_ratio', '?')}"
        )
        dz = report.get("diarization", {})
        sm = dz.get("speaker_map", {})
        if sm:
            lines.append(
                f"  speaker map: caller={sm.get('caller')} agent={sm.get('agent')} "
                f"(basis {sm.get('basis')})"
            )
    else:
        lines.append(
            f"  scorability: separated tracks {_yn(sc['separated_tracks'])}, "
            f"caller activity {_yn(sc['enough_caller_activity'])}, "
            f"agent activity {_yn(sc['enough_agent_activity'])}"
        )
    if report["scorable"]:
        lines.append(f"  => {report['recommendation']}")
        if not report.get("verdict_eligible", True):
            lines.append(
                f"  [!] not verdict-eligible ({report.get('verdict_mode', 'scan')} "
                f"mode): {report.get('verdict_ineligible_reason')}"
            )
    else:
        lines.append(f"  => {report['recommendation']}")
        lines.append(f"     next step: {report['next_step']}")
    return "\n".join(lines)


def _yn(v: bool) -> str:
    return "yes" if v else "no"
