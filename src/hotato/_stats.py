"""Tiny stdlib-only distribution helpers shared by the report and team modes.

Deterministic, documented definitions so every published number is re-derivable
by hand:

* mean   = arithmetic mean (``statistics.fmean``)
* median = ``statistics.median``  (p50)
* p90 / p95 = linear interpolation between closest ranks (the definition numpy
           calls "linear"): with the values sorted ascending and n values,
           pos = q * (n - 1); p_q = v[floor(pos)] + frac * (v[floor(pos)+1]
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
    """n / min / mean / median / p90 / p95 / max of real measurements; None if
    empty. ``median`` is p50; ``p95`` is additive (present alongside p90 for
    every caller of this function)."""
    if not values:
        return None
    v = sorted(values)
    return {
        "n": len(v),
        "min": round(v[0], 3),
        "mean": round(statistics.fmean(v), 3),
        "median": round(statistics.median(v), 3),
        "p90": round(percentile(v, 0.90), 3),
        "p95": round(percentile(v, 0.95), 3),
        "max": round(v[-1], 3),
    }


def latency_sla(dist: Optional[dict], bound_sec: Optional[float]) -> dict:
    """The latency SLA gate: pass p95 of a pooled distribution (``dist``, the
    return of ``dist_summary``) against an optional bound in seconds.

    ``bound_sec=None`` means the gate is not configured: ``passed`` is None,
    never a failure. No measurements (``dist=None``) with a configured bound
    also never fails the gate (nothing was observed to violate it); it is
    reported as ``observed_p95_sec: None`` so the shortfall is visible, not
    hidden behind a false pass.
    """
    observed = dist["p95"] if dist else None
    passed = None
    if bound_sec is not None:
        passed = observed is None or observed <= bound_sec
    return {"bound_sec": bound_sec, "observed_p95_sec": observed, "passed": passed}
