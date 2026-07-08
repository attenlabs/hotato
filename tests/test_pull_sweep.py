"""connect -> pull -> sweep: list parsers, mono adapters, the pull loop, and the
sweep end-to-end -- all fully offline.

Every test mocks ``urllib.request.urlopen`` (the only HTTP surface in
``hotato.capture``), so nothing here touches the network. Payload shapes mirror
the endpoints verified verbatim in
``hotato-launch/INTEGRATION-SPEC-2026-07-07.md``:

  * Vapi        GET /call -> JSON array of Call objects (id, createdAt)
  * Twilio      GET .../Recordings.json -> {"recordings":[{sid, date_created}]}
  * Bland       GET /v1/calls -> {"calls":[{call_id}]}
  * ElevenLabs  GET /v1/convai/conversations -> {"conversations":[{conversation_id}]}
  * Synthflow   GET /v2/calls -> response.response.calls[].call_id
  * Millis      GET /call-logs -> {"histories":[{session_id}]}
  * Cartesia    GET /agents/calls -> {"data":[{id}]}
  * Retell      list-calls UNCONFIRMED -> no endpoint, explicit ids only
"""

import io
import json
import os
import urllib.error
import urllib.request
from importlib import resources

import pytest

from hotato import capture as cap
from hotato import cli
from hotato._engine.audio import read_wav, write_wav


# --- offline HTTP plumbing (longest-key-first routing) ----------------------

class _Resp:
    def __init__(self, data):
        self._data = data

    def read(self):
        return self._data

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def _install(monkeypatch, routes, seen=None):
    """Route by URL substring -> bytes (or an Exception to raise). Longest key
    wins, so '/v1/calls/b1' matches before '/v1/calls'."""
    keys = sorted(routes, key=len, reverse=True)

    def fake_urlopen(req, timeout=None):
        url = req.full_url
        if seen is not None:
            seen.append(req)
        for key in keys:
            if key in url:
                payload = routes[key]
                if isinstance(payload, Exception):
                    raise payload
                return _Resp(payload)
        raise AssertionError(f"unexpected URL fetched offline: {url}")

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)


def _http(code, url="https://x.test"):
    return urllib.error.HTTPError(url, code, "err", None, io.BytesIO(b"nope"))


def _urls(seen):
    return [r.full_url for r in seen]


def _stereo_bytes(tmp_path, name="s.wav"):
    p = tmp_path / name
    write_wav(str(p), 8000, [[0.0] * 800, [0.0] * 800])
    return p.read_bytes()


def _mono_bytes(tmp_path, name="m.wav"):
    p = tmp_path / name
    write_wav(str(p), 8000, [[0.0] * 800])
    return p.read_bytes()


def _bundled_stereo():
    return (resources.files("hotato")
            .joinpath("data", "audio", "01-hard-interruption.example.wav")
            .read_bytes())


@pytest.fixture(autouse=True)
def _isolated_home(tmp_path, monkeypatch):
    monkeypatch.setenv("HOTATO_HOME", str(tmp_path / "home"))
    monkeypatch.delenv("HOTATO_ALLOW_MONO", raising=False)
    for var in ("VAPI_API_KEY", "RETELL_API_KEY", "TWILIO_ACCOUNT_SID",
                "TWILIO_AUTH_TOKEN", "BLAND_API_KEY", "ELEVENLABS_API_KEY",
                "SYNTHFLOW_API_KEY", "SYNTHFLOW_MODEL_ID", "MILLIS_API_KEY",
                "CARTESIA_API_KEY", "CARTESIA_AGENT_ID"):
        monkeypatch.delenv(var, raising=False)


# =========================================================================
# 1. Per-platform LIST parsers (verified field paths)
# =========================================================================

def test_list_vapi_parses_array_of_call_objects(monkeypatch):
    arr = [{"id": "v1", "createdAt": "2026-07-06T10:00:00Z"},
           {"id": "v2", "createdAt": "2026-07-07T10:00:00Z"}]
    seen = []
    _install(monkeypatch, {"api.vapi.ai/call": json.dumps(arr).encode()}, seen)
    items = cap.list_calls("vapi", {"api_key": "k"}, limit=50)
    assert [it["id"] for it in items] == ["v2", "v1"]  # newest first
    assert seen[0].get_header("Authorization") == "Bearer k"


