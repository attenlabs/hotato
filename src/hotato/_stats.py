"""Tiny stdlib-only distribution helpers shared by the report and team modes.

Deterministic, documented definitions so every published number is re-derivable
by hand:

* mean   = arithmetic mean (``statistics.fmean``)
* median = ``statistics.median``  (p50)
* p90 / p95 = linear interpolation between closest ranks (the definition numpy
           calls "linear"): with the values sorted ascending and n values,
           pos = q * (n - 1); p_q = v[floor(pos)] + frac * (v[floor(pos)+1]
           - v[floor(pos)]).
* nearest-rank percentile (``nearest_rank``, used by the fleet corpus
  percentiles): with the values sorted ascending and n values,
  rank = ceil(q * n) (1-based, at least 1); p_q = v[rank - 1]. Every
  percentile is an observed measurement, never an interpolation.

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


def nearest_rank(values, q: float) -> Optional[float]:
    """Deterministic nearest-rank percentile of ``values`` (q in (0, 1]).

    With the values sorted ascending and n values: rank = ceil(q * n)
    (1-based, clamped to at least 1); the percentile is v[rank - 1]. The
    result is always one of the observed measurements (never interpolated),
    so it is re-derivable by hand and stable across platforms. Empty input
    returns None, never a fabricated number.
    """
    if not values:
        return None
    v = sorted(values)
    rank = max(1, math.ceil(q * len(v)))
    return v[min(rank, len(v)) - 1]


def _numeric(values) -> list:
    """Only the real numeric measurements. Defensive: a caller pooling values from
    a malformed / hand-edited envelope side may include a non-numeric verdict
    field (a string, a list, null); those must never reach ``sorted`` / ``round``
    below (a raw TypeError). ``bool`` is excluded so a stray True/False is not
    silently treated as 1/0."""
    return [x for x in values
            if isinstance(x, (int, float)) and not isinstance(x, bool)]


def dist_summary(values) -> Optional[dict]:
    """n / min / mean / median / p90 / p95 / max of real measurements; None if
    empty (or nothing numeric). ``median`` is p50; ``p95`` is additive (present
    alongside p90 for every caller of this function)."""
    v = sorted(_numeric(values))
    if not v:
        return None
    return {
        "n": len(v),
        "min": round(v[0], 3),
        "mean": round(statistics.fmean(v), 3),
        "median": round(statistics.median(v), 3),
        "p90": round(percentile(v, 0.90), 3),
        "p95": round(percentile(v, 0.95), 3),
        "max": round(v[-1], 3),
    }


def corpus_percentiles(values, total: int) -> dict:
    """p50 / p90 / p99 of a pooled corpus by the nearest-rank method.

    ``values`` is every candidate measurement pooled across the corpus (nulls
    and junk included); ``total`` is the number of events the metric could
    have been measured on. Only real numeric measurements enter the
    percentiles (see ``_numeric``); ``excluded_null`` = total - n counts the
    events with no numeric measurement for the metric (null or missing, e.g.
    transcript-path talk-over), shown so the exclusion is visible and a null
    is never treated as 0. The shape is stable even with nothing measured:
    the percentiles are None and the exclusion count still stated.
    """
    v = sorted(_numeric(values))

    def p(q: float) -> Optional[float]:
        r = nearest_rank(v, q)
        return round(r, 3) if r is not None else None

    return {
        "method": "nearest-rank",
        "n": len(v),
        "excluded_null": max(0, total - len(v)),
        "p50": p(0.50),
        "p90": p(0.90),
        "p99": p(0.99),
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
