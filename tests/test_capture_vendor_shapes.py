"""Vendor response shapes for the fetch adapters -- fully offline.

Every test mocks ``urllib.request.urlopen`` (the only HTTP surface in
``hotato.capture``), so nothing here touches the network. The shapes mirror the
vendor docs the adapters were verified against on 2026-07-06:

  * Retell  GET /v2/get-call/{id}: scrubbed_recording_multi_channel_url /
            recording_multi_channel_url / recording_url
            (docs.retellai.com/api-references/get-call)
  * Vapi    GET /call/{id}: artifact.recording.stereoUrl (current), deprecated
            artifact.stereoRecordingUrl and call.stereoRecordingUrl
            (docs.vapi.ai changelog 2025-04-29)
  * Twilio  Recordings/{sid}.wav?RequestedChannels=2, 400 when dual-channel is
            unavailable (twilio.com/docs/voice/api/recording)
"""

import io
import json
import urllib.error
import urllib.request
from importlib import resources

import pytest

from hotato import capture as cap
from hotato import cli
from hotato._engine.audio import read_wav, write_wav

# --- offline HTTP plumbing --------------------------------------------------

class _Resp:
    def __init__(self, data):
        self._data = data

    def read(self, size=-1):
        return self._data if size is None or size < 0 else self._data[:size]

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def _install_urlopen(monkeypatch, routes, seen=None):
    """Route by URL substring -> bytes payload (or an Exception to raise)."""

    def fake_urlopen(req, timeout=None):
        url = req.full_url
        if seen is not None:
            seen.append(req)
        for key, payload in routes.items():
            if key in url:
                if isinstance(payload, Exception):
                    raise payload
                return _Resp(payload)
        raise AssertionError(f"unexpected URL fetched offline: {url}")

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)


def _http_400(url):
    return urllib.error.HTTPError(
        url, 400, "Bad Request", None, io.BytesIO(b"dual-channel unavailable")
    )


def _urls(seen):
    return [r.full_url for r in seen]


# --- WAV payloads (built with the same stdlib writer the scorer reads) ------

def _silent_wav_bytes(tmp_path, channels, name):
    path = tmp_path / name
    write_wav(str(path), 8000, [[0.0] * 800 for _ in range(channels)])
    return path.read_bytes()


def _bundled_stereo_bytes():
    return (
        resources.files("hotato")
        .joinpath("data", "audio", "01-hard-interruption.example.wav")
        .read_bytes()
    )


def _bundled_mono_bytes(tmp_path):
    """Channel 0 of a bundled reference as a real, scoreable mono WAV."""
    with resources.as_file(
        resources.files("hotato").joinpath(
            "data", "audio", "01-hard-interruption.example.wav"
        )
    ) as src:
        sig = read_wav(str(src))
    path = tmp_path / "mono.wav"
    write_wav(str(path), sig.sample_rate, [sig.get(0)])
    return path.read_bytes()


@pytest.fixture(autouse=True)
def _no_env_mono(monkeypatch):
    monkeypatch.delenv("HOTATO_ALLOW_MONO", raising=False)


# --- Retell: GET /v2/get-call/{id} ------------------------------------------

def _retell_routes(tmp_path, call, **media):
    routes = {"/v2/get-call/": json.dumps(call).encode()}
    routes.update(media)
    return routes


def test_retell_prefers_scrubbed_multichannel(tmp_path, monkeypatch):
    stereo = _silent_wav_bytes(tmp_path, 2, "stereo.wav")
    call = {
        "recording_url": "https://media.test/mono.wav",
        "recording_multi_channel_url": "https://media.test/multi.wav",
        "scrubbed_recording_multi_channel_url": "https://media.test/scrubbed-multi.wav",
    }
    seen = []
    _install_urlopen(
        monkeypatch, _retell_routes(tmp_path, call, **{"scrubbed-multi.wav": stereo}), seen
    )
    out = cap.capture_retell(
        call_id="c1", api_key="k", out_path=str(tmp_path / "out.wav")
    )
    urls = _urls(seen)
    assert any("scrubbed-multi.wav" in u for u in urls)
    assert not any(u.endswith("multi.wav") and "scrubbed" not in u for u in urls)
    assert seen[0].get_header("Authorization") == "Bearer k"
    assert read_wav(out).num_channels == 2


