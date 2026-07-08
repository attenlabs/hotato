"""``hotato scan``: surface CANDIDATE turn-taking moments in a whole call.

Walks the two VAD activity tracks (caller and agent) across the ENTIRE
recording and lists the moments where turn-taking physically happened:

  overlap_while_agent_talking   the caller became active while the agent was
                                active; reports the overlap length and whether
                                (and after how long) the agent went silent
                                within the search window
  agent_start_during_caller     the agent started a fresh utterance while the
                                caller was active (a premature-start candidate)
  long_response_gap             the caller finished a turn and the agent's next
                                utterance came late (or never)
  agent_stop_no_caller          the agent went from active to quiet with no
                                caller energy anywhere nearby; nothing on the
                                caller channel explains the drop (not a
                                barge-in, not a caller-driven handoff)
  echo_correlated_activity      a caller run whose envelope is a lag-shifted
                                copy of the agent's own audio (high cross-channel
                                cosine coherence); the "caller" energy is likely
                                leaked TTS, so any yield to it may be the agent
                                hearing itself, not a real interruption

HONESTY, stated once and repeated in the output header: candidates are timing
events. This tool cannot know whether a caller sound was "mhm" or "stop";
energy is not intent, and no candidate carries an intent claim. You decide the
expected behavior and label the moment with:

  hotato fixture create --onset <t> --expect yield|hold

Long recordings are read in a windowed pass (the WAV is decoded chunk by
chunk; only the per-frame RMS track is kept in memory), and the per-frame
values are identical to the reference ``frame_rms`` over the whole file.
Everything runs offline; no audio leaves the machine.
"""

from __future__ import annotations

import array
import math
import os
import struct
import sys
import wave
from typing import List, Optional, Tuple

from ._engine.score import ScoreConfig
from ._engine.vad import energy_vad

# numpy is an optional acceleration only, mirroring the engine. It is
# resolved lazily on first use so importing the module stays cheap; an
# explicit ``_np = None`` assignment (tests) forces the stdlib path.
_NP_UNRESOLVED = object()
_np = _NP_UNRESOLVED


def _resolve_np():
    """Return the numpy module if importable, else None. Memoized in ``_np``."""
    global _np
    if _np is _NP_UNRESOLVED:
        try:  # optional acceleration only, mirroring the engine
            import numpy
        except Exception:  # pragma: no cover - numpy is genuinely optional
            _np = None
        else:
            _np = numpy
    return _np

__all__ = ["scan_recording", "activity_tracks", "render_text", "KINDS",
           "SCAN_NOTE", "DEFAULT_TOP", "DEFAULT_MIN_GAP_SEC"]

KINDS = ("overlap_while_agent_talking", "agent_start_during_caller",
         "long_response_gap", "agent_stop_no_caller",
         "echo_correlated_activity")

SCAN_NOTE = (
    "Candidates are timing events. You decide the expected behavior; label "
    "with: hotato fixture create --onset <t> --expect yield|hold"
)

DEFAULT_TOP = 20
DEFAULT_MIN_GAP_SEC = 2.0

# Samples decoded per read while walking the file (per pass, all channels).
_CHUNK_FRAMES = 262144


def _decode(raw: bytes, sampwidth: int) -> List[float]:
    """Decode interleaved PCM bytes to floats in [-1, 1], exactly like the
    engine's ``read_wav``."""
    if sampwidth == 1:
        a = array.array("B")
        a.frombytes(raw)
        return [(x - 128) / 128.0 for x in a]
    if sampwidth == 2:
        a = array.array("h")
        a.frombytes(raw)
        if sys.byteorder == "big":
            a.byteswap()
        return [x / 32768.0 for x in a]
    if sampwidth == 4:
        a = array.array("i")
        a.frombytes(raw)
        if sys.byteorder == "big":
            a.byteswap()
        return [x / 2147483648.0 for x in a]
    raise ValueError(
        f"unsupported sample width {sampwidth * 8}-bit; please convert to "
        "16-bit PCM (for example with ffmpeg -acodec pcm_s16le)"
    )


def _rms(seg: List[float]) -> float:
    """Identical math to the engine's ``frame_rms`` inner step."""
    np = _resolve_np()
    if np is not None:
        arr = np.asarray(seg, dtype=np.float64)
        return float(np.sqrt(np.mean(arr * arr))) if arr.size else 0.0
    if not seg:
        return 0.0
    acc = 0.0
    for x in seg:
        acc += x * x
    return (acc / len(seg)) ** 0.5