def test_list_twilio_parses_recordings_array(monkeypatch):
    body = {"recordings": [{"sid": "RE1", "date_created": "Mon, 06 Jul 2026 10:00:00 +0000"},
                           {"sid": "RE2", "date_created": "Tue, 07 Jul 2026 10:00:00 +0000"}]}
    _install(monkeypatch, {"Recordings.json": json.dumps(body).encode()})
    items = cap.list_calls("twilio", {"account_sid": "AC1", "auth_token": "t"}, limit=50)
    assert {it["id"] for it in items} == {"RE1", "RE2"}


def test_list_bland_parses_calls_array(monkeypatch):
    body = {"total_count": 2, "count": 2,
            "calls": [{"call_id": "b1"}, {"c_id": "b2"}]}
    seen = []
    _install(monkeypatch, {"api.bland.ai/v1/calls": json.dumps(body).encode()}, seen)
    items = cap.list_calls("bland", {"api_key": "k"}, limit=50)
    assert {it["id"] for it in items} == {"b1", "b2"}
    assert seen[0].get_header("Authorization") == "k"  # raw key, header 'authorization'


def test_list_elevenlabs_parses_conversations(monkeypatch):
    body = {"conversations": [{"conversation_id": "e1", "start_time_unix_secs": 1},
                              {"conversation_id": "e2", "start_time_unix_secs": 2}],
            "has_more": False}
    seen = []
    _install(monkeypatch, {"/v1/convai/conversations": json.dumps(body).encode()}, seen)
    items = cap.list_calls("elevenlabs", {"api_key": "k"}, limit=50)
    assert [it["id"] for it in items] == ["e2", "e1"]
    assert seen[0].get_header("Xi-api-key") == "k"


def test_list_synthflow_navigates_response_response_calls(monkeypatch):
    body = {"response": {"response": {"calls": [{"call_id": "s1", "start_time": 1},
                                                {"call_id": "s2", "start_time": 2}]}}}
    _install(monkeypatch, {"api.synthflow.ai/v2/calls": json.dumps(body).encode()})
    items = cap.list_calls("synthflow", {"api_key": "k", "model_id": "m1"}, limit=50)
    assert [it["id"] for it in items] == ["s2", "s1"]


def test_list_synthflow_single_response_nesting_also_parses(monkeypatch):
    body = {"response": {"calls": [{"call_id": "s9"}]}}
    _install(monkeypatch, {"api.synthflow.ai/v2/calls": json.dumps(body).encode()})
    items = cap.list_calls("synthflow", {"api_key": "k", "model_id": "m1"}, limit=50)
    assert [it["id"] for it in items] == ["s9"]


def test_list_synthflow_without_model_id_is_clean_error(monkeypatch):
    _install(monkeypatch, {"api.synthflow.ai/v2/calls": b"{}"})
    with pytest.raises(ValueError, match="model_id"):
        cap.list_calls("synthflow", {"api_key": "k"}, limit=50)


def test_list_millis_parses_histories(monkeypatch):
    body = {"histories": [{"session_id": "m1", "ts": 1}, {"session_id": "m2", "ts": 2}],
            "next_cursor": None}
    _install(monkeypatch, {"/call-logs": json.dumps(body).encode()})
    items = cap.list_calls("millis", {"api_key": "k"}, limit=50)
    assert [it["id"] for it in items] == ["m2", "m1"]


def test_list_cartesia_parses_data_and_requires_agent_id(monkeypatch):
    body = {"data": [{"id": "c1", "start_time": 1}], "has_more": False}
    seen = []
    _install(monkeypatch, {"/agents/calls": json.dumps(body).encode()}, seen)
    items = cap.list_calls("cartesia", {"api_key": "k", "agent_id": "a1"}, limit=50)
    assert [it["id"] for it in items] == ["c1"]
    assert seen[0].get_header("Cartesia-version") == "2026-03-01"
    _install(monkeypatch, {"/agents/calls": body and json.dumps(body).encode()})
    with pytest.raises(ValueError, match="agent_id"):
        cap.list_calls("cartesia", {"api_key": "k"}, limit=50)


def test_list_retell_is_honest_unconfirmed_no_fabricated_endpoint(monkeypatch):
    # No urlopen installed: a fabricated call would raise AssertionError, but we
    # must fail BEFORE any network with an honest "no verified list" error.
    with pytest.raises(ValueError, match="no verified list-calls endpoint"):
        cap.list_calls("retell", {"api_key": "k"}, limit=50)


def test_list_livekit_pipecat_are_capture_in_your_infra(monkeypatch):
    for stack in ("livekit", "pipecat"):
        with pytest.raises(ValueError, match="capture-in-your-infra"):
            cap.list_calls(stack, {}, limit=50)


