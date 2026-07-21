"""Per-channel acoustic health metrics: deterministic signal measures over
the SAME decoded audio the scorer reads.

Every number here is a SIGNAL measure -- a statement about the recording's
audio energy over time -- never a measure of intelligibility, word content,
or intent:

  snr_db                     speech-frame RMS vs noise-floor-frame RMS, the
                             two pools split by the reference energy VAD gate
                             (``_engine.vad.energy_vad``). A signal-to-noise
                             ESTIMATE of the capture, not a speech-quality or
                             recognition score.
  percent_silence            the share of frames below that same energy gate.
                             Silence in the energy sense (no acoustic energy
                             above the gate), never a semantic-pause judgement.
  energy_burst_rate_per_min  sustained bursts of acoustic energy per minute
                             (a burst = an active run at least
                             ``trust.SPEECH_MIN_RUN_SEC`` long). Deliberately
                             named for what it counts: bursts of energy,
                             never words, syllables, or a speaking rate.
  clipping_fraction          the share of samples at or above full scale
                             (``trust.CLIP_PEAK_LIN``), the sign of a capture
                             recorded too hot.
  duration_sec               the channel's decoded length in seconds.

Everything reuses hotato's existing primitives, nothing reimplements one: the
hardened WAV reader (``core._read_wav``, the same decode ``run``/``trust``/
``scan`` use), the reference framing (``_engine.audio.frame_rms``), the
reference energy VAD (``_engine.vad.energy_vad``), and the clipping counter
(``trust._peak_and_clip`` with ``trust.CLIP_PEAK_LIN``). Pure stdlib math,
fully deterministic: the same audio and config reproduce every number, and
every emitted value is a finite JSON number or ``None`` with a stated reason
(``errors.safe_json_dumps`` refuses NaN/Infinity).
"""

from __future__ import annotations

import math
import os
from typing import List, Optional

from ._engine.audio import frame_rms
from ._engine.score import ScoreConfig
from ._engine.vad import VADParams, energy_vad
from .trust import CLIP_PEAK_LIN, SPEECH_MIN_RUN_SEC, _peak_and_clip

__all__ = [
    "ACOUSTIC_NOTE",
    "acoustic_report",
    "acoustic_report_split",
    "channel_acoustics",
    "channel_summary_line",
]

# The honest scope statement carried on every acoustic block, so a machine
# reader gets the same boundary the docs state: these are signal measures.
ACOUSTIC_NOTE = (
    "per-channel signal measures over energy frames: snr_db compares "
    "speech-frame RMS to noise-floor-frame RMS split by the energy VAD gate; "
    "percent_silence is the share of frames below that gate; "
    "energy_burst_rate_per_min counts sustained bursts of acoustic energy, "
    "never words; clipping_fraction is the share of samples at full scale. "
    "Signal quality only: none of these measure intelligibility, word "
    "content, or intent."
)

# Linear RMS floor mirroring ``_engine.audio.to_dbfs``'s -120 dBFS convention:
# an RMS pool at/below it reads as the floor, so an SNR over digital silence
# stays a finite number (never Infinity).
_RMS_FLOOR = 10.0 ** (-120.0 / 20.0)

# The stated reasons snr_db is null -- one pool of frames was empty, so there
# is no ratio to estimate. Stable wording; consumers may match on it.
SNR_NO_SPEECH_REASON = (
    "no frame cleared the energy VAD gate (no detected speech-band energy), "
    "so there is no speech pool to estimate an SNR from"
)
SNR_NO_NOISE_REASON = (
    "every frame cleared the energy VAD gate (the channel is never quiet), "
    "so there is no noise-floor pool to estimate an SNR from"
)


def _pool_rms(rms: List[float]) -> float:
    """Energy-mean RMS of a pool of per-frame RMS values (sqrt of the mean
    square), floored so digital silence stays finite in a dB ratio."""
    acc = 0.0
    for r in rms:
        acc += r * r
    return max((acc / len(rms)) ** 0.5, _RMS_FLOOR)


def _burst_count(active: List[bool], hop_sec: float,
                 min_run_sec: float) -> int:
    """How many sustained active runs (energy bursts) the gated timeline
    carries: a run counts once, when it first reaches ``min_run_sec``."""
    min_frames = max(1, int(round(min_run_sec / hop_sec)))
    bursts = 0
    run = 0
    for a in active:
        if a:
            run += 1
            if run == min_frames:
                bursts += 1
        else:
            run = 0
    return bursts


