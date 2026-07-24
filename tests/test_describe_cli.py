"""``hotato describe``: the generated capability manifest, and the uniform
``Exit codes:`` epilog every subparser now carries.

Machine-drivability surface: an agent should be able to learn the WHOLE CLI
(every subcommand, its args, its exit codes, the schema URLs) from one call
instead of scraping --help across ~18 subparsers. Pinned here:

  * `hotato describe --format json` emits one deterministic manifest that
    covers every subcommand (including the nested ``benchmark compare`` /
    ``fixture create``) and the two schema URLs;
  * `hotato describe --format text` is a readable summary of the same data;
  * every subparser's --help carries an "Exit codes:" line, templated from
    the single `_EXIT_CODES` source of truth so the epilog and the manifest
    can never say something different.
"""

import argparse
import json
from importlib import resources

from hotato import cli

# The full set of dotted subcommand names build_parser() defines today
# (top-level, plus the two nested ones). Kept explicit so a test failure
# names exactly what went missing, rather than just a count mismatch.
_ALL_SUBCOMMANDS = [
    "run", "capture", "drive", "setup", "connect", "pull", "sweep", "report", "team",
    "export", "benchmark", "benchmark compare",
    "bench", "bench run", "bench verify", "doctor", "demo", "start",
    "card", "diagnose",
    "inspect", "plan", "explain", "patch", "apply", "fixture", "fixture create",
    "fixture promote", "regression", "regression prepare",
    "counterexample", "counterexample compile", "counterexample verify",
    "counterexample reproduce", "counterexample inspect",
    "counterexample export", "counterexample predicate",
    "contract", "contract create", "contract verify",
    "contract inspect", "contract pack", "contract unpack",
    "trace", "trace ingest", "trace attach", "trace export",
    "observe", "observe capture", "observe cost", "observe percentiles",
    "observe report",
    "assert", "assert init", "assert run", "assert packs",
    "test", "test run",
    "suite", "suite run",
    "release", "release compare",
    "baseline", "baseline check",
    "record", "record render", "record verify",
    "rubric", "rubric run", "rubric calibrate",
    "scenario", "scenario init", "scenario validate",
    "conversation", "conversation verify",
    "simulate",
    "autopsy", "pin",
    "compare", "scan", "synth", "battery", "battery robustness",
    "gauntlet", "gauntlet badge", "trust",
    "ingest", "analyze", "verify", "fix", "fix trial", "prove",
    "candidate", "candidate hash", "candidate verify", "loop",
    "investigate", "investigate label", "describe",
    "init", "init webhook", "init starter", "init ci",
    "issue", "issue create",
    "pr", "pr create",
    "fleet", "fleet init", "fleet agent", "fleet agent add", "fleet agent list",
    "fleet ingest", "fleet discover", "fleet review", "fleet label", "fleet status",
    "fleet benchmark", "fleet experiment", "fleet experiment create", "fleet experiment run", "fleet experiment propose", "fleet experiment approve", "fleet run", "fleet contract", "fleet contract create", "fleet retention", "fleet delete", "fleet redact", "fleet canary", "fleet canary start",
    "fleet canary rollback", "fleet export", "fleet trend",
    "serve", "console",
    "telephony", "telephony capabilities", "telephony create",
    "telephony status", "telephony cancel", "telephony export",
    "caller", "caller run", "caller verify",
    "load", "load telephony", "load telephony run", "load telephony verify",
    "load caller", "load caller run", "load caller verify",
    "production", "production serve", "production ingest", "production status",
    "production finalize", "production maintain", "production alerts",
    "production export-regression", "production verify-regression",
    "production audit", "production delete",
]


def _iter_subparsers(parser, prefix=""):
    """Yield (dotted_name, subparser) for every subparser build_parser()
    defines, recursing into nested subparsers (benchmark compare, fixture
    create)."""
    for action in parser._actions:
        if isinstance(action, argparse._SubParsersAction):
            for name, sub in action.choices.items():
                full = f"{prefix} {name}".strip()
                yield full, sub
                yield from _iter_subparsers(sub, full)


# --- describe --format json ---------------------------------------------

def test_describe_json_covers_every_subcommand(capsys):
    code = cli.main(["describe", "--format", "json"])
    assert code == 0
    manifest = json.loads(capsys.readouterr().out)

    names = set()

    def _collect(cmds):
        for c in cmds:
            names.add(c["name"])
            _collect(c.get("subcommands", []))

    _collect(manifest["subcommands"])
    assert names == set(_ALL_SUBCOMMANDS)


def test_describe_json_top_level_fields(capsys):
    from hotato import __version__

    code = cli.main(["describe", "--format", "json"])
    assert code == 0
    manifest = json.loads(capsys.readouterr().out)
    assert manifest["tool"] == "hotato"
    assert manifest["version"] == __version__
    assert manifest["schemas"]["envelope"] == "https://hotato.dev/schema/envelope.v1.json"
    assert manifest["schemas"]["error"] == "https://hotato.dev/schema/error.v1.json"


