"""Round-3 security regressions for hotato.capture (fully local, no real network).

Defect (round 3): every authenticated call in capture.py followed HTTP redirects
with urllib's default handler, which RE-SENDS the Authorization header (Bearer API
key / Twilio Basic AccountSid:AuthToken) to the redirect target even when it is a
DIFFERENT host -- a full-credential exfiltration primitive reachable via a
compromised/tampered vendor endpoint, a malicious CDN/proxy, a DNS-poisoned path,
or a bad --base-url.

These tests stand up two real localhost HTTP servers (one issuing a cross-host
302, one acting as attacker infra) and drive capture._download through the REAL
urllib path (not the mocked one), asserting the credential never reaches the
off-host target while a same-host redirect still carries it (downloads keep
working).
"""

from __future__ import annotations

import http.server
import socketserver
import tempfile
import threading

import pytest

from hotato import capture as cap


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
    handler.send_response(200)
    handler.send_header("Content-Length", "4")
    handler.end_headers()
    handler.wfile.write(b"data")


def test_credential_not_leaked_on_cross_host_redirect():
    """A 302 to a DIFFERENT host must NOT carry the Authorization header."""
    attacker = _Srv(_ok_capturing)

    def redirect(handler, captured):
        handler.send_response(302)
        # cross-host: 'localhost' != '127.0.0.1' even on the same machine
        handler.send_header("Location", f"http://localhost:{attacker.port}/steal")
        handler.end_headers()

    origin = _Srv(redirect)
    dest = tempfile.mktemp(suffix=".wav")
    try:
        cap._download(
            f"http://127.0.0.1:{origin.port}/rec.wav",
            dest,
            headers={"Authorization": "Basic SECRET_TWILIO_TOKEN_ABC123"},
        )
    except Exception:
        # a failed download is acceptable; a LEAKED credential is not
        pass
    finally:
        origin.stop()
        attacker.stop()
    assert attacker.captured.get("auth") is None, (
        "Authorization header was exfiltrated to a cross-host redirect target"
    )


def test_credential_kept_on_same_host_redirect():
    """A same-host redirect (e.g. a CDN path change on the vendor's own host) must
    still carry the credential so legitimate authenticated downloads work."""
    seen = {}

    def handler(h, captured):
        if h.path.startswith("/rec"):
            h.send_response(302)
            h.send_header("Location", "/final")  # same host, relative
            h.end_headers()
        else:
            seen["auth"] = h.headers.get("Authorization")
            h.send_response(200)
            h.send_header("Content-Length", "4")
            h.end_headers()
            h.wfile.write(b"data")

    srv = _Srv(handler)
    dest = tempfile.mktemp(suffix=".wav")
    try:
        cap._download(
            f"http://127.0.0.1:{srv.port}/rec.wav",
            dest,
            headers={"Authorization": "Basic KEEP_ME"},
        )
    finally:
        srv.stop()
    assert seen.get("auth") == "Basic KEEP_ME"