def windowed_frame_rms(
    path: str,
    caller_channel: int = 0,
    agent_channel: int = 1,
    frame_ms: float = 20.0,
    hop_ms: float = 10.0,
) -> Tuple[List[float], List[float], float, int, float]:
    """One windowed pass over a WAV: per-frame RMS for the two selected
    channels without holding the decoded audio in memory.

    Returns ``(caller_rms, agent_rms, hop_sec, sample_rate, duration_sec)``.
    The frame values equal the reference ``frame_rms`` over the whole file
    (same 20 ms window, 10 ms hop, same partial tail frames).
    """
    try:
        wf = wave.open(path, "rb")
    except (wave.Error, EOFError, struct.error) as exc:
        raise ValueError(
            f"{path!r} is not a readable PCM WAV ({exc}). Export a PCM WAV, "
            "e.g. ffmpeg -i input -acodec pcm_s16le output.wav"
        ) from exc
    with wf:
        n_channels = wf.getnchannels()
        sampwidth = wf.getsampwidth()
        sample_rate = wf.getframerate()
        declared_frames = wf.getnframes()
        # A zero/negative sample rate is a corrupt header: ``wave`` reads it back
        # but ``hop / sample_rate`` below would raise a bare ZeroDivisionError.
        # Reject it as a clean usage error, matching core._read_wav so `run` and
        # `scan`/`analyze` agree on this input.
        if sample_rate <= 0:
            raise ValueError(
                f"{path!r} declares an invalid sample rate ({sample_rate} Hz); "
                "the file is corrupt or was mis-exported. Re-export a PCM WAV, "
                "e.g. ffmpeg -i input -acodec pcm_s16le -ar 16000 output.wav"
            )
        if n_channels < 2:
            raise ValueError(
                "--stereo file has one channel; a single mixed mono call is "
                "not enough to attribute talk-over reliably. Export a real "
                "two-channel recording (caller on one channel, agent on the "
                "other)."
            )
        for role, idx in (("caller", caller_channel), ("agent", agent_channel)):
            if idx < 0 or idx >= n_channels:
                raise ValueError(
                    f"--{role}-channel {idx} is out of range for a "
                    f"{n_channels}-channel recording "
                    f"(valid channels: 0..{n_channels - 1})."
                )

        frame_len = max(1, int(round(sample_rate * frame_ms / 1000.0)))
        hop = max(1, int(round(sample_rate * hop_ms / 1000.0)))
        hop_sec = hop / sample_rate

        buf_c: List[float] = []
        buf_a: List[float] = []
        buf_offset = 0          # absolute sample index of buf_*[0]
        next_start = 0          # absolute sample index of the next frame
        rms_c: List[float] = []
        rms_a: List[float] = []

        def _emit_full_frames():
            nonlocal next_start, buf_offset, buf_c, buf_a
            end = buf_offset + len(buf_c)
            while next_start + frame_len <= end:
                s = next_start - buf_offset
                rms_c.append(_rms(buf_c[s:s + frame_len]))
                rms_a.append(_rms(buf_a[s:s + frame_len]))
                next_start += hop
            drop = next_start - buf_offset
            if drop > 0:
                buf_c = buf_c[drop:]
                buf_a = buf_a[drop:]
                buf_offset = next_start

        while True:
            try:
                raw = wf.readframes(_CHUNK_FRAMES)
            except (wave.Error, EOFError, struct.error) as exc:
                raise ValueError(
                    f"{path!r} is not a readable PCM WAV ({exc}). Export a "
                    "PCM WAV, e.g. ffmpeg -i input -acodec pcm_s16le "
                    "output.wav"
                ) from exc
            if not raw:
                break
            frame_bytes = sampwidth * n_channels
            whole = len(raw) - (len(raw) % frame_bytes)
            if whole == 0:
                break
            samples = _decode(raw[:whole], sampwidth)
            buf_c.extend(samples[caller_channel::n_channels])
            buf_a.extend(samples[agent_channel::n_channels])
            _emit_full_frames()

        n_total = buf_offset + len(buf_c)
        if n_total == 0 and not rms_c:
            raise ValueError(
                f"{path!r} contains no audio samples (empty or header-only "
                "WAV)."
            )
        # Truncation guard, matching core._read_wav: for a well-formed file the
        # header's declared frame count equals the decoded samples per channel; a
        # short/cut-off data chunk decodes fewer. Without this, `analyze`/`scan`
        # would silently report a truncated recording as a normal short call while
        # `run` (via core._read_wav) correctly rejects the same file.
        if declared_frames and n_total < declared_frames:
            raise ValueError(
                f"{path!r} is truncated or corrupt: its header declares "
                f"{declared_frames} frames but only {n_total} are present. "
                "Re-export the full recording."
            )
        # Tail: partial frames, exactly like frame_rms (frames exist while
        # their start lies inside the signal).
        while next_start < n_total:
            s = next_start - buf_offset
            rms_c.append(_rms(buf_c[s:s + frame_len]))
            rms_a.append(_rms(buf_a[s:s + frame_len]))
            next_start += hop

        return rms_c, rms_a, hop_sec, sample_rate, n_total / sample_rate


