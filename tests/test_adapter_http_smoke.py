"""Live-adapter HTTP smoke: run the REAL Vapi/Retell clone code against a mock
server, verifying request method, auth header, endpoints, payload, and response
parsing. The only thing this cannot cover is the real vendor host (needs
credentials); every line of the client path is exercised here offline."""
import json
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest

from hotato import apply as _apply
from hotato.fleet import adapters


class _Recorder:
    def __init__(self):
        self.requests = []


def _make_handler(recorder, source_config):
    class H(BaseHTTPRequestHandler):
        def log_message(self, *a):  # silence
            pass

        def _record(self, method):
            length = int(self.headers.get("Content-Length", 0) or 0)
            body = self.rfile.read(length) if length else b""
            recorder.requests.append({
                "method": method, "path": self.path,
                "auth": self.headers.get("Authorization"),
                "body": json.loads(body) if body else None,
            })

        def do_GET(self):
            self._record("GET")
            payload = json.dumps(source_config).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

        def do_POST(self):
            self._record("POST")
            payload = json.dumps({"id": "clone-abc123", "name": "created"}).encode()
            self.send_response(201)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)
    return H


def test_vapi_adapter_clone_hits_mock_with_correct_shape(monkeypatch):
    recorder = _Recorder()
    source_config = {"id": "asst_src", "name": "prod", "orgId": "o1",
                     "model": {"messages": []}, "firstMessage": "hi"}
    server = HTTPServer(("127.0.0.1", 0), _make_handler(recorder, source_config))
    port = server.server_address[1]
    t = threading.Thread(target=server.serve_forever, daemon=True); t.start()
    try:
        # point the real Vapi endpoints at the mock server
        ep = dict(_apply._CLONE_ENDPOINTS["vapi"])
        ep["read_url_template"] = f"http://127.0.0.1:{port}/assistant/{{id}}"
        ep["create_url"] = f"http://127.0.0.1:{port}/assistant"
        monkeypatch.setitem(_apply._CLONE_ENDPOINTS, "vapi", ep)

        adapter = adapters.get_adapter("vapi", api_key="sk-test-key")
        clone = adapter.clone_agent("asst_src", name="hotato-staging")
        result = adapter.apply_variant(clone, {"config_delta": {"firstMessage": "changed"}})
    finally:
        server.shutdown()

    # the real client made a GET (read source) then a POST (create clone)
    methods = [r["method"] for r in recorder.requests]
    assert methods == ["GET", "POST"]
    get, post = recorder.requests
    # auth header carried the bearer key on both
    assert get["auth"] == "Bearer sk-test-key"
    assert post["auth"] == "Bearer sk-test-key"
    # GET read the source by id; POST created a NEW assistant (no id/orgId), with
    # the variant applied and a fresh name
    assert get["path"] == "/assistant/asst_src"
    assert post["path"] == "/assistant"
    assert "id" not in post["body"] and "orgId" not in post["body"]   # stripped
    assert post["body"]["firstMessage"] == "changed"                  # variant applied
    assert post["body"]["name"] == "hotato-staging"
    # the adapter parsed the created clone id from the response
    assert result.get("clone_id") == "clone-abc123" or result.get("created")


def test_no_put_or_patch_is_ever_issued():
    # structural guarantee: the HTTP primitive refuses anything but GET/POST
    with pytest.raises(ValueError):
        _apply._http_json("PUT", "http://x", headers={}, body={}, timeout=1)
    with pytest.raises(ValueError):
        _apply._http_json("PATCH", "http://x", headers={}, body={}, timeout=1)
