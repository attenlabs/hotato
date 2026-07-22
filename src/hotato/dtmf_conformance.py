"""DTMF conformance: a "DTMF sent" claim is conformant only when the tones are
audibly present in the DELIVERED audio at the claimed time.

This is the evidence-over-logs move applied to DTMF. hotato already refuses to
take an agent's word that a tool ran -- the ``tool_call`` assertion counts only
ingested trace spans. The same failure class exists for DTMF: a tool log can
record digits as sent while the far-end line never carries the tones. This
module decides the question the recording can answer -- are the two
DTMF frequencies for each claimed digit present in the delivered audio within
the claimed window -- with a pure-stdlib Goertzel filter over the standard 4x4
tone table.

Functional scope, stated plainly: the check measures tone PRESENCE in the
recording it is given, within the window the claim states. It measures timing
and say-do (was the tone there, when the claim says it was), not intent, and
not what the far end did with the tones -- delivered-audio presence is the
strongest claim a single recording supports.

Everything here is deterministic pure-stdlib ``math``: the same samples and
threshold reproduce every energy and every per-digit verdict on any machine.
"""

from __future__ import annotations

import math
from typing import Any, Dict, List, Optional, Sequence, Tuple

__all__ = [
    "DTMF_ROW_FREQS",
    "DTMF_COL_FREQS",
    "DTMF_DIGITS",
    "DEFAULT_ENERGY_THRESHOLD",
    "SILENCE_FLOOR",
    "goertzel_power",
    "digit_frequencies",
    "detect_digit_presence",
    "check_dtmf_conformance",
    "check_dtmf_conformance_wav",
]

# The standard DTMF row (low group) and column (high group) frequencies in Hz.
# A digit is exactly one row tone plus one column tone; the 4x4 grid names the
# 16 symbols 0-9, A-D, ``*`` and ``#``. This table is public (ITU-T Q.23).
DTMF_ROW_FREQS: Tuple[int, int, int, int] = (697, 770, 852, 941)
DTMF_COL_FREQS: Tuple[int, int, int, int] = (1209, 1336, 1477, 1633)

_DTMF_GRID = (
    ("1", "2", "3", "A"),
    ("4", "5", "6", "B"),
    ("7", "8", "9", "C"),
    ("*", "0", "#", "D"),
)

# digit -> (row_freq_hz, col_freq_hz)
DTMF_DIGITS: Dict[str, Tuple[int, int]] = {}
for _r, _rf in enumerate(DTMF_ROW_FREQS):
    for _c, _cf in enumerate(DTMF_COL_FREQS):
        DTMF_DIGITS[_DTMF_GRID[_r][_c]] = (_rf, _cf)

# A digit is present when the energy fraction in BOTH of its tone bins clears
# this threshold. The measure is scale-invariant relative bin energy:
# ``goertzel_power(f) / (n * sum(x^2))``, the share of the window's total energy
# in bin f (Parseval). A single pure sine puts ~0.5 there; a digit's two
# equal-amplitude sines put ~0.25 in EACH bin, so 0.06 clears a two-tone digit with
# wide margin while rejecting silence, single tones, and broadband energy.
DEFAULT_ENERGY_THRESHOLD: float = 0.06

# A slot whose mean-square energy is at or below this floor carries no audio to
# measure a tone from: the two frequencies cannot be present, so the claim is
# not supported (FAIL, not INCONCLUSIVE -- the slot WAS in range and readable).
SILENCE_FLOOR: float = 1e-9


def digit_frequencies(digit: str) -> Tuple[int, int]:
    """The (row, column) frequency pair for a single DTMF digit.

    ``digit`` is case-insensitive for the letter keys A-D. Raises ``ValueError``
    for any symbol outside the 16-key table.
    """
    key = digit.upper() if digit.isalpha() else digit
    if key not in DTMF_DIGITS:
        raise ValueError(f"{digit!r} is not a DTMF symbol (expected 0-9, A-D, * or #)")
    return DTMF_DIGITS[key]


def goertzel_power(samples: Sequence[float], sample_rate: int, freq: float) -> float:
    """Goertzel estimate of ``|X_k|^2`` at the bin nearest ``freq`` over
    ``samples`` (single frequency, no full FFT). Pure stdlib, deterministic.

    Returns the unnormalized bin power; callers normalize by ``n * sum(x^2)``
    to get the scale-invariant energy fraction.
    """
    n = len(samples)
    if n == 0 or sample_rate <= 0:
        return 0.0
    k = int(round(n * freq / sample_rate))
    omega = 2.0 * math.pi * k / n
    coeff = 2.0 * math.cos(omega)
    s_prev = 0.0
    s_prev2 = 0.0
    for x in samples:
        s = x + coeff * s_prev - s_prev2
        s_prev2 = s_prev
        s_prev = s
    return s_prev2 * s_prev2 + s_prev * s_prev - coeff * s_prev * s_prev2


def _relative_bin_energy(
    samples: Sequence[float], sample_rate: int, freq: float, total_energy: float
) -> float:
    """Share of the window's total energy in the bin nearest ``freq`` (0..1)."""
    n = len(samples)
    if n == 0 or total_energy <= 0.0:
        return 0.0
    return goertzel_power(samples, sample_rate, freq) / (n * total_energy)