def channel_acoustics(samples, sample_rate: int, *, channel: int,
                      role: Optional[str] = None,
                      vad_params: Optional[VADParams] = None,
                      cfg: Optional[ScoreConfig] = None) -> dict:
    """The acoustic-health block for ONE channel's decoded samples: the five
    signal measures documented in the module docstring, computed with the
    reference framing + energy VAD so the numbers line up with what
    ``trust``/``scan``/``run`` would see on the same audio.

    ``role`` is carried through verbatim (``"caller"``/``"agent"``/``None``):
    it labels which mapped party the channel is, never a claim about who is
    audible on it. ``snr_db`` is ``None`` (with ``snr_null_reason`` stating
    why) when either frame pool is empty."""
    cfg = cfg or ScoreConfig()
    params = vad_params if vad_params is not None else VADParams()

    rms, hop = frame_rms(samples, sample_rate, cfg.frame_ms, cfg.hop_ms)
    vad = energy_vad(rms, hop, params)
    active = vad.active
    n = len(active)
    duration = len(samples) / sample_rate

    speech = [r for r, a in zip(rms, active) if a]
    noise = [r for r, a in zip(rms, active) if not a]

    snr_db = None
    snr_null_reason: Optional[str] = None
    if not speech:
        snr_null_reason = SNR_NO_SPEECH_REASON
    elif not noise:
        snr_null_reason = SNR_NO_NOISE_REASON
    else:
        snr_db = round(
            20.0 * math.log10(_pool_rms(speech) / _pool_rms(noise)), 1)

    percent_silence = round(100.0 * len(noise) / n, 1) if n else 100.0
    bursts = _burst_count(active, hop, SPEECH_MIN_RUN_SEC)
    rate = round(bursts / (duration / 60.0), 1) if duration > 0 else None

    _, clipped_fraction = _peak_and_clip(samples, CLIP_PEAK_LIN)

    return {
        "channel": channel,
        "role": role,
        "duration_sec": round(duration, 3),
        "snr_db": snr_db,
        "snr_null_reason": snr_null_reason,
        "percent_silence": percent_silence,
        "energy_bursts": bursts,
        "energy_burst_rate_per_min": rate,
        "clipping_fraction": round(clipped_fraction, 4),
    }


def _envelope(source: str, sample_rate: int, channels: List[dict]) -> dict:
    return {
        "tool": "hotato",
        "kind": "acoustic",
        "schema_version": "1",
        "source": source,
        "sample_rate": sample_rate,
        "note": ACOUSTIC_NOTE,
        "channels": channels,
    }


def acoustic_report(path: str, *, caller_channel: int = 0,
                    agent_channel: int = 1,
                    cfg: Optional[ScoreConfig] = None) -> dict:
    """The acoustic-health block for one recording: every channel measured
    with :func:`channel_acoustics`, in channel order.

    Reads the WAV through ``core._read_wav`` -- the SAME hardened decode
    ``run``/``trust``/``scan`` use -- so a corrupt/empty/non-WAV file raises
    the same clean ``ValueError`` those do. Roles are attached only when the
    recording actually has separated channels (2 or more): the mapped caller
    channel scores under ``cfg.caller_vad`` and the mapped agent channel
    under ``cfg.agent_vad`` (the reference VAD parameters the scorer itself
    uses), so the frame pools match the scorer's own gate. A mono file still
    measures (its mix's signal health is still a fact) but carries no role:
    a mixed channel is nobody's channel, and labeling it would overclaim."""
    from .core import _read_wav  # deferred: the hardened reader, reused

    cfg = cfg or ScoreConfig()
    signal = _read_wav(path)
    multi = signal.num_channels >= 2
    channels = []
    for idx in range(signal.num_channels):
        role: Optional[str] = None
        params = VADParams()
        if multi and idx == caller_channel:
            role, params = "caller", cfg.caller_vad
        elif multi and idx == agent_channel:
            role, params = "agent", cfg.agent_vad
        channels.append(channel_acoustics(
            signal.get(idx), signal.sample_rate, channel=idx, role=role,
            vad_params=params, cfg=cfg,
        ))
    return _envelope(os.path.basename(path), signal.sample_rate, channels)


def acoustic_report_split(caller_path: str, agent_path: str, *,
                          cfg: Optional[ScoreConfig] = None) -> dict:
    """The acoustic-health block for a split-file pair (one WAV per party,
    ``hotato report --caller/--agent``'s input form): channel 0 of each file,
    the same first-channel convention the scorer's split path uses, measured
    under that role's reference VAD parameters. The pair renders as channels
    0 (caller) and 1 (agent) of one block, so the table shape matches the
    stereo case."""
    from .core import _read_wav  # deferred: the hardened reader, reused

    cfg = cfg or ScoreConfig()
    c = _read_wav(caller_path)
    a = _read_wav(agent_path)
    channels = [
        channel_acoustics(c.get(0), c.sample_rate, channel=0, role="caller",
                          vad_params=cfg.caller_vad, cfg=cfg),
        channel_acoustics(a.get(0), a.sample_rate, channel=1, role="agent",
                          vad_params=cfg.agent_vad, cfg=cfg),
    ]
    source = f"{os.path.basename(caller_path)} + {os.path.basename(agent_path)}"
    return _envelope(source, c.sample_rate, channels)


def channel_summary_line(ch: dict) -> str:
    """One channel's acoustic block as a single terse text line, shared by
    every text renderer that surfaces the block so the same numbers never
    read two ways."""
    if ch.get("role"):
        label = f"{ch['role']} ch{ch['channel']}"
    else:
        label = f"channel {ch['channel']}"
    snr = (f"SNR {ch['snr_db']} dB" if ch.get("snr_db") is not None
           else "SNR n/a")
    rate = ch.get("energy_burst_rate_per_min")
    rate_txt = f"{rate}/min energy bursts" if rate is not None else (
        "energy-burst rate n/a")
    return (f"{label}: {snr}, {ch['percent_silence']}% silence, "
            f"{rate_txt}, clipping {ch['clipping_fraction'] * 100:.2f}%, "
            f"{ch['duration_sec']}s")
