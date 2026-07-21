"""``hotato baseline check``: a per-dimension timing drift gate between two
saved runs.

Reads a tolerance file (the same dependency-free YAML subset / JSON
``assertions.yaml`` uses; see :func:`hotato.assert_.parse_assertions_yaml`)
mapping a timing dimension to the increase it may absorb, plus a BASELINE and
a CANDIDATE result JSON in the shape ``hotato run --format json`` emits (the
run envelope from :mod:`hotato.core`: ``tool: "hotato"`` with an ``events``
list -- the envelope that carries the raw timing measurements; a
``hotato release compare`` envelope carries per-dimension PASS/FAIL counts,
not timings, and is refused with a pointer). For each tolerance dimension the
check pools the measured values across each side's scorable events, compares
the pooled means, and gates the increase:

* ``"+10%"`` (or ``"10%"``): the candidate mean may exceed the baseline mean
  by at most 10 percent of the baseline mean,
* ``"+0.05"`` (or a bare ``0.05``): by at most 0.05 seconds, absolute.

Every checked dimension is a lower-is-better timing measurement
(``seconds_to_yield``, ``talk_over_sec``, ``response_gap_sec``,
``premature_start_sec``), so the gate is one-sided: only an INCREASE beyond
the tolerance is drift; a decrease is reported as movement and passes. A
dimension the tolerance file names that has no numeric measurement on a side
is a REFUSAL (the caller's usage-error / exit-2 path), never a silent pass:
nothing was measured, so nothing can honestly be called within tolerance.

Exit codes: 0 = every checked dimension within tolerance; 1 = at least one
dimension drifted beyond its tolerance; 2 = usage error / unusable input (a
malformed tolerance file, an unknown dimension, a result JSON that is not a
run envelope, or a dimension with no measurements on a side).
"""

from __future__ import annotations

import json
import statistics
from typing import Any, Dict, List, Tuple

from .assert_ import parse_assertions_yaml
from .errors import open_regular as _open_regular

__all__ = [
    "KIND",
    "VERSION",
    "DIMENSIONS",
    "parse_tolerances",
    "load_run_envelope",
    "check_envelopes",
    "check_files",
    "render_text",
    "render_junit",
]

KIND = "hotato.baseline-check"
VERSION = 1

# Every dimension the gate can check, mapped to where its per-event value
# lives in the run envelope: ("verdict", key) reads event["verdict"][key];
# ("latency", key) reads event["signals"]["latency"][key]. All four are
# lower-is-better timing measurements in seconds, which is what makes the
# one-sided increase gate honest.
DIMENSIONS: Dict[str, Tuple[str, str]] = {
    "seconds_to_yield": ("verdict", "seconds_to_yield"),
    "talk_over_sec": ("verdict", "talk_over_sec"),
    "response_gap_sec": ("latency", "response_gap_sec"),
    "premature_start_sec": ("latency", "premature_start_sec"),
}

# Float slack for the boundary comparison only ("+10%" of 1.0 against a
# candidate at exactly 1.1 must pass despite binary-float representation);
# far below any real timing movement (measurements are rounded to 3 decimals).
_EPS = 1e-9


def _parse_one_tolerance(dim: str, raw: Any) -> Tuple[str, float]:
    """One dimension's tolerance -> ``(kind, amount)`` with kind ``"percent"``
    (amount in percent of the baseline mean) or ``"absolute"`` (amount in
    seconds). Accepts ``"+10%"`` / ``"10%"`` / ``"+0.05"`` / a bare number
    (the YAML subset already coerces an unquoted ``+0.05`` to a float).
    Anything else -- including a negative amount, which would demand an
    improvement and is a bound, not a tolerance -- is a ValueError (the
    caller's usage-error / exit-2 path)."""
    if isinstance(raw, (int, float)) and not isinstance(raw, bool):
        amount, kind = float(raw), "absolute"
    elif isinstance(raw, str):
        s = raw.strip()
        if s.startswith("+"):
            s = s[1:].strip()
        kind = "absolute"
        if s.endswith("%"):
            kind = "percent"
            s = s[:-1].strip()
        try:
            amount = float(s)
        except ValueError:
            raise ValueError(
                f"dimension {dim!r}: unreadable tolerance {raw!r}; use an "
                'absolute bound in seconds like "+0.05" or a percent of the '
                'baseline like "+10%"'
            )
    else:
        raise ValueError(
            f"dimension {dim!r}: tolerance must be a string like \"+10%\" / "
            f"\"+0.05\" or a number, got {type(raw).__name__}"
        )
    if amount < 0:
        raise ValueError(
            f"dimension {dim!r}: tolerance {raw!r} is negative; a tolerance "
            "is how much INCREASE the candidate may absorb (0 or more)"
        )
    return kind, amount


