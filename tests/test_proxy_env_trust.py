"""Regression for the ambient-proxy-trust finding (#18).

``urllib.request.build_opener()`` always installs a default ``ProxyHandler``
unless the caller explicitly passes one, and that default ``ProxyHandler``
reads ``HTTP_PROXY``/``HTTPS_PROXY`` from ``os.environ`` at construction time.
``capture._ensure_safe_opener()`` (which ``apply._http_json`` also routes
through, via ``from . import capture as _capture;
_capture._ensure_safe_opener()``) only passed a custom redirect handler to
``build_opener()``, so ambient proxy env vars were silently honored for every
credentialed Vapi/Retell/Twilio/etc call, with no documented decision and no
opt-out.

These are behavioral tests (they drive a real local HTTP server through the
real opener) rather than introspecting ``OpenerDirector`` internals, because
``ProxyHandler({})`` deliberately registers no ``<scheme>_open`` methods and
so never appears in ``opener.handlers`` -- asserting on that list would not
actually prove requests bypass the proxy.

Pinned contract:

  1. by default, proxy env vars ARE still honored (unchanged behavior, and a
     deliberate, now-documented choice -- matches curl/pip/git): a request
     routed at a real local target through a bogus ``HTTP_PROXY`` fails,
     because it is sent to the (unreachable) proxy instead of the target.
  2. ``HOTATO_NO_PROXY=1`` makes the same request bypass that bogus proxy and
     reach the real target directly.
  3. ``apply._http_json`` shares the same opener/opt-out (it calls
     ``capture._ensure_safe_opener()`` before issuing its request).
"""

import json
import http.server
import threading

import pytest

from hotato import capture as cap


class _OKHandler(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        body = json.dumps({"ok": True}).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *a):  # silence test output
        pass


@pytest.fixture
def local_server():
    srv = http.server.HTTPServer(("127.0.0.1", 0), _OKHandler)
    port = srv.server_address[1]
    thread = threading.Thread(target=srv.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{port}/"
    finally:
        srv.shutdown()
        thread.join(timeout=5)


@pytest.fixture
def fresh_opener(monkeypatch):
    """Reset the process-wide urllib opener singleton so each test observes
    an opener built fresh from the env vars it sets, instead of a stale
    opener installed (or not) by a prior test or import."""
    import urllib.request

    monkeypatch.setattr(cap, "_SAFE_OPENER_INSTALLED", False)
    monkeypatch.setattr(urllib.request, "_opener", None)
    monkeypatch.delenv("HOTATO_NO_PROXY", raising=False)
    monkeypatch.delenv("HTTP_PROXY", raising=False)
    monkeypatch.delenv("http_proxy", raising=False)


def test_ambient_http_proxy_is_honored_by_default(local_server, fresh_opener, monkeypatch):
    """A bogus HTTP_PROXY routes the request to the (unreachable) proxy
    instead of the real target, so the call fails -- proving the proxy env
    var is honored by default, as documented."""
    monkeypatch.setenv("HTTP_PROXY", "http://127.0.0.1:1")

    with pytest.raises(ValueError):
        cap._http_get(local_server, timeout=2)


def test_hotato_no_proxy_bypasses_ambient_proxy(local_server, fresh_opener, monkeypatch):
    """The same bogus HTTP_PROXY is ignored once HOTATO_NO_PROXY=1 is set, and
    the request reaches the real local target directly."""
    monkeypatch.setenv("HTTP_PROXY", "http://127.0.0.1:1")
    monkeypatch.setenv("HOTATO_NO_PROXY", "1")

    data = cap._http_get(local_server, timeout=2)
    assert json.loads(data) == {"ok": True}


def test_apply_http_json_shares_captures_no_proxy_opt_out(
    local_server, fresh_opener, monkeypatch
):
    """apply._http_json calls capture._ensure_safe_opener() before issuing
    its request, so HOTATO_NO_PROXY=1 also covers apply's credentialed
    read/create calls, not just capture's."""
    from hotato import apply as ap

    monkeypatch.setenv("HTTP_PROXY", "http://127.0.0.1:1")
    monkeypatch.setenv("HOTATO_NO_PROXY", "1")

    # local_server only serves GET with a bare {"ok": true} body, which is a
    # valid (if minimal) JSON object -- enough for _http_json's shape check.
    result = ap._http_json("GET", local_server, headers={}, body=None, timeout=2)
    assert result == {"ok": True}
