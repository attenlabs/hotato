"""`hotato sweep --demo`: the full pull -> analyze sweep over the two bundled
real demo calls, credential-less and fully offline.

The offline guarantee is asserted the way the rest of this suite does it:
``urllib.request.urlopen`` is the only HTTP surface in ``hotato.capture``
(see tests/test_pull_sweep.py), so the guard fixture replaces it -- and raw
socket connects -- with functions that fail the test on first touch. Every
test in this module runs under that guard.
"""

import json
import os
import socket
import urllib.request
from importlib import resources

import pytest

from hotato import capture as cap
from hotato import cli


@pytest.fixture(autouse=True)
def _isolated_home(tmp_path, monkeypatch):
    # Same isolation as test_pull_sweep: no real ~/.hotato connections and no
    # ambient vendor env vars can leak into a demo run.
    monkeypatch.setenv("HOTATO_HOME", str(tmp_path / "home"))
    monkeypatch.delenv("HOTATO_ALLOW_MONO", raising=False)
    for var in ("VAPI_API_KEY", "RETELL_API_KEY", "TWILIO_ACCOUNT_SID",
                "TWILIO_AUTH_TOKEN", "BLAND_API_KEY", "ELEVENLABS_API_KEY",
                "SYNTHFLOW_API_KEY", "SYNTHFLOW_MODEL_ID", "MILLIS_API_KEY",
                "CARTESIA_API_KEY", "CARTESIA_AGENT_ID"):
        monkeypatch.delenv(var, raising=False)


@pytest.fixture(autouse=True)
def _no_network(monkeypatch):
    """Fail the test the instant anything reaches for the network."""
    def guard(*args, **kwargs):
        raise AssertionError("network attempted during a --demo sweep")
    monkeypatch.setattr(urllib.request, "urlopen", guard)
    monkeypatch.setattr(socket, "create_connection", guard)
    monkeypatch.setattr(socket.socket, "connect", guard)


def _bundled_stereo():
    return (resources.files("hotato")
            .joinpath("data", "audio", "01-hard-interruption.example.wav")
            .read_bytes())


# =========================================================================
# 1. the credential-less happy path: one command, a real sweep report
# =========================================================================

def test_sweep_demo_writes_dashboard(tmp_path, capsys):
    out = tmp_path / "sweep.html"
    rc = cli.main(["sweep", "--demo", "--out", str(out), "--no-open"])
    assert rc == 0
    html = out.read_text()
    assert "<audio" in html and "candidate" in html
    err = capsys.readouterr().err
    assert "demo" in err and "2 scanned" in err
    assert "no credentials, no network" in err


def test_sweep_demo_default_out_is_the_sweep_naming_scheme(tmp_path, monkeypatch, capsys):
    # A real sweep writes hotato-sweep-<stack>.html; the demo stack is "demo".
    monkeypatch.chdir(tmp_path)
    rc = cli.main(["sweep", "--demo", "--no-open"])
    assert rc == 0
    assert os.path.isfile("hotato-sweep-demo.html")


def test_sweep_demo_json_envelope_and_pull_summary(capsys):
    rc = cli.main(["sweep", "--demo", "--format", "json"])
    assert rc == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["kind"] == "analyze"
    assert payload["pull"] == {"stack": "demo", "listed": 2, "pulled": 2,
                               "skipped": 0}
    assert payload["calls_scanned"] == 2
    assert payload["total_candidates"] >= 1
    assert payload["shown"] == len(payload["candidates"])


def test_sweep_demo_json_shape_matches_a_real_sweep(tmp_path, monkeypatch, capsys):
    """The demo envelope has exactly the keys a real sweep's envelope has, so
    everything a user or an agent builds against the demo output keeps working
    on the first real sweep."""
    rc = cli.main(["sweep", "--demo", "--format", "json"])
    assert rc == 0
    demo_payload = json.loads(capsys.readouterr().out)

    stereo = _bundled_stereo()

    def fake_pull(stack, creds, *, out_dir, ids=None, since=None, limit=50,
                  allow_mono=False, log=None):
        os.makedirs(out_dir, exist_ok=True)
        p = os.path.join(out_dir, "vapi__a.wav")
        with open(p, "wb") as fh:
            fh.write(stereo)
        return {"stack": stack, "out_dir": out_dir, "listed": 1,
                "pulled": [{"id": "a", "path": p}], "skipped": []}

    monkeypatch.setattr(cap, "pull", fake_pull)
    rc = cli.main(["sweep", "--stack", "vapi", "--api-key", "k",
                   "--dir", str(tmp_path / "pull"), "--format", "json"])
    assert rc == 0
    real_payload = json.loads(capsys.readouterr().out)
    assert set(demo_payload.keys()) == set(real_payload.keys())
    assert set(demo_payload["pull"].keys()) == set(real_payload["pull"].keys())


# =========================================================================
# 2. exclusivity: --demo takes no stack, credential, or pull flags
# =========================================================================

@pytest.mark.parametrize("extra", [
    ["--stack", "vapi"],
    ["--api-key", "k"],
    ["--account-sid", "AC1"],
    ["--auth-token", "t"],
    ["--call-id", "c1"],
    ["--since", "7d"],
    ["--allow-mono"],
    ["--dir", "some-dir"],
])
def test_sweep_demo_rejects_stack_and_credential_flags(extra, capsys):
    rc = cli.main(["sweep", "--demo", "--no-open"] + extra)
    assert rc == 2
    err = capsys.readouterr().err
    assert "error:" in err and "--demo" in err
    # the offending flag is named, so the fix is one edit
    assert extra[0] in err


def test_sweep_demo_conflict_names_every_offending_flag(capsys):
    rc = cli.main(["sweep", "--demo", "--stack", "vapi", "--since", "7d",
                   "--api-key", "k", "--no-open"])
    assert rc == 2
    err = capsys.readouterr().err
    for flag in ("--stack", "--since", "--api-key"):
        assert flag in err


def test_sweep_demo_conflict_is_the_structured_json_error(capsys):
    rc = cli.main(["sweep", "--demo", "--stack", "vapi", "--format", "json"])
    assert rc == 2
    payload = json.loads(capsys.readouterr().out)
    assert payload["ok"] is False
    assert payload["exit_code"] == 2
    assert "--demo" in payload["message"]


def test_sweep_demo_never_reads_stored_connections(tmp_path, monkeypatch, capsys):
    """A stored connection must not leak into a demo sweep: the demo path
    resolves no stack and no credentials at all."""
    def boom(stack, overrides):
        raise AssertionError("credential resolution attempted during --demo")
    monkeypatch.setattr(cap, "_resolve_for_pull", boom)
    out = tmp_path / "sweep.html"
    rc = cli.main(["sweep", "--demo", "--out", str(out), "--no-open"])
    assert rc == 0
    assert out.is_file()
