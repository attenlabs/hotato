"""Regression: every machine-readable ``next`` command hotato emits must be
copy-paste-safe when the workspace path contains a space (the common case on
macOS and Windows, e.g. ``/Users/me/My Calls``).

Each interpolated filesystem path is ``shlex.quote()``'d, so the exact command
hotato tells an agent to run parses back -- via ``shlex.split``, the way an
agent consumes a ``next`` hint -- with the path intact as ONE argument, instead
of fragmenting at the space into two tokens (which pre-fix turned
``--scenarios /My Calls/x`` into a stray ``Calls/x`` positional and an
``unrecognized arguments`` usage error).

Pins the ``fixture create``, ``contract create``, ``init webhook``,
``init starter``, ``loop`` and ``investigate`` ``next`` builders.
"""

import json
import shlex
from importlib import resources

from hotato import cli, loop

# A bundled dual-channel example: caller interrupts at 2.40s, agent should yield.
HARD = str(
    resources.files("hotato").joinpath("data", "audio",
                                        "01-hard-interruption.example.wav")
)


def _out(capsys):
    return json.loads(capsys.readouterr().out)


def _survives_split(command: str, spaced_dir: str) -> bool:
    """True iff, after an agent parses ``command`` with ``shlex.split`` (the
    documented way a ``next`` hint is consumed), the spaced workspace path
    survives as a single token. Pre-fix the space fragmented the path, so no
    resulting token contained the whole ``spaced_dir`` substring."""
    return any(spaced_dir in tok for tok in shlex.split(command))


def test_fixture_create_next_survives_and_runs_verbatim(tmp_path, capsys):
    space = tmp_path / "My Calls"          # a real space in the workspace path
    out_dir = space / "fx"
    assert cli.main([
        "fixture", "create", "--stereo", HARD, "--id", "fx-space-001",
        "--onset", "2.40", "--expect", "yield", "--out", str(out_dir),
        "--format", "json",
    ]) == 0
    next_cmd = _out(capsys)["next"]

    assert str(space) in next_cmd                       # the path is there...
    assert _survives_split(next_cmd, str(space))        # ...and intact as one arg

    # Strongest proof: the exact command an agent copies runs verbatim.
    argv = shlex.split(next_cmd)[1:]                    # drop leading "hotato"
    assert cli.main(argv) == 0


def test_contract_create_next_survives_spaced_path(tmp_path, capsys):
    space = tmp_path / "My Calls"
    out_dir = space / "contracts"
    assert cli.main([
        "contract", "create", "--stereo", HARD, "--id", "ct-space-001",
        "--onset", "2.40", "--expect", "yield", "--out", str(out_dir),
        "--format", "json",
    ]) == 0
    next_cmd = _out(capsys)["next"]
    assert next_cmd.startswith("hotato contract verify ")
    assert str(space) in next_cmd
    assert _survives_split(next_cmd, str(space))


def test_init_webhook_next_survives_spaced_path(tmp_path, capsys):
    space = tmp_path / "My Project"
    out_dir = space / "worker"
    assert cli.main([
        "init", "webhook", "--stack", "vapi", "--out", str(out_dir),
        "--format", "json",
    ]) == 0
    cd_cmd = _out(capsys)["next"][0]
    assert cd_cmd.startswith("cd ")
    assert str(space) in cd_cmd
    assert _survives_split(cd_cmd, str(space))


def test_init_starter_next_survives_spaced_path(tmp_path, capsys):
    space = tmp_path / "My Project"
    out_dir = space / "kit"
    assert cli.main([
        "init", "starter", "--stack", "vapi", "--out", str(out_dir),
        "--format", "json",
    ]) == 0
    cd_cmd = next(c for c in _out(capsys)["next"] if c and c.startswith("cd "))
    assert str(space) in cd_cmd
    assert _survives_split(cd_cmd, str(space))


def test_investigate_next_survives_spaced_state_path(tmp_path, capsys):
    space = tmp_path / "My Calls"
    space.mkdir(parents=True)
    state = space / "investigate-state.json"
    # Discovery only; exit code reflects scorability, not the path -- read the
    # emitted next hints either way.
    cli.main([
        "investigate", str(HARD), "--state", str(state),
        "--format", "json",
    ])
    hints = _out(capsys).get("next") or []
    assert hints, "expected at least one candidate label hint"
    command = hints[0]["command"]
    # ref is PATH#N -- the '#' plus a space is a double hazard shlex.quote fixes.
    assert str(space) in command
    assert _survives_split(command, str(space))


def test_loop_patch_next_survives_spaced_plan_path():
    # `_message` builds the `hotato patch PLAN` hint from the plan file the
    # loop persisted; that path inherits the (possibly spaced) state dir. Drive
    # the builder directly so the assertion is about quoting, not scorer luck.
    plan_path = "/Users/me/My Calls/.hotato/loop-fixplan.json"

    # awaiting_verify, propose-a-step branch.
    _msg, cmds = loop._message("awaiting_verify", {"planning": {
        "decision": "propose_one_step", "fixes_awaiting_verify": 1,
        "plan_path": plan_path,
    }})
    patch_cmd = next(c for c in cmds if c.startswith("hotato patch "))
    assert plan_path in patch_cmd
    assert _survives_split(patch_cmd, plan_path)

    # awaiting_verify, both-axes "do not tune a single threshold" branch.
    _msg2, cmds2 = loop._message("awaiting_verify", {"planning": {
        "decision": "do_not_tune_single_threshold", "plan_path": plan_path,
    }})
    patch_cmd2 = next(c for c in cmds2 if c.startswith("hotato patch "))
    assert plan_path in patch_cmd2
    assert _survives_split(patch_cmd2, plan_path)