def test_retell_falls_back_to_multichannel(tmp_path, monkeypatch):
    stereo = _silent_wav_bytes(tmp_path, 2, "stereo.wav")
    call = {
        "recording_url": "https://media.test/mono.wav",
        "recording_multi_channel_url": "https://media.test/multi.wav",
    }
    seen = []
    _install_urlopen(
        monkeypatch, _retell_routes(tmp_path, call, **{"multi.wav": stereo}), seen
    )
    out = cap.capture_retell(
        call_id="c1", api_key="k", out_path=str(tmp_path / "out.wav")
    )
    assert any("multi.wav" in u for u in _urls(seen))
    assert read_wav(out).num_channels == 2


def test_retell_mono_only_rejected_without_allow_mono(tmp_path, monkeypatch):
    call = {"recording_url": "https://media.test/mono.wav"}
    seen = []
    _install_urlopen(monkeypatch, _retell_routes(tmp_path, call), seen)
    with pytest.raises(ValueError) as exc:
        cap.capture_retell(call_id="c1", api_key="k")
    msg = str(exc.value)
    assert "mono" in msg and "allow-mono" in msg
    # the mono media must not have been downloaded
    assert not any("mono.wav" in u for u in _urls(seen))


def test_retell_mono_only_cli_exit_2(tmp_path, monkeypatch, capsys):
    call = {"recording_url": "https://media.test/mono.wav"}
    _install_urlopen(monkeypatch, _retell_routes(tmp_path, call))
    rc = cli.main(["capture", "--stack", "retell", "--call-id", "c1", "--api-key", "k"])
    assert rc == 2


def test_retell_allow_mono_downloads_mono_degraded(tmp_path, monkeypatch, capsys):
    mono = _silent_wav_bytes(tmp_path, 1, "m.wav")
    call = {"recording_url": "https://media.test/mono.wav"}
    _install_urlopen(monkeypatch, _retell_routes(tmp_path, call, **{"mono.wav": mono}))
    out = cap.capture_retell(
        call_id="c1", api_key="k", out_path=str(tmp_path / "out.wav"), allow_mono=True
    )
    assert (tmp_path / "out.wav").read_bytes() == mono
    assert "degraded" in capsys.readouterr().err
    assert read_wav(out).num_channels == 1


def test_retell_multichannel_with_wrong_channel_count_rejected(tmp_path, monkeypatch):
    one_channel = _silent_wav_bytes(tmp_path, 1, "one.wav")
    call = {"recording_multi_channel_url": "https://media.test/multi.wav"}
    _install_urlopen(monkeypatch, _retell_routes(tmp_path, call, **{"multi.wav": one_channel}))
    with pytest.raises(ValueError, match="expected 2"):
        cap.capture_retell(call_id="c1", api_key="k", out_path=str(tmp_path / "out.wav"))


def test_retell_no_recording_at_all_is_clean_error(tmp_path, monkeypatch):
    _install_urlopen(monkeypatch, _retell_routes(tmp_path, {"call_status": "ended"}))
    with pytest.raises(ValueError, match="no recording"):
        cap.capture_retell(call_id="c1", api_key="k")


def test_retell_env_allow_mono_cli_scores_degraded(tmp_path, monkeypatch, capsys):
    """HOTATO_ALLOW_MONO=1 is the installed-CLI escape hatch: the mono pull runs
    end to end, scored without party attribution, loudly marked degraded."""
    mono = _bundled_mono_bytes(tmp_path)
    call = {"recording_url": "https://media.test/mono.wav"}
    _install_urlopen(monkeypatch, _retell_routes(tmp_path, call, **{"mono.wav": mono}))
    monkeypatch.setenv("HOTATO_ALLOW_MONO", "1")
    rc = cli.main(["capture", "--stack", "retell", "--call-id", "c1", "--api-key", "k"])
    assert rc in (0, 1)
    captured = capsys.readouterr()
    assert "degraded" in captured.err
    assert "did_yield=" in captured.out


