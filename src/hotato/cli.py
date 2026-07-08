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
import os
import sys
import tempfile

from . import __version__
from . import capture as _capture
from . import errors as _errors
from ._engine.score import ScoreConfig
from ._engine.vad import BackendUnavailable, VADParams
from .core import SUITE_ID, dump_frames_for_input, process_exit_code, run_single, run_suite

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
  Retell:   hotato capture --stack retell --call-id <id>          # + RETELL_API_KEY

Already have a 2-channel WAV (caller on channel 0, agent on channel 1)?
  hotato run --stereo your_call.wav --expect yield

Turn a bad moment into a permanent regression test (docs/BAD-CALL-TO-CI.md):
  hotato scan --stereo full_call.wav                # list candidate moments
  hotato fixture create --stereo full_call.wav --onset 42.18 \\
      --expect yield --id refund-cutoff-001 --out tests/hotato
  hotato run --scenarios tests/hotato/scenarios --audio tests/hotato/audio

See what a failure looks like, in one command (packaged bad-agent battery; it
fails by design and opens the report):
  hotato demo

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

# The label contract, stated wherever yield/hold appears (canonical wording;
# also in README.md, METHODOLOGY.md, and docs/BAD-CALL-TO-CI.md).
_LABEL_NOTE = (
    "Hotato does not infer intent. You label the expected behavior for the "
    "event: yield means the agent should stop for the caller. hold means the "
    "agent should keep speaking through a backchannel/noise/acknowledgement. "
    "Hotato then measures whether the timing matched that label."
)

# The single source of truth for every subcommand's exit-code contract. Keyed
# by the dotted subcommand name ("benchmark compare", "fixture create", ...).
# Both the per-subparser "Exit codes:" epilog (below) AND `hotato describe`'s
# capability manifest are templated straight from this table, so the two
# surfaces can never drift apart. Append-only in spirit: a shipped code's
# meaning does not change once documented.
_EXIT_CODES: dict = {
    "run": (
        (0, "every scorable event passed"),
        (1, "a scorable event failed (regression)"),
        (2, "usage error or unusable input (bad flags, a corrupt file, or a "
            "single recording with no scorable events); --no-fail always "
            "exits 0"),
    ),
    "capture": (
        (0, "captured and scored, every scorable event passed"),
        (1, "a scorable event failed"),
        (2, "usage error, missing credentials, or unusable input (including "
            "a capture with no scorable events)"),
    ),
    "setup": (
        (0, "the recording scaffold was printed"),
    ),
    "connect": (
        (0, "credentials stored (0600); a live auth check ran when the stack "
            "supports it"),
        (2, "usage error, missing credentials, or a failed auth check (nothing "
            "stored)"),
    ),
    "pull": (
        (0, "listed and fetched recent recordings; per-call fetch failures are "
            "reported as skips, never a crash"),
        (2, "usage error, missing credentials, --allow-mono required, or a stack "
            "with no list endpoint and no explicit ids"),
    ),
    "sweep": (
        (0, "pulled recent recordings then analyzed them (candidate moments "
            "listed, possibly zero; never a pass/fail and never a verdict)"),
        (2, "usage error, missing credentials, --allow-mono required, or a stack "
            "with no list endpoint and no explicit ids"),
    ),
    "report": (
        (0, "report written, every scorable event passed"),
        (1, "a scorable event failed"),
        (2, "usage error or unusable input; --no-fail always exits 0"),
    ),
    "team": (
        (0, "aggregated (fewer than 2 runs is stated plainly, never padded "
            "into a trend); --no-fail always exits 0"),
        (1, "--max-response-gap latency SLA breached"),
        (2, "usage error or an unreadable run directory"),
    ),
    "export": (
        (0, "exported, every scorable event passed"),
        (1, "a scorable event failed, or --max-response-gap latency SLA "
            "breached; --no-fail always exits 0"),
        (2, "usage error or unusable input"),
    ),
    "benchmark": (
        (0, "scored (a regression is reported but does not fail by default)"),
        (1, "with --fail-on-regression, a scored event failed its scenario "
            "thresholds"),
        (2, "usage error (missing --stack / --recordings) or unusable "
            "input"),
    ),
    "benchmark compare": (
        (0, "compared (measurements only; never a gate)"),
        (2, "usage error (fewer than two result files) or unreadable "
            "input"),
    ),
    "doctor": (
        (0, "every scorable event passed"),
        (1, "a scorable event failed"),
        (2, "usage error or unusable input; --no-fail always exits 0"),
    ),
    "demo": (
        (0, "ran (the battery fails by design; stays 0 unless --fail)"),
        (1, "with --fail, the real regression code -- this battery fails by "
            "design"),
    ),
    "diagnose": (
        (0, "no failing events"),
        (1, "failing events were diagnosed"),
        (2, "unusable input"),
    ),
    "inspect": (
        (0, "inspected"),
        (2, "missing credentials, bad flags, or an unreadable file"),
    ),
    "plan": (
        (0, "plan written (including refusals)"),
        (2, "unusable input or missing credentials"),
    ),
    "fixture": (
        (2, "no subcommand given (see hotato fixture create --help)"),
    ),
    "fixture create": (
        (0, "fixture written (scored immediately)"),
        (2, "refused: unusable input or a not-scorable moment"),
    ),
    "compare": (
        (0, "compared (measures, does not gate by default)"),
        (1, "with --fail-on-worse, the result is regressed or worse"),
        (2, "usage error, unusable input, or a not-scorable side"),
    ),
    "scan": (
        (0, "scanned (with or without candidates; the count is reported)"),
        (2, "usage error or unreadable input"),
    ),
    "ingest": (
        (0, "ran (candidates reported, possibly zero; never a pass/fail)"),
        (2, "parse / fetch / IO error, or not-scorable input"),
    ),
    "analyze": (
        (0, "ran (candidate moments listed across the folder, possibly zero; "
            "never a pass/fail and never a verdict)"),
        (2, "usage error (not a folder) or an IO error reading the folder"),
    ),
    "patch": (
        (0, "patch produced (a config merge-patch/curl or source edit for a "
            "config-fixable plan, or the vendor-neutral engagement-control "
            "pointer for the both-axes case -- both are valid outputs; hotato "
            "never applies the change)"),
        (2, "the input is not a hotato fix plan, or is unreadable"),
    ),
    "verify": (
        (0, "verified: the before/after rollup was produced (a low-n claim is "
            "refused honestly but still exits 0; the per-fixture facts hold)"),
        (1, "with --fail-on-regression, at least one fixture regressed or got "
            "worse"),
        (2, "usage error, unreadable input, or no fixtures pair between the two "
            "sides"),
    ),
    "loop": (
        (0, "advanced the loop and persisted state (or re-reported where it "
            "left off)"),
        (2, "usage error: no folder on the first run, an unreadable state file, "
            "or a path that is not a folder"),
    ),
    "describe": (
        (0, "manifest printed"),
    ),
}


def _exit_codes_epilog(key: str) -> str:
    """Render the ``Exit codes:`` line for subcommand ``key`` from the single
    ``_EXIT_CODES`` source of truth, so the CLI --help text and `hotato
    describe`'s manifest can never say something different."""
    parts = ", ".join(f"{code} = {desc}" for code, desc in _EXIT_CODES[key])
    return f"Exit codes: {parts}."


def _add_cred_args(parser) -> None:
    """The shared credential flags for connect/pull/sweep. Each falls back to
    ~/.hotato/connections.json then the stack's environment variable, so after
    `hotato connect` they are optional."""
    parser.add_argument("--api-key", default=None,
                        help="vendor API key (vapi/retell/bland/elevenlabs/"
                             "synthflow/millis/cartesia); else the connection or "
                             "the stack's env var")
    parser.add_argument("--account-sid", default=None,
                        help="[twilio] Account SID (else the connection or "
                             "TWILIO_ACCOUNT_SID)")
    parser.add_argument("--auth-token", default=None,
                        help="[twilio] Auth Token (else the connection or "
                             "TWILIO_AUTH_TOKEN)")
    parser.add_argument("--model-id", default=None,
                        help="[synthflow] model id required by its list endpoint "
                             "(else the connection or SYNTHFLOW_MODEL_ID)")
    parser.add_argument("--agent-id", default=None,
                        help="[cartesia] agent id required by its list endpoint "
                             "(else the connection or CARTESIA_AGENT_ID)")
    parser.add_argument("--base-url", default=None,
                        help="[millis] regional API base (else the connection or "
                             "the US default)")


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
    n_not_scorable = s.get("not_scorable", 0)
    counts = f"failed={s['failed']}"
    if n_not_scorable:
        counts += f", not_scorable={n_not_scorable}"
    print(f"  {s['passed']}/{s['events']} events pass  ({counts})")
    for e in env["events"]:
        v = e["verdict"]
        if e.get("scorable") is False:
            # An input problem, never an agent verdict: no PASS, no FAIL.
            print(f"  [NOT SCORABLE] {e['event_id']}")
            print(f"         reason: {e['not_scorable_reason']}")
            continue
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
    # The envelope exit_code is schema-frozen to 0|1 and reflects scorable
    # failures only. When the process-level code differs (a single run whose
    # every event is not scorable maps to the CLI's exit-2 unusable-input
    # convention), printing the envelope code would mislead; print the code
    # the process actually returns instead. Fully-scorable runs keep the
    # exact `exit_code=` line.
    pec = process_exit_code(env)
    if pec != env["exit_code"]:
        print(f"  process_exit_code={pec}")
    else:
        print(f"  exit_code={env['exit_code']}")


def _cmd_run(args) -> int:
    backend = getattr(args, "backend", "energy")
    # A battery runs on an explicit --suite, OR on --scenarios and --audio given
    # together without it: that is the exact command `fixture create` emits in
    # its own `next` field (and documents in this command's epilog and
    # docs/BAD-CALL-TO-CI.md) -- it must not need a bare --suite bolted on.
    suite_mode = bool(args.suite) or bool(args.scenarios and args.audio)
    # Conflicting inputs: a battery run scores multiple labelled scenarios and
    # silently ignoring a single recording passed alongside it would mislead.
    # Reject the combination up front (clean usage error -> exit 2) rather than
    # quietly dropping the user's file.
    if suite_mode and (args.stereo or args.caller or args.agent):
        raise ValueError(
            "--suite (or --scenarios/--audio together) runs a labelled battery "
            "and cannot be combined with a single recording (--stereo / --caller "
            "/ --agent). Run one or the other."
        )
    if args.dump_frames:
        if suite_mode:
            raise ValueError(
                "--dump-frames works on a single recording; drop --suite (and/or "
                "--scenarios/--audio) and pass --stereo, or --caller and --agent"
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
    if suite_mode:
        # The bundled battery is the ENERGY reference: it always scores with energy
        # so the golden numbers stay byte-stable, regardless of --backend.
        env = run_suite(
            suite=args.suite or SUITE_ID,
            stack=args.stack,
            scenarios_dir=args.scenarios,
            audio_dir=args.audio,
            caller_channel=args.caller_channel,
            agent_channel=args.agent_channel,
            echo_gate=getattr(args, "echo_gate", False),
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
            echo_gate=getattr(args, "echo_gate", False),
        )
    _emit(env, args.format)
    if args.no_fail:
        return 0
    return process_exit_code(env)


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
        allow_mono=args.allow_mono,
        out=args.out,
        fmt=args.format,
    )


def _cmd_setup(args) -> int:
    return _capture.run_setup(args.stack)


def _cmd_connect(args) -> int:
    return _capture.run_connect(
        args.stack,
        api_key=args.api_key,
        account_sid=args.account_sid,
        auth_token=args.auth_token,
        model_id=args.model_id,
        agent_id=args.agent_id,
        base_url=args.base_url,
        no_verify=args.no_verify,
        fmt=args.format,
    )


def _cmd_pull(args) -> int:
    return _capture.run_pull(
        args.stack,
        ids=args.call_id or None,
        since=args.since,
        limit=args.limit,
        out=args.out,
        allow_mono=args.allow_mono,
        api_key=args.api_key,
        account_sid=args.account_sid,
        auth_token=args.auth_token,
        model_id=args.model_id,
        agent_id=args.agent_id,
        base_url=args.base_url,
        fmt=args.format,
    )


def _cmd_sweep(args) -> int:
    return _capture.run_sweep(
        args.stack,
        ids=args.call_id or None,
        since=args.since,
        limit=args.limit,
        dir=args.dir,
        out=args.out,
        allow_mono=args.allow_mono,
        top=args.top,
        audio_top=args.audio_top,
        pre=args.pre,
        post=args.post,
        min_gap=args.min_gap,
        no_open=args.no_open,
        api_key=args.api_key,
        account_sid=args.account_sid,
        auth_token=args.auth_token,
        model_id=args.model_id,
        agent_id=args.agent_id,
        base_url=args.base_url,
        caller_channel=args.caller_channel,
        agent_channel=args.agent_channel,
        fmt=args.format,
    )


def _load_base_envelope(path: str) -> dict:
    """Load a previous envelope JSON for --base. Anything that is not a hotato
    envelope is a clean usage error (exit 2), never a silent no-op diff."""
    with open(path, encoding="utf-8") as fh:
        base = json.load(fh)
    if not (isinstance(base, dict) and base.get("tool") == "hotato"
            and base.get("kind") != "frame-dump"
            and isinstance(base.get("events"), list)):
        raise ValueError(
            f"--base {path!r} is not a hotato envelope JSON. Save one with: "
            "hotato run --suite barge-in --format json > base.json"
        )
    return base


