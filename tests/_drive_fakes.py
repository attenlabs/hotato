"""Shared REAL-local-HTTPServer fakes for the drive-a-call + pull tests.

These are honest local sockets on 127.0.0.1 (no monkeypatched urlopen): the real
``hotato.drive`` / ``hotato.capture`` client code makes real HTTP requests to a
throwaway ``http.server`` that plays the provider. Every request the client made
is recorded so a test can assert method + path + auth header + body shape, and
the method allowlist (never PUT/PATCH/DELETE) can be checked directly.
"""
import json
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from urllib.parse import parse_qs, urlparse

from hotato._engine.audio import write_wav


def stereo_wav_bytes(tmp_path, channels=2, name="stereo.wav"):
    """A real, scoreable PCM WAV (built by the same stdlib writer the scorer
    reads) with ``channels`` channels."""
    path = tmp_path / name
    write_wav(str(path), 8000, [[0.0] * 800 for _ in range(channels)])
    return path.read_bytes()


class Recorder:
    def __init__(self):
        self.requests = []
        self.status_polls = 0
        self.recording_polls = 0

    @property
    def methods(self):
        return [r["method"] for r in self.requests]

    def by(self, method, needle):
        return [r for r in self.requests
                if r["method"] == method and needle in r["path"]]


def start(handler_cls):
    """Start ``handler_cls`` on an ephemeral loopback port; return (server, base_url)."""
    server = HTTPServer(("127.0.0.1", 0), handler_cls)
    port = server.server_address[1]
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server, f"http://127.0.0.1:{port}"


def _base_handler(recorder):
    class H(BaseHTTPRequestHandler):
        def log_message(self, *a):  # silence
            pass

        def _read_body(self):
            length = int(self.headers.get("Content-Length", 0) or 0)
            raw = self.rfile.read(length) if length else b""
            ctype = (self.headers.get("Content-Type") or "").split(";")[0].strip()
            if not raw:
                return None
            if ctype == "application/json":
                return json.loads(raw)
            if ctype == "application/x-www-form-urlencoded":
                return {k: v[0] if len(v) == 1 else v
                        for k, v in parse_qs(raw.decode()).items()}
            return raw.decode("utf-8", "replace")

        def _record(self, method):
            recorder.requests.append({
                "method": method, "path": self.path,
                "auth": self.headers.get("Authorization"),
                "body": self._read_body(),
            })

        def _json(self, obj, code=200):
            payload = json.dumps(obj).encode()
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

        def _wav(self, data):
            self.send_response(200)
            self.send_header("Content-Type", "audio/wav")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def _404(self):
            self.send_response(404)
            self.send_header("Content-Length", "0")
            self.end_headers()

        # Any verb that would MUTATE a resource is refused loudly, so a test's
        # method-allowlist assertion has teeth: the client must never issue one.
        def do_PUT(self):
            self._record("PUT"); self._404()

        def do_PATCH(self):
            self._record("PATCH"); self._404()

        def do_DELETE(self):
            self._record("DELETE"); self._404()

    return H


# public alias: the pull-smoke tests build their own GET-only handlers on top of
# this (it already carries _record/_json/_wav/_404 and the PUT/PATCH/DELETE guard).
base_handler = _base_handler


def twilio_handler(recorder, stereo_bytes, *, account_sid="AC1",
                   call_sid="CA_call", recording_sid="RE_rec",
                   completes_after=2, recording_after=1):
    """A fake Twilio REST API: create-call POST, a status poll that flips to
    'completed' after ``completes_after`` GETs, a recordings list that yields the
    recording after ``recording_after`` GETs, and the dual-channel media."""
    Base = _base_handler(recorder)
    root = f"/2010-04-01/Accounts/{account_sid}"

    class H(Base):
        def do_POST(self):
            self._record("POST")
            if self.path.endswith("/Calls.json"):
                return self._json({"sid": call_sid, "status": "queued"}, 201)
            return self._404()

        def do_GET(self):
            self._record("GET")
            path = urlparse(self.path).path
            if path == f"{root}/Calls/{call_sid}.json":
                recorder.status_polls += 1
                status = ("completed" if recorder.status_polls >= completes_after
                          else "in-progress")
                return self._json({"sid": call_sid, "status": status})
            if path == f"{root}/Recordings.json":
                recorder.recording_polls += 1
                recs = ([{"sid": recording_sid}]
                        if recorder.recording_polls >= recording_after else [])
                return self._json({"recordings": recs})
            if path == f"{root}/Recordings/{recording_sid}.wav":
                return self._wav(stereo_bytes)
            return self._404()

    return H


def vapi_handler(recorder, stereo_bytes, *, call_id="vc_1", ends_after=2):
    """A fake Vapi API: create-call POST, a status poll that flips to 'ended'
    after ``ends_after`` GETs (carrying artifact.recording.stereoUrl on the local
    server), and the stereo media."""
    Base = _base_handler(recorder)

    class H(Base):
        def _stereo_url(self):
            host = self.headers.get("Host")
            return f"http://{host}/rec/{call_id}.stereo.wav"

        def do_POST(self):
            self._record("POST")
            if urlparse(self.path).path == "/call":
                return self._json({"id": call_id, "status": "queued"}, 201)
            return self._404()

        def do_GET(self):
            self._record("GET")
            path = urlparse(self.path).path
            if path == f"/call/{call_id}":
                recorder.status_polls += 1
                if recorder.status_polls >= ends_after:
                    return self._json({
                        "id": call_id, "status": "ended",
                        "artifact": {"recording": {"stereoUrl": self._stereo_url()}},
                    })
                return self._json({"id": call_id, "status": "ringing"})
            if path == f"/rec/{call_id}.stereo.wav":
                return self._wav(stereo_bytes)
            return self._404()

    return H
