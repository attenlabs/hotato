"""Zero-install CLI.

    # score one recording (two-channel: caller on one channel, agent on the other)
    uvx hotato run --stereo call.wav --stack livekit --format json

    # run the bundled 8-scenario battery; exits non-zero on any regression (for CI)
    uvx hotato run --suite barge-in --stack pipecat --format json

Everything runs offline; no audio leaves the machine.
"""

from __future__ import annotations

import argparse
import json
import sys

from . import __version__
from . import capture as _capture
from ._engine.score import ScoreConfig
from ._engine.vad import BackendUnavailable, VADParams
from .core import SUITE_ID, dump_frames_for_input, run_single, run_suite

# Printed to stderr when `--backend neural` is combined with `--suite`: the bundled
# self-test IS the energy reference, so it always scores with energy regardless.
_SUITE_ENERGY_ONLY_NOTE = (
    "note: --backend neural is ignored for --suite -- the bundled self-test is the "
    "ENERGY reference and always scores with energy so the numbers stay reproducible. "
    "Point --backend neural at your OWN recording: "
    "hotato run --stereo your_call.wav --backend neural"
)

# The first-run "aha": lead with scoring the user's OWN call, not the synthetic
# self-test. Printed when `hotato` is run with no subcommand.
_FIRST_RUN_GUIDE = """\
hotato -- the open, offline turn-taking eval for voice agents.
Does your agent drop the turn, or hog it?

Score YOUR OWN call in under a minute (bring a dual-channel recording):

  Vapi:     hotato capture --stack vapi   --call-id <id>          # + VAPI_API_KEY
  Twilio:   hotato capture --stack twilio --recording-sid RE...   # + TWILIO_ACCOUNT_SID / TWILIO_AUTH_TOKEN
  LiveKit:  hotato setup   --stack livekit   # scaffold two-track egress, then --caller a.wav --agent b.wav
  Pipecat:  hotato setup   --stack pipecat   # drop in the 2-channel recorder, then score the WAV
  Retell:   hotato setup   --stack retell    # honest: no self-serve stereo export yet -- prints the workaround

Already have a 2-channel WAV (caller on channel 0, agent on channel 1)?
  hotato run --stereo your_call.wav --expect yield

No recording handy? Watch the capture -> score loop run end-to-end, fully offline:
  hotato capture --stack vapi --demo

Self-test (checks Hotato ITSELF on synthetic fixtures -- NOT a test of your agent):
  hotato run --suite barge-in

Offline. MIT. No accuracy score anywhere: reproducible timing measurements with an
exposed method and an explicit ceiling. Docs: README.md / METHODOLOGY.md
"""

_SELF_TEST_NOTE = (
    "note: --suite is Hotato's SELF-TEST on synthetic fixtures -- it checks the "
    "tool itself, not your agent. To score YOUR agent, bring a real dual-channel "
    "call: hotato capture --stack vapi --call-id <id>  (see: hotato)"
)


def _emit(env: dict, fmt: str) -> None:
    if fmt == "json":
        print(json.dumps(env, indent=2))
        return
    # human-readable summary
    s = env["summary"]
    head = (
        f"hotato [{env['mode']}] stack={env['stack']} "
        f"offline={env['offline']}"
    )
    print(head)
    print(f"  {s['passed']}/{s['events']} events pass  (failed={s['failed']})")
    for e in env["events"]:
        v = e["verdict"]
        mark = "PASS" if v["passed"] else "FAIL"
        tty = v["seconds_to_yield"]
        tty_s = "-" if tty is None else f"{tty:.2f}s"
        print(
            f"  [{mark}] {e['event_id']}: did_yield={v['did_yield']} "
            f"seconds_to_yield={tty_s} talk_over={v['talk_over_sec']:.2f}s"
        )
        if not v["passed"] and e.get("fix"):
            fx = e["fix"]
            print(f"         fix[{fx['fix_class']}]: {fx['title']}")
            if fx["fix_class"] == "config" and fx.get("knob"):
                print(f"            knob: {fx['knob']['parameter']}")
                print(f"            move: {fx['knob']['direction']}")
            elif fx["fix_class"] == "engagement-control" and fx.get("pointer"):
                print(f"            -> {fx['pointer']['layer']}")
    if env.get("funnel"):
        print("  note: no single sensitivity threshold satisfies this battery; "
              "see funnel pointer in --format json.")
    print(f"  exit_code={env['exit_code']}")