def test_retell_cli_full_pull_and_score(tmp_path, monkeypatch, capsys):
    """`hotato capture --stack retell --call-id ...` end to end on the current
    response shape, using a bundled reference as the downloaded media."""
    call = {"scrubbed_recording_multi_channel_url": "https://media.test/scrubbed-multi.wav"}
    _install_urlopen(
        monkeypatch,
        _retell_routes(tmp_path, call, **{"scrubbed-multi.wav": _bundled_stereo_bytes()}),
    )
    rc = cli.main(["capture", "--stack", "retell", "--call-id", "c1", "--api-key", "k"])
    assert rc == 0
    assert "did_yield=" in capsys.readouterr().out


# --- Vapi: GET /call/{id} ----------------------------------------------------

def test_vapi_current_shape_artifact_recording_stereo_url(tmp_path, monkeypatch):
    stereo = _silent_wav_bytes(tmp_path, 2, "stereo.wav")
    call = {
        "artifact": {
            "recording": {"stereoUrl": "https://media.test/current.wav"},
            "stereoRecordingUrl": "https://media.test/legacy-artifact.wav",
        },
        "stereoRecordingUrl": "https://media.test/legacy-call.wav",
    }
    seen = []
    _install_urlopen(
        monkeypatch,
        {"/call/": json.dumps(call).encode(), "current.wav": stereo},
        seen,
    )
    out = cap.capture_vapi(call_id="v1", api_key="k", out_path=str(tmp_path / "out.wav"))
    urls = _urls(seen)
    assert any("current.wav" in u for u in urls)
    assert not any("legacy" in u for u in urls)
    assert read_wav(out).num_channels == 2


def test_vapi_legacy_artifact_stereo_recording_url(tmp_path, monkeypatch):
    stereo = _silent_wav_bytes(tmp_path, 2, "stereo.wav")
    call = {"artifact": {"stereoRecordingUrl": "https://media.test/legacy-artifact.wav"}}
    seen = []
    _install_urlopen(
        monkeypatch,
        {"/call/": json.dumps(call).encode(), "legacy-artifact.wav": stereo},
        seen,
    )
    cap.capture_vapi(call_id="v1", api_key="k", out_path=str(tmp_path / "out.wav"))
    assert any("legacy-artifact.wav" in u for u in _urls(seen))


def test_vapi_legacy_toplevel_stereo_recording_url(tmp_path, monkeypatch):
    stereo = _silent_wav_bytes(tmp_path, 2, "stereo.wav")
    call = {"stereoRecordingUrl": "https://media.test/legacy-call.wav"}
    seen = []
    _install_urlopen(
        monkeypatch,
        {"/call/": json.dumps(call).encode(), "legacy-call.wav": stereo},
        seen,
    )
    cap.capture_vapi(call_id="v1", api_key="k", out_path=str(tmp_path / "out.wav"))
    assert any("legacy-call.wav" in u for u in _urls(seen))


def test_vapi_defensive_recording_stereo_recording_url(tmp_path, monkeypatch):
    """Defensive variant: stereoRecordingUrl nested under artifact.recording."""
    stereo = _silent_wav_bytes(tmp_path, 2, "stereo.wav")
    call = {
        "artifact": {
            "recording": {"stereoRecordingUrl": "https://media.test/rec-level.wav"}
        }
    }
    seen = []
    _install_urlopen(
        monkeypatch,
        {"/call/": json.dumps(call).encode(), "rec-level.wav": stereo},
        seen,
    )
    cap.capture_vapi(call_id="v1", api_key="k", out_path=str(tmp_path / "out.wav"))
    assert any("rec-level.wav" in u for u in _urls(seen))


