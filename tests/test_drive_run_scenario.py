"""fleet.adapters run_scenario: the drive-a-call gate + end-to-end origination
against a real local fake provider. run_scenario now ORIGINATES a real call, so
it must (a) refuse without credentials, (b) refuse without an explicit egress
opt-in, and (c) when authorized, place the call and return the pulled recording
-- while NEVER issuing a PUT/PATCH against a provider config."""
import pytest

from hotato._engine.audio import read_wav
from hotato.fleet import adapters
from hotato.fleet.adapters import CapabilityError
from tests import _drive_fakes as fakes


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    for var in ("HOTATO_DRIVE_OPT_IN", "VAPI_PHONE_NUMBER_ID",
                "HOTATO_DRIVE_CUSTOMER_NUMBER", "HOTATO_DRIVE_TO_NUMBER",
                "HOTATO_DRIVE_FROM_NUMBER", "VAPI_BASE_URL", "TWILIO_BASE_URL"):
        monkeypatch.delenv(var, raising=False)


def _scenario_v1(**extra):
    doc = {
        "kind": "hotato.scenario", "version": 1, "id": "s-run",
        "goal": {"type": "get_refund", "target": "order A-1001"},
        "caller": {"script": [{"say": "Hi there"}, {"say": "I need a refund"}]},
    }
    doc.update(extra)
    return doc


# --- gating: refuse without credentials -------------------------------------

def test_vapi_run_scenario_refuses_without_credentials():
    v = adapters.get_adapter("vapi")  # no api key
    with pytest.raises(CapabilityError, match="requires credentials"):
        v.run_scenario({"clone_id": "asst"}, {"egress_opt_in": True,
                                              "phone_number_id": "pn",
                                              "customer_number": "+1"})


def test_twilio_run_scenario_refuses_without_credentials():
    t = adapters.get_adapter("twilio")  # no account_sid/token
    with pytest.raises(CapabilityError, match="requires credentials"):
        t.run_scenario(None, _scenario_v1(egress_opt_in=True, to_number="+1",
                                          from_number="+2"))


# --- gating: refuse without an explicit egress opt-in -----------------------

def test_vapi_run_scenario_refuses_without_egress_opt_in():
    v = adapters.get_adapter("vapi", api_key="k")
    with pytest.raises(CapabilityError, match="egress opt-in"):
        v.run_scenario({"clone_id": "asst"}, {"phone_number_id": "pn",
                                              "customer_number": "+1"})


def test_twilio_run_scenario_refuses_without_egress_opt_in():
    t = adapters.get_adapter("twilio", account_sid="AC1", auth_token="tok")
    with pytest.raises(CapabilityError, match="egress opt-in"):
        t.run_scenario(None, _scenario_v1(to_number="+1", from_number="+2"))


# --- gating: refuse when the drive parameters are missing -------------------

def test_vapi_run_scenario_refuses_without_number_params():
    v = adapters.get_adapter("vapi", api_key="k")
    with pytest.raises(CapabilityError, match="phone_number_id"):
        v.run_scenario({"clone_id": "asst"}, {"egress_opt_in": True})


def test_twilio_run_scenario_refuses_without_number_params():
    t = adapters.get_adapter("twilio", account_sid="AC1", auth_token="tok")
    with pytest.raises(CapabilityError, match="to_number"):
        t.run_scenario(None, _scenario_v1(egress_opt_in=True))


def test_env_var_alone_satisfies_the_egress_opt_in(monkeypatch):
    # HOTATO_DRIVE_OPT_IN=1 is the env equivalent of the scenario flag; with it
    # set, the refusal advances past the opt-in gate to the missing-params gate.
    monkeypatch.setenv("HOTATO_DRIVE_OPT_IN", "1")
    v = adapters.get_adapter("vapi", api_key="k")
    with pytest.raises(CapabilityError, match="phone_number_id"):
        v.run_scenario({"clone_id": "asst"}, {})


# --- happy path: authorized drive against a real local fake provider --------

def test_vapi_run_scenario_drives_the_clone_end_to_end(tmp_path, monkeypatch):
    monkeypatch.setenv("HOTATO_ALLOW_PRIVATE_URLS", "1")  # loopback recording server
    recorder = fakes.Recorder()
    stereo = fakes.stereo_wav_bytes(tmp_path)
    server, base = fakes.start(fakes.vapi_handler(recorder, stereo))
    try:
        v = adapters.get_adapter("vapi", api_key="sk-live")
        # the clone dict from apply_variant carries the clone_id we must drive
        result = v.run_scenario(
            {"clone_id": "asst_clone", "pending": True},
            {"egress_opt_in": True, "phone_number_id": "pn_1",
             "customer_number": "+15005550009", "base_url": base,
             "poll_interval": 0, "max_wait": 5},
        )
    finally:
        server.shutdown()

    assert read_wav(result["recording"]).num_channels == 2
    assert result["origin"]["kind"] == "real"
    assert result["origin"]["caller"] == "assistant-originated"
    # the call was originated FROM THE CLONE, not the production source
    post = recorder.by("POST", "/call")[0]
    assert post["body"]["assistantId"] == "asst_clone"
    # production is never mutated: no PUT/PATCH/DELETE anywhere
    assert set(recorder.methods) <= {"GET", "POST"}


def test_twilio_run_scenario_drives_scripted_call_end_to_end(tmp_path, monkeypatch):
    monkeypatch.setenv("HOTATO_ALLOW_PRIVATE_URLS", "1")
    recorder = fakes.Recorder()
    stereo = fakes.stereo_wav_bytes(tmp_path)
    server, base = fakes.start(fakes.twilio_handler(recorder, stereo))
    try:
        t = adapters.get_adapter("twilio", account_sid="AC1", auth_token="tok")
        result = t.run_scenario(
            None,
            _scenario_v1(egress_opt_in=True, to_number="+15005550001",
                         from_number="+15005550002", base_url=base,
                         poll_interval=0, max_wait=5),
        )
    finally:
        server.shutdown()

    assert read_wav(result["recording"]).num_channels == 2
    assert result["origin"]["kind"] == "real"
    assert result["origin"]["caller"] == "scripted-twiml"
    # the scripted caller script reached the wire as TwiML
    post = recorder.by("POST", "/Calls.json")[0]
    assert "<Say>Hi there</Say>" in post["body"]["Twiml"]
    assert post["body"]["RecordingChannels"] == "dual"
    assert set(recorder.methods) <= {"GET", "POST"}


# --- honest discovery: the new ops never raise NotImplementedError ----------

def test_drive_adapters_advertise_run_scenario_and_refuse_cleanly():
    # both drive adapters advertise run_scenario; invoking without creds raises a
    # CapabilityError (the credentialed refusal), NEVER NotImplementedError.
    for adapter in (adapters.get_adapter("vapi"),
                    adapters.get_adapter("twilio")):
        assert adapter.supports("run_scenario")
        try:
            adapter.run_scenario({"clone_id": "x"}, {"egress_opt_in": True,
                                                     "phone_number_id": "p",
                                                     "customer_number": "+1",
                                                     "to_number": "+1",
                                                     "from_number": "+2"})
        except CapabilityError:
            pass
        except NotImplementedError as exc:  # pragma: no cover - the bug
            pytest.fail(f"{adapter.stack} run_scenario raised NotImplementedError: {exc}")