def detect_digit_presence(
    samples: Sequence[float],
    sample_rate: int,
    digit: str,
    threshold: float = DEFAULT_ENERGY_THRESHOLD,
) -> Dict[str, Any]:
    """Decide whether one DTMF digit's two frequencies are present in a slot of
    delivered audio.

    Result shape (one dict per digit): ``status`` is ``"PASS"`` (both tone bins
    clear ``threshold``), ``"FAIL"`` (the slot is readable but the tones are not
    present), or ``"INCONCLUSIVE"`` (no samples to measure -- the claimed window
    fell outside the recording). ``row_freq`` / ``col_freq`` are the expected
    frequencies and ``row_energy`` / ``col_energy`` the measured relative bin
    energies, the evidence behind the verdict.
    """
    row_f, col_f = digit_frequencies(digit)
    n = len(samples)
    base: Dict[str, Any] = {
        "digit": digit,
        "row_freq": row_f,
        "col_freq": col_f,
        "threshold": threshold,
    }
    if n == 0:
        base.update(status="INCONCLUSIVE", row_energy=None, col_energy=None,
                    reason="the claimed window has no samples in the recording")
        return base
    total_energy = math.fsum(x * x for x in samples) / n
    if total_energy <= SILENCE_FLOOR:
        base.update(status="FAIL", row_energy=0.0, col_energy=0.0,
                    reason="the slot carries no energy; the claimed tones are absent")
        return base
    total_sq = total_energy * n
    row_e = _relative_bin_energy(samples, sample_rate, row_f, total_sq)
    col_e = _relative_bin_energy(samples, sample_rate, col_f, total_sq)
    present = row_e >= threshold and col_e >= threshold
    base.update(status="PASS" if present else "FAIL", row_energy=row_e, col_energy=col_e)
    if not present:
        base["reason"] = (
            f"tone energy below threshold {threshold:g} "
            f"(row {row_f}Hz={row_e:.4f}, col {col_f}Hz={col_e:.4f})"
        )
    return base


def check_dtmf_conformance(
    samples: Sequence[float],
    sample_rate: int,
    digits: str,
    window_start_sec: float,
    window_end_sec: float,
    threshold: float = DEFAULT_ENERGY_THRESHOLD,
) -> Dict[str, Any]:
    """Check that every digit of ``digits`` is audibly present, in order, within
    the claimed send window ``[window_start_sec, window_end_sec)``.

    Window semantics: the window is split into ``len(digits)`` equal, adjacent
    slots (slot i carries digit i); each slot is judged independently by
    :func:`detect_digit_presence`. Overall ``status`` is ``"FAIL"`` if any digit
    fails, else ``"INCONCLUSIVE"`` if any digit could not be measured, else
    ``"PASS"``. Per-digit results carry the measured relative bin energies as
    evidence.
    """
    if not digits:
        raise ValueError("digits must be a non-empty DTMF string")
    if window_end_sec <= window_start_sec:
        raise ValueError("window_end_sec must be greater than window_start_sec")
    for d in digits:
        digit_frequencies(d)  # validate up front

    n_total = len(samples)
    slot_sec = (window_end_sec - window_start_sec) / len(digits)
    per_digit: List[Dict[str, Any]] = []
    for i, d in enumerate(digits):
        slot_start = window_start_sec + i * slot_sec
        slot_end = window_start_sec + (i + 1) * slot_sec
        lo = max(0, int(math.floor(slot_start * sample_rate)))
        hi = min(n_total, int(math.floor(slot_end * sample_rate)))
        slot = samples[lo:hi] if hi > lo else []
        res = detect_digit_presence(slot, sample_rate, d, threshold)
        res["index"] = i
        res["window"] = [slot_start, slot_end]
        per_digit.append(res)

    if any(r["status"] == "FAIL" for r in per_digit):
        status = "FAIL"
    elif any(r["status"] == "INCONCLUSIVE" for r in per_digit):
        status = "INCONCLUSIVE"
    else:
        status = "PASS"
    return {
        "status": status,
        "digits": digits,
        "window": [window_start_sec, window_end_sec],
        "threshold": threshold,
        "sample_rate": sample_rate,
        "per_digit": per_digit,
    }


def check_dtmf_conformance_wav(
    wav_path: str,
    channel: int,
    digits: str,
    window_start_sec: float,
    window_end_sec: float,
    threshold: float = DEFAULT_ENERGY_THRESHOLD,
) -> Dict[str, Any]:
    """Read one channel of a delivered-audio WAV and run
    :func:`check_dtmf_conformance` on it. Uses hotato's hardened WAV reader
    (``core._read_wav``), the same decode ``run``/``trust``/``scan`` read.
    """
    from .core import _read_wav, _require_channel

    signal = _read_wav(wav_path)
    _require_channel(signal, channel, "channel")
    samples = signal.get(channel)
    result = check_dtmf_conformance(
        samples, signal.sample_rate, digits, window_start_sec, window_end_sec, threshold
    )
    result["wav_path"] = wav_path
    result["channel"] = channel
    return result