def test_vapi_defensive_recording_stereo_dict_url(tmp_path, monkeypatch):
    """Defensive variant: a {"url": ...} object under artifact.recording.stereo."""
    stereo = _silent_wav_bytes(tmp_path, 2, "stereo.wav")
    call = {
        "artifact": {
            "recording": {"stereo": {"url": "https://media.test/stereo-obj.wav"}}
        }
    }
    seen = []
    _install_urlopen(
        monkeypatch,
        {"/call/": json.dumps(call).encode(), "stereo-obj.wav": stereo},
        seen,
    )
    cap.capture_vapi(call_id="v1", api_key="k", out_path=str(tmp_path / "out.wav"))
    assert any("stereo-obj.wav" in u for u in _urls(seen))


def test_vapi_non_dict_stereo_falls_through_to_legacy(tmp_path, monkeypatch):
    """A non-dict recording.stereo value must not crash; the chain falls
    through to the legacy fields."""
    stereo = _silent_wav_bytes(tmp_path, 2, "stereo.wav")
    call = {
        "artifact": {"recording": {"stereo": True}},
        "stereoRecordingUrl": "https://media.test/legacy-call.wav",
    }
    seen = []
    _install_urlopen(
        monkeypatch,
        {"/call/": json.dumps(call).encode(), "legacy-call.wav": stereo},
        seen,
    )
    cap.capture_vapi(call_id="v1", api_key="k", out_path=str(tmp_path / "out.wav"))
    assert any("legacy-call.wav" in u for u in _urls(seen))


def test_vapi_no_stereo_anywhere_is_clean_error(tmp_path, monkeypatch):
    call = {
        "artifact": {
            "recording": {"mono": {"combinedUrl": "https://media.test/mono.wav"}}
        }
    }
    _install_urlopen(monkeypatch, {"/call/": json.dumps(call).encode()})
    with pytest.raises(ValueError, match="artifact.recording.stereoUrl"):
        cap.capture_vapi(call_id="v1", api_key="k")


# --- Twilio: Recordings/{sid}.wav?RequestedChannels=2 ------------------------

def test_twilio_requests_dual_channel_media(tmp_path, monkeypatch):
    stereo = _silent_wav_bytes(tmp_path, 2, "stereo.wav")
    seen = []
    _install_urlopen(monkeypatch, {"RequestedChannels=2": stereo}, seen)
    out = cap.capture_twilio(
        recording_sid="RE1", account_sid="AC1", auth_token="t",
        out_path=str(tmp_path / "out.wav"),
    )
    urls = _urls(seen)
    assert urls and ".wav?RequestedChannels=2" in urls[0]
    assert seen[0].get_header("Authorization", "").startswith("Basic ")
    assert read_wav(out).num_channels == 2


def test_twilio_400_without_allow_mono_is_clean_error(tmp_path, monkeypatch):
    url = "https://api.twilio.com/2010-04-01/Accounts/AC1/Recordings/RE1.wav?RequestedChannels=2"
    _install_urlopen(monkeypatch, {"RequestedChannels=2": _http_400(url)})
    with pytest.raises(ValueError) as exc:
        cap.capture_twilio(
            recording_sid="RE1", account_sid="AC1", auth_token="t",
            out_path=str(tmp_path / "out.wav"),
        )
    msg = str(exc.value)
    assert "mono" in msg and "RecordingChannels=dual" in msg and "allow-mono" in msg


def test_twilio_400_with_allow_mono_falls_back_to_mono(tmp_path, monkeypatch, capsys):
    mono = _silent_wav_bytes(tmp_path, 1, "m.wav")
    url = "https://api.twilio.com/2010-04-01/Accounts/AC1/Recordings/RE1.wav?RequestedChannels=2"
    seen = []
    _install_urlopen(
        monkeypatch,
        {"RequestedChannels=2": _http_400(url), "RequestedChannels=1": mono},
        seen,
    )
    out = cap.capture_twilio(
        recording_sid="RE1", account_sid="AC1", auth_token="t",
        out_path=str(tmp_path / "out.wav"), allow_mono=True,
    )
    assert any("RequestedChannels=1" in u for u in _urls(seen))
    assert (tmp_path / "out.wav").read_bytes() == mono
    assert "degraded" in capsys.readouterr().err
    assert read_wav(out).num_channels == 1


