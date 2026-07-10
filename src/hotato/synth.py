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
from typing import List, Optional

from . import __version__

SCHEMA_VERSION = "1"
TOOL = f"hotato-synth/{__version__}"


# --- WAV read/write (stdlib) ----------------------------------------------
def _read(path: str):
    with wave.open(path, "rb") as wf:
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
    with wave.open(path, "rb") as wf:
        while True:
            chunk = wf.readframes(1 << 16)
            if not chunk:
                break
            h.update(chunk)
    return h.hexdigest()


def _raw_sha256(path: str) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
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
    for s in (0.01, 0.02):
        m.append({"transform": "onset_offset", "seconds": s})
    m.append({"transform": "clip", "ceiling": 0.5})
    return m


__all__ = ["perturb", "default_matrix", "SCHEMA_VERSION", "TOOL"]
