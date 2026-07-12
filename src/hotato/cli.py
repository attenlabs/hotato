"""Zero-install CLI.

    # score one recording (two-channel: caller on one channel, agent on the other)
    uvx hotato run --stereo call.wav --stack livekit --format json

    # run the bundled 8-scenario battery; exits non-zero on any regression (for CI)
    uvx hotato run --suite barge-in --stack pipecat --format json

Everything runs offline; no audio leaves the machine.
"""

from __future__ import annotations

from .errors import open_regular as _open_regular

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

def _atomic_write_text(path: str, text: str) -> None:
    """Write ``text`` to ``path`` atomically: a temp file in the SAME directory,
    then ``os.replace`` (the pattern already proven in connections.py /
    loop.save_state). ``open(path, "w")`` truncates the target the instant it is
    opened, so a crash / full disk / kill mid-write leaves a previously-good
    ``--out`` file truncated in place; writing a temp file first and renaming it
    means the destination is only ever the old bytes or the complete new bytes,
    never a half-written mix."""
    d = os.path.dirname(os.path.abspath(path)) or "."
    fd, tmp = tempfile.mkstemp(dir=d, prefix=".hotato-tmp-", suffix=".part")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(text)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def _atomic_write_json(path: str, obj) -> None:
    """Atomic JSON write mirroring the existing ``json.dump(obj, fh, indent=2)``
    + trailing newline form, but crash-safe (see ``_atomic_write_text``)."""
    _atomic_write_text(path, _errors.safe_json_dumps(obj, indent=2) + "\n")


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
        (2, "usage error or unusable input (including an unreadable or "
            "schema-mismatched --trace file); --no-fail always exits 0"),
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
    "explain": (
        (0, "explained, nothing attributable (no failing/ambiguous events)"),
        (1, "explained: at least one attribution or refusal was produced"),
        (2, "usage error or unusable input (a bad candidate ref, a file that "
            "is not a hotato result, or an unreadable contract bundle)"),
    ),
    "fixture": (
        (2, "no subcommand given (see hotato fixture create/promote --help)"),
    ),
    "fixture create": (
        (0, "fixture written (scored immediately)"),
        (2, "refused: unusable input or a not-scorable moment"),
    ),
    "fixture promote": (
        (0, "fixture written (scored immediately)"),
        (2, "refused: a bad candidate ref, a file that is not a sweep/analyze "
            "result, a source recording that does not resolve, or a "
            "not-scorable candidate"),
    ),
    "contract": (
        (2, "no subcommand given (see hotato contract create/verify/inspect/"
            "pack/unpack --help)"),
    ),
    "contract create": (
        (0, "contract bundle written (scored immediately)"),
        (2, "refused: unusable input, a mono recording without --diarize, or "
            "a not-scorable moment; no bundle is written"),
    ),
    "contract verify": (
        (0, "verified: every contract's re-scored timing passes its policy, "
            "and every embedded assertion (if any) is PASS/INCONCLUSIVE"),
        (1, "at least one contract regressed (its re-scored timing no longer "
            "meets its policy pass_conditions) or is no longer scorable, OR "
            "at least one embedded assertion deterministically FAILed "
            "(reported as a separate dimension, never blended with timing)"),
        (2, "usage error, a directory with no contracts, a corrupt/"
            "unreadable contract.json, an unreadable --transcript file, or a "
            "malformed embedded assertions block"),
    ),
    "contract inspect": (
        (0, "contract printed"),
        (2, "usage error or an unreadable/corrupt contract.json"),
    ),
    "contract pack": (
        (0, "the bundle was packed into one deterministic .hotato archive "
            "with a sha256 manifest"),
        (2, "usage error, a bundle with no contract.json, or an existing "
            "--out without --force"),
    ),
    "contract unpack": (
        (0, "the archive was unpacked and every member verified against its "
            "sha256 manifest"),
        (2, "usage error, a corrupt/tampered archive (sha256 mismatch), an "
            "existing --out without --force, or a hostile archive (path "
            "traversal, a symlink or encrypted member, a duplicate or "
            "undeclared member, too many members, or a declared/actual "
            "decompressed size past --max-bytes)"),
    ),
    "trace": (
        (2, "no subcommand given (see hotato trace ingest/attach/export "
            "--help)"),
    ),
    "trace ingest": (
        (0, "voice_trace.jsonl written"),
        (2, "usage error, an unreadable input file, or a source with no "
            "spans; --out is refused unopened without --force when it "
            "already exists"),
    ),
    "trace attach": (
        (0, "the trace was written into the bundle and evidence/"
            "timeline.html was re-rendered"),
        (2, "usage error, a missing/corrupt bundle, an unreadable or "
            "schema-mismatched trace file, or an already-attached trace "
            "without --force"),
    ),
    "trace export": (
        (0, "the attached trace was written back out as OTel-flavored "
            "bridge JSONL"),
        (2, "usage error, no trace attached to the bundle, or an existing "
            "--out without --force"),
    ),
    "assert": (
        (2, "no subcommand given (see hotato assert init/run --help)"),
    ),
    "assert init": (
        (0, "a starter assertions.yaml was written"),
        (2, "usage error, an unreadable/mismatched --from-trace file, "
            "nothing could be inferred (no tool_call spans with a "
            "renderable name and no --stereo timing), an unscorable "
            "--stereo recording, or an existing --out without --force"),
    ),
    "assert run": (
        (0, "every assertion's deterministic status was PASS or (under the "
            "default --inconclusive-policy report) INCONCLUSIVE"),
        (1, "at least one assertion's deterministic status was FAIL -- or, "
            "under --inconclusive-policy fail, at least one INCONCLUSIVE "
            "(missing required input) result"),
        (2, "under --inconclusive-policy refuse, at least one INCONCLUSIVE "
            "result withheld the verdict (this refusal takes precedence over "
            "a FAIL); OR a usage error / unusable input: a malformed "
            "--assertions file (including a bad inconclusive_policy value), "
            "an unreadable --transcript/--trace file, an unscorable --stereo "
            "recording, or --transcribe without --stereo (or combined with "
            "--transcript)"),
    ),
    "test": (
        (2, "no subcommand given (see hotato test run --help)"),
    ),
    "test run": (
        (0, "the deterministic lane passed (or, under --inconclusive-policy "
            "report, at most INCONCLUSIVE) AND every success.required condition "
            "held"),
        (1, "a success.required condition failed, a deterministic assertion "
            "FAILed, or -- under --inconclusive-policy fail -- an INCONCLUSIVE "
            "(missing required input) result gated"),
        (2, "under --inconclusive-policy refuse an INCONCLUSIVE result withheld "
            "the verdict (takes precedence over a FAIL); OR a usage error / "
            "unusable input: a malformed conversation-test file, an "
            "unreadable --transcript/--trace/--state file, an unscorable "
            "--audio recording, or html/md without --out and --audio"),
    ),
    "scenario": (
        (2, "no subcommand given (see hotato scenario init/validate --help)"),
    ),
    "scenario init": (
        (0, "a starter conversation-test.yaml was written"),
        (2, "usage error, or an existing --out without --force"),
    ),
    "scenario validate": (
        (0, "every conversation-test file validated"),
        (2, "at least one file is malformed, or a usage error (no file/dir, or "
            "a directory with no conversation-test files)"),
    ),
    "conversation": (
        (2, "no subcommand given (see hotato conversation verify --help)"),
    ),
    "conversation verify": (
        (0, "every bound artifact re-hashed to its recorded digest"),
        (2, "REFUSED: a tampered (digest-mismatched) or missing child, a "
            "malformed manifest, or a usage error (no such directory/manifest)"),
    ),
    "simulate": (
        (0, "every produced conversation is labelled origin=simulated and "
            "validated as a faithful rendering of its scenario (and, under "
            "--matrix --conversation-test, every scored aggregate passed)"),
        (1, "at least one produced simulation was SIMULATOR_INVALID -- a broken "
            "fixture, never an agent PASS/FAIL -- or, under --matrix "
            "--conversation-test, a scored aggregate FAILed (or an INCONCLUSIVE "
            "gated under inconclusive_policy fail)"),
        (2, "under --matrix --conversation-test with inconclusive_policy refuse, "
            "a scored INCONCLUSIVE withheld the verdict; OR a usage error / "
            "unusable input: a malformed or unreadable scenario / "
            "conversation-test file"),
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
    "trust": (
        (0, "the recording is eligible for scan (the input-health report is printed; "
            "never a turn-taking verdict)"),
        (2, "NOT SCORABLE (mono, identical channels, or a silent required "
            "channel -- the report names the reason and the next step), a usage "
            "error, or unreadable input"),
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
        (1, "a gate you opted into failed: with --fail-on-regression a fixture "
            "regressed or got worse, or with --policy a guardrail was violated "
            "or a target.improve criterion was not met"),
        (2, "usage error, unreadable input, an invalid --policy file, or no "
            "fixtures pair between the two sides"),
    ),
    "apply": (
        (0, "the staging clone was rendered (a dry run by default: prints the "
            "clone it WOULD create and the patch it WOULD apply, creating "
            "nothing and touching no network; with --yes and credentials it "
            "created a NEW staging assistant and applied the patch to that "
            "clone -- the source is never mutated)"),
        (2, "usage error: no --clone (production apply is not supported), no "
            "--name, no opposite-risk battery (both a yield and a hold "
            "fixture), a stack with no assistant to clone, a patch that "
            "produced no config change, or unreadable input"),
        (3, "principled refusal: the plan is the both-axes threshold funnel, so "
            "no single-threshold patch is applied by design (the exact "
            "engagement-control recommendation is printed). The refusal is the "
            "feature; this distinct code lets a script tell it apart from a "
            "usage error"),
    ),
    "fix": (
        (2, "no subcommand given (see hotato fix trial --help)"),
    ),
    "fix trial": (
        (0, "IMPROVED: the verify claim is supported, at least one "
            "previously-failing fixture now passes, and nothing regressed "
            "(no fixture, no contract, no --policy criterion)"),
        (1, "fail-closed: REGRESSED (a fixture, a contract, or --policy "
            "regressed) or INCONCLUSIVE (too few previously-failing "
            "fixtures, or none now pass) -- inconclusive is not a pass and "
            "exits the same non-zero code as a regression"),
        (2, "usage error or unusable input: the same gates hotato apply "
            "enforces (no --name, no opposite-risk battery, a stack with no "
            "clone target, a patch with no concrete change) or hotato "
            "verify/contract verify already enforce (no fixtures pair, an "
            "invalid --policy, a --contracts dir with no contracts, "
            "unreadable input)"),
        (3, "principled refusal: the patch is the both-axes threshold "
            "funnel, so no trial is run and no single-threshold patch is "
            "endorsed by design (the exact engagement-control "
            "recommendation is printed). The refusal is the feature; this "
            "distinct code, shared with hotato apply, lets a script tell it "
            "apart from a usage error"),
    ),
    "loop": (
        (0, "advanced the loop and persisted state (or re-reported where it "
            "left off)"),
        (2, "usage error: no folder on the first run, an unreadable state file, "
            "or a path that is not a folder"),
    ),
    "investigate": (
        (0, "the recording is candidate-eligible: trust + scan ran and "
            "candidates (if any) were persisted; a real yield/hold VERDICT "
            "may still be refused (K6) -- see verdict_status"),
        (2, "usage error (neither SOURCE nor --stack/--call-id, both given, "
            "a bad channel/--min-gap flag, a missing credential, or an "
            "unreadable state file), or the recording is NOT SCORABLE at all"),
    ),
    "investigate label": (
        (0, "a signed, CI-ready contract was written from this candidate"),
        (2, "usage error (a bad --expect, a bad candidate ref, an unresolved "
            "source recording, or an existing contract without --force), or "
            "the candidate turned out not scorable"),
    ),
    "describe": (
        (0, "manifest printed"),
    ),
    "card": (
        (0, "the SVG card was rendered (written to --out, or to stdout)"),
        (2, "usage error, unreadable input, a bad candidate ref, or an input "
            "that is not a fix plan / verify result / sweep candidate"),
    ),
    "start": (
        (0, "the guided first run completed (or a stubbed mode printed the "
            "shipped command to use instead)"),
        (2, "usage error: no mode given, or --dir is not a directory"),
    ),
    "init": (
        (2, "no subcommand given (see hotato init webhook --help / hotato "
            "init starter --help)"),
    ),
    "init webhook": (
        (0, "scaffolded the webhook worker project to --out"),
        (2, "usage error (unknown --stack / --target), or a destination file "
            "already exists without --force"),
    ),
    "init starter": (
        (0, "scaffolded the starter kit (CI gate, hotato.yaml, fixtures/, "
            "contracts/, reports/) to --out"),
        (2, "usage error (unknown --stack), or a destination file already "
            "exists without --force"),
    ),
    "issue": (
        (2, "no subcommand given (see hotato issue create --help)"),
    ),
    "issue create": (
        (0, "rendered the issue (a dry run by default: prints the body and the "
            "exact gh command, creating nothing; with --yes it created the "
            "issue through gh)"),
        (2, "usage error: no --repo, a file that is not a sweep/analyze "
            "result, no candidate moments to file, or gh failed or was not "
            "found"),
    ),
    "pr": (
        (2, "no subcommand given (see hotato pr create --help)"),
    ),
    "pr create": (
        (0, "rendered the pull request (a dry run by default: prints the body "
            "and the exact git and gh commands, changing nothing; with --yes it "
            "cut a feature branch, committed the fixtures, pushed, and opened "
            "the PR through gh)"),
        (2, "usage error: no --repo, no --fixtures, no --title, a fixtures "
            "directory with no scenarios to add, or git/gh failed or was not "
            "found"),
    ),
    "fleet": (
        (2, "no subcommand given (see hotato fleet <cmd> --help)"),
    ),
    "fleet init": (
        (0, "the workspace was created/ensured under --home"),
        (2, "usage error or an unwritable --home"),
    ),
    "fleet agent": (
        (2, "no subcommand given (see hotato fleet agent add/list --help)"),
    ),
    "fleet agent add": (
        (0, "the agent was registered in the workspace"),
        (2, "usage error or an unwritable --home"),
    ),
    "fleet agent list": (
        (0, "listed the registered agents (possibly none)"),
        (2, "usage error or an unreadable --home"),
    ),
    "fleet ingest": (
        (0, "the recording was registered (or deduped on a replayed pull)"),
        (2, "usage error, unreadable audio, or an unwritable --home"),
    ),
    "fleet discover": (
        (0, "scanned the recording (candidate moments listed, possibly zero; "
            "never a verdict and never an auto-label)"),
        (2, "not-scorable input (the honest trust recommendation is printed), a "
            "usage error, or unreadable audio"),
    ),
    "fleet review": (
        (0, "listed candidates awaiting a human label (possibly none)"),
        (2, "usage error or an unreadable --home"),
    ),
    "fleet label": (
        (0, "recorded the human label on the candidate"),
        (2, "usage error, an invalid decision, or an unreadable --home"),
    ),
    "fleet status": (
        (0, "printed the workspace counts and job-queue stats"),
        (2, "usage error or an unreadable --home"),
    ),
    "fleet benchmark": (
        (0, "the private per-agent benchmark was produced"),
        (2, "usage error or an unwritable --home"),
    ),
    "fleet experiment": (
        (2, "no subcommand given (see hotato fleet experiment run --help)"),
    ),
    "fleet experiment create": (
        (0, "the trial manifest was precommitted from the battery (its fixture "
            "universe is now pinned before any capture)"),
        (2, "usage error or unreadable battery/policy input"),
    ),
    "fleet experiment propose": (
        (0, "a bounded variant set was generated and persisted"),
        (2, "usage error or no catalogue entry for the stack/intent"),
    ),
    "fleet experiment approve": (
        (0, "the human approval was recorded (no deployment is performed)"),
        (2, "usage error or unknown trial"),
    ),
    "fleet run": (
        (0, "recordings were ingested + discovered and candidates reclustered"),
        (2, "usage error or unreadable recording input"),
    ),
    "fleet contract": (
        (2, "no subcommand given (see hotato fleet contract create --help)"),
    ),
    "fleet contract create": (
        (0, "a failure contract was minted from the candidate and registered"),
        (2, "usage error, unknown candidate, or a not-scorable recording"),
    ),
    "fleet retention": (
        (0, "a retention/consent policy was attached to the recording"),
        (2, "usage error or unknown recording"),
    ),
    "fleet delete": (
        (0, "the recording audio was deleted and a receipt recorded"),
        (1, "deletion was blocked by a legal hold"),
        (2, "usage error or unknown recording"),
    ),
    "fleet redact": (
        (0, "a derived redacted copy was written and registered"),
        (2, "usage error or unknown recording"),
    ),
    "synth": (
        (0, "synthetic-derived perturbations were written as a separate axis"),
        (2, "usage error or unreadable source audio"),
    ),
    "fleet experiment run": (
        (0, "the before/after battery improved under the pinned manifest "
            "(recommendation recorded; never auto-deployed)"),
        (1, "inconclusive or refused: no green paired proof, so nothing is "
            "recommended for deployment"),
        (2, "usage error or unreadable battery/before/after/policy input"),
    ),
    "fleet canary": (
        (2, "recommendation-only: live canary routing is not enabled in this "
            "release"),
    ),
    "fleet canary start": (
        (2, "recommendation-only: live canary routing is not enabled in this "
            "release"),
    ),
    "fleet canary rollback": (
        (2, "recommendation-only: live canary routing is not enabled in this "
            "release"),
    ),
    "fleet export": (
        (0, "wrote (or printed) the status + agents/trials manifest"),
        (2, "usage error or an unwritable --out"),
    ),
    "fleet trend": (
        (0, "wrote the self-contained trend dashboard (possibly zero agents "
            "or zero history: honest-empty states, never a crash)"),
        (2, "usage error or an unwritable --out"),
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
        print(_errors.safe_json_dumps(env, indent=2))
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
    # The pass RATE is over the SCORABLE events only (passed + failed). A
    # not-scorable event is an input problem, excluded from both sides of the
    # ratio, so it never silently deflates the headline. It is reported
    # separately in the counts above.
    n_scorable = s["passed"] + s["failed"]
    print(f"  {s['passed']}/{n_scorable} events pass  ({counts})")
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
    if env.get("transcript"):
        _print_transcript_panel(env["transcript"])
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


# --------------------------------------------------------------------------- #
# --transcribe: the --format text panel for the ``transcript`` envelope key
# core.run_single attaches (opt-in, CONTEXT ONLY -- see
# core._attach_transcript_context / hotato.transcribe for the seam itself and
# its honesty invariants). Nothing here touches scoring; this only renders a
# key that is already present in ``env`` by the time ``_emit`` sees it.
# --------------------------------------------------------------------------- #

_TRANSCRIPT_PANEL_MAX_CHARS = 400


def _print_transcript_panel(block: dict) -> None:
    """The --format text panel: short, clearly labelled 'context, not scored'
    so it can never be mistaken for a verdict. Full text/segments are always
    available in --format json; this is a terminal-sized preview."""
    print()
    print("  Transcript (context, not scored):")
    text = block.get("text") or ""
    if not text:
        print("    (no speech detected)")
    else:
        shown = text if len(text) <= _TRANSCRIPT_PANEL_MAX_CHARS else (
            text[:_TRANSCRIPT_PANEL_MAX_CHARS].rstrip() + "..."
        )
        print(f"    {shown}")
    print(
        f"    model={block.get('model')} device={block.get('device')} "
        "-- never affects the verdict above; see --format json for the full "
        "transcript"
    )


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
    if suite_mode and (args.stereo or args.caller or args.agent or getattr(args, "mono", None)):
        raise ValueError(
            "--suite (or --scenarios/--audio together) runs a labelled battery "
            "and cannot be combined with a single recording (--stereo / --caller "
            "/ --agent / --mono). Run one or the other."
        )
    if suite_mode and getattr(args, "transcribe", False):
        raise ValueError(
            "--transcribe works on a single recording; drop --suite (and/or "
            "--scenarios/--audio) and pass --stereo (or --mono --diarize)"
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
        _atomic_write_json(args.dump_frames, dump)
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
            mono=getattr(args, "mono", None),
            caller_channel=args.caller_channel,
            agent_channel=args.agent_channel,
            onset_sec=args.onset,
            expect=args.expect,
            stack=args.stack,
            max_talk_over_sec=args.max_talk_over,
            max_time_to_yield_sec=args.max_time_to_yield,
            cfg=cfg,
            echo_gate=getattr(args, "echo_gate", False),
            diarize=getattr(args, "diarize", False),
            diarizer=getattr(args, "diarizer", "pyannote"),
            caller_speaker=getattr(args, "caller_speaker", None),
            agent_speaker=getattr(args, "agent_speaker", None),
            egress_opt_in=getattr(args, "egress_opt_in", False),
            transcribe=getattr(args, "transcribe", False),
            transcribe_model=getattr(args, "transcribe_model", "base.en"),
            transcribe_device=getattr(args, "transcribe_device", "auto"),
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
        demo=args.demo,
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
        notify=args.notify,
    )


def _load_base_envelope(path: str) -> dict:
    """Load a previous envelope JSON for --base. Anything that is not a hotato
    envelope is a clean usage error (exit 2), never a silent no-op diff."""
    with _open_regular(path, "r", encoding="utf-8") as fh:
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
    # An optional voice trace is loaded here (import inside the function so
    # report.py never imports hotato.trace -- that would be a circular import)
    # and handed to the report builder purely as CONTEXT; it is never scored.
    trace = None
    if args.trace:
        from . import trace as _trace
        trace = _trace.load_voice_trace_jsonl(args.trace)
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
            trace=trace,
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
            trace=trace,
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
    skipped = loaded["skipped"]
    if len(runs) < 2:
        # Not enough runs to show a trend, exit 0 (we never pad one). This path
        # must STILL honor --format json (an agent piping this into json.load
        # would otherwise crash on a plain sentence) and STILL surface WHY each
        # file was rejected (load_run_dir already recorded file+reason), so the
        # user can see why 0 of N files were recognized as run envelopes.
        msg = (
            f"team mode needs at least 2 run envelopes to aggregate; found "
            f"{len(runs)} in {args.dir}. Save runs with: "
            "hotato run --suite barge-in --format json > runs/001.json"
        )
        if args.format == "json":
            print(_errors.safe_json_dumps({
                "tool": "hotato",
                "kind": "team",
                "runs_found": len(runs),
                "message": msg,
                "skipped": skipped,
            }, indent=2))
        else:
            print(msg)
            if skipped:
                print(f"  {len(skipped)} file(s) not recognized as run "
                      "envelopes:")
                for s in skipped:
                    print(f"    {s['file']}: {s['why']}")
        return 0
    agg = _aggregate.aggregate_runs(runs, order=args.order,
                                    skipped=loaded["skipped"],
                                    max_response_gap_sec=args.max_response_gap)
    if args.out:
        _atomic_write_json(args.out, agg)
        print(f"wrote aggregate envelope to {args.out}", file=sys.stderr)
    if args.html:
        _atomic_write_text(args.html, _aggregate.build_team_page_html(agg))
        print(f"wrote self-contained HTML team page to {args.html}",
              file=sys.stderr)
    if args.format == "json":
        print(_errors.safe_json_dumps(agg, indent=2))
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
        _atomic_write_json(args.out, result)
        print(f"wrote stack benchmark result to {args.out}", file=sys.stderr)
    else:
        print(_errors.safe_json_dumps(result, indent=2))
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
        text = _errors.safe_json_dumps(cmp_env, indent=2)
    else:
        text = _stackbench.render_comparison_md(cmp_env)
    if args.out:
        _atomic_write_text(args.out, text if text.endswith("\n") else text + "\n")
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

    _atomic_write_text(out, html_str)

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
    with _open_regular(path, "r", encoding="utf-8") as fh:
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
        print(_errors.safe_json_dumps(diagnosis, indent=2))
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
        print(_errors.safe_json_dumps(result, indent=2))
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
    _atomic_write_json(args.out, plan)
    if args.format == "json":
        print(_errors.safe_json_dumps(plan, indent=2))
    else:
        print(_fixplan.render_text(plan))
    print(f"wrote fix plan ({plan['decision']}) to {args.out}", file=sys.stderr)
    return 0


def _cmd_explain(args) -> int:
    from . import explain as _explain

    explanation = _explain.explain(args.source)
    if args.html:
        _atomic_write_text(args.html, _explain.render_html(explanation))
        print(f"wrote {args.html}", file=sys.stderr)
    if args.format == "json":
        print(_errors.safe_json_dumps(explanation, indent=2))
    else:
        print(_explain.render_text(explanation))
    if explanation["attributions"] or explanation["refusals"]:
        return 1
    return 0


def _cmd_patch(args) -> int:
    from . import patch as _patch

    # A fix plan JSON (hotato.fixplan.v1), not a run envelope. A missing file
    # (FileNotFoundError), malformed JSON (ValueError), or a non-plan document
    # (ValueError from build_patch) all surface as the clean exit-2 usage error.
    with _open_regular(args.fixplan, "r", encoding="utf-8") as fh:
        plan = json.load(fh)
    result = _patch.build_patch(plan, source=args.fixplan)
    if args.out:
        _atomic_write_json(args.out, result)
        print(f"wrote patch artifact to {args.out}", file=sys.stderr)
    if args.format == "json":
        print(_errors.safe_json_dumps(result, indent=2))
    else:
        print(_patch.render_text(result))
    return 0


def _cmd_apply(args) -> int:
    from . import apply as _apply

    # Load the patch artifact (a hotato patch JSON). A missing file
    # (FileNotFoundError), malformed JSON (ValueError), or a non-patch document
    # (ValueError from build_apply) all surface as the clean exit-2 usage error.
    with _open_regular(args.patch_json, "r", encoding="utf-8") as fh:
        patch = json.load(fh)
    # Best-effort read of the referenced plan (offline; the patch is
    # self-describing, so a moved/absent plan is not an error).
    plan = _apply.load_referenced_plan(patch, args.patch_json)

    result = _apply.build_apply(
        patch,
        name=args.name,
        clone=args.clone,
        battery_dir=args.battery,
        patch_source=args.patch_json,
        plan=plan,
    )

    # REFUSAL-FIRST: the both-axes threshold funnel. Print the exact canon
    # refusal and exit with the documented, distinct refusal code (the refusal
    # is the feature). No network, no clone, nothing created.
    if result.get("refused"):
        if args.format == "json":
            print(_errors.safe_json_dumps(result, indent=2))
        else:
            print(_apply.render_refusal_text(result))
        return _apply.REFUSAL_EXIT_CODE

    if not args.yes:
        # DEFAULT = dry run: print exactly the clone it WOULD create and the
        # patch it WOULD apply, creating nothing. No network on this path.
        if args.format == "json":
            print(_errors.safe_json_dumps(result, indent=2))
        else:
            print(_apply.render_text(result))
        return 0

    # --yes: the one place this reaches the network. create_clone is the ONLY
    # networked function; it reads the source (GET), applies the patch to a
    # COPY, and creates a NEW staging assistant (POST), never mutating the
    # source. Credentials resolve flag > connections.json > env.
    creds = _capture.resolve_creds(args.stack or result["stack"],
                                   {"api_key": args.api_key})
    clone = result["clone"]
    outcome = _apply.create_clone(
        stack=result["stack"],
        source_id=clone["based_on_source_id"],
        name=clone["name"],
        merge_patch=clone["merge_patch"],
        api_key=creds["api_key"],
    )
    result["created"] = True
    result["dry_run"] = False
    result["applies_change"] = True
    result["clone_id"] = outcome["clone_id"]
    if args.format == "json":
        print(_errors.safe_json_dumps(result, indent=2))
    else:
        print(_apply.render_text(result))
    return 0


def _cmd_verify(args) -> int:
    from . import verify as _verify

    result = _verify.verify_sides(args.before, args.after, min_n=args.min_n)
    # Optional hotato.verify.yaml policy: gate the run on declared success
    # criteria (target.improve) AND hard guardrails (max_new_false_yields /
    # max_not_scorable / require_hold|yield_fixture). Loaded and evaluated here,
    # then attached so the text/JSON/HTML surfaces all render the same result.
    policy_result = None
    if getattr(args, "policy", None):
        policy = _verify.load_policy(args.policy)
        policy_result = _verify.evaluate_policy(result, policy)
        result["policy"] = policy_result
    if args.out:
        # Dispatch on the requested file's extension: a .html/.htm path writes the
        # self-contained offline proof page, anything else keeps the long-standing
        # behaviour of writing the full proof JSON.
        if args.out.lower().endswith((".html", ".htm")):
            _atomic_write_text(args.out, _verify.render_html(result))
            print(f"wrote verify report to {args.out}", file=sys.stderr)
        else:
            _atomic_write_json(args.out, result)
            print(f"wrote verify proof to {args.out}", file=sys.stderr)
    if args.format == "json":
        print(_errors.safe_json_dumps(result, indent=2))
    else:
        print(_verify.render_text(result))
    # Exit non-zero when the run failed a gate the user opted into:
    #  * --policy: any guardrail violated or any target unmet (the anti-bandaid
    #    gate -- you cannot pass by moving one axis while regressing the other);
    #  * --fail-on-regression: any fixture regressed or got worse.
    # Absent both, verify measures and exits 0 (a low-n claim is refused, not
    # failed).
    if policy_result is not None and not policy_result["passed"]:
        return 1
    if args.fail_on_regression and result["regressions"]:
        return 1
    return 0


def _cmd_fix_trial(args) -> int:
    from . import apply as _apply
    from . import fix_trial as _fix_trial
    from . import verify as _verify

    # Load the patch artifact exactly like `hotato apply` does: a missing
    # file, malformed JSON, or a non-patch document all surface as the clean
    # exit-2 usage error via build_apply's own validation.
    with _open_regular(args.patch_json, "r", encoding="utf-8") as fh:
        patch = json.load(fh)
    plan = _apply.load_referenced_plan(patch, args.patch_json)

    policy = _verify.load_policy(args.policy) if args.policy else None

    result = _fix_trial.run_trial(
        patch,
        name=args.name,
        before=args.before,
        after=args.after,
        battery=args.battery,
        contracts=args.contracts,
        policy=policy,
        min_n=args.min_n,
        patch_source=args.patch_json,
        plan=plan,
    )
    if args.html:
        _atomic_write_text(args.html, _fix_trial.render_html(result))
        print(f"wrote {args.html}", file=sys.stderr)
    if args.out:
        _atomic_write_json(args.out, result)
        print(f"wrote fix-trial proof to {args.out}", file=sys.stderr)
    if args.format == "json":
        print(_errors.safe_json_dumps(result, indent=2))
    else:
        print(_fix_trial.render_text(result))
    return result["exit_code"]


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
        print(_errors.safe_json_dumps(result, indent=2))
    else:
        print(_loop.render_text(result))
    return code


# --- investigate: one call-id -> ranked candidates + the label commands ---

def _cmd_investigate(args) -> int:
    from . import investigate as _investigate

    result, code = _investigate.run_investigate(
        args.source,
        stack=args.stack,
        call_id=args.call_id,
        api_key=args.api_key,
        account_sid=args.account_sid,
        auth_token=args.auth_token,
        model_id=args.model_id,
        agent_id=args.agent_id,
        base_url=args.base_url,
        allow_mono=args.allow_mono,
        caller_channel=args.caller_channel,
        agent_channel=args.agent_channel,
        min_gap=args.min_gap,
        top=args.top,
        state_path=args.state,
        channel_map_confirmed=args.confirm_channels,
    )
    if args.format == "json":
        print(_errors.safe_json_dumps(result, indent=2))
    else:
        print(_investigate.render_text(result))
    return code


def _cmd_investigate_label(args) -> int:
    from . import investigate as _investigate

    result = _investigate.run_investigate_label(
        args.ref,
        expect=args.expect,
        contract_id=args.id,
        out_dir=args.out,
        folder=args.folder,
        stack=args.stack,
        rationale=args.rationale,
        max_talk_over_sec=args.max_talk_over,
        max_time_to_yield_sec=args.max_time_to_yield,
        pre_sec=args.pre,
        post_sec=args.post,
        no_clip=args.no_clip,
        force=args.force,
        caller_channel=args.caller_channel,
        agent_channel=args.agent_channel,
        include_identifiers=args.include_identifiers,
        confirm_channels=args.confirm_channels,
        reviewer=args.reviewer,
    )
    if args.format == "json":
        print(_errors.safe_json_dumps(
            _investigate.label_result_json(result), indent=2))
    else:
        print(_investigate.render_label_text(result))
    return 0


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
        print(_errors.safe_json_dumps(_fixture.result_json(result), indent=2))
    else:
        print(_fixture.render_text(result))
    return 0


def _cmd_fixture_promote(args) -> int:
    from . import fixture as _fixture

    result = _fixture.promote_candidate(
        args.ref,
        expect=args.expect,
        fixture_id=args.id,
        out_dir=args.out,
        folder=args.folder,
        title=args.title,
        stack=args.stack,
        max_talk_over_sec=args.max_talk_over,
        max_time_to_yield_sec=args.max_time_to_yield,
        tags=args.tags,
        pre_sec=args.pre,
        post_sec=args.post,
        no_clip=args.no_clip,
        force=args.force,
        caller_channel=args.caller_channel,
        agent_channel=args.agent_channel,
    )
    if args.format == "json":
        print(_errors.safe_json_dumps(
            _fixture.promote_result_json(result), indent=2))
    else:
        print(_fixture.render_promote_text(result))
    return 0


# --- fleet: the local Guardian control plane over the evidence kernel ------
#
# `hotato fleet ...` exposes the already-built domain API (hotato.fleet.api.
# FleetAPI) as an umbrella CLI, mirroring the `contract` group: a nested
# subparser (dest="fleet_command") whose leaves each set `func=_cmd_fleet_*`.
# Every leaf carries --home / --workspace|-w / --format via `_fleet_common`, and
# every handler runs its work in try/finally with `api.close()` so the SQLite
# connection is always released. Live routing (clone/canary) is NOT implemented
# here: canary is recommendation-only and exits non-zero.

_FLEET_CANARY_MSG = (
    "not enabled in this release: canary routing requires a connected stack "
    "with credentials and a tested rollback; this build recommends only."
)


def _fleet_open(args):
    """Instantiate a FleetAPI at the resolved home (--home override, else the
    real DEFAULT_HOME ~/.hotato/fleet)."""
    from .fleet.api import FleetAPI
    from .fleet.registry import DEFAULT_HOME

    return FleetAPI(home=args.home or DEFAULT_HOME)


def _fleet_emit(args, payload, text_lines):
    """Shared json/text output idiom (mirrors the contract handlers): --format
    json prints the raw dict/list, text prints the pre-rendered lines."""
    if getattr(args, "format", "text") == "json":
        print(_errors.safe_json_dumps(payload, indent=2))
    else:
        for line in text_lines:
            print(line)


def _fleet_load_run_json(directory):
    """A before/after trial arg is a DIRECTORY holding run.json (the suite
    envelope) plus its wavs; load the envelope."""
    with _open_regular(os.path.join(directory, "run.json"), "r", encoding="utf-8") as fh:
        return json.load(fh)


def _cmd_fleet_init(args) -> int:
    api = _fleet_open(args)
    try:
        res = api.init_workspace(args.workspace, name=args.name)
        _fleet_emit(args, res, [
            f"workspace: {res['workspace_id']}",
            f"home:      {res['home']}",
            f"mode:      {res['mode']}",
        ])
        return 0
    finally:
        api.close()


def _cmd_fleet_agent_add(args) -> int:
    api = _fleet_open(args)
    try:
        res = api.agent_add(
            args.workspace, args.agent_id, stack=args.stack,
            connection_id=args.connection, external_ref=args.external_ref,
        )
        _fleet_emit(args, res, [
            f"registered agent {res['agent_id']} (stack {res['stack']}) "
            f"in workspace {res['workspace_id']}",
        ])
        return 0
    finally:
        api.close()


def _cmd_fleet_agent_list(args) -> int:
    api = _fleet_open(args)
    try:
        agents = api.agent_list(args.workspace)
        if args.format == "json":
            print(_errors.safe_json_dumps(agents, indent=2))
        elif not agents:
            print("no agents registered")
        else:
            print(f"{'AGENT_ID':<28} {'STACK':<10} EXTERNAL_REF")
            for a in agents:
                print(f"{a['agent_id']:<28} {(a.get('stack') or ''):<10} "
                      f"{a.get('external_ref') or '-'}")
        return 0
    finally:
        api.close()


def _cmd_fleet_ingest(args) -> int:
    api = _fleet_open(args)
    try:
        res = api.ingest_recording(args.workspace, args.agent, args.call_wav,
                                   call_id=args.call_id)
        lines = [f"call_id:      {res['call_id']}",
                 f"deduped:      {res['deduped']}"]
        if not res.get("deduped"):
            lines.append(f"recording_id: {res['recording_id']}")
        _fleet_emit(args, res, lines)
        return 0
    finally:
        api.close()


def _cmd_fleet_discover(args) -> int:
    api = _fleet_open(args)
    try:
        res = api.discover(args.workspace, args.agent, args.call_wav)
        if not res.get("scorable"):
            if args.format == "json":
                print(_errors.safe_json_dumps(res, indent=2))
            else:
                print("not scorable")
                print(res.get("recommendation") or "recording is not scorable")
            return 2
        lines = [f"scorable: yes ({res.get('input_health')})",
                 f"candidates: {len(res['candidates'])}"]
        for c in res["candidates"]:
            lines.append(f"  {c['candidate_id']}  onset={c.get('onset_sec')}  "
                         f"severity={c.get('severity')}")
        _fleet_emit(args, res, lines)
        return 0
    finally:
        api.close()


def _cmd_fleet_review(args) -> int:
    api = _fleet_open(args)
    try:
        q = api.review_queue(args.workspace, agent_id=args.agent, limit=args.limit)
        if args.format == "json":
            print(_errors.safe_json_dumps(q, indent=2))
        elif not q:
            print("review queue is empty")
        else:
            print(f"{len(q)} candidate(s) awaiting review:")
            for c in q:
                print(f"  {c['candidate_id']}  agent={c.get('agent_id')}  "
                      f"onset={c.get('onset_sec')}  severity={c.get('severity')}  "
                      f"cluster={c.get('cluster')}")
        return 0
    finally:
        api.close()


def _cmd_fleet_label(args) -> int:
    api = _fleet_open(args)
    try:
        res = api.label(args.workspace, args.candidate_id, decision=args.decision,
                        reviewer=args.reviewer, rationale=args.rationale)
        _fleet_emit(args, res, [
            f"labeled {res['candidate_id']} as {res['decision']} -> status "
            f"{res['status']} (label {res['label_id']})",
        ])
        return 0
    finally:
        api.close()


def _cmd_fleet_status(args) -> int:
    api = _fleet_open(args)
    try:
        res = api.status(args.workspace)
        counts = res.get("counts", {})
        jobs = res.get("jobs", {})
        lines = [f"workspace: {res['workspace_id']}  mode: {res['mode']}",
                 f"home:      {res['home']}", "counts:"]
        for k in sorted(counts):
            lines.append(f"  {k:<12} {counts[k]}")
        lines.append("jobs:")
        if jobs:
            for k in sorted(jobs):
                lines.append(f"  {k:<12} {jobs[k]}")
        else:
            lines.append("  (none)")
        _fleet_emit(args, res, lines)
        return 0
    finally:
        api.close()


def _cmd_fleet_benchmark(args) -> int:
    api = _fleet_open(args)
    try:
        res = api.benchmark(args.workspace,
                            min_evidence_tier=getattr(args, "min_tier", None))
        lines = [f"workspace: {res['workspace_id']}  scope: {res['scope']}"]
        if getattr(args, "min_tier", None) is not None:
            lines.append(f"evidence-tier floor: {args.min_tier}")
        lines.append("agents (ranked by paired-or-better trials):")
        for r in res["agents"]:
            lines.append(
                f"  {r['agent_id']:<16} {r['stack']:<8} trials={r['trials']} "
                f"improved={r['improved']} paired+={r['paired_or_better']} "
                f"refused={r['refused']} contracts={r['contracts']}"
                f"(hs {r['high_stakes_contracts']})")
        if not res["agents"]:
            lines.append("  (no agents registered)")
        _fleet_emit(args, res, lines)
        return 0
    finally:
        api.close()


def _cmd_fleet_experiment_create(args) -> int:
    api = _fleet_open(args)
    try:
        with _open_regular(args.battery, "r", encoding="utf-8") as fh:
            battery_env = json.load(fh)
        policy = None
        if args.policy:
            with _open_regular(args.policy, "r", encoding="utf-8") as fh:
                policy = json.load(fh)
        res = api.experiment_create(
            args.workspace, args.agent, trial_id=args.trial_id,
            battery_env=battery_env, policy=policy, min_n=args.min_n)
        lines = [f"trial:          {res['trial_id']}",
                 f"manifest:       {res['manifest_digest']}",
                 f"fixtures:       {len(res['fixtures'])} pinned (before any capture)",
                 f"next:           {res['next']}"]
        _fleet_emit(args, res, lines)
        return 0
    finally:
        api.close()


def _cmd_fleet_experiment_run(args) -> int:
    api = _fleet_open(args)
    try:
        manifest_ref = getattr(args, "manifest", None)
        battery_env = None
        if not manifest_ref:
            if not args.battery:
                raise ValueError(
                    "hotato fleet experiment run needs --battery, or --manifest "
                    "from a prior `hotato fleet experiment create`")
            with _open_regular(args.battery, "r", encoding="utf-8") as fh:
                battery_env = json.load(fh)
        before_env = _fleet_load_run_json(args.before)
        after_env = _fleet_load_run_json(args.after)
        policy = None
        if args.policy:
            with _open_regular(args.policy, "r", encoding="utf-8") as fh:
                policy = json.load(fh)
        res = api.experiment_run(
            args.workspace, args.agent, trial_id=args.trial_id,
            battery_env=battery_env, before_env=before_env, before_dir=args.before,
            after_env=after_env, after_dir=args.after, policy=policy, min_n=args.min_n,
            manifest_ref=manifest_ref,
        )
        lines = [f"trial:          {res['trial_id']}",
                 f"verdict:        {res['verdict']}",
                 f"evidence_tier:  {res['evidence_tier']}",
                 f"recommendation: {res['recommendation']}"]
        if res.get("refusal"):
            lines.append(f"refusal:        {res['refusal'].get('reason')}")
        _fleet_emit(args, res, lines)
        return 0 if res["verdict"] == "improved" else 1
    finally:
        api.close()


def _cmd_fleet_run(args) -> int:
    from . import notify as _notify

    # Validate --notify URLs BEFORE the run (same discipline as sweep's
    # --min-gap / channel validation): a bad scheme is an immediate exit-2
    # usage error, never a surprise after ingest+discover already ran.
    notify_urls = _notify.validate_notify_urls(getattr(args, "notify", None))
    api = _fleet_open(args)
    try:
        res = api.run(args.workspace, args.agent, recordings=list(args.recordings or []),
                      caller_channel=args.caller_channel, agent_channel=args.agent_channel)
        lines = [f"agent:          {res['agent_id']}",
                 f"ingested:       {len(res['ingested'])} recording(s)",
                 f"clusters:       {res['clusters']}",
                 f"top candidates: {len(res['top_candidates'])} (hotato fleet review)"]
        _fleet_emit(args, res, lines)
        if notify_urls:
            payload = _notify.fleet_run_payload(
                workspace_id=args.workspace, agent_id=args.agent, res=res,
                home=api.home)
            _notify.notify_all(notify_urls, payload)
        return 0
    finally:
        api.close()


def _cmd_fleet_contract_create(args) -> int:
    api = _fleet_open(args)
    try:
        res = api.contract_from_candidate(
            args.workspace, args.from_candidate, reviewer=args.reviewer,
            decision=args.decision, high_stakes=args.high_stakes,
            max_talk_over_sec=args.max_talk_over_sec,
            max_time_to_yield_sec=args.max_time_to_yield_sec, rationale=args.rationale)
        _fleet_emit(args, res, [f"contract:    {res['contract_id']}",
                                f"decision:    {res['decision']}",
                                f"high_stakes: {res['high_stakes']}",
                                f"dir:         {res['dir']}"])
        return 0
    finally:
        api.close()


def _cmd_fleet_experiment_propose(args) -> int:
    api = _fleet_open(args)
    try:
        cfg = None
        if args.current_config:
            with _open_regular(args.current_config, "r", encoding="utf-8") as fh:
                cfg = json.load(fh)
        res = api.experiment_propose(args.workspace, args.agent, intent=args.intent,
                                     current_config=cfg, max_variants=args.max_variants)
        lines = [f"agent:    {res['agent_id']}", f"stack:    {res['stack']}",
                 f"intent:   {res['intent']}", f"variants: {res['count']}"]
        for v in res["variants"]:
            lines.append(f"  - {v['variant_id']} ({v.get('kind')})")
        _fleet_emit(args, res, lines)
        return 0
    finally:
        api.close()


def _cmd_fleet_experiment_approve(args) -> int:
    api = _fleet_open(args)
    try:
        res = api.approve_trial(args.workspace, args.trial_id, approver=args.approver,
                                note=args.note)
        _fleet_emit(args, res, [f"trial:    {res['trial_id']}",
                                f"approved: {res['approved']} by {res['approver']}",
                                f"note:     {res['note']}"])
        return 0
    finally:
        api.close()


def _cmd_fleet_retention(args) -> int:
    api = _fleet_open(args)
    try:
        res = api.set_retention(
            args.workspace, args.recording_id, consent_basis=args.consent_basis,
            allowed_purposes=list(args.purpose or []), retention_days=args.retention_days,
            pii_class=args.pii_class, legal_hold=args.legal_hold)
        _fleet_emit(args, res, [f"recording: {res['recording_id']}",
                                f"policy:    {json.dumps(res['policy'])}"])
        return 0
    finally:
        api.close()


def _cmd_fleet_delete(args) -> int:
    api = _fleet_open(args)
    try:
        res = api.delete_recording(args.workspace, args.recording_id, reason=args.reason,
                                   actor=args.actor)
        if res.get("deleted"):
            _fleet_emit(args, res, [f"recording: {res['recording_id']} DELETED",
                                    f"receipt:   {res['receipt']['receipt_digest']}"])
            return 0
        _fleet_emit(args, res, [f"recording: {res['recording_id']} NOT deleted "
                                "(blocked by legal hold)"])
        return 1
    finally:
        api.close()


def _cmd_fleet_redact(args) -> int:
    api = _fleet_open(args)
    try:
        spans = []
        for sp in (args.span or []):
            a, b = sp.split(":")
            spans.append((float(a), float(b)))
        res = api.redact_recording(args.workspace, args.recording_id, spans, actor=args.actor)
        _fleet_emit(args, res, [f"parent:  {res['parent_recording_id']}",
                                f"derived: {res.get('derived_digest')}",
                                "note:    DERIVED redacted copy; not the original evidence"])
        return 0
    finally:
        api.close()


def _cmd_synth(args) -> int:
    from . import synth as _synth
    res = _synth.synth_battery(args.source, args.out, seed=args.seed)
    payload = {"tool": "hotato", "kind": "synth-battery", "source": args.source,
               "out": args.out, "axis": "synthetic", "count": len(res), "items": res}
    if getattr(args, "format", "text") == "json":
        print(_errors.safe_json_dumps(payload, indent=2))
    else:
        print(f"synth: {len(res)} synthetic-derived clip(s) from {args.source} -> "
              f"{args.out}")
        print("  kept as a SEPARATE 'synthetic' axis; never blended with real-call "
              "evidence.")
    return 0


def _cmd_fleet_canary(args) -> int:
    # Deliberately does NOT touch a live stack: routing/rollback need connected
    # credentials and a tested rollback path, absent in this release.
    payload = {"enabled": False, "action": args.fleet_canary_command,
               "message": _FLEET_CANARY_MSG}
    if getattr(args, "format", "text") == "json":
        print(_errors.safe_json_dumps(payload, indent=2))
    else:
        print(_FLEET_CANARY_MSG)
    return 2


def _cmd_fleet_export(args) -> int:
    api = _fleet_open(args)
    try:
        status = api.status(args.workspace)
        agents = api.agent_list(args.workspace)
        trials = api.registry._all(
            "SELECT trial_id, agent_id, verdict, evidence_tier FROM trials "
            "WHERE workspace_id=? ORDER BY created_at", (args.workspace,))
        manifest = {"workspace_id": args.workspace, "home": api.home,
                    "status": status, "agents": agents, "trials": trials}
        if args.out:
            os.makedirs(args.out, exist_ok=True)
            path = os.path.join(args.out, "fleet-export.json")
            _atomic_write_json(path, manifest)
            if args.format == "json":
                print(_errors.safe_json_dumps({"out": path, **manifest}, indent=2))
            else:
                print(f"wrote {path}")
        else:
            print(_errors.safe_json_dumps(manifest, indent=2))
        return 0
    finally:
        api.close()


def _cmd_fleet_trend(args) -> int:
    from .fleet import trend as _trend

    api = _fleet_open(args)
    try:
        data = _trend.collect(api.registry, args.workspace)
        html = _trend.build_trend_html(data)
        out = args.out or _trend.DEFAULT_OUT
        _atomic_write_text(out, html)
        agents = data["agents"]
        total_candidates = sum(a["candidates_total"] for a in agents)
        total_trials = sum(a["trials_total"] for a in agents)
        line = (f"wrote {out}: {len(agents)} agent(s), {total_candidates} "
                f"candidate moment(s), {total_trials} experiment trial(s) trended")
        if args.format == "json":
            print(_errors.safe_json_dumps({"out": out, **data}, indent=2))
        else:
            print(line)
        return 0
    finally:
        api.close()


def _cmd_contract_create(args) -> int:
    from . import contract as _contract

    result = _contract.create_contract(
        from_candidate=args.from_candidate,
        stereo=args.stereo,
        caller=args.caller,
        agent=args.agent,
        mono=args.mono,
        diarize=args.diarize,
        diarizer=args.diarizer,
        caller_speaker=args.caller_speaker,
        agent_speaker=args.agent_speaker,
        egress_opt_in=args.egress_opt_in,
        contract_id=args.id,
        expect=args.expect,
        out_dir=args.out,
        onset_sec=args.onset,
        folder=args.folder,
        stack=args.stack,
        max_talk_over_sec=args.max_talk_over,
        max_time_to_yield_sec=args.max_time_to_yield,
        rationale=args.rationale,
        pre_sec=args.pre,
        post_sec=args.post,
        no_clip=args.no_clip,
        force=args.force,
        caller_channel=args.caller_channel,
        agent_channel=args.agent_channel,
        include_identifiers=args.include_identifiers,
        confirm_channels=args.confirm_channels,
    )
    if args.format == "json":
        print(_errors.safe_json_dumps(_contract.create_result_json(result), indent=2))
    else:
        print(_contract.render_create_text(result))
    return 0


def _cmd_contract_verify(args) -> int:
    from . import contract as _contract

    v = _contract.verify_contracts(
        args.dir, transcript_path=getattr(args, "transcript", None),
    )
    if args.html:
        _atomic_write_text(args.html, _contract.render_verify_html(v))
        print(f"wrote {args.html}", file=sys.stderr)
    if args.junit:
        _atomic_write_text(args.junit, _contract.render_verify_junit(v))
        print(f"wrote {args.junit}", file=sys.stderr)
    if args.format == "json":
        print(_errors.safe_json_dumps(_contract.verify_result_json(v), indent=2))
    else:
        print(_contract.render_verify_text(v))
    return v["exit_code"]


def _cmd_contract_inspect(args) -> int:
    from . import contract as _contract

    contract = _contract.inspect_contract(args.path)
    if args.format == "json":
        print(_errors.safe_json_dumps(contract, indent=2))
    else:
        print(_contract.render_inspect_text(contract))
    return 0


def _cmd_contract_pack(args) -> int:
    from . import contract as _contract

    result = _contract.pack_contract(args.bundle, out_path=args.out, force=args.force)
    if args.format == "json":
        print(_errors.safe_json_dumps(_contract.pack_result_json(result), indent=2))
    else:
        print(_contract.render_pack_text(result))
    return 0


def _cmd_contract_unpack(args) -> int:
    from . import contract as _contract

    result = _contract.unpack_contract(
        args.archive, args.out, force=args.force, max_bytes=args.max_bytes,
    )
    if args.format == "json":
        print(_errors.safe_json_dumps(_contract.unpack_result_json(result), indent=2))
    else:
        print(_contract.render_unpack_text(result))
    return 0


def _cmd_trace_ingest(args) -> int:
    from . import trace as _trace

    result = _trace.ingest_otel(
        args.otel, out_path=args.out, call_id=args.call_id, stack=args.stack,
        agent_id=args.agent_id, git_sha=args.git_sha,
        config_hash=args.config_hash,
        include_identifiers=args.include_identifiers,
        include_text=args.include_text, force=args.force,
    )
    if args.format == "json":
        print(_errors.safe_json_dumps(_trace.ingest_result_json(result), indent=2))
    else:
        print(_trace.render_ingest_text(result))
    return 0


def _cmd_trace_attach(args) -> int:
    from . import trace as _trace

    result = _trace.attach_trace(args.bundle, args.trace, force=args.force)
    if args.format == "json":
        print(_errors.safe_json_dumps(_trace.attach_result_json(result), indent=2))
    else:
        print(_trace.render_attach_text(result))
    return 0


def _cmd_trace_export(args) -> int:
    from . import trace as _trace

    result = _trace.export_trace(
        args.bundle, out_path=args.out, fmt=args.format, force=args.force,
    )
    if args.json:
        print(_errors.safe_json_dumps(_trace.export_result_json(result), indent=2))
    else:
        print(_trace.render_export_text(result))
    return 0


def _cmd_assert_init(args) -> int:
    from . import assert_ as A

    spans = A.load_spans_file(args.from_trace)
    timing = None
    if args.stereo:
        env = run_single(stereo=args.stereo)
        timing = env["events"]
    result = A.build_init_stub(spans, timing=timing, source_trace=args.from_trace)

    if os.path.exists(args.out) and not args.force:
        raise ValueError(
            f"{args.out!r} already exists; pass --force to overwrite it, or "
            "choose a new --out"
        )
    _atomic_write_text(args.out, result["yaml"])

    if args.format == "json":
        payload = {
            "tool": _errors.TOOL, "schema_version": _errors.SCHEMA_VERSION,
            "kind": "assert-init", "path": args.out,
            "tool_names": result["tool_names"],
            "skipped_tool_names": result["skipped_tool_names"],
            "used_timing": result["used_timing"],
        }
        print(_errors.safe_json_dumps(payload, indent=2))
    else:
        lines = [f"wrote a starter assertions file: {args.out}"]
        if result["tool_names"]:
            lines.append(f"  tool_call checks: {', '.join(result['tool_names'])}")
        if len(result["tool_names"]) >= 2:
            lines.append(f"  call-order check: {' -> '.join(result['tool_names'])}")
        if result["skipped_tool_names"]:
            lines.append(
                "  skipped (unsafe characters, add by hand if needed): "
                + ", ".join(result["skipped_tool_names"])
            )
        if result["used_timing"]:
            lines.append("  timing check: verdict.did_yield (from --stereo)")
        next_cmd = f"hotato assert run --assertions {args.out} --trace {args.from_trace}"
        if args.stereo:
            next_cmd += f" --stereo {args.stereo}"
        lines.append(f"next: {next_cmd}")
        print("\n".join(lines))
    return 0


def _cmd_assert_run(args) -> int:
    from . import assert_ as A

    if args.transcribe and args.transcript:
        raise ValueError(
            "pass either --transcript FILE or --transcribe (with --stereo), "
            "not both"
        )
    if args.transcribe and not args.stereo:
        raise ValueError("--transcribe needs --stereo FILE to run ASR over")

    transcript = None
    transcript_path = None
    if args.transcript:
        transcript_path = args.transcript
    elif args.transcribe:
        from .transcribe import transcribe as _transcribe

        t = _transcribe(
            args.stereo, model=args.transcribe_model, device=args.transcribe_device,
        )
        transcript = [
            {"role": None, "text": seg.text, "start": seg.start, "end": seg.end}
            for seg in t.segments
        ]

    timing = None
    if args.stereo:
        env = run_single(stereo=args.stereo)
        timing = env["events"]

    ctx = A.build_context(
        transcript=transcript,
        transcript_path=transcript_path,
        trace_path=args.trace,
        timing=timing,
    )
    env_out = A.run_assertions_from_file(
        args.assertions, ctx,
        inconclusive_policy=getattr(args, "inconclusive_policy", None),
    )
    if args.format == "json":
        print(_errors.safe_json_dumps(env_out, indent=2))
    else:
        print(A.render_run_text(env_out), end="")
    return env_out["exit_code"]


def _load_state_adapter(path: str):
    """A :class:`hotato.state_adapter.MockStateAdapter` from ``--state``: a
    SQLite file by extension (``.db``/``.sqlite``/``.sqlite3``), else a JSON
    sandbox. The post-call system of record the ``state``/``state_change``
    (Authority 2) kinds query; a query is a plain lookup, no model/network."""
    from .state_adapter import MockStateAdapter

    low = path.lower()
    if low.endswith((".db", ".sqlite", ".sqlite3")):
        return MockStateAdapter.from_sqlite_file(path)
    return MockStateAdapter.from_json_file(path)


def _cmd_test_run(args) -> int:
    from . import assert_ as A
    from . import conversation_test as CT
    from . import test_run as TR

    fmt = args.format
    audio = args.audio or []
    if len(audio) > 2:
        raise ValueError(
            "--audio takes ONE dual-channel recording, or TWO mono files "
            "(caller agent)"
        )
    # html/md render the unified TIMING report, which needs a recording to score
    # and a directory to write into; refuse cleanly rather than emit a page with
    # no timeline. json/text need neither.
    if fmt in ("html", "md"):
        if not args.out:
            raise ValueError(
                f"--format {fmt} writes report.{fmt} into --out DIR; pass --out"
            )
        if not audio:
            raise ValueError(
                f"--format {fmt} renders the timing report, which needs --audio "
                "(a recording to score); pass --audio, or use --format json/text"
            )

    doc = CT.load_conversation_test_file(args.test_file)

    # Score the supplied recording for the timing context (and the report's
    # timeline). No audio -> timing stays None -> timing-reading assertions are
    # INCONCLUSIVE (missing input), never guessed.
    timing = None
    stereo = caller = agent = None
    if len(audio) == 1:
        stereo = audio[0]
        timing = run_single(stereo=stereo)["events"]
    elif len(audio) == 2:
        caller, agent = audio
        timing = run_single(caller=caller, agent=agent)["events"]

    state_adapter = _load_state_adapter(args.state) if args.state else None

    ctx = A.build_context(
        transcript_path=args.transcript,
        trace_path=args.trace,
        timing=timing,
        state_adapter=state_adapter,
    )

    result = TR.evaluate_conversation_test(
        doc, ctx, agent_id=args.agent, repetitions=args.repetitions,
    )

    manifest = None
    if args.out:
        import datetime as _dt

        created_at = args.created_at or _dt.datetime.now(
            _dt.timezone.utc
        ).strftime("%Y-%m-%dT%H:%M:%SZ")
        # The manifest's single 'audio' slot binds a dual-channel recording; two
        # mono files score but are not partially bound (an incomplete audio child
        # would misrepresent the evidence).
        audio_bind = audio[0] if len(audio) == 1 else None
        manifest = TR.assemble_conversation_artifact(
            args.out,
            conversation_id=doc["id"],
            agent_id=args.agent,
            origin=TR._origin_from_doc(doc),
            created_at=created_at,
            assertions_env=result["assertions"],
            audio_path=audio_bind,
            transcript_path=args.transcript,
            trace_path=args.trace,
            timing=timing,
        )
        result["conversation"] = manifest

    report_path = None
    if fmt in ("html", "md"):
        from . import report as _report
        from . import trace as _trace

        trace_obj = _trace.load_voice_trace_jsonl(args.trace) if args.trace else None
        build = _report.build_report_html if fmt == "html" else _report.build_report_md
        page, _env = build(
            stereo=stereo, caller=caller, agent=agent,
            trace=trace_obj, assertions=result["assertions"],
            conversation=manifest,
        )
        report_path = os.path.join(args.out, f"report.{fmt}")
        _atomic_write_text(report_path, page)

    if fmt == "json":
        print(_errors.safe_json_dumps(result, indent=2))
    else:
        print(TR.render_summary_text(result), end="")
        if args.out:
            print(f"wrote conversation artifact to {args.out}/", file=sys.stderr)
        if report_path:
            print(f"wrote report to {report_path}", file=sys.stderr)
    return result["exit_code"]


def _cmd_scenario_init(args) -> int:
    from . import conversation_test as CT

    name = args.name or "example-scenario"
    text = CT.build_scenario_starter(name, agent=args.agent)
    if os.path.exists(args.out) and not args.force:
        raise ValueError(
            f"{args.out!r} already exists; pass --force to overwrite it, or "
            "choose a new --out"
        )
    _atomic_write_text(args.out, text)
    if args.format == "json":
        payload = {
            "tool": _errors.TOOL, "schema_version": _errors.SCHEMA_VERSION,
            "kind": "scenario-init", "path": args.out, "scenario_id": name,
            "agent": args.agent,
        }
        print(_errors.safe_json_dumps(payload, indent=2))
    else:
        print(f"wrote a starter conversation-test file: {args.out}")
        print(
            f"next: hotato test run {args.out} --agent {args.agent} \\\n"
            "        --audio call.wav --trace voice_trace.jsonl \\\n"
            "        --transcript call.transcript.json --out ./conv-artifact"
        )
    return 0


def _cmd_scenario_validate(args) -> int:
    from . import conversation_test as CT

    path = args.path
    if os.path.isdir(path):
        files = sorted(
            os.path.join(path, f)
            for f in os.listdir(path)
            if f.endswith((".yaml", ".yml", ".json"))
        )
        if not files:
            raise ValueError(
                f"{path!r} contains no conversation-test files "
                "(*.yaml / *.yml / *.json)"
            )
    else:
        files = [path]

    results = []
    all_ok = True
    for f in files:
        try:
            doc = CT.load_conversation_test_file(f)
            results.append(
                {"path": f, "ok": True, "id": doc["id"], "agent": doc["agent"]}
            )
        except (ValueError, OSError) as exc:
            all_ok = False
            results.append({"path": f, "ok": False, "error": str(exc)})

    if args.format == "json":
        print(_errors.safe_json_dumps(
            {"tool": _errors.TOOL, "schema_version": _errors.SCHEMA_VERSION,
             "kind": "scenario-validate", "ok": all_ok, "results": results},
            indent=2,
        ))
    else:
        for r in results:
            if r["ok"]:
                print(f"OK    {r['path']}  (id={r['id']}, agent={r['agent']})")
            else:
                print(f"BAD   {r['path']}  -- {r['error']}")
        print(f"{sum(1 for r in results if r['ok'])}/{len(results)} valid")
    # exit 2 (unusable input) when any file is malformed -- mirrors the CLI's
    # usage-error convention and `assert`'s up-front validation posture.
    return 0 if all_ok else 2


def _render_conversation_verify_text(v: dict) -> str:
    head = "VERIFIED" if v["ok"] else "REFUSED"
    lines = [f"conversation {v['conversation_id']}: {head}"]
    if v["verified"]:
        lines.append(f"  verified: {', '.join(v['verified'])}")
    for m in v["mismatches"]:
        lines.append(
            f"  MISMATCH {m['artifact']} ({m['path']}): expected "
            f"{m['expected'][:12]}..., got {m['actual'][:12]}..."
        )
    for m in v["missing"]:
        loc = f" ({m['path']})" if m.get("path") else ""
        lines.append(f"  MISSING  {m['artifact']}{loc}: {m.get('reason', '')}")
    lines.append(f"  {v['reason']}")
    return "\n".join(lines) + "\n"


def _cmd_conversation_verify(args) -> int:
    from . import conversation as CV

    verdict = CV.verify(args.dir)
    if args.format == "json":
        print(_errors.safe_json_dumps(verdict, indent=2))
    else:
        print(_render_conversation_verify_text(verdict), end="")
    # A digest mismatch / missing child is a REFUSAL (exit 2): a tampered or
    # absent artifact is refused, never silently accepted.
    return 2 if verdict["refused"] else 0


def _cmd_simulate(args) -> int:
    import datetime as _dt

    from . import scenario as _scn
    from . import simulate as SIM

    # --matrix switches to the parallel scenario-matrix runner. --conversation-test
    # / --parallel are matrix-only; refuse them in single-run mode rather than
    # silently ignore them.
    if args.matrix:
        if args.scenario:
            raise ValueError(
                "pass the scenario to --matrix OR as the positional argument, "
                "not both"
            )
        return _cmd_simulate_matrix(args)
    if args.conversation_test is not None or args.parallel is not None:
        raise ValueError(
            "--conversation-test / --parallel apply to --matrix mode only"
        )
    if not args.scenario:
        raise ValueError(
            "provide a scenario file (positional), or --matrix <scenario.yaml> "
            "to run the whole variation matrix"
        )

    doc = _scn.load_scenario_file(args.scenario)
    # --seed folds into the base seed (so derived per-run seeds shift with it);
    # --repetitions overrides the matrix repetition count (or sets it for a
    # matrix-less scenario). Both leave the transcript byte-stable for a fixed
    # (scenario, seed): a SEEDED REPLAY is byte-identical, never "the model is
    # deterministic".
    if args.seed is not None:
        doc = {**doc, "seed": args.seed}
    if args.repetitions is not None:
        vm = dict(doc.get("variation_matrix") or {})
        vm["repetitions"] = args.repetitions
        doc = {**doc, "variation_matrix": vm}

    runs = SIM.expand(doc)
    # A single run takes the explicit --seed (or the scenario seed) directly, so
    # `simulate s.json --seed 5` uses 5; multi-run expansions keep their
    # deterministic per-variation seeds.
    if len(runs) == 1:
        runs[0]["seed"] = args.seed if args.seed is not None else int(doc.get("seed", 0))

    single = len(runs) == 1
    created_at = args.created_at or _dt.datetime.now(
        _dt.timezone.utc
    ).strftime("%Y-%m-%dT%H:%M:%SZ")

    run_records = []
    verdicts = []
    invalid = 0
    for i, run in enumerate(runs):
        rendered = SIM.render(run["scenario"], run["seed"])
        verdict = SIM.validate_simulation(run["scenario"], rendered)
        verdicts.append(verdict)
        if not verdict["ok"]:
            invalid += 1
        out_dir = None
        conversation_id = None
        if args.out:
            out_dir = args.out if single else os.path.join(
                args.out, f"{rendered['scenario_id']}-{i:03d}")
            manifest = SIM.write_artifact(
                rendered, out_dir, created_at=created_at,
                agent_id=args.agent, conversation_id=None)
            conversation_id = manifest["conversation_id"]
        run_records.append({
            "index": i,
            "seed": run["seed"],
            "variation": run.get("variation"),
            # EVERY produced conversation is labelled origin=simulated -- never
            # real, and never merged into a real bucket.
            "origin_kind": rendered["origin"]["kind"],
            "content_hash": rendered["content_hash"],
            "conversation_id": conversation_id,
            "out": out_dir,
            "simulation": verdict,
        })

    reliab = SIM.reliability([v["ok"] for v in verdicts])
    # SIMULATOR_INVALID is a broken FIXTURE (exit 1), never an agent PASS/FAIL.
    exit_code = 1 if invalid else 0

    payload = {
        "tool": _errors.TOOL, "schema_version": _errors.SCHEMA_VERSION,
        "kind": "simulate", "scenario_id": doc["id"],
        "origin_kind": "simulated", "runs": run_records,
        "reliability": reliab, "invalid_count": invalid,
        "all_simulated": all(r["origin_kind"] == "simulated" for r in run_records),
        "exit_code": exit_code,
    }
    if args.format == "json":
        print(_errors.safe_json_dumps(payload, indent=2))
    else:
        print(_render_simulate_text(payload), end="")
        if args.out:
            print(f"wrote {len(run_records)} simulated artifact(s) under "
                  f"{args.out}/", file=sys.stderr)
    return exit_code


def _render_simulate_text(p: dict) -> str:
    lines = [
        f"hotato simulate: {p['scenario_id']} -- {len(p['runs'])} run(s), "
        "origin=simulated (never real)"
    ]
    for r in p["runs"]:
        sim = r["simulation"]
        mark = "ok" if sim["ok"] else sim["status"]
        loc = f"  -> {r['out']}" if r["out"] else ""
        lines.append(
            f"  run {r['index'] + 1}: seed={r['seed']} "
            f"{r['content_hash'][:12]} sim={mark}{loc}"
        )
        if not sim["ok"]:
            lines.append(f"      {sim['reason']}")
    rel = p["reliability"]
    lines.append(
        f"reliability: pass@1={rel['pass_at_1']:.3f} "
        f"pass@k={rel['pass_at_k']:.3f} pass^k={rel['pass_caret_k']:.3f} "
        f"(n={rel['n']})"
    )
    lines.append(f"  {rel['note']}")
    return "\n".join(lines) + "\n"


def _cmd_simulate_matrix(args) -> int:
    from . import conversation_test as _ct
    from . import scenario as _scn
    from . import simulate as SIM

    doc = _scn.load_scenario_file(args.matrix)
    # --seed folds into the base seed (per-run seeds shift with it); --repetitions
    # overrides the matrix repetition count. Both keep the summary byte-stable for
    # a fixed (scenario, seed): same scenario -> same seeds -> byte-identical.
    if args.seed is not None:
        doc = {**doc, "seed": args.seed}
    if args.repetitions is not None:
        vm = dict(doc.get("variation_matrix") or {})
        vm["repetitions"] = args.repetitions
        doc = {**doc, "variation_matrix": vm}

    ct = (_ct.load_conversation_test_file(args.conversation_test)
          if args.conversation_test else None)

    summary = SIM.run_matrix(
        doc, conversation_test=ct, out_dir=args.out, max_workers=args.parallel,
    )

    if args.format == "json":
        payload = {"tool": _errors.TOOL, "schema_version": _errors.SCHEMA_VERSION,
                   **summary}
        print(_errors.safe_json_dumps(payload, indent=2))
    else:
        print(_render_matrix_text(summary), end="")
        if args.out:
            print(f"wrote {summary['counts']['runs']} simulated artifact(s) "
                  f"under {args.out}/", file=sys.stderr)
    return summary["exit_code"]


def _render_matrix_text(s: dict) -> str:
    c = s["counts"]
    lines = [
        f"hotato simulate --matrix: {s['scenario_id']} -- {c['runs']} run(s) "
        f"across {len(s['variation_cells'])} variation cell(s), "
        "origin=simulated (never real)"
    ]
    if s["scored"]:
        lines.append(
            f"scored against conversation-test {s['conversation_test_id']} "
            f"(inconclusive_policy={s['inconclusive_policy']})"
        )
    else:
        lines.append("no conversation-test scored (simulations only)")
    lines.append(
        f"valid: {c['valid']}  simulator_invalid: {c['simulator_invalid']} "
        "(bucketed separately, never an agent PASS/FAIL)"
    )
    # ATTRIBUTABLE per-variation reliability -- one line per cell, never blended.
    lines.append("per-variation reliability (never blended):")
    for cell in s["variation_cells"]:
        v = cell["cell"]
        rel = cell["reliability"]
        lines.append(
            f"  [{v['locale']} rate={v['speaking_rate']} noise={v['noise']} "
            f"behavior={v['behavior']}] "
            f"pass@1={rel['pass_at_1']:.3f} pass@k={rel['pass_at_k']:.3f} "
            f"pass^k={rel['pass_caret_k']:.3f} (n={rel['n']})"
        )
    rel = s["reliability"]
    lines.append(
        f"scenario reliability [{s['reliability_basis']}]: "
        f"pass@1={rel['pass_at_1']:.3f} pass@k={rel['pass_at_k']:.3f} "
        f"pass^k={rel['pass_caret_k']:.3f} (n={rel['n']})"
    )
    lines.append(f"  {s['reliability_note']}")
    if s["simulator_invalid"]:
        lines.append("SIMULATOR_INVALID (broken fixtures, never agent PASS/FAIL):")
        for r in s["simulator_invalid"]:
            lines.append(f"  {r['run_id']} seed={r['seed']}: {r['reason']}")
    return "\n".join(lines) + "\n"


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
        print(_errors.safe_json_dumps(cmp_env, indent=2))
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
        _atomic_write_json(args.out, result)
        print(
            f"wrote {result['total_candidates']} candidates to {args.out}",
            file=sys.stderr,
        )
    if args.format == "json":
        capped = dict(result)
        if args.top > 0:
            capped["candidates"] = result["candidates"][:args.top]
        capped["shown"] = len(capped["candidates"])
        print(_errors.safe_json_dumps(capped, indent=2))
    else:
        print(_scan.render_text(result, top=args.top))
    return 0


def _cmd_trust(args) -> int:
    from . import trust as _trust

    report = _trust.trust_report(
        args.stereo,
        caller_channel=args.caller_channel,
        agent_channel=args.agent_channel,
        diarize=getattr(args, "diarize", False),
        diarizer=getattr(args, "diarizer", "pyannote"),
        egress_opt_in=getattr(args, "egress_opt_in", False),
    )
    if args.format == "json":
        print(_errors.safe_json_dumps(report, indent=2))
    else:
        print(_trust.render_text(report))
    # 0 when eligible for scan, 2 when not scorable (the report's own exit_code, which
    # matches the CLI's unusable-input convention).
    return int(report["exit_code"])


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
        text = _errors.safe_json_dumps(capped, indent=2)
        if args.out:
            _atomic_write_text(args.out, text + "\n")
            print(f"wrote ranked candidates JSON to {args.out}", file=sys.stderr)
        print(text)
        return 0
    # Default: the self-contained HTML dashboard with the hear-the-bug player.
    out = args.out or "hotato-analyze.html"
    html_str = _analyze.build_dashboard_html(
        aggregate, per_file, top=args.top, audio_top=args.audio_top,
    )
    _atomic_write_text(out, html_str)
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


def _cmd_init_webhook(args) -> int:
    from . import initcmd as _initcmd

    result = _initcmd.scaffold_webhook(
        args.stack, args.target, args.out, force=args.force,
    )
    if args.format == "json":
        print(_errors.safe_json_dumps(result, indent=2))
    else:
        print(_initcmd.render_text(result), end="")
    return 0


def _cmd_init_starter(args) -> int:
    from . import initcmd as _initcmd

    result = _initcmd.scaffold_starter(args.stack, args.out, force=args.force)
    if args.format == "json":
        print(_errors.safe_json_dumps(_initcmd.starter_result_json(result), indent=2))
    else:
        print(_initcmd.render_starter_text(result), end="")
    return 0


def _cmd_issue_create(args) -> int:
    from . import issuecmd as _issue

    # An explicit --repo is required for BOTH the dry run (it names the repo in
    # the rendered command) and the create. Missing it is a clean usage error
    # (exit 2), never a guessed target.
    if not args.repo:
        raise ValueError(
            "--repo OWNER/REPO is required: hotato issue create sweep.json "
            "--repo owner/repo"
        )
    # The SAME parser hotato fixture promote uses (a missing file, a non-JSON
    # file, or a foreign JSON is refused with the honest reason). The promote
    # refs read this file by name, so the ref in the issue resolves exactly
    # like a ref on the command line.
    doc = _issue.load_sweep_result(args.sweep_json)
    report_ref = os.path.basename(args.sweep_json)
    env = _issue.build_issue(
        doc, report_ref=report_ref, repo=args.repo, top=args.top,
        labels=args.label or [],
    )

    if not args.yes:
        # DEFAULT = dry run: print the rendered body and the exact command,
        # create nothing. gh is never invoked on this path.
        if args.format == "json":
            payload = dict(env)
            payload["dry_run"] = True
            payload["created"] = False
            print(_errors.safe_json_dumps(payload, indent=2))
        else:
            print(env["body"])
            print()
            print("Dry run: nothing was created. Re-run with --yes to create "
                  "the issue with this exact command:")
            print(f"  {env['gh_command_display']}")
        return 0

    # --yes + --repo: the one place this shells out. A missing gh binary raises
    # FileNotFoundError -> the standard exit-2 structured error; a non-zero gh
    # exit is surfaced as a clean usage error with gh's own message.
    rc, out, err = _issue.create_via_gh(env["gh_command"], env["body"])
    if rc != 0:
        detail = (err or out).strip()
        raise ValueError(
            f"gh issue create failed (exit {rc})"
            + (f": {detail}" if detail else ".")
        )
    url = out.strip()
    if args.format == "json":
        payload = dict(env)
        payload["dry_run"] = False
        payload["created"] = True
        payload["issue_url"] = url
        print(_errors.safe_json_dumps(payload, indent=2))
    else:
        print(env["body"])
        print()
        print(f"created issue: {url}" if url else "created the issue.")
    return 0


def _cmd_pr_create(args) -> int:
    from . import prcmd as _pr

    # An explicit --repo, --fixtures, and --title are required for BOTH the dry
    # run (they name the repo, the files, and the PR title in the rendered
    # commands) and the create. A missing one is a clean usage error (exit 2),
    # never a guessed value.
    if not args.repo:
        raise ValueError(
            "--repo OWNER/REPO is required: hotato pr create --fixtures "
            "tests/hotato --repo owner/repo --title 'Add turn-taking fixtures'"
        )
    if not args.fixtures:
        raise ValueError(
            "--fixtures DIR is required: point it at the fixtures directory "
            "hotato fixture promote wrote (with scenarios/ and audio/), e.g. "
            "--fixtures tests/hotato"
        )
    if not args.title:
        raise ValueError(
            "--title is required: a short pull request title, e.g. --title "
            "'Add turn-taking regression fixtures'"
        )
    # Filesystem read only (the scenarios off disk); the rendering below is a
    # pure, offline function. A directory that is not a fixtures directory, or a
    # scenario whose audio is missing, is refused with the honest reason.
    fixtures = _pr.load_fixtures(args.fixtures)
    env = _pr.build_pr(
        fixtures, fixtures_dir=args.fixtures, repo=args.repo, title=args.title,
        branch=args.branch, base=args.base,
    )

    if not args.yes:
        # DEFAULT = dry run: print the rendered body and the exact commands,
        # change nothing. Neither git nor gh is invoked on this path.
        if args.format == "json":
            payload = dict(env)
            payload["dry_run"] = True
            payload["created"] = False
            print(_errors.safe_json_dumps(payload, indent=2))
        else:
            print(env["body"])
            print()
            print("Dry run: nothing was created. Re-run with --yes to cut the "
                  "feature branch, commit the fixtures, push, and open the PR "
                  "with these exact commands:")
            for disp in env["git_commands_display"]:
                print(f"  {disp}")
            print(f"  {env['gh_command_display']}")
        return 0

    # --yes + --repo: the one place this shells out. A missing git/gh binary
    # raises FileNotFoundError -> the standard exit-2 structured error; a
    # non-zero git or gh exit is surfaced as a clean usage error with the
    # command's own message. The change lands on a NEW feature branch (never the
    # default branch directly) and the push is never a force-push.
    outcome = _pr.create_via_git_gh(
        env["git_commands"], env["gh_command"], env["body"],
    )
    if not outcome["ok"]:
        detail = (outcome["stderr"] or outcome["stdout"]).strip()
        raise ValueError(
            f"{outcome['failed_command']} failed (exit {outcome['returncode']})"
            + (f": {detail}" if detail else ".")
        )
    url = outcome["pr_url"]
    if args.format == "json":
        payload = dict(env)
        payload["dry_run"] = False
        payload["created"] = True
        payload["pr_url"] = url
        print(_errors.safe_json_dumps(payload, indent=2))
    else:
        print(env["body"])
        print()
        print(f"created pull request: {url}" if url
              else "created the pull request.")
    return 0


_DEMO_HEADER = "hotato demo: recorded calls a provider's default agent fails"
_DEMO_NOTE = ("these are two recorded calls on a provider's default "
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
        print(_errors.safe_json_dumps(manifest, indent=2))
    else:
        print(_render_describe_text(manifest), end="")
    return 0


def _cmd_card(args) -> int:
    from . import card as _card

    svg = _card.make_card(args.input,
                          include_identifiers=args.include_identifiers)
    if args.out:
        _atomic_write_text(args.out, svg)
        print(f"wrote card to {args.out}", file=sys.stderr)
    else:
        sys.stdout.write(svg)
    return 0


def _cmd_start(args) -> int:
    from . import start as _start

    return _start.run_start(
        demo=args.demo, stack=args.stack, folder=args.folder,
        stereo=args.stereo, out_dir=args.dir, fmt=args.format,
        label=getattr(args, "label", None), onset_sec=getattr(args, "onset", None),
        caller_channel=getattr(args, "caller_channel", 0),
        agent_channel=getattr(args, "agent_channel", 1),
        confirm_channels=getattr(args, "confirm_channels", False),
    )


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
    # single-channel (mono) scoring via the opt-in, quality-gated [diarize] front-end
    r.add_argument("--mono", default=None, metavar="WAV",
                   help="single-channel WAV to score by first separating it into "
                        "caller/agent via speaker diarization (requires --diarize). "
                        "The dual-channel --stereo path stays the gold reference.")
    r.add_argument("--diarize", action="store_true",
                   help="separate a --mono recording into caller/agent via speaker "
                        "diarization, then score it. Quality-gated: below the "
                        "confidence bar the verdict is labeled indicative only (no "
                        "SLA gate fires) and a non-separable file is not scorable "
                        "(exit 2). Opt-in [diarize] extra; absent it errors cleanly "
                        "and NEVER scores raw mono.")
    r.add_argument("--diarizer", default="pyannote",
                   choices=["pyannote", "sortformer", "pyannoteai"],
                   help="diarizer backend for --mono: 'pyannote' (local, offline, "
                        "CPU-viable, default), 'sortformer' (local, GPU, best "
                        "self-hostable on phone), 'pyannoteai' (HOSTED, best "
                        "absolute, needs --egress-opt-in)")
    r.add_argument("--caller-speaker", default=None, metavar="LABEL",
                   help="override the caller<-speaker mapping (e.g. SPEAKER_00) "
                        "instead of the floor-dominance proposal")
    r.add_argument("--agent-speaker", default=None, metavar="LABEL",
                   help="override the agent<-speaker mapping (e.g. SPEAKER_01)")
    r.add_argument("--egress-opt-in", action="store_true",
                   help="permit the HOSTED --diarizer pyannoteai to upload your "
                        "audio off this machine (audio leaves this machine); local "
                        "backends never need this")
    # optional, non-reference transcript CONTEXT layer (faster-whisper)
    r.add_argument("--transcribe", action="store_true",
                   help="attach a transcript as CONTEXT next to the score (opt-in "
                        "[transcribe] extra, faster-whisper, fully offline). It NEVER "
                        "changes did_yield / talk_over_sec / time_to_yield / the "
                        "verdict -- the same run without --transcribe is byte-identical "
                        "on timing. Needs a single audio file (--stereo, or --mono "
                        "--diarize); not supported with separate --caller/--agent. "
                        "Absent the extra, errors cleanly and never skips silently.")
    r.add_argument("--transcribe-model", default="base.en", metavar="NAME",
                   help="faster-whisper model name or local path (default base.en)")
    r.add_argument("--transcribe-device", default="auto", choices=["auto", "cpu", "cuda"],
                   help="device for --transcribe (default auto: cuda if available, else cpu)")
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
            "accuracy percentage anywhere. `--demo` runs the same flow over the "
            "two bundled real demo calls with no stack, no credentials and no "
            "network, so the first sweep works before anything is connected."
        ),
        epilog=(
            _exit_codes_epilog("sweep") + "\n\n"
            "Examples:\n"
            "  hotato sweep --demo                            # bundled real calls, zero setup\n"
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
    sw.add_argument("--demo", action="store_true",
                    help="sweep the two bundled real demo calls instead of a "
                         "stack: no credentials, no network, zero setup. Takes "
                         "no stack, credential, or pull flags")
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
    sw.add_argument("--notify", action="append", default=None, metavar="URL",
                    help="POST a JSON summary (counts, top candidate timing, "
                         "local artifact paths -- no audio, no credentials, no "
                         "transcript) to this webhook URL when the sweep "
                         "finishes; repeatable. Off by default; fails open (a "
                         "down webhook never breaks the sweep). "
                         "See docs/EGRESS.md")
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
            "  hotato report --stereo call.wav --trace voice_trace.jsonl --out report.html\n"
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
    rp.add_argument("--trace", default=None, metavar="voice_trace.jsonl",
                    help="a hotato voice trace (hotato trace ingest ... --out "
                         "voice_trace.jsonl) to render as a collapsed 'Trace "
                         "(context, not a score)' section: discrete voice-"
                         "pipeline events (TTS cancel/stop, ASR partials, tool "
                         "calls) shown alongside the timing. Context only, "
                         "never scored; a redacted span shows [redacted].")
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
    t.add_argument("--order", default="name", choices=["name", "mtime"],
                   help="run order for the trend: filename (DEFAULT, "
                        "content-derived; use a numeric prefix as an explicit "
                        "index) or file mtime (filesystem-dependent, not "
                        "reproducible across checkout/extract/rsync)")
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

    st = sub.add_parser(
        "start",
        help="guided, credential-less first run: sweep the bundled demo "
             "calls, create+verify one demo failure contract, write the "
             "result + dashboard + funnel card, and print the exact next "
             "commands",
        description=(
            "The zero-setup first run. `hotato start --demo` sweeps the two "
            "bundled real demo calls (no account, no network, no "
            "credentials), writes the sweep result (hotato-sweep.json), a "
            "self-contained HTML dashboard (hotato-sweep.html), and the "
            "threshold-funnel card (hotato-no-single-threshold.svg); creates "
            "one demo failure contract (contracts/demo-missed-interruption."
            "hotato) from a real missed-interruption candidate with --expect "
            "yield and verifies it immediately (it genuinely fails -- this "
            "is the loop: a real failure becomes a candidate, becomes a "
            "portable contract, and contract verify catches it); then prints "
            "the exact next commands: promote a candidate into a permanent "
            "fixture, run those fixtures in CI, re-verify the demo contract, "
            "and render a card. hotato start --stereo <call.wav> runs the fully-"
            "wired guided own-call flow (trust -> scan -> review -> human label "
            "-> contract + evidence-tier card). --stack/--folder route you to "
            "hotato sweep / analyze."
        ),
        epilog=(
            _exit_codes_epilog("start") + "\n\n"
            "Examples:\n"
            "  hotato start --demo\n"
            "  hotato start --demo --dir ./firstrun --format json"
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    st.add_argument("--demo", action="store_true",
                    help="run the guided demo first run (the only fully-wired "
                         "mode in this build)")
    st.add_argument("--stack", default=None,
                    help="[not yet in this build] route to hotato sweep "
                         "--stack")
    st.add_argument("--folder", default=None,
                    help="[not yet in this build] route to hotato analyze")
    st.add_argument("--stereo", default=None, metavar="CALL_WAV",
                    help="guided own-call flow on a dual-channel recording: trust "
                         "preflight, candidate scan, local review page, (with "
                         "--label) a human-labelled contract + evidence-tier card")
    st.add_argument("--confirm-channels", action="store_true",
                    help="confirm the caller/agent channel mapping when --stereo "
                         "trust flags a possible swap (required to mint a contract "
                         "from a swap-suspect recording)")
    st.add_argument("--dir", default=None, metavar="DIR",
                    help="directory to write the outputs into (default: the "
                         "current directory)")
    st.add_argument("--format", default="text", choices=["text", "json"],
                    help="output format (default text)")
    st.add_argument("--label", default=None, choices=["yield", "hold"],
                    help="(--stereo) your human label for the top candidate; creates a contract")
    st.add_argument("--onset", type=float, default=None,
                    help="(--stereo) pin the caller onset in seconds (else the top candidate)")
    st.add_argument("--caller-channel", type=int, default=0,
                    help="(--stereo) caller channel index")
    st.add_argument("--agent-channel", type=int, default=1,
                    help="(--stereo) agent channel index")
    st.set_defaults(func=_cmd_start)

    cd = sub.add_parser(
        "card",
        help="render a shareable SVG card (1200x630, offline, no external "
             "assets) from a sweep candidate (FILE#N), a fix plan, or a "
             "verify result",
        description=(
            "Turn a hotato result into a self-contained SVG card you can drop "
            "into a PR, an issue, or a slide. Four kinds are auto-detected: a "
            "talk-over candidate and a false-stop candidate (from a "
            "sweep/analyze candidate ref FILE#N), the threshold-funnel fix "
            "plan (the hero card), and a supported verify rollup. The SVG is "
            "DETERMINISTIC (a pure function of the input JSON: no timestamps, "
            "no version, no randomness) and references no font, image, "
            "stylesheet, or link; all color is inline. It names the MEASURED "
            "timing moment and never a verdict about intent, and carries no "
            "accuracy number. Redacted by default: a call id, a path (only a "
            "basename is ever shown), and a vendor recording name are hidden "
            "unless --include-identifiers."
        ),
        epilog=(
            _exit_codes_epilog("card") + "\n\n"
            "Examples:\n"
            "  hotato sweep --demo --format json > hotato-sweep.json\n"
            "  hotato card hotato-sweep.json#1 --out talk-over.svg\n\n"
            "  hotato demo --format json > demo.json\n"
            "  hotato plan demo.json --out fix-plan.json\n"
            "  hotato card fix-plan.json --out no-single-threshold.svg"
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    cd.add_argument("input", metavar="INPUT[#REF]",
                    help="a fix-plan or verify JSON file, or a sweep/analyze "
                         "candidate ref FILE#N (the #N rank the report shows)")
    cd.add_argument("--out", default=None, metavar="FILE.svg",
                    help="write the SVG here (atomic); without it the SVG is "
                         "written to stdout")
    cd.add_argument("--include-identifiers", action="store_true",
                    help="show the source recording's basename on a candidate "
                         "card; hidden by default (a card is shareable)")
    cd.set_defaults(func=_cmd_card)

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

    # --- explain: root-cause-by-layer, composed from diagnose + plan --------
    ex = sub.add_parser(
        "explain",
        help="root-cause-by-layer analysis of a finished result: likely "
             "layer, fixability, evidence for/against, unknowns, and a safe "
             "next action (read-only; refuses rather than guesses)",
        description=(
            "Read a finished result -- a run envelope (hotato run --format "
            "json > result.json), a sweep/analyze candidate ref "
            "(hotato-sweep.json#N), or a contract bundle directory "
            "(<id>.hotato) -- and emit a layer-general attribution per "
            "attributable failure: failure_layer, type, confidence, "
            "fixability (safe_to_patch | needs_human | insufficient_evidence "
            "| do_not_patch), opposite_risk, evidence_for, evidence_against, "
            "and explicit unknowns. This adds no new scoring engine: it "
            "reframes hotato diagnose's per-event findings and the same "
            "policy gate hotato plan enforces (a mapped knob, a passing "
            "opposite-risk fixture, config-only-safe). A candidate ref "
            "carries no human label, so it is always REFUSED with the exact "
            "promote command for both labels. When evidence genuinely "
            "cannot support one root cause (echo bleed, an ambiguous slow "
            "yield, a contract whose false stop could be backchannel, "
            "ambient noise, or echo), explain REFUSES with the reason "
            "instead of guessing. Read-only: nothing is fetched, mutated, or "
            "applied."
        ),
        epilog=(
            _exit_codes_epilog("explain") + "\n"
            "Examples:\n"
            "  hotato explain result.json\n"
            "  hotato explain hotato-sweep.json#1\n"
            "  hotato explain contracts/refund-cutoff-001.hotato\n"
            "  hotato explain result.json --format json\n"
            "  hotato explain result.json --html explain.html"
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    ex.add_argument(
        "source", metavar="RESULT",
        help="a hotato envelope JSON, a FILE#N / FILE#CALL:N candidate ref, "
             "or a contract bundle directory",
    )
    ex.add_argument("--format", default="text", choices=["json", "text"],
                    help="output format (default text)")
    ex.add_argument("--html", default=None, metavar="PATH",
                    help="also write a self-contained HTML report to PATH")
    ex.set_defaults(func=_cmd_explain)

    # --- fixture create: bad call moment -> permanent regression fixture ----
    fx = sub.add_parser(
        "fixture",
        help="turn a bad call moment into a permanent regression fixture "
             "(hotato fixture create / promote)",
        description=(
            "Fixture tooling for the regression loop (see "
            "docs/BAD-CALL-TO-CI.md)."
        ),
        epilog=_exit_codes_epilog("fixture"),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    fxsub = fx.add_subparsers(dest="fixture_command", required=True,
                              metavar="create|promote")
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

    # --- fixture promote: sweep/analyze candidate -> regression fixture -----
    fp = fxsub.add_parser(
        "promote",
        help="promote one sweep/analyze candidate (FILE#N or FILE#CALL:N) "
             "into a permanent regression fixture",
        description=(
            "Promote a candidate moment from a `hotato sweep --format json` "
            "or `hotato analyze --format json` result into a fixture that "
            "`hotato run --scenarios DIR --audio DIR` scores forever. The "
            "ref names the result file and the candidate: FILE#N is the Nth "
            "candidate in the file (1-based, rank order -- the same #N rank "
            "the report shows); FILE#CALL:N is the Nth candidate from one "
            "call (its source file name, with or without the extension, or "
            "the pulled call id). The candidate carries the recording, the "
            "onset, and the kind, so unlike `fixture create` no --stereo "
            "and no --onset is needed; you add the label. The created "
            "fixture is scored immediately; a candidate that cannot be "
            "judged is refused with the honest reason (exit 2), never "
            "written as a fixture that would report a meaningless verdict. "
            "Offline; no accuracy percentage anywhere."
        ),
        epilog=(
            _exit_codes_epilog("fixture promote") + "\n\n" + _LABEL_NOTE
            + "\n\n"
            "Examples:\n"
            "  hotato sweep --demo --format json > hotato-sweep.json\n"
            "  hotato fixture promote hotato-sweep.json#3 --expect yield \\\n"
            "      --id refund-cutoff-001 --out tests/hotato\n"
            "  hotato fixture promote analyze.json#call_abc123:2 --expect hold \\\n"
            "      --id ack-hold-002 --out tests/hotato\n"
            "  hotato run --scenarios tests/hotato/scenarios --audio tests/hotato/audio"
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    fp.add_argument("ref", metavar="CANDIDATE_REF",
                    help="which candidate: FILE#N or FILE#CALL:N, e.g. "
                         "hotato-sweep.json#3 or analyze.json#call_abc123:2")
    fp.add_argument("--expect", required=True, choices=["yield", "hold"],
                    help="YOUR label for the event: 'yield' (the agent should "
                         "stop for the caller) or 'hold' (the agent should "
                         "keep speaking)")
    fp.add_argument("--id", required=True,
                    help="fixture id slug, e.g. refund-interruption-001")
    fp.add_argument("--out", required=True, metavar="DIR",
                    help="fixture root; writes DIR/scenarios/<id>.json and "
                         "DIR/audio/<id>.example.wav")
    fp.add_argument("--folder", default=None, metavar="DIR",
                    help="folder holding the swept/analyzed recordings, for "
                         "when the folder recorded in the result file does "
                         "not resolve from here")
    fp.add_argument("--title", default=None,
                    help="human title (default: the id with spaces)")
    fp.add_argument("--stack", default="generic",
                    choices=["generic", "vapi", "twilio", "livekit",
                             "pipecat", "retell"],
                    help="voice stack the recording came from (labels the "
                         "validation fix knob only)")
    fp.add_argument("--max-talk-over", type=float, default=None,
                    help="[yield] fail the fixture if talk-over exceeds this many seconds")
    fp.add_argument("--max-time-to-yield", type=float, default=None,
                    help="[yield] fail the fixture if the yield is slower than this many seconds")
    fp.add_argument("--tags", default=None,
                    help="comma-separated tags for the scenario JSON")
    fp.add_argument("--pre", type=float, default=2.0,
                    help="seconds of audio kept BEFORE the onset when clipping (default 2.0)")
    fp.add_argument("--post", type=float, default=6.0,
                    help="seconds of audio kept AFTER the onset when clipping (default 6.0)")
    fp.add_argument("--no-clip", action="store_true",
                    help="keep the full recording and the original onset instead of clipping")
    fp.add_argument("--force", action="store_true",
                    help="overwrite an existing fixture with the same id")
    fp.add_argument("--caller-channel", type=int, default=0)
    fp.add_argument("--agent-channel", type=int, default=1)
    fp.add_argument("--format", default="text", choices=["text", "json"],
                    help="output format (default text)")
    fp.set_defaults(func=_cmd_fixture_promote)

    # --- contract: the portable failure contract -----------------------------
    ct = sub.add_parser(
        "contract",
        help="turn a bad call moment into a portable, CI-enforced failure "
             "contract (hotato contract create/verify/inspect/pack/unpack)",
        description=(
            "Failure-contract tooling: create a self-contained "
            "<id>.hotato bundle from a real call moment (audio, frame "
            "evidence, an input-health report, a shareable card, and a CI "
            "policy), verify a directory of contracts for CI, inspect one, "
            "and pack/unpack the bundle as a single portable archive. See "
            "docs/CONTRACTS.md."
        ),
        epilog=_exit_codes_epilog("contract"),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    ctsub = ct.add_subparsers(dest="contract_command", required=True,
                              metavar="create|verify|inspect|pack|unpack")

    cc = ctsub.add_parser(
        "create",
        help="write a <id>.hotato bundle from one call moment, validated by "
             "scoring it immediately",
        description=(
            "Turn ONE moment of a recording you already have into a "
            "portable failure contract: contract.json, the (clipped) "
            "audio, frame-level evidence, an input-health (trust) report, a "
            "shareable SVG card, a CI policy, and the exact replay/CI "
            "commands. Reuses the same round-trip scorability guarantee "
            "`fixture create` gives: a not-scorable moment is refused with "
            "the honest reason (exit 2) and no bundle is written. A mono "
            "recording is rejected by default; pass --mono with --diarize "
            "for the opt-in, quality-gated diarized-mono path (never "
            "silently upgraded past indicative-only). Offline; no accuracy "
            "percentage anywhere."
        ),
        epilog=(
            _exit_codes_epilog("contract create") + "\n\n" + _LABEL_NOTE
            + "\n\n"
            "Examples:\n"
            "  hotato sweep --demo --format json > hotato-sweep.json\n"
            "  hotato contract create --from-candidate hotato-sweep.json#1 \\\n"
            "      --expect yield --id refund-cutoff-001 --out contracts\n"
            "  hotato contract create --stereo bad-call.wav --onset 42.18 \\\n"
            "      --expect yield --id refund-cutoff-001 --out contracts\n"
            "  hotato contract verify contracts/ --junit contracts-junit.xml"
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    cc.add_argument("--from-candidate", metavar="FILE#N",
                    help="a hotato sweep/analyze --format json candidate ref "
                         "(FILE#N or FILE#CALL:N); resolves the source "
                         "recording and onset the same way `fixture promote` "
                         "does")
    cc.add_argument("--stereo", help="two-channel WAV (caller on one channel, "
                                     "agent on the other)")
    cc.add_argument("--caller", help="mono WAV of the caller channel (with --agent)")
    cc.add_argument("--agent", help="mono WAV of the agent channel (with --caller)")
    cc.add_argument("--mono", metavar="WAV",
                    help="a SINGLE-channel recording; requires --diarize (the "
                         "opt-in, quality-gated mono-scorability front-end). "
                         "A mono file passed to --stereo is always rejected; "
                         "this is the only supported mono path")
    cc.add_argument("--diarize", action="store_true",
                    help="[with --mono] diarize the mono recording into "
                         "caller/agent tracks before scoring; refuses a "
                         "non-separable file (exit 2), never a raw-mono guess")
    cc.add_argument("--diarizer", default="pyannote",
                    choices=["pyannote", "sortformer", "pyannoteai"],
                    help="[--diarize] backend (default pyannote, local); "
                         "pyannoteai is HOSTED and needs --egress-opt-in")
    cc.add_argument("--caller-speaker", default=None, metavar="LABEL",
                    help="[--diarize] override the proposed caller speaker "
                         "label instead of the floor-dominance heuristic")
    cc.add_argument("--agent-speaker", default=None, metavar="LABEL",
                    help="[--diarize] override the proposed agent speaker label")
    cc.add_argument("--egress-opt-in", action="store_true",
                    help="[--diarizer pyannoteai] allow this one call's audio "
                         "to leave the machine for the hosted backend")
    cc.add_argument("--onset", type=float, default=None,
                    help="the moment (seconds into the SOURCE recording) the "
                         "caller took or attempted the floor; required with "
                         "--stereo/--caller+--agent/--mono, resolved "
                         "automatically with --from-candidate")
    cc.add_argument("--expect", required=True, choices=["yield", "hold"],
                    help="YOUR label for the event: 'yield' (the agent "
                         "should stop for the caller) or 'hold' (the agent "
                         "should keep speaking)")
    cc.add_argument("--id", required=True,
                    help="contract id slug, e.g. refund-cutoff-001")
    cc.add_argument("--out", required=True, metavar="DIR",
                    help="contract root; writes DIR/<id>.hotato/")
    cc.add_argument("--folder", default=None, metavar="DIR",
                    help="[--from-candidate] folder holding the swept/"
                         "analyzed recordings, when the folder recorded in "
                         "the result file does not resolve from here")
    cc.add_argument("--stack", default="generic",
                    choices=["generic", "vapi", "twilio", "livekit",
                             "pipecat", "retell"],
                    help="voice stack the recording came from")
    cc.add_argument("--max-talk-over", type=float, default=None,
                    help="[yield] the contract's policy fails if talk-over "
                         "exceeds this many seconds")
    cc.add_argument("--max-time-to-yield", type=float, default=None,
                    help="[yield] the contract's policy fails if the yield is "
                         "slower than this many seconds")
    cc.add_argument("--rationale", default=None,
                    help="optional free-text note on why you labeled the "
                         "event this way")
    cc.add_argument("--pre", type=float, default=2.0,
                    help="seconds of audio kept BEFORE the onset when clipping "
                         "(default 2.0; --stereo/--caller+--agent/--from-candidate only)")
    cc.add_argument("--post", type=float, default=6.0,
                    help="seconds of audio kept AFTER the onset when clipping "
                         "(default 6.0)")
    cc.add_argument("--no-clip", action="store_true",
                    help="keep the full recording and the original onset "
                         "instead of clipping")
    cc.add_argument("--force", action="store_true",
                    help="overwrite an existing contract with the same id")
    cc.add_argument("--caller-channel", type=int, default=0)
    cc.add_argument("--agent-channel", type=int, default=1)
    cc.add_argument("--confirm-channels", action="store_true",
                    help="human confirmation that the caller/agent channel "
                         "mapping is correct despite a suspected swap or "
                         "crosstalk/leakage; without it, such a contract's "
                         "verdict is withheld (null did_yield/seconds_to_yield/"
                         "talk_over/passed) and `contract verify` REFUSES it")
    cc.add_argument("--include-identifiers", action="store_true",
                    help="show the source recording's basename / candidate "
                         "ref in the bundle and the card instead of "
                         "redacting them (default: redacted)")
    cc.add_argument("--format", default="text", choices=["text", "json"],
                    help="output format (default text)")
    cc.set_defaults(func=_cmd_contract_create)

    cv = ctsub.add_parser(
        "verify",
        help="batch-verify a directory of contracts for CI: re-score every "
             "bundle's audio against its recorded policy",
        description=(
            "Re-score every contract's bundled audio against the policy "
            "recorded in its own contract.json (this is what changes after "
            "an engine upgrade, a threshold change, or a re-captured audio "
            "file) and report pass/fail per contract and overall. DIR is a "
            "single <id>.hotato bundle, or a parent directory of them."
        ),
        epilog=(
            _exit_codes_epilog("contract verify") + "\n\n"
            "Examples:\n"
            "  hotato contract verify contracts/\n"
            "  hotato contract verify contracts/ --format json --junit contracts-junit.xml\n"
            "  hotato contract verify contracts/refund-cutoff-001.hotato --html verify.html"
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    cv.add_argument("dir", metavar="DIR",
                    help="a contracts directory, or one <id>.hotato bundle")
    cv.add_argument("--html", default=None, metavar="PATH",
                    help="also write a self-contained HTML rollup")
    cv.add_argument("--junit", default=None, metavar="PATH",
                    help="also write a JUnit XML report (one testcase per "
                         "contract) for a CI dashboard")
    cv.add_argument("--format", default="text", choices=["text", "json"],
                    help="stdout format (default text; json prints the full "
                         "batch result)")
    cv.add_argument("--transcript", default=None, metavar="FILE",
                    help="a transcript JSON file (hotato assert's own "
                         "--transcript shape: a plain array of {role, text, "
                         "start, end} turns, or a {\"segments\": [...]} "
                         "envelope) used as context for every contract's "
                         "embedded `assertions` block, if any; works fully "
                         "without the [transcribe] extra")
    cv.set_defaults(func=_cmd_contract_verify)

    ci_ = ctsub.add_parser(
        "inspect",
        help="print one contract's contract.json",
        description="Load and print one <id>.hotato bundle's contract.json.",
        epilog=_exit_codes_epilog("contract inspect"),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    ci_.add_argument("path", metavar="PATH",
                     help="a <id>.hotato bundle directory, or its contract.json")
    ci_.add_argument("--format", default="text", choices=["text", "json"],
                     help="output format (default text)")
    ci_.set_defaults(func=_cmd_contract_inspect)

    cp = ctsub.add_parser(
        "pack",
        help="pack a <id>.hotato bundle directory into one deterministic "
             ".hotato archive with a sha256 manifest",
        description=(
            "Pack a contract bundle directory into a single portable "
            ".hotato file (a zip with a MANIFEST.sha256.json of every "
            "member), so it can be sent, attached, or committed as one "
            "file. `contract unpack` verifies every member's sha256 on the "
            "way back out."
        ),
        epilog=_exit_codes_epilog("contract pack"),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    cp.add_argument("bundle", metavar="BUNDLE_DIR",
                    help="a <id>.hotato bundle directory")
    cp.add_argument("--out", default=None, metavar="PATH",
                    help="archive path (default: BUNDLE_DIR.pack next to it, "
                         "e.g. refund-cutoff-001.hotato.pack -- never the "
                         "bundle directory's own name)")
    cp.add_argument("--force", action="store_true",
                    help="overwrite an existing archive at --out")
    cp.add_argument("--format", default="text", choices=["text", "json"],
                    help="output format (default text)")
    cp.set_defaults(func=_cmd_contract_pack)

    cu = ctsub.add_parser(
        "unpack",
        help="unpack a .hotato archive, verifying every member against its "
             "sha256 manifest",
        description=(
            "Unpack a .hotato archive written by `contract pack` back into "
            "a bundle directory, verifying every member's sha256 against "
            "the packed manifest. Any mismatch (a corrupt or tampered "
            "archive) is refused (exit 2) and nothing partial is left "
            "behind. Treats the archive as hostile input: path traversal, "
            "absolute/backslash/drive-letter paths, symlink or encrypted "
            "members, duplicate names, members not declared in the "
            "manifest, too many members, and a decompressed size (declared "
            "or actual) past --max-bytes are all refused before or during "
            "extraction."
        ),
        epilog=_exit_codes_epilog("contract unpack"),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    cu.add_argument("archive", metavar="ARCHIVE",
                    help="a .hotato archive written by `contract pack`")
    cu.add_argument("--out", required=True, metavar="DIR",
                    help="directory to unpack the bundle into")
    cu.add_argument("--force", action="store_true",
                    help="overwrite an existing --out directory")
    cu.add_argument("--max-bytes", type=int, default=None, metavar="N",
                    help="cap on total decompressed bytes, enforced against "
                         "the archive's ACTUAL decompressed content (default "
                         "512 MiB, or $HOTATO_CONTRACT_MAX_UNPACK_BYTES; "
                         "raise only for a trusted archive)")
    cu.add_argument("--format", default="text", choices=["text", "json"],
                    help="output format (default text)")
    cu.set_defaults(func=_cmd_contract_unpack)

    # --- trace: the observability bridge (voice_trace.v1) --------------------
    tr = sub.add_parser(
        "trace",
        help="attach an OTel-flavored voice trace to a failure contract "
             "(hotato trace ingest/attach/export)",
        description=(
            "Bridge a voice-pipeline trace (audio activity, TTS cancel/"
            "stop, ASR partials, tool calls, ...) into a failure contract's "
            "evidence, so a talk-over is not just a measured span but "
            "'evidence suggests TTS cancellation lagged: cancel requested "
            "at 42.40s, audio stopped at 43.60s'. Stays open and local: "
            "never a hosted trace store, never a network call. See "
            "docs/TRACE.md and docs/OTEL.md."
        ),
        epilog=_exit_codes_epilog("trace"),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    trsub = tr.add_subparsers(dest="trace_command", required=True,
                              metavar="ingest|attach|export")

    ti = trsub.add_parser(
        "ingest",
        help="parse an OTel-flavored source into hotato.voice_trace.v1 "
             "JSONL",
        description=(
            "Parse --otel FILE (a standard OTel JSON export with a "
            "top-level resourceSpans array, OR hotato's own documented "
            "per-line bridge JSONL -- see docs/OTEL.md) into "
            "hotato.voice_trace.v1 and write it as JSONL (one meta line, "
            "then one line per span, same convention evidence/frames.jsonl "
            "uses). NOT full OTel wire-protocol coverage: only span name, "
            "start/end time, attributes, and span events are read; an "
            "unrecognized span name is passed through unchanged rather "
            "than dropped."
        ),
        epilog=(
            _exit_codes_epilog("trace ingest") + "\n\n"
            "Examples:\n"
            "  hotato trace ingest --otel traces.jsonl --out voice_trace.jsonl\n"
            "  hotato trace ingest --otel export.json --out voice_trace.jsonl \\\n"
            "      --stack vapi --include-identifiers --include-text"
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    ti.add_argument("--otel", required=True, metavar="FILE",
                    help="an OTel JSON export (resourceSpans) or hotato's "
                         "OTel bridge JSONL")
    ti.add_argument("--out", required=True, metavar="PATH",
                    help="voice_trace.jsonl path to write")
    ti.add_argument("--call-id", default=None,
                    help="override/attach a call id (redacted unless "
                         "--include-identifiers)")
    ti.add_argument("--stack", default=None,
                    help="override/attach the deployment stack (default: "
                         "read from the source's resource attributes)")
    ti.add_argument("--agent-id", default=None,
                    help="override/attach an agent id (redacted unless "
                         "--include-identifiers)")
    ti.add_argument("--git-sha", default=None,
                    help="override/attach the deployment git SHA")
    ti.add_argument("--config-hash", default=None,
                    help="override/attach the deployment config hash")
    ti.add_argument("--include-identifiers", action="store_true",
                    help="keep call_id and deployment.agent_id instead of "
                         "redacting them (default: redacted)")
    ti.add_argument("--include-text", action="store_true",
                    help="keep an asr_partial span's transcript text "
                         "instead of redacting it (default: redacted; "
                         "text_redacted stays true either way it is stated)")
    ti.add_argument("--force", action="store_true",
                    help="overwrite an existing --out file")
    ti.add_argument("--format", default="text", choices=["text", "json"],
                    help="output format (default text)")
    ti.set_defaults(func=_cmd_trace_ingest)

    ta = trsub.add_parser(
        "attach",
        help="write a voice trace into a contract bundle and re-render its "
             "evidence timeline with the trace's events drawn as an "
             "aligned row",
        description=(
            "Copy --trace (a hotato.voice_trace.v1 JSONL from `trace "
            "ingest`) into <bundle>/traces/voice_trace.jsonl and re-render "
            "evidence/timeline.html. Rebuilds the timeline from the "
            "bundle's OWN evidence/frames.jsonl and contract.json -- never "
            "re-runs the VAD or diarizer, so this never needs the "
            "diarization extra installed and never re-scores the audio."
        ),
        epilog=(
            _exit_codes_epilog("trace attach") + "\n\n"
            "Examples:\n"
            "  hotato trace attach contracts/refund-cutoff-001.hotato \\\n"
            "      --trace voice_trace.jsonl"
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    ta.add_argument("bundle", metavar="BUNDLE_DIR",
                    help="a <id>.hotato contract bundle directory")
    ta.add_argument("--trace", required=True, metavar="PATH",
                    help="a hotato.voice_trace.v1 JSONL file (from `hotato "
                         "trace ingest`)")
    ta.add_argument("--force", action="store_true",
                    help="replace an already-attached trace")
    ta.add_argument("--format", default="text", choices=["text", "json"],
                    help="output format (default text)")
    ta.set_defaults(func=_cmd_trace_attach)

    te = trsub.add_parser(
        "export",
        help="write a bundle's attached trace back out as OTel-flavored "
             "bridge JSONL",
        description=(
            "Write <bundle>/traces/voice_trace.jsonl back out as hotato's "
            "OTel-flavored bridge JSONL -- the same shape `trace ingest` "
            "reads, so ingest -> attach -> export -> ingest round-trips "
            "the same spans."
        ),
        epilog=(
            _exit_codes_epilog("trace export") + "\n\n"
            "Examples:\n"
            "  hotato trace export contracts/refund-cutoff-001.hotato \\\n"
            "      --format otel --out otel.jsonl"
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    te.add_argument("bundle", metavar="BUNDLE_DIR",
                    help="a <id>.hotato contract bundle directory")
    te.add_argument("--format", default="otel", choices=["otel"],
                    help="export format (only 'otel' -- hotato's OTel "
                         "bridge JSONL -- is supported today)")
    te.add_argument("--out", required=True, metavar="PATH",
                    help="path to write the exported JSONL")
    te.add_argument("--force", action="store_true",
                    help="overwrite an existing --out file")
    te.add_argument("--json", action="store_true",
                    help="print the machine result summary instead of text "
                         "(--format is already claimed by the export "
                         "format on this subcommand)")
    te.set_defaults(func=_cmd_trace_export)

    # --- assert: the deterministic assertion engine (assert.v1) -------------

    asrt = sub.add_parser(
        "assert",
        help="run a deterministic assertions.yaml against a call's "
             "transcript/spans/timing (phrase/pii/policy/tool_call/outcome)",
        description=(
            "The honesty wall made structural: phrase, pii, policy, "
            "tool_call, and outcome assertions -- every one of them pure "
            "regex/checksum/span-lookup, never a model call. The summary "
            "always splits deterministic pass/fail/inconclusive counts "
            "from a separate judge count and never emits a merged score "
            "(a judge kind is a separate, quarantined capability, not "
            "built here)."
        ),
        epilog=_exit_codes_epilog("assert"),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    asub = asrt.add_subparsers(dest="assert_command", required=True,
                               metavar="init|run")

    ai = asub.add_parser(
        "init",
        help="write a starter assertions.yaml inferred from a trace's "
             "tool_call spans (+ timing, with --stereo)",
        description=(
            "Infer a starter assertions.yaml from --from-trace (a "
            "hotato.voice_trace.v1 JSONL, from `hotato trace ingest`): one "
            "tool_call 'was it called' assertion per distinct tool seen, "
            "a require_order assertion when 2+ distinct tools were "
            "observed, and -- only with --stereo -- one outcome "
            "field_present starter grounded in that recording's own "
            "scored verdict. A STARTER the user edits, never a claim "
            "these are the RIGHT assertions for the call."
        ),
        epilog=(
            _exit_codes_epilog("assert init") + "\n\n"
            "Examples:\n"
            "  hotato assert init --from-trace voice_trace.jsonl\n"
            "  hotato assert init --from-trace voice_trace.jsonl "
            "--stereo call.wav \\\n"
            "      --out assertions.yaml"
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    ai.add_argument("--from-trace", required=True, metavar="FILE",
                    help="a hotato.voice_trace.v1 JSONL (from `hotato "
                         "trace ingest`)")
    ai.add_argument("--stereo", default=None, metavar="WAV",
                    help="also score this recording to seed a timing-based "
                         "outcome starter (optional; the trace's tool_call "
                         "spans alone are enough for the tool_call "
                         "starters)")
    ai.add_argument("--out", default="assertions.yaml", metavar="PATH",
                    help="assertions.yaml path to write (default "
                         "assertions.yaml)")
    ai.add_argument("--force", action="store_true",
                    help="overwrite an existing --out file")
    ai.add_argument("--format", default="text", choices=["text", "json"],
                    help="output format (default text)")
    ai.set_defaults(func=_cmd_assert_init)

    ar = asub.add_parser(
        "run",
        help="evaluate an assertions.yaml against a call's transcript/"
             "spans/timing",
        description=(
            "Build a Context from --transcript (or --transcribe over "
            "--stereo), --trace, and -- via --stereo -- a freshly scored "
            "run's timing, then evaluate --assertions against it. Every "
            "one of the 5 kinds here is deterministic; --format text "
            "prints per-kind PASS/FAIL/INCONCLUSIVE counts, and the judge "
            "count separately -- never one merged number. By default an "
            "INCONCLUSIVE (missing-input) result never fails the run; a "
            "CI/compliance suite can make it gate with --inconclusive-policy "
            "fail or refuse (or the same key in the assertions file)."
        ),
        epilog=(
            _exit_codes_epilog("assert run") + "\n\n"
            "Examples:\n"
            "  hotato assert run --transcript call.transcript.json \\\n"
            "      --assertions assertions.yaml\n"
            "  hotato assert run --stereo call.wav --trace "
            "voice_trace.jsonl \\\n"
            "      --assertions assertions.yaml\n"
            "  hotato assert run --stereo call.wav --transcribe "
            "--assertions assertions.yaml \\\n"
            "      --format json"
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    ar.add_argument("--assertions", required=True, metavar="FILE",
                    help="the assertions.yaml (or equivalent JSON) file to "
                         "evaluate")
    ar.add_argument("--stereo", default=None, metavar="WAV",
                    help="score this recording for timing context "
                         "(outcome's field_present) and, with --transcribe, "
                         "as the ASR input")
    ar.add_argument("--transcript", default=None, metavar="FILE",
                    help="a transcript JSON file (a plain array of {role, "
                         "text, start, end} turns, or the {\"segments\": "
                         "[...]} shape hotato.transcribe / the MCP surface "
                         "write) -- lets assert run work fully without the "
                         "[transcribe] extra; never combined with "
                         "--transcribe")
    ar.add_argument("--transcribe", action="store_true",
                    help="transcribe --stereo with faster-whisper (the "
                         "[transcribe] extra) instead of passing "
                         "--transcript; requires --stereo")
    ar.add_argument("--transcribe-model", default="base.en", metavar="NAME",
                    help="faster-whisper model name for --transcribe "
                         "(default base.en)")
    ar.add_argument("--transcribe-device", default="auto",
                    choices=["auto", "cpu", "cuda"],
                    help="device for --transcribe (default auto: cuda if "
                         "available, else cpu)")
    ar.add_argument("--trace", default=None, metavar="FILE",
                    help="a hotato.voice_trace.v1 JSONL (from `hotato "
                         "trace ingest`); tool_call assertions read only "
                         "these spans, never transcript text")
    ar.add_argument("--inconclusive-policy", default=None,
                    choices=["report", "fail", "refuse"],
                    help="how an INCONCLUSIVE (missing required input) result "
                         "gates the exit code: report (default) never fails "
                         "on inconclusive; fail treats it like a FAIL (exit "
                         "1); refuse withholds a verdict (exit 2, precedence "
                         "over a FAIL). Overrides any inconclusive_policy in "
                         "the --assertions file; CI/compliance suites should "
                         "set fail or refuse")
    ar.add_argument("--format", default="text", choices=["text", "json"],
                    help="output format (default text)")
    ar.set_defaults(func=_cmd_assert_run)

    # --- test: the Phase-1 EXIT -- one conversation-test file end to end -----
    tst = sub.add_parser(
        "test",
        help="evaluate a conversation-test file against a supplied call and "
             "emit the per-dimension scorecard + conversation artifact",
        description=(
            "The Phase-1 conversation-QA entry point: one conversation-test "
            "file drives a deterministic evaluation of a supplied call, "
            "producing a per-dimension scorecard and a digest-bound "
            "conversation artifact. Success is a boolean over named conditions "
            "-- never a blended score."
        ),
        epilog=_exit_codes_epilog("test"),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    tstsub = tst.add_subparsers(dest="test_command", required=True, metavar="run")
    tr_ = tstsub.add_parser(
        "run",
        help="evaluate one conversation-test.yaml against a supplied "
             "transcript/trace/state/audio",
        description=(
            "Load + validate a conversation-test file, build a Context from "
            "the supplied --transcript / --trace / --state / --audio (scored "
            "for timing), evaluate the DETERMINISTIC assertion lane (the "
            "model-judged rubric lane is quarantined until Phase 3 -> "
            "INCONCLUSIVE), evaluate the file's success.required conditions, "
            "bind the evidence into a hotato.conversation.v1 artifact (--out), "
            "and render the unified report + per-dimension scorecard. There is "
            "NO overall_score anywhere. Missing input leaves a check "
            "INCONCLUSIVE, never guessed. The exit code honors the file's "
            "inconclusive_policy (report/fail/refuse) exactly as `assert run`, "
            "raised to non-zero when a success.required condition fails. "
            "--repetitions N runs the deterministic lane N times and reports a "
            "plain run count (reliability pass^k lands in Phase 2, never "
            "fabricated here)."
        ),
        epilog=(
            _exit_codes_epilog("test run") + "\n\n"
            "Examples:\n"
            "  hotato test run refund.yaml --agent support-v3 \\\n"
            "      --audio call.wav --trace voice_trace.jsonl \\\n"
            "      --transcript call.transcript.json --out ./conv-artifact\n"
            "  hotato test run refund.yaml --agent support-v3 \\\n"
            "      --trace voice_trace.jsonl --format json"
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    tr_.add_argument("test_file", metavar="conversation-test.yaml",
                     help="the conversation-test.v1 file to evaluate")
    tr_.add_argument("--agent", required=True, metavar="ID",
                     help="agent_id under test (recorded on the conversation "
                          "artifact)")
    tr_.add_argument("--transcript", default=None, metavar="FILE",
                     help="a transcript JSON (a {role,text,start,end} array or "
                          "the {\"segments\": [...]} shape); phrase/pii/count "
                          "checks read it, absent -> those are INCONCLUSIVE")
    tr_.add_argument("--trace", default=None, metavar="FILE",
                     help="a hotato.voice_trace.v1 JSONL (from `hotato trace "
                          "ingest`); tool_call/tool_result/sequence/latency "
                          "checks read only these spans, absent -> INCONCLUSIVE")
    tr_.add_argument("--state", default=None, metavar="FILE",
                     help="a mock state-adapter sandbox (JSON, or SQLite by "
                          "extension) the state/state_change (Authority 2) "
                          "checks query; absent -> those are INCONCLUSIVE")
    tr_.add_argument("--audio", nargs="+", default=None,
                     metavar="WAV",
                     help="ONE dual-channel recording, or TWO mono files "
                          "(caller agent), scored for the timing context and "
                          "the report timeline; the single dual-channel form is "
                          "bound into the artifact's audio slot")
    tr_.add_argument("--repetitions", type=int, default=None, metavar="N",
                     help="run the deterministic lane N times (default: the "
                          "file's repetitions, else 1); reports a plain run "
                          "count -- reliability (pass^k) is Phase 2")
    tr_.add_argument("--out", default=None, metavar="DIR",
                     help="write the conversation artifact (conversation.json + "
                          "bound children) here; required for --format html/md")
    tr_.add_argument("--created-at", default=None, metavar="ISO8601",
                     help="the artifact's created_at (default: now, UTC); set "
                          "it for a byte-reproducible manifest")
    tr_.add_argument("--format", default="text",
                     choices=["text", "html", "md", "json"],
                     help="text (default: the per-dimension summary), html/md "
                          "(the unified report into --out, needs --audio), or "
                          "json (the full machine result)")
    tr_.set_defaults(func=_cmd_test_run)

    # --- scenario: author + validate conversation-test files ----------------
    scn = sub.add_parser(
        "scenario",
        help="write a starter conversation-test.yaml, or validate one/many",
        description=(
            "Author and validate conversation-test files. `init` writes a "
            "starter you edit; `validate` structurally validates one file or a "
            "directory of them (exit 2 on any malformed file) -- mirrors "
            "`assert init` / a validation pass."
        ),
        epilog=_exit_codes_epilog("scenario"),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    scnsub = scn.add_subparsers(dest="scenario_command", required=True,
                                metavar="init|validate")
    si = scnsub.add_parser(
        "init",
        help="write a starter conversation-test.yaml",
        description=(
            "Write a starter conversation-test.yaml: a simulated caller, the "
            "two SEPARATE assertion lanes (deterministic checks tagged across "
            "the report dimensions, plus one quarantined rubric ref), a boolean "
            "success over named conditions (never a score), and a commented "
            "`# inconclusive_policy: fail` line a CI suite uncomments. A starter "
            "you edit, never a claim these are the RIGHT checks for your call."
        ),
        epilog=(
            _exit_codes_epilog("scenario init") + "\n\n"
            "Examples:\n"
            "  hotato scenario init refund-flow --out refund.yaml\n"
            "  hotato scenario init --agent support-v3"
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    si.add_argument("name", nargs="?", default="example-scenario",
                    help="the scenario id (default example-scenario)")
    si.add_argument("--agent", default="my-agent-v1", metavar="ID",
                    help="agent_id under test to seed the file with (default "
                         "my-agent-v1)")
    si.add_argument("--out", default="conversation-test.yaml", metavar="PATH",
                    help="path to write (default conversation-test.yaml)")
    si.add_argument("--force", action="store_true",
                    help="overwrite an existing --out file")
    si.add_argument("--format", default="text", choices=["text", "json"],
                    help="output format (default text)")
    si.set_defaults(func=_cmd_scenario_init)

    sv = scnsub.add_parser(
        "validate",
        help="structurally validate one conversation-test file or a directory "
             "of them",
        description=(
            "Validate a conversation-test file (or every *.yaml/*.yml/*.json in "
            "a directory) against the conversation-test.v1 shape and honesty "
            "wall (no overall_score, closed success/dimension vocabularies, "
            "separate lanes). Exit 2 if any file is malformed."
        ),
        epilog=(
            _exit_codes_epilog("scenario validate") + "\n\n"
            "Examples:\n"
            "  hotato scenario validate refund.yaml\n"
            "  hotato scenario validate ./scenarios --format json"
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    sv.add_argument("path", metavar="PATH",
                    help="a conversation-test file, or a directory of them")
    sv.add_argument("--format", default="text", choices=["text", "json"],
                    help="output format (default text)")
    sv.set_defaults(func=_cmd_scenario_validate)

    # --- conversation: verify a conversation artifact -----------------------
    cvp = sub.add_parser(
        "conversation",
        help="verify a conversation artifact's bound evidence by digest",
        description=(
            "Operate on hotato.conversation.v1 artifacts. `verify` re-hashes "
            "every bound child and REFUSES on any tamper or missing file -- a "
            "tampered or absent artifact is refused, never silently accepted."
        ),
        epilog=_exit_codes_epilog("conversation"),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    cvsub = cvp.add_subparsers(dest="conversation_command", required=True,
                               metavar="verify")
    cvv = cvsub.add_parser(
        "verify",
        help="re-hash a conversation artifact's bound children; REFUSE on tamper",
        description=(
            "Digest-verify a conversation artifact directory (containing "
            "conversation.json): re-hash every bound child against its recorded "
            "sha256 and REFUSE (exit 2) on any mismatch or missing child. The "
            "evidence-kernel posture: refuse, never silently accept a tampered "
            "or absent artifact."
        ),
        epilog=(
            _exit_codes_epilog("conversation verify") + "\n\n"
            "Examples:\n"
            "  hotato conversation verify ./conv-artifact\n"
            "  hotato conversation verify ./conv-artifact --format json"
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    cvv.add_argument("dir", metavar="DIR",
                     help="a conversation artifact directory (or a "
                          "conversation.json path)")
    cvv.add_argument("--format", default="text", choices=["text", "json"],
                     help="output format (default text)")
    cvv.set_defaults(func=_cmd_conversation_verify)

    # --- simulate: render a scenario into labelled origin=simulated calls ----
    sm = sub.add_parser(
        "simulate",
        help="render a scenario.v1 into deterministic origin=simulated "
             "conversation artifact(s) (a scripted caller; no live agent)",
        description=(
            "Render a hotato.scenario.v1 file with a DETERMINISTIC scripted "
            "caller into one or more hotato.conversation.v1 artifacts, each "
            "labelled origin=simulated (never real, never merged into a real "
            "bucket). No live agent, no TTS, no network on this path. A SEEDED "
            "REPLAY is byte-identical (the produced transcript is content-"
            "hashed); different seeds differ only where the scenario allows "
            "(probabilistic backchannels). Each produced conversation is checked "
            "for faithfulness to its scenario -- a bad rendering is reported as "
            "SIMULATOR_INVALID, NEVER scored as an agent PASS/FAIL. --repetitions "
            "expands the variation matrix and reports Reliability (pass@1 / "
            "pass@k / pass^k); for this deterministic caller pass^k == pass@1, "
            "reported honestly, not fabricated variance. There is NO "
            "overall_score anywhere."
        ),
        epilog=(
            _exit_codes_epilog("simulate") + "\n\n"
            "Examples:\n"
            "  hotato simulate refund.scenario.yaml --out ./sim\n"
            "  hotato simulate refund.scenario.yaml --repetitions 5 --format json\n"
            "  hotato simulate --matrix refund.scenario.yaml --out ./matrix\n"
            "  hotato simulate --matrix refund.scenario.yaml \\\n"
            "      --conversation-test refund.test.yaml --parallel 8 --format json"
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    sm.add_argument("scenario", metavar="scenario.yaml", nargs="?", default=None,
                    help="the hotato.scenario.v1 file to render (single-run mode; "
                         "use --matrix to run the whole variation matrix)")
    sm.add_argument("--matrix", default=None, metavar="scenario.yaml",
                    help="run the scenario's FULL variation matrix in parallel "
                         "(the 'simulate hundreds' exit): expand every cell, "
                         "render+validate each in a bounded pool, optionally score "
                         "against --conversation-test, and print an ATTRIBUTABLE "
                         "per-variation reliability summary (no blend, no "
                         "overall_score)")
    sm.add_argument("--conversation-test", default=None, metavar="TEST.yaml",
                    dest="conversation_test",
                    help="(--matrix) score each produced simulated conversation "
                         "against this conversation-test's DETERMINISTIC "
                         "assertions; SIMULATOR_INVALID runs are bucketed "
                         "separately, never an agent PASS/FAIL")
    sm.add_argument("--parallel", type=int, default=None, metavar="N",
                    help="(--matrix) max worker threads (default: a CPU-based "
                         "cap); the worker count NEVER changes the byte-identical "
                         "summary")
    sm.add_argument("--seed", type=int, default=None, metavar="N",
                    help="base seed (default: the scenario's seed, else 0); a "
                         "seeded replay is byte-identical")
    sm.add_argument("--repetitions", type=int, default=None, metavar="N",
                    help="override the variation matrix's repetition count "
                         "(drives Reliability pass^k)")
    sm.add_argument("--out", default=None, metavar="DIR",
                    help="write the simulated conversation artifact(s) here "
                         "(one dir for a single run, per-run subdirs otherwise); "
                         "each carries origin.kind=simulated")
    sm.add_argument("--agent", default="unbound", metavar="ID",
                    help="agent_id recorded on the artifact (default 'unbound': "
                         "the caller-side stimulus is not bound to an agent until "
                         "a later live-play slice)")
    sm.add_argument("--created-at", default=None, metavar="ISO8601",
                    help="the artifact's created_at (default: now, UTC); set it "
                         "for a byte-reproducible manifest")
    sm.add_argument("--format", default="text", choices=["text", "json"],
                    help="output format (default text)")
    sm.set_defaults(func=_cmd_simulate)

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

    syn = sub.add_parser(
        "synth",
        help="generate deterministic synthetic perturbations of a REAL recording "
             "(a separate synthetic axis; never blended with real evidence)",
        description=(
            "Apply the deterministic transform matrix (sample rate, gain, additive "
            "noise at a declared SNR, delayed cross-channel leakage, channel "
            "inversion, silence, onset offsets, clipping, backchannel/agent-gap/"
            "packet-gap sweeps) to a real fixture, writing derived clips that each "
            "carry parent hash, transform recipe, seed, tool+version, output hashes, "
            "and an explicit SYNTHETIC designation. A thousand synthetic clips never "
            "raise the evidence of one real recapture (plan section 11)."
        ),
        epilog=_exit_codes_epilog("synth"),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    syn.add_argument("source", metavar="SOURCE_WAV",
                     help="the real recording to derive perturbations from")
    syn.add_argument("--out", required=True, metavar="DIR",
                     help="directory to write the synthetic-derived clips into")
    syn.add_argument("--seed", type=int, default=1, metavar="N",
                     help="deterministic seed (default 1)")
    syn.add_argument("--format", default="text", choices=["text", "json"],
                     help="output format (default text)")
    syn.set_defaults(func=_cmd_synth)

    # --- trust: input-health check (is this recording even scorable?) --------
    tr = sub.add_parser(
        "trust",
        help="check whether a recording is scorable before you scan it "
             "(input health only; never a turn-taking verdict)",
        description=(
            "The input-health check, or 'trust doctor': inspect ONE recording "
            "and report whether the audio is good enough to score, so a bad "
            "export is caught before it produces a confident but meaningless "
            "verdict. Reports per-channel activity (caller expected on channel "
            "0, agent on channel 1), a possible channel-swap flag, sample rate, "
            "duration, clipping, leading silence, crosstalk risk, and the three "
            "scorability checks (separated tracks, enough caller activity, "
            "enough agent activity), then recommends 'eligible for scan' or 'NOT "
            "SCORABLE' with the specific reason AND the next step. It NEVER "
            "labels intent and NEVER emits a yield/hold or pass/fail verdict: "
            "that is what `hotato scan` / `hotato run` are for. Offline; no "
            "accuracy percentage anywhere."
        ),
        epilog=(
            _exit_codes_epilog("trust") + "\n\n"
            "Examples:\n"
            "  hotato trust --stereo call.wav\n"
            "  hotato trust --stereo call.wav --format json\n"
            "  hotato trust --stereo call.wav && hotato scan --stereo call.wav"
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    tr.add_argument("--stereo", required=True, metavar="WAV",
                    help="two-channel WAV (caller on one channel, agent on the "
                         "other); with --diarize, a single-channel (mono) WAV whose "
                         "separability is reported instead")
    tr.add_argument("--diarize", action="store_true",
                    help="for a MONO file: report whether it is confidently "
                         "SEPARABLE into caller/agent (a high/low/refuse tier) via "
                         "the opt-in [diarize] front-end, so you know before "
                         "scoring whether a diarized-mono verdict would be confident "
                         "or only indicative. Still never a turn-taking verdict.")
    tr.add_argument("--diarizer", default="pyannote",
                    choices=["pyannote", "sortformer", "pyannoteai"],
                    help="diarizer backend for --diarize (default pyannote, local)")
    tr.add_argument("--egress-opt-in", action="store_true",
                    help="permit the HOSTED --diarizer pyannoteai to upload audio "
                         "off this machine")
    tr.add_argument("--caller-channel", type=int, default=0)
    tr.add_argument("--agent-channel", type=int, default=1)
    tr.add_argument("--format", default="text", choices=["text", "json"],
                    help="output format (default text; json for agents)")
    tr.set_defaults(func=_cmd_trust)

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

    # --- apply: the guarded, CLONE-ONLY staged apply ------------------------
    ap = sub.add_parser(
        "apply",
        help="apply a patch to a fresh STAGING clone only (never the source); "
             "dry run by default, refuses the both-axes threshold funnel",
        description=(
            "The most conservative rung of the fix ladder, and the only command "
            "that can mutate external platform state. Read a hotato patch "
            "artifact and either PRINT the fresh staging clone it WOULD create "
            "(the default dry run, fully offline) or, only with --yes and "
            "credentials, create a NEW staging assistant that is the source "
            "config with the patch applied. Five hard rules hold by "
            "construction: (1) CLONE-ONLY -- there is no production-apply path; "
            "a non---clone invocation errors, and nothing here ever PUTs/PATCHes "
            "the source (the one writing call is a POST that creates a NEW "
            "assistant). (2) REFUSAL-FIRST -- a both-axes threshold-funnel patch "
            "(do_not_tune_single_threshold) is REFUSED before anything, with the "
            "exact vendor-neutral engagement-control recommendation, and a "
            "distinct exit code; the refusal is the feature. (3) OPPOSITE-RISK "
            "REQUIRED -- apply refuses unless --battery carries BOTH a yield and "
            "a hold fixture, so a fix is never applied blind. (4) GATED SIDE "
            "EFFECT -- the default dry run prints exactly the clone it would "
            "create and touches no network; only --yes with credentials calls "
            "the platform (create_clone is the only networked function: it reads "
            "the source, applies the patch to a copy, and creates a NEW "
            "assistant). (5) NAME REQUIRED -- the clone must be named explicitly. "
            "Clone-appliable stacks: vapi, retell (their config is a REST "
            "assistant); LiveKit/Pipecat keep config in source, so apply points "
            "you at the source edit from hotato patch instead."
        ),
        epilog=(
            _exit_codes_epilog("apply") + "\n\n"
            "Examples:\n"
            "  hotato patch fixplan.json --format json --out patch.json\n"
            "  # dry run: prints the clone it WOULD create, creates nothing\n"
            "  hotato apply patch.json --clone --name staging-refund-fix \\\n"
            "      --battery tests/hotato\n"
            "  # actually create the staging clone (needs credentials)\n"
            "  hotato apply patch.json --clone --name staging-refund-fix \\\n"
            "      --battery tests/hotato --yes\n\n"
            "Then re-capture the battery through the CLONE and prove it:\n"
            "  hotato verify --before before/ --after after/ --policy hotato.verify.yaml"
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    ap.add_argument("patch_json", metavar="PATCH_JSON",
                    help="a hotato patch artifact written with "
                         "hotato patch --format json --out patch.json")
    ap.add_argument("--clone", action="store_true",
                    help="REQUIRED: apply to a fresh staging clone. There is no "
                         "production-apply path; without --clone this errors")
    ap.add_argument("--name", default=None, metavar="NAME",
                    help="name for the NEW staging assistant to create "
                         "(required)")
    ap.add_argument("--battery", default=None, metavar="DIR",
                    help="an opposite-risk battery (a fixtures dir with "
                         "scenarios/, or run-envelope/scenario JSONs) that "
                         "carries BOTH a yield and a hold fixture; apply refuses "
                         "without it so a fix is never applied blind")
    ap.add_argument("--yes", action="store_true",
                    help="actually create the staging clone through the "
                         "platform API (needs credentials); without it this is "
                         "a dry run that prints the clone it would create and "
                         "touches no network")
    ap.add_argument("--stack", default=None,
                    choices=["vapi", "retell"],
                    help="override the clone stack (default: the patch's stack); "
                         "used to resolve credentials under --yes")
    ap.add_argument("--api-key", default=None,
                    help="platform API key for --yes (else the connection or the "
                         "stack's env var, e.g. VAPI_API_KEY / RETELL_API_KEY)")
    ap.add_argument("--format", default="text", choices=["json", "text"],
                    help="output format (default text)")
    ap.set_defaults(func=_cmd_apply)

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
            "  hotato verify --before before/ --after after/ --min-n 5 --fail-on-regression\n"
            "  hotato verify --before before.json --after after.json --out verify.html\n"
            "  hotato verify --before before.json --after after.json --policy hotato.verify.yaml"
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
    vf.add_argument("--policy", default=None, metavar="hotato.verify.yaml",
                    help="gate the run against a hotato.verify.yaml policy: "
                         "target.improve success criteria (e.g. talk_over_sec_p95 "
                         "-0.5, failed_count decrease) AND hard guardrails "
                         "(max_new_false_yields, max_not_scorable, "
                         "require_hold_fixture, require_yield_fixture). verify "
                         "exits 1 unless every guardrail holds and every target "
                         "is met, so a one-axis bandaid cannot pass")
    vf.add_argument("--format", default="text", choices=["json", "text"],
                    help="output format (default text)")
    vf.add_argument("--out", default=None, metavar="PATH",
                    help="also write the proof here: a .html/.htm path writes a "
                         "self-contained offline before/after report (headline "
                         "PASSED/FAILED, target talk-over shift, opposite-risk "
                         "false-yield check); any other path writes the full "
                         "proof JSON")
    vf.set_defaults(func=_cmd_verify)

    # --- fix: compose apply's gate + verify + contract verify + explain -----
    fxt = sub.add_parser(
        "fix",
        help="prove a candidate fix end to end, fail-closed (hotato fix "
             "trial)",
        description=(
            "Compose the shipped, already-guarded primitives into ONE "
            "before/after proof that a candidate change actually holds "
            "(see hotato fix trial --help)."
        ),
        epilog=_exit_codes_epilog("fix"),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    fxtsub = fxt.add_subparsers(dest="fix_command", required=True,
                                metavar="trial")
    ft = fxtsub.add_parser(
        "trial",
        help="S4: apply's offline gate + verify's battery-scale rollup + "
             "contract verify + explain, one before/after report, "
             "fail-closed",
        description=(
            "hotato fix trial evaluates a hotato patch artifact through the "
            "EXACT SAME offline gate hotato apply --clone already enforces "
            "(refusal-first on the both-axes threshold funnel, "
            "opposite-risk-battery-required, clone-only -- it never creates "
            "a clone itself and never touches the network, the same "
            "guarantee apply's own dry run gives, by construction), then "
            "scores the previously-failing BEFORE run against the AFTER run "
            "you re-captured through the clone with hotato verify (every "
            "paired fixture in the battery, not just the target one -- the "
            "'neighbouring cases' check), re-verifies any --contracts "
            "directory against its own recorded policy, and folds in "
            "hotato explain's root-cause attribution for the ORIGINAL "
            "failure as the report's attribution section. It adds no new "
            "scoring engine: every number here is one apply/verify/contract "
            "verify/explain already measures. The verdict is FAIL-CLOSED: "
            "'improved' requires the verify claim to be supported (>= "
            "--min-n previously-failing fixtures), at least one to now "
            "pass, NO regression anywhere in the battery (including the "
            "hold/opposite-risk axis), no contract regression, and no "
            "--policy violation. A low-n or zero-improvement result is "
            "'inconclusive', not a soft pass; any regression is "
            "'regressed'. Both exit the same non-zero code, so CI never "
            "treats 'we could not tell' as green."
        ),
        epilog=(
            _exit_codes_epilog("fix trial") + "\n\n"
            "Examples:\n"
            "  hotato patch fixplan.json --format json --out patch.json\n"
            "  hotato apply patch.json --clone --name staging-refund-fix \\\n"
            "      --battery tests/hotato\n"
            "  # ... re-capture the battery through the source (before/) and\n"
            "  # the clone (after/) ...\n"
            "  hotato fix trial patch.json --name staging-refund-fix \\\n"
            "      --before before/ --after after/ --battery tests/hotato \\\n"
            "      --policy hotato.verify.yaml --out fix-trial.json \\\n"
            "      --html fix-trial.html\n"
            "  hotato fix trial patch.json --name staging-refund-fix \\\n"
            "      --before before/ --after after/ --contracts contracts/"
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    ft.add_argument("patch_json", metavar="PATCH_JSON",
                    help="a hotato patch artifact from hotato patch "
                         "--format json --out patch.json")
    ft.add_argument("--name", default=None, metavar="NAME",
                    help="name of the staging clone this trial is proving "
                         "(required for a non-refused patch; same as hotato "
                         "apply --name -- fix trial evaluates the SAME "
                         "clone-only gate, it never creates a clone itself)")
    ft.add_argument("--before", required=True, metavar="RUN.json|DIR",
                    help="the OLD run envelope(s): the original failure "
                         "evidence, captured through the SOURCE (also the "
                         "default opposite-risk --battery, and the "
                         "attribution source, when those are omitted)")
    ft.add_argument("--after", required=True, metavar="RUN.json|DIR",
                    help="the NEW run envelope(s), re-captured through the "
                         "staging CLONE after the patch was applied there")
    ft.add_argument("--battery", default=None, metavar="DIR",
                    help="the opposite-risk battery apply's gate checks "
                         "(BOTH a yield and a hold fixture); defaults to "
                         "--before, which already carries the labels")
    ft.add_argument("--contracts", default=None, metavar="DIR",
                    help="also re-verify a directory of hotato contracts "
                         "(the neighbouring-cases check); any contract "
                         "regression fails the trial")
    ft.add_argument("--policy", default=None, metavar="hotato.verify.yaml",
                    help="gate verify's rollup against a hotato.verify.yaml "
                         "policy (target.improve + guardrails); a violation "
                         "fails the trial")
    ft.add_argument("--min-n", type=int, default=3,
                    help="minimum previously-failing fixtures needed to "
                         "support the claim (default 3); below it the "
                         "trial is inconclusive, not a pass")
    ft.add_argument("--format", default="text", choices=["json", "text"],
                    help="output format (default text)")
    ft.add_argument("--out", default=None, metavar="PATH",
                    help="also write the full proof JSON here")
    ft.add_argument("--html", default=None, metavar="PATH",
                    help="also write a self-contained before/after HTML "
                         "report (verdict, verify proof, contract check, "
                         "and the attribution section)")
    ft.set_defaults(func=_cmd_fix_trial)

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

    # --- investigate: one call-id (or local WAV) -> ranked candidates + the -
    # --- exact command to label each one into a signed, CI-ready contract ---
    iv = sub.add_parser(
        "investigate",
        help="pull/open one recording, authenticate its capture origin, run "
             "the K6 trust gate, and rank its candidate moments -- with the "
             "exact command to label each one into a contract",
        description=(
            "One recording in, ranked candidate turn-taking moments out: "
            "opens a local dual-channel WAV, or pulls one live from a "
            "connected stack (--stack/--call-id, reusing the same fetch "
            "`hotato pull` uses), authenticates its capture origin (a "
            "previously-frozen fixture clip, a fetch from the stack's own "
            "API for a named call id, or an operator-asserted local file), "
            "runs the input-health / K6 verdict-eligibility gate (`hotato "
            "trust`, contract mode), and scans it for candidate moments "
            "(`hotato scan`). Discovery only: no intent is inferred and no "
            "verdict or label is ever fabricated here -- a suspected channel "
            "swap or crosstalk REFUSES the verdict path (advisory candidates "
            "are still shown) until you confirm the mapping or fix it. State "
            "persists to .hotato/investigate-state.json (also a valid "
            "FILE#N candidate ref for `hotato fixture promote` / `hotato "
            "contract create --from-candidate`)."
        ),
        epilog=(
            _exit_codes_epilog("investigate") + "\n\n" + _LABEL_NOTE + "\n\n"
            "Examples:\n"
            "  hotato investigate call.wav\n"
            "  hotato investigate --stack vapi --call-id abc123\n"
            "  hotato investigate label .hotato/investigate-state.json#1 \\\n"
            "      --expect yield"
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    iv.add_argument("source", nargs="?", default=None, metavar="SOURCE",
                    help="a local dual-channel WAV path (omit when using "
                         "--stack/--call-id)")
    iv.add_argument("--stack", default=None, choices=list(_capture.PULL_STACKS),
                    help="pull the recording from this connected stack "
                         "instead of a local SOURCE")
    iv.add_argument("--call-id", default=None, metavar="ID",
                    help="the call/recording id to pull (with --stack); for "
                         "twilio pass a Recording SID (RE...)")
    _add_cred_args(iv)
    iv.add_argument("--allow-mono", action="store_true",
                    help="allow pulling a mono/mixed stack recording "
                         "(degraded; it will not be candidate-eligible for "
                         "separated scoring)")
    iv.add_argument("--caller-channel", type=int, default=0)
    iv.add_argument("--agent-channel", type=int, default=1)
    iv.add_argument("--min-gap", type=float, default=2.0,
                    help="minimum response gap in seconds to surface as a "
                         "candidate (default 2.0)")
    iv.add_argument("--top", type=int, default=10,
                    help="how many top candidate moments to show and print "
                         "label commands for (default 10; 0 shows all)")
    iv.add_argument("--state", default=None, metavar="PATH",
                    help="investigate state file (default "
                         ".hotato/investigate-state.json in the current "
                         "directory)")
    iv.add_argument("--confirm-channels", action="store_true",
                    help="human confirmation that the caller/agent channel "
                         "mapping is correct despite a suspected swap or "
                         "crosstalk/leakage; without it the verdict path is "
                         "refused (K6), though candidates are still shown")
    iv.add_argument("--format", default="text", choices=["json", "text"],
                    help="output format (default text)")
    iv.set_defaults(func=_cmd_investigate)

    ivl = sub.add_parser(
        "investigate label",
        help="label one hotato investigate candidate into a signed, "
             "CI-ready contract (this IS the human yield/hold decision)",
        description=(
            "The human's yield/hold decision for one `hotato investigate` "
            "candidate ref. Wraps `hotato contract create --from-candidate` "
            "exactly (same round-trip scorability guarantee, same signed "
            "label-record when a signing key is configured): nothing here "
            "fabricates a label or a verdict."
        ),
        epilog=(
            _exit_codes_epilog("investigate label") + "\n\n" + _LABEL_NOTE
            + "\n\n"
            "Examples:\n"
            "  hotato investigate label .hotato/investigate-state.json#1 \\\n"
            "      --expect yield\n"
            "  hotato investigate label .hotato/investigate-state.json#2 \\\n"
            "      --expect hold --id refund-backchannel-002 --out contracts"
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    ivl.add_argument("ref", metavar="CANDIDATE_REF",
                     help="a candidate ref printed by hotato investigate "
                          "(STATE_FILE#N)")
    ivl.add_argument("--expect", required=True, choices=["yield", "hold"],
                     help="YOUR label for the event: 'yield' (the agent "
                          "should stop for the caller) or 'hold' (the agent "
                          "should keep speaking)")
    ivl.add_argument("--id", default=None,
                     help="contract id slug (default: derived from the "
                          "candidate's source, onset, and label)")
    ivl.add_argument("--out", default="contracts", metavar="DIR",
                     help="contract root; writes DIR/<id>.hotato/ (default "
                          "contracts)")
    ivl.add_argument("--folder", default=None, metavar="DIR",
                     help="folder holding the investigated recording, when "
                          "the path recorded in the state file does not "
                          "resolve from here")
    ivl.add_argument("--stack", default="generic",
                     choices=["generic", "vapi", "twilio", "livekit",
                              "pipecat", "retell"],
                     help="voice stack the recording came from")
    ivl.add_argument("--max-talk-over", type=float, default=None,
                     help="[yield] the contract's policy fails if talk-over "
                          "exceeds this many seconds")
    ivl.add_argument("--max-time-to-yield", type=float, default=None,
                     help="[yield] the contract's policy fails if the yield "
                          "is slower than this many seconds")
    ivl.add_argument("--rationale", default=None,
                     help="optional free-text note on why you labeled the "
                          "event this way")
    ivl.add_argument("--reviewer", default=None, metavar="NAME",
                     help="your name/identity, bound into the signed "
                          "label-record and the contract's identity.reviewer "
                          "(default: HOTATO_REVIEWER env var, then USER/"
                          "USERNAME)")
    ivl.add_argument("--pre", type=float, default=2.0,
                     help="seconds of audio kept BEFORE the onset when "
                          "clipping (default 2.0)")
    ivl.add_argument("--post", type=float, default=6.0,
                     help="seconds of audio kept AFTER the onset when "
                          "clipping (default 6.0)")
    ivl.add_argument("--no-clip", action="store_true",
                     help="keep the full recording and the original onset "
                          "instead of clipping")
    ivl.add_argument("--force", action="store_true",
                     help="overwrite an existing contract with the same id")
    ivl.add_argument("--caller-channel", type=int, default=0)
    ivl.add_argument("--agent-channel", type=int, default=1)
    ivl.add_argument("--confirm-channels", action="store_true",
                     help="human confirmation that the caller/agent channel "
                          "mapping is correct despite a suspected swap or "
                          "crosstalk/leakage; without it the contract's "
                          "verdict is withheld and `contract verify` "
                          "REFUSES it")
    ivl.add_argument("--include-identifiers", action="store_true",
                     help="show the source recording's basename / candidate "
                          "ref in the bundle and the card instead of "
                          "redacting them (default: redacted)")
    ivl.add_argument("--format", default="text", choices=["text", "json"],
                     help="output format (default text)")
    ivl.set_defaults(func=_cmd_investigate_label)

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

    # --- init: scaffold a self-hostable integration (webhook worker) ---------
    from . import initcmd as _initcmd
    from . import issuecmd as _issuecmd

    it = sub.add_parser(
        "init",
        help="scaffold a hotato integration or a whole-repo starter kit "
             "(hotato init webhook | hotato init starter)",
        description=(
            "Scaffolding for adding hotato to a voice-agent repository: a "
            "passive webhook worker (see hotato init webhook --help) or a "
            "whole-repo starter kit -- CI gate, hotato.yaml, fixtures/, "
            "contracts/, reports/ (see hotato init starter --help)."
        ),
        epilog=_exit_codes_epilog("init"),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    itsub = it.add_subparsers(dest="init_command", required=True,
                              metavar="webhook|starter")
    iw = itsub.add_parser(
        "webhook",
        help="generate a ready-to-deploy webhook worker that verifies the "
             "webhook, fetches the recording read-only, and scans for "
             "candidate moments",
        description=(
            "Generate a small, self-hostable webhook worker that turns a voice "
            "platform's call-ended webhook into a passive turn-taking "
            "regression monitor. The worker verifies the webhook secret, then "
            "hands the payload to `hotato ingest` -- the same composable "
            "primitive -- which fetches the dual-channel recording READ-ONLY "
            "and scans it for CANDIDATE moments; it adds no vendor call of its "
            "own. It NEVER calls a platform config-mutation endpoint and NEVER "
            "labels intent or emits a verdict: discovery only. It writes a "
            "candidate report and, when configured, posts a Slack summary "
            "and/or a GitHub notification (both off by default; it opens no "
            "GitHub issue unless you explicitly turn that on). The scaffold "
            "writes README.md, hotato.yaml, app.py, requirements.txt, "
            "Dockerfile, .env.example, .github/workflows/deploy.yml, and "
            "tests/test_webhook_contract.py -- a contract test that pins the "
            "four invariants above. Offline scaffolding: no network, no "
            "credentials needed to generate."
        ),
        epilog=(
            _exit_codes_epilog("init webhook") + "\n\n"
            "Examples:\n"
            "  hotato init webhook --stack vapi   --target fastapi --out hotato-webhook\n"
            "  hotato init webhook --stack retell --target fastapi --out ./worker\n"
            "  hotato init webhook --stack twilio --target fastapi --out ./worker --force\n\n"
            "Then, in the generated project:\n"
            "  pytest -q tests/test_webhook_contract.py   # the four invariants\n"
            "  uvicorn app:app --reload                   # POST /webhook"
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    iw.add_argument("--stack", required=True, choices=list(_initcmd.WEBHOOK_STACKS),
                    help="voice stack whose webhook the worker receives")
    iw.add_argument("--target", default="fastapi", choices=list(_initcmd.TARGETS),
                    help="worker framework to generate (default fastapi)")
    iw.add_argument("--out", required=True, metavar="DIR",
                    help="directory to scaffold the worker into")
    iw.add_argument("--force", action="store_true",
                    help="overwrite existing files in --out")
    iw.add_argument("--format", default="text", choices=["text", "json"],
                    help="output format (default text)")
    iw.set_defaults(func=_cmd_init_webhook)

    ist = itsub.add_parser(
        "starter",
        help="generate a whole-repo starter kit: CI gate, hotato.yaml, "
             "fixtures/, contracts/, reports/",
        description=(
            "Generate a whole-repo starter kit for adding hotato to an "
            "EXISTING voice-agent repository (pass --out .): a GitHub "
            "Actions workflow that verifies contracts/ and fixtures/ on "
            "push, pull request, and weekly (a no-op, never a failure, "
            "until you have added a first one); a hotato.yaml config "
            "skeleton tuned for the stack (credential env var names for an "
            "auto-pull stack, or a plain no-credentials-needed note for a "
            "capture-in-your-infra stack); fixtures/, contracts/, and "
            "reports/ directories with README stubs; and .gitignore entries "
            "that exclude local/pulled recordings while keeping pinned "
            "fixture and contract audio clips committed. Generated files "
            "are deliberately namespaced (HOTATO.md, not README.md; "
            "hotato-contracts.yml, not hotato.yml) so a first run does not "
            "collide with files a real repo almost always already has. "
            "Offline scaffolding: no network, no credentials needed to "
            "generate. Every stack referenced here ships a real hotato "
            "connector today (see docs/ADAPTER-STATUS.md); vapi/retell/"
            "twilio auto-pull the recording, livekit/pipecat are capture-"
            "in-your-infra."
        ),
        epilog=(
            _exit_codes_epilog("init starter") + "\n\n"
            "Examples:\n"
            "  hotato init starter --stack vapi    --out .\n"
            "  hotato init starter --stack livekit --out ./my-agent-repo\n"
            "  hotato init starter --stack pipecat --out . --force\n\n"
            "Then:\n"
            "  cat HOTATO.md                                # what was added, next steps\n"
            "  hotato contract create --stereo call.wav --onset 42.18 \\\n"
            "      --expect yield --id refund-cutoff-001 --out contracts\n"
            "  hotato contract verify contracts --junit hotato.xml   # the CI gate, locally"
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    ist.add_argument("--stack", required=True, choices=list(_initcmd.STARTER_STACKS),
                     help="voice stack this repo runs on")
    ist.add_argument("--out", required=True, metavar="DIR",
                     help="directory to scaffold the starter kit into "
                          "(often . -- your existing repo root)")
    ist.add_argument("--force", action="store_true",
                     help="overwrite existing files in --out")
    ist.add_argument("--format", default="text", choices=["text", "json"],
                     help="output format (default text)")
    ist.set_defaults(func=_cmd_init_starter)

    # --- issue: file a sweep's candidates as a GitHub issue -----------------
    iss = sub.add_parser(
        "issue",
        help="file a sweep's candidate moments as a GitHub issue "
             "(hotato issue create)",
        description=(
            "Turn a sweep result into a GitHub issue that asks a human to "
            "confirm or ignore each candidate (see hotato issue create "
            "--help)."
        ),
        epilog=_exit_codes_epilog("issue"),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    isssub = iss.add_subparsers(dest="issue_command", required=True,
                                metavar="create")
    ic = isssub.add_parser(
        "create",
        help="render a GitHub issue from a sweep/analyze result: a worst-"
             "candidate block and a confirm-or-ignore promote command per "
             "top candidate (dry run by default; --yes creates it via gh)",
        description=(
            "Render a GitHub issue from a `hotato sweep --format json` (or "
            "`hotato analyze --format json`) result. The body carries a "
            "worst-candidate block (call id, time, kind, the measured number, "
            "the report it came from) and, for each of the top --top "
            "candidates, a confirm-or-ignore section with the exact `hotato "
            "fixture promote FILE#N` command for BOTH a yield and a hold label "
            "and a close-it line for when the moment is not a turn-taking "
            "failure. These are MEASURED CANDIDATE moments, never verdicts and "
            "never intent. The DEFAULT is a dry run: it prints the body and "
            "the exact `gh issue create` command and creates nothing. Only "
            "`--yes` with an explicit `--repo` actually opens the issue "
            "(through `gh`), mirroring the project default that Hotato never "
            "files an issue on your behalf unless you ask. Offline rendering; "
            "no audio and no network on the dry-run path."
        ),
        epilog=(
            _exit_codes_epilog("issue create") + "\n\n"
            "Examples:\n"
            "  hotato sweep --demo --format json > hotato-sweep.json\n"
            "  hotato issue create hotato-sweep.json --repo owner/repo --top 3\n"
            "  hotato issue create hotato-sweep.json --repo owner/repo \\\n"
            "      --label turn-taking --label regression --yes\n\n"
            "Requires the GitHub CLI (`gh`) authenticated only for --yes."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    ic.add_argument("sweep_json", metavar="SWEEP_JSON",
                    help="a hotato sweep/analyze result written with "
                         "--format json")
    ic.add_argument("--repo", default=None, metavar="OWNER/REPO",
                    help="the GitHub repository to file the issue in "
                         "(required)")
    ic.add_argument("--top", type=int, default=_issuecmd.DEFAULT_TOP,
                    metavar="N",
                    help="how many top-ranked candidates to include "
                         f"(default {_issuecmd.DEFAULT_TOP}; 0 for all)")
    ic.add_argument("--label", action="append", default=None, metavar="LABEL",
                    help="a GitHub label to apply (repeat for several)")
    ic.add_argument("--yes", action="store_true",
                    help="actually create the issue through gh; without it "
                         "this is a dry run that prints the body and the "
                         "command and creates nothing")
    ic.add_argument("--format", default="text", choices=["text", "json"],
                    help="output format (default text)")
    ic.set_defaults(func=_cmd_issue_create)

    # --- pr: open a pull request that adds promoted fixtures ----------------
    prp = sub.add_parser(
        "pr",
        help="open a pull request that adds promoted fixtures "
             "(hotato pr create)",
        description=(
            "Turn a directory of promoted fixtures into a GitHub pull request "
            "that adds them as regression tests (see hotato pr create --help)."
        ),
        epilog=_exit_codes_epilog("pr"),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    prsub = prp.add_subparsers(dest="pr_command", required=True,
                               metavar="create")
    pc = prsub.add_parser(
        "create",
        help="render a pull request that adds a hotato fixtures directory: a "
             "line per fixture and the run command to score them (dry run by "
             "default; --yes cuts a branch, commits, pushes, and opens the PR "
             "via git and gh)",
        description=(
            "Render a GitHub pull request that adds a hotato fixtures "
            "directory (the --out DIR that `hotato fixture promote` wrote, with "
            "scenarios/ and audio/). The body lists each fixture (its id, the "
            "yield/hold label a maintainer chose, the call it was promoted "
            "from, and the onset) and carries the exact `hotato run` command "
            "that scores every added fixture. These are MEASURED CANDIDATE "
            "moments saved as tests, never verdicts and never intent. The "
            "DEFAULT is a dry run: it prints the body and the exact git and "
            "`gh pr create` commands and changes nothing. Only `--yes` with an "
            "explicit `--repo` actually cuts the branch, commits the fixtures, "
            "pushes, and opens the PR. Two invariants hold even under --yes: "
            "the change lands on a NEW feature branch (never the default branch "
            "directly) and the push is never a force-push. Offline rendering; "
            "no audio and no network on the dry-run path."
        ),
        epilog=(
            _exit_codes_epilog("pr create") + "\n\n"
            "Examples:\n"
            "  hotato pr create --fixtures tests/hotato --repo owner/repo \\\n"
            "      --title 'Add turn-taking regression fixtures'\n"
            "  hotato pr create --fixtures tests/hotato --repo owner/repo \\\n"
            "      --title 'Add turn-taking regression fixtures' \\\n"
            "      --branch hotato/turn-taking-fixtures --base main --yes\n\n"
            "Requires git and the GitHub CLI (`gh`) authenticated only for "
            "--yes."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    pc.add_argument("--fixtures", default=None, metavar="DIR",
                    help="the hotato fixtures directory to add (scenarios/ and "
                         "audio/), e.g. tests/hotato (required)")
    pc.add_argument("--repo", default=None, metavar="OWNER/REPO",
                    help="the GitHub repository to open the pull request in "
                         "(required)")
    pc.add_argument("--title", default=None, metavar="TITLE",
                    help="the pull request title (required)")
    pc.add_argument("--branch", default=None, metavar="NAME",
                    help="the feature branch to cut and push (default "
                         "hotato/<title slug>); never the default branch")
    pc.add_argument("--base", default=None, metavar="BRANCH",
                    help="the base branch the PR targets (default: the repo "
                         "default gh picks)")
    pc.add_argument("--yes", action="store_true",
                    help="actually cut the branch, commit the fixtures, push, "
                         "and open the PR through git and gh; without it this "
                         "is a dry run that prints the body and the exact "
                         "commands and changes nothing")
    pc.add_argument("--format", default="text", choices=["text", "json"],
                    help="output format (default text)")
    pc.set_defaults(func=_cmd_pr_create)

    # --- fleet: the local, self-hosted Guardian control plane ----------------
    def _fleet_common(sp):
        """Add --home / --workspace|-w / --format to a fleet LEAF parser."""
        sp.add_argument("--home", default=None, metavar="DIR",
                        help="fleet control-plane home (default ~/.hotato/fleet)")
        sp.add_argument("--workspace", "-w", default="default", metavar="ID",
                        help="workspace id (default 'default')")
        sp.add_argument("--format", default="text", choices=["text", "json"],
                        help="output format (default text)")

    def _fleet_parser(parent, name, dotted, help_text):
        """Add a fleet subparser carrying the uniform ``Exit codes:`` epilog
        (templated from the single ``_EXIT_CODES`` source of truth, like every
        other subparser)."""
        return parent.add_parser(
            name, help=help_text,
            epilog=_exit_codes_epilog(dotted),
            formatter_class=argparse.RawDescriptionHelpFormatter,
        )

    fl = sub.add_parser(
        "fleet",
        help="the local Guardian control plane over the evidence kernel "
             "(hotato fleet init/agent/ingest/discover/review/label/status/"
             "experiment/canary/export/trend)",
        description=(
            "A private, self-hosted control plane that registers voice "
            "agents (no product-level cap), ingests completed calls, "
            "discovers candidate turn-taking failures, holds a human review "
            "queue, records human labels, and runs manifest-bound before/"
            "after experiments -- always RECOMMENDING, never auto-labeling "
            "and never auto-deploying in this release. Local mode is "
            "zero-dependency (a SQLite registry + a content-addressed "
            "artifact store under --home, default ~/.hotato/fleet). See "
            "plan section 16."
        ),
        epilog=_exit_codes_epilog("fleet"),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    flsub = fl.add_subparsers(
        dest="fleet_command", required=True,
        metavar="init|agent|ingest|discover|review|label|status|experiment|canary|export|trend|run|contract|retention|delete|redact")

    fi = _fleet_parser(flsub, "init", "fleet init",
                       "create/ensure a workspace in the fleet home")
    _fleet_common(fi)
    fi.add_argument("--name", default=None, help="optional human name for the "
                                                 "workspace")
    fi.set_defaults(func=_cmd_fleet_init)

    fa = _fleet_parser(flsub, "agent", "fleet agent",
                       "register and list voice agents (hotato fleet agent "
                       "add/list)")
    fasub = fa.add_subparsers(dest="fleet_agent_command", required=True,
                              metavar="add|list")
    faa = _fleet_parser(fasub, "add", "fleet agent add",
                        "register a voice agent in the workspace")
    _fleet_common(faa)
    faa.add_argument("--agent-id", "--name", dest="agent_id", required=True,
                     metavar="ID", help="the agent id/name to register")
    faa.add_argument("--stack", required=True,
                     choices=["vapi", "retell", "livekit", "pipecat", "twilio"],
                     help="the voice stack this agent runs on")
    faa.add_argument("--connection", default=None, metavar="ID",
                     help="optional stored connection id for this stack")
    faa.add_argument("--external-ref", "--assistant-id", dest="external_ref",
                     default=None, metavar="REF",
                     help="the provider-side reference, e.g. a vapi assistant id")
    faa.set_defaults(func=_cmd_fleet_agent_add)
    fal = _fleet_parser(fasub, "list", "fleet agent list",
                        "list registered agents (agent_id, stack, external_ref)")
    _fleet_common(fal)
    fal.set_defaults(func=_cmd_fleet_agent_list)

    fin = _fleet_parser(flsub, "ingest", "fleet ingest",
                        "register a completed call's recording (idempotent on "
                        "call id)")
    _fleet_common(fin)
    fin.add_argument("--agent", required=True, metavar="ID",
                     help="the agent id the call belongs to")
    fin.add_argument("--call-id", default=None, metavar="ID",
                     help="optional stable call id (default derived from the "
                          "decoded PCM, so duplicate pulls dedupe)")
    fin.add_argument("call_wav", metavar="CALL_WAV",
                     help="two-channel recording (caller ch0, agent ch1)")
    fin.set_defaults(func=_cmd_fleet_ingest)

    fd = _fleet_parser(flsub, "discover", "fleet discover",
                       "scan a recording for candidate turn-taking moments")
    _fleet_common(fd)
    fd.add_argument("--agent", required=True, metavar="ID",
                    help="the agent id the recording belongs to")
    fd.add_argument("call_wav", metavar="CALL_WAV",
                    help="two-channel recording (caller ch0, agent ch1)")
    fd.set_defaults(func=_cmd_fleet_discover)

    fr = _fleet_parser(flsub, "review", "fleet review",
                       "list candidates awaiting a human label")
    _fleet_common(fr)
    fr.add_argument("--agent", default=None, metavar="ID",
                    help="only candidates for this agent")
    fr.add_argument("--limit", type=int, default=5, metavar="N",
                    help="max candidates to list (default 5)")
    fr.set_defaults(func=_cmd_fleet_review)

    flb = _fleet_parser(flsub, "label", "fleet label",
                        "record a HUMAN label on a candidate")
    _fleet_common(flb)
    flb.add_argument("candidate_id", metavar="CANDIDATE_ID",
                     help="the candidate to label")
    flb.add_argument("--decision", required=True,
                     choices=["yield", "hold", "not_a_useful_event", "bad_input"],
                     help="your decision for the candidate")
    flb.add_argument("--reviewer", required=True, metavar="NAME",
                     help="who is applying the label")
    flb.add_argument("--rationale", default=None,
                     help="optional free-text note on the label")
    flb.set_defaults(func=_cmd_fleet_label)

    fst = _fleet_parser(flsub, "status", "fleet status",
                        "workspace counts + job-queue stats")
    _fleet_common(fst)
    fst.set_defaults(func=_cmd_fleet_status)

    fbe = _fleet_parser(flsub, "benchmark", "fleet benchmark",
                        "private per-agent comparison in this workspace (not a public leaderboard)")
    _fleet_common(fbe)
    fbe.add_argument("--min-tier", type=int, default=None,
                     help="exclude trials below this evidence tier (0..4)")
    fbe.set_defaults(func=_cmd_fleet_benchmark)

    fe = _fleet_parser(flsub, "experiment", "fleet experiment",
                       "run a manifest-bound before/after trial (hotato fleet "
                       "experiment run)")
    fesub = fe.add_subparsers(dest="fleet_experiment_command", required=True,
                              metavar="create|run")
    fec = _fleet_parser(fesub, "create", "fleet experiment create",
                        "precommit the trial manifest from the COMPLETE battery "
                        "BEFORE any after-side capture, so the pinned fixture "
                        "universe cannot be cherry-picked to the results")
    _fleet_common(fec)
    fec.add_argument("--agent", required=True, metavar="ID",
                     help="the agent under test")
    fec.add_argument("--trial-id", required=True, metavar="ID",
                     help="a stable id for this trial")
    fec.add_argument("--battery", required=True, metavar="BATTERY_RUN.json",
                     help="the COMPLETE battery run.json to pin now (a hotato suite result)")
    fec.add_argument("--min-n", type=int, default=1, metavar="N",
                     help="minimum paired fixtures required (default 1)")
    fec.add_argument("--policy", default=None, metavar="POLICY.json",
                     help="optional JSON policy "
                          "{max_talk_over_sec, max_time_to_yield_sec}")
    fec.set_defaults(func=_cmd_fleet_experiment_create)

    fer = _fleet_parser(fesub, "run", "fleet experiment run",
                        "recompute a before/after battery under an immutable "
                        "manifest and record a recommendation (never deploys)")
    _fleet_common(fer)
    fer.add_argument("--agent", required=True, metavar="ID",
                     help="the agent under test")
    fer.add_argument("--trial-id", required=True, metavar="ID",
                     help="a stable id for this trial")
    fer.add_argument("--battery", default=None, metavar="BATTERY_RUN.json",
                     help="the battery run.json envelope (required unless --manifest is given)")
    fer.add_argument("--manifest", default=None, metavar="DIGEST",
                     help="consume a manifest precommitted by `experiment create` "
                          "(its digest); the pinned universe was fixed before capture")
    fer.add_argument("--before", required=True, metavar="BEFORE_DIR",
                     help="a directory holding the BEFORE run.json + its wavs")
    fer.add_argument("--after", required=True, metavar="AFTER_DIR",
                     help="a directory holding the AFTER run.json + its wavs")
    fer.add_argument("--min-n", type=int, default=1, metavar="N",
                     help="minimum paired fixtures required (default 1)")
    fer.add_argument("--policy", default=None, metavar="POLICY.json",
                     help="optional JSON policy "
                          "{max_talk_over_sec, max_time_to_yield_sec}")
    fer.set_defaults(func=_cmd_fleet_experiment_run)

    fep = _fleet_parser(fesub, "propose", "fleet experiment propose",
                        "generate a bounded set of config variants (baseline + "
                        "lower/higher/adjacent/two-param, capped ~6) from the typed "
                        "catalogue, each with an expected-effects block; runs nothing")
    _fleet_common(fep)
    fep.add_argument("--agent", required=True, metavar="ID", help="the agent under test")
    fep.add_argument("--intent", required=True, metavar="INTENT",
                     help="the fix intent (a catalogue key, e.g. more_sensitive)")
    fep.add_argument("--current-config", default=None, metavar="CONFIG.json",
                     help="optional current turn-taking config JSON (from hotato inspect)")
    fep.add_argument("--max-variants", type=int, default=6, metavar="N",
                     help="cap on proposed variants (default 6)")
    fep.set_defaults(func=_cmd_fleet_experiment_propose)

    fea = _fleet_parser(fesub, "approve", "fleet experiment approve",
                        "record a HUMAN approval decision on a trial recommendation "
                        "(recorded only; never deploys in this release)")
    _fleet_common(fea)
    fea.add_argument("--trial-id", required=True, metavar="ID", help="the trial to approve")
    fea.add_argument("--approver", required=True, metavar="WHO",
                     help="the human approving (recorded in the decision)")
    fea.add_argument("--note", default=None, metavar="TEXT", help="optional approval note")
    fea.set_defaults(func=_cmd_fleet_experiment_approve)

    frun = _fleet_parser(flsub, "run", "fleet run",
                         "batch discovery: ingest + discover a set of dual-channel "
                         "recordings, cluster candidates across calls, and surface the "
                         "top-5 review queue (plan §9.1/§16). Never auto-labels")
    _fleet_common(frun)
    frun.add_argument("--agent", required=True, metavar="ID", help="the agent id")
    frun.add_argument("--recordings", nargs="*", default=None, metavar="WAV",
                      help="dual-channel recordings to ingest+discover (offline; live "
                           "pull from a connection needs credentials)")
    frun.add_argument("--caller-channel", type=int, default=0, metavar="N")
    frun.add_argument("--agent-channel", type=int, default=1, metavar="N")
    frun.add_argument("--notify", action="append", default=None, metavar="URL",
                      help="POST a JSON summary (counts, top candidate timing, "
                           "local artifact paths -- no audio, no credentials, "
                           "no transcript) to this webhook URL when the run "
                           "finishes; repeatable. Off by default; fails open "
                           "(a down webhook never breaks the run). "
                           "See docs/EGRESS.md")
    frun.set_defaults(func=_cmd_fleet_run)

    fct = _fleet_parser(flsub, "contract", "fleet contract",
                        "create a failure contract from a reviewed candidate "
                        "(hotato fleet contract create)")
    fctsub = fct.add_subparsers(dest="fleet_contract_command", required=True,
                                metavar="create")
    fctc = _fleet_parser(fctsub, "create", "fleet contract create",
                         "one-click: label a reviewed candidate and mint + register a "
                         "real failure contract from its recording (plan §9.2/§14)")
    _fleet_common(fctc)
    fctc.add_argument("--from-candidate", required=True, metavar="ID",
                      help="the reviewed candidate id to build the contract from")
    fctc.add_argument("--decision", required=True, choices=["yield", "hold"],
                      help="the HUMAN label for the moment")
    fctc.add_argument("--reviewer", required=True, metavar="WHO", help="the human reviewer")
    fctc.add_argument("--high-stakes", action="store_true",
                      help="mark this a high-stakes contract (stricter gates apply)")
    fctc.add_argument("--rationale", default=None, metavar="TEXT")
    fctc.add_argument("--max-talk-over-sec", type=float, default=None, metavar="S")
    fctc.add_argument("--max-time-to-yield-sec", type=float, default=None, metavar="S")
    fctc.set_defaults(func=_cmd_fleet_contract_create)

    fret = _fleet_parser(flsub, "retention", "fleet retention",
                         "attach a retention/consent policy to a recording (plan §14)")
    _fleet_common(fret)
    fret.add_argument("--recording-id", required=True, metavar="ID")
    fret.add_argument("--consent-basis", required=True, metavar="BASIS",
                      help="the lawful/consent basis for retaining this recording")
    fret.add_argument("--purpose", nargs="*", default=None, metavar="P",
                      help="allowed purposes")
    fret.add_argument("--retention-days", type=int, default=None, metavar="N")
    fret.add_argument("--pii-class", default="unknown", choices=["none", "pii", "phi", "unknown"])
    fret.add_argument("--legal-hold", action="store_true",
                      help="block expiry/deletion until the hold is lifted")
    fret.set_defaults(func=_cmd_fleet_retention)

    fdel = _fleet_parser(flsub, "delete", "fleet delete",
                         "delete a recording's stored audio, leaving a durable "
                         "deletion receipt (plan §14). A legal hold blocks it")
    _fleet_common(fdel)
    fdel.add_argument("--recording-id", required=True, metavar="ID")
    fdel.add_argument("--reason", required=True, metavar="TEXT")
    fdel.add_argument("--actor", required=True, metavar="WHO")
    fdel.set_defaults(func=_cmd_fleet_delete)

    frd = _fleet_parser(flsub, "redact", "fleet redact",
                        "produce a DERIVED redacted copy (silenced spans) with new "
                        "PCM hash + parent lineage; never the original evidence (§14)")
    _fleet_common(frd)
    frd.add_argument("--recording-id", required=True, metavar="ID")
    frd.add_argument("--span", nargs="*", default=None, metavar="START:END",
                     help="second spans to silence, e.g. 3.2:4.0 7.1:7.5")
    frd.add_argument("--actor", required=True, metavar="WHO")
    frd.set_defaults(func=_cmd_fleet_redact)

    fca = _fleet_parser(flsub, "canary", "fleet canary",
                        "canary routing (recommendation-only in this release; "
                        "hotato fleet canary start/rollback)")
    fcasub = fca.add_subparsers(dest="fleet_canary_command", required=True,
                                metavar="start|rollback")
    fcas = _fleet_parser(fcasub, "start", "fleet canary start",
                         "(disabled) start a canary rollout")
    _fleet_common(fcas)
    fcas.set_defaults(func=_cmd_fleet_canary)
    fcar = _fleet_parser(fcasub, "rollback", "fleet canary rollback",
                         "(disabled) roll a canary back")
    _fleet_common(fcar)
    fcar.set_defaults(func=_cmd_fleet_canary)

    fx = _fleet_parser(flsub, "export", "fleet export",
                       "dump status + a manifest of registered agents/trials")
    _fleet_common(fx)
    fx.add_argument("--out", default=None, metavar="DIR",
                    help="write DIR/fleet-export.json (default: print json to "
                         "stdout)")
    fx.set_defaults(func=_cmd_fleet_export)

    ft = _fleet_parser(flsub, "trend", "fleet trend",
                       "a self-contained HTML page of turn-taking trend "
                       "lines across every agent in the workspace")
    _fleet_common(ft)
    ft.add_argument("--out", default=None, metavar="FILE",
                    help="where to write the dashboard (default "
                         "hotato-fleet-trend.html)")
    ft.set_defaults(func=_cmd_fleet_trend)

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


def _route_investigate_label(argv) -> "list | None":
    """``hotato investigate label REF ...`` merges its first two tokens into
    the single subcommand name ``"investigate label"`` (registered with the
    literal space) before argparse ever sees them. ``investigate`` itself
    takes a positional SOURCE, and argparse cannot disambiguate an optional
    positional from a subparsers action sharing the same first token (both
    want it), so the merge happens here instead -- exactly the same
    argv-rewrite technique :func:`_route_bare_folder` already uses. Every
    other ``investigate ...`` invocation (including ``investigate --help``)
    is left untouched."""
    if len(argv) >= 2 and argv[0] == "investigate" and argv[1] == "label":
        return ["investigate label"] + list(argv[2:])
    return None


def main(argv=None) -> int:
    parser = build_parser()
    raw = sys.argv[1:] if argv is None else list(argv)
    raw = _route_investigate_label(raw) or raw
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
            print(_errors.safe_json_dumps(_errors.cli_error(exc), indent=2))
            return 2
        print(f"error: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