def test_twilio_mono_validation_rejects_1_channel_download(tmp_path, monkeypatch):
    one_channel = _silent_wav_bytes(tmp_path, 1, "one.wav")
    _install_urlopen(monkeypatch, {"RequestedChannels=2": one_channel})
    with pytest.raises(ValueError, match="expected 2"):
        cap.capture_twilio(
            recording_sid="RE1", account_sid="AC1", auth_token="t",
            out_path=str(tmp_path / "out.wav"),
        )


# --- defect (round 3): wrong-typed nested/url fields -> clean ValueError -----
#
# A vendor response can carry a documented field of the WRONG type (a nested
# object where a dict is expected, or a URL field that is a list/dict/number).
# Every capture_* adapter must raise a clean ValueError naming the field, never a
# raw AttributeError from a `.get()` chain or `urlparse` on a non-string.

def test_vapi_recording_not_a_dict_is_clean_error(tmp_path, monkeypatch):
    call = {"artifact": {"recording": "not-a-dict"}}
    seen = []
    _install_urlopen(monkeypatch, {"/call/": json.dumps(call).encode()}, seen)
    with pytest.raises(ValueError, match="artifact.recording"):
        cap.capture_vapi(call_id="v1", api_key="k")
    # nothing beyond the call JSON was fetched (no download attempted)
    assert all("/call/" in u for u in _urls(seen))


def test_vapi_stereo_url_is_a_list_is_clean_error(tmp_path, monkeypatch):
    call = {"artifact": {"recording": {"stereoUrl": ["https://media.test/a.wav"]}}}
    _install_urlopen(monkeypatch, {"/call/": json.dumps(call).encode()})
    with pytest.raises(ValueError, match="URL string"):
        cap.capture_vapi(call_id="v1", api_key="k")


def test_retell_multichannel_url_is_a_list_is_clean_error(tmp_path, monkeypatch):
    call = {"recording_multi_channel_url": ["https://x", "https://y"]}
    seen = []
    _install_urlopen(monkeypatch, _retell_routes(tmp_path, call), seen)
    with pytest.raises(ValueError, match="URL string"):
        cap.capture_retell(call_id="c1", api_key="k")
    assert all("/v2/get-call/" in u for u in _urls(seen))


def test_millis_nested_recording_url_is_a_dict_is_clean_error(tmp_path, monkeypatch):
    call = {"recording": {"recording_url": {"nested": "oops"}}}
    _install_urlopen(monkeypatch, {"/call-logs/": json.dumps(call).encode()})
    with pytest.raises(ValueError, match="URL string"):
        cap.capture_millis(session_id="s1", api_key="k")


def test_bland_recording_url_is_a_dict_is_clean_error(tmp_path, monkeypatch):
    call = {"recording_url": {"n": 1}}
    _install_urlopen(monkeypatch, {"/v1/calls/": json.dumps(call).encode()})
    with pytest.raises(ValueError, match="URL string"):
        cap.capture_bland(call_id="c1", api_key="k")


def test_synthflow_recording_url_is_a_number_is_clean_error(tmp_path, monkeypatch):
    call = {"response": {"calls": [{"recording_url": 123}]}}
    _install_urlopen(monkeypatch, {"/v2/calls/": json.dumps(call).encode()})
    with pytest.raises(ValueError, match="URL string"):
        cap.capture_synthflow(call_id="c1", api_key="k")


def test_capture_wrong_typed_fields_cli_exit_2(tmp_path, monkeypatch):
    """The flagship single-call `hotato capture` surfaces the clean error as
    exit 2, never a traceback."""
    call = {"artifact": {"recording": "not-a-dict"}}
    _install_urlopen(monkeypatch, {"/call/": json.dumps(call).encode()})
    assert cli.main(["capture", "--stack", "vapi", "--call-id", "v1",
                     "--api-key", "k"]) == 2
