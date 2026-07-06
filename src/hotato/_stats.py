"""Tiny stdlib-only distribution helpers shared by the report and team modes.

Deterministic, documented definitions so every published number is re-derivable
by hand:

* mean   = arithmetic mean (``statistics.fmean``)
* median = ``statistics.median``
* p90    = linear interpolation between closest ranks (the definition numpy
           calls "linear"): with the values sorted ascending and n values,
           pos = 0.9 * (n - 1); p90 = v[floor(pos)] + frac * (v[floor(pos)+1]
           - v[floor(pos)]).

Empty input returns None, never a fabricated number.
"""

from __future__ import annotations

import math
import statistics
from typing import Optional


def percentile(values, q: float) -> Optional[float]:
    """Linear-interpolation percentile of ``values`` (q in 0..1)."""
    if not values:
        return None
    v = sorted(values)
    pos = q * (len(v) - 1)
    lo = math.floor(pos)
    hi = min(lo + 1, len(v) - 1)
    frac = pos - lo
    return v[lo] + (v[hi] - v[lo]) * frac


def dist_summary(values) -> Optional[dict]:
    """n / min / mean / median / p90 / max of real measurements; None if empty."""
    if not values:
        return None
    v = sorted(values)
    return {
        "n": len(v),
        "min": round(v[0], 3),
        "mean": round(statistics.fmean(v), 3),
        "median": round(statistics.median(v), 3),
        "p90": round(percentile(v, 0.90), 3),
        "max": round(v[-1], 3),
    }
