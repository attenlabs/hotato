"""``hotato.notify``: the optional ``--notify`` webhook on ``sweep`` and
``hotato fleet run``.

Covers: the payload shape (no forbidden fields -- no audio, no credentials, no
transcript text), fail-open delivery (a monkeypatched ``urlopen`` failure never
raises and never surfaces as a non-zero exit), the non-http(s) scheme refusal
(fail-closed, before any network attempt), and that ``--notify`` is actually
wired into the ``sweep`` and ``fleet run`` CLI paths.
"""

from __future__ import annotations

import io
import json
import socket
import urllib.error
import urllib.request

import pytest

from hotato import cli
from hotato import notify as _notify


class _FakeResp(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *a):
        self.close()


# =========================================================================
# 1. post_notification: User-Agent, JSON body, success
# =========================================================================

def test_post_notification_sends_hotato_user_agent_and_json_body(monkeypatch):
    seen = {}

    def fake_urlopen(req, timeout=10):
        seen["req"] = req
        seen["body"] = json.loads(req.data.decode("utf-8"))
        return _FakeResp(b"ok")

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
    ok = _notify.post_notification("https://hooks.example.com/x", {"a": 1})
    assert ok is True
    ua = seen["req"].get_header("User-agent")
    assert ua and ua.startswith("hotato/")
    assert seen["req"].get_header("Content-type") == "application/json"
    assert seen["req"].get_method() == "POST"
    assert seen["body"] == {"a": 1}


# =========================================================================
# 2. fail-open: any network/HTTP problem is one warning, never a raise
# =========================================================================

def test_post_notification_fails_open_on_connection_error(monkeypatch, capsys):
    def fake_urlopen(req, timeout=10):
        raise urllib.error.URLError("connection refused")

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
    ok = _notify.post_notification("https://hooks.example.com/x", {"a": 1})
    assert ok is False
    err = capsys.readouterr().err
    assert "[notify]" in err and "hooks.example.com" in err


def test_post_notification_fails_open_on_http_error(monkeypatch, capsys):
    def fake_urlopen(req, timeout=10):
        raise urllib.error.HTTPError("https://hooks.example.com/x", 500,
                                     "boom", None, None)

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
    ok = _notify.post_notification("https://hooks.example.com/x", {"a": 1})
    assert ok is False
    assert "delivery failed" in capsys.readouterr().err


def test_post_notification_fails_open_on_timeout(monkeypatch):
    def fake_urlopen(req, timeout=10):
        raise socket.timeout("timed out")

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
    ok = _notify.post_notification("https://hooks.example.com/x", {"a": 1})
    assert ok is False


def test_notify_all_tries_every_url_even_if_one_fails(monkeypatch):
    calls = []

    def fake_urlopen(req, timeout=10):
        calls.append(req.full_url)
        if "bad" in req.full_url:
            raise urllib.error.URLError("nope")
        return _FakeResp(b"ok")

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
    _notify.notify_all(["https://a.example.com/bad", "https://b.example.com/ok"],
                       {"a": 1})
    assert len(calls) == 2


# =========================================================================
# 3. URL scheme refusal: fail-CLOSED, raised before any network attempt
# =========================================================================

@pytest.mark.parametrize("bad_url", [
    "file:///etc/passwd",
    "ftp://example.com/x",
    "data:text/plain,hi",
    "javascript:alert(1)",
    "not-a-url",
    "",
])
def test_validate_notify_url_refuses_non_http_schemes(bad_url):
    with pytest.raises(ValueError):
        _notify.validate_notify_url(bad_url)


def test_validate_notify_url_refuses_hostless_url():
    with pytest.raises(ValueError):
        _notify.validate_notify_url("https://")


def test_post_notification_refuses_bad_scheme_without_touching_network(monkeypatch):
    def fake_urlopen(req, timeout=10):
        raise AssertionError("must not reach the network for a refused scheme")

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
    with pytest.raises(ValueError):
        _notify.post_notification("file:///etc/passwd", {"a": 1})


def test_validate_notify_urls_accepts_good_list_and_empty():
    assert _notify.validate_notify_urls(None) == []
    assert _notify.validate_notify_urls([]) == []
    urls = ["https://a.example.com/hook", "http://b.example.com/hook"]
    assert _notify.validate_notify_urls(urls) == urls