def test_describe_json_schema_urls_match_the_shipped_schema_files(capsys):
    code = cli.main(["describe", "--format", "json"])
    manifest = json.loads(capsys.readouterr().out)

    def _schema_id(filename):
        return json.loads(
            resources.files("hotato").joinpath("schema", filename)
            .read_text(encoding="utf-8")
        )["$id"]

    assert manifest["schemas"]["envelope"] == _schema_id("envelope.v1.json")
    assert manifest["schemas"]["error"] == _schema_id("error.v1.json")


def test_describe_json_every_command_has_args_and_purpose(capsys):
    code = cli.main(["describe", "--format", "json"])
    manifest = json.loads(capsys.readouterr().out)

    def _check(cmds):
        for c in cmds:
            assert isinstance(c["args"], list)
            assert c["purpose"]  # non-empty
            for a in c["args"]:
                assert set(a) >= {"name", "type", "required", "default", "help"}
                assert a["type"] in ("str", "int", "float", "bool") or a["type"].startswith("list[")
            _check(c.get("subcommands", []))

    _check(manifest["subcommands"])


def test_describe_json_run_args_include_stereo_and_suite(capsys):
    code = cli.main(["describe", "--format", "json"])
    manifest = json.loads(capsys.readouterr().out)
    run = next(c for c in manifest["subcommands"] if c["name"] == "run")
    arg_names = {a["name"] for a in run["args"]}
    assert "--stereo" in arg_names
    assert "--suite" in arg_names
    stereo = next(a for a in run["args"] if a["name"] == "--stereo")
    assert stereo["required"] is False
    assert stereo["type"] == "str"


def test_describe_json_diagnose_surfaces_fleet_flag(capsys):
    cli.main(["describe", "--format", "json"])
    manifest = json.loads(capsys.readouterr().out)
    diagnose = next(c for c in manifest["subcommands"] if c["name"] == "diagnose")
    arg_names = {a["name"] for a in diagnose["args"]}
    assert "--fleet" in arg_names
    # The positional envelope is optional now (--fleet is the alternative).
    envelope = next(a for a in diagnose["args"] if a["name"] == "envelope")
    assert envelope["required"] is False


def test_describe_json_capture_stack_is_required(capsys):
    code = cli.main(["describe", "--format", "json"])
    manifest = json.loads(capsys.readouterr().out)
    capture = next(c for c in manifest["subcommands"] if c["name"] == "capture")
    stack = next(a for a in capture["args"] if a["name"] == "--stack")
    assert stack["required"] is True
    assert stack["choices"]


def test_describe_json_nested_subcommands_are_walked(capsys):
    code = cli.main(["describe", "--format", "json"])
    manifest = json.loads(capsys.readouterr().out)
    benchmark = next(c for c in manifest["subcommands"] if c["name"] == "benchmark")
    assert {s["name"] for s in benchmark["subcommands"]} == {"benchmark compare"}
    fixture = next(c for c in manifest["subcommands"] if c["name"] == "fixture")
    assert {s["name"] for s in fixture["subcommands"]} == {"fixture create",
                                                           "fixture promote"}


def test_describe_json_exit_codes_present_for_every_command_that_has_them(capsys):
    code = cli.main(["describe", "--format", "json"])
    manifest = json.loads(capsys.readouterr().out)

    def _flat(cmds):
        for c in cmds:
            yield c
            yield from _flat(c.get("subcommands", []))

    by_name = {c["name"]: c for c in _flat(manifest["subcommands"])}
    assert set(by_name) == set(cli._EXIT_CODES)
    for name, codes in cli._EXIT_CODES.items():
        manifest_codes = by_name[name]["exit_codes"]
        assert [(e["code"], e["meaning"]) for e in manifest_codes] == list(codes)


def test_describe_json_is_deterministic(capsys):
    cli.main(["describe", "--format", "json"])
    first = capsys.readouterr().out
    cli.main(["describe", "--format", "json"])
    second = capsys.readouterr().out
    assert first == second


# --- describe --format text ----------------------------------------------

def test_describe_text_is_readable_and_deterministic(capsys):
    code = cli.main(["describe"])
    assert code == 0
    first = capsys.readouterr().out
    assert "hotato" in first
    assert "hotato run" in first
    assert "hotato benchmark compare" in first
    assert "hotato fixture create" in first
    assert "exit codes:" in first

    cli.main(["describe", "--format", "text"])
    second = capsys.readouterr().out
    assert first == second


# --- every subparser carries a uniform "Exit codes:" epilog ---------------

def test_every_subparser_help_has_an_exit_codes_epilog():
    parser = cli.build_parser()
    seen = set()
    for name, sub in _iter_subparsers(parser):
        seen.add(name)
        help_text = sub.format_help()
        assert "Exit codes:" in help_text, f"{name!r} --help has no Exit codes: epilog"
    assert seen == set(_ALL_SUBCOMMANDS)


def test_exit_codes_epilog_matches_the_single_source_of_truth():
    for name in _ALL_SUBCOMMANDS:
        epilog = cli._exit_codes_epilog(name)
        assert epilog.startswith("Exit codes: ")
        for code, meaning in cli._EXIT_CODES[name]:
            assert f"{code} = {meaning}" in epilog