# =========================================================================
# 2. A bad payload -> clean error, never an action-from-payload
# =========================================================================

def test_bad_list_payload_is_clean_error_and_fetches_nothing(tmp_path, monkeypatch):
    _install(monkeypatch, {"api.vapi.ai/call": json.dumps({"not": "an array"}).encode()})
    calls = []
    monkeypatch.setattr(cap, "fetch_one", lambda *a, **k: calls.append(a) or "x")
    with pytest.raises(ValueError, match="did not contain the documented"):
        cap.pull("vapi", {"api_key": "k"}, out_dir=str(tmp_path / "d"))
    assert calls == []  # nothing was fetched off a malformed payload


def test_bad_payload_via_cli_exits_2(monkeypatch, tmp_path, capsys):
    _install(monkeypatch, {"api.vapi.ai/call": b"[not json"})
    rc = cli.main(["pull", "--stack", "vapi", "--api-key", "k",
                   "--out", str(tmp_path / "d")])
    assert rc == 2


# =========================================================================
# 3. The pull loop (list -> loop the single-call fetch -> honest skips)
# =========================================================================

def test_pull_loop_full_chain_vapi(tmp_path, monkeypatch):
    arr = [{"id": "v1", "createdAt": "2026-07-07T10:00:00Z"},
           {"id": "v2", "createdAt": "2026-07-06T10:00:00Z"}]
    stereo = _stereo_bytes(tmp_path)
    routes = {
        "api.vapi.ai/call?": json.dumps(arr).encode(),
        "/call/v1": json.dumps({"artifact": {"recording": {"stereoUrl": "https://m.test/v1.wav"}}}).encode(),
        "/call/v2": json.dumps({"artifact": {"recording": {"stereoUrl": "https://m.test/v2.wav"}}}).encode(),
        "v1.wav": stereo,
        "v2.wav": stereo,
    }
    _install(monkeypatch, routes)
    out = tmp_path / "pulled"
    res = cap.pull("vapi", {"api_key": "k"}, out_dir=str(out), limit=50)
    assert res["listed"] == 2
    assert len(res["pulled"]) == 2 and not res["skipped"]
    got = sorted(os.path.basename(p["path"]) for p in res["pulled"])
    assert got == ["vapi__v1.wav", "vapi__v2.wav"]
    assert all(read_wav(p["path"]).num_channels == 2 for p in res["pulled"])


def test_pull_loop_skips_one_bad_call_and_continues(tmp_path, monkeypatch):
    ok = _stereo_bytes(tmp_path)

    def fake_fetch(stack, ident, creds, out_path=None, *, allow_mono=False):
        if ident == "bad":
            raise ValueError("no stereo recording on this call")
        with open(out_path, "wb") as fh:
            fh.write(ok)
        return out_path

    monkeypatch.setattr(cap, "fetch_one", fake_fetch)
    _install(monkeypatch, {"api.vapi.ai/call": json.dumps(
        [{"id": "good"}, {"id": "bad"}]).encode()})
    res = cap.pull("vapi", {"api_key": "k"}, out_dir=str(tmp_path / "d"))
    assert [p["id"] for p in res["pulled"]] == ["good"]
    assert [s["id"] for s in res["skipped"]] == ["bad"]
    assert "no stereo recording" in res["skipped"][0]["reason"]


def test_pull_explicit_ids_work_for_retell_without_a_list(tmp_path, monkeypatch):
    calls = []

    def fake_fetch(stack, ident, creds, out_path=None, *, allow_mono=False):
        calls.append((stack, ident))
        open(out_path, "wb").close()
        return out_path

    monkeypatch.setattr(cap, "fetch_one", fake_fetch)
    # No urlopen installed: retell must NOT hit a list endpoint when ids given.
    res = cap.pull("retell", {"api_key": "k"}, out_dir=str(tmp_path / "d"),
                   ids=["c1", "c2"])
    assert [i for _, i in calls] == ["c1", "c2"]
    assert len(res["pulled"]) == 2


def test_pull_retell_without_ids_is_honest_error(tmp_path, monkeypatch):
    with pytest.raises(ValueError, match="no verified list-calls endpoint"):
        cap.pull("retell", {"api_key": "k"}, out_dir=str(tmp_path / "d"))


# =========================================================================
# 4. --allow-mono gating
# =========================================================================

def test_pull_mono_stack_requires_allow_mono(tmp_path, monkeypatch):
    monkeypatch.setattr(cap, "fetch_one", lambda *a, **k: None)
    with pytest.raises(ValueError, match="allow-mono"):
        cap.pull("bland", {"api_key": "k"}, out_dir=str(tmp_path / "d"))