def _cmd_report(args) -> int:
    from . import report as _report

    # --suite is the bundled self-test battery; combining it with one recording
    # would silently drop the file, so reject the mix (clean usage error -> 2).
    if args.suite and (args.stereo or args.caller or args.agent):
        raise ValueError(
            "--suite renders the bundled self-test battery and cannot be combined "
            "with a single recording (--stereo / --caller / --agent). Run one or "
            "the other."
        )
    base = _load_base_envelope(args.base) if args.base else None
    base_label = os.path.basename(args.base) if args.base else None
    out = args.out
    if out is None:
        out = "hotato-report.md" if args.format == "md" else "hotato-report.html"
    if args.suite:
        env = _report.write_report(
            out,
            fmt=args.format,
            embed_audio=args.embed_audio,
            base=base,
            base_label=base_label,
            suite=args.suite,
            stack=args.stack,
            scenarios_dir=args.scenarios,
            audio_dir=args.audio,
        )
    else:
        if not (args.stereo or (args.caller and args.agent)):
            raise ValueError(
                "provide --stereo FILE, or both --caller FILE and --agent FILE, "
                "or --suite to render the bundled battery"
            )
        env = _report.write_report(
            out,
            fmt=args.format,
            embed_audio=args.embed_audio,
            base=base,
            base_label=base_label,
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
        )
    kind = ("self-contained HTML report" if args.format == "html"
            else "markdown report")
    print(
        f"wrote {kind} ({env['summary']['events']} events) to {out}",
        file=sys.stderr,
    )
    if args.embed_audio:
        # Embedding grows the file by roughly the audio size; state the total
        # plainly so nobody ships a page they have not sized.
        size = os.path.getsize(out)
        print(f"report size: {size} bytes ({size / 1048576.0:.1f} MB) "
              f"with audio embedded", file=sys.stderr)
    if args.no_fail:
        return 0
    return process_exit_code(env)


def _emit_team_text(agg: dict, dirpath: str) -> None:
    pr = agg["pass_rate"]
    latest = agg["pass_rate_over_time"][-1]
    print(f"hotato team: {agg['runs']} runs from {dirpath} "
          f"(ordered by {agg['ordered_by']})")
    print(f"  events: {agg['events_total']} total")
    if pr["latest"] is not None:
        print(f"  pass rate: latest {latest['passed']} of {latest['events']} "
              f"({pr['latest']:.2f}), mean {pr['mean']:.2f}")
        print(f"  trend: {pr['first']:.2f} to {pr['latest']:.2f} "
              f"({pr['direction']}) across {agg['runs']} runs")
    for name, key in (("talk-over", "talk_over_sec"),
                      ("time to yield", "seconds_to_yield")):
        d = agg[key]
        if d:
            print(f"  {name}: mean {d['mean']:.2f}s median {d['median']:.2f}s "
                  f"p90 {d['p90']:.2f}s (n={d['n']})")
        else:
            print(f"  {name}: no measurements")
    d = agg["response_gap_sec"]
    if d:
        print(f"  response gap: mean {d['mean']:.2f}s median {d['median']:.2f}s "
              f"p90 {d['p90']:.2f}s p95 {d['p95']:.2f}s (n={d['n']})")
    else:
        print("  response gap: no measurements")
    sla = agg["latency_sla"]
    if sla["bound_sec"] is not None:
        observed = (f'{sla["observed_p95_sec"]:.2f}s'
                    if sla["observed_p95_sec"] is not None else "no measurements")
        verdict = "pass" if sla["passed"] else "fail"
        print(f"  latency SLA: p95 response gap {observed} vs bound "
              f"{sla['bound_sec']:.2f}s ({verdict})")
    mc = agg["most_common_failure_class"]
    if mc:
        print(f"  most common failure class: {mc['fix_class']} "
              f"({mc['count']} of {mc['of_failures']} failures)")
    else:
        print("  most common failure class: no failures")
    if agg["skipped"]:
        skipped = ", ".join(s["file"] for s in agg["skipped"])
        print(f"  skipped (not run envelopes): {skipped}")


def _cmd_team(args) -> int:
    from . import aggregate as _aggregate

    loaded = _aggregate.load_run_dir(args.dir, order=args.order)
    runs = loaded["runs"]
    if len(runs) < 2:
        # Stated plainly, exit 0: one run has no trend and we never pad one.
        print(
            f"team mode needs at least 2 run envelopes to aggregate; found "
            f"{len(runs)} in {args.dir}. Save runs with: "
            "hotato run --suite barge-in --format json > runs/001.json"
        )
        return 0
    agg = _aggregate.aggregate_runs(runs, order=args.order,
                                    skipped=loaded["skipped"],
                                    max_response_gap_sec=args.max_response_gap)
    if args.out:
        with open(args.out, "w", encoding="utf-8") as fh:
            json.dump(agg, fh, indent=2)
        print(f"wrote aggregate envelope to {args.out}", file=sys.stderr)
    if args.html:
        with open(args.html, "w", encoding="utf-8") as fh:
            fh.write(_aggregate.build_team_page_html(agg))
        print(f"wrote self-contained HTML team page to {args.html}",
              file=sys.stderr)
    if args.format == "json":
        print(json.dumps(agg, indent=2))
    else:
        _emit_team_text(agg, args.dir)
    if args.no_fail:
        return 0
    return agg["exit_code"]


def _cmd_export(args) -> int:
    from . import export as _export

    if args.suite and (args.stereo or args.caller or args.agent):
        raise ValueError(
            "--suite exports the bundled self-test battery and cannot be "
            "combined with a single recording (--stereo / --caller / --agent). "
            "Run one or the other."
        )
    if not args.suite and not (args.stereo or (args.caller and args.agent)):
        raise ValueError(
            "provide --stereo FILE, or both --caller FILE and --agent FILE, "
            "or --suite to export the bundled battery"
        )
    res = _export.run_export(
        out_dir=args.out,
        stereo=args.stereo,
        caller=args.caller,
        agent=args.agent,
        caller_channel=args.caller_channel,
        agent_channel=args.agent_channel,
        onset_sec=args.onset,
        expect=args.expect,
        stack=args.stack,
        suite=args.suite,
        scenarios_dir=args.scenarios,
        audio_dir=args.audio,
        max_talk_over_sec=args.max_talk_over,
        max_time_to_yield_sec=args.max_time_to_yield,
        max_response_gap_sec=args.max_response_gap,
    )
    print(
        f"wrote {res['events_rows']} event rows to {res['paths']['events']}, "
        f"{res['frames_rows']} frame rows to {res['paths']['frames']}, "
        f"and the envelope to {res['paths']['envelope']}",
        file=sys.stderr,
    )
    d = res["latency_summary"]["response_gap_sec"]
    if d:
        print(f"response gap: mean {d['mean']:.2f}s median {d['median']:.2f}s "
              f"p90 {d['p90']:.2f}s p95 {d['p95']:.2f}s (n={d['n']})",
              file=sys.stderr)
    sla = res["latency_sla"]
    if sla["bound_sec"] is not None:
        observed = (f'{sla["observed_p95_sec"]:.2f}s'
                    if sla["observed_p95_sec"] is not None else "no measurements")
        verdict = "pass" if sla["passed"] else "fail"
        print(f"latency SLA: p95 response gap {observed} vs bound "
              f"{sla['bound_sec']:.2f}s ({verdict})", file=sys.stderr)
    if args.no_fail:
        return 0
    if sla["passed"] is False:
        return 1
    return process_exit_code(res["env"])


def _cmd_benchmark(args) -> int:
    from . import stackbench as _stackbench

    if not args.stack or not args.recordings:
        raise ValueError(
            "hotato benchmark scores YOUR captured recordings against a fixed "
            "scenario set: provide --stack and --recordings DIR (one dual-channel "
            "recording per scenario, named <scenario-id>.wav). To compare saved "
            "results: hotato benchmark compare A.json B.json"
        )
    result = _stackbench.run_stackbench(
        stack=args.stack,
        recordings_dir=args.recordings,
        scenarios_dir=args.scenarios,
        suffix=args.suffix,
        caller_channel=args.caller_channel,
        agent_channel=args.agent_channel,
    )
    sc = result["scenarios"]
    print(
        f"scored {sc['captured']} of {sc['total']} scenarios from "
        f"{args.recordings} (stack={result['stack']})",
        file=sys.stderr,
    )
    if sc["not_captured"]:
        # Stated plainly; these were never scored and never count as failures.
        print(
            "not captured (no matching recording; not scored, not failed): "
            + ", ".join(sc["not_captured"]),
            file=sys.stderr,
        )
    if args.out:
        with open(args.out, "w", encoding="utf-8") as fh:
            json.dump(result, fh, indent=2)
            fh.write("\n")
        print(f"wrote stack benchmark result to {args.out}", file=sys.stderr)
    else:
        print(json.dumps(result, indent=2))
    if args.fail_on_regression and result["summary"]["regression"]:
        return 1
    return 0


def _cmd_benchmark_compare(args) -> int:
    from . import stackbench as _stackbench

    if len(args.results) < 2:
        raise ValueError(
            "compare needs at least two benchmark result files: "
            "hotato benchmark compare A.json B.json"
        )
    loaded = [(p, _stackbench.load_result(p)) for p in args.results]
    cmp_env = _stackbench.compare_results(loaded)
    if args.format == "json":
        text = json.dumps(cmp_env, indent=2)
    else:
        text = _stackbench.render_comparison_md(cmp_env)
    if args.out:
        with open(args.out, "w", encoding="utf-8") as fh:
            fh.write(text)
            if not text.endswith("\n"):
                fh.write("\n")
        print(
            f"wrote comparison ({len(cmp_env['compared'])} shared scenarios, "
            f"{len(cmp_env['skipped'])} skipped) to {args.out}",
            file=sys.stderr,
        )
    else:
        print(text)
    return 0


def _try_open(path: str) -> None:
    """Best-effort: open the report in a browser. Never crash if headless; on a
    clearly-headless machine just print the path so the run stays clean."""
    abspath = os.path.abspath(path)
    headless = (
        sys.platform.startswith("linux")
        and not os.environ.get("DISPLAY")
        and not os.environ.get("WAYLAND_DISPLAY")
    )
    if not headless:
        try:
            import webbrowser

            if webbrowser.open("file://" + abspath):
                return
        except Exception:
            pass
    print(f"open it in your browser to see the per-event timelines: {abspath}")


def _cmd_doctor(args) -> int:
    from . import report as _report

    # The 5-minute path in one command: score a recording if given, else run the
    # bundled self-test; render the HTML report; open it best-effort. A pure
    # convenience wrapper over the existing scorer + report -- nothing new claimed.
    has_recording = bool(args.stereo or (args.caller and args.agent))
    out = args.out or os.path.join(tempfile.gettempdir(), "hotato-report.html")

    if has_recording:
        # A real recording gets its audio embedded: the report is the shareable
        # artifact, and hearing the exact scored call next to its timeline is
        # the point. The self-test below stays unembedded (synthetic fixtures,
        # smaller page).
        html_str, env = _report.build_report_html(
            stereo=args.stereo,
            caller=args.caller,
            agent=args.agent,
            caller_channel=args.caller_channel,
            agent_channel=args.agent_channel,
            onset_sec=args.onset,
            expect=args.expect,
            stack=args.stack,
            embed_audio=True,
        )
    else:
        # No recording (or explicit --demo): fall back to the bundled self-test.
        html_str, env = _report.build_report_html(suite=SUITE_ID, stack=args.stack)
        print(_SELF_TEST_NOTE, file=sys.stderr)

    with open(out, "w", encoding="utf-8") as fh:
        fh.write(html_str)

    fmt = getattr(args, "format", "text")
    if fmt == "json":
        # Mirrors `demo --format json`: stdout stays the pure machine
        # envelope, every human-readable line (including the report path)
        # goes to stderr, so an agent parsing stdout never has to skip lines.
        _emit(env, "json")
        print(f"report: {out}", file=sys.stderr)
    else:
        _emit(env, "text")
        print(f"\nreport: {out}")

    if args.no_open:
        msg = (f"open it in your browser to see the per-event timelines: "
               f"{os.path.abspath(out)}")
        print(msg, file=sys.stderr if fmt == "json" else sys.stdout)
    else:
        _try_open(out)

    if args.no_fail:
        return 0
    return process_exit_code(env)


# --- the guarded fix ladder (read-only phase): diagnose -> inspect -> plan ---

def _load_envelope_for(path: str, flag: str) -> dict:
    """Load an envelope JSON for diagnose/plan; anything else (a frame dump,
    a benchmark result, a compare result, arbitrary JSON) is a clean usage
    error (exit 2). A run envelope carries no ``kind`` key."""
    with open(path, encoding="utf-8") as fh:
        env = json.load(fh)
    if not (isinstance(env, dict) and env.get("tool") == "hotato"
            and env.get("kind") is None
            and isinstance(env.get("events"), list)):
        raise ValueError(
            f"{flag} {path!r} is not a hotato run envelope JSON (frame dumps, "
            "benchmark results, and compare results are not run envelopes). "
            "Save one with: hotato run --suite barge-in --format json > "
            "result.json"
        )
    return env


