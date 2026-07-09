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
from ._engine.vad import energy_vad, first_active_sec
from .echo import echo_signal
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


def trust_report(
    path: str,
    *,
    caller_channel: int = 0,
    agent_channel: int = 1,
    cfg: Optional[ScoreConfig] = None,
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

    # A single-channel recording cannot separate caller from agent at all: report
    # it as not scorable rather than accessing a channel that does not exist.
    if signal.num_channels < 2:
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
    caller_active = energy_vad(rms_c, hop, cfg.caller_vad).active
    agent_active = energy_vad(rms_a, hop, cfg.agent_vad).active
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
    crosstalk_out = {
        "coherence": crosstalk["coherence"],
        "lag_sec": crosstalk["lag_sec"],
        "suspected": crosstalk["echo_suspected"],
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
    if crosstalk_out["suspected"]:
        warnings.append(
            f"crosstalk risk: cross-channel coherence {crosstalk_out['coherence']} "
            f"at {crosstalk_out['lag_sec']}s lag; the caller channel may be "
            "carrying the agent's own audio (echo bleed), so a scan may see the "
            "agent hearing itself"
        )
    if swap:
        warnings.append(channels["swap_reason"])

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
    )


def _clip_block(peak: float, clipped_fraction: float) -> dict:
    return {
        "peak": round(peak, 4),
        "peak_dbfs": _peak_dbfs(peak),
        "clipped_fraction": round(clipped_fraction, 6),
        "clipped": clipped_fraction >= CLIP_WARN_FRACTION,
    }


def _finalize(base: dict, *, channels, crosstalk, scorability, warnings,
              scorable: bool, reason, next_step) -> dict:
    """Attach the channel/crosstalk/scorability blocks and the recommendation,
    and set the process exit code (0 scorable, 2 not)."""
    if channels is not None:
        base["channels"] = channels
    if crosstalk is not None:
        base["crosstalk_risk"] = crosstalk
    base["scorability"] = scorability
    base["warnings"] = warnings
    base["scorable"] = scorable
    if scorable:
        base["recommendation"] = SAFE_RECOMMENDATION
        base["not_scorable_reason"] = None
        base["next_step"] = None
        base["exit_code"] = 0
    else:
        base["recommendation"] = f"NOT SCORABLE: {reason}"
        base["not_scorable_reason"] = reason
        base["next_step"] = next_step
        base["exit_code"] = 2
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
        ct_tag = ("HIGH" if ct["suspected"] else "low")
        lines.append(
            f"  crosstalk: coherence {ct['coherence']} ({ct_tag}) at "
            f"{ct['lag_sec']}s lag"
        )
        if ch["possible_swap"]:
            lines.append(f"  possible channel swap: {ch['swap_reason']}")
    sc = report["scorability"]
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
