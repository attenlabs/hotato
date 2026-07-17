"""docs/BAD-CALL-TO-CI.md advertises a completable command path.

Regression guard for the 1.9.0-era loop-bridge defect: the doc's provider-call
section advertised ``investigate`` -> ``investigate label`` and stopped short
of the PR gate, while ``pr create --fixtures`` refused the ``.hotato``
contract bundle ``investigate label`` emits. These tests hold the doc's
fenced commands to the parser that ships: every ``hotato <verb> [<sub>]``
named in a fenced block must resolve, and the provider-call sequence must
reach ``pr create`` with the bundle it just produced.
"""

import os
import re

import pytest

from hotato import cli

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DOC = os.path.join(ROOT, "docs", "BAD-CALL-TO-CI.md")

_WORD = re.compile(r"^[a-z][a-z-]*$")


def _read():
    with open(DOC, encoding="utf-8") as fh:
        return fh.read()


def _fenced_blocks(text):
    return re.findall(r"(?ms)^```[a-z]*\n(.*?)^```$", text)


def _hotato_command_lines(text):
    """Every ``hotato ...`` invocation inside a fenced block, with backslash
    continuations joined onto one logical line. A rendered output header like
    ``hotato compare: a -> b`` is sample output, not a command, and is
    skipped."""
    out = []
    for block in _fenced_blocks(text):
        cur = None
        for raw in block.splitlines():
            line = raw.strip()
            if cur is not None:
                cur += " " + line.rstrip("\\").strip()
                if not line.endswith("\\"):
                    out.append(cur)
                    cur = None
                continue
            if not line.startswith("hotato "):
                continue
            if re.match(r"hotato [a-z-]+:", line):
                continue
            body = line.rstrip("\\").strip()
            if line.endswith("\\"):
                cur = body
            else:
                out.append(body)
    return out


def _verb_tokens(cmd):
    """The leading subcommand words of one ``hotato ...`` line (one or two),
    stopping at the first flag, path, or placeholder."""
    verbs = []
    for tok in cmd.split()[1:]:
        if not _WORD.match(tok):
            break
        verbs.append(tok)
        if len(verbs) == 2:
            break
    return verbs


def _help_exits_zero(argv, capsys):
    with pytest.raises(SystemExit) as exc:
        cli.main(list(argv) + ["--help"])
    capsys.readouterr()
    return exc.value.code in (0, None)


def test_every_fenced_hotato_command_resolves_in_the_parser(capsys):
    cmds = _hotato_command_lines(_read())
    assert cmds, "no fenced hotato commands found in docs/BAD-CALL-TO-CI.md"
    for cmd in cmds:
        verbs = _verb_tokens(cmd)
        assert verbs, f"unparseable hotato command in the doc: {cmd!r}"
        assert _help_exits_zero(verbs, capsys), (
            f"docs/BAD-CALL-TO-CI.md names `hotato {' '.join(verbs)}` "
            "in a fenced block but the CLI parser has no such command; "
            "the advertised path must be runnable as written"
        )


def test_provider_call_sequence_reaches_the_pr_gate():
    m = re.search(r"(?ms)^## Start from your own provider call$(.*?)^## ",
                  _read())
    assert m, "no '## Start from your own provider call' section found"
    blocks = _fenced_blocks(m.group(1))
    assert blocks, "the provider-call section has no fenced command block"
    primary = blocks[0]
    assert "hotato investigate " in primary
    assert "hotato investigate label " in primary
    assert "hotato pr create " in primary, (
        "the advertised provider-call path must reach the PR gate in the "
        "same fenced sequence (investigate -> investigate label -> pr create)"
    )
    pr_line = next(l for l in primary.splitlines()
                   if l.strip().startswith("hotato pr create"))
    # the primary path hands pr create the bundle investigate label wrote
    assert ".hotato" in pr_line