def _cmd_diagnose(args) -> int:
    from . import diagnose as _diagnose

    env = _load_envelope_for(args.envelope, "diagnose")
    diagnosis = _diagnose.diagnose_envelope(env, source=args.envelope)
    if args.format == "json":
        print(json.dumps(diagnosis, indent=2))
    else:
        print(_diagnose.render_text(diagnosis))
    # 0 = nothing failed, 1 = failing events were diagnosed, 2 = unusable input.
    return 1 if diagnosis["battery"]["failed"] else 0


def _cmd_inspect(args) -> int:
    from . import inspectcfg as _inspectcfg

    result = _inspectcfg.run_inspect(
        stack=args.stack,
        assistant_id=args.assistant_id,
        agent_id=args.agent_id,
        config=args.config,
        api_key=args.api_key,
    )
    if args.format == "json":
        print(json.dumps(result, indent=2))
    else:
        print(_inspectcfg.render_text(result))
    return 0


def _cmd_plan(args) -> int:
    from . import diagnose as _diagnose
    from . import fixplan as _fixplan
    from . import inspectcfg as _inspectcfg

    # The result JSON arrives either as the positional argument
    # (hotato plan result.json) or as --run result.json; exactly one.
    if args.result_json and args.run and args.result_json != args.run:
        raise ValueError(
            "two different result files were given (positional "
            f"{args.result_json!r} and --run {args.run!r}); pass one"
        )
    run_path = args.run or args.result_json
    if not run_path:
        raise ValueError(
            "provide the finished run to plan from: hotato plan result.json "
            "(or --run result.json). Save one with: hotato run --suite "
            "barge-in --format json > result.json"
        )
    env = _load_envelope_for(run_path, "plan input")
    diagnosis = _diagnose.diagnose_envelope(env, source=run_path)

    inspected = None
    target_info = {}
    has_target = bool(args.assistant_id or args.agent_id or args.config)
    if has_target:
        if args.stack == "twilio":
            raise ValueError(
                "Twilio carries the audio but has no turn-taking agent "
                "config to inspect; point the target flag at the stack that "
                "runs the agent (--stack vapi|retell|livekit|pipecat)"
            )
        if not args.stack or args.stack == "generic":
            raise ValueError(
                "a target flag (--assistant-id / --agent-id / --config) needs "
                "--stack vapi|retell|livekit|pipecat so plan knows how to "
                "inspect it"
            )
        inspected = _inspectcfg.run_inspect(
            stack=args.stack,
            assistant_id=args.assistant_id,
            agent_id=args.agent_id,
            config=args.config,
            api_key=args.api_key,
        )
        target_info = {
            k: v for k, v in (
                ("assistant_id", args.assistant_id),
                ("agent_id", args.agent_id),
                ("config_path", args.config),
            ) if v
        }

    plan = _fixplan.build_plan(
        diagnosis=diagnosis,
        inspected=inspected,
        stack=args.stack,
        target_info=target_info,
    )
    with open(args.out, "w", encoding="utf-8") as fh:
        json.dump(plan, fh, indent=2)
        fh.write("\n")
    if args.format == "json":
        print(json.dumps(plan, indent=2))
    else:
        print(_fixplan.render_text(plan))
    print(f"wrote fix plan ({plan['decision']}) to {args.out}", file=sys.stderr)
    return 0


def _cmd_patch(args) -> int:
    from . import patch as _patch

    # A fix plan JSON (hotato.fixplan.v1), not a run envelope. A missing file
    # (FileNotFoundError), malformed JSON (ValueError), or a non-plan document
    # (ValueError from build_patch) all surface as the clean exit-2 usage error.
    with open(args.fixplan, encoding="utf-8") as fh:
        plan = json.load(fh)
    result = _patch.build_patch(plan, source=args.fixplan)
    if args.out:
        with open(args.out, "w", encoding="utf-8") as fh:
            json.dump(result, fh, indent=2)
            fh.write("\n")
        print(f"wrote patch artifact to {args.out}", file=sys.stderr)
    if args.format == "json":
        print(json.dumps(result, indent=2))
    else:
        print(_patch.render_text(result))
    return 0


def _cmd_verify(args) -> int:
    from . import verify as _verify

    result = _verify.verify_sides(args.before, args.after, min_n=args.min_n)
    if args.out:
        with open(args.out, "w", encoding="utf-8") as fh:
            json.dump(result, fh, indent=2)
            fh.write("\n")
        print(f"wrote verify proof to {args.out}", file=sys.stderr)
    if args.format == "json":
        print(json.dumps(result, indent=2))
    else:
        print(_verify.render_text(result))
    # 0 = rollup produced; 1 = a regression, only when the user opts to gate on it.
    if args.fail_on_regression and result["regressions"]:
        return 1
    return 0


def _cmd_loop(args) -> int:
    from . import loop as _loop

    result, code = _loop.run_loop(
        args.folder,
        fixtures_dir=args.fixtures,
        state_path=args.state,
        rediscover=args.rediscover,
        stack=args.stack,
        min_gap=args.min_gap,
        top=args.top,
    )
    if args.format == "json":
        print(json.dumps(result, indent=2))
    else:
        print(_loop.render_text(result))
    return code


# --- the regression loop: scan -> fixture create -> run -> compare ---------

def _cmd_fixture_create(args) -> int:
    from . import fixture as _fixture

    result = _fixture.create_fixture(
        stereo=args.stereo,
        caller=args.caller,
        agent=args.agent,
        fixture_id=args.id,
        title=args.title,
        onset_sec=args.onset,
        expect=args.expect,
        out_dir=args.out,
        stack=args.stack,
        max_talk_over_sec=args.max_talk_over,
        max_time_to_yield_sec=args.max_time_to_yield,
        tags=args.tags,
        category=args.category,
        pre_sec=args.pre,
        post_sec=args.post,
        no_clip=args.no_clip,
        force=args.force,
        caller_channel=args.caller_channel,
        agent_channel=args.agent_channel,
    )
    if args.format == "json":
        print(json.dumps(_fixture.result_json(result), indent=2))
    else:
        print(_fixture.render_text(result))
    return 0


def _cmd_compare(args) -> int:
    from . import compare as _compare
    from . import report as _report

    cmp_env = _compare.compare_recordings(
        before_stereo=args.before,
        before_caller=args.before_caller,
        before_agent=args.before_agent,
        after_stereo=args.after,
        after_caller=args.after_caller,
        after_agent=args.after_agent,
        onset_sec=args.onset,
        before_onset_sec=args.before_onset,
        after_onset_sec=args.after_onset,
        expect=args.expect,
        stack=args.stack,
        max_talk_over_sec=args.max_talk_over,
        max_time_to_yield_sec=args.max_time_to_yield,
        caller_channel=args.caller_channel,
        agent_channel=args.agent_channel,
    )
    before_name = _compare.input_name(args.before, args.before_caller,
                                      args.before_agent)
    after_name = _compare.input_name(args.after, args.after_caller,
                                     args.after_agent)
    if args.out:
        # The shareable HTML report: the after take scored in full, with the
        # before take as the base for the per-scenario regression deltas.
        _report.write_report(
            args.out,
            fmt="html",
            base=cmp_env["before"]["envelope"],
            base_label=f"before: {before_name}",
            stereo=args.after,
            caller=args.after_caller,
            agent=args.after_agent,
            caller_channel=args.caller_channel,
            agent_channel=args.agent_channel,
            onset_sec=(args.after_onset if args.after_onset is not None
                       else args.onset),
            expect=args.expect,
            stack=args.stack,
            max_talk_over_sec=args.max_talk_over,
            max_time_to_yield_sec=args.max_time_to_yield,
        )
        print(f"wrote before/after HTML report to {args.out}",
              file=sys.stderr)
    if args.format == "json":
        print(json.dumps(cmp_env, indent=2))
    else:
        print(_compare.render_text(cmp_env, before_name, after_name))
    if cmp_env["result"] == "not_scorable":
        # No verdict is invented for an unjudgeable side: unusable input.
        return 2
    if args.fail_on_worse and cmp_env["result"] in ("regressed", "worse"):
        return 1
    return 0


def _cmd_scan(args) -> int:
    from . import scan as _scan

    result = _scan.scan_recording(
        args.stereo,
        caller_channel=args.caller_channel,
        agent_channel=args.agent_channel,
        min_gap_sec=args.min_gap,
    )
    if args.out:
        # The file gets EVERY candidate; --top caps only the stdout listing.
        with open(args.out, "w", encoding="utf-8") as fh:
            json.dump(result, fh, indent=2)
            fh.write("\n")
        print(
            f"wrote {result['total_candidates']} candidates to {args.out}",
            file=sys.stderr,
        )
    if args.format == "json":
        capped = dict(result)
        if args.top > 0:
            capped["candidates"] = result["candidates"][:args.top]
        capped["shown"] = len(capped["candidates"])
        print(json.dumps(capped, indent=2))
    else:
        print(_scan.render_text(result, top=args.top))
    return 0


def _cmd_analyze(args) -> int:
    from . import analyze as _analyze

    aggregate, per_file = _analyze.analyze_folder(
        args.folder,
        caller_channel=args.caller_channel,
        agent_channel=args.agent_channel,
        min_gap_sec=args.min_gap,
        pre_sec=args.pre,
        post_sec=args.post,
    )
    if args.format == "json":
        # stdout is the machine surface: the ranked candidates capped by --top,
        # with the full count kept in total_candidates so nothing is hidden.
        capped = dict(aggregate)
        if args.top > 0:
            capped["candidates"] = aggregate["candidates"][:args.top]
        capped["shown"] = len(capped["candidates"])
        text = json.dumps(capped, indent=2)
        if args.out:
            with open(args.out, "w", encoding="utf-8") as fh:
                fh.write(text)
                fh.write("\n")
            print(f"wrote ranked candidates JSON to {args.out}", file=sys.stderr)
        print(text)
        return 0
    # Default: the self-contained HTML dashboard with the hear-the-bug player.
    out = args.out or "hotato-analyze.html"
    html_str = _analyze.build_dashboard_html(
        aggregate, per_file, top=args.top, audio_top=args.audio_top,
    )
    with open(out, "w", encoding="utf-8") as fh:
        fh.write(html_str)
    size = os.path.getsize(out)
    print(
        f"wrote analyze dashboard ({aggregate['total_candidates']} candidate "
        f"moments across {aggregate['calls_scanned']} calls"
        + (f", {aggregate['calls_skipped']} skipped" if aggregate['calls_skipped'] else "")
        + f") to {out}  [{size / 1048576.0:.1f} MB]",
        file=sys.stderr,
    )
    if not args.no_open:
        _try_open(out)
    return 0


def _cmd_ingest(args) -> int:
    from . import ingest as _ingest

    return _ingest.run_ingest(
        args.stack,
        event=args.event,
        call_id=args.call_id,
        recording_sid=args.recording_sid,
        caller_channel=args.caller_channel,
        agent_channel=args.agent_channel,
        allow_mono=args.allow_mono,
        out=args.out,
        fmt=args.format,
        top=args.top,
        min_gap=args.min_gap,
    )


_DEMO_HEADER = "hotato demo: real recorded calls a provider's default agent fails"
_DEMO_NOTE = ("these are two real recorded calls on a provider's default "
              "settings; run it to see what Hotato catches.")


def _cmd_demo(args) -> int:
    # The packaged demo battery: two REAL recorded probe calls against a voice
    # agent on a provider's DEFAULT interruption settings (fd-01 misses a real
    # interruption, fd-02 false-stops on a backchannel). Both fail, on both
    # axes, so a first-time user hears exactly what Hotato catches: the [FAIL]
    # verdicts, both fix classes (config and engagement-control), the report
    # timelines, and the exact scored audio embedded under each one. Same
    # scorer, same envelope, same report as `run` and `doctor`; nothing new is
    # claimed. The clips are operator-recorded and MIT-licensed (see each
    # scenario's provenance block).
    from importlib import resources

    from . import report as _report

    demo_root = resources.files("hotato").joinpath("data", "demo", "failing")
    scenarios_dir = str(demo_root.joinpath("scenarios"))
    audio_dir = str(demo_root.joinpath("audio"))
    out = args.out or os.path.join(tempfile.gettempdir(), "hotato-demo-report.html")

    env = _report.write_report(
        out,
        fmt="html",
        suite=SUITE_ID,
        stack="generic",
        scenarios_dir=scenarios_dir,
        audio_dir=audio_dir,
        embed_audio=True,
    )

    if args.format == "json":
        # stdout stays the pure machine envelope; the report path goes to stderr.
        _emit(env, "json")
        print(f"report: {out}", file=sys.stderr)
    else:
        print(_DEMO_HEADER)
        _emit(env, "text")
        print(_DEMO_NOTE)
        print(f"report: {out}")

    if not args.no_open:
        _try_open(out)

    if args.fail:
        # The real regression code (1: this battery fails by design).
        return process_exit_code(env)
    # Default exit 0: the failures are intentional, so a demo run never breaks
    # a script or a CI job that merely wanted to see the output.
    return 0


# --- describe: the generated capability manifest (machine-drivability) -----

def _scalar_type_name(py_type) -> str:
    if py_type is float:
        return "float"
    if py_type is int:
        return "int"
    return "str"


def _arg_type_name(action: argparse.Action) -> str:
    if isinstance(action, (argparse._StoreTrueAction, argparse._StoreFalseAction)):
        return "bool"
    if action.nargs in ("+", "*"):
        return f"list[{_scalar_type_name(action.type)}]"
    return _scalar_type_name(action.type)


