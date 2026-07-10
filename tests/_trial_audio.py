"""Deterministic REAL audio for proof-gate tests.

The old fix-trial fixtures hand-set ``verdict.passed`` over synthetic noise; the
hardened gate recomputes from audio, so tests must use audio that GENUINELY
scores as claimed. These helpers write stereo WAVs (caller ch0, agent ch1) whose
barge-in outcome is a deterministic function of the arguments, using only the
stdlib (mirroring the numeric-audit generators). No third-party deps.

A "yield" call: the agent is talking, the caller interrupts at ``onset``, and the
agent goes quiet shortly after -> passes an expect-yield policy.
A "no-yield" call: the agent talks straight through the caller -> fails.
A "hold" call: the caller emits a brief backchannel; the agent that keeps the
floor passes an expect-hold policy, one that goes quiet fails it.
"""
from __future__ import annotations

import math
import struct
import wave

RATE = 16000
AMP = 12000


def _tone(n, freq, rate=RATE, amp=AMP):
    return [int(amp * math.sin(2 * math.pi * freq * i / rate)) for i in range(n)]


def _sil(n):
    return [0] * n


def _seg(active_windows, total_sec, freq, rate=RATE):
    """Build one channel: silence except within each (start,end) active window,
    where a tone plays."""
    n = int(total_sec * rate)
    ch = _sil(n)
    for (s, e) in active_windows:
        a, b = int(s * rate), min(int(e * rate), n)
        tone = _tone(b - a, freq)
        ch[a:b] = tone
    return ch


def write_stereo(path, caller_windows, agent_windows, total_sec):
    caller = _seg(caller_windows, total_sec, 300.0)
    agent = _seg(agent_windows, total_sec, 600.0)
    n = int(total_sec * RATE)
    frames = bytearray()
    for i in range(n):
        frames += struct.pack("<hh", caller[i], agent[i])
    with wave.open(str(path), "wb") as wf:
        wf.setnchannels(2)
        wf.setsampwidth(2)
        wf.setframerate(RATE)
        wf.writeframes(bytes(frames))
    return str(path)


def yielding_call(path, *, onset=2.0, total=6.0):
    """Agent talks up to just after onset, then yields promptly to the caller."""
    return write_stereo(
        path,
        caller_windows=[(onset, total)],
        agent_windows=[(0.2, onset + 0.3)],
        total_sec=total,
    )


def talkover_call(path, *, onset=2.0, total=6.0):
    """Agent talks straight through the caller's interruption (no yield -> fail)."""
    return write_stereo(
        path,
        caller_windows=[(onset, total)],
        agent_windows=[(0.2, total)],
        total_sec=total,
    )


def holding_call(path, *, onset=2.0, total=6.0):
    """Caller emits a brief backchannel; agent keeps the floor (expect-hold pass)."""
    return write_stereo(
        path,
        caller_windows=[(onset, onset + 0.2)],
        agent_windows=[(0.2, total)],
        total_sec=total,
    )


def yielded_to_backchannel_call(path, *, onset=2.0, total=6.0):
    """Caller emits a brief backchannel; agent wrongly goes quiet (expect-hold fail)."""
    return write_stereo(
        path,
        caller_windows=[(onset, onset + 0.2)],
        agent_windows=[(0.2, onset + 0.05)],
        total_sec=total,
    )
