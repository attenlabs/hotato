#!/usr/bin/env python3
"""Aggregate bench_results.json into the spec-8 tables: per-tier did_yield
agreement (confusion matrix), DER summary (both conventions), assignment
accuracy. Mirrors the V1 report's table shapes so before/after is a direct
diff."""

from __future__ import annotations

import json
import os
import statistics

HERE = os.path.dirname(os.path.abspath(__file__))
IN_PATH = os.path.join(HERE, "bench_results.json")


def _confusion(records):
    """correct_yield / missed_yield / correct_hold / phantom_yield, for
    records where D produced a verdict (tier != refuse/backend_unavailable)."""
    out = {"n": 0, "scored": 0, "agree": 0, "correct_yield": 0, "missed_yield": 0,
           "correct_hold": 0, "phantom_yield": 0}
    for r in records:
        out["n"] += 1
        d = r.get("d_did_yield")
        if d is None:
            continue
        out["scored"] += 1
        t = r["truth_did_yield"]
        if t and d:
            out["correct_yield"] += 1
            out["agree"] += 1
        elif t and not d:
            out["missed_yield"] += 1
        elif not t and not d:
            out["correct_hold"] += 1
            out["agree"] += 1
        else:
            out["phantom_yield"] += 1
    return out


def _pct(a, b):
    return f"{round(100 * a / b)}%" if b else "-"


def print_agreement_table(records):
    tiers = ["high", "low", "refuse", "backend_unavailable"]
    print("| tier | n | scored | agree | agreement | correct_yield | missed_yield | correct_hold | phantom_yield |")
    print("|---|---|---|---|---|---|---|---|---|")
    all_c = _confusion(records)
    print(f"| ALL | {all_c['n']} | {all_c['scored']} | {all_c['agree']} | "
          f"{_pct(all_c['agree'], all_c['scored'])} | {all_c['correct_yield']} | "
          f"{all_c['missed_yield']} | {all_c['correct_hold']} | {all_c['phantom_yield']} |")
    for tier in tiers:
        sub = [r for r in records if r.get("tier") == tier]
        if not sub:
            continue
        c = _confusion(sub)
        agreement = _pct(c["agree"], c["scored"]) if c["scored"] else "(not scored)"
        print(f"| {tier} | {c['n']} | {c['scored']} | {c['agree']} | {agreement} | "
              f"{c['correct_yield']} | {c['missed_yield']} | {c['correct_hold']} | "
              f"{c['phantom_yield']} |")


def print_der_table(records):
    nist = [r["der_nist"] for r in records if r.get("der_nist") is not None]
    strict = [r["der_strict"] for r in records if r.get("der_strict") is not None]
    print("| convention | n | mean | median | min | max |")
    print("|---|---|---|---|---|---|")
    for name, vals in (("NIST (0.25s collar, overlap ignored)", nist),
                        ("strict (0 collar, overlap scored)", strict)):
        if not vals:
            print(f"| {name} | 0 | - | - | - | - |")
            continue
        print(f"| {name} | {len(vals)} | {statistics.mean(vals):.3f} | "
              f"{statistics.median(vals):.3f} | {min(vals):.3f} | {max(vals):.3f} |")


def print_per_fixture_table(records):
    print("| fixture | tier | T yield | D yield | assign ok | DER NIST | DER strict | overlap | churn |")
    print("|---|---|---|---|---|---|---|---|---|")
    for r in records:
        t = "True" if r["truth_did_yield"] else "False"
        d = r.get("d_did_yield")
        d_s = "-" if d is None else ("True" if d else "False")
        mismatch = "**" if (d is not None and d != r["truth_did_yield"]) else ""
        assign = r.get("assign_ok")
        assign_s = "-" if assign is None else ("yes" if assign else "**NO**")
        der_n = r.get("der_nist")
        der_s = r.get("der_strict")
        print(f"| {r['id']} | {r.get('tier')} | {t} | {mismatch}{d_s}{mismatch} | "
              f"{assign_s} | {der_n if der_n is not None else '-'} | "
              f"{der_s if der_s is not None else '-'} | "
              f"{r.get('overlap_ratio', '-')} | {r.get('segment_churn_per_sec', '-')} |")


def main() -> int:
    if not os.path.exists(IN_PATH):
        print(f"missing {IN_PATH}; run bench_diarize.py first")
        return 1
    with open(IN_PATH, encoding="utf-8") as fh:
        data = json.load(fh)
    records = data["records"]

    print(f"\n## Per-fixture (n={len(records)})\n")
    print_per_fixture_table(records)

    print("\n## Verdict agreement by tier\n")
    print_agreement_table(records)

    print("\n## DER (spec 8.1 cross-check)\n")
    print_der_table(records)

    errs = [r for r in records if r.get("backend_error")]
    if errs:
        print("\n## Backend errors\n")
        for r in errs:
            print(f"- {r['id']}: {r['backend_error']}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