def _manifest_arg(action: argparse.Action) -> "dict | None":
    """One argparse action -> a manifest arg entry, or None to skip an action
    that is not a real user-facing argument (-h/--help, and the subparsers
    action itself, which is walked separately as ``subcommands``)."""
    if isinstance(action, (argparse._HelpAction, argparse._SubParsersAction)):
        return None
    positional = not action.option_strings
    name = action.dest if positional else "/".join(action.option_strings)
    required = (action.nargs not in ("?", "*")) if positional else bool(action.required)
    default = action.default if action.default is not argparse.SUPPRESS else None
    entry = {
        "name": name,
        "type": _arg_type_name(action),
        "required": required,
        "default": default,
        "help": action.help or "",
    }
    if action.choices:
        entry["choices"] = list(action.choices)
    return entry


def _describe_subcommand(name: str, parser: argparse.ArgumentParser, prefix: str) -> dict:
    """Walk one subparser (recursing into any nested subparsers, e.g.
    ``benchmark compare`` / ``fixture create``) into a manifest entry."""
    full_name = f"{prefix} {name}".strip()
    args = []
    subcommands = []
    for action in parser._actions:
        if isinstance(action, argparse._SubParsersAction):
            for sub_name, sub_parser in action.choices.items():
                subcommands.append(_describe_subcommand(sub_name, sub_parser, full_name))
            continue
        entry = _manifest_arg(action)
        if entry is not None:
            args.append(entry)
    out = {
        "name": full_name,
        "purpose": parser.description or parser.format_usage().strip(),
        "args": args,
    }
    if full_name in _EXIT_CODES:
        out["exit_codes"] = [
            {"code": code, "meaning": meaning}
            for code, meaning in _EXIT_CODES[full_name]
        ]
    if subcommands:
        out["subcommands"] = subcommands
    return out


def build_capability_manifest() -> dict:
    """Generate the CAPABILITY MANIFEST straight from ``build_parser()``'s own
    argparse structure: every subcommand's name, purpose, argument list, and
    documented exit codes, plus the tool version and the two schema URLs. This
    is the ``hotato describe`` payload -- one call for an agent to learn the
    whole CLI instead of scraping --help across every subcommand. Because it
    is generated from the live parser (not hand-maintained), it can never
    drift from the real flags; it is otherwise pure and deterministic."""
    from importlib import resources

    parser = build_parser()
    subs_action = None
    for action in parser._actions:
        if isinstance(action, argparse._SubParsersAction):
            subs_action = action
            break
    subcommands = [
        _describe_subcommand(name, sub_parser, "")
        for name, sub_parser in (subs_action.choices.items() if subs_action else ())
    ]

    def _schema_id(filename: str) -> str:
        return json.loads(
            resources.files("hotato").joinpath("schema", filename)
            .read_text(encoding="utf-8")
        )["$id"]

    return {
        "tool": _errors.TOOL,
        "schema_version": _errors.SCHEMA_VERSION,
        "version": __version__,
        "schemas": {
            "envelope": _schema_id("envelope.v1.json"),
            "error": _schema_id("error.v1.json"),
        },
        "subcommands": subcommands,
    }


def _render_describe_text(manifest: dict) -> str:
    lines = [f"hotato {manifest['version']} -- capability manifest"]
    lines.append(f"schemas: envelope={manifest['schemas']['envelope']} "
                 f"error={manifest['schemas']['error']}")
    lines.append("")

    def _walk(cmds, indent=""):
        for c in cmds:
            lines.append(f"{indent}hotato {c['name']}")
            if c.get("purpose"):
                lines.append(f"{indent}  {c['purpose']}")
            for a in c["args"]:
                tag = "required" if a["required"] else f"default={a['default']!r}"
                lines.append(f"{indent}    {a['name']} ({a['type']}, {tag}): {a['help']}")
            if c.get("exit_codes"):
                codes = ", ".join(f"{e['code']}={e['meaning']}" for e in c["exit_codes"])
                lines.append(f"{indent}    exit codes: {codes}")
            if c.get("subcommands"):
                _walk(c["subcommands"], indent + "  ")

    _walk(manifest["subcommands"])
    return "\n".join(lines) + "\n"