def _runs(active: List[bool], min_frames: int) -> List[Tuple[int, int]]:
    """Maximal contiguous active runs of at least ``min_frames``, as
    ``(start, end)`` frame indices (end exclusive)."""
    runs = []
    i, n = 0, len(active)
    while i < n:
        if active[i]:
            j = i
            while j < n and active[j]:
                j += 1
            if j - i >= min_frames:
                runs.append((i, j))
            i = j
        else:
            i += 1
    return runs


def _agent_quiet_point(agent: List[bool], start: int, search_end: int,
                       quiet_frames: int, n: int) -> Optional[int]:
    """First frame in [start, search_end) where the agent goes quiet and
    stays quiet for ``quiet_frames`` (the engine's yield scan, minus the
    caller-proximity judgement: scan reports the timing fact only)."""
    i = start
    while i < search_end:
        if not agent[i]:
            run, j = 0, i
            while j < n and not agent[j]:
                run += 1
                if run >= quiet_frames:
                    return i
                j += 1
            i = j + 1
        else:
            i += 1
    return None


def scan_recording(
    path: str,
    *,
    caller_channel: int = 0,
    agent_channel: int = 1,
    cfg: Optional[ScoreConfig] = None,
    min_gap_sec: float = DEFAULT_MIN_GAP_SEC,
) -> dict:
    """Scan one two-channel recording and return every candidate moment as
    timing facts, sorted by salience (overlap length or gap length, longest
    first). No intent is claimed anywhere: see ``SCAN_NOTE``."""
    if cfg is None:
        cfg = ScoreConfig()
    if min_gap_sec <= 0:
        raise ValueError(f"--min-gap must be > 0 seconds; got {min_gap_sec}.")
    rms_c, rms_a, hop, sample_rate, duration = windowed_frame_rms(
        path, caller_channel, agent_channel, cfg.frame_ms, cfg.hop_ms
    )
    caller = energy_vad(rms_c, hop, cfg.caller_vad).active
    agent = energy_vad(rms_a, hop, cfg.agent_vad).active
    n = min(len(caller), len(agent))
    caller, agent = caller[:n], agent[:n]

    min_run = max(1, int(round(cfg.onset_min_run_sec / hop)))
    lookback = max(1, int(round(cfg.agent_onset_lookback_sec / hop)))
    quiet_frames = max(1, int(round(cfg.yield_hangover_sec / hop)))
    silence_frames = max(1, int(round(cfg.turn_end_silence_sec / hop)))
    search_frames = int(round(cfg.max_search_sec / hop))
    proximity_frames = max(1, int(round(cfg.caller_proximity_sec / hop)))

    caller_runs = _runs(caller, min_run)
    agent_runs = _runs(agent, min_run)
    candidates = []

    # 1. caller became active while the agent was active.
    for cs, ce in caller_runs:
        agent_at_onset = any(
            agent[j] for j in range(max(0, cs - lookback), min(cs + 1, n))
        )
        if not agent_at_onset:
            continue
        search_end = min(n, cs + search_frames)
        q = _agent_quiet_point(agent, cs, search_end, quiet_frames, n)
        overlap_end = q if q is not None else search_end
        overlap = sum(
            1 for k in range(cs, overlap_end) if caller[k] and agent[k]
        ) * hop
        candidates.append({
            "t_sec": round(cs * hop, 3),
            "kind": "overlap_while_agent_talking",
            "durations": {"overlap_sec": round(overlap, 3)},
            "agent_reaction": {
                "went_silent_within_search": q is not None,
                "after_sec": round((q - cs) * hop, 3) if q is not None
                             else None,
                "search_window_sec": cfg.max_search_sec,
            },
            "_salience": overlap,
        })

    # 2. the agent started a fresh utterance while the caller was active.
    for a_start, a_end in agent_runs:
        if a_start == 0 or not caller[a_start]:
            continue
        k = a_start
        while k < n and caller[k] and agent[k]:
            k += 1
        overlap = (k - a_start) * hop
        # How much longer the caller kept the floor after the agent came in.
        te = None
        for cs, ce in caller_runs:
            if cs <= a_start < ce:
                nxt = next((s for s, _ in caller_runs if s >= ce), None)
                if nxt is None or nxt - ce >= silence_frames:
                    te = ce
                break
        candidates.append({
            "t_sec": round(a_start * hop, 3),
            "kind": "agent_start_during_caller",
            "durations": {
                "overlap_sec": round(overlap, 3),
                "caller_kept_talking_sec": (
                    round(max(0, te - a_start) * hop, 3)
                    if te is not None else None
                ),
            },
            "agent_reaction": None,
            "_salience": overlap,
        })

    # 3. the caller finished a turn and the agent's next utterance came late.
    for idx, (cs, ce) in enumerate(caller_runs):
        if ce >= n:
            continue  # caller still talking at end of recording
        nxt_caller = (caller_runs[idx + 1][0]
                      if idx + 1 < len(caller_runs) else None)
        if nxt_caller is not None and nxt_caller - ce < silence_frames:
            continue  # not a turn end, the same turn resumes
        if agent[ce]:
            continue  # the agent was already talking at the turn end
        next_agent = next((s for s, _ in agent_runs if s >= ce), None)
        gap = ((next_agent - ce) if next_agent is not None else (n - ce)) * hop
        if gap < min_gap_sec:
            continue
        candidates.append({
            "t_sec": round(ce * hop, 3),
            "kind": "long_response_gap",
            "durations": {"gap_sec": round(gap, 3)},
            "agent_reaction": {
                "next_agent_onset_sec": (round(next_agent * hop, 3)
                                         if next_agent is not None else None),
            },
            "_salience": gap,
        })

    # 4. the agent went quiet mid-run with no caller energy anywhere nearby
    #    (no barge-in, no caller-driven handoff explains the drop). Pure
    #    timing: an active agent run ends, stays quiet for at least
    #    yield_hangover_sec (a real stop, not a brief mid-sentence breath),
    #    and the caller track shows zero activity within caller_proximity_sec
    #    of that stop on either side.
    for idx, (a_start, a_end) in enumerate(agent_runs):
        if a_end >= n:
            continue  # agent still talking at end of recording
        next_agent = (agent_runs[idx + 1][0]
                      if idx + 1 < len(agent_runs) else None)
        trailing = (next_agent - a_end) if next_agent is not None else n - a_end
        if trailing < quiet_frames:
            continue  # not a real stop, the same run resumes shortly
        lo = max(0, a_end - proximity_frames)
        hi = min(n, a_end + proximity_frames)
        caller_nearby = any(caller[k] for k in range(lo, hi))
        if caller_nearby:
            continue  # caller energy nearby explains the drop
        candidates.append({
            "t_sec": round(a_end * hop, 3),
            "kind": "agent_stop_no_caller",
            "durations": {
                "trailing_silence_sec": round(trailing * hop, 3),
                "caller_proximity_sec": cfg.caller_proximity_sec,
            },
            "agent_reaction": None,
            "_salience": trailing * hop,
        })

    # 5. echo-correlated caller activity: the caller channel is carrying a
    #    lag-shifted copy of the agent's OWN audio (leaked TTS), not independent
    #    speech. Computed on the same RMS envelopes: a whole-call coherence finds
    #    the echo lag, then each caller run is scored locally at that lag. Runs
    #    above the coherence threshold are flagged so a "barge-in" that is really
    #    the agent hearing itself is not mistaken for a real caller event.
    from .echo import (echo_signal, window_cosine,
                       DEFAULT_COHERENCE_THRESHOLD)
    echo = echo_signal(rms_c[:n], rms_a[:n], hop)
    if echo["echo_suspected"]:
        lag_frames = int(round(echo["lag_sec"] / hop))
        for cs, ce in caller_runs:
            coh = window_cosine(rms_c, rms_a, cs, ce, lag_frames)
            if coh < DEFAULT_COHERENCE_THRESHOLD:
                continue
            candidates.append({
                "t_sec": round(cs * hop, 3),
                "kind": "echo_correlated_activity",
                "durations": {
                    "activity_sec": round((ce - cs) * hop, 3),
                    "lag_sec": echo["lag_sec"],
                },
                "agent_reaction": {
                    "coherence": round(coh, 3),
                    "echo_suspected": True,
                },
                "_salience": coh,
            })

    candidates.sort(key=lambda c: (-c["_salience"], c["t_sec"]))
    for c in candidates:
        del c["_salience"]

    return {
        "tool": "hotato",
        "kind": "scan",
        "schema_version": "1",
        "source": os.path.basename(path),
        "sample_rate": sample_rate,
        "duration_sec": round(duration, 3),
        "hop_sec": hop,
        "note": SCAN_NOTE,
        "config": {
            "min_gap_sec": min_gap_sec,
            "search_window_sec": cfg.max_search_sec,
        },
        "total_candidates": len(candidates),
        "candidates": candidates,
    }


