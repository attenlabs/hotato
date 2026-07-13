"""``hotato start --demo``: the guided, credential-less first run.

Pinned here:

  * --demo writes the sweep result (hotato-sweep.json), the HTML dashboard
    (hotato-sweep.html), the threshold-funnel card
    (hotato-no-single-threshold.svg), and one demo failure contract
    (contracts/demo-missed-interruption.hotato/) into --dir;
  * the demo contract is created from the bundled sweep's #2 candidate (the
    real missed-interruption call) with --expect yield, and verified
    immediately: it genuinely FAILS that policy (the agent talked over the
    caller instead of yielding), so --demo prints "verified contract: FAIL as
    expected";
  * it prints the exact next commands: promote a candidate, run fixtures in
    CI, re-verify the demo contract (`hotato contract verify contracts/`),
    and render a card;
  * it touches NO network (asserted the way the rest of the suite does it, by
    failing the test on first reach for urllib/socket);
  * a run with no mode is a usage error (exit 2), and the sweep it writes
    resolves back into a card ref.
"""

import json
import math
import os
import socket
import struct
import urllib.request
import wave
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


# --- the demo failure contract ---------------------------------------------

def test_start_demo_creates_a_real_failure_contract(tmp_path):
    rc = cli.main(["start", "--demo", "--dir", str(tmp_path)])
    assert rc == 0
    bundle = tmp_path / "contracts" / "demo-missed-interruption.hotato"
    contract_json = bundle / "contract.json"
    assert contract_json.is_file()
    doc = json.loads(contract_json.read_text())
    assert doc["schema"] == "hotato.contract.v1"
    assert doc["id"] == "demo-missed-interruption"
    assert doc["label"]["expected_behavior"] == "yield"
    assert doc["label"]["label_source"] == "human"
    # the bundled missed-interruption call really does fail a --expect yield
    # policy: the agent talked over the caller instead of yielding.
    assert doc["measurement"]["scorable"] is True
    assert doc["measurement"]["passed"] is False
    assert doc["measurement"]["did_yield"] is False
    # the rest of the bundle (audio, evidence, policy, CI scaffold) is written
    # too -- this is a real `contract create`, not a stub.
    assert (bundle / "audio" / "event.wav").is_file()
    assert (bundle / "evidence" / "trust.json").is_file()
    assert (bundle / "evidence" / "card.svg").is_file()
    assert (bundle / "policy" / "verify.yaml").is_file()
    assert (bundle / "ci" / "github-action.yml").is_file()


def test_start_demo_prints_verified_contract_fail_as_expected(tmp_path, capsys):
    rc = cli.main(["start", "--demo", "--dir", str(tmp_path)])
    assert rc == 0
    out = capsys.readouterr().out
    assert "verified contract: FAIL as expected" in out


def test_start_demo_scopes_the_ci_gate_to_evidence_and_policy(tmp_path, capsys):
    # The frozen demo contract's CI gate catches a change to the recorded
    # evidence or policy; it does NOT prove the CURRENT agent stopped
    # regressing -- that needs a fresh recapture (docs/RECAPTURE.md).
    rc = cli.main(["start", "--demo", "--dir", str(tmp_path)])
    assert rc == 0
    out = capsys.readouterr().out
    assert ("a CI gate on this contract catches any change to the evidence "
            "or policy") in out
    assert "catching the AGENT regressing requires a fresh recapture" in out
    assert "docs/RECAPTURE.md" in out
    assert "exact failure a CI regression gate would catch" not in out


def test_start_demo_explains_its_own_exit_0(tmp_path, capsys):
    # start --demo exits 0 because the guided setup succeeded, not because
    # the demo contract passed -- the demo contract genuinely FAILS its
    # policy. The line must say so, since the process exit code alone reads
    # as a pass.
    rc = cli.main(["start", "--demo", "--dir", str(tmp_path)])
    assert rc == 0
    out = capsys.readouterr().out
    assert ("start --demo itself exits 0 because setup succeeded" in out)
    assert "run the next command to see the contract's CI exit 1" in out
    assert "hotato contract verify contracts/" in out


def test_start_demo_prints_the_contract_verify_next_command(tmp_path, capsys):
    rc = cli.main(["start", "--demo", "--dir", str(tmp_path)])
    assert rc == 0
    out = capsys.readouterr().out
    assert "hotato contract verify contracts/" in out


def test_start_demo_json_format_includes_the_contract(tmp_path, capsys):
    rc = cli.main(["start", "--demo", "--dir", str(tmp_path),
                   "--format", "json"])
    assert rc == 0
    payload = json.loads(capsys.readouterr().out)
    assert "contracts/demo-missed-interruption.hotato/contract.json" in \
        payload["written"]
    assert any(c == "hotato contract verify contracts/"
               for c in payload["next_commands"])
    contract = payload["contract"]
    assert contract["id"] == "demo-missed-interruption"
    assert contract["expect"] == "yield"
    assert contract["scorable"] is True
    assert contract["passed"] is False
    assert contract["verified_fail_as_expected"] is True


