"""Deterministic telephony-line degradations used ONLY by the
telephony-degraded corpus class.

Two effects, both pure and stdlib-only, applied to float samples in [-1, 1]:

  mu_law_round_trip   G.711 mu-law companding (ITU-T mathematical definition,
                       compress -> 8-bit quantize -> expand), the standard
                       distortion an analog phone line's codec introduces.
  apply_packet_loss    a fixed, non-random schedule of short zeroed windows
                       ("mild" packet loss: short, infrequent drops), the same
                       on every machine because it is a fixed schedule, not a
                       random draw.

Neither function touches the vendored engine or the shared render path in
``examples/render_examples.py``; they are a POST-processing step applied only
to the telephony-degraded scenarios, after the normal deterministic render.
"""

from __future__ import annotations

import math
from typing import List

MU = 255.0
_LN1P_MU = math.log1p(MU)
_LEVELS = 256           # standard mu-law codeword count
_HALF = _LEVELS // 2    # 128 usable steps per sign, like the real codec


def mu_law_round_trip(samples: List[float]) -> List[float]:
    """Compress each sample through the ITU-T G.711 mu-law curve, quantize to
    the codec's 8-bit resolution, then expand back to linear. Deterministic,
    no RNG. This is the standard mathematical mu-law formula, not a lookup
    table lifted from any specific codebase:

        compress:  y = sign(x) * ln(1 + mu*|x|) / ln(1 + mu)
        expand:    x = sign(y) * (exp(|y| * ln(1 + mu)) - 1) / mu
    """
    out = []
    for x in samples:
        if x > 1.0:
            x = 1.0
        elif x < -1.0:
            x = -1.0
        if x == 0.0:
            out.append(0.0)
            continue
        sign = 1.0 if x > 0 else -1.0
        comp = sign * math.log1p(MU * abs(x)) / _LN1P_MU
        # quantize to the codec's 8-bit resolution (256 codewords)
        step = round(comp * (_HALF - 1))
        step = max(-(_HALF - 1), min(_HALF - 1, step))
        q = step / (_HALF - 1)
        if q == 0.0:
            out.append(0.0)
            continue
        qsign = 1.0 if q > 0 else -1.0
        lin = qsign * math.expm1(abs(q) * _LN1P_MU) / MU
        out.append(lin)
    return out


def apply_packet_loss(
    samples: List[float],
    sample_rate: int,
    loss_ms: float = 20.0,
    period_ms: float = 650.0,
    offset_ms: float = 140.0,
) -> List[float]:
    """Zero fixed-length windows on a fixed schedule: ``loss_ms`` dropped every
    ``period_ms``, starting ``offset_ms`` in. A short, infrequent drop pattern
    ("mild"), the same schedule on every machine (no randomness)."""
    n = len(samples)
    loss_len = max(1, int(round(loss_ms * sample_rate / 1000.0)))
    period_len = max(1, int(round(period_ms * sample_rate / 1000.0)))
    offset = max(0, int(round(offset_ms * sample_rate / 1000.0)))
    out = list(samples)
    i = offset
    while i < n:
        end = min(n, i + loss_len)
        for k in range(i, end):
            out[k] = 0.0
        i += period_len
    return out


def degrade_telephony(samples: List[float], sample_rate: int) -> List[float]:
    """The combined 8 kHz telephony-line render: mu-law round trip, then mild
    packet loss. Applied identically to every degraded channel."""
    return apply_packet_loss(mu_law_round_trip(samples), sample_rate)
