"""Local synthetic capture for the MockAdapter (tests/offline demo only).

Generates a fresh stereo 'after' recording where the agent yields, replaying the
same caller stimulus as the scenario. Stdlib only.
"""
from __future__ import annotations

import hashlib
import math
import os
import struct
import wave

RATE = 16000
AMP = 12000


def _tone(n, freq):
    return [int(AMP * math.sin(2 * math.pi * freq * i / RATE)) for i in range(n)]


def capture(work_dir: str, clone_ref: str, scenario: dict, *, fixed: bool = True) -> dict:
    """Synthesize one recording for a scenario, replaying the SAME scripted caller
    stimulus regardless of the agent's behaviour, so a failing 'before'
    (``fixed=False``) and a passing 'after' (``fixed=True``) share an identical
    caller channel -- a faithful same-scenario recapture. ``fixed`` decides only the
    AGENT channel:
      * yield scenario: fixed -> agent yields (pass); not fixed -> agent talks over (fail).
      * hold scenario:  fixed -> agent holds the floor (pass); not fixed -> agent yields
        to the backchannel (fail).
    """
    onset = float(scenario.get("caller_onset_sec", 2.0))
    total = 6.0
    n = int(total * RATE)
    caller = [0] * n
    agent = [0] * n
    a = int(onset * RATE)
    expect_yield = bool((scenario.get("expected") or {}).get("yield", True))
    ag0 = int(0.2 * RATE)
    # a per-clone agent tone so different clones (a failing 'before' vs a fixed
    # 'after', or two passing captures) yield DISTINCT decoded PCM -- a fresh
    # recording, not a byte-identical re-score -- while the caller stimulus and the
    # pass/fail behaviour are unchanged.
    afreq = 600.0 + (int(hashlib.sha256(str(clone_ref).encode()).hexdigest(), 16) % 11)
    if expect_yield:
        # a real interruption: caller talks from onset to end (identical both sides).
        caller[a:] = _tone(n - a, 300.0)
        if fixed:
            end = min(n, int((onset + 0.3) * RATE))            # agent yields -> pass
            agent[ag0:end] = _tone(end - ag0, afreq)
        else:
            agent[ag0:] = _tone(n - ag0, afreq)                # agent talks over -> fail
    else:
        # a mere backchannel (short "mhm"); caller identical both sides.
        blip = min(n - a, int(0.25 * RATE))
        caller[a:a + blip] = _tone(blip, 300.0)
        if fixed:
            agent[ag0:] = _tone(n - ag0, afreq)                # agent holds -> pass
        else:
            end = min(n, int((onset + 0.3) * RATE))            # agent drops the floor -> fail hold
            agent[ag0:end] = _tone(end - ag0, afreq)
    path = os.path.join(work_dir, f"{clone_ref}-{scenario.get('id','s')}.wav")
    frames = bytearray()
    for i in range(n):
        frames += struct.pack("<hh", caller[i], agent[i])
    with wave.open(path, "wb") as wf:
        wf.setnchannels(2); wf.setsampwidth(2); wf.setframerate(RATE)
        wf.writeframes(bytes(frames))
    return {"recording": path, "clone_ref": clone_ref, "scenario_id": scenario.get("id")}


def capture_yielding(work_dir: str, clone_ref: str, scenario: dict) -> dict:
    """The fixed (passing) recapture -- what a fixed clone produces."""
    return capture(work_dir, clone_ref, scenario, fixed=True)


__all__ = ["capture", "capture_yielding"]
