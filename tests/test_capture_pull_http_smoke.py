"""Strong pull tests (audit gap 2): the capture_vapi / capture_retell /
capture_twilio pull paths exercised against a REAL local HTTPServer over real
127.0.0.1 sockets -- NOT a monkeypatched urlopen. Covers success, the auth
header, the recording-URL fallback chains, and a 404, so the whole client path
(request build, JSON parse, URL selection, validated download, atomic write) runs
for real, the same strength the clone/apply path already had in
test_adapter_http_smoke."""
from urllib.parse import parse_qs, urlparse

import pytest

from hotato import capture as cap
from hotato._engine.audio import read_wav
from tests import _drive_fakes as fakes


@pytest.fixture(autouse=True)
def _env(monkeypatch):
    # the media server is on 127.0.0.1 -> the default-deny SSRF guard blocks it
    # unless the operator opts in; a LOCAL test recording server is the documented
    # case. Also clear the mono escape hatch so the mono-policy tests are honest.
    monkeypatch.setenv("HOTATO_ALLOW_PRIVATE_URLS", "1")
    monkeypatch.delenv("HOTATO_ALLOW_MONO", raising=False)


# --- Vapi: GET /call/{id} -> artifact.recording.stereoUrl -------------------

def _vapi_handler(recorder, stereo, *, call_id="v1", legacy=False, not_found=False):
    Base = fakes.base_handler(recorder)

    class H(Base):
        def do_GET(self):
            self._record("GET")
            path = urlparse(self.path).path
            if path == f"/call/{call_id}":
                if not_found:
                    return self._json({"error": "not found"}, 404)
                url = f"http://{self.headers.get('Host')}/media/stereo.wav"
                call = ({"artifact": {"stereoRecordingUrl": url}} if legacy
                        else {"artifact": {"recording": {"stereoUrl": url}}})
                return self._json(call)
            if path == "/media/stereo.wav":
                return self._wav(stereo)
            return self._404()

    return H


def test_capture_vapi_pull_success_and_auth_header(tmp_path):
    recorder = fakes.Recorder()
    stereo = fakes.stereo_wav_bytes(tmp_path)
    server, base = fakes.start(_vapi_handler(recorder, stereo))
    try:
        out = cap.capture_vapi(call_id="v1", api_key="k", base_url=base,
                               out_path=str(tmp_path / "o.wav"))
    finally:
        server.shutdown()
    assert read_wav(out).num_channels == 2
    # the call GET carried the Bearer key; the pre-signed media GET carried none
    assert recorder.by("GET", "/call/v1")[0]["auth"] == "Bearer k"
    assert recorder.by("GET", "/media/")[0]["auth"] is None
    assert set(recorder.methods) == {"GET"}


def test_capture_vapi_pull_falls_back_to_legacy_stereo_field(tmp_path):
    recorder = fakes.Recorder()
    stereo = fakes.stereo_wav_bytes(tmp_path)
    server, base = fakes.start(_vapi_handler(recorder, stereo, legacy=True))
    try:
        out = cap.capture_vapi(call_id="v1", api_key="k", base_url=base,
                               out_path=str(tmp_path / "o.wav"))
    finally:
        server.shutdown()
    # the current stereoUrl was absent; the deprecated artifact.stereoRecordingUrl
    # fallback still yielded a real 2-channel download
    assert read_wav(out).num_channels == 2


def test_capture_vapi_pull_404_is_a_clean_error(tmp_path):
    recorder = fakes.Recorder()
    server, base = fakes.start(_vapi_handler(recorder, b"", not_found=True))
    try:
        with pytest.raises(ValueError, match="404"):
            cap.capture_vapi(call_id="v1", api_key="k", base_url=base)
    finally:
        server.shutdown()
    # the media was never fetched on a 404 call lookup
    assert not recorder.by("GET", "/media/")


# --- Retell: GET /v2/get-call/{id} -> *_multi_channel_url -------------------

def _retell_handler(recorder, stereo, *, call_id="c1", field="scrubbed",
                    not_found=False):
    Base = fakes.base_handler(recorder)

    class H(Base):
        def do_GET(self):
            self._record("GET")
            path = urlparse(self.path).path
            if path == f"/v2/get-call/{call_id}":
                if not_found:
                    return self._json({"error": "not found"}, 404)
                url = f"http://{self.headers.get('Host')}/media/multi.wav"
                key = ("scrubbed_recording_multi_channel_url" if field == "scrubbed"
                       else "recording_multi_channel_url")
                return self._json({key: url})
            if path == "/media/multi.wav":
                return self._wav(stereo)
            return self._404()

    return H


