"""``hotato drive``: the P1.4 verb that re-runs the LIVE agent to get a red
regression gate to green.

Every test here is HERMETIC. No real telephony call is ever originated: the two
functions that reach a provider (:func:`hotato.drive.place_call_vapi` /
:func:`hotato.drive.place_call_twilio`) are monkeypatched, so the CLI is driven
end to end against a canned recording, never a network. The refusal tests point
those same functions at a boom-stub that fails LOUDLY if the code ever reaches a
dial, proving the credential / egress / target gates fire first.

Covered: the vapi + twilio drive paths to a verdict (mocked origination), the
invariant-pass and invariant-fail cases, the unsupported-stack message, and the
credential / egress-opt-in / drive-target gating (no call without them).
"""
import json
from importlib import resources

import pytest

from hotato import cli

# Two bundled reference recordings with KNOWN, deterministic verdicts. drive
# scores whatever the (mocked) live call returns, NOT the frozen bundle audio --
# that is the whole point (fresh evidence, not the pinned recording).
YIELDS = str(resources.files("hotato").joinpath(
    "data", "audio", "01-hard-interruption.example.wav"))   # yields  -> expect yield PASSES
HOLDS = str(resources.files("hotato").joinpath(
    "data", "audio", "02-backchannel-mhm.example.wav"))     # holds   -> expect yield FAILS


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    # Nothing may leak a credential, a target, or an opt-in in from the host
    # environment, or the gating tests would pass for the wrong reason.
    for var in ("HOTATO_DRIVE_OPT_IN", "VAPI_API_KEY", "VAPI_PHONE_NUMBER_ID",
                "HOTATO_DRIVE_CUSTOMER_NUMBER", "HOTATO_DRIVE_TO_NUMBER",
                "HOTATO_DRIVE_FROM_NUMBER", "TWILIO_ACCOUNT_SID",
                "TWILIO_AUTH_TOKEN", "VAPI_BASE_URL", "TWILIO_BASE_URL"):
        monkeypatch.delenv(var, raising=False)


class _Spy:
    """Records every (args, kwargs) it is called with and returns a canned
    drive result carrying ``recording`` (a real local WAV the scorer reads).
    Standing in for place_call_vapi / place_call_twilio means NO socket, NO
    provider, NO real call is ever opened by these tests."""

    def __init__(self, recording, *, provider, caller):
        self.recording = recording
        self.provider = provider
        self.caller = caller
        self.calls = []

    def __call__(self, *args, **kwargs):
        self.calls.append((args, kwargs))
        return {
            "recording": self.recording,
            "provider": self.provider,
            "provider_call_id": f"{self.provider}_call_1",
            "status": "ended" if self.provider == "vapi" else "completed",
            "origin": {"kind": "real", "provider": self.provider,
                       "caller": self.caller},
        }


def _boom(*args, **kwargs):  # pragma: no cover - reached only if a gate fails
    raise AssertionError(
        "a REAL call origination was attempted; a gate should have refused first")


def _make_bundle(tmp_path, *, stack, cid="refund-cutoff-001", expect="yield"):
    contracts = tmp_path / "contracts"
    code = cli.main([
        "contract", "create", "--stereo", YIELDS, "--onset", "2.40",
        "--expect", expect, "--id", cid, "--out", str(contracts),
        "--stack", stack,
    ])
    assert code == 0
    return contracts / (cid + ".hotato")


def _write_scenario(tmp_path):
    doc = {
        "kind": "hotato.scenario", "version": 1, "id": "refund-caller",
        "goal": {"type": "get_refund", "target": "order A-1001"},
        "caller": {"script": [{"say": "My order A-1001 arrived damaged."},
                              {"say": "Yes, please refund it."}]},
    }
    p = tmp_path / "caller.scenario.json"
    p.write_text(json.dumps(doc), encoding="utf-8")
    return p


# --- vapi + twilio drive to a PASS verdict (mocked origination) --------------

def test_vapi_drive_passes_and_prints_the_recapture_command(tmp_path, monkeypatch, capsys):
    spy = _Spy(YIELDS, provider="vapi", caller="assistant-originated")
    monkeypatch.setattr("hotato.drive.place_call_vapi", spy)
    monkeypatch.setattr("hotato.drive.place_call_twilio", _boom)
    bundle = _make_bundle(tmp_path, stack="vapi")

    code = cli.main([
        "drive", str(bundle), "--stack", "vapi", "--assistant", "asst_clone",
        "--api-key", "sk-live", "--phone-number-id", "pn_1",
        "--customer", "+15551230000", "--yes",
    ])
    out = capsys.readouterr().out
    assert code == 0
    assert "PASS" in out
    # the ONE next step is the exact command to commit the recaptured green contract
    assert "hotato contract create" in out
    assert "refund-cutoff-001-recapture" in out
    assert "--expect yield" in out
    # the assistant clone (not production) was the origination target
    (args, kwargs) = spy.calls[0]
    assert args[0] == "asst_clone"
    assert kwargs["phone_number_id"] == "pn_1"
    assert kwargs["customer_number"] == "+15551230000"
    assert kwargs["api_key"] == "sk-live"