def test_validate_notify_urls_raises_on_first_bad_url_in_a_list():
    with pytest.raises(ValueError):
        _notify.validate_notify_urls(["https://good.example.com", "file:///etc/passwd"])


# =========================================================================
# 4. payload shape: only the whitelisted fields, ever
# =========================================================================

_FORBIDDEN_SUBSTRINGS = (
    "api_key", "apikey", "credential", "password", "secret", "auth_token",
    "transcript", "recording_url", "base64", "audio_data",
)


def _assert_no_forbidden_content(payload: dict) -> None:
    blob = json.dumps(payload).lower()
    for bad in _FORBIDDEN_SUBSTRINGS:
        assert bad not in blob, f"forbidden field leaked into payload: {bad!r}"


def test_sweep_payload_shape_and_no_forbidden_fields():
    aggregate = {
        "calls_scanned": 3,
        "calls_skipped": 1,
        "total_candidates": 2,
        "candidates": [
            {"source": "call1.wav", "t_sec": 1.2, "kind": "long_response_gap",
             "durations": {"gap_sec": 2.5},
             "agent_reaction": {"next_agent_onset_sec": 3.7},
             "salience": 2.5, "window": {"start_sec": 0.0, "end_sec": 5.0}},
            {"source": "call2.wav", "t_sec": 0.5, "kind": "overlap_while_agent_talking",
             "durations": {"overlap_sec": 0.3}, "agent_reaction": None,
             "salience": 0.3, "window": {"start_sec": 0.0, "end_sec": 2.0}},
        ],
    }
    payload = _notify.sweep_payload(stack="vapi", aggregate=aggregate,
                                    out_file="hotato-sweep-vapi.html",
                                    pull_dir="hotato-sweep-vapi")
    assert payload["tool"] == "hotato"
    assert payload["kind"] == "sweep"
    assert payload["stack"] == "vapi"
    assert payload["counts"] == {"calls_scanned": 3, "calls_skipped": 1,
                                 "candidates_found": 2}
    assert payload["artifacts"] == {"dashboard": "hotato-sweep-vapi.html",
                                    "pull_dir": "hotato-sweep-vapi"}
    assert isinstance(payload["text"], str) and "vapi" in payload["text"]
    assert len(payload["top_candidates"]) == 2
    for c in payload["top_candidates"]:
        # ONLY id/kind/timing numbers -- never the raw candidate dict (no
        # "window", no "agent_reaction", no "salience", no "source" leak
        # beyond what is baked into the id).
        assert set(c.keys()) <= {"id", "kind", "t_sec", "onset_sec",
                                 "severity", "durations"}
    assert payload["top_candidates"][0]["id"] == "call1.wav#0"
    _assert_no_forbidden_content(payload)


def test_sweep_payload_caps_top_candidates():
    aggregate = {
        "calls_scanned": 1, "calls_skipped": 0, "total_candidates": 10,
        "candidates": [
            {"source": "c.wav", "t_sec": float(i), "kind": "long_response_gap",
             "durations": {"gap_sec": float(i)}}
            for i in range(10)
        ],
    }
    payload = _notify.sweep_payload(stack="demo", aggregate=aggregate, top=3)
    assert len(payload["top_candidates"]) == 3


def test_fleet_run_payload_shape_and_no_forbidden_fields():
    res = {
        "workspace_id": "ws1", "agent_id": "support-bot",
        "ingested": [{"recording_id": "r1", "scorable": True, "candidates": 2}],
        "clusters": 4,
        "reviewed_candidates": 2,
        "top_candidates": [
            {"candidate_id": "cand-abc-0", "onset_sec": 1.5, "severity": 0.8,
             "cluster": "overlap_while_agent_talking", "status": "new",
             "components": {"severity": 0.8, "input_health": "clean"},
             "suggestion": {"label": "maybe_yield"}},
        ],
    }
    payload = _notify.fleet_run_payload(workspace_id="ws1", agent_id="support-bot",
                                        res=res, home="/home/u/.hotato/fleet")
    assert payload["tool"] == "hotato"
    assert payload["kind"] == "fleet_run"
    assert payload["workspace_id"] == "ws1"
    assert payload["agent_id"] == "support-bot"
    assert payload["counts"] == {"recordings_ingested": 1, "clusters": 4,
                                 "candidates_found": 2}
    assert payload["artifacts"] == {"home": "/home/u/.hotato/fleet"}
    cand = payload["top_candidates"][0]
    assert set(cand.keys()) <= {"id", "kind", "t_sec", "onset_sec",
                                "severity", "durations"}
    assert cand["id"] == "cand-abc-0"
    assert cand["kind"] == "overlap_while_agent_talking"
    _assert_no_forbidden_content(payload)


