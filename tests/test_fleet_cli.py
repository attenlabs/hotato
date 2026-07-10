"""``hotato fleet ...``: the local Guardian control plane exposed as an umbrella
CLI over the (separately tested) ``hotato.fleet.api.FleetAPI``.

These drive the SAME entrypoint every other CLI test uses -- ``cli.main([...])``
-> ``build_parser()`` -> ``args.func(args)`` -- end to end, always against a
``--home`` under ``tmp_path`` so they NEVER touch the real ``~/.hotato/fleet``
(``registry.DEFAULT_HOME``). They pin the Guardian loop the umbrella wraps:
init a workspace, register and list agents, ingest a real two-channel recording
(deduped on replay), discover candidate moments, review the queue, record a
human label, and roll up status -- asserting exit codes and that the created ids
surface in the output. Canary routing is recommendation-only and must exit 2.
"""
from __future__ import annotations

import json
import os

import pytest

from hotato import cli
from hotato.fleet.registry import DEFAULT_HOME
from tests._trial_audio import talkover_call


def _home(tmp_path):
    # A tmp home so nothing is ever written under the real DEFAULT_HOME.
    return str(tmp_path / "fleet-home")


def _out(capsys):
    return capsys.readouterr().out


def _setup_agent(home, *, ws="ws1", agent="support-bot", stack="vapi",
                 external_ref="asst_1"):
    """init + agent add; returns nothing (asserts happy-path exit codes)."""
    assert cli.main(["fleet", "init", "--home", home, "-w", ws, "--name", "Acme"]) == 0
    assert cli.main(["fleet", "agent", "add", "--home", home, "-w", ws,
                     "--agent-id", agent, "--stack", stack,
                     "--assistant-id", external_ref]) == 0


def test_fleet_init_creates_workspace(tmp_path, capsys):
    home = _home(tmp_path)
    rc = cli.main(["fleet", "init", "--home", home, "-w", "ws1", "--name", "Acme"])
    assert rc == 0
    out = _out(capsys)
    assert "ws1" in out and "local" in out
    # --home was honored: the registry db lives under the tmp home, NOT DEFAULT_HOME.
    assert os.path.exists(os.path.join(home, "fleet.db"))
    assert os.path.abspath(home) != os.path.abspath(DEFAULT_HOME)


def test_fleet_init_json_format(tmp_path, capsys):
    home = _home(tmp_path)
    rc = cli.main(["fleet", "init", "--home", home, "-w", "ws1", "--format", "json"])
    assert rc == 0
    payload = json.loads(_out(capsys))
    assert payload["workspace_id"] == "ws1"
    assert payload["mode"] == "local"
    assert os.path.abspath(payload["home"]) == os.path.abspath(home)


def test_fleet_agent_add_and_list(tmp_path, capsys):
    home = _home(tmp_path)
    _setup_agent(home)
    capsys.readouterr()  # drain setup output

    rc = cli.main(["fleet", "agent", "list", "--home", home, "-w", "ws1"])
    assert rc == 0
    out = _out(capsys)
    assert "support-bot" in out and "vapi" in out and "asst_1" in out

    # json surface returns the raw agent rows
    rc = cli.main(["fleet", "agent", "list", "--home", home, "-w", "ws1",
                   "--format", "json"])
    assert rc == 0
    rows = json.loads(_out(capsys))
    assert [r["agent_id"] for r in rows] == ["support-bot"]
    assert rows[0]["stack"] == "vapi"
    assert rows[0]["external_ref"] == "asst_1"


def test_fleet_agent_name_alias_sets_id(tmp_path, capsys):
    # --name is an accepted alias for --agent-id (both target the same field).
    home = _home(tmp_path)
    assert cli.main(["fleet", "init", "--home", home, "-w", "ws1"]) == 0
    rc = cli.main(["fleet", "agent", "add", "--home", home, "-w", "ws1",
                   "--name", "aliased-bot", "--stack", "retell"])
    assert rc == 0
    capsys.readouterr()
    cli.main(["fleet", "agent", "list", "--home", home, "-w", "ws1",
              "--format", "json"])
    rows = json.loads(_out(capsys))
    assert rows[0]["agent_id"] == "aliased-bot"
    assert rows[0]["stack"] == "retell"


def test_fleet_ingest_dedupes_on_replay(tmp_path, capsys):
    home = _home(tmp_path)
    _setup_agent(home)
    capsys.readouterr()
    wav = str(tmp_path / "call.wav")
    talkover_call(wav)

    rc = cli.main(["fleet", "ingest", "--home", home, "-w", "ws1",
                   "--agent", "support-bot", wav])
    assert rc == 0
    first = _out(capsys)
    assert "rec-" in first and "call-" in first
    assert "deduped:      False" in first

    # a duplicate webhook / re-pull converges on one recording
    rc = cli.main(["fleet", "ingest", "--home", home, "-w", "ws1",
                   "--agent", "support-bot", wav, "--format", "json"])
    assert rc == 0
    payload = json.loads(_out(capsys))
    assert payload["deduped"] is True


