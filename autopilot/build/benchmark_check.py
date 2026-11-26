"""Benchmark-regression gate. Runs the benchmark command, compares each metric with the
committed baseline, and fails on any regression beyond the tolerance.

Default measurement: hotato's own harness, `python -m hotato.benchmark --quiet --out DIR`, which
writes DIR/measurement-error.json. Its `aggregate` block is flattened to dotted metric names, e.g.
`error_stats_ms.onset_sec.max_ms` and `confusion.missed_yield`.

Env: BENCH_CMD (optional override; prints a JSON object of metric -> number), BENCH_BASELINE
(default benchmarks/baseline.json, {"higher": {...}, "lower": {...}}), BENCH_TOLERANCE (default 0).
Gate files are owner-only (CODEOWNERS), so the agent cannot lower its own bar.
"""
import json
import os
import subprocess
import sys
import tempfile


def regressions(baseline, current, tol=0.0):
    """baseline: {"higher": {m: v}, "lower": {m: v}}. Returns a list of messages."""
    bad = []
    for m, base in baseline.get("higher", {}).items():
        cur = current.get(m)
        if cur is None or cur < base - tol * abs(base):
            bad.append(f"{m}: {cur} < baseline {base}")
    for m, base in baseline.get("lower", {}).items():
        cur = current.get(m)
        if cur is None or cur > base + tol * abs(base):
            bad.append(f"{m}: {cur} > baseline {base}")
    return bad


def flatten(obj, prefix=""):
    """Numeric leaves of a nested dict as {"a.b.c": number}; lists and non-numbers are skipped."""
    out = {}
    for k, v in obj.items():
        name = f"{prefix}.{k}" if prefix else str(k)
        if isinstance(v, dict):
            out.update(flatten(v, name))
        elif isinstance(v, (int, float)) and not isinstance(v, bool):
            out[name] = v
    return out


def measure():
    """Run the measurement and return {metric: number}, or None if it failed."""
    cmd = os.environ.get("BENCH_CMD")
    if cmd:
        out = subprocess.run(cmd, shell=True, capture_output=True, text=True)
        return json.loads(out.stdout) if out.returncode == 0 else None
    with tempfile.TemporaryDirectory() as d:
        out = subprocess.run([sys.executable, "-m", "hotato.benchmark", "--quiet", "--out", d],
                             capture_output=True, text=True)
        path = os.path.join(d, "measurement-error.json")
        if out.returncode or not os.path.exists(path):
            return None
        return flatten(json.load(open(path)).get("aggregate", {}))


def main():
    base_path = os.environ.get("BENCH_BASELINE", "benchmarks/baseline.json")
    if not os.path.exists(base_path):
        print(f"benchmark: no baseline at {base_path}")
        return 1
    baseline = json.load(open(base_path))
    current = measure()
    if current is None:
        print("benchmark: command failed")
        return 1
    bad = regressions(baseline, current, float(os.environ.get("BENCH_TOLERANCE", "0")))
    for b in bad:
        print("REGRESSION", b)
    print(f"benchmark: {len(bad)} regression(s)")
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