def test_twilio_drive_from_a_scenario_file_passes(tmp_path, monkeypatch, capsys):
    spy = _Spy(YIELDS, provider="twilio", caller="scripted-twiml")
    monkeypatch.setattr("hotato.drive.place_call_twilio", spy)
    monkeypatch.setattr("hotato.drive.place_call_vapi", _boom)
    scenario = _write_scenario(tmp_path)

    code = cli.main([
        "drive", str(scenario), "--stack", "twilio",
        "--account-sid", "AC1", "--auth-token", "tok",
        "--to", "+15550000001", "--from", "+15550000002", "--yes",
    ])
    out = capsys.readouterr().out
    assert code == 0
    assert "PASS" in out
    assert "hotato contract create" in out
    # the loaded scenario.v1 caller stimulus reached place_call_twilio, plus the
    # drive target and credentials
    (args, kwargs) = spy.calls[0]
    assert args[0]["caller"]["script"][0]["say"].startswith("My order A-1001")
    assert kwargs["to_number"] == "+15550000001"
    assert kwargs["from_number"] == "+15550000002"
    assert kwargs["sid"] == "AC1" and kwargs["token"] == "tok"


# --- the invariant-FAIL case (the agent still violates the invariant) --------

def test_drive_fails_when_the_fresh_call_still_violates_the_invariant(
        tmp_path, monkeypatch, capsys):
    # The mocked live call returns a recording where the agent HOLDS through the
    # caller; expect=yield -> did_yield False -> the gate stays red.
    spy = _Spy(HOLDS, provider="vapi", caller="assistant-originated")
    monkeypatch.setattr("hotato.drive.place_call_vapi", spy)
    bundle = _make_bundle(tmp_path, stack="vapi")

    code = cli.main([
        "drive", str(bundle), "--stack", "vapi", "--assistant", "asst_clone",
        "--api-key", "sk-live", "--phone-number-id", "pn_1",
        "--customer", "+15551230000", "--yes",
    ])
    out = capsys.readouterr().out
    assert code == 1
    assert "FAIL" in out
    assert "still" in out and "yield" in out
    # a FAIL never prints a "commit the green contract" next step
    assert "hotato contract create" not in out


# --- unsupported stack: the manual recapture path, never a faked dial --------

def test_unsupported_stack_points_at_recapture_and_never_dials(
        tmp_path, monkeypatch, capsys):
    monkeypatch.setattr("hotato.drive.place_call_vapi", _boom)
    monkeypatch.setattr("hotato.drive.place_call_twilio", _boom)
    bundle = _make_bundle(tmp_path, stack="livekit")

    code = cli.main(["drive", str(bundle)])  # stack livekit read from the bundle
    out = capsys.readouterr().out
    assert code == 2
    assert "not available for stack 'livekit'" in out
    assert "docs/RECAPTURE.md" in out


# --- the three real-call gates: no call without creds / opt-in / target ------

def test_drive_refuses_without_credentials(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr("hotato.drive.place_call_vapi", _boom)
    bundle = _make_bundle(tmp_path, stack="vapi")
    # --yes given, target given, but NO --api-key and no VAPI_API_KEY env.
    code = cli.main([
        "drive", str(bundle), "--stack", "vapi", "--assistant", "asst_clone",
        "--phone-number-id", "pn_1", "--customer", "+15551230000", "--yes",
    ])
    err = capsys.readouterr().err
    assert code == 2
    assert "requires credentials" in err


def test_drive_refuses_without_egress_opt_in(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr("hotato.drive.place_call_vapi", _boom)
    bundle = _make_bundle(tmp_path, stack="vapi")
    # credentials + target present, but NO --yes and no HOTATO_DRIVE_OPT_IN.
    code = cli.main([
        "drive", str(bundle), "--stack", "vapi", "--assistant", "asst_clone",
        "--api-key", "sk-live", "--phone-number-id", "pn_1",
        "--customer", "+15551230000",
    ])
    err = capsys.readouterr().err
    assert code == 2
    assert "--yes" in err


def test_drive_refuses_without_a_drive_target(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr("hotato.drive.place_call_vapi", _boom)
    bundle = _make_bundle(tmp_path, stack="vapi")
    # credentials + opt-in present, but NO phone-number-id / customer.
    code = cli.main([
        "drive", str(bundle), "--stack", "vapi", "--assistant", "asst_clone",
        "--api-key", "sk-live", "--yes",
    ])
    err = capsys.readouterr().err
    assert code == 2
    assert "phone_number_id" in err


def test_env_opt_in_alone_authorizes_the_call(tmp_path, monkeypatch, capsys):
    # HOTATO_DRIVE_OPT_IN=1 is the env equivalent of --yes: the drive proceeds
    # to originate (mocked) without the flag.
    monkeypatch.setenv("HOTATO_DRIVE_OPT_IN", "1")
    spy = _Spy(YIELDS, provider="vapi", caller="assistant-originated")
    monkeypatch.setattr("hotato.drive.place_call_vapi", spy)
    bundle = _make_bundle(tmp_path, stack="vapi")

    code = cli.main([
        "drive", str(bundle), "--stack", "vapi", "--assistant", "asst_clone",
        "--api-key", "sk-live", "--phone-number-id", "pn_1",
        "--customer", "+15551230000",
    ])
    assert code == 0
    assert len(spy.calls) == 1


def test_twilio_bundle_without_a_scenario_is_refused(tmp_path, monkeypatch, capsys):
    # A .hotato bundle stores timing evidence, not a caller script, so a twilio
    # scripted drive needs --scenario; refuse honestly rather than fabricate one.
    monkeypatch.setattr("hotato.drive.place_call_twilio", _boom)
    bundle = _make_bundle(tmp_path, stack="twilio")
    code = cli.main([
        "drive", str(bundle), "--stack", "twilio", "--account-sid", "AC1",
        "--auth-token", "tok", "--to", "+1", "--from", "+2", "--yes",
    ])
    err = capsys.readouterr().err
    assert code == 2
    assert "caller.script" in err or "scripted caller" in err
