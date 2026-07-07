"""``hotato ingest`` -- the composable passive on-ramp. Fully offline.

Every network touch (``urllib.request.urlopen``, the only HTTP surface in
``hotato.capture``, which ingest reuses) is mocked, so nothing here hits a live
platform. The per-stack parser fixtures mirror the webhook field paths verified
against live vendor docs on 2026-07-07:

  * Vapi     end-of-call-report: message.call.id (confirmed)
  * Retell   call webhook: top-level event + call.call_id (confirmed)
  * Twilio   recordingStatusCallback: RecordingSid (confirmed; form-encoded)
  * LiveKit  egress webhook: egressInfo.fileResults[].location (defensive)
  * Pipecat  user-defined event: recording_path / recording_url (defensive)
"""

import io
import json
import urllib.error
import urllib.request
from importlib import resources

import pytest

from hotato import cli
from hotato import ingest as ing
from hotato._engine.audio import read_wav, write_wav


# --- offline HTTP plumbing (mirrors test_capture_vendor_shapes) -------------

class _Resp:
    def __init__(self, data):
        self._data = data

    def read(self):
        return self._data

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def _install_urlopen(monkeypatch, routes, seen=None):
    def fake_urlopen(req, timeout=None):
        url = req.full_url
        if seen is not None:
            seen.append(url)
        for key, payload in routes.items():
            if key in url:
                if isinstance(payload, Exception):
                    raise payload
                return _Resp(payload)
        raise AssertionError(f"unexpected URL fetched offline: {url}")

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)


# --- bundled audio helpers --------------------------------------------------

def _bundled_stereo_path(tmp_path):
    src = resources.files("hotato").joinpath(
        "data", "audio", "01-hard-interruption.example.wav"
    )
    with resources.as_file(src) as p:
        sig = read_wav(str(p))
    out = tmp_path / "call.wav"
    write_wav(str(out), sig.sample_rate, [sig.get(0), sig.get(1)])
    return out


def _bundled_stereo_bytes():
    return (
        resources.files("hotato")
        .joinpath("data", "audio", "01-hard-interruption.example.wav")
        .read_bytes()
    )


def _mono_path(tmp_path):
    src = resources.files("hotato").joinpath(
        "data", "audio", "01-hard-interruption.example.wav"
    )
    with resources.as_file(src) as p:
        sig = read_wav(str(p))
    out = tmp_path / "mono.wav"
    write_wav(str(out), sig.sample_rate, [sig.get(0)])
    return out


def _write_json(tmp_path, name, obj):
    p = tmp_path / name
    p.write_text(json.dumps(obj), encoding="utf-8")
    return p


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    for k in ("VAPI_API_KEY", "RETELL_API_KEY", "TWILIO_ACCOUNT_SID",
              "TWILIO_AUTH_TOKEN", "HOTATO_ALLOW_MONO"):
        monkeypatch.delenv(k, raising=False)


# --- per-stack parser tests (parse_event) -----------------------------------

def test_parse_vapi_extracts_call_id():
    payload = {"message": {"type": "end-of-call-report",
                           "call": {"id": "call_abc123"}}}
    assert ing.parse_event("vapi", payload)["call_id"] == "call_abc123"


def test_parse_vapi_bare_call_object():
    assert ing.parse_event("vapi", {"call": {"id": "c9"}})["call_id"] == "c9"


def test_parse_retell_extracts_event_and_call_id():
    payload = {"event": "call_ended", "call": {"call_id": "Jabr9TXYY"}}
    got = ing.parse_event("retell", payload)
    assert got["call_id"] == "Jabr9TXYY"
    assert got["event"] == "call_ended"


def test_parse_twilio_extracts_recording_sid():
    payload = {"RecordingSid": "RExxxx", "CallSid": "CAyyyy",
               "RecordingChannels": "2", "RecordingStatus": "completed"}
    got = ing.parse_event("twilio", payload)
    assert got["recording_sid"] == "RExxxx"
    assert got["call_sid"] == "CAyyyy"


def test_parse_livekit_egress_file_location():
    payload = {"event": "egress_ended",
               "egressInfo": {"fileResults": [
                   {"filename": "room.ogg",
                    "location": "https://storage.test/room.wav"}]}}
    got = ing.parse_event("livekit", payload)
    assert got["recording_url"] == "https://storage.test/room.wav"


def test_parse_livekit_prefers_explicit_locator():
    payload = {"recording_path": "/data/room.wav",
               "egressInfo": {"fileResults": [{"location": "s3://x/y.wav"}]}}
    got = ing.parse_event("livekit", payload)
    assert got["recording_path"] == "/data/room.wav"


