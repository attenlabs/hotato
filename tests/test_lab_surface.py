"""The 1.17.0 public/lab surface split ("The Narrowing").

One registration layer (``cli._SurfaceRouter``) decides every command's
visibility: the top-level ``--help`` lists ONLY the durable public surface;
every other command registers unlisted under its pre-1.17 name (the compat
alias, byte-identical behavior) and gets ``hotato lab <cmd>`` as its
canonical spelling. Pinned here:

  * the top-level help listing is exactly the public surface -- no lab
    command leaks in, no public command drops out;
  * every lab command stays fully callable under its old top-level spelling;
  * ``hotato lab <cmd>`` and ``hotato <cmd>`` produce byte-identical output;
  * ``hotato lab --help`` lists every lab command with its one-line
    description plus the stability statement;
  * the stability statement renders in the top-level --help epilog tail.
"""

import argparse

import pytest

from hotato import cli


def _top_subparsers(parser):
    for action in parser._actions:
        if isinstance(action, argparse._SubParsersAction):
            return action
    raise AssertionError("no top-level subparsers action")


def _listed_commands(parser):
    """The command names the top-level --help actually lists (argparse renders
    one pseudo-action per add_parser call that passed ``help=``)."""
    return {a.dest for a in _top_subparsers(parser)._choices_actions}


def _lab_names(parser):
    return {name for name, _ in parser._lab_commands}


# --- top-level help = exactly the public surface ---------------------------

def test_top_level_help_lists_exactly_the_public_surface():
    parser = cli.build_parser()
    assert _listed_commands(parser) == cli._PUBLIC_SURFACE


def test_every_registered_command_is_public_or_lab_never_both():
    parser = cli.build_parser()
    registered = set(_top_subparsers(parser).choices)
    lab = _lab_names(parser)
    assert lab == registered - cli._PUBLIC_SURFACE
    assert not (lab & cli._PUBLIC_SURFACE)
    # the public surface is fully registered, not aspirational
    assert cli._PUBLIC_SURFACE <= registered


def test_top_level_help_epilog_carries_the_stability_statement(capsys):
    with pytest.raises(SystemExit):
        cli.main(["--help"])
    out = capsys.readouterr().out
    for line in cli._STABILITY_STATEMENT.splitlines():
        assert line in out
    assert "hotato lab --help" in out


# --- the compat aliases: old spellings keep working ------------------------

def test_lab_commands_stay_callable_under_their_old_top_level_spelling():
    parser = cli.build_parser()
    choices = _top_subparsers(parser).choices
    for name in _lab_names(parser):
        assert name in choices, f"compat alias {name!r} lost its registration"


def test_lab_spelling_and_compat_spelling_are_byte_identical(capsys):
    # --help output is generated from the one shared parser, so comparing it
    # end to end proves both spellings dispatch through identical machinery.
    with pytest.raises(SystemExit) as exc:
        cli.main(["gauntlet", "--help"])
    assert exc.value.code in (0, None)
    via_alias = capsys.readouterr().out

    with pytest.raises(SystemExit) as exc:
        cli.main(["lab", "gauntlet", "--help"])
    assert exc.value.code in (0, None)
    via_lab = capsys.readouterr().out

    assert via_alias == via_lab


def test_lab_investigate_label_routes_like_the_top_level_spelling(capsys):
    with pytest.raises(SystemExit) as exc:
        cli.main(["lab", "investigate", "label", "--help"])
    assert exc.value.code in (0, None)
    via_lab = capsys.readouterr().out

    with pytest.raises(SystemExit) as exc:
        cli.main(["investigate", "label", "--help"])
    assert exc.value.code in (0, None)
    assert via_lab == capsys.readouterr().out


def test_lab_run_executes_the_command(capsys):
    # a real execution through the lab spelling, not just help text
    assert cli.main(["lab", "run", "--suite", "barge-in",
                     "--format", "json"]) == 0
    capsys.readouterr()


# --- hotato lab --help ------------------------------------------------------

def test_lab_help_lists_every_lab_command_with_a_description(capsys):
    assert cli.main(["lab", "--help"]) == 0
    out = capsys.readouterr().out
    parser = cli.build_parser()
    for name, help_line in parser._lab_commands:
        assert f"\n  {name}" in out, f"lab listing omits {name!r}"
        if help_line:
            # the one-line description survives (wrapped, so match its head)
            assert help_line.split()[0] in out
    for line in cli._STABILITY_STATEMENT.splitlines():
        # the statement is re-wrapped in the listing; match word-wise
        for word in ("durable", "pre-1.17"):
            assert word in out


def test_bare_lab_prints_the_listing_too(capsys):
    assert cli.main(["lab"]) == 0
    assert "hotato lab" in capsys.readouterr().out


def test_unknown_lab_command_refuses_with_exit_2(capsys):
    assert cli.main(["lab", "definitely-not-a-command"]) == 2
    err = capsys.readouterr().err
    assert "unknown lab command" in err
    assert "hotato lab --help" in err


# --- no public command is reachable only through lab ------------------------

def test_public_commands_keep_their_top_level_help(capsys):
    with pytest.raises(SystemExit) as exc:
        cli.main(["autopsy", "--help"])
    assert exc.value.code in (0, None)
    assert "autopsy" in capsys.readouterr().out