def parse_tolerances(text: str) -> Dict[str, Dict[str, Any]]:
    """Parse a tolerance document (YAML subset or JSON) into an ordered
    ``{dimension: {"raw", "kind", "amount"}}`` mapping. A document that is not
    a non-empty mapping, or that names a dimension outside :data:`DIMENSIONS`,
    is a ValueError (exit 2)."""
    if not text.strip():
        raise ValueError(
            "tolerance file is empty; give one dimension per line, e.g. "
            'response_gap_sec: "+10%"'
        )
    doc = parse_assertions_yaml(text)
    if not isinstance(doc, dict) or not doc:
        raise ValueError(
            "tolerance file must be a mapping of dimension to tolerance, "
            'e.g. response_gap_sec: "+10%" and seconds_to_yield: "+0.05"'
        )
    out: Dict[str, Dict[str, Any]] = {}
    for dim, raw in doc.items():
        if dim not in DIMENSIONS:
            known = ", ".join(DIMENSIONS)
            raise ValueError(
                f"unknown dimension {dim!r} in the tolerance file; hotato "
                f"measures: {known}"
            )
        kind, amount = _parse_one_tolerance(dim, raw)
        out[dim] = {"raw": raw, "kind": kind, "amount": amount}
    return out


def load_run_envelope(path: str) -> dict:
    """Load one saved run envelope. Anything that is not the ``hotato run
    --format json`` shape is a clean usage error (exit 2), never a silent
    zero-measurement check."""
    with _open_regular(path, "r", encoding="utf-8") as fh:
        try:
            data = json.load(fh)
        except json.JSONDecodeError as exc:
            raise ValueError(f"{path!r} is not readable JSON: {exc}")
    if isinstance(data, dict) and data.get("kind") == "hotato.release-compare":
        raise ValueError(
            f"{path!r} is a `hotato release compare` envelope, which carries "
            "per-dimension PASS/FAIL counts, not timings. `baseline check` "
            "reads the run envelope `hotato run --format json` emits, where "
            "the timing measurements live."
        )
    if not (isinstance(data, dict) and data.get("tool") == "hotato"
            and isinstance(data.get("events"), list)):
        raise ValueError(
            f"{path!r} is not a hotato run envelope. Save one with: "
            "hotato run --suite barge-in --format json --no-fail > baseline.json"
        )
    return data


def _dimension_values(env: dict, dim: str) -> List[float]:
    """Every numeric measurement of ``dim`` across the envelope's scorable
    events. Not-scorable events are input problems and never contribute; a
    null / missing / non-numeric value is skipped, never coerced."""
    where, key = DIMENSIONS[dim]
    out: List[float] = []
    for e in env.get("events", []):
        if not isinstance(e, dict) or e.get("scorable") is False:
            continue
        if where == "verdict":
            src = e.get("verdict")
        else:
            sig = e.get("signals")
            src = sig.get("latency") if isinstance(sig, dict) else None
        if not isinstance(src, dict):
            continue
        v = src.get(key)
        if isinstance(v, (int, float)) and not isinstance(v, bool):
            out.append(float(v))
    return out


def _require_measurements(values: List[float], *, dim: str, side: str,
                          path: str) -> None:
    if not values:
        raise ValueError(
            f"the tolerance file names dimension {dim!r} but the {side} "
            f"result {path!r} carries no numeric {dim} measurement on any "
            "scorable event; nothing was measured, so the check refuses "
            "rather than calling it within tolerance"
        )


def check_envelopes(tolerances: Dict[str, Dict[str, Any]],
                    baseline_env: dict, candidate_env: dict, *,
                    baseline_path: str = "baseline",
                    candidate_path: str = "candidate") -> dict:
    """Gate the candidate envelope against the baseline envelope, one entry
    per tolerance dimension, and return the check envelope (``exit_code`` 0
    within tolerance / 1 on drift). A dimension with no measurements on a
    side raises ValueError (the exit-2 refusal)."""
    dims: Dict[str, Any] = {}
    drifted: List[str] = []
    for dim, tol in tolerances.items():
        b_vals = _dimension_values(baseline_env, dim)
        c_vals = _dimension_values(candidate_env, dim)
        _require_measurements(b_vals, dim=dim, side="baseline",
                              path=baseline_path)
        _require_measurements(c_vals, dim=dim, side="candidate",
                              path=candidate_path)
        b_mean = statistics.fmean(b_vals)
        c_mean = statistics.fmean(c_vals)
        delta = c_mean - b_mean
        if tol["kind"] == "percent":
            allowed = abs(b_mean) * tol["amount"] / 100.0
        else:
            allowed = tol["amount"]
        within = delta <= allowed + _EPS
        if not within:
            drifted.append(dim)
        dims[dim] = {
            "tolerance": tol["raw"],
            "tolerance_kind": tol["kind"],
            "allowed_increase_sec": round(allowed, 6),
            "baseline": {"mean_sec": round(b_mean, 6), "n": len(b_vals)},
            "candidate": {"mean_sec": round(c_mean, 6), "n": len(c_vals)},
            "delta_sec": round(delta, 6),
            "within": within,
        }
    return {
        "kind": KIND,
        "version": VERSION,
        "baseline_path": baseline_path,
        "candidate_path": candidate_path,
        "dimensions": dims,
        "drifted": drifted,
        "within_tolerance": not drifted,
        "exit_code": 1 if drifted else 0,
        "note": (
            "per-dimension drift gate on the pooled mean across each side's "
            "scorable events; one-sided (every dimension is lower-is-better "
            "timing, so only an increase beyond the tolerance is drift, and a "
            "decrease passes). A dimension with no measurements on a side "
            "refuses (exit 2), never a silent pass."
        ),
    }