def _cmd_run(args) -> int:
    backend = getattr(args, "backend", "energy")
    # Conflicting inputs: --suite runs the bundled self-test battery and silently
    # ignoring a single recording passed alongside it would mislead. Reject the
    # combination up front (clean usage error -> exit 2) rather than quietly
    # dropping the user's file.
    if args.suite and (args.stereo or args.caller or args.agent):
        raise ValueError(
            "--suite runs the bundled self-test battery and cannot be combined "
            "with a single recording (--stereo / --caller / --agent). Run one or "
            "the other."
        )
    if args.dump_frames:
        if args.suite:
            raise ValueError(
                "--dump-frames works on a single recording; drop --suite and pass "
                "--stereo, or --caller and --agent"
            )
        dump = dump_frames_for_input(
            stereo=args.stereo,
            caller=args.caller,
            agent=args.agent,
            caller_channel=args.caller_channel,
            agent_channel=args.agent_channel,
            onset_sec=args.onset,
        )
        with open(args.dump_frames, "w", encoding="utf-8") as fh:
            json.dump(dump, fh, indent=2)
        print(
            f"wrote per-frame evidence ({len(dump['frames'])} frames) to "
            f"{args.dump_frames}",
            file=sys.stderr,
        )
    if args.suite:
        # The bundled battery is the ENERGY reference: it always scores with energy
        # so the golden numbers stay byte-stable, regardless of --backend.
        env = run_suite(
            suite=args.suite,
            stack=args.stack,
            scenarios_dir=args.scenarios,
            audio_dir=args.audio,
            caller_channel=args.caller_channel,
            agent_channel=args.agent_channel,
        )
        if backend != "energy":
            print(_SUITE_ENERGY_ONLY_NOTE, file=sys.stderr)
        # Keep stdout (and the JSON envelope) byte-for-byte the same; the self-test
        # framing goes to stderr so the hero output and machine output are untouched.
        if not args.scenarios and not args.audio:
            print(_SELF_TEST_NOTE, file=sys.stderr)
    else:
        # Energy stays the default and is passed as cfg=None (byte-identical to the
        # reference path). A non-energy backend is an explicit, opt-in cross-check
        # applied to BOTH channels; if its extra is missing this raises a clean
        # BackendUnavailable that main() surfaces as exit code 2 (never a fallback).
        cfg = None
        if backend != "energy":
            cfg = ScoreConfig(
                caller_vad=VADParams(backend=backend),
                agent_vad=VADParams(backend=backend),
            )
        env = run_single(
            stereo=args.stereo,
            caller=args.caller,
            agent=args.agent,
            caller_channel=args.caller_channel,
            agent_channel=args.agent_channel,
            onset_sec=args.onset,
            expect=args.expect,
            stack=args.stack,
            max_talk_over_sec=args.max_talk_over,
            max_time_to_yield_sec=args.max_time_to_yield,
            cfg=cfg,
        )
    _emit(env, args.format)
    if args.no_fail:
        return 0
    return env["exit_code"]


def _cmd_capture(args) -> int:
    return _capture.run_capture(
        args.stack,
        demo=args.demo,
        stereo=args.stereo,
        caller=args.caller,
        agent=args.agent,
        onset=args.onset,
        expect=args.expect,
        caller_channel=args.caller_channel,
        agent_channel=args.agent_channel,
        call_id=args.call_id,
        api_key=args.api_key,
        recording_sid=args.recording_sid,
        account_sid=args.account_sid,
        auth_token=args.auth_token,
        out=args.out,
        fmt=args.format,
    )