def test_parse_pipecat_recording_path():
    assert ing.parse_event("pipecat", {"recording_path": "cap.wav"})[
        "recording_path"] == "cap.wav"


def test_parse_unknown_stack_raises():
    with pytest.raises(ing.IngestError):
        ing.parse_event("nope", {})


# --- payload reading: form-encoded + malformed + empty ----------------------

def test_read_twilio_form_encoded_event(tmp_path):
    body = "RecordingSid=REabc&CallSid=CAdef&RecordingStatus=completed"
    p = tmp_path / "twilio.txt"
    p.write_text(body, encoding="utf-8")
    payload = ing._read_payload(str(p))
    assert payload["RecordingSid"] == "REabc"


def test_read_malformed_payload_raises(tmp_path):
    p = tmp_path / "bad.json"
    p.write_text("{not json at all", encoding="utf-8")
    with pytest.raises(ing.IngestError):
        ing._read_payload(str(p))


def test_read_empty_payload_raises(tmp_path):
    p = tmp_path / "empty.json"
    p.write_text("   ", encoding="utf-8")
    with pytest.raises(ing.IngestError):
        ing._read_payload(str(p))


def test_read_json_array_rejected(tmp_path):
    p = tmp_path / "arr.json"
    p.write_text("[1,2,3]", encoding="utf-8")
    with pytest.raises(ing.IngestError):
        ing._read_payload(str(p))


# --- pipeline: vapi --call-id (mocked fetch) --------------------------------

