"""Deterministic synthetic acoustic perturbations for robustness testing.

Start with deterministic transforms of REAL fixtures, not a generative speech
program (plan §11). Each derived clip carries its parent hash, transform recipe,
seed, tool+version, output hashes, and an explicit SYNTHETIC designation, so a
thousand generated perturbations can never be mistaken for -- or raise the
evidentiary confidence of -- one real recapture. Synthetic and real stay on
separate report axes.

Zero-dependency: stdlib ``wave``/``struct``/``math`` only, mirroring the numeric
-audit generators.
"""
from __future__ import annotations

import hashlib
import math
import os
import struct
import wave
from typing import List

from . import __version__
from .errors import open_regular as _open_regular
from .errors import wav_read as _wav_read

SCHEMA_VERSION = "1"
TOOL = f"hotato-synth/{__version__}"


# --- WAV read/write (stdlib) ----------------------------------------------
def _read(path: str):
    with _wav_read(path) as wf:
        nch, width, rate, n = (wf.getnchannels(), wf.getsampwidth(),
                               wf.getframerate(), wf.getnframes())
        raw = wf.readframes(n)
    if width != 2:
        raise ValueError("synth supports 16-bit PCM WAV only")
    samples = list(struct.unpack("<%dh" % (len(raw) // 2), raw))
    chans = [samples[c::nch] for c in range(nch)]
    return chans, rate


def _write(path: str, chans: List[List[int]], rate: int):
    nch = len(chans)
    n = len(chans[0])
    inter = bytearray()
    for i in range(n):
        for c in range(nch):
            v = max(-32768, min(32767, int(chans[c][i])))
            inter += struct.pack("<h", v)
    with wave.open(path, "wb") as wf:
        wf.setnchannels(nch)
        wf.setsampwidth(2)
        wf.setframerate(rate)
        wf.writeframes(bytes(inter))


def _pcm_sha256(path: str) -> str:
    h = hashlib.sha256()
    with _wav_read(path) as wf:
        while True:
            chunk = wf.readframes(1 << 16)
            if not chunk:
                break
            h.update(chunk)
    return h.hexdigest()


def _raw_sha256(path: str) -> str:
    h = hashlib.sha256()
    with _open_regular(path) as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _lcg(seed: int):
    """Deterministic PRNG (no Math.random / os.urandom): a linear congruential
    generator so a seed always reproduces the same noise."""
    state = seed & 0xFFFFFFFF
    while True:
        state = (1103515245 * state + 12345) & 0x7FFFFFFF
        yield (state / 0x3FFFFFFF) - 1.0   # in [-1, 1)


# --- transforms -----------------------------------------------------------
def _resample_nearest(ch: List[int], src_rate: int, dst_rate: int) -> List[int]:
    if src_rate == dst_rate:
        return list(ch)
    n_out = int(len(ch) * dst_rate / src_rate)
    return [ch[min(len(ch) - 1, int(i * src_rate / dst_rate))] for i in range(n_out)]


def _apply(chans, rate, recipe: dict, seed: int):
    kind = recipe["transform"]
    out = [list(c) for c in chans]
    out_rate = rate
    if kind == "resample":
        dst = int(recipe["rate"])
        out = [_resample_nearest(c, rate, dst) for c in out]
        out_rate = dst
    elif kind == "gain":
        g = float(recipe["gain_db"])
        f = 10 ** (g / 20.0)
        out = [[int(s * f) for s in c] for c in out]
    elif kind == "noise":
        snr = float(recipe["snr_db"])
        amp = 32767 * (10 ** (-snr / 20.0))
        rng = _lcg(seed)
        out = [[int(s + amp * next(rng)) for s in c] for c in out]
    elif kind == "leakage":
        # add a delayed, attenuated copy of the caller channel onto the agent one
        db = float(recipe["leak_db"]); lag = int(float(recipe.get("lag_sec", 0.2)) * rate)
        f = 10 ** (db / 20.0)
        if len(out) >= 2:
            src = out[0]
            agent = out[1]
            for i in range(len(agent)):
                j = i - lag
                if 0 <= j < len(src):
                    agent[i] = int(agent[i] + f * src[j])
    elif kind == "invert_channels":
        if len(out) >= 2:
            out[0], out[1] = out[1], out[0]
    elif kind == "leading_silence":
        pad = int(float(recipe["seconds"]) * rate)
        out = [[0] * pad + c for c in out]
    elif kind == "trailing_silence":
        pad = int(float(recipe["seconds"]) * rate)
        out = [c + [0] * pad for c in out]
    elif kind == "onset_offset":
        shift = int(float(recipe["seconds"]) * rate)
        out = [[0] * shift + c[:-shift] if shift > 0 and shift < len(c) else list(c) for c in out]
    elif kind == "clip":
        ceil = int(32767 * float(recipe.get("ceiling", 0.5)))
        out = [[max(-ceil, min(ceil, s)) for s in c] for c in out]
    elif kind == "backchannel":
        # add a short caller-side utterance (backchannel) of a swept duration:
        # a fixed tone burst on the caller channel (out[0]) so a scorer sees a
        # brief overlap of the declared length. Deterministic (no PRNG).
        dur = float(recipe["duration_sec"]); at = float(recipe.get("at_sec", 0.0))
        freq = float(recipe.get("freq", 300.0)); amp = float(recipe.get("amp", 8000))
        caller = out[0]
        start = int(at * rate); length = int(dur * rate)
        for k in range(length):
            i = start + k
            if 0 <= i < len(caller):
                caller[i] = int(max(-32768, min(32767,
                    caller[i] + amp * math.sin(2 * math.pi * freq * k / rate))))
    elif kind == "agent_gap":
        # sweep a silence gap into the AGENT channel around a hangover boundary:
        # zero a window of the declared length so the agent goes briefly quiet.
        gap = float(recipe["gap_sec"]); at = float(recipe.get("at_sec", 0.0))
        agent = out[-1]
        start = int(at * rate); length = int(gap * rate)
        for k in range(length):
            i = start + k
            if 0 <= i < len(agent):
                agent[i] = 0
    elif kind == "packet_gap":
        # simulate periodic packet loss: zero a ``gap_ms`` window every
        # ``period_ms`` across every channel. Deterministic, length-preserving.
        gap_ms = float(recipe["gap_ms"]); period_ms = float(recipe.get("period_ms", 200.0))
        gap_n = int(gap_ms * rate / 1000.0)
        period_n = max(1, int(period_ms * rate / 1000.0))
        for c in out:
            i = 0
            while i < len(c):
                for k in range(gap_n):
                    if i + k < len(c):
                        c[i + k] = 0
                i += period_n
    elif kind == "dropout":
        # ``count`` dropout gaps of ``gap_ms`` each at SEEDED positions: the
        # LCG draws every window start, so one seed always drops the same
        # windows. Zeroed on every channel; length-preserving.
        count = int(recipe.get("count", 3))
        gap_ms = float(recipe["gap_ms"])
        gap_n = max(1, int(gap_ms * rate / 1000.0))
        rng = _lcg(seed)
        span = max(1, len(out[0]) - gap_n)
        for _ in range(count):
            start = int(((next(rng) + 1.0) / 2.0) * span)
            for c in out:
                for k in range(gap_n):
                    if start + k < len(c):
                        c[start + k] = 0
    elif kind == "jitter_resample":
        # constant clock-skew jitter: nearest-neighbour resample by a fixed
        # ``factor`` while KEEPING the declared sample rate, so the whole
        # timeline stretches (factor > 1) or compresses (factor < 1)
        # uniformly. Deterministic; no PRNG.
        factor = float(recipe["factor"])
        if factor <= 0:
            raise ValueError("jitter_resample factor must be > 0")
        out = [[c[min(len(c) - 1, int(i / factor))]
                for i in range(int(len(c) * factor))] for c in out]
    else:
        raise ValueError(f"unknown transform {kind!r}")
    return out, out_rate


def perturb(source_wav: str, recipe: dict, *, out_path: str, seed: int = 1) -> dict:
    """Apply one transform to a real fixture and write a derived clip plus a
    provenance block (never mutating the source). Returns the provenance."""
    chans, rate = _read(source_wav)
    parent_pcm = _pcm_sha256(source_wav)
    out_chans, out_rate = _apply(chans, rate, recipe, seed)
    _write(out_path, out_chans, out_rate)
    return {
        "schema_version": SCHEMA_VERSION,
        "synthetic": True,                 # explicit: NEVER a real recording
        "axis": "synthetic",               # a SEPARATE report axis, never blended
        "designation": "synthetic-derived",
        "tool": TOOL,
        "seed": seed,
        "recipe": recipe,
        "parent": {"path": os.path.basename(source_wav), "pcm_sha256": parent_pcm},
        "output": {"path": os.path.basename(out_path),
                   "pcm_sha256": _pcm_sha256(out_path),
                   "raw_sha256": _raw_sha256(out_path),
                   "sample_rate": out_rate},
    }


def default_matrix() -> List[dict]:
    """A compact, documented perturbation matrix (plan §11 'fast version')."""
    m = []
    for r in (8000, 16000, 48000):
        m.append({"transform": "resample", "rate": r})
    for g in (-6, -12, -25, -48):
        m.append({"transform": "gain", "gain_db": g})
    for snr in (30, 20, 10):
        m.append({"transform": "noise", "snr_db": snr})
    for db in (-50, -40, -30):
        m.append({"transform": "leakage", "leak_db": db, "lag_sec": 0.2})
    m.append({"transform": "invert_channels"})
    for s in (0.5, 2.0):
        m.append({"transform": "leading_silence", "seconds": s})
    for s in (0.5, 2.0):
        m.append({"transform": "trailing_silence", "seconds": s})
    for s in (0.01, 0.02):
        m.append({"transform": "onset_offset", "seconds": s})
    m.append({"transform": "clip", "ceiling": 0.5})
    # backchannel duration sweep (caller-side brief utterances).
    for d in (0.1, 0.3, 0.6):
        m.append({"transform": "backchannel", "duration_sec": d, "at_sec": 2.0})
    # agent silence-gap sweep around hangover boundaries.
    for g in (0.05, 0.1, 0.2):
        m.append({"transform": "agent_gap", "gap_sec": g, "at_sec": 2.0})
    # packet-gap simulation (periodic loss).
    for gm in (20, 40):
        m.append({"transform": "packet_gap", "gap_ms": gm, "period_ms": 200})
    return m


def synth_battery(source_wav: str, out_dir: str, *, seed: int = 1) -> List[dict]:
    """Run the full :func:`default_matrix` over ONE real fixture and return a list
    of derived-clip provenance records.

    Each record is explicitly tagged ``axis="synthetic"`` and designation
    ``"synthetic-derived"`` so a caller can render these on a SEPARATE report axis
    -- never blended with real-call results (a thousand generated perturbations
    must never raise the evidentiary confidence of one real recapture, plan §11).
    Every record keeps the parent PCM hash, the transform recipe, the seed, the
    tool+version, and the derived output hashes. Fully deterministic (the LCG PRNG
    plus a fixed matrix and fixed clip names; no ``os.urandom``)."""
    os.makedirs(out_dir, exist_ok=True)
    records: List[dict] = []
    for i, recipe in enumerate(default_matrix()):
        name = f"synthetic-{i:03d}-{recipe['transform']}.wav"
        prov = perturb(source_wav, recipe, out_path=os.path.join(out_dir, name),
                       seed=seed)
        prov["axis"] = "synthetic"            # explicit separate axis
        prov["designation"] = "synthetic-derived"
        records.append(prov)
    return records


# --- degradation/robustness battery ---------------------------------------
def robustness_stages(*, snrs=(20, 10, 5), clip_ceiling=0.5, dropout_count=3,
                      dropout_gap_ms=60.0, jitter_factor=1.005) -> List[dict]:
    """The staged degradation ladder: a clean baseline, additive noise at
    stepped SNRs, hard clipping at a ceiling fraction, seeded dropout gaps,
    and a constant clock-skew resample. Fixed stage names; every stage is
    deterministic for a fixed seed."""
    stages: List[dict] = [{"stage": "baseline", "recipe": None}]
    for snr in snrs:
        stages.append({"stage": f"noise-snr{int(snr)}db",
                       "recipe": {"transform": "noise", "snr_db": snr}})
    stages.append({"stage": f"clip-{clip_ceiling:g}",
                   "recipe": {"transform": "clip", "ceiling": clip_ceiling}})
    stages.append({"stage": f"dropout-{int(dropout_count)}x{dropout_gap_ms:g}ms",
                   "recipe": {"transform": "dropout", "count": dropout_count,
                              "gap_ms": dropout_gap_ms}})
    stages.append({"stage": f"jitter-{jitter_factor:g}x",
                   "recipe": {"transform": "jitter_resample",
                              "factor": jitter_factor}})
    return stages


def _stage_metrics(event: dict) -> dict:
    v = event.get("verdict") or {}
    lat = (event.get("signals") or {}).get("latency") or {}
    return {
        "did_yield": v.get("did_yield"),
        "seconds_to_yield": v.get("seconds_to_yield"),
        "talk_over_sec": v.get("talk_over_sec"),
        "response_gap_sec": lat.get("response_gap_sec"),
    }


def _metric_delta(base: dict, cur: dict) -> dict:
    def _d(key):
        a, b = base.get(key), cur.get(key)
        if a is None or b is None:
            return None
        return round(b - a, 6)
    flipped = (base.get("did_yield") is not None
               and cur.get("did_yield") is not None
               and bool(base["did_yield"]) != bool(cur["did_yield"]))
    return {
        "did_yield_flipped": flipped,
        "seconds_to_yield_delta": _d("seconds_to_yield"),
        "talk_over_sec_delta": _d("talk_over_sec"),
        "response_gap_sec_delta": _d("response_gap_sec"),
    }


def robustness_battery(source_wav: str, out_dir: str, *, seed: int = 1,
                       onset_sec=None, expect: str = "yield",
                       stages=None) -> dict:
    """Render the staged degradation ladder over ONE recording, score every
    stage with the SAME scorer and the SAME labels (onset/expect), and report
    how did_yield / talk_over / response_gap moved against the clean baseline.

    Byte-reproducible for a fixed seed: the LCG fixes the noise samples and
    the dropout positions, the stage names and clip filenames are fixed, and
    the scorer is deterministic. Derived clips carry the same synthetic
    provenance as :func:`perturb` on the SEPARATE synthetic axis. The movement
    table measures scorer stability under degradation -- timing signal only,
    never agent intent."""
    from . import core as _core  # deferred: synth stays stdlib-only at import

    stage_list = list(stages) if stages is not None else robustness_stages()
    if not stage_list or stage_list[0].get("recipe") is not None:
        raise ValueError("robustness battery needs a leading baseline stage")
    os.makedirs(out_dir, exist_ok=True)
    rows: List[dict] = []
    baseline_metrics = None
    for i, st in enumerate(stage_list):
        recipe = st.get("recipe")
        if recipe is None:
            path, prov = source_wav, None
        else:
            path = os.path.join(out_dir, f"robustness-{i:02d}-{st['stage']}.wav")
            prov = perturb(source_wav, recipe, out_path=path, seed=seed)
        env = _core.run_single(stereo=path, onset_sec=onset_sec, expect=expect)
        event = env["events"][0]
        scorable = event.get("scorable", True) is not False
        metrics = _stage_metrics(event)
        row = {
            "stage": st["stage"],
            "recipe": recipe,
            "clip": None if prov is None else prov["output"],
            "scorable": scorable,
            "metrics": metrics,
            "delta": None,
        }
        if not scorable:
            row["not_scorable_reason"] = event.get("not_scorable_reason")
        if recipe is None:
            if not scorable:
                raise ValueError(
                    "the baseline recording is not scorable, so there is no "
                    "reference to measure stage movement against: "
                    f"{event.get('not_scorable_reason')}"
                )
            baseline_metrics = metrics
        elif scorable:
            row["delta"] = _metric_delta(baseline_metrics, metrics)
        rows.append(row)
    return {
        "schema_version": SCHEMA_VERSION,
        "synthetic": True,                 # explicit: NEVER a real recording
        "axis": "synthetic",               # a SEPARATE report axis, never blended
        "designation": "synthetic-derived",
        "tool": TOOL,
        "seed": seed,
        "expect": expect,
        "onset_sec": onset_sec,
        "source": {"path": os.path.basename(source_wav),
                   "pcm_sha256": _pcm_sha256(source_wav)},
        "stages": rows,
        "summary": {
            "stage_count": len(rows),
            "did_yield_flipped_stages": [
                r["stage"] for r in rows
                if r["delta"] and r["delta"]["did_yield_flipped"]],
            "not_scorable_stages": [
                r["stage"] for r in rows if not r["scorable"]],
        },
    }


__all__ = ["perturb", "default_matrix", "synth_battery", "robustness_stages",
           "robustness_battery", "SCHEMA_VERSION", "TOOL"]