def test_capture_retell_pull_prefers_scrubbed_and_carries_auth(tmp_path):
    recorder = fakes.Recorder()
    stereo = fakes.stereo_wav_bytes(tmp_path)
    server, base = fakes.start(_retell_handler(recorder, stereo, field="scrubbed"))
    try:
        out = cap.capture_retell(call_id="c1", api_key="k", base_url=base,
                                 out_path=str(tmp_path / "o.wav"))
    finally:
        server.shutdown()
    assert read_wav(out).num_channels == 2
    assert recorder.by("GET", "/v2/get-call/c1")[0]["auth"] == "Bearer k"


def test_capture_retell_pull_falls_back_to_unscrubbed_multichannel(tmp_path):
    recorder = fakes.Recorder()
    stereo = fakes.stereo_wav_bytes(tmp_path)
    server, base = fakes.start(_retell_handler(recorder, stereo, field="plain"))
    try:
        out = cap.capture_retell(call_id="c1", api_key="k", base_url=base,
                                 out_path=str(tmp_path / "o.wav"))
    finally:
        server.shutdown()
    assert read_wav(out).num_channels == 2


def test_capture_retell_pull_404_is_a_clean_error(tmp_path):
    recorder = fakes.Recorder()
    server, base = fakes.start(_retell_handler(recorder, b"", not_found=True))
    try:
        with pytest.raises(ValueError, match="404"):
            cap.capture_retell(call_id="c1", api_key="k", base_url=base)
    finally:
        server.shutdown()


# --- Twilio: Recordings/{sid}.wav?RequestedChannels=2 -----------------------

def _twilio_handler(recorder, stereo, *, mono=None, force_400=False,
                    account_sid="AC1", rec_sid="RE1"):
    Base = fakes.base_handler(recorder)
    media = f"/2010-04-01/Accounts/{account_sid}/Recordings/{rec_sid}.wav"

    class H(Base):
        def do_GET(self):
            self._record("GET")
            parsed = urlparse(self.path)
            if parsed.path == media:
                ch = parse_qs(parsed.query).get("RequestedChannels", ["2"])[0]
                if ch == "2":
                    if force_400:
                        self.send_response(400)
                        self.send_header("Content-Length", "0")
                        self.end_headers()
                        return
                    return self._wav(stereo)
                if ch == "1":
                    return self._wav(mono if mono is not None else stereo)
            return self._404()

    return H


def test_capture_twilio_pull_success_and_basic_auth(tmp_path):
    recorder = fakes.Recorder()
    stereo = fakes.stereo_wav_bytes(tmp_path)
    server, base = fakes.start(_twilio_handler(recorder, stereo))
    try:
        out = cap.capture_twilio(recording_sid="RE1", account_sid="AC1",
                                 auth_token="t", base_url=base,
                                 out_path=str(tmp_path / "o.wav"))
    finally:
        server.shutdown()
    assert read_wav(out).num_channels == 2
    media = recorder.by("GET", "/Recordings/RE1.wav")[0]
    assert "RequestedChannels=2" in media["path"]
    # Twilio media is fetched WITH the Basic auth (operator-built URL, same host)
    assert media["auth"].startswith("Basic ")
    assert set(recorder.methods) == {"GET"}


def test_capture_twilio_pull_400_without_allow_mono_is_clean_error(tmp_path):
    recorder = fakes.Recorder()
    stereo = fakes.stereo_wav_bytes(tmp_path)
    server, base = fakes.start(_twilio_handler(recorder, stereo, force_400=True))
    try:
        with pytest.raises(ValueError, match="mono"):
            cap.capture_twilio(recording_sid="RE1", account_sid="AC1",
                               auth_token="t", base_url=base)
    finally:
        server.shutdown()
    # the mono fallback was NOT fetched without the opt-in
    assert not any("RequestedChannels=1" in r["path"] for r in recorder.requests)


def test_capture_twilio_pull_400_with_allow_mono_falls_back_to_mono(tmp_path):
    recorder = fakes.Recorder()
    stereo = fakes.stereo_wav_bytes(tmp_path)
    mono = fakes.stereo_wav_bytes(tmp_path, channels=1, name="mono.wav")
    server, base = fakes.start(_twilio_handler(recorder, stereo, mono=mono,
                                               force_400=True))
    try:
        out = cap.capture_twilio(recording_sid="RE1", account_sid="AC1",
                                 auth_token="t", base_url=base, allow_mono=True,
                                 out_path=str(tmp_path / "o.wav"))
    finally:
        server.shutdown()
    # the documented fallback re-requested RequestedChannels=1 and scored the mono
    assert any("RequestedChannels=1" in r["path"] for r in recorder.requests)
    assert read_wav(out).num_channels == 1