def test_pull_mono_stack_with_allow_mono_proceeds(tmp_path, monkeypatch):
    def fake_fetch(stack, ident, creds, out_path=None, *, allow_mono=False):
        assert allow_mono is True
        open(out_path, "wb").close()
        return out_path

    monkeypatch.setattr(cap, "fetch_one", fake_fetch)
    _install(monkeypatch, {"api.bland.ai/v1/calls": json.dumps(
        {"calls": [{"call_id": "b1"}]}).encode()})
    res = cap.pull("bland", {"api_key": "k"}, out_dir=str(tmp_path / "d"),
                   allow_mono=True)
    assert len(res["pulled"]) == 1


def test_cli_pull_mono_without_allow_mono_exits_2(tmp_path, monkeypatch, capsys):
    rc = cli.main(["pull", "--stack", "elevenlabs", "--api-key", "k",
                   "--out", str(tmp_path / "d")])
    assert rc == 2
    assert "allow-mono" in capsys.readouterr().err


# =========================================================================
# 5. Mono single-fetch adapters (spec endpoints, direct download)
# =========================================================================

def test_capture_bland_downloads_recording_url(tmp_path, monkeypatch):
    mono = _mono_bytes(tmp_path)
    routes = {"/v1/calls/b1": json.dumps({"recording_url": "https://m.test/b1.wav"}).encode(),
              "b1.wav": mono}
    _install(monkeypatch, routes)
    out = cap.capture_bland(call_id="b1", api_key="k", out_path=str(tmp_path / "o.wav"))
    assert read_wav(out).num_channels == 1


def test_capture_elevenlabs_downloads_audio_endpoint(tmp_path, monkeypatch):
    mono = _mono_bytes(tmp_path)
    seen = []
    _install(monkeypatch, {"/v1/convai/conversations/e1/audio": mono}, seen)
    out = cap.capture_elevenlabs(conversation_id="e1", api_key="k",
                                 out_path=str(tmp_path / "o.wav"))
    assert (tmp_path / "o.wav").read_bytes() == mono
    assert seen[0].get_header("Xi-api-key") == "k"


def test_capture_synthflow_extracts_recording_url(tmp_path, monkeypatch):
    mono = _mono_bytes(tmp_path)
    body = {"response": {"response": {"calls": [{"recording_url": "https://m.test/s1.wav"}]}}}
    _install(monkeypatch, {"/v2/calls/s1": json.dumps(body).encode(), "s1.wav": mono})
    out = cap.capture_synthflow(call_id="s1", api_key="k", out_path=str(tmp_path / "o.wav"))
    assert (tmp_path / "o.wav").read_bytes() == mono


def test_capture_millis_extracts_recording(tmp_path, monkeypatch):
    mono = _mono_bytes(tmp_path)
    body = {"recording": {"recording_url": "https://m.test/m1.wav"}}
    _install(monkeypatch, {"/call-logs/m1": json.dumps(body).encode(), "m1.wav": mono})
    out = cap.capture_millis(session_id="m1", api_key="k", out_path=str(tmp_path / "o.wav"))
    assert (tmp_path / "o.wav").read_bytes() == mono


def test_capture_cartesia_downloads_audio_with_version_header(tmp_path, monkeypatch):
    mono = _mono_bytes(tmp_path)
    seen = []
    _install(monkeypatch, {"/agents/calls/c1/audio": mono}, seen)
    cap.capture_cartesia(call_id="c1", api_key="k", out_path=str(tmp_path / "o.wav"))
    assert (tmp_path / "o.wav").read_bytes() == mono
    assert seen[0].get_header("Cartesia-version") == "2026-03-01"


def test_capture_mono_missing_recording_is_clean_error(tmp_path, monkeypatch):
    _install(monkeypatch, {"/v1/calls/b1": json.dumps({"status": "completed"}).encode()})
    with pytest.raises(ValueError, match="no recording_url"):
        cap.capture_bland(call_id="b1", api_key="k", out_path=str(tmp_path / "o.wav"))


# =========================================================================
# 6. capture --stack <mono> end to end (degraded, gated)
# =========================================================================

def test_cli_capture_mono_stack_needs_allow_mono(tmp_path, monkeypatch):
    rc = cli.main(["capture", "--stack", "bland", "--call-id", "b1", "--api-key", "k"])
    assert rc == 2  # mono without --allow-mono


