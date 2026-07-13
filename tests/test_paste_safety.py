"""Paste-safety: every shell command hotato PRINTS for a user to copy must run
as-is on a SINGLE line.

The bug this guards against: hotato printed example commands that broke when
pasted into a shell --

    hotato fixture promote ... --expect <yield|hold> \\
    hotato connect vapi --api-key <key>   # stores it

The shell reads ``<yield`` as "redirect from a file named yield", ``|hold`` as
"pipe to a command hold", a trailing ``\\`` leaves the line half-parsed, and a
`` # `` starts a comment. So a printed command line must contain NONE of:

  * an unquoted ``<`` or ``>`` (angle-bracket placeholder / stray redirect),
  * a `` | `` shell pipe (e.g. a ``<yield|hold>`` choice inlined),
  * a trailing ``\\`` line-continuation, or
  * an inline `` # `` comment.

Part A drives the main guided-output surfaces offline and checks every printed
command line. Part B walks the whole argparse ``--help`` tree as a regression
guard. A genuine redirect to a concrete file (``--format json > result.json``)
is runnable as-is and is the documented save-to-file idiom, so Part B allows a
lone ``>``; Part A's surfaces contain no such redirect, so it flags ``>`` too,
exactly as the bug report describes.
"""

import re
import socket
import urllib.request
from importlib import resources

import pytest

from hotato import cli


# A bundled dual-channel example call; scan/investigate/loop score it offline.
EXAMPLE_WAV = str(
    resources.files("hotato").joinpath(
        "data", "audio", "01-hard-interruption.example.wav")
)


@pytest.fixture(autouse=True)
def _offline(tmp_path, monkeypatch):
    """No guided surface may touch the network, and no ambient credential or
    stored connection can change what it prints."""
    def guard(*args, **kwargs):
        raise AssertionError("network attempted while printing a guided surface")
    monkeypatch.setattr(urllib.request, "urlopen", guard)
    monkeypatch.setattr(socket, "create_connection", guard)
    monkeypatch.setattr(socket.socket, "connect", guard)
    monkeypatch.setenv("HOTATO_HOME", str(tmp_path / "home"))
    for var in ("VAPI_API_KEY", "RETELL_API_KEY", "TWILIO_ACCOUNT_SID",
                "TWILIO_AUTH_TOKEN"):
        monkeypatch.delenv(var, raising=False)


# --------------------------------------------------------------------------
# Detecting a printed shell command and its paste hazards
# --------------------------------------------------------------------------

# Markers a command line may carry before the command itself: an indent, a
# shell-comment / bullet / prompt, a numbered step, or a short "label:" prefix
# the way `hotato investigate` prints "label: hotato investigate label ...".
_MARKER = re.compile(r"^(?:[#>$*\-]+\s+|\d+\.\s+|[A-Za-z][\w .\-]*:\s+)")


def _command_of(line):
    """If ``line`` is a shell command a user would paste, return the command
    text (markers/labels peeled); else None."""
    s = line.strip()
    # peel any leading markers/labels (repeatedly, e.g. "next:  - hotato ...")
    while True:
        m = _MARKER.match(s)
        if not m:
            break
        s = s[m.end():].strip()
    if s.startswith("hotato ") or s.startswith("export "):
        return s
    if re.match(r"^[A-Z][A-Z0-9_]*=\S+\s+hotato ", s):  # ENV=val hotato ...
        return s
    return None


def _hazards(cmd, *, allow_redirect_out):
    """Paste hazards in ``cmd``. ``allow_redirect_out`` permits a lone ``>``
    (a genuine ``> concrete-file`` redirect, runnable as-is)."""
    c = cmd.rstrip()
    hits = []
    if "<" in c:
        hits.append("unquoted '<' (angle-bracket placeholder / redirect)")
    if ">" in c and not allow_redirect_out:
        hits.append("unquoted '>'")
    if " | " in c:
        hits.append("shell pipe ' | '")
    if c.endswith("\\"):
        hits.append("trailing '\\' line-continuation")
    if " # " in c:
        hits.append("inline ' # ' comment")
    return hits


def _assert_block_is_paste_safe(text, *, where, allow_redirect_out=False):
    """Assert every printed command line in ``text`` is paste-safe. Returns the
    number of command lines checked (so callers can guard against a vacuous
    pass)."""
    checked = 0
    problems = []
    for line in text.splitlines():
        cmd = _command_of(line)
        if cmd is None:
            continue
        checked += 1
        h = _hazards(cmd, allow_redirect_out=allow_redirect_out)
        if h:
            problems.append(f"    {where}: [{', '.join(h)}]  {cmd!r}")
    assert not problems, (
        f"paste-breaking printed command(s) in {where}:\n" + "\n".join(problems)
    )
    return checked