def test_start_demo_contract_verify_cli_reports_the_regression(tmp_path, capsys):
    """The printed next command is not decorative: running it for real against
    the written bundle re-scores the SAME failure and reports it, exactly like
    CI would on a regression (exit 1, one contract, not passed)."""
    assert cli.main(["start", "--demo", "--dir", str(tmp_path)]) == 0
    capsys.readouterr()
    contracts_dir = tmp_path / "contracts"
    rc = cli.main(["contract", "verify", str(contracts_dir), "--format", "json"])
    assert rc == 1
    v = json.loads(capsys.readouterr().out)
    assert v["count"] == 1
    assert v["summary"] == {"passed": 0, "failed": 1}
    assert v["results"][0]["id"] == "demo-missed-interruption"
    assert v["results"][0]["passed"] is False


def test_start_demo_writes_contract_is_offline_too(tmp_path):
    # covered by the module-level _no_network fixture: if the contract step
    # reached for the network this whole test module would already fail.
    assert cli.main(["start", "--demo", "--dir", str(tmp_path)]) == 0
    assert (tmp_path / "contracts" / "demo-missed-interruption.hotato"
            / "contract.json").is_file()


def test_start_demo_contract_is_idempotent(tmp_path):
    """Running --demo twice into the same --dir must not error on the
    already-existing contract bundle."""
    assert cli.main(["start", "--demo", "--dir", str(tmp_path)]) == 0
    assert cli.main(["start", "--demo", "--dir", str(tmp_path)]) == 0
    assert (tmp_path / "contracts" / "demo-missed-interruption.hotato"
            / "contract.json").is_file()


# --- usage / stubbed modes ------------------------------------------------

def test_start_requires_a_mode(capsys):
    assert cli.main(["start"]) == 2
    assert "error:" in capsys.readouterr().err


def test_start_bad_dir_is_a_usage_error(tmp_path):
    assert cli.main(["start", "--demo", "--dir",
                     str(tmp_path / "does-not-exist")]) == 2


def test_start_dropped_unwired_stack_folder_flags():
    # --stack/--folder were unfinished stubs on the flagship command; they are
    # dropped rather than advertised as "[not yet in this build]". They are now
    # unknown arguments -> argparse usage error (SystemExit 2 at parse time).
    for flag, val in (("--stack", "vapi"), ("--folder", "x")):
        with pytest.raises(SystemExit) as ei:
            cli.main(["start", flag, val])
        assert ei.value.code == 2


# --- K6: --stereo --confirm-channels reaches create_contract's own gate -----

def _write_swapped_stereo(path, *, duration_sec=6.0, sr=16000):
    """A dual-channel call whose caller (ch0) dominates and agent (ch1) is
    brief -- the reverse of the usual pattern, tripping the swap heuristic."""
    n = int(duration_sec * sr)

    def _on(segs, t):
        return any(s <= t < e for s, e in segs)

    caller_segments = [(0.2, 5.8)]
    agent_segments = [(2.0, 2.5)]
    frames = bytearray()
    for i in range(n):
        t = i / sr
        c = int(0.35 * 32767 * math.sin(2 * math.pi * 220.0 * i / sr)) if _on(caller_segments, t) else 0
        a = int(0.35 * 32767 * math.sin(2 * math.pi * 330.0 * i / sr)) if _on(agent_segments, t) else 0
        frames += struct.pack("<hh", c, a)
    with wave.open(str(path), "wb") as w:
        w.setnchannels(2)
        w.setsampwidth(2)
        w.setframerate(sr)
        w.writeframes(bytes(frames))
    return str(path)


def test_start_stereo_confirm_channels_reaches_a_real_verdict(tmp_path, capsys):
    p = _write_swapped_stereo(tmp_path / "swapped.wav")
    unconfirmed_dir = tmp_path / "unconfirmed"
    confirmed_dir = tmp_path / "confirmed"
    unconfirmed_dir.mkdir()
    confirmed_dir.mkdir()

    # Without --confirm-channels: start.py's own swap_blocked gate skips
    # contract creation entirely (pre-existing behavior) -- no verdict either
    # way, never silently invented.
    rc = cli.main([
        "start", "--stereo", p, "--dir", str(unconfirmed_dir), "--label", "hold",
        "--onset", "2.0", "--format", "json",
    ])
    assert rc == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["possible_channel_swap"] is True
    assert payload["contract"] is None

    # With --confirm-channels: the SAME confirmation must reach
    # create_contract's own K6 gate (not just unblock start.py's earlier
    # check), so the contract carries a REAL, non-null verdict.
    rc2 = cli.main([
        "start", "--stereo", p, "--dir", str(confirmed_dir),
        "--label", "hold", "--onset", "2.0", "--confirm-channels",
        "--format", "json",
    ])
    assert rc2 == 0
    payload2 = json.loads(capsys.readouterr().out)
    assert payload2["contract"] is not None
    contract_json = confirmed_dir / payload2["contract"]["dir"] / "contract.json"
    doc = json.loads(contract_json.read_text())
    m = doc["measurement"]
    assert m["verdict_eligible"] is True
    assert m["did_yield"] is not None
