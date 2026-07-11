"""Round-5 HTTP-primitive hardening regressions for apply.py and capture.py.

Defect #13: both ``apply._http_json`` and ``capture._http_get`` wrapped only
``resp.read()`` in a try/except that caught ``urllib.error.HTTPError`` and
``urllib.error.URLError``. A server that sends a Content-Length header and then
closes the connection mid-body (request already drained, clean FIN) makes
CPython's ``http.client`` raise ``http.client.IncompleteRead`` from
``resp.read()`` -- a type that is NOT a subclass of HTTPError/URLError, so it
escaped both functions' except clauses, propagated past the CLI's
``HANDLED = (ValueError, OSError, BackendUnavailable)`` boundary (IncompleteRead
is neither), and printed a raw Python traceback instead of a clean, actionable
error. These tests reproduce that exact mid-body disconnect (mocked, and for
the CLI-level test via a real localhost socket) and assert both primitives now
convert it into a scoped ``ValueError`` that never leaks a credential.

Defect #15: ``apply._http_json`` built its own ``urllib.request`` call and never
installed ``capture``'s process-wide credential-safe / SSRF-safe redirect
opener (``_ensure_safe_opener`` / ``_CredentialSafeRedirectHandler``), so an
authenticated apply GET/POST that hit a cross-host 3xx redirect (compromised
CDN edge, DNS hijack of the vendor domain, vendor-side redirect to a storage
backend) would carry the ``Authorization: Bearer <api key>`` header to the new
host via stock urllib -- unlike every credentialed call in capture.py, which
already strips it. This test drives ``apply._http_json`` through the REAL
urllib path (a live localhost redirect) and asserts the credential is not
leaked to the off-host target.
"""

from __future__ import annotations

import http.client
import http.server
import socketserver
import threading

import pytest

from hotato import apply as _apply
from hotato import capture as cap


# --- #13: IncompleteRead (and sibling read-time disconnects) escape ---------

class _RespRaisesOnRead:
    """A fake ``urlopen`` response whose headers were fine but ``.read()`` blows
    up mid-body -- exactly what CPython's ``http.client`` does when a server
    sends Content-Length then closes with the body truncated."""

    def __init__(self, exc: Exception):
        self._exc = exc

    def __enter__(self):
        return self

    def __exit__(self, *exc_info):
        return False

    def read(self):
        raise self._exc


def test_apply_http_json_incomplete_read_becomes_clean_value_error(monkeypatch):
    def fake_urlopen(req, timeout=None):
        return _RespRaisesOnRead(http.client.IncompleteRead(b"", 40))

    monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)
    with pytest.raises(ValueError) as exc:
        _apply._http_json(
            "GET", "https://api.vapi.ai/assistant/asst_9",
            headers={"Authorization": "Bearer sk_super_secret"}, body=None,
            timeout=5,
        )
    msg = str(exc.value)
    # a clean, scoped ValueError -- not a raw IncompleteRead/traceback escaping
    assert "GET https://api.vapi.ai/assistant/asst_9" in msg
    assert "sk_super_secret" not in msg


def test_apply_http_json_connection_error_becomes_clean_value_error(monkeypatch):
    def fake_urlopen(req, timeout=None):
        return _RespRaisesOnRead(ConnectionResetError("connection reset by peer"))

    monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)
    with pytest.raises(ValueError) as exc:
        _apply._http_json(
            "POST", "https://api.vapi.ai/assistant",
            headers={"Authorization": "Bearer sk_super_secret"},
            body={"name": "x"}, timeout=5,
        )
    assert "POST https://api.vapi.ai/assistant" in str(exc.value)
    assert "sk_super_secret" not in str(exc.value)


def test_capture_http_get_incomplete_read_becomes_clean_value_error(monkeypatch):
    def fake_urlopen(req, timeout=None):
        return _RespRaisesOnRead(http.client.IncompleteRead(b"", 40))

    monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)
    with pytest.raises(ValueError) as exc:
        cap._http_get(
            "https://api.retellai.com/v2/get-call/call_1",
            headers={"Authorization": "Bearer rk_super_secret"},
        )
    msg = str(exc.value)
    assert "https://api.retellai.com/v2/get-call/call_1" in msg
    assert "rk_super_secret" not in msg


def test_capture_http_get_connection_error_becomes_clean_value_error(monkeypatch):
    def fake_urlopen(req, timeout=None):
        return _RespRaisesOnRead(BrokenPipeError("broken pipe"))

    monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)
    with pytest.raises(ValueError) as exc:
        cap._http_get(
            "https://api.vapi.ai/call/call_1",
            headers={"Authorization": "Bearer sk_super_secret"},
        )
    assert "https://api.vapi.ai/call/call_1" in str(exc.value)
    assert "sk_super_secret" not in str(exc.value)


