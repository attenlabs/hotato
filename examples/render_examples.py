#!/usr/bin/env python3
"""Render the example fixtures deterministically, using the shipped engine.

This is the project-local mirror of the canonical upstream generator
(``openrepo/scenarios/generate_fixtures.py``). The render algorithm is
identical and stdlib-only, and the WAV writer is imported from the vendored
engine (``hotato._engine.audio.write_wav``), so this script needs no
third-party packages and no access to the upstream checkout. It renders the
example scenarios that live OUTSIDE the shipped package (this ``examples/``
tree), never the frozen bundled battery.

Determinism: the per-channel seed is derived from ``sha256(scenario_id)``, so
two runs are byte-identical on any machine (never Python's per-process-salted
``hash()``). CI renders twice and diffs to prove it.

Usage:

    python examples/render_examples.py            # render committed examples in place
    python examples/render_examples.py OUT_DIR    # render every WAV under OUT_DIR
                                                  # (mirrors the set layout; used by
                                                  #  the CI determinism diff, leaves the
                                                  #  committed fixtures untouched)

Audio layout, per scenario <id>:
    <id>.example.wav   stereo reference; channel 0 = caller, channel 1 = agent.
    <id>.caller.wav    mono caller stimulus (play into YOUR agent, then score
                       caller + your recording).
"""

from __future__ import annotations

import hashlib
import json
import math
import os
import random
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(HERE)

# Use the vendored engine's WAV writer so no upstream checkout is required and
# the bytes match the engine that scores them.
sys.path.insert(0, os.path.join(REPO, "src"))
from hotato._engine.audio import write_wav  # noqa: E402

# (scenarios_dir, audio_dir) relative to this file. Each set is rendered
# independently. funnel-demo is a deliberately-bad-agent battery (see README).
SETS = [
    (os.path.join(HERE, "scenarios"), os.path.join(HERE, "audio")),
    (os.path.join(HERE, "funnel-demo", "scenarios"), os.path.join(HERE, "funnel-demo", "audio")),
]


def render_channel(sample_rate, total_samples, segments, seed, continuous=False,
                   noise_floor_amp=0.0006):
    """Syllable-modulated band-limited noise inside each segment (short words
    with brief sub-hangover gaps, near-silence between segments), or, when
    ``continuous`` is set, one unbroken run per segment so the active-track
    boundaries equal the rendered segment boundaries to within one frame hop.
    ``noise_floor_amp`` sets the uniform noise floor's peak amplitude (default
    0.0006, about -69 dBFS RMS); raising it renders a noisier line with the
    same RNG draw order, so default renders stay byte-identical.

    Byte-for-byte identical to the upstream ``render_channel``."""
    rng = random.Random(seed)
    buf = [0.0] * total_samples
    lp = 0.0
    alpha = 0.55
    fade = max(1, int(0.004 * sample_rate))
    for (s, e) in segments:
        seg_start = max(0, int(s * sample_rate))
        seg_end = min(total_samples, int(e * sample_rate))
        if continuous:
            phase = rng.random() * math.tau
            for i in range(seg_start, seg_end):
                t = (i - seg_start) / sample_rate
                n = rng.uniform(-1.0, 1.0)
                lp = alpha * lp + (1.0 - alpha) * n
                colored = 0.7 * lp + 0.3 * (n - lp)
                env = 0.7 + 0.3 * math.sin(math.tau * 4.5 * t + phase)
                edge = min(1.0, (i - seg_start) / fade, (seg_end - i) / fade)
                buf[i] += 0.5 * env * edge * colored
            continue
        pos = seg_start
        while pos < seg_end:
            word_len = int(rng.uniform(0.22, 0.42) * sample_rate)
            gap_len = int(rng.uniform(0.03, 0.07) * sample_rate)
            wend = min(seg_end, pos + word_len)
            phase = rng.random() * math.tau
            for i in range(pos, wend):
                t = (i - seg_start) / sample_rate
                n = rng.uniform(-1.0, 1.0)
                lp = alpha * lp + (1.0 - alpha) * n
                colored = 0.7 * lp + 0.3 * (n - lp)
                env = 0.7 + 0.3 * math.sin(math.tau * 4.5 * t + phase)
                edge = min(1.0, (i - pos) / fade, (wend - i) / fade)
                buf[i] += 0.5 * env * edge * colored
            pos = wend + gap_len
    peak = max((abs(x) for x in buf), default=0.0)
    if peak > 0:
        scale = 0.6 / peak
        buf = [x * scale for x in buf]
    for i in range(total_samples):
        buf[i] += rng.uniform(-1.0, 1.0) * noise_floor_amp
    return buf