def check_files(tolerances_path: str, baseline_path: str,
                candidate_path: str) -> dict:
    """``hotato baseline check`` as one call: read the three files, then
    :func:`check_envelopes`."""
    with _open_regular(tolerances_path, "r", encoding="utf-8") as fh:
        text = fh.read()
    try:
        tolerances = parse_tolerances(text)
    except ValueError as exc:
        raise ValueError(f"{tolerances_path!r}: {exc}")
    return check_envelopes(
        tolerances,
        load_run_envelope(baseline_path),
        load_run_envelope(candidate_path),
        baseline_path=baseline_path,
        candidate_path=candidate_path,
    )


def _signed(x: float) -> str:
    return f"+{x:.3f}" if x >= 0 else f"{x:.3f}"


def render_text(check: dict) -> str:
    """A human-readable per-dimension drift table, one line per checked
    dimension, mirroring `release compare`'s plain rollup style."""
    lines = [
        f"hotato baseline check: baseline {check['baseline_path']} -> "
        f"candidate {check['candidate_path']}",
    ]
    for dim, d in check["dimensions"].items():
        b, c = d["baseline"], d["candidate"]
        mark = "within" if d["within"] else "DRIFT"
        lines.append(
            f"  {dim:<21} {b['mean_sec']:.3f}s (n={b['n']})  ->  "
            f"{c['mean_sec']:.3f}s (n={c['n']})   "
            f"delta {_signed(d['delta_sec'])}s vs allowed "
            f"+{d['allowed_increase_sec']:.3f}s ({d['tolerance']})  {mark}"
        )
    if check["drifted"]:
        lines.append("drift beyond tolerance: " + ", ".join(check["drifted"]))
    else:
        lines.append("every checked dimension stayed within its tolerance")
    lines.append(f"  {check['note']}")
    return "\n".join(lines) + "\n"


# Same escape table as contract.render_verify_junit, kept local so this
# module stays independent of the heavier contract import chain.
_JUNIT_ESCAPE = str.maketrans({"&": "&amp;", "<": "&lt;", ">": "&gt;",
                               '"': "&quot;"})


def _jesc(s) -> str:
    return str(s if s is not None else "").translate(_JUNIT_ESCAPE)


def render_junit(check: dict, *,
                 suite_name: str = "hotato baseline check") -> str:
    """JUnit XML for CI: one ``<testcase>`` per checked dimension
    (``classname="hotato.baseline"``), a ``<failure>`` child for a drifted
    one carrying the dimension's full delta record, mirroring
    ``contract.render_verify_junit``'s conventions."""
    dims = check["dimensions"]
    failures = sum(1 for d in dims.values() if not d["within"])
    cases = []
    for dim, d in dims.items():
        case = f'  <testcase classname="hotato.baseline" name="{_jesc(dim)}">'
        if not d["within"]:
            reason = (
                f"{dim}: candidate mean {d['candidate']['mean_sec']}s vs "
                f"baseline mean {d['baseline']['mean_sec']}s; delta "
                f"{_signed(d['delta_sec'])}s exceeds allowed "
                f"+{d['allowed_increase_sec']}s ({d['tolerance']})"
            )
            case += (f'\n    <failure message="{_jesc(reason)}">'
                     f'{_jesc(json.dumps(d, sort_keys=True))}'
                     "</failure>\n  ")
        case += "</testcase>"
        cases.append(case)
    return (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        f'<testsuite name="{_jesc(suite_name)}" tests="{len(cases)}" '
        f'failures="{failures}">\n' + "\n".join(cases) + "\n</testsuite>\n"
    )