def test_fleet_discover_lists_candidates(tmp_path, capsys):
    home = _home(tmp_path)
    _setup_agent(home)
    capsys.readouterr()
    wav = str(tmp_path / "call.wav")
    talkover_call(wav)
    assert cli.main(["fleet", "ingest", "--home", home, "-w", "ws1",
                     "--agent", "support-bot", wav]) == 0
    capsys.readouterr()

    rc = cli.main(["fleet", "discover", "--home", home, "-w", "ws1",
                   "--agent", "support-bot", wav, "--format", "json"])
    assert rc == 0
    payload = json.loads(_out(capsys))
    assert payload["scorable"] is True
    assert payload["candidates"]
    assert all(c["candidate_id"].startswith("cand-") for c in payload["candidates"])


def test_fleet_review_and_label(tmp_path, capsys):
    home = _home(tmp_path)
    _setup_agent(home)
    wav = str(tmp_path / "call.wav")
    talkover_call(wav)
    assert cli.main(["fleet", "ingest", "--home", home, "-w", "ws1",
                     "--agent", "support-bot", wav]) == 0
    assert cli.main(["fleet", "discover", "--home", home, "-w", "ws1",
                     "--agent", "support-bot", wav]) == 0
    capsys.readouterr()

    # review the queue and grab a candidate id from the json surface
    rc = cli.main(["fleet", "review", "--home", home, "-w", "ws1",
                   "--format", "json"])
    assert rc == 0
    queue = json.loads(_out(capsys))
    assert queue, "discover should have populated the review queue"
    cid = queue[0]["candidate_id"]

    # the text review surface mentions the candidate id too
    rc = cli.main(["fleet", "review", "--home", home, "-w", "ws1"])
    assert rc == 0
    assert cid in _out(capsys)

    # a HUMAN label promotes a yield/hold candidate to a labeled failure
    rc = cli.main(["fleet", "label", "--home", home, "-w", "ws1", cid,
                   "--decision", "yield", "--reviewer", "alice",
                   "--rationale", "clear talk-over"])
    assert rc == 0
    out = _out(capsys)
    assert cid in out and "yield" in out and "labeled" in out

    # the labeled candidate leaves the (status='new') review queue
    cli.main(["fleet", "review", "--home", home, "-w", "ws1", "--format", "json"])
    assert cid not in [c["candidate_id"] for c in json.loads(_out(capsys))]


def test_fleet_status_counts_after_loop(tmp_path, capsys):
    home = _home(tmp_path)
    _setup_agent(home)
    wav = str(tmp_path / "call.wav")
    talkover_call(wav)
    cli.main(["fleet", "ingest", "--home", home, "-w", "ws1",
              "--agent", "support-bot", wav])
    cli.main(["fleet", "discover", "--home", home, "-w", "ws1",
              "--agent", "support-bot", wav])
    capsys.readouterr()

    rc = cli.main(["fleet", "status", "--home", home, "-w", "ws1", "--format", "json"])
    assert rc == 0
    st = json.loads(_out(capsys))
    counts = st["counts"]
    assert counts["agents"] == 1
    assert counts["calls"] == 1
    assert counts["recordings"] == 1
    assert counts["candidates"] >= 1

    # the text surface is a clean, emoji-free summary
    rc = cli.main(["fleet", "status", "--home", home, "-w", "ws1"])
    assert rc == 0
    out = _out(capsys)
    assert "counts:" in out and "agents" in out and "jobs:" in out


def test_fleet_canary_is_recommendation_only(tmp_path, capsys):
    home = _home(tmp_path)
    for action in ("start", "rollback"):
        rc = cli.main(["fleet", "canary", action, "--home", home, "-w", "ws1"])
        assert rc == 2  # live routing is not enabled in this release
        assert "not enabled in this release" in _out(capsys)


def test_fleet_home_isolated_from_default_home(tmp_path):
    # Belt and suspenders: every subcommand ran under tmp_path in the tests
    # above; assert the umbrella never created the real DEFAULT_HOME as a
    # side effect of these runs (the db is under tmp home instead).
    home = _home(tmp_path)
    assert cli.main(["fleet", "init", "--home", home, "-w", "ws1"]) == 0
    assert os.path.exists(os.path.join(home, "fleet.db"))
    assert os.path.commonpath([os.path.abspath(home), str(tmp_path)]) == str(tmp_path)