def test_cli_capture_mono_stack_scores_degraded_with_allow_mono(tmp_path, monkeypatch, capsys):
    # a bundled mono track stands in for the vendor's combined recording
    sig = read_wav(str(resources.files("hotato").joinpath(
        "data", "audio", "01-hard-interruption.example.wav")))
    mono_path = tmp_path / "mono.wav"
    write_wav(str(mono_path), sig.sample_rate, [sig.get(0)])
    mono = mono_path.read_bytes()
    routes = {"/v1/calls/b1": json.dumps({"recording_url": "https://m.test/b1.wav"}).encode(),
              "b1.wav": mono}
    _install(monkeypatch, routes)
    rc = cli.main(["capture", "--stack", "bland", "--call-id", "b1", "--api-key", "k",
                   "--allow-mono"])
    assert rc in (0, 1)
    err = capsys.readouterr().err
    assert "degraded" in err


# =========================================================================
# 7. sweep end to end: a mocked pull feeds the real analyze
# =========================================================================

def test_sweep_end_to_end_mocked_pull_feeds_analyze(tmp_path, monkeypatch, capsys):
    stereo = _bundled_stereo()

    def fake_pull(stack, creds, *, out_dir, ids=None, since=None, limit=50,
                  allow_mono=False, log=None):
        os.makedirs(out_dir, exist_ok=True)
        paths = []
        for name in ("vapi__a.wav", "vapi__b.wav"):
            p = os.path.join(out_dir, name)
            with open(p, "wb") as fh:
                fh.write(stereo)
            paths.append({"id": name, "path": p})
        return {"stack": stack, "out_dir": out_dir, "listed": 2,
                "pulled": paths, "skipped": []}

    monkeypatch.setattr(cap, "pull", fake_pull)
    out = tmp_path / "sweep.html"
    rc = cli.main(["sweep", "--stack", "vapi", "--api-key", "k",
                   "--dir", str(tmp_path / "pull"), "--out", str(out), "--no-open"])
    assert rc == 0
    html = out.read_text()
    assert "hotato analyze" in html and "<audio" in html
    assert "candidate" in html
    err = capsys.readouterr().err
    assert "sweep" in err and "2 scanned" in err


def test_sweep_json_carries_pull_summary(tmp_path, monkeypatch, capsys):
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
    out = capsys.readouterr().out
    payload = json.loads(out)
    assert payload["kind"] == "analyze"
    assert payload["pull"]["stack"] == "vapi" and payload["pull"]["pulled"] == 1


def test_download_is_atomic_preserves_existing_file_on_local_write_failure(
        tmp_path, monkeypatch):
    """Regression: _download writes to a sibling temp then os.replace()s, so a
    LOCAL write/finalize failure AFTER a successful fetch (ENOSPC / quota /
    permission race) can never clobber a pre-existing file at dest with a
    truncated mix -- dest is only ever the old bytes or the complete new bytes.
    Before the fix _download did open(dest,'wb').write(data) and destroyed the
    original in place on any such failure."""
    dest = tmp_path / "precious.wav"
    dest.write_bytes(b"PRECIOUS-GOOD-DATA" * 100)
    before = dest.read_bytes()

    monkeypatch.setattr(cap, "_http_get", lambda *a, **k: b"X" * 5000)
    monkeypatch.setattr(cap, "_validate_download_url", lambda u, *a, **k: u)
    # Simulate the finalize step failing (e.g. the rename hitting a full disk).
    def boom(src, dst):
        raise OSError(28, "No space left on device")
    monkeypatch.setattr(cap.os, "replace", boom)

    with pytest.raises(OSError):
        cap._download("http://vendor.example/rec.wav", str(dest))

    assert dest.read_bytes() == before, "original file must be untouched"
    # no orphaned temp part-file left behind next to dest
    leftovers = [p for p in os.listdir(str(tmp_path)) if ".hotato-dl-" in p]
    assert leftovers == [], f"temp part-file leaked: {leftovers}"


def test_download_writes_new_bytes_on_success(tmp_path, monkeypatch):
    """The atomic path still delivers the complete fetched bytes on success."""
    dest = tmp_path / "out.wav"
    monkeypatch.setattr(cap, "_http_get", lambda *a, **k: b"NEWDATA" * 10)
    monkeypatch.setattr(cap, "_validate_download_url", lambda u, *a, **k: u)
    cap._download("http://vendor.example/rec.wav", str(dest))
    assert dest.read_bytes() == b"NEWDATA" * 10
    assert [p for p in os.listdir(str(tmp_path)) if ".hotato-dl-" in p] == []