# --------------------------------------------------------------------------
# Part A: the main guided-output surfaces, driven offline
# --------------------------------------------------------------------------

def _run(argv, capsys):
    """Drive one CLI invocation and return everything it printed (out + err)."""
    try:
        cli.main(argv)
    except SystemExit:
        pass
    cap = capsys.readouterr()
    return cap.out + "\n" + cap.err


def test_bare_hotato_first_run_screen_is_paste_safe(capsys):
    text = _run([], capsys)
    n = _assert_block_is_paste_safe(text, where="bare `hotato`")
    assert n >= 1, "expected the first-run screen to show at least one command"


def test_start_demo_is_paste_safe(tmp_path, capsys):
    text = _run(["start", "--demo", "--dir", str(tmp_path)], capsys)
    n = _assert_block_is_paste_safe(text, where="hotato start --demo")
    assert n >= 1, "expected `start --demo` to print next commands"


def test_setup_vapi_scaffold_is_paste_safe(capsys):
    text = _run(["setup", "--stack", "vapi"], capsys)
    n = _assert_block_is_paste_safe(text, where="hotato setup --stack vapi")
    assert n >= 1, "expected the setup scaffold to print a score command"


def test_init_starter_auto_pull_next_is_paste_safe(tmp_path, capsys):
    out_dir = tmp_path / "kit-vapi"
    text = _run(["init", "starter", "--stack", "vapi", "--out", str(out_dir)],
                capsys)
    n = _assert_block_is_paste_safe(text, where="hotato init starter (vapi)")
    assert n >= 1, "expected the starter to print next commands"


def test_init_starter_capture_only_next_is_paste_safe(tmp_path, capsys):
    out_dir = tmp_path / "kit-livekit"
    text = _run(["init", "starter", "--stack", "livekit", "--out", str(out_dir)],
                capsys)
    n = _assert_block_is_paste_safe(text, where="hotato init starter (livekit)")
    assert n >= 1


def test_scan_guidance_is_paste_safe(capsys):
    text = _run(["scan", "--stereo", EXAMPLE_WAV], capsys)
    _assert_block_is_paste_safe(text, where="hotato scan")


def test_investigate_guidance_is_paste_safe(tmp_path, capsys):
    state = tmp_path / "investigate-state.json"
    text = _run(["investigate", EXAMPLE_WAV, "--state", str(state)], capsys)
    n = _assert_block_is_paste_safe(text, where="hotato investigate")
    assert n >= 1, "expected investigate to print per-candidate label commands"


def test_loop_guidance_is_paste_safe(tmp_path, capsys):
    folder = tmp_path / "recordings"
    folder.mkdir()
    (folder / "call-01.wav").write_bytes(
        resources.files("hotato").joinpath(
            "data", "audio", "01-hard-interruption.example.wav").read_bytes())
    text = _run(["loop", str(folder)], capsys)
    n = _assert_block_is_paste_safe(text, where="hotato loop")
    assert n >= 1, "expected loop to print the next label command"


def test_fixture_no_subcommand_usage_is_paste_safe(capsys):
    # A usage error still names commands; they must be paste-safe too.
    text = _run(["fixture"], capsys)
    _assert_block_is_paste_safe(text, where="hotato fixture (usage)")


# --------------------------------------------------------------------------
# Part B: the whole --help tree (regression guard across every subcommand)
# --------------------------------------------------------------------------

def _walk(parser, path, out):
    import argparse
    for label, txt in (("description", parser.description),
                       ("epilog", parser.epilog)):
        if txt:
            out.append((path, label, txt))
    for act in parser._actions:
        if isinstance(act, argparse._SubParsersAction):
            for name, sub in act.choices.items():
                _walk(sub, f"{path} {name}", out)


def test_all_help_epilogs_are_paste_safe():
    parser = cli.build_parser()
    blocks = []
    _walk(parser, "hotato", blocks)
    assert len(blocks) > 30, "expected many subcommand help blocks to scan"
    total_checked = 0
    for path, label, txt in blocks:
        # Only indented example lines are commands; a flowing description
        # paragraph is prose (it may legitimately contain '>=' etc.).
        cmd_lines = "\n".join(
            ln for ln in txt.splitlines() if ln[:1].isspace())
        total_checked += _assert_block_is_paste_safe(
            cmd_lines, where=f"{path} ({label})", allow_redirect_out=True)
    assert total_checked > 20, (
        "scanned too few command lines; the walk found nothing to check")
