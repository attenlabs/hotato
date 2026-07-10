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
  recommendation            "safe to scan", or "NOT SCORABLE" with the specific
                            reason AND the next step to fix it

HONESTY, the whole point of this command: it NEVER labels intent and NEVER emits
a turn-taking verdict (no yield / hold, no pass / fail). It answers exactly one
question -- is this audio good enough to score? -- and stops there. A recording
that is safe to scan may still contain agent bugs; that is what ``hotato scan`` /
``hotato run`` are for.

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
]

NOTE = (
    "input health only: whether the audio is scorable, not a judgement of the "
    "agent's turn-taking. It never labels intent and never emits a yield/hold "
    "or pass/fail verdict."
)

# A recording that clears every gate below.
SAFE_RECOMMENDATION = "safe to scan"

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
# (suspected + warning + the recommendation is downgraded off "safe to scan").
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
                      caller_noise_floor_db=None, agent_noise_floor_db=None) -> dict:
    """Cross-channel leakage (bleed): the level of the loudest consistent,
    attenuated delayed COPY of one channel found on the other, and whether it is
    loud enough to be mistaken for the other party's activity downstream.

    Both directions are measured -- an agent copy leaking onto the caller channel
    (the dangerous direction: leaked agent audio read as caller activity) and the
    reverse. The loudest consistent copy is reported. ``leakage_suspected`` is set
    when that copy is at or above the fixed ``LEAKAGE_WARN_DB`` ratio OR when the
    dynamic rule finds it would cross the RECEIVING channel's VAD gate for a
    sustained run (``crosses_gate``, computed against that receiver's noise floor);
    a fainter copy that does neither is reported (for transparency) but not flagged,
    and no consistent copy at all reports ``leakage_db = None``."""
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
            candidates.append((db, cons, lag, direction, crosses))
    if not candidates:
        return {"leakage_db": None, "leakage_direction": None,
                "leakage_lag_sec": None, "leakage_suspected": False}
    # The loudest consistent copy (highest dB = most leaked energy) is the worst.
    db, cons, lag, direction, crosses = max(candidates, key=lambda t: t[0])
    return {
        "leakage_db": db,
        "leakage_direction": direction,
        "leakage_lag_sec": lag,
        # Fixed -40 dB ratio bar OR the dynamic gate-crossing rule; the dynamic term
        # only ADDS suspected cases (it can flag EARLIER than -40 dB), never removes.
        "leakage_suspected": db >= LEAKAGE_WARN_DB or crosses,
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
) -> dict:
    """Inspect one recording and return the input-health report.

    Reads the WAV through the same hardened reader ``run`` uses (a corrupt,
    empty, truncated, or non-WAV file raises the usual ValueError -> the CLI's
    exit-2 usage error), then frames and VADs each channel with the reference
    engine so the activity numbers line up with what ``scan`` / ``run`` would
    see. A mono file, identical channels, or a silent required channel are NOT
    raised: they are reported as ``scorable: false`` with the reason and the
    next step, because "is this scorable?" is exactly the question asked.

    ``exit_code`` in the returned dict is 0 when the recording is safe to scan
    and 2 when it is not, matching the CLI's unusable-input convention.
    """
    from .core import _read_wav  # hardened WAV reader; reused, never reimplemented

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
                base, signal, diarizer=diarizer, egress_opt_in=egress_opt_in, cfg=cfg
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

    return _finalize(
        base,
        channels=channels,
        crosstalk=crosstalk_out,
        scorability=scorability,
        warnings=warnings,
        scorable=reason is None,
        reason=reason,
        next_step=next_step,
        caution=caution,
    )


def _clip_block(peak: float, clipped_fraction: float) -> dict:
    return {
        "peak": round(peak, 4),
        "peak_dbfs": _peak_dbfs(peak),
        "clipped_fraction": round(clipped_fraction, 6),
        "clipped": clipped_fraction >= CLIP_WARN_FRACTION,
    }


def _finalize(base: dict, *, channels, crosstalk, scorability, warnings,
              scorable: bool, reason, next_step, caution=None) -> dict:
    """Attach the channel/crosstalk/scorability blocks and the recommendation,
    and set the process exit code (0 scorable, 2 not).

    ``caution`` downgrades the recommendation of a still-scorable recording OFF
    "safe to scan" (a verdict-changing warning -- leakage, low signal, or a possible
    channel swap -- can corrupt a downstream timing measurement) WITHOUT changing
    scorability or the exit code -- ``scorable`` and the not-scorable gate are
    untouched, only the human-facing recommendation is.

    ``input_health`` is an explicit 3-state summary of that same axis, additive to
    the report: "clean" (scorable, no verdict-changing warning), "caution" (scorable
    but a verdict-changing warning is present, i.e. ``caution`` is set), or
    "not_scorable" (a gate failed). It restates in one field what a machine reader
    would otherwise have to infer from ``scorable`` + the recommendation prefix."""
    if channels is not None:
        base["channels"] = channels
    if crosstalk is not None:
        base["crosstalk_risk"] = crosstalk
    base["scorability"] = scorability
    base["warnings"] = warnings
    base["scorable"] = scorable
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
                   cfg: ScoreConfig) -> dict:
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
            "hotato run --mono <file> --diarize"
        )
        # "clean" scorable; a below-bar tier carries indicative_only, not a
        # verdict-changing input-health warning, so the input itself reads clean.
        base["input_health"] = "clean"
        base["not_scorable_reason"] = None
        base["next_step"] = None
        base["exit_code"] = 0
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
    else:
        lines.append(f"  => {report['recommendation']}")
        lines.append(f"     next step: {report['next_step']}")
    return "\n".join(lines)


def _yn(v: bool) -> str:
    return "yes" if v else "no"