def build_scenario(scenario):
    sr = int(scenario["sample_rate"])
    total = int(round(scenario["duration_sec"] * sr))
    rr = scenario.get("reference_render", {})
    agent_seg = rr.get("agent_segments_sec", [])
    caller_seg = rr.get("caller_segments_sec", [])
    continuous = bool(rr.get("continuous", False))
    # Optional physical knobs, byte-identical defaults (mirrors upstream):
    # noise_floor_amp (+ per-channel overrides) and post-render channel gains.
    base_noise = float(rr.get("noise_floor_amp", 0.0006))
    agent_noise = float(rr.get("agent_noise_floor_amp", base_noise))
    caller_noise = float(rr.get("caller_noise_floor_amp", base_noise))
    caller_gain = float(rr.get("caller_gain", 1.0))
    agent_gain = float(rr.get("agent_gain", 1.0))
    scenario_id = scenario["id"]
    seed_base = int(hashlib.sha256(scenario_id.encode()).hexdigest()[:8], 16)
    agent = render_channel(sr, total, agent_seg, seed_base + 1, continuous=continuous,
                           noise_floor_amp=agent_noise)
    if agent_gain != 1.0:
        agent = [x * agent_gain for x in agent]
    if rr.get("caller_is_echo_of_agent"):
        delay = int(round(rr.get("echo_delay_sec", 0.12) * sr))
        gain = float(rr.get("echo_gain", 0.35))
        caller = [0.0] * total
        for i in range(total):
            j = i - delay
            if 0 <= j < total:
                caller[i] = agent[j] * gain
        rng = random.Random(seed_base + 99)
        for i in range(total):
            caller[i] += rng.uniform(-1.0, 1.0) * caller_noise
    else:
        caller = render_channel(sr, total, caller_seg, seed_base + 2, continuous=continuous,
                                noise_floor_amp=caller_noise)
    if caller_gain != 1.0:
        caller = [x * caller_gain for x in caller]
    return sr, caller, agent


def render_set(scenarios_dir, audio_dir, write_manifest=False):
    os.makedirs(audio_dir, exist_ok=True)
    written = []
    manifest = []
    for name in sorted(os.listdir(scenarios_dir)):
        if not name.endswith(".json") or name == "manifest.json":
            continue
        with open(os.path.join(scenarios_dir, name), "r", encoding="utf-8") as fh:
            scenario = json.load(fh)
        sr, caller, agent = build_scenario(scenario)
        example = os.path.join(audio_dir, scenario["id"] + ".example.wav")
        caller_only = os.path.join(audio_dir, scenario["id"] + ".caller.wav")
        write_wav(example, sr, [caller, agent])
        write_wav(caller_only, sr, [caller])
        written.append(scenario["id"])
        manifest.append(
            {
                "id": scenario["id"],
                "title": scenario["title"],
                "category": scenario["category"],
                "sample_rate": scenario["sample_rate"],
                "expected_yield": scenario.get("expected", {}).get("yield"),
                "example_wav": f"audio/{scenario['id']}.example.wav",
                "caller_wav": f"audio/{scenario['id']}.caller.wav",
            }
        )
        print(f"  wrote {os.path.basename(example)} and {os.path.basename(caller_only)} ({sr} Hz)")
    # Only for in-place renders: keep a machine-readable index next to the labels.
    # Skipped for temp/CI renders so the committed scenarios are never touched.
    if write_manifest:
        with open(os.path.join(scenarios_dir, "manifest.json"), "w", encoding="utf-8") as fh:
            json.dump({"scenarios": manifest}, fh, indent=2)
    return written


def main(argv=None):
    argv = sys.argv[1:] if argv is None else argv
    out_root = argv[0] if argv else None
    total = 0
    for scenarios_dir, audio_dir in SETS:
        if out_root:
            # mirror the set folder name under out_root, leaving committed audio alone
            audio_dir = os.path.join(out_root, os.path.basename(os.path.dirname(scenarios_dir)) or "root",
                                     os.path.basename(audio_dir))
        total += len(render_set(scenarios_dir, audio_dir, write_manifest=out_root is None))
    print(f"\nRendered {total} example scenarios"
          + (f" into {out_root}" if out_root else " into examples/"))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