def _cmd_describe(args) -> int:
    manifest = build_capability_manifest()
    if args.format == "json":
        print(json.dumps(manifest, indent=2))
    else:
        print(_render_describe_text(manifest), end="")
    return 0


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
        description=(
            "Score one dual-channel recording's turn-taking, or run the "
            "bundled synthetic self-test battery. Offline; no audio leaves "
            "the machine. There is no accuracy percentage anywhere: results "
            "are reproducible timing measurements with every threshold "
            "exposed and every frame inspectable (see --dump-frames)."
        ),
        epilog=(
            _exit_codes_epilog("run") + "\n\n"
            "Offline: runs locally; no audio leaves the machine. There is no "
            "accuracy percentage anywhere -- results are reproducible timing "
            "measurements with every threshold exposed and every frame inspectable "
            "(see --dump-frames).\n\n" + _LABEL_NOTE
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
    r.add_argument("--format", default="text", choices=["json", "text"],
                   help="output format (default text; use json for the machine envelope)")
    r.add_argument("--dump-frames", default=None, metavar="PATH",
                   help="write the per-frame VAD evidence (t_sec, per-channel dBFS, "
                        "active flags, threshold and noise floor for both channels) "
                        "to PATH as JSON, so every reported number is re-derivable "
                        "by hand; requires a single recording (--stereo or --caller/--agent)")
    r.add_argument("--echo-gate", action="store_true",
                   help="hold a yield out of the verdict (mark it not-scorable) when it "
                        "coincides with high cross-channel echo coherence, i.e. the agent "
                        "most likely heard its own audio bleed rather than a real caller; "
                        "off by default, and the additive signals.echo block is always "
                        "reported either way")
    r.add_argument("--no-fail", action="store_true", help="always exit 0 (do not fail CI on a regression)")
    r.set_defaults(func=_cmd_run)

    # --- capture: score YOUR OWN call from a specific stack ----------------
    c = sub.add_parser(
        "capture",
        help="score a real call from your stack (the out-of-box aha)",
        description=(
            "Capture a real dual-channel call from your voice stack and score its "
            "turn-taking. Vapi, Retell, and Twilio pull the recording for you (API "
            "key only, no SDK); LiveKit/Pipecat capture in your own infra (see `hotato setup`), "
            "then pass the file here. Everything is scored OFFLINE; the only network "
            "is the direct recording download. There is no accuracy percentage -- "
            "reproducible timing measurements only."
        ),
        epilog=(
            _exit_codes_epilog("capture") + "\n\n"
            "Examples:\n"
            "  hotato capture --stack vapi --call-id <id>            # + VAPI_API_KEY\n"
            "  hotato capture --stack retell --call-id <id>          # + RETELL_API_KEY\n"
            "  hotato capture --stack twilio --recording-sid RE...   # + TWILIO_ACCOUNT_SID/TOKEN\n"
            "  hotato capture --stack livekit --caller a.wav --agent b.wav\n"
            "  hotato capture --stack pipecat --stereo captured.wav\n"
            "  hotato capture --stack vapi --demo                    # offline, zero deps"
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    c.add_argument("--stack", required=True, choices=list(_capture.CAPTURE_STACKS),
                   help="voice stack the call came from (the mono stacks bland/"
                        "elevenlabs/synthflow/millis/cartesia need --allow-mono)")
    c.add_argument("--demo", action="store_true",
                   help="prove the capture -> score loop on a bundled two-channel reference (offline, zero deps, no API)")
    # already-captured input (works for every stack, incl. livekit/pipecat/retell)
    c.add_argument("--stereo", "--wav", dest="stereo",
                   help="score an existing two-channel WAV (caller on ch0, agent on ch1)")
    c.add_argument("--caller", help="mono WAV of the caller channel (with --agent)")
    c.add_argument("--agent", help="mono WAV of the agent channel (with --caller)")
    # vapi
    c.add_argument("--call-id", help="[vapi] the id of an ended, recorded call")
    c.add_argument("--api-key", help="[vapi|retell] private API key (else env VAPI_API_KEY / RETELL_API_KEY)")
    # twilio
    c.add_argument("--recording-sid", help="[twilio] the Recording SID (RE...) of a dual-channel recording")
    c.add_argument("--allow-mono", action="store_true",
                   help="accept a mono-only recording in degraded mode; separated talk-over cannot be attributed on mono")
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
        epilog=_exit_codes_epilog("setup"),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    s.add_argument("--stack", required=True, choices=list(_capture.STACKS),
                   help="voice stack to scaffold")
    s.set_defaults(func=_cmd_setup)

    # --- connect: one-time credential capture for pull/sweep --------------
    cn = sub.add_parser(
        "connect",
        help="store a stack's credentials once (0600, local only) so pull/sweep "
             "need no keys",
        description=(
            "Capture a voice stack's API credentials ONCE, run a lightweight live "
            "auth check (list one recent call, unless --no-verify), and store them "
            "in ~/.hotato/connections.json with file mode 0600. The credentials "
            "stay on this machine and are sent only to the vendor's own API, never "
            "to Hotato. After connecting, `hotato pull` / `hotato sweep` need no "
            "--api-key, and --stack is optional when exactly one stack is "
            "connected. Connectable stacks are the vendor-hosted-recording ones "
            "(vapi, retell, twilio, bland, elevenlabs, synthflow, millis, "
            "cartesia); LiveKit/Pipecat are capture-in-your-infra (use `hotato "
            "setup`)."
        ),
        epilog=(
            _exit_codes_epilog("connect") + "\n\n"
            "Examples:\n"
            "  hotato connect vapi --api-key <key>\n"
            "  VAPI_API_KEY=<key> hotato connect vapi        # reads the env var\n"
            "  hotato connect twilio --account-sid AC... --auth-token ...\n"
            "  hotato connect synthflow --api-key <key> --model-id <id>"
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    cn.add_argument("stack", choices=list(_capture.CONNECT_STACKS),
                    help="voice stack to connect")
    _add_cred_args(cn)
    cn.add_argument("--no-verify", action="store_true",
                    help="skip the live auth check; just store the credentials")
    cn.add_argument("--format", default="text", choices=["json", "text"],
                    help="output format (default text)")
    cn.set_defaults(func=_cmd_connect)

    # --- pull: bulk-fetch recent recordings into a local directory --------
    pu = sub.add_parser(
        "pull",
        help="bulk-fetch recent recordings from a connected stack into a local "
             "folder",
        description=(
            "List a stack's recent recordings via its verified list endpoint and "
            "download each one by looping the same single-call fetch `hotato "
            "capture` uses, into a local directory. Dual-channel stacks (vapi, "
            "twilio, retell) fetch a separated 2-channel file; mono/mixed stacks "
            "(bland, elevenlabs, synthflow, millis, cartesia) require --allow-mono "
            "and are indicative only. A recording that cannot be fetched is "
            "reported as a clean skip with its reason and the pull continues -- "
            "one bad call never crashes the run. Retell has no verified list "
            "endpoint, so pull it from explicit --call-id values. Everything scores "
            "OFFLINE later; the only network here is the direct recording download."
        ),
        epilog=(
            _exit_codes_epilog("pull") + "\n\n"
            "Examples:\n"
            "  hotato pull --stack vapi --since 7d --limit 50\n"
            "  hotato pull                                   # only-connected stack\n"
            "  hotato pull --stack retell --call-id c1 --call-id c2\n"
            "  hotato pull --stack bland --allow-mono --limit 20"
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    pu.add_argument("--stack", default=None, choices=list(_capture.PULL_STACKS),
                    help="stack to pull from (optional if exactly one is connected)")
    pu.add_argument("--since", default=None, metavar="WINDOW",
                    help="only recordings newer than this window, e.g. 7d, 12h, "
                         "30m, 2w (applied server-side where the vendor confirms "
                         "a date filter, else client-side)")
    pu.add_argument("--limit", type=int, default=50,
                    help="max recordings to fetch (default 50)")
    pu.add_argument("--call-id", action="append", metavar="ID",
                    help="fetch an explicit recording id (repeatable); required "
                         "for stacks without a list endpoint (retell). For twilio "
                         "pass Recording SIDs (RE...)")
    pu.add_argument("--allow-mono", action="store_true",
                    help="allow pulling mono/mixed stacks (degraded; separated "
                         "talk-over cannot be attributed on mono)")
    _add_cred_args(pu)
    pu.add_argument("--out", default=None, metavar="DIR",
                    help="download directory (default hotato-pull-<stack>)")
    pu.add_argument("--format", default="text", choices=["json", "text"],
                    help="output format (default text)")
    pu.set_defaults(func=_cmd_pull)

    # --- sweep: pull recent recordings then analyze them in one flow ------
    sw = sub.add_parser(
        "sweep",
        help="connect once, then pull + analyze every recent real call in one "
             "command",
        description=(
            "The flagship 'connect once, see every turn-taking problem across all "
            "your real calls' flow: pull a stack's recent recordings (see `hotato "
            "pull`), then run the exact same zero-config analyze as `hotato "
            "analyze` over the pulled folder. Writes ONE self-contained, offline "
            "HTML dashboard of the ranked candidate turn-taking moments across "
            "every call, with the hear-the-bug audio player on the top moments. "
            "Dual-channel stacks give separated scoring; mono/mixed stacks require "
            "--allow-mono and cannot be attributed per party (they surface as "
            "skipped in the dashboard). Candidates are MEASURED timing moments you "
            "review and label, never verdicts and never intent. Offline; no "
            "accuracy percentage anywhere."
        ),
        epilog=(
            _exit_codes_epilog("sweep") + "\n\n"
            "Examples:\n"
            "  hotato sweep --stack vapi --since 7d           # pull + dashboard\n"
            "  hotato sweep                                   # only-connected stack\n"
            "  hotato sweep --stack twilio --limit 100 --out calls.html\n"
            "  hotato sweep --stack retell --call-id c1 --call-id c2\n\n"
            + _LABEL_NOTE
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    sw.add_argument("--stack", default=None, choices=list(_capture.PULL_STACKS),
                    help="stack to sweep (optional if exactly one is connected)")
    sw.add_argument("--since", default=None, metavar="WINDOW",
                    help="only recordings newer than this window, e.g. 7d, 12h, 2w")
    sw.add_argument("--limit", type=int, default=50,
                    help="max recordings to pull before analyzing (default 50)")
    sw.add_argument("--call-id", action="append", metavar="ID",
                    help="sweep explicit recording ids (repeatable); required for "
                         "stacks without a list endpoint (retell)")
    sw.add_argument("--allow-mono", action="store_true",
                    help="allow sweeping mono/mixed stacks (degraded; they cannot "
                         "be scored per party and surface as skipped)")
    _add_cred_args(sw)
    sw.add_argument("--dir", default=None, metavar="DIR",
                    help="download directory for the pulled recordings "
                         "(default hotato-sweep-<stack>)")
    sw.add_argument("--caller-channel", type=int, default=0)
    sw.add_argument("--agent-channel", type=int, default=1)
    sw.add_argument("--top", type=int, default=25,
                    help="cap the ranked moments shown (0 shows all; default 25)")
    sw.add_argument("--audio-top", type=int, default=8,
                    help="embed the hear-the-bug player for the top N moments "
                         "(default 8)")
    sw.add_argument("--pre", type=float, default=2.0,
                    help="seconds kept BEFORE each moment (default 2.0)")
    sw.add_argument("--post", type=float, default=4.0,
                    help="seconds kept AFTER each moment (default 4.0)")
    sw.add_argument("--min-gap", type=float, default=2.0,
                    help="minimum response gap in seconds to surface (default 2.0)")
    sw.add_argument("--format", default="html", choices=["html", "json"],
                    help="output: 'html' dashboard (default) or 'json' ranked "
                         "candidates + a pull summary")
    sw.add_argument("--out", default=None, metavar="PATH",
                    help="where to write the dashboard (default "
                         "hotato-sweep-<stack>.html)")
    sw.add_argument("--no-open", action="store_true",
                    help="do not launch a browser for the HTML dashboard")
    sw.set_defaults(func=_cmd_sweep)

    # --- report: one self-contained, offline HTML page with per-event timelines
    rp = sub.add_parser(
        "report",
        help="render a shareable, self-contained HTML report with per-event timelines",
        description=(
            "Render ONE self-contained HTML file (inline CSS + inline SVG, zero "
            "external requests, opens offline by double-click). For every event it "
            "draws a to-scale caller/agent activity timeline from the real frame "
            "data: the overlap shaded, the caller-onset and yield markers, the "
            "measured talk-over seconds, expected-vs-actual, a PASS/FAIL chip, and "
            "the exact ScoreConfig thresholds used. Every number is a real "
            "measurement; there is no accuracy percentage anywhere."
        ),
        epilog=(
            _exit_codes_epilog("report") + "\n\n"
            "Examples:\n"
            "  hotato report --stereo call.wav --out report.html\n"
            "  hotato report --stereo call.wav --embed-audio --out report.html\n"
            "  hotato report --caller a.wav --agent b.wav --expect yield --out r.html\n"
            "  hotato report --suite barge-in --out selftest.html"
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    rp.add_argument("--stereo", help="two-channel WAV (caller on one channel, agent on the other)")
    rp.add_argument("--caller", help="mono WAV of the caller channel")
    rp.add_argument("--agent", help="mono WAV of the agent channel")
    rp.add_argument("--onset", type=float, default=None, help="caller onset in seconds (else auto-detected)")
    rp.add_argument("--expect", default="yield", choices=["yield", "hold"],
                    help="expected behaviour: 'yield' (stop for the caller) or 'hold' (keep the floor)")
    rp.add_argument("--max-talk-over", type=float, default=None, help="fail if talk-over exceeds this many seconds")
    rp.add_argument("--max-time-to-yield", type=float, default=None, help="fail if the yield is slower than this many seconds")
    rp.add_argument("--suite", nargs="?", const=SUITE_ID, default=None,
                    help=f"render a labelled battery instead of a single file (default suite: {SUITE_ID!r})")
    rp.add_argument("--scenarios", default=None, help="dir of scenario JSON labels (defaults to the bundled battery)")
    rp.add_argument("--audio", default=None, help="dir of scenario audio (defaults to the bundled fixtures)")
    rp.add_argument("--stack", default="generic",
                    choices=["generic", "vapi", "twilio", "livekit", "pipecat", "retell"],
                    help="voice stack the recording came from (labels the fix knob only)")
    rp.add_argument("--caller-channel", type=int, default=0)
    rp.add_argument("--agent-channel", type=int, default=1)
    rp.add_argument("--embed-audio", action="store_true",
                    help="embed the exact scored audio under each timeline as an "
                         "inline base64 WAV with a native player. The report stays "
                         "ONE self-contained offline file (zero external requests); "
                         "it just grows by roughly the audio size, printed when "
                         "done. Any file over 8 MB is noted and skipped. HTML "
                         "format only.")
    rp.add_argument("--format", default="html", choices=["html", "md"],
                    help="report format: 'html' (self-contained page, default) or "
                         "'md' (same content as Markdown tables). For PDF, print "
                         "the HTML from any browser; the page ships print CSS.")
    rp.add_argument("--base", default=None, metavar="BASE.json",
                    help="a previous envelope JSON (hotato run --format json > "
                         "base.json) to compare against: renders per-scenario "
                         "talk-over and time-to-yield deltas with clear "
                         "worse/better marks")
    rp.add_argument("--out", default=None, metavar="PATH",
                    help="where to write the report (default hotato-report.html, "
                         "or hotato-report.md with --format md)")
    rp.add_argument("--no-fail", action="store_true", help="always exit 0 (do not fail CI on a regression)")
    rp.set_defaults(func=_cmd_report)

    # --- team: aggregate a directory of run envelopes -----------------------
    t = sub.add_parser(
        "team",
        help="aggregate a directory of run envelopes into a trend (pass rate, "
             "talk-over, time to yield)",
        description=(
            "Aggregate many runs into one honest trend view. Point it at a "
            "directory of envelope JSONs (hotato run --format json > runs/001.json). "
            "It reports runs, mean/median/p90 talk-over and time-to-yield pooled "
            "across all events, mean/median/p90/p95 response gap (dead air before "
            "the agent speaks), pass rate per run over time, the most common "
            "failure class, and a pass-rate trend line in the HTML page. Every "
            "number is a real measurement pooled from the envelopes; fewer than 2 "
            "runs is stated plainly (exit 0), never padded into a trend. "
            "--max-response-gap gates the pooled p95 response gap: a latency SLA "
            "that fails (exit 1) exactly when p95 exceeds the bound."
        ),
        epilog=(
            _exit_codes_epilog("team") + "\n\n"
            "Examples:\n"
            "  hotato run --suite barge-in --format json > runs/001.json\n"
            "  hotato team runs/ --html team.html\n"
            "  hotato team runs/ --order name --format json\n"
            "  hotato team runs/ --max-response-gap 0.8"
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    t.add_argument("dir", help="directory of hotato envelope JSONs")
    t.add_argument("--order", default="mtime", choices=["mtime", "name"],
                   help="run order for the trend: file mtime (default) or "
                        "filename (use a numeric prefix as an explicit index)")
    t.add_argument("--out", default=None, metavar="PATH",
                   help="write the aggregate envelope JSON here")
    t.add_argument("--html", default=None, metavar="PATH",
                   help="write a self-contained HTML team page here")
    t.add_argument("--format", default="text", choices=["json", "text"],
                   help="stdout format (default text)")
    t.add_argument("--max-response-gap", type=float, default=None,
                   help="latency SLA: fail if the pooled p95 response gap "
                        "(dead air before the agent speaks) exceeds this many "
                        "seconds")
    t.add_argument("--no-fail", action="store_true",
                   help="always exit 0 (do not fail CI on a latency SLA breach)")
    t.set_defaults(func=_cmd_team)

    # --- export: research-grade CSVs + the envelope --------------------------
    x = sub.add_parser(
        "export",
        help="write research CSVs (events.csv, frames.csv) plus envelope.json",
        description=(
            "Score a recording (or the bundled battery) exactly like `hotato run` "
            "and write three files into a directory: events.csv (one row per "
            "event, every measured signal + verdict), frames.csv (one row per "
            "VAD frame, the evidence behind every number), and envelope.json "
            "(the standard machine envelope). Column meanings are documented in "
            "comment lines at the top of each CSV. Stdlib only, offline. Also "
            "prints mean/median/p90/p95 response gap (dead air before the agent "
            "speaks) pooled across the exported events; --max-response-gap gates "
            "the pooled p95 as a latency SLA (exit 1 when it is exceeded)."
        ),
        epilog=(
            _exit_codes_epilog("export") + "\n\n"
            "Examples:\n"
            "  hotato export --stereo call.wav --out research/\n"
            "  hotato export --suite barge-in --out research/\n"
            "  hotato export --suite barge-in --out research/ --max-response-gap 0.8"
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    x.add_argument("--stereo", help="two-channel WAV (caller on one channel, agent on the other)")
    x.add_argument("--caller", help="mono WAV of the caller channel")
    x.add_argument("--agent", help="mono WAV of the agent channel")
    x.add_argument("--onset", type=float, default=None, help="caller onset in seconds (else auto-detected)")
    x.add_argument("--expect", default="yield", choices=["yield", "hold"],
                   help="expected behaviour: 'yield' (stop for the caller) or 'hold' (keep the floor)")
    x.add_argument("--max-talk-over", type=float, default=None, help="fail if talk-over exceeds this many seconds")
    x.add_argument("--max-time-to-yield", type=float, default=None, help="fail if the yield is slower than this many seconds")
    x.add_argument("--max-response-gap", type=float, default=None,
                   help="latency SLA: fail if the pooled p95 response gap "
                        "(dead air before the agent speaks, across the exported "
                        "events) exceeds this many seconds")
    x.add_argument("--suite", nargs="?", const=SUITE_ID, default=None,
                   help=f"export a labelled battery instead of a single file (default suite: {SUITE_ID!r})")
    x.add_argument("--scenarios", default=None, help="dir of scenario JSON labels (defaults to the bundled battery)")
    x.add_argument("--audio", default=None, help="dir of scenario audio (defaults to the bundled fixtures)")
    x.add_argument("--stack", default="generic",
                   choices=["generic", "vapi", "twilio", "livekit", "pipecat", "retell"],
                   help="voice stack the recording came from (labels the fix knob only)")
    x.add_argument("--caller-channel", type=int, default=0)
    x.add_argument("--agent-channel", type=int, default=1)
    x.add_argument("--out", required=True, metavar="DIR",
                   help="output directory (created if missing): events.csv, "
                        "frames.csv, envelope.json")
    x.add_argument("--no-fail", action="store_true", help="always exit 0 (do not fail CI on a regression)")
    x.set_defaults(func=_cmd_export)

    # --- benchmark: identical scenarios, YOUR stack, comparable results ----
    b = sub.add_parser(
        "benchmark",
        help="score YOUR stack's captured recordings on a fixed scenario set; "
             "compare result files with: hotato benchmark compare",
        description=(
            "Run one fixed scenario set through YOUR configured voice stack and "
            "score the recordings you captured, so result files are comparable "
            "across stacks and configs. You bring the captures (see `hotato "
            "setup` and `hotato capture`); hotato measures timing on the "
            "recordings it is given, offline. It ships no vendor numbers, no "
            "leaderboard, and no accuracy percentage. Scenarios without a "
            "matching recording are listed as not captured, never scored as "
            "failures. Walkthrough: docs/BENCHMARK-STACKS.md."
        ),
        epilog=(
            _exit_codes_epilog("benchmark") + "\n\n"
            "Examples:\n"
            "  hotato benchmark --stack livekit --recordings captures/livekit --out livekit.json\n"
            "  hotato benchmark --stack vapi --recordings captures/vapi --out vapi.json\n"
            "  hotato benchmark compare livekit.json vapi.json\n"
            "  hotato benchmark compare livekit.json vapi.json --format json --out cmp.json"
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    bsub = b.add_subparsers(dest="bench_command", required=False,
                            metavar="compare")
    bc = bsub.add_parser(
        "compare",
        help="side-by-side table of two or more benchmark result files",
        description=(
            "Compare two or more benchmark result JSONs scenario by scenario: "
            "yielded, talk-over, and time to yield per input, with signed "
            "deltas against the first file, plus summary medians. Only the "
            "intersection of scenarios captured in every input is compared; "
            "the rest is listed as skipped. Measurements only: no ranking, "
            "no winner."
        ),
        epilog=_exit_codes_epilog("benchmark compare"),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    bc.add_argument("results", nargs="+", metavar="RESULT.json",
                    help="benchmark result files written by "
                         "`hotato benchmark --out` (two or more)")
    bc.add_argument("--format", default="md", choices=["md", "json"],
                    help="comparison format (default md)")
    bc.add_argument("--out", default=None, metavar="FILE",
                    help="write the comparison here (default: stdout)")
    bc.set_defaults(func=_cmd_benchmark_compare)
    b.add_argument("--stack", default=None,
                   choices=["vapi", "twilio", "livekit", "pipecat", "generic"],
                   help="the voice stack the recordings came from (labels the "
                        "result and the fix knobs; never changes a measurement)")
    b.add_argument("--recordings", default=None, metavar="DIR",
                   help="directory of YOUR captured dual-channel recordings, "
                        "one per scenario, named <scenario-id>.wav")
    b.add_argument("--scenarios", default=None, metavar="DIR",
                   help="dir of scenario JSON labels (default: the bundled "
                        "battery; corpus/suites/*/scenarios also work)")
    b.add_argument("--suffix", default=None,
                   help="recording filename suffix (default: auto-detect among "
                        ".wav, .stereo.wav, .example.wav)")
    b.add_argument("--caller-channel", type=int, default=0)
    b.add_argument("--agent-channel", type=int, default=1)
    b.add_argument("--out", default=None, metavar="PATH",
                   help="write the benchmark result JSON here (default: stdout)")
    b.add_argument("--fail-on-regression", action="store_true",
                   help="exit 1 when any SCORED event fails its scenario "
                        "thresholds (default: exit 0; the benchmark measures, "
                        "it does not gate)")
    b.set_defaults(func=_cmd_benchmark)

    # --- doctor: the 5-minute path in one command --------------------------
    d = sub.add_parser(
        "doctor",
        help="one command: score (or self-test), render the HTML report, open it",
        description=(
            "The 5-minute path in one command. If you pass a recording (--stereo, "
            "or --caller and --agent) it scores that; otherwise it runs the bundled "
            "self-test battery. Either way it renders the self-contained HTML report "
            "and tries to open it in your browser (best-effort; on a headless box it "
            "just prints the path). A convenience wrapper over the existing scorer "
            "and report -- nothing new is claimed. Everything runs offline."
        ),
        epilog=(
            _exit_codes_epilog("doctor") + "\n\n" + _LABEL_NOTE + "\n\n"
            "Examples:\n"
            "  hotato doctor --stereo call.wav        # score your call, open the report\n"
            "  hotato doctor --demo                   # self-test, open the report\n"
            "  hotato doctor                          # same self-test fallback\n"
            "  hotato doctor --no-open --format json  # the machine envelope"
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    d.add_argument("--stereo", help="two-channel WAV (caller on one channel, agent on the other)")
    d.add_argument("--caller", help="mono WAV of the caller channel")
    d.add_argument("--agent", help="mono WAV of the agent channel")
    d.add_argument("--demo", action="store_true",
                   help="run the bundled self-test battery (the default when no recording is given)")
    d.add_argument("--onset", type=float, default=None, help="caller onset in seconds (else auto-detected)")
    d.add_argument("--expect", default="yield", choices=["yield", "hold"],
                   help="expected behaviour for a recording: 'yield' or 'hold'")
    d.add_argument("--stack", default="generic",
                   choices=["generic", "vapi", "twilio", "livekit", "pipecat", "retell"],
                   help="voice stack the recording came from (labels the fix knob only)")
    d.add_argument("--caller-channel", type=int, default=0)
    d.add_argument("--agent-channel", type=int, default=1)
    d.add_argument("--out", default=None, metavar="PATH",
                   help="where to write the report (default: a temp file)")
    d.add_argument("--format", default="text", choices=["json", "text"],
                   help="stdout format (default text summary; json prints "
                        "only the machine envelope to stdout, with the report "
                        "path on stderr)")
    d.add_argument("--no-open", action="store_true", help="do not launch a browser; just write and print the path")
    d.add_argument("--no-fail", action="store_true", help="always exit 0 (do not fail on a regression)")
    d.set_defaults(func=_cmd_doctor)

    # --- demo: the packaged real-call failing battery -----------------------
    dm = sub.add_parser(
        "demo",
        help="run the packaged battery of two real failing calls and open its report",
        description=(
            "Run the packaged two-scenario battery of REAL recorded calls "
            "against a voice agent on a provider's DEFAULT interruption "
            "settings: one where the agent talks straight over a real "
            "interruption, one where it false-stops on a backchannel. Both "
            "fail, so you hear what Hotato catches in under a minute: the "
            "[FAIL] verdicts, the fix classes (config and engagement-control), "
            "the per-event report timelines, and the exact scored audio "
            "embedded under each one. Renders the self-contained HTML report "
            "and opens it best-effort. Exits 0 by default so a demo never "
            "breaks a script; pass --fail to get the real regression exit "
            "code. Offline, zero extra files."
        ),
        epilog=(
            _exit_codes_epilog("demo") + "\n\n" + _LABEL_NOTE + "\n\n"
            "Examples:\n"
            "  hotato demo                          # run, print, open the report\n"
            "  hotato demo --no-open --out demo.html\n"
            "  hotato demo --format json            # the machine envelope\n"
            "  hotato demo --fail                   # exit 1 (real regression code)"
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    dm.add_argument("--out", default=None, metavar="PATH",
                    help="where to write the HTML report (default: a temp file)")
    dm.add_argument("--no-open", action="store_true",
                    help="do not launch a browser; just write and print the path")
    dm.add_argument("--format", default="text", choices=["text", "json"],
                    help="output format (default text)")
    dm.add_argument("--fail", action="store_true",
                    help="exit with the real regression code (1: this battery "
                         "fails by design) instead of the default 0")
    dm.set_defaults(func=_cmd_demo)

    # --- diagnose: Level 0 of the guarded fix ladder (read-only) ------------
    dg = sub.add_parser(
        "diagnose",
        help="explain a finished run: per-failure diagnosis + a battery-level "
             "decision (read-only)",
        description=(
            "Read a hotato envelope JSON (hotato run --format json > result.json) "
            "and emit one diagnosis per failing event (finding, measured evidence, "
            "likely layer, config_only_safe, plain-language notes) plus a "
            "battery-level decision. Honesty rules are built in: a battery that "
            "misses a real interruption AND false-stops on a backchannel gets "
            "do_not_tune_single_threshold; a slow yield without a passing "
            "opposite-risk fixture stays unknown_root_cause (TTS buffering, "
            "transport, and VAD are indistinguishable from one recording); "
            "not-scorable events are input problems, never agent failures. "
            "Read-only: nothing is fetched and nothing is changed."
        ),
        epilog=(
            _exit_codes_epilog("diagnose") + "\n"
            "Examples:\n"
            "  hotato run --suite barge-in --format json > result.json\n"
            "  hotato diagnose result.json\n"
            "  hotato diagnose result.json --format json"
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    dg.add_argument("envelope", metavar="RESULT.json",
                    help="a hotato envelope JSON from run/capture")
    dg.add_argument("--format", default="text", choices=["json", "text"],
                    help="output format (default text: the Level 0 advisory)")
    dg.set_defaults(func=_cmd_diagnose)

    # --- inspect: Level 1, read the CURRENT turn-taking config --------------
    ins = sub.add_parser(
        "inspect",
        help="read the current turn-taking config from a stack and normalize "
             "it (read-only)",
        description=(
            "Fetch (Vapi, Retell) or statically parse (LiveKit, Pipecat) the "
            "turn-taking configuration a target is actually running and "
            "normalize it into one model: interrupt_min_words, "
            "interrupt_voice_seconds, resume_backoff_seconds, "
            "endpointing_wait_seconds, backchannel_aware, plus the raw fields "
            "and provenance. Unknown or absent options are null with a note; "
            "values are never guessed. Suspicious values are surfaced as "
            "observations, not judgments. Read-only by construction: the only "
            "network calls are GETs, config files are parsed without being "
            "imported or executed, and nothing is ever written back."
        ),
        epilog=(
            _exit_codes_epilog("inspect") + "\n"
            "Examples:\n"
            "  hotato inspect --stack vapi --assistant-id <id>     # + VAPI_API_KEY\n"
            "  hotato inspect --stack retell --agent-id <id>       # + RETELL_API_KEY\n"
            "  hotato inspect --stack livekit --config agent.py\n"
            "  hotato inspect --stack pipecat --config bot.py --format json"
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    ins.add_argument("--stack", required=True,
                     choices=["vapi", "retell", "livekit", "pipecat"],
                     help="which stack to inspect")
    ins.add_argument("--assistant-id", help="[vapi] assistant id to fetch")
    ins.add_argument("--agent-id", help="[retell] agent id to fetch")
    ins.add_argument("--config", metavar="FILE.py",
                     help="[livekit|pipecat] python config file to parse "
                          "statically (never imported or executed)")
    ins.add_argument("--api-key",
                     help="[vapi|retell] API key (else env VAPI_API_KEY / "
                          "RETELL_API_KEY); used for one read-only GET")
    ins.add_argument("--format", default="text", choices=["json", "text"],
                     help="output format (default text)")
    ins.set_defaults(func=_cmd_inspect)

    # --- plan: Level 2, a guarded fix plan (proposal only, no apply) --------
    pl = sub.add_parser(
        "plan",
        help="combine a diagnosis with the inspected config into a guarded "
             "fix-plan JSON (proposal only; no apply command exists)",
        description=(
            "Diagnose a finished run, optionally inspect the live config, and "
            "write a fix plan (schema hotato.fixplan.v1). A change is proposed "
            "only when the failure maps cleanly to one setting, the step is one "
            "bounded move in an unambiguous direction within documented bounds, "
            "the battery contains a passing opposite-risk fixture, and the "
            "diagnosis is config-only-safe; otherwise the plan downgrades "
            "honestly (refusal on the threshold funnel, instrumentation "
            "checklist on an ambiguous slow yield, insufficient_coverage when "
            "the verifying fixture is missing). Plans never carry an absolute "
            "magic value: from -> to is one step relative to the inspected "
            "current value, or direction + bounds only when it is unknown. "
            "production_apply is always false; applying anything is a later "
            "phase and is not shipped."
        ),
        epilog=(
            _exit_codes_epilog("plan") + "\n"
            "Examples:\n"
            "  hotato plan result.json\n"
            "  hotato plan result.json --stack vapi --assistant-id <id>\n"
            "  hotato plan result.json --stack livekit --config agent.py\n"
            "  hotato plan result.json --out my-plan.json --format json"
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    pl.add_argument("result_json", nargs="?", default=None,
                    metavar="RESULT.json",
                    help="a hotato envelope JSON from run/capture "
                         "(equivalent to --run)")
    pl.add_argument("--run", default=None, metavar="RESULT.json",
                    help="a hotato envelope JSON from run/capture")
    pl.add_argument("--stack", default=None,
                    choices=["generic", "vapi", "retell", "livekit",
                             "pipecat", "twilio"],
                    help="target stack (default: the stack recorded in the "
                         "envelope, else generic: plan from the diagnosis "
                         "alone, using the generic knob families; twilio: the "
                         "transport has no turn-taking knobs, so the plan "
                         "points at channel assignment and the upstream "
                         "voice-agent stack)")
    pl.add_argument("--assistant-id", help="[vapi] assistant id to inspect")
    pl.add_argument("--agent-id", help="[retell] agent id to inspect")
    pl.add_argument("--config", metavar="FILE.py",
                    help="[livekit|pipecat] python config file to parse "
                         "statically for current values")
    pl.add_argument("--api-key",
                    help="[vapi|retell] API key (else env VAPI_API_KEY / "
                         "RETELL_API_KEY); used for one read-only GET")
    pl.add_argument("--out", default="hotato-fixplan.json", metavar="PATH",
                    help="where to write the plan JSON (default "
                         "hotato-fixplan.json)")
    pl.add_argument("--format", default="text", choices=["json", "text"],
                    help="stdout format (default text summary; json prints "
                         "the full plan)")
    pl.set_defaults(func=_cmd_plan)

    # --- fixture create: bad call moment -> permanent regression fixture ----
    fx = sub.add_parser(
        "fixture",
        help="turn a bad call moment into a permanent regression fixture "
             "(hotato fixture create)",
        description=(
            "Fixture tooling for the regression loop (see "
            "docs/BAD-CALL-TO-CI.md)."
        ),
        epilog=_exit_codes_epilog("fixture"),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    fxsub = fx.add_subparsers(dest="fixture_command", required=True,
                              metavar="create")
    fc = fxsub.add_parser(
        "create",
        help="write scenarios/<id>.json + audio/<id>.example.wav from one "
             "call moment, validated by scoring it immediately",
        description=(
            "Turn ONE moment of a recording you already have into a fixture "
            "that `hotato run --scenarios DIR --audio DIR` scores forever. "
            "By default the audio is clipped around the event (--pre seconds "
            "before the onset, --post after) and the fixture onset is "
            "re-based to the clip; --no-clip keeps the full recording. The "
            "audio is always written as ONE two-channel WAV (caller on "
            "channel 0, agent on channel 1). The created fixture is scored "
            "immediately; an input that cannot be judged is refused with the "
            "honest reason (exit 2), never written as a fixture that would "
            "report a meaningless verdict. Offline; no accuracy percentage "
            "anywhere."
        ),
        epilog=(
            _exit_codes_epilog("fixture create") + "\n\n" + _LABEL_NOTE + "\n\n"
            "Examples:\n"
            "  hotato fixture create --stereo bad-call.wav --id refund-cutoff-001 \\\n"
            "      --onset 42.18 --expect yield --max-talk-over 0.6 --out tests/hotato\n"
            "  hotato fixture create --caller c.wav --agent a.wav --id ack-hold-002 \\\n"
            "      --onset 12.4 --expect hold --out tests/hotato\n"
            "  hotato run --scenarios tests/hotato/scenarios --audio tests/hotato/audio"
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    fc.add_argument("--stereo", help="two-channel WAV (caller on one channel, agent on the other)")
    fc.add_argument("--caller", help="mono WAV of the caller channel (with --agent)")
    fc.add_argument("--agent", help="mono WAV of the agent channel (with --caller)")
    fc.add_argument("--id", required=True,
                    help="fixture id slug, e.g. refund-interruption-001")
    fc.add_argument("--title", default=None,
                    help="human title (default: the id with spaces)")
    fc.add_argument("--onset", type=float, required=True,
                    help="the moment (seconds into the SOURCE recording) the "
                         "caller took or attempted the floor")
    fc.add_argument("--expect", required=True, choices=["yield", "hold"],
                    help="YOUR label for the event: 'yield' (the agent should "
                         "stop for the caller) or 'hold' (the agent should "
                         "keep speaking)")
    fc.add_argument("--out", required=True, metavar="DIR",
                    help="fixture root; writes DIR/scenarios/<id>.json and "
                         "DIR/audio/<id>.example.wav")
    fc.add_argument("--stack", default="generic",
                    choices=["generic", "vapi", "twilio", "livekit",
                             "pipecat", "retell"],
                    help="voice stack the recording came from (labels the "
                         "validation fix knob only)")
    fc.add_argument("--max-talk-over", type=float, default=None,
                    help="[yield] fail the fixture if talk-over exceeds this many seconds")
    fc.add_argument("--max-time-to-yield", type=float, default=None,
                    help="[yield] fail the fixture if the yield is slower than this many seconds")
    fc.add_argument("--tags", default=None,
                    help="comma-separated tags for the scenario JSON")
    fc.add_argument("--category", default=None,
                    choices=["should_yield", "should_not_yield"],
                    help="scenario category (default: derived from --expect)")
    fc.add_argument("--pre", type=float, default=2.0,
                    help="seconds of audio kept BEFORE the onset when clipping (default 2.0)")
    fc.add_argument("--post", type=float, default=6.0,
                    help="seconds of audio kept AFTER the onset when clipping (default 6.0)")
    fc.add_argument("--no-clip", action="store_true",
                    help="keep the full recording and the original onset instead of clipping")
    fc.add_argument("--force", action="store_true",
                    help="overwrite an existing fixture with the same id")
    fc.add_argument("--caller-channel", type=int, default=0)
    fc.add_argument("--agent-channel", type=int, default=1)
    fc.add_argument("--format", default="text", choices=["text", "json"],
                    help="output format (default text)")
    fc.set_defaults(func=_cmd_fixture_create)

    # --- compare: the shareable before/after on one fixed moment ------------
    cp = sub.add_parser(
        "compare",
        help="score a before and an after take of the same moment and report "
             "what actually moved",
        description=(
            "Score two recordings of the SAME scenario (the bad take and the "
            "take after your change) with the identical expectation, bounds, "
            "and reference config, and report the movement per measured "
            "signal plus one machine-stable result word: fixed, regressed, "
            "improved, worse, unchanged, still_pass, or not_scorable. Every "
            "mark is computed from real measurements only; an unjudgeable "
            "side renders NOT SCORABLE, never an invented verdict. Offline; "
            "no accuracy percentage anywhere."
        ),
        epilog=(
            _exit_codes_epilog("compare") + "\n\n"
            "Examples:\n"
            "  hotato compare --before bad.wav --after fixed.wav --onset 12.4 --expect yield\n"
            "  hotato compare --before bad.wav --after fixed.wav \\\n"
            "      --before-onset 12.4 --after-onset 11.9 --expect yield --out report.html\n"
            "  hotato compare --before a.wav --after b.wav --onset 3.1 --expect hold --format json"
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    cp.add_argument("--before", metavar="WAV",
                    help="two-channel WAV of the BEFORE take")
    cp.add_argument("--after", metavar="WAV",
                    help="two-channel WAV of the AFTER take")
    cp.add_argument("--before-caller", metavar="WAV",
                    help="mono caller WAV of the before take (with --before-agent)")
    cp.add_argument("--before-agent", metavar="WAV",
                    help="mono agent WAV of the before take (with --before-caller)")
    cp.add_argument("--after-caller", metavar="WAV",
                    help="mono caller WAV of the after take (with --after-agent)")
    cp.add_argument("--after-agent", metavar="WAV",
                    help="mono agent WAV of the after take (with --after-caller)")
    cp.add_argument("--onset", type=float, default=None,
                    help="caller onset in seconds, applied to BOTH takes "
                         "(else auto-detected per take)")
    cp.add_argument("--before-onset", type=float, default=None,
                    help="override the onset for the before take (the moment "
                         "often shifts between takes)")
    cp.add_argument("--after-onset", type=float, default=None,
                    help="override the onset for the after take")
    cp.add_argument("--expect", default="yield", choices=["yield", "hold"],
                    help="the shared label: 'yield' (stop for the caller) or "
                         "'hold' (keep the floor)")
    cp.add_argument("--stack", default="generic",
                    choices=["generic", "vapi", "twilio", "livekit",
                             "pipecat", "retell"],
                    help="voice stack the recordings came from (labels the fix knob only)")
    cp.add_argument("--max-talk-over", type=float, default=None,
                    help="fail bound applied identically to both takes")
    cp.add_argument("--max-time-to-yield", type=float, default=None,
                    help="fail bound applied identically to both takes")
    cp.add_argument("--caller-channel", type=int, default=0)
    cp.add_argument("--agent-channel", type=int, default=1)
    cp.add_argument("--format", default="text", choices=["text", "json"],
                    help="output format (default text)")
    cp.add_argument("--out", default=None, metavar="PATH",
                    help="also write the self-contained HTML report: the "
                         "after take with the before take as the base "
                         "comparison")
    cp.add_argument("--fail-on-worse", action="store_true",
                    help="exit 1 when the result is regressed or worse "
                         "(default: exit 0; compare measures, it does not gate)")
    cp.set_defaults(func=_cmd_compare)

    # --- scan: candidate turn-taking moments across a whole call ------------
    sc = sub.add_parser(
        "scan",
        help="list candidate turn-taking moments in a whole recording "
             "(timing facts only; you label them)",
        description=(
            "Walk the caller and agent VAD activity tracks across the WHOLE "
            "recording and list candidate turn-taking moments as timing "
            "facts: overlap onsets (the caller became active while the agent "
            "was active, with the overlap length and whether the agent went "
            "silent), agent starts during caller activity, and long response "
            "gaps after the caller finished. Candidates are timing events, "
            "not intent: this tool cannot know whether a caller sound was "
            "'mhm' or 'stop'. You decide the expected behavior and label the "
            "moment with hotato fixture create. Long files are read in a "
            "windowed pass. Offline; no accuracy percentage anywhere."
        ),
        epilog=(
            _exit_codes_epilog("scan") + "\n\n"
            "Examples:\n"
            "  hotato scan --stereo full-call.wav\n"
            "  hotato scan --stereo full-call.wav --top 5\n"
            "  hotato scan --stereo full-call.wav --format json --out candidates.json\n"
            "  hotato fixture create --stereo full-call.wav --onset 42.18 \\\n"
            "      --expect yield --id found-moment-001 --out tests/hotato"
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    sc.add_argument("--stereo", required=True, metavar="WAV",
                    help="two-channel WAV (caller on one channel, agent on the other)")
    sc.add_argument("--caller-channel", type=int, default=0)
    sc.add_argument("--agent-channel", type=int, default=1)
    sc.add_argument("--top", type=int, default=20,
                    help="cap the printed candidates by salience (overlap or "
                         "gap length, longest first); 0 shows all (default 20)")
    sc.add_argument("--min-gap", type=float, default=2.0,
                    help="minimum response gap in seconds to surface as a "
                         "candidate (default 2.0)")
    sc.add_argument("--format", default="text", choices=["text", "json"],
                    help="output format (default text)")
    sc.add_argument("--out", default=None, metavar="PATH",
                    help="write EVERY candidate as JSON here (--top caps only "
                         "the stdout listing)")
    sc.set_defaults(func=_cmd_scan)

    # --- ingest: the composable passive on-ramp (webhook -> candidates) ------
    ig = sub.add_parser(
        "ingest",
        help="wire a webhook to auto-scan every completed call for candidate "
             "moments (discovery, not a verdict)",
        description=(
            "The composable passive on-ramp: point a webhook at `hotato ingest` "
            "once and every completed call is scanned for CANDIDATE turn-taking "
            "moments automatically, so you never have to remember to run a CLI "
            "after a bad call. It COMPOSES existing primitives -- it parses the "
            "platform's webhook payload for the call id / recording locator, "
            "reuses the SAME per-stack fetch as `hotato capture` to pull the "
            "dual-channel recording, then runs `hotato scan` for candidates. "
            "Ingest is DISCOVERY, never a pass/fail and never an intent claim: "
            "it surfaces TIMING candidates only. You review them and promote one "
            "to a permanent regression test with `hotato fixture create` -- the "
            "human label step stays human; ingest never auto-labels, "
            "auto-fixtures, or auto-tunes. It is NOT a daemon: Hotato ships the "
            "command, YOU own the trigger (a webhook handler, a serverless "
            "function, a cron over your call log). The only network is the same "
            "recording fetch `capture` does; everything else is offline. A "
            "webhook payload is untrusted DATA and is never executed."
        ),
        epilog=(
            _exit_codes_epilog("ingest") + "\n\n"
            "Wire your webhook -> hotato ingest (see docs/INGEST.md):\n"
            "  # in your webhook handler, save the payload and call ingest\n"
            "  hotato ingest --stack vapi   --event payload.json    # + VAPI_API_KEY\n"
            "  hotato ingest --stack retell --event payload.json    # + RETELL_API_KEY\n"
            "  hotato ingest --stack twilio --event payload.json    # + TWILIO_ACCOUNT_SID/TOKEN\n"
            "  hotato ingest --stack livekit --event payload.json   # egress file locator\n"
            "  hotato ingest --stack pipecat --event payload.json   # your own event\n\n"
            "Or skip the payload with a direct id:\n"
            "  hotato ingest --stack vapi   --call-id <id> --out candidates.html\n"
            "  hotato ingest --stack twilio --recording-sid RE... --format json\n\n"
            "Then promote a candidate to a regression test:\n"
            "  hotato fixture create --stereo <call>.wav --onset <t> \\\n"
            "      --expect yield|hold --id found-moment-001 --out tests/hotato"
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    ig.add_argument("--stack", required=True, choices=list(_capture.STACKS),
                    help="voice stack the webhook came from")
    ig.add_argument("--event", metavar="PAYLOAD.json",
                    help="the platform webhook payload (JSON, or a form-encoded "
                         "body for Twilio); untrusted DATA, never executed")
    ig.add_argument("--call-id", metavar="ID",
                    help="[vapi|retell] a call id directly, instead of --event")
    ig.add_argument("--recording-sid", metavar="RE...",
                    help="[twilio] a Recording SID directly, instead of --event")
    ig.add_argument("--allow-mono", action="store_true",
                    help="let the fetch pull a mono-only recording (retell/twilio); "
                         "discovery still needs 2 channels to attribute overlap, so "
                         "a mono mix is reported not-scorable (exit 2)")
    ig.add_argument("--caller-channel", type=int, default=0)
    ig.add_argument("--agent-channel", type=int, default=1)
    ig.add_argument("--top", type=int, default=20,
                    help="cap the listing by salience (longest overlap or gap "
                         "first); 0 shows all (default 20)")
    ig.add_argument("--min-gap", type=float, default=2.0,
                    help="minimum response gap in seconds to surface as a "
                         "candidate (default 2.0)")
    ig.add_argument("--format", default="text", choices=["text", "json"],
                    help="stdout format (default text); JSON is the candidate list")
    ig.add_argument("--out", default=None, metavar="report.html",
                    help="also write an HTML candidate report here (all candidates)")
    ig.set_defaults(func=_cmd_ingest)

    # --- analyze: zero-config drop-a-folder discovery + hear-the-bug ---------
    an = sub.add_parser(
        "analyze",
        help="drop a FOLDER of dual-channel calls: ranked candidate-moment "
             "dashboard with a hear-the-bug audio playhead (zero config)",
        description=(
            "Zero-config discovery over a whole FOLDER of dual-channel call "
            "recordings. No scenarios, no labels, no onset, no flags required: "
            "just point it at the folder. Every WAV is walked label-free with "
            "the same whole-call scanner as `hotato scan`; the candidate "
            "turn-taking moments are aggregated across all calls and ranked by "
            "the scanner's own salience (overlap seconds, gap seconds, echo "
            "coherence) so the worst moments float to the top. It writes ONE "
            "self-contained, offline HTML dashboard: each top moment shows the "
            "call file, the timestamp, the candidate kind, the measured number, "
            "and a to-scale caller/agent timeline. For the top moments the REAL "
            "audio around the moment is embedded inline (base64, nothing "
            "uploaded) with a PLAYHEAD that sweeps the timeline in sync with "
            "playback, so you press play and HEAR the overlap or gap land where "
            "the chart marks it. Candidates are MEASURED timing moments, never "
            "verdicts and never intent: you decide the expected behavior and "
            "label the ones that matter with `hotato fixture create`. "
            "Non-dual-channel or unreadable files are reported cleanly as "
            "skipped with their reason, never a crash. Offline; no accuracy "
            "percentage anywhere."
        ),
        epilog=(
            _exit_codes_epilog("analyze") + "\n\n"
            "Examples:\n"
            "  hotato analyze ./recordings                      # dashboard -> hotato-analyze.html\n"
            "  hotato analyze ./recordings --out calls.html --audio-top 12\n"
            "  hotato analyze ./recordings --format json        # ranked candidates for an agent\n"
            "  hotato ./recordings                              # bare folder routes here\n\n"
            + _LABEL_NOTE
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    an.add_argument("folder", metavar="FOLDER",
                    help="a directory of dual-channel call recordings (WAVs); "
                         "walked recursively, label-free")
    an.add_argument("--caller-channel", type=int, default=0)
    an.add_argument("--agent-channel", type=int, default=1)
    an.add_argument("--top", type=int, default=25,
                    help="cap the ranked moments shown in the dashboard (and the "
                         "stdout JSON) by salience, longest overlap or gap first; "
                         "0 shows all (default 25)")
    an.add_argument("--audio-top", type=int, default=8,
                    help="embed the hear-the-bug audio player for the top N "
                         "moments (the rest show the timeline only); keeps the "
                         "page a reasonable size (default 8)")
    an.add_argument("--pre", type=float, default=2.0,
                    help="seconds of audio/timeline kept BEFORE each moment (default 2.0)")
    an.add_argument("--post", type=float, default=4.0,
                    help="seconds of audio/timeline kept AFTER each moment (default 4.0)")
    an.add_argument("--min-gap", type=float, default=2.0,
                    help="minimum response gap in seconds to surface as a "
                         "candidate (default 2.0)")
    an.add_argument("--format", default="html", choices=["html", "json"],
                    help="output: 'html' (the self-contained dashboard, default) "
                         "or 'json' (ranked candidates + metadata to stdout)")
    an.add_argument("--out", default=None, metavar="PATH",
                    help="where to write the dashboard (default hotato-analyze.html); "
                         "with --format json, also writes the full ranked JSON here")
    an.add_argument("--no-open", action="store_true",
                    help="do not launch a browser for the HTML dashboard; just "
                         "write and print the path")
    an.set_defaults(func=_cmd_analyze)

    # --- patch: Level 3, turn a fix plan into a paste-ready patch ------------
    pt = sub.add_parser(
        "patch",
        help="render a fix plan into a literal, paste-ready patch per platform "
             "(produces the change; never applies it)",
        description=(
            "Read a fix plan (schema hotato.fixplan.v1, from hotato plan) and "
            "render its abstract {field, from, to} recommendation into a "
            "LITERAL, paste-ready artifact for the target stack: a JSON "
            "merge-patch body plus a ready curl against the platform's real "
            "config-update endpoint (Vapi, Retell), or the exact source edit "
            "when the config lives in agent code (LiveKit, Pipecat). Field names "
            "come straight from the plan (verified in fixmap's knob catalogue). "
            "patch ONLY handles the config-fixable classes: for a plan whose "
            "decision is do_not_tune_single_threshold (the genuine both-axes "
            "case) it emits NO config patch and prints the vendor-neutral, "
            "numbers-free engagement-control pointer instead. HONEST: patch "
            "PRODUCES the change; it NEVER applies it to your platform and makes "
            "no network call. You review it, apply it, then prove it with "
            "hotato verify."
        ),
        epilog=(
            _exit_codes_epilog("patch") + "\n\n"
            "Examples:\n"
            "  hotato plan result.json --stack vapi --assistant-id <id> --out fixplan.json\n"
            "  hotato patch fixplan.json                 # the curl + merge-patch to paste\n"
            "  hotato patch fixplan.json --format json --out patch.json"
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    pt.add_argument("fixplan", metavar="FIXPLAN.json",
                    help="a fix plan JSON from hotato plan (schema "
                         "hotato.fixplan.v1)")
    pt.add_argument("--format", default="text", choices=["json", "text"],
                    help="output format (default text; json prints the full "
                         "patch artifact)")
    pt.add_argument("--out", default=None, metavar="PATH",
                    help="also write the patch artifact JSON here")
    pt.set_defaults(func=_cmd_patch)

    # --- verify: battery-scale before/after proof a fix held ----------------
    vf = sub.add_parser(
        "verify",
        help="prove a fix across the whole battery: before/after run envelopes "
             "-> N of M failing fixtures now pass",
        description=(
            "After you apply a config change and RE-CAPTURE the previously "
            "failing fixtures, verify scores the old and new run envelopes "
            "against each other and reports what really moved across the whole "
            "battery: 'N of M fixtures that used to fail now pass, and K of L "
            "hold fixtures still pass'. It reuses the compare TAXONOMY (fixed, "
            "regressed, improved, worse, unchanged, still_pass, not_scorable) "
            "per fixture and aggregate's pooled-distribution definitions for the "
            "before/after talk-over and time-to-yield shift. It reports "
            "COINCIDENCE, never causation, and REFUSES a battery-scale claim "
            "when too few fixtures failed to characterize (--min-n): the "
            "per-fixture facts still print, but the headline proof is withheld "
            "and said so. An unjudgeable side is not_scorable, never an invented "
            "verdict; a fixture on only one side is reported unpaired, never "
            "silently dropped. Each side is a single run envelope JSON or a "
            "directory of them; fixtures pair by event_id then scenario_id."
        ),
        epilog=(
            _exit_codes_epilog("verify") + "\n\n"
            "Examples:\n"
            "  # score the same battery before and after the change\n"
            "  hotato run --scenarios tests/hotato/scenarios --audio tests/hotato/audio \\\n"
            "      --format json > before.json      # (the failing take)\n"
            "  hotato run --scenarios tests/hotato/scenarios --audio tests/hotato/audio-new \\\n"
            "      --format json > after.json       # (after applying the patch + re-capturing)\n"
            "  hotato verify --before before.json --after after.json\n"
            "  hotato verify --before before/ --after after/ --min-n 5 --fail-on-regression"
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    vf.add_argument("--before", required=True, metavar="RUN.json|DIR",
                    help="the OLD run envelope(s): a single hotato run JSON, or "
                         "a directory of them (the previously-failing take)")
    vf.add_argument("--after", required=True, metavar="RUN.json|DIR",
                    help="the NEW run envelope(s) after applying the change and "
                         "re-capturing the same fixtures")
    vf.add_argument("--min-n", type=int, default=3,
                    help="minimum number of previously-failing fixtures needed "
                         "to state a battery-scale proof; below it the headline "
                         "claim is refused honestly (default 3)")
    vf.add_argument("--fail-on-regression", action="store_true",
                    help="exit 1 if any fixture regressed or got worse (default: "
                         "exit 0; verify measures, it does not gate)")
    vf.add_argument("--format", default="text", choices=["json", "text"],
                    help="output format (default text)")
    vf.add_argument("--out", default=None, metavar="PATH",
                    help="also write the full proof JSON here")
    vf.set_defaults(func=_cmd_verify)

    # --- loop: one-command orchestration of the closed loop, with memory ----
    lp = sub.add_parser(
        "loop",
        help="drive the closed fix loop (find -> label -> plan -> verify) and "
             "remember where it left off across runs",
        description=(
            "One command for the closed loop, with memory. First run over a "
            "FOLDER of calls runs discovery (analyze -> scan -> rank) and "
            "records the candidate moments in a small local state file "
            "(.hotato/loop-state.json by default): a second run then tells you "
            "what is waiting on YOU -- 'you have N candidate moments awaiting "
            "your label', or, once you have labeled fixtures with hotato fixture "
            "create, 'a fix plan is ready; apply it with hotato patch, then "
            "prove it with hotato verify'. It orchestrates and tracks state; the "
            "human keeps the two irreversible decisions. HARD rules: it NEVER "
            "auto-labels (you supply every yield/hold intent), NEVER auto-applies "
            "(it produces a plan and points at hotato patch; applying and "
            "verifying stay human), and mutates no platform."
        ),
        epilog=(
            _exit_codes_epilog("loop") + "\n\n"
            "Examples:\n"
            "  hotato loop ./recordings                          # run 1: discover -> awaiting_label\n"
            "  hotato fixture create --stereo rec.wav --onset 12.4 \\\n"
            "      --expect yield --id refund-001 --out tests/hotato\n"
            "  hotato loop ./recordings --fixtures tests/hotato   # run 2: plan -> awaiting_verify\n"
            "  hotato loop ./recordings --format json             # machine state\n\n"
            + _LABEL_NOTE
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    lp.add_argument("folder", nargs="?", default=None, metavar="FOLDER",
                    help="a directory of dual-channel call recordings to "
                         "discover from (required on the first run)")
    lp.add_argument("--fixtures", default=None, metavar="DIR",
                    help="the fixture root you labeled with hotato fixture "
                         "create --out DIR (DIR/scenarios + DIR/audio); when it "
                         "has scenarios, loop plans a fix from them")
    lp.add_argument("--state", default=None, metavar="PATH",
                    help="loop state file (default .hotato/loop-state.json in "
                         "the current directory)")
    lp.add_argument("--rediscover", action="store_true",
                    help="re-run discovery over the folder even if state already "
                         "exists")
    lp.add_argument("--stack", default="generic",
                    choices=["generic", "vapi", "retell", "livekit", "pipecat",
                             "twilio"],
                    help="stack to plan against when it reaches the planning "
                         "step (default generic)")
    lp.add_argument("--min-gap", type=float, default=2.0,
                    help="minimum response gap in seconds to surface as a "
                         "discovery candidate (default 2.0)")
    lp.add_argument("--top", type=int, default=10,
                    help="how many top candidate moments to record in state for "
                         "the label step (default 10)")
    lp.add_argument("--format", default="text", choices=["json", "text"],
                    help="output format (default text)")
    lp.set_defaults(func=_cmd_loop)

    # --- describe: the generated capability manifest (machine-drivability) --
    ds = sub.add_parser(
        "describe",
        help="emit a generated capability manifest of the whole CLI (every "
             "subcommand, its args, and its exit codes)",
        description=(
            "Walk this CLI's own argparse structure and emit a generated "
            "CAPABILITY MANIFEST: every subcommand's name, purpose, argument "
            "list (name, type, required, default, help), and documented exit "
            "codes, plus the tool version and the two schema URLs (envelope, "
            "error). One call for an agent to learn the whole CLI instead of "
            "scraping --help across every subcommand. Generated straight from "
            "the parser, so it can never drift from the real flags. Pure and "
            "deterministic: same input, same output, every time."
        ),
        epilog=_exit_codes_epilog("describe"),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    ds.add_argument("--format", default="text", choices=["json", "text"],
                    help="output format (default text: a readable summary; "
                         "json for the machine manifest)")
    ds.set_defaults(func=_cmd_describe)

    return p


def _route_bare_folder(argv, parser) -> "list | None":
    """Nicety: ``hotato <folder>`` (a bare positional that is an existing
    directory, not a known subcommand or a flag) routes to ``analyze <folder>``.
    Returns the rewritten argv, or None to leave it untouched."""
    if not argv:
        return None
    first = argv[0]
    if first.startswith("-"):
        return None
    subcommands = set()
    for action in parser._actions:
        if isinstance(action, argparse._SubParsersAction):
            subcommands = set(action.choices)
            break
    if first in subcommands:
        return None
    if os.path.isdir(first):
        return ["analyze"] + list(argv)
    return None


def main(argv=None) -> int:
    parser = build_parser()
    raw = sys.argv[1:] if argv is None else list(argv)
    rerouted = _route_bare_folder(raw, parser)
    args = parser.parse_args(rerouted if rerouted is not None else raw)
    # Bare `hotato` (no subcommand): guide the user to score their OWN call.
    if getattr(args, "func", None) is None:
        print(_FIRST_RUN_GUIDE, end="")
        return 0
    try:
        return args.func(args)
    except _errors.HANDLED as exc:
        # The SHARED handled-error contract (errors.HANDLED): ValueError, the
        # OSError family (missing / unreadable / directory / already-exists file
        # inputs), and BackendUnavailable.
        # BackendUnavailable = --backend neural requested without the [neural] extra
        # (or without cached weights): a clean, explicit config error, never a silent
        # fallback to the energy reference.
        if getattr(args, "format", "text") == "json":
            # The machine surface gets the SAME structured error object the one
            # MCP tool emits (schema/error.v1.json): ok=false, a stable
            # error_code, and exit_code 2. So an agent parses one shape for the
            # whole call lifecycle (success envelope, or this on failure). The
            # plain "error:" line below stays for --format text.
            print(json.dumps(_errors.cli_error(exc), indent=2))
            return 2
        print(f"error: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