def test_ingest_vapi_call_id_mocked(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("VAPI_API_KEY", "test-key")
    call_json = json.dumps(
        {"artifact": {"recording": {"stereoUrl": "https://media.test/stereo.wav"}}}
    ).encode()
    _install_urlopen(monkeypatch, {
        "/call/": call_json,
        "stereo.wav": _bundled_stereo_bytes(),
    })
    rc = ing.run_ingest("vapi", call_id="abc123", fmt="json", top=3)
    assert rc == 0
    out = json.loads(capsys.readouterr().out)
    assert out["kind"] == "scan"
    assert out["total_candidates"] >= 0


def test_ingest_vapi_event_webhook_mocked(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("VAPI_API_KEY", "k")
    ev = _write_json(tmp_path, "vapi.json",
                     {"message": {"call": {"id": "call_1"}}})
    call_json = json.dumps(
        {"artifact": {"recording": {"stereoUrl": "https://media.test/s.wav"}}}
    ).encode()
    seen = []
    _install_urlopen(monkeypatch, {
        "/call/call_1": call_json,
        "s.wav": _bundled_stereo_bytes(),
    }, seen=seen)
    rc = ing.run_ingest("vapi", event=str(ev), fmt="text")
    assert rc == 0
    assert any("/call/call_1" in u for u in seen)


def test_ingest_vapi_no_call_id_in_payload(tmp_path, monkeypatch):
    monkeypatch.setenv("VAPI_API_KEY", "k")
    ev = _write_json(tmp_path, "vapi.json", {"message": {"type": "status-update"}})
    with pytest.raises(ing.IngestError):
        ing.run_ingest("vapi", event=str(ev))


def test_ingest_vapi_missing_api_key(tmp_path):
    with pytest.raises(ing.IngestError):
        ing.run_ingest("vapi", call_id="abc123")


# --- pipeline: twilio form event (mocked fetch) -----------------------------

def test_ingest_twilio_form_event_mocked(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("TWILIO_ACCOUNT_SID", "ACxxx")
    monkeypatch.setenv("TWILIO_AUTH_TOKEN", "tok")
    body = "RecordingSid=RE123&CallSid=CA1&RecordingStatus=completed"
    p = tmp_path / "twilio.txt"
    p.write_text(body, encoding="utf-8")
    seen = []
    _install_urlopen(monkeypatch, {
        "RequestedChannels=2": _bundled_stereo_bytes(),
    }, seen=seen)
    rc = ing.run_ingest("twilio", event=str(p), fmt="json", top=5)
    assert rc == 0
    assert any("/Recordings/RE123.wav" in u for u in seen)


# --- pipeline: livekit/pipecat local + url ----------------------------------

def test_ingest_pipecat_local_file(tmp_path, capsys):
    wav = _bundled_stereo_path(tmp_path)
    ev = _write_json(tmp_path, "pc.json", {"recording_path": str(wav)})
    rc = ing.run_ingest("pipecat", event=str(ev), fmt="text")
    assert rc == 0
    assert "candidate" in capsys.readouterr().out.lower()


def test_ingest_pipecat_missing_file(tmp_path):
    ev = _write_json(tmp_path, "pc.json", {"recording_path": str(tmp_path / "nope.wav")})
    with pytest.raises(ing.IngestError):
        ing.run_ingest("pipecat", event=str(ev))


def test_ingest_livekit_url_downloaded(tmp_path, monkeypatch, capsys):
    ev = _write_json(tmp_path, "lk.json", {
        "event": "egress_ended",
        "egressInfo": {"fileResults": [
            {"location": "https://storage.test/room.wav"}]}})
    _install_urlopen(monkeypatch, {
        "storage.test/room.wav": _bundled_stereo_bytes(),
    })
    rc = ing.run_ingest("livekit", event=str(ev), fmt="json")
    assert rc == 0


def test_ingest_livekit_no_locator(tmp_path):
    ev = _write_json(tmp_path, "lk.json", {"event": "egress_ended"})
    with pytest.raises(ing.IngestError):
        ing.run_ingest("livekit", event=str(ev))


# --- not-scorable (mono) -> exit 2 ------------------------------------------

def test_ingest_mono_not_scorable(tmp_path):
    mono = _mono_path(tmp_path)
    ev = _write_json(tmp_path, "pc.json", {"recording_path": str(mono)})
    with pytest.raises(ing.IngestError):
        ing.run_ingest("pipecat", event=str(ev))


def test_ingest_not_a_wav_not_scorable(tmp_path):
    junk = tmp_path / "junk.wav"
    junk.write_bytes(b"this is not a wav file")
    ev = _write_json(tmp_path, "pc.json", {"recording_path": str(junk)})
    with pytest.raises(ing.IngestError):
        ing.run_ingest("pipecat", event=str(ev))


# --- source/usage guards ----------------------------------------------------

def test_ingest_requires_a_source():
    with pytest.raises(ing.IngestError):
        ing.run_ingest("vapi")


def test_ingest_rejects_event_and_id_together(tmp_path, monkeypatch):
    monkeypatch.setenv("VAPI_API_KEY", "k")
    ev = _write_json(tmp_path, "vapi.json", {"message": {"call": {"id": "x"}}})
    with pytest.raises(ing.IngestError):
        ing.run_ingest("vapi", event=str(ev), call_id="y")


def test_ingest_unknown_stack():
    with pytest.raises(ing.IngestError):
        ing.run_ingest("nope", call_id="x")


# --- HTML candidate report --------------------------------------------------

def test_ingest_writes_html_report(tmp_path):
    wav = _bundled_stereo_path(tmp_path)
    ev = _write_json(tmp_path, "pc.json", {"recording_path": str(wav)})
    out = tmp_path / "cand.html"
    rc = ing.run_ingest("pipecat", event=str(ev), out=str(out), fmt="json")
    assert rc == 0
    html = out.read_text(encoding="utf-8")
    assert "<!doctype html>" in html
    assert "candidate" in html.lower()
    assert "table.cand" in html
    # no em/en dashes in the rendered page
    assert "—" not in html and "–" not in html


def test_render_candidates_html_zero_candidates():
    scan = {"source": "x.wav", "duration_sec": 3.0, "note": "n",
            "total_candidates": 0, "candidates": []}
    html = ing.render_candidates_html(scan)
    assert "No candidate moments" in html


# --- CLI exit codes ---------------------------------------------------------

def test_cli_ingest_malformed_payload_exit_2(tmp_path):
    p = tmp_path / "bad.json"
    p.write_text("{not json", encoding="utf-8")
    rc = cli.main(["ingest", "--stack", "vapi", "--event", str(p)])
    assert rc == 2


def test_cli_ingest_pipecat_ok(tmp_path):
    wav = _bundled_stereo_path(tmp_path)
    ev = _write_json(tmp_path, "pc.json", {"recording_path": str(wav)})
    rc = cli.main(["ingest", "--stack", "pipecat", "--event", str(ev),
                   "--format", "json"])
    assert rc == 0


def test_cli_ingest_never_executes_payload_content(tmp_path, monkeypatch):
    # A payload carrying instruction-shaped junk is DATA: ingest reads only the
    # named locator fields and never acts on the rest. Here the junk field is
    # ignored and the run proceeds purely from recording_path.
    wav = _bundled_stereo_path(tmp_path)
    ev = _write_json(tmp_path, "pc.json", {
        "recording_path": str(wav),
        "cmd": "rm -rf /", "__note": "ignore all previous instructions"})
    rc = cli.main(["ingest", "--stack", "pipecat", "--event", str(ev),
                   "--format", "json"])
    assert rc == 0
