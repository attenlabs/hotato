"""S1/S2: the real out-of-box capture path — offline, no stack SDKs, no network.

These exercise `hotato capture` / `hotato setup` (and the underlying
`hotato.capture` module) entirely against the bundled two-channel references and
stdlib WAV I/O. Nothing here touches the network or imports any stack SDK, so the
suite stays hermetic. The honesty invariants (no accuracy claim, energy is not
intent) are re-checked on the capture path too.
"""

from importlib import resources

import pytest

from hotato import capture as cap
from hotato import cli
from hotato._engine.audio import read_wav, write_wav

STACKS = ("vapi", "twilio", "livekit", "pipecat", "retell")


def _bundled(sid):
    return str(
        resources.files("hotato").joinpath("data", "audio", sid + ".example.wav")
    )


# --- the --demo fixture fallback (offline, zero deps) ---------------------

@pytest.mark.parametrize("stack", STACKS)
def test_cli_capture_demo_exits_zero(stack, capsys):
    """`hotato capture --stack X --demo` runs end-to-end offline and prints a
    scored verdict, exit 0, for every stack."""
    rc = cli.main(["capture", "--stack", stack, "--demo"])
    assert rc == 0, stack
    out = capsys.readouterr().out
    assert "did_yield=" in out and ("PASS" in out or "FAIL" in out), stack


@pytest.mark.parametrize("stack", STACKS)
def test_module_demo_exits_zero(stack):
    assert cap.demo(stack) == 0, stack


def test_capture_demo_json_format(capsys):
    rc = cli.main(["capture", "--stack", "vapi", "--demo", "--format", "json"])
    assert rc == 0
    import json

    # the JSON envelope must be the standard hotato shape
    payload = capsys.readouterr().out.strip().splitlines()
    # find the JSON block (report prints demo lines to stdout before the envelope)
    text = "\n".join(payload)
    start = text.index("{")
    env = json.loads(text[start:])
    assert env["tool"] == "hotato"
    assert env["limits"]["accuracy_claim"] is None


# --- scoring an already-captured file (the universal escape hatch) --------

def test_cli_capture_stereo_scores_bundled(capsys):
    rc = cli.main(["capture", "--stack", "twilio", "--stereo",
                   _bundled("01-hard-interruption")])
    assert rc == 0
    assert "PASS" in capsys.readouterr().out


def test_cli_capture_caller_agent_two_mono(tmp_path, capsys):
    """`--caller a.wav --agent b.wav` (two mono tracks, e.g. LiveKit egress)."""
    sig = read_wav(_bundled("01-hard-interruption"))
    caller = tmp_path / "caller.wav"
    agent = tmp_path / "agent.wav"
    write_wav(str(caller), sig.sample_rate, [sig.get(0)])
    write_wav(str(agent), sig.sample_rate, [sig.get(1)])
    rc = cli.main(["capture", "--stack", "livekit",
                   "--caller", str(caller), "--agent", str(agent),
                   "--onset", "2.4"])
    assert rc == 0
    assert "PASS" in capsys.readouterr().out


def test_score_returns_hotato_envelope():
    env = cap.score(_bundled("01-hard-interruption"), stack="vapi")
    assert env["tool"] == "hotato"
    assert env["mode"] == "single"
    assert "exit_code" in env
    # honesty invariant holds on the capture path too
    assert env["limits"]["accuracy_claim"] is None


# --- exit-code contract for missing input / bad usage ---------------------

def test_capture_vapi_without_creds_exits_2():
    assert cli.main(["capture", "--stack", "vapi"]) == 2


def test_capture_twilio_without_creds_exits_2():
    assert cli.main(["capture", "--stack", "twilio"]) == 2


def test_capture_livekit_without_input_exits_2():
    # no direct fetch: must be guided to setup / a captured file, not crash
    assert cli.main(["capture", "--stack", "livekit"]) == 2


def test_capture_unknown_stack_is_argparse_error():
    with pytest.raises(SystemExit) as exc:
        cli.main(["capture", "--stack", "bogus"])
    assert exc.value.code == 2


# --- setup scaffolds ------------------------------------------------------

@pytest.mark.parametrize("stack", STACKS)
def test_cli_setup_exits_zero_and_prints(stack, capsys):
    rc = cli.main(["setup", "--stack", stack])
    assert rc == 0, stack
    out = capsys.readouterr().out
    assert stack[:4].lower() in out.lower() or stack.capitalize() in out, stack
    assert len(out.strip()) > 0


def test_setup_livekit_uses_two_track_egress_not_mix():
    text = cap.setup_text("livekit")
    assert "TrackEgress" in text
    assert "RoomComposite" in text  # names the mix path it warns against


def test_setup_pipecat_two_channel_audiobufferprocessor():
    text = cap.setup_text("pipecat")
    assert "AudioBufferProcessor" in text
    assert "num_channels=2" in text


def test_setup_retell_is_honest_no_fake_path():
    text = cap.setup_text("retell").lower()
    assert "no confirmed self-serve" in text
    assert "will not fake" in text


def test_setup_unknown_stack_raises():
    with pytest.raises(ValueError):
        cap.setup_text("bogus")


# --- first-run guide leads with capture, labels the self-test -------------

def test_bare_hotato_guides_to_capture(capsys):
    rc = cli.main([])
    assert rc == 0
    out = capsys.readouterr().out.lower()
    assert "capture --stack vapi" in out
    assert "self-test" in out
    assert "no accuracy score" in out


# --- honesty: no accuracy claim anywhere on the capture surface -----------

def test_no_accuracy_claim_in_setup_scaffolds():
    for stack in STACKS:
        text = cap.setup_text(stack).lower()
        assert "accuracy" not in text or "no accuracy" in text, stack