# =========================================================================
# 5. CLI wiring smoke: --notify on sweep and fleet run actually fires
# =========================================================================

def test_sweep_notify_flag_is_wired(tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("HOTATO_HOME", raising=False)
    monkeypatch.setenv("HOTATO_HOME", str(tmp_path / "home"))

    calls = []

    def fake_post(url, payload, timeout=10):
        calls.append((url, payload))
        return True

    monkeypatch.setattr(_notify, "post_notification", fake_post)
    rc = cli.main(["sweep", "--demo", "--no-open",
                   "--notify", "https://hooks.example.com/a",
                   "--notify", "https://hooks.example.com/b"])
    assert rc == 0
    assert len(calls) == 2
    assert calls[0][0] == "https://hooks.example.com/a"
    assert calls[1][0] == "https://hooks.example.com/b"
    assert calls[0][1]["kind"] == "sweep"


def test_sweep_without_notify_never_calls_post_notification(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("HOTATO_HOME", str(tmp_path / "home"))

    def fail(*a, **k):
        raise AssertionError("post_notification must not be called without --notify")

    monkeypatch.setattr(_notify, "post_notification", fail)
    rc = cli.main(["sweep", "--demo", "--no-open"])
    assert rc == 0


def test_sweep_notify_rejects_bad_scheme_before_the_pull(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("HOTATO_HOME", str(tmp_path / "home"))

    def fail(*a, **k):
        raise AssertionError("must not reach the network for a refused --notify scheme")

    monkeypatch.setattr(urllib.request, "urlopen", fail)
    rc = cli.main(["sweep", "--demo", "--no-open", "--notify", "file:///etc/passwd"])
    assert rc == 2


def test_fleet_run_notify_flag_is_wired(tmp_path, monkeypatch, capsys):
    from tests._trial_audio import talkover_call

    home = str(tmp_path / "fleet-home")
    wav = str(tmp_path / "call.wav")
    talkover_call(wav)
    assert cli.main(["fleet", "init", "--home", home, "-w", "ws1"]) == 0
    assert cli.main(["fleet", "agent", "add", "--home", home, "-w", "ws1",
                     "--agent-id", "support-bot", "--stack", "vapi",
                     "--assistant-id", "asst_1"]) == 0

    calls = []

    def fake_post(url, payload, timeout=10):
        calls.append((url, payload))
        return True

    monkeypatch.setattr(_notify, "post_notification", fake_post)
    rc = cli.main(["fleet", "run", "--home", home, "-w", "ws1",
                   "--agent", "support-bot", "--recordings", wav,
                   "--notify", "https://hooks.example.com/fleet"])
    assert rc == 0
    assert len(calls) == 1
    assert calls[0][0] == "https://hooks.example.com/fleet"
    assert calls[0][1]["kind"] == "fleet_run"
    assert calls[0][1]["agent_id"] == "support-bot"


def test_fleet_run_notify_rejects_bad_scheme_before_the_run(tmp_path, monkeypatch):
    home = str(tmp_path / "fleet-home")
    assert cli.main(["fleet", "init", "--home", home, "-w", "ws1"]) == 0

    def fail(*a, **k):
        raise AssertionError("must not reach the network for a refused --notify scheme")

    monkeypatch.setattr(urllib.request, "urlopen", fail)
    rc = cli.main(["fleet", "run", "--home", home, "-w", "ws1",
                   "--agent", "support-bot", "--notify", "ftp://example.com/x"])
    assert rc == 2
