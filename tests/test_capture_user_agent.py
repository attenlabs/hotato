"""Provider APIs sit behind Cloudflare, which 403s (error 1010) urllib's default
User-Agent as a bot signature -- so a valid key looks like an auth failure and a
credential probe never reaches the vendor. _http_get must send an explicit hotato
User-Agent (honest, not a spoofed browser) on every provider call, and preserve a
caller-supplied one."""
from __future__ import annotations

import io
import urllib.request

import hotato.capture as cap


class _FakeResp(io.BytesIO):
    def __enter__(self): return self
    def __exit__(self, *a): self.close()


def _capture_request(monkeypatch):
    seen = {}
    def fake_urlopen(req, timeout=60):
        seen["req"] = req
        return _FakeResp(b"[]")
    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
    return seen


def test_http_get_sends_a_hotato_user_agent(monkeypatch):
    seen = _capture_request(monkeypatch)
    cap._http_get("https://api.vapi.ai/call?limit=1",
                  headers={"Authorization": "Bearer x"})
    ua = seen["req"].get_header("User-agent")
    assert ua and ua.startswith("hotato/"), f"expected a hotato UA, got {ua!r}"
    # never urllib's default bot-signature UA that Cloudflare 1010-blocks
    assert "urllib" not in (ua or "").lower()


def test_caller_supplied_user_agent_is_preserved(monkeypatch):
    seen = _capture_request(monkeypatch)
    cap._http_get("https://api.vapi.ai/call",
                  headers={"Authorization": "Bearer x", "User-Agent": "custom/9"})
    assert seen["req"].get_header("User-agent") == "custom/9"


def test_http_get_json_also_carries_the_ua(monkeypatch):
    seen = _capture_request(monkeypatch)
    cap._http_get_json("https://api.retellai.com/v2/list-calls",
                       headers={"Authorization": "Bearer x"})
    assert seen["req"].get_header("User-agent").startswith("hotato/")
