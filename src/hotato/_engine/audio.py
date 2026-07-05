"""Minimal WAV reading and framing. Standard library only.

No third-party dependencies are required. PCM WAV at 8, 16, or 32 bit,
mono or multi-channel, is supported. If numpy happens to be importable it
is used to speed up the per-frame RMS, but it is never required and the
results are identical without it.
"""

from __future__ import annotations

import array
import math
import sys
import wave
from dataclasses import dataclass
from typing import List, Optional, Tuple

try:  # optional acceleration only
    import numpy as _np
except Exception:  # pragma: no cover - numpy is genuinely optional
    _np = None


@dataclass
class Signal:
    """A decoded multi-channel signal with samples in the range [-1, 1]."""

    sample_rate: int
    channels: List[List[float]]

    @property
    def num_channels(self) -> int:
        return len(self.channels)

    @property
    def num_samples(self) -> int:
        return len(self.channels[0]) if self.channels else 0

    @property
    def duration_sec(self) -> float:
        return self.num_samples / self.sample_rate if self.sample_rate else 0.0

    def get(self, index: int) -> List[float]:
        if index < 0 or index >= self.num_channels:
            raise IndexError(
                f"channel {index} out of range for {self.num_channels}-channel audio"
            )
        return self.channels[index]


def read_wav(path: str) -> Signal:
    """Read a PCM WAV file into a Signal with float samples in [-1, 1]."""
    with wave.open(path, "rb") as wf:
        n_channels = wf.getnchannels()
        sampwidth = wf.getsampwidth()
        sample_rate = wf.getframerate()
        n_frames = wf.getnframes()
        raw = wf.readframes(n_frames)

    if sampwidth == 1:
        # 8-bit PCM is unsigned with a midpoint of 128.
        a = array.array("B")
        a.frombytes(raw)
        samples = [(x - 128) / 128.0 for x in a]
    elif sampwidth == 2:
        a = array.array("h")
        a.frombytes(raw)
        if sys.byteorder == "big":  # WAV is little-endian
            a.byteswap()
        samples = [x / 32768.0 for x in a]
    elif sampwidth == 4:
        a = array.array("i")
        a.frombytes(raw)
        if sys.byteorder == "big":
            a.byteswap()
        samples = [x / 2147483648.0 for x in a]
    else:
        raise ValueError(
            f"unsupported sample width {sampwidth * 8}-bit; "
            "please convert to 16-bit PCM (for example with ffmpeg -acodec pcm_s16le)"
        )

    channels = [samples[ch::n_channels] for ch in range(n_channels)]
    return Signal(sample_rate=sample_rate, channels=channels)


def write_wav(path: str, sample_rate: int, channels: List[List[float]]) -> None:
    """Write float channels in [-1, 1] to a 16-bit PCM WAV file."""
    n_channels = len(channels)
    n_samples = len(channels[0]) if channels else 0
    interleaved = array.array("h", bytes(2 * n_channels * n_samples))
    for ch in range(n_channels):
        data = channels[ch]
        for i in range(n_samples):
            v = data[i]
            if v > 1.0:
                v = 1.0
            elif v < -1.0:
                v = -1.0
            interleaved[i * n_channels + ch] = int(round(v * 32767.0))
    if sys.byteorder == "big":
        interleaved.byteswap()
    with wave.open(path, "wb") as wf:
        wf.setnchannels(n_channels)
        wf.setsampwidth(2)
        wf.setframerate(sample_rate)
        wf.writeframes(interleaved.tobytes())


def frame_rms(
    samples: List[float],
    sample_rate: int,
    frame_ms: float = 20.0,
    hop_ms: float = 10.0,
) -> Tuple[List[float], float]:
    """Return per-frame linear RMS and the hop length in seconds."""
    frame_len = max(1, int(round(sample_rate * frame_ms / 1000.0)))
    hop = max(1, int(round(sample_rate * hop_ms / 1000.0)))
    hop_sec = hop / sample_rate
    n = len(samples)
    rms: List[float] = []
    i = 0
    if _np is not None:
        arr = _np.asarray(samples, dtype=_np.float64)
        while i < n:
            seg = arr[i : i + frame_len]
            rms.append(float(_np.sqrt(_np.mean(seg * seg))) if seg.size else 0.0)
            i += hop
    else:
        while i < n:
            seg = samples[i : i + frame_len]
            if seg:
                acc = 0.0
                for x in seg:
                    acc += x * x
                rms.append((acc / len(seg)) ** 0.5)
            else:
                rms.append(0.0)
            i += hop
    return rms, hop_sec


def to_dbfs(rms: List[float], floor_db: float = -120.0) -> List[float]:
    """Convert linear RMS to dBFS with a floor to avoid log(0)."""
    out = []
    lin_floor = 10 ** (floor_db / 20.0)
    for r in rms:
        out.append(20.0 * math.log10(r if r > lin_floor else lin_floor))
    return out
