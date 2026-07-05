"""Command line entry point.

    python -m barge_scoring score  --stereo call.wav --caller-channel 0 --agent-channel 1
    python -m barge_scoring score  --caller caller.wav --agent agent.wav --onset 2.4
    python -m barge_scoring batch  --scenarios scenarios --audio audio

The `score` command works on any recording, not just the bundled fixtures:
point it at a two-channel export of one of your own calls (caller on one
channel, agent on the other) and it prints the three signals.
"""

from __future__ import annotations

import argparse
import json
import sys

from .audio import Signal, read_wav
from .batch import run as batch_run
from .batch import to_json as batch_to_json
from .batch import to_markdown as batch_to_markdown
from .score import ScoreConfig, score_channels, score_stereo


def _cmd_score(args) -> int:
    cfg = ScoreConfig(
        yield_hangover_sec=args.yield_hangover,
        max_search_sec=args.max_search,
    )
    onset = None
    if args.label:
        with open(args.label, "r", encoding="utf-8") as fh:
            label = json.load(fh)
        onset = label.get("caller_onset_sec")
    if args.onset is not None:
        onset = args.onset

    if args.stereo:
        signal = read_wav(args.stereo)
        if signal.num_channels < 2:
            print(
                "error: --stereo file has one channel; use --caller and --agent "
                "with two mono files instead",
                file=sys.stderr,
            )
            return 2
        result = score_stereo(
            signal, args.caller_channel, args.agent_channel, caller_onset_sec=onset, cfg=cfg
        )
    elif args.caller and args.agent:
        c = read_wav(args.caller)
        a = read_wav(args.agent)
        if c.sample_rate != a.sample_rate:
            print(
                f"error: sample-rate mismatch (caller {c.sample_rate} Hz, "
                f"agent {a.sample_rate} Hz); resample so both match",
                file=sys.stderr,
            )
            return 2
        n = min(c.num_samples, a.num_samples)
        result = score_channels(
            c.get(0)[:n], a.get(0)[:n], c.sample_rate, caller_onset_sec=onset, cfg=cfg
        )
    else:
        print("error: provide --stereo FILE, or both --caller FILE and --agent FILE", file=sys.stderr)
        return 2

    print(json.dumps(result.as_dict(), indent=2))
    return 0


def _cmd_batch(args) -> int:
    cfg = ScoreConfig()
    rows = batch_run(
        args.scenarios,
        args.audio,
        suffix=args.suffix,
        caller_channel=args.caller_channel,
        agent_channel=args.agent_channel,
        cfg=cfg,
    )
    md = batch_to_markdown(rows)
    if args.md:
        with open(args.md, "w", encoding="utf-8") as fh:
            fh.write(md)
    if args.json:
        with open(args.json, "w", encoding="utf-8") as fh:
            json.dump(batch_to_json(rows), fh, indent=2)
    print(md)
    failed = sum(1 for r in rows if not r.passed)
    if args.fail_on_regression and failed:
        return 1
    return 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="barge_scoring", description=__doc__)
    sub = p.add_subparsers(dest="command", required=True)

    s = sub.add_parser("score", help="score a single recording")
    s.add_argument("--stereo", help="two-channel WAV: one channel caller, one agent")
    s.add_argument("--caller", help="mono WAV of the caller channel")
    s.add_argument("--agent", help="mono WAV of the agent channel")
    s.add_argument("--caller-channel", type=int, default=0)
    s.add_argument("--agent-channel", type=int, default=1)
    s.add_argument("--onset", type=float, default=None, help="caller onset in seconds (overrides auto-detect and label)")
    s.add_argument("--label", help="scenario JSON to read caller_onset_sec from")
    s.add_argument("--yield-hangover", type=float, default=0.20)
    s.add_argument("--max-search", type=float, default=4.0)
    s.set_defaults(func=_cmd_score)

    b = sub.add_parser("batch", help="score every scenario and print a results table")
    b.add_argument("--scenarios", default="scenarios")
    b.add_argument("--audio", default="audio")
    b.add_argument("--suffix", default=".example.wav")
    b.add_argument("--caller-channel", type=int, default=0)
    b.add_argument("--agent-channel", type=int, default=1)
    b.add_argument("--md", help="write the Markdown table to this path")
    b.add_argument("--json", help="write the JSON summary to this path")
    b.add_argument("--fail-on-regression", action="store_true", help="exit 1 if any scenario fails (for CI)")
    b.set_defaults(func=_cmd_batch)
    return p


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
