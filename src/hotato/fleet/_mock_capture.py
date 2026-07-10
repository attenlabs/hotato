"""Local synthetic capture for the MockAdapter (tests/offline demo only).

Generates a fresh stereo 'after' recording where the agent yields, replaying the
same caller stimulus as the scenario. Stdlib only.
"""
from __future__ import annotations

import math
import os
import struct
import wave

RATE = 16000
AMP = 12000


def _tone(n, freq):
    return [int(AMP * math.sin(2 * math.pi * freq * i / RATE)) for i in range(n)]


def capture_yielding(work_dir: str, clone_ref: str, scenario: dict) -> dict:
    onset = float(scenario.get("caller_onset_sec", 2.0))
    total = 6.0
    n = int(total * RATE)
    caller = [0] * n
    agent = [0] * n
    # caller: scripted interruption from onset to end (300 Hz)
    a = int(onset * RATE)
    caller[a:] = _tone(n - a, 300.0)
    # agent: talks until just after onset then YIELDS (600 Hz)
    end = min(n, int((onset + 0.3) * RATE))
    agent[int(0.2 * RATE):end] = _tone(end - int(0.2 * RATE), 600.0)
    path = os.path.join(work_dir, f"{clone_ref}-{scenario.get('id','s')}.wav")
    frames = bytearray()
    for i in range(n):
        frames += struct.pack("<hh", caller[i], agent[i])
    with wave.open(path, "wb") as wf:
        wf.setnchannels(2); wf.setsampwidth(2); wf.setframerate(RATE)
        wf.writeframes(bytes(frames))
    return {"recording": path, "clone_ref": clone_ref, "scenario_id": scenario.get("id")}


__all__ = ["capture_yielding"]