def _cmd_setup(args) -> int:
    return _capture.run_setup(args.stack)


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="hotato",
        description="Hotato: the open turn-taking eval for voice agents (barge-in / "
        "turn-taking / overlap / backchannel). Does your agent drop the turn, or hog "
        "it? Offline. MIT. There is NO accuracy percentage anywhere: results are "
        "reproducible timing measurements with an exposed method and an explicit "
        "ceiling.",
    )
    p.add_argument("--version", action="version", version=f"hotato {__version__}")
    # Not required: bare `hotato` prints the first-run guide (score your OWN call),
    # rather than an argparse usage error.
    sub = p.add_subparsers(dest="command", required=False)

    r = sub.add_parser(
        "run",
        help="score one recording, or run the synthetic self-test battery",
        epilog=(
            "Offline: runs locally; no audio leaves the machine. There is no "
            "accuracy percentage anywhere -- results are reproducible timing "
            "measurements with every threshold exposed and every frame inspectable "
            "(see --dump-frames)."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    # single-recording inputs
    r.add_argument("--stereo", help="two-channel WAV (caller on one channel, agent on the other)")
    r.add_argument("--caller", help="mono WAV of the caller channel")
    r.add_argument("--agent", help="mono WAV of the agent channel")
    r.add_argument("--onset", type=float, default=None, help="caller onset in seconds (else auto-detected)")
    r.add_argument("--expect", default="yield", choices=["yield", "hold"],
                   help="expected behaviour for a single recording: 'yield' (stop for the caller) or 'hold' (keep the floor; the caller event is a backchannel)")
    r.add_argument("--max-talk-over", type=float, default=None, help="fail if talk-over exceeds this many seconds")
    r.add_argument("--max-time-to-yield", type=float, default=None, help="fail if the yield is slower than this many seconds")
    # battery input
    r.add_argument("--suite", nargs="?", const=SUITE_ID, default=None,
                   help=f"run a labelled battery instead of a single file (default suite: {SUITE_ID!r})")
    r.add_argument("--scenarios", default=None, help="dir of scenario JSON labels (defaults to the bundled battery)")
    r.add_argument("--audio", default=None, help="dir of scenario audio (defaults to the bundled fixtures)")
    # shared
    r.add_argument("--stack", default="generic",
                   choices=["generic", "vapi", "twilio", "livekit", "pipecat", "retell"],
                   help="voice stack the recording came from (livekit|pipecat|vapi|generic); tunes the config-fix knob names")
    r.add_argument("--backend", default="energy", choices=["energy", "neural"],
                   help="VAD backend for a single recording: 'energy' (default -- the "
                        "deterministic REFERENCE behind every published number) or "
                        "'neural' (OPTIONAL, non-reference cross-check via the [neural] "
                        "extra; tightens onset precision but does NOT recover intent -- a "
                        "cough still reads as speech energy, and no accuracy is claimed). "
                        "The --suite self-test always uses the energy reference. Without "
                        "the [neural] extra installed, --backend neural errors cleanly.")
    r.add_argument("--caller-channel", type=int, default=0)
    r.add_argument("--agent-channel", type=int, default=1)
    r.add_argument("--format", default="json", choices=["json", "text"], help="output format (default json)")
    r.add_argument("--dump-frames", default=None, metavar="PATH",
                   help="write the per-frame VAD evidence (t_sec, per-channel dBFS, "
                        "active flags, threshold and noise floor for both channels) "
                        "to PATH as JSON, so every reported number is re-derivable "
                        "by hand; requires a single recording (--stereo or --caller/--agent)")
    r.add_argument("--no-fail", action="store_true", help="always exit 0 (do not fail CI on a regression)")
    r.set_defaults(func=_cmd_run)

    # --- capture: score YOUR OWN call from a specific stack ----------------
    c = sub.add_parser(
        "capture",
        help="score a real call from your stack (the out-of-box aha)",
        description=(
            "Capture a real dual-channel call from your voice stack and score its "
            "turn-taking. Vapi and Twilio pull the recording for you (API key only, "
            "no SDK); LiveKit/Pipecat capture in your own infra (see `hotato setup`), "
            "then pass the file here. Everything is scored OFFLINE; the only network "
            "is the direct recording download. There is no accuracy percentage -- "
            "reproducible timing measurements only."
        ),
        epilog=(
            "Examples:\n"
            "  hotato capture --stack vapi --call-id <id>            # + VAPI_API_KEY\n"
            "  hotato capture --stack twilio --recording-sid RE...   # + TWILIO_ACCOUNT_SID/TOKEN\n"
            "  hotato capture --stack livekit --caller a.wav --agent b.wav\n"
            "  hotato capture --stack pipecat --stereo captured.wav\n"
            "  hotato capture --stack vapi --demo                    # offline, zero deps"
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    c.add_argument("--stack", required=True, choices=list(_capture.STACKS),
                   help="voice stack the call came from")
    c.add_argument("--demo", action="store_true",
                   help="prove the capture -> score loop on a bundled two-channel reference (offline, zero deps, no API)")
    # already-captured input (works for every stack, incl. livekit/pipecat/retell)
    c.add_argument("--stereo", "--wav", dest="stereo",
                   help="score an existing two-channel WAV (caller on ch0, agent on ch1)")
    c.add_argument("--caller", help="mono WAV of the caller channel (with --agent)")
    c.add_argument("--agent", help="mono WAV of the agent channel (with --caller)")
    # vapi
    c.add_argument("--call-id", help="[vapi] the id of an ended, recorded call")
    c.add_argument("--api-key", help="[vapi] private API key (else env VAPI_API_KEY)")
    # twilio
    c.add_argument("--recording-sid", help="[twilio] the Recording SID (RE...) of a dual-channel recording")
    c.add_argument("--account-sid", help="[twilio] Account SID (else env TWILIO_ACCOUNT_SID)")
    c.add_argument("--auth-token", help="[twilio] Auth Token (else env TWILIO_AUTH_TOKEN)")
    # shared scoring knobs
    c.add_argument("--onset", type=float, default=None, help="caller onset in seconds (else auto-detected)")
    c.add_argument("--expect", default="yield", choices=["yield", "hold"],
                   help="'yield' (agent should stop for the caller) or 'hold' (caller event is a backchannel)")
    c.add_argument("--caller-channel", type=int, default=0)
    c.add_argument("--agent-channel", type=int, default=1)
    c.add_argument("--out", default=None, help="where to write the downloaded recording (else a temp file)")
    c.add_argument("--format", default="text", choices=["json", "text"], help="output format (default text)")
    c.set_defaults(func=_cmd_capture)

    # --- setup: scaffold the exact recording config for a stack -----------
    s = sub.add_parser(
        "setup",
        help="print the exact dual-channel recording config for a stack",
        description=(
            "Print the copy-paste recording scaffold for your stack: how to turn on "
            "dual-channel / two-track / stereo capture so caller and agent stay on "
            "separate channels, plus the command to score the result."
        ),
    )
    s.add_argument("--stack", required=True, choices=list(_capture.STACKS),
                   help="voice stack to scaffold")
    s.set_defaults(func=_cmd_setup)

    return p


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    # Bare `hotato` (no subcommand): guide the user to score their OWN call.
    if getattr(args, "func", None) is None:
        print(_FIRST_RUN_GUIDE, end="")
        return 0
    try:
        return args.func(args)
    except (ValueError, FileNotFoundError, BackendUnavailable) as exc:
        # BackendUnavailable = --backend neural requested without the [neural] extra
        # (or without cached weights): a clean, explicit config error, never a silent
        # fallback to the energy reference.
        print(f"error: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
