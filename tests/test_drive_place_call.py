"""place_call_twilio / place_call_vapi against a REAL local HTTPServer that plays
the provider. No monkeypatched urlopen: the real drive client makes real HTTP
requests over 127.0.0.1. Every request is captured so we assert the create-call
POST, the status-poll sequence, the recording flow into the pull path, the origin
provenance, and the method allowlist (never PUT/PATCH/DELETE)."""
import pytest

from hotato import drive
from hotato._engine.audio import read_wav
from tests import _drive_fakes as fakes


@pytest.fixture(autouse=True)
def _allow_loopback_download(monkeypatch):
    # the fake recording server is on 127.0.0.1, which the default-deny SSRF guard
    # blocks; a LOCAL test recording server is exactly the documented opt-out.
    monkeypatch.setenv("HOTATO_ALLOW_PRIVATE_URLS", "1")


def _scenario():
    return {
        "kind": "hotato.scenario", "version": 1, "id": "s-drive",
        "goal": {"type": "get_refund", "target": "order A-1001"},
        "caller": {"script": [{"say": "Hi, I need help"}, {"say": "with my order"}]},
    }


# --- Twilio -----------------------------------------------------------------

def test_place_call_twilio_drives_polls_and_pulls(tmp_path):
    recorder = fakes.Recorder()
    stereo = fakes.stereo_wav_bytes(tmp_path)
    server, base = fakes.start(fakes.twilio_handler(recorder, stereo))
    try:
        result = drive.place_call_twilio(
            _scenario(), to_number="+15005550001", from_number="+15005550002",
            sid="AC1", token="tok", base_url=base, poll_interval=0, max_wait=5,
            out_path=str(tmp_path / "out.wav"),
        )
    finally:
        server.shutdown()

    # the pulled recording is a real 2-channel WAV
    assert read_wav(result["recording"]).num_channels == 2
    # honest provenance: real agent conversation, scripted-twiml caller
    assert result["provider"] == "twilio"
    assert result["provider_call_id"] == "CA_call"
    assert result["recording_sid"] == "RE_rec"
    assert result["status"] == "completed"
    assert result["origin"] == {
        "kind": "real", "provider": "twilio", "provider_call_id": "CA_call",
        "caller": "scripted-twiml", "recording_sid": "RE_rec",
        "direction": "inbound-to-agent",
    }

    # the create-call POST carried the scripted TwiML + dual-channel recording opts
    posts = recorder.by("POST", "/Calls.json")
    assert len(posts) == 1
    body = posts[0]["body"]
    assert body["To"] == "+15005550001" and body["From"] == "+15005550002"
    assert body["Record"] == "true" and body["RecordingChannels"] == "dual"
    assert "<Response>" in body["Twiml"] and "<Say>Hi, I need help</Say>" in body["Twiml"]
    # Basic auth on every request
    assert all(r["auth"].startswith("Basic ") for r in recorder.requests)
    # status was polled to completion (>= 2 GETs on the call resource)
    assert recorder.status_polls >= 2
    # METHOD ALLOWLIST: only GET/POST were ever issued -- nothing mutated a config
    assert set(recorder.methods) <= {"GET", "POST"}


def test_place_call_twilio_raises_on_non_completed_status(tmp_path):
    recorder = fakes.Recorder()
    stereo = fakes.stereo_wav_bytes(tmp_path)
    # a handler whose call goes straight to a terminal 'failed' (busy/no-answer/...)
    Base = fakes.twilio_handler(recorder, stereo)

    class Failing(Base):
        def do_GET(self):
            if "/Calls/CA_call.json" in self.path:
                self._record("GET")
                return self._json({"sid": "CA_call", "status": "failed"})
            return super().do_GET()

    server, base = fakes.start(Failing)
    try:
        with pytest.raises(ValueError, match="not 'completed'"):
            drive.place_call_twilio(
                _scenario(), to_number="+1", from_number="+2", sid="AC1",
                token="tok", base_url=base, poll_interval=0, max_wait=5)
    finally:
        server.shutdown()
    # no recording was ever fetched for a failed call
    assert not recorder.by("GET", "/Recordings/")


# --- Vapi -------------------------------------------------------------------

def test_place_call_vapi_originates_polls_and_pulls(tmp_path):
    recorder = fakes.Recorder()
    stereo = fakes.stereo_wav_bytes(tmp_path)
    server, base = fakes.start(fakes.vapi_handler(recorder, stereo))
    try:
        result = drive.place_call_vapi(
            "asst_clone", phone_number_id="pn_1", customer_number="+15005550003",
            api_key="sk-test", base_url=base, poll_interval=0, max_wait=5,
            out_path=str(tmp_path / "out.wav"),
        )
    finally:
        server.shutdown()

    assert read_wav(result["recording"]).num_channels == 2
    assert result["provider"] == "vapi"
    assert result["provider_call_id"] == "vc_1"
    assert result["status"] == "ended"
    assert result["origin"]["kind"] == "real"
    assert result["origin"]["caller"] == "assistant-originated"
    assert result["origin"]["provider_call_id"] == "vc_1"
    assert result["origin"]["assistant_id"] == "asst_clone"

    # the create-call POST originated FROM the assistant TO the customer number
    posts = recorder.by("POST", "/call")
    assert len(posts) == 1
    body = posts[0]["body"]
    assert body["assistantId"] == "asst_clone"
    assert body["phoneNumberId"] == "pn_1"
    assert body["customer"] == {"number": "+15005550003"}
    # Bearer auth on the API calls (create + status poll)...
    call_reqs = [r for r in recorder.requests if "/call" in r["path"]]
    assert call_reqs and all(r["auth"] == "Bearer sk-test" for r in call_reqs)
    # ...but the pre-signed recording download carries NO credential (never
    # attaches the API key to a media URL from the vendor's JSON response)
    media = [r for r in recorder.requests if "/rec/" in r["path"]]
    assert media and all(r["auth"] is None for r in media)
    assert recorder.status_polls >= 2
    # METHOD ALLOWLIST: GET/POST only
    assert set(recorder.methods) <= {"GET", "POST"}


def test_place_call_vapi_accepts_a_clone_dict_as_the_assistant():
    # a clone dict from apply_variant (clone_id) resolves to the assistant id
    assert drive._assistant_id({"clone_id": "asst_9", "pending": True}) == "asst_9"
    assert drive._assistant_id("asst_str") == "asst_str"
    with pytest.raises(ValueError, match="assistant id"):
        drive._assistant_id({"nope": 1})
