"""``hotato start --demo``: the guided, credential-less first run.

Pinned here:

  * --demo writes the sweep result (hotato-sweep.json), the HTML dashboard
    (hotato-sweep.html), and the threshold-funnel card
    (hotato-no-single-threshold.svg) into --dir;
  * it prints the exact next commands: promote a candidate, run fixtures in CI,
    and render a card;
  * it touches NO network (asserted the way the rest of the suite does it, by
    failing the test on first reach for urllib/socket);
  * a run with no mode is a usage error (exit 2), and the sweep it writes
    resolves back into a card ref.
"""

import json
import os
import socket
import urllib.request
from importlib import resources

import pytest

from hotato import cli


@pytest.fixture(autouse=True)
def _no_network(monkeypatch):
    """Fail the instant anything reaches for the network during --demo."""
    def guard(*args, **kwargs):
        raise AssertionError("network attempted during hotato start --demo")
    monkeypatch.setattr(urllib.request, "urlopen", guard)
    monkeypatch.setattr(socket, "create_connection", guard)
    monkeypatch.setattr(socket.socket, "connect", guard)


@pytest.fixture(autouse=True)
def _isolated_home(tmp_path, monkeypatch):
    # No stored connection or ambient vendor key can leak into a demo start.
    monkeypatch.setenv("HOTATO_HOME", str(tmp_path / "home"))
    for var in ("VAPI_API_KEY", "RETELL_API_KEY", "TWILIO_ACCOUNT_SID",
                "TWILIO_AUTH_TOKEN"):
        monkeypatch.delenv(var, raising=False)


# --- the fully-wired demo path --------------------------------------------

def test_start_demo_writes_sweep_json_html_and_card(tmp_path):
    rc = cli.main(["start", "--demo", "--dir", str(tmp_path)])
    assert rc == 0
    sweep = tmp_path / "hotato-sweep.json"
    html = tmp_path / "hotato-sweep.html"
    card = tmp_path / "hotato-no-single-threshold.svg"
    assert sweep.is_file() and html.is_file() and card.is_file()

    doc = json.loads(sweep.read_text())
    assert doc["kind"] == "analyze"
    assert doc["candidates"], "the demo sweep finds candidate moments"
    assert doc["pull"] == {"stack": "demo", "listed": 2, "pulled": 2,
                           "skipped": 0}

    assert "<audio" in html.read_text()
    assert "NO SINGLE THRESHOLD CAN" in card.read_text()


def test_start_demo_prints_the_next_commands(tmp_path, capsys):
    rc = cli.main(["start", "--demo", "--dir", str(tmp_path)])
    assert rc == 0
    out = capsys.readouterr().out
    assert "hotato fixture promote hotato-sweep.json#1" in out
    assert "hotato run --scenarios tests/hotato/scenarios" in out
    assert "hotato card hotato-sweep.json#1" in out


def test_start_demo_json_format(tmp_path, capsys):
    rc = cli.main(["start", "--demo", "--dir", str(tmp_path),
                   "--format", "json"])
    assert rc == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["kind"] == "start"
    assert payload["ran"] is True
    assert payload["offline"] is True
    assert "hotato-sweep.json" in payload["written"]
    assert any("fixture promote" in c for c in payload["next_commands"])
    assert any("hotato card" in c for c in payload["next_commands"])


def test_start_demo_sweep_resolves_back_into_a_card(tmp_path):
    """The sweep start writes is a real analyze result: a #N ref off it renders
    a card, so the printed `hotato card hotato-sweep.json#1` actually works."""
    assert cli.main(["start", "--demo", "--dir", str(tmp_path)]) == 0
    out = tmp_path / "c.svg"
    rc = cli.main(["card", str(tmp_path / "hotato-sweep.json") + "#1",
                   "--out", str(out)])
    assert rc == 0 and out.is_file()


# --- usage / stubbed modes ------------------------------------------------

def test_start_requires_a_mode(capsys):
    assert cli.main(["start"]) == 2
    assert "error:" in capsys.readouterr().err


def test_start_bad_dir_is_a_usage_error(tmp_path):
    assert cli.main(["start", "--demo", "--dir",
                     str(tmp_path / "does-not-exist")]) == 2


def test_start_stub_mode_routes_to_the_shipped_command(capsys):
    rc = cli.main(["start", "--stack", "vapi"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "not yet in this build" in out
    assert "hotato sweep" in out