def test_incomplete_read_over_a_real_socket_matches_the_repro(monkeypatch):
    """End-to-end confirmation over a REAL socket (no urlopen mocking): a server
    that sends Content-Length then closes mid-body must surface as a clean
    ValueError from ``_http_json``, not a raw IncompleteRead escaping to the
    caller (the exact scenario the finding's live-socket repro demonstrated)."""
    monkeypatch.setenv("HOTATO_ALLOW_PRIVATE_URLS", "1")

    class H(http.server.BaseHTTPRequestHandler):
        def do_GET(self):  # noqa: N802
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", "100")
            self.end_headers()
            self.wfile.write(b'{"a":')
            self.wfile.flush()
            # Close the connection with the body truncated (clean FIN, no more
            # data): this is what makes http.client raise IncompleteRead.
            try:
                self.connection.shutdown(1)  # SHUT_WR
            except OSError:
                pass

        def log_message(self, *a):  # silence
            return

    srv = socketserver.TCPServer(("127.0.0.1", 0), H)
    port = srv.server_address[1]
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    try:
        with pytest.raises(ValueError) as exc:
            _apply._http_json(
                "GET", f"http://127.0.0.1:{port}/assistant/asst_9",
                headers={"Authorization": "Bearer sk_super_secret"}, body=None,
                timeout=5,
            )
        assert "sk_super_secret" not in str(exc.value)
    finally:
        srv.shutdown()
        srv.server_close()


# --- #15: apply._http_json now shares capture's credential-safe redirect opener

@pytest.fixture(autouse=True)
def _allow_loopback_test_servers(monkeypatch):
    """These tests stand up REAL HTTP servers on 127.0.0.1 to exercise the
    installed opener's redirect handling over the real urllib path. Opt into
    private URLs the same way test_fix_round3_security.py does, so the
    default-deny SSRF guard on the redirect target does not mask the
    credential-stripping assertion this test is actually making.

    Also force a clean urllib opener state: ``capture._ensure_safe_opener``
    installs its safe opener process-wide, once, guarded by the module-level
    ``_SAFE_OPENER_INSTALLED`` flag -- so if any earlier test in this SAME
    process already triggered it (e.g. a capture._http_get call elsewhere in
    this file), that installed opener would keep protecting apply._http_json
    even against the PRE-fix code, masking the very regression this test
    exists to catch. Reset both the flag and urllib's installed default
    opener so each test observes exactly what a fresh process would."""
    monkeypatch.setenv("HOTATO_ALLOW_PRIVATE_URLS", "1")
    monkeypatch.setattr(cap, "_SAFE_OPENER_INSTALLED", False)
    monkeypatch.setattr("urllib.request._opener", None)


class _Srv:
    """A throwaway localhost HTTP server driven by a per-request handler fn."""

    def __init__(self, handler_fn):
        captured = {}
        self.captured = captured

        class H(http.server.BaseHTTPRequestHandler):
            def do_GET(self):  # noqa: N802
                handler_fn(self, captured)

            def log_message(self, *a):  # silence
                return

        self._srv = socketserver.TCPServer(("127.0.0.1", 0), H)
        self.port = self._srv.server_address[1]
        self._t = threading.Thread(target=self._srv.serve_forever, daemon=True)
        self._t.start()

    def stop(self):
        self._srv.shutdown()
        self._srv.server_close()


def _ok_capturing(handler, captured):
    captured["auth"] = handler.headers.get("Authorization")
    body = b'{"id": "asst_stolen"}'
    handler.send_response(200)
    handler.send_header("Content-Type", "application/json")
    handler.send_header("Content-Length", str(len(body)))
    handler.end_headers()
    handler.wfile.write(body)


def test_apply_http_json_credential_not_leaked_on_cross_host_redirect():
    """A 302 to a DIFFERENT host must NOT carry the Authorization: Bearer key,
    now that apply._http_json shares capture's credential-safe redirect opener."""
    attacker = _Srv(_ok_capturing)

    def redirect(handler, captured):
        handler.send_response(302)
        # cross-host: 'localhost' != '127.0.0.1' even on the same machine
        handler.send_header("Location", f"http://localhost:{attacker.port}/assistant")
        handler.end_headers()

    origin = _Srv(redirect)
    try:
        try:
            _apply._http_json(
                "GET", f"http://127.0.0.1:{origin.port}/assistant/asst_9",
                headers={"Authorization": "Bearer sk_super_secret"}, body=None,
                timeout=5,
            )
        except Exception:
            # a failed fetch is acceptable; a LEAKED credential is not
            pass
    finally:
        origin.stop()
        attacker.stop()
    assert attacker.captured.get("auth") is None, (
        "Authorization header was exfiltrated to a cross-host redirect target"
    )


def test_apply_http_json_credential_kept_on_same_host_redirect():
    """A same-host redirect must still carry the credential so a legitimate
    authenticated apply GET/POST keeps working."""
    seen = {}

    def handler(h, captured):
        if h.path.startswith("/assistant/asst_9"):
            h.send_response(302)
            h.send_header("Location", "/assistant/final")  # same host, relative
            h.end_headers()
        else:
            seen["auth"] = h.headers.get("Authorization")
            body = b'{"id": "asst_9", "name": "prod"}'
            h.send_response(200)
            h.send_header("Content-Type", "application/json")
            h.send_header("Content-Length", str(len(body)))
            h.end_headers()
            h.wfile.write(body)

    srv = _Srv(handler)
    try:
        result = _apply._http_json(
            "GET", f"http://127.0.0.1:{srv.port}/assistant/asst_9",
            headers={"Authorization": "Bearer keep_me"}, body=None, timeout=5,
        )
    finally:
        srv.stop()
    assert seen.get("auth") == "Bearer keep_me"
    assert result == {"id": "asst_9", "name": "prod"}
