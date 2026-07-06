#!/usr/bin/env python3
"""Turn a hotato result envelope into a tidy Markdown PR comment.

Reads one hotato JSON envelope (the exact shape emitted by
`hotato run --suite barge-in --format json`) and writes a Markdown comment
body to stdout: a header, a one line pass/fail summary, a per scenario table,
and a short regressions section.

Stdlib only, deterministic, no network, no third party deps. The same input
always renders the same bytes, so a CI job can post it as a sticky comment and
update it in place on every push.

Pass an optional baseline with --base to add talk over and time to yield
deltas against a reference run (for example the same suite scored on the PR
base branch). Any scenario that got slower or started overlapping more shows up
in the regressions section.

Usage:
    hotato run --suite barge-in --format json | python3 scripts/pr_comment.py
    python3 scripts/pr_comment.py head.json
    python3 scripts/pr_comment.py --base base.json head.json
"""

from __future__ import annotations

import argparse
import json
import sys

# Hidden marker on the first line. The GitHub Actions workflow finds the sticky
# comment by this exact string, so keep it stable and keep it first.
MARKER = "<!-- hotato-pr-comment -->"

# A metric moved by at least this many seconds counts as a real change, not noise.
EPS = 0.01


def _fmt_sec(value):
    """Format a seconds value, or a dash when it is absent."""
    if value is None:
        return "-"
    return f"{float(value):.2f}s"


def _fmt_delta(value):
    """Format a signed seconds delta."""
    sign = "+" if value >= 0 else ""
    return f"{sign}{float(value):.2f}s"


def _expect(event):
    return "yield" if event.get("expected_yield") else "hold"


def _fail_reason(event):
    """A short, honest reason a scenario did not pass."""
    verdict = event["verdict"]
    reasons = [r for r in verdict.get("reasons", []) if r]
    if reasons:
        return "; ".join(reasons)
    did_yield = verdict.get("did_yield")
    expected = event.get("expected_yield")
    if expected and not did_yield:
        return "did not yield to a real interruption"
    if not expected and did_yield:
        return "yielded to something it should have held through"
    return "a timing threshold was exceeded"


def _base_index(base_env):
    """Map event_id to its verdict in a baseline envelope."""
    if not base_env:
        return {}
    index = {}
    for event in base_env.get("events", []):
        event_id = event.get("event_id")
        if event_id is not None:
            index[event_id] = event.get("verdict", {})
    return index


def render_markdown(env, base=None):
    """Render a hotato envelope (and optional baseline) as a Markdown comment body."""
    summary = env.get("summary", {})
    events = env.get("events", [])
    passed = summary.get("passed", 0)
    total = summary.get("events", len(events))
    failed = summary.get("failed", 0)
    regression = bool(summary.get("regression"))

    lines = [MARKER, "## hotato turn-taking eval", ""]

    # Strengths first: lead with what passed, then the count that failed.
    head = f"{passed} of {total} scenarios pass. {failed} fail."
    head += " Regression detected." if regression else " No regression."
    lines += [head, ""]

    # Per scenario table.
    lines += [
        "| scenario | expect | yielded | time to yield | talk over | result |",
        "| --- | --- | --- | --- | --- | --- |",
    ]
    for event in events:
        verdict = event["verdict"]
        lines.append(
            f"| {event.get('event_id', '?')} "
            f"| {_expect(event)} "
            f"| {'yes' if verdict.get('did_yield') else 'no'} "
            f"| {_fmt_sec(verdict.get('seconds_to_yield'))} "
            f"| {_fmt_sec(verdict.get('talk_over_sec'))} "
            f"| {'pass' if verdict.get('passed') else 'FAIL'} |"
        )
    lines.append("")

    # Regressions: verdict failures always, plus metric slippage against a base.
    base_index = _base_index(base)
    fails = [e for e in events if not e["verdict"].get("passed")]

    slower = []
    if base_index:
        for event in events:
            verdict = event["verdict"]
            prior = base_index.get(event.get("event_id"))
            if not prior:
                continue
            notes = []
            d_talk = float(verdict.get("talk_over_sec") or 0) - float(prior.get("talk_over_sec") or 0)
            if d_talk >= EPS:
                notes.append(f"talk over {_fmt_delta(d_talk)}")
            cur_ttl, base_ttl = verdict.get("seconds_to_yield"), prior.get("seconds_to_yield")
            if cur_ttl is not None and base_ttl is not None:
                d_ttl = float(cur_ttl) - float(base_ttl)
                if d_ttl >= EPS:
                    notes.append(f"time to yield {_fmt_delta(d_ttl)}")
            if prior.get("passed") and not verdict.get("passed"):
                notes.append("now failing")
            if notes:
                slower.append(f"- {event.get('event_id', '?')}: " + ", ".join(notes) + " vs base")

    lines.append("### Regressions")
    if not fails and not slower:
        lines.append("None.")
    else:
        for event in fails:
            lines.append(f"- {event.get('event_id', '?')}: {_fail_reason(event)}")
        # Avoid listing a failing scenario twice when it also slipped on a metric.
        fail_ids = {e.get("event_id") for e in fails}
        for line in slower:
            if not any(line.startswith(f"- {fid}:") for fid in fail_ids):
                lines.append(line)
    lines.append("")

    lines.append(
        "<sub>Reproducible timing measured locally from call audio. "
        "Swap the bundled self-test step for your own captured recordings to "
        "gate on your agent. github.com/attenlabs/hotato</sub>"
    )
    return "\n".join(lines) + "\n"


def _load(path_or_dash):
    if path_or_dash in (None, "-"):
        return json.load(sys.stdin)
    with open(path_or_dash, "r", encoding="utf-8") as fh:
        return json.load(fh)


def main(argv=None):
    parser = argparse.ArgumentParser(description="Render a hotato envelope as a Markdown PR comment.")
    parser.add_argument("envelope", nargs="?", default="-", help="hotato JSON envelope path, or - for stdin (default).")
    parser.add_argument("--base", default=None, help="optional baseline envelope for talk over / time to yield deltas.")
    args = parser.parse_args(argv)

    env = _load(args.envelope)
    base = _load(args.base) if args.base else None
    sys.stdout.write(render_markdown(env, base))
    return 0


if __name__ == "__main__":
    sys.exit(main())