def activity_tracks(
    path: str,
    *,
    caller_channel: int = 0,
    agent_channel: int = 1,
    cfg: Optional[ScoreConfig] = None,
) -> Tuple[List[bool], List[bool], float, int, float]:
    """The exact per-frame caller/agent VAD activity tracks ``scan_recording``
    walks, for callers that also need to DRAW the tracks (a per-moment timeline)
    rather than just list candidates.

    Returns ``(caller_active, agent_active, hop_sec, sample_rate, duration_sec)``
    with the two boolean tracks trimmed to a common length. Same windowed pass,
    same ``energy_vad``, so the tracks line up frame-for-frame with the
    candidates ``scan_recording`` emits for the same file and config.
    """
    if cfg is None:
        cfg = ScoreConfig()
    rms_c, rms_a, hop, sample_rate, duration = windowed_frame_rms(
        path, caller_channel, agent_channel, cfg.frame_ms, cfg.hop_ms
    )
    caller = energy_vad(rms_c, hop, cfg.caller_vad).active
    agent = energy_vad(rms_a, hop, cfg.agent_vad).active
    n = min(len(caller), len(agent))
    return caller[:n], agent[:n], hop, sample_rate, duration


def _line(i: int, c: dict) -> str:
    d = c["durations"]
    if c["kind"] == "overlap_while_agent_talking":
        r = c["agent_reaction"]
        if r["went_silent_within_search"]:
            tail = f"agent went silent after {r['after_sec']:.2f}s"
        else:
            tail = (f"agent did not go silent within "
                    f"{r['search_window_sec']:.1f}s")
        detail = f"overlap={d['overlap_sec']:.2f}s  {tail}"
    elif c["kind"] == "agent_start_during_caller":
        detail = f"overlap={d['overlap_sec']:.2f}s"
        if d.get("caller_kept_talking_sec") is not None:
            detail += (f"  caller kept talking "
                       f"{d['caller_kept_talking_sec']:.2f}s")
    elif c["kind"] == "long_response_gap":
        detail = f"gap={d['gap_sec']:.2f}s"
        nxt = c["agent_reaction"]["next_agent_onset_sec"]
        detail += (f"  next agent onset {nxt:.2f}s" if nxt is not None
                   else "  no agent onset before the end of the recording")
    elif c["kind"] == "echo_correlated_activity":
        coh = c["agent_reaction"]["coherence"]
        detail = (f"WARNING likely agent echo: coherence={coh:.2f} at lag "
                  f"{d['lag_sec']:.2f}s  (caller channel looks like leaked TTS; "
                  f"a yield here may be the agent hearing itself)")
    else:
        detail = (f"trailing silence={d['trailing_silence_sec']:.2f}s  "
                  f"no caller energy within {d['caller_proximity_sec']:.2f}s")
    return (f"  [{i:>2}] t={c['t_sec']:.2f}s  {c['kind']:<28} {detail}")


def render_text(scan: dict, top: int = DEFAULT_TOP) -> str:
    total = scan["total_candidates"]
    shown = scan["candidates"] if top <= 0 else scan["candidates"][:top]
    lines = [
        f"hotato scan: {scan['source']}  ({scan['duration_sec']:.1f}s, "
        f"{total} candidate moment{'s' if total != 1 else ''})",
        scan["note"],
    ]
    if total == 0:
        lines.append("  no candidate moments found (no overlap onsets, no "
                     f"response gaps over {scan['config']['min_gap_sec']:.1f}s)")
        return "\n".join(lines)
    if len(shown) < total:
        lines.append(f"showing {len(shown)} of {total} by salience (longest "
                     "overlap or gap first); --top 0 shows all")
    for i, c in enumerate(shown, 1):
        lines.append(_line(i, c))
    return "\n".join(lines)
