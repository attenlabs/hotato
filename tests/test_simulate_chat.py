"""``hotato simulate --chat URL``: the chat-agent simulation driver.

Pins the properties that make the slice shippable:

  (a) E2E -- the scripted scenario.v1 turn plan is driven against an
      in-process stdlib http.server stub agent (the documented tiny contract:
      POST {conversation_id, turn_index, text} -> 200 {text}), the replies are
      recorded VERBATIM with monotonic timestamps, the written transcript is
      EXACTLY the shape `hotato investigate --transcript` consumes (it scores
      it, exit 0, honesty gate applied), and the next step is printed;
  (b) PROVENANCE -- origin.kind == "simulated" on the transcript file, with
      the scripted simulator + seed named and the agent replies labelled as
      the live agent's own;
  (c) MEASUREMENT -- the agent reply latency is the measured HTTP round trip:
      a delayed stub shows up in latency_ms AND as the gap that places the
      agent turn on the timeline;
  (d) EGRESS GATE -- a non-local URL is refused (exit 2) without
      --egress-opt-in BEFORE any request; a non-http(s) scheme is always
      refused; localhost needs no opt-in;
  (e) CONTRACT -- a reply off the contract (missing 'text', non-JSON) is a
      loud exit-2 error naming the contract, and the driven POST bodies match
      the script verbatim and in order;
  (f) DETERMINISTIC PACING -- caller turn durations derive from the scenario's
      pacing model, identical across two drives (only the measured latency
      moves the timeline).
"""

import json
import threading
import time
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest

from hotato import chat_sim, cli


def _scenario(**over):
    doc = {
        "kind": "hotato.scenario", "version": 1, "id": "refund-chat",
        "goal": {"type": "get_refund", "target": "order A-1001"},
        "facts": {"order_id": "A-1001"},
        "caller": {
            "script": [
                {"say": "Hi, my order A-1001 arrived damaged and I want a "
                        "refund."},
                {"say": "It is A-1001."},
                {"say": "Please send the refund to my card."},
            ],
            "behavior": {"backchannels": {"probability": 0.0}},
        },
        "environment": {"locale": "en-US", "route": "chat"},
        "seed": 3,
    }
    doc.update(over)
    return doc


def _write_scenario(tmp_path, doc, name="s.json"):
    p = tmp_path / name
    p.write_text(json.dumps(doc), encoding="utf-8")
    return str(p)


def _stub_agent(reply_fn, recorder):
    """An in-process stdlib chat agent implementing the documented contract:
    POST JSON in, ``reply_fn(body) -> raw bytes`` out (bytes so a test can
    also answer off-contract)."""
    class H(BaseHTTPRequestHandler):
        def log_message(self, *a):  # silence
            pass

        def do_POST(self):
            length = int(self.headers.get("Content-Length", 0) or 0)
            body = json.loads(self.rfile.read(length)) if length else None
            recorder.append(body)
            payload = reply_fn(body)
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)
    return H


class _agent:
    """Context manager: serve ``reply_fn`` on 127.0.0.1:<ephemeral> and yield
    the URL; requests land in ``self.requests``."""

    def __init__(self, reply_fn):
        self._reply_fn = reply_fn
        self.requests = []

    def __enter__(self):
        self._server = HTTPServer(
            ("127.0.0.1", 0), _stub_agent(self._reply_fn, self.requests))
        port = self._server.server_address[1]
        self._thread = threading.Thread(
            target=self._server.serve_forever, daemon=True)
        self._thread.start()
        return f"http://127.0.0.1:{port}/chat"

    def __exit__(self, *exc):
        self._server.shutdown()
        return False


def _echo_reply(body):
    return json.dumps(
        {"text": f"Understood turn {body['turn_index']}: {body['text']}"}
    ).encode("utf-8")


# --------------------------------------------------------------------------
# (a) + (b) + (e): e2e chat sim -> transcript -> investigate --transcript
# --------------------------------------------------------------------------

def test_chat_e2e_transcript_scored_by_investigate(tmp_path, capsys, monkeypatch):
    monkeypatch.chdir(tmp_path)
    s = _write_scenario(tmp_path, _scenario())
    out = tmp_path / "chat"
    agent = _agent(_echo_reply)
    with agent as url:
        code = cli.main(["simulate", s, "--chat", url,
                         "--out", str(out), "--format", "json"])
    assert code == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["kind"] == "simulate-chat"
    assert payload["origin_kind"] == "simulated"
    assert payload["exit_code"] == 0
    path = payload["transcript_path"]
    assert path.endswith("chat-transcript.json")
    # the printed next step is the investigate --transcript on-ramp
    assert "hotato investigate --transcript" in payload["next"]

    # the written transcript: origin=simulated provenance + verbatim replies
    doc = json.loads((out / "chat-transcript.json").read_text(encoding="utf-8"))
    assert doc["origin"]["kind"] == "simulated"
    assert doc["origin"]["simulator"]["scenario_id"] == "refund-chat"
    assert doc["origin"]["simulator"]["seed"] == 3
    assert doc["origin"]["agent_replies"] == "live-chat-http"
    segs = doc["segments"]
    assert [x["role"] for x in segs] == ["caller", "agent"] * 3
    assert segs[1]["text"] == ("Understood turn 0: Hi, my order A-1001 "
                               "arrived damaged and I want a refund.")
    # monotonic timestamps, every span non-degenerate
    starts = [x["start"] for x in segs]
    assert starts == sorted(starts)
    assert all(x["end"] > x["start"] for x in segs)

    # the driven POST bodies match the script verbatim, in order
    with_ids = [(r["turn_index"], r["text"]) for r in agent.requests]
    assert with_ids == [
        (0, "Hi, my order A-1001 arrived damaged and I want a refund."),
        (1, "It is A-1001."),
        (2, "Please send the refund to my card."),
    ]

    # ... and `hotato investigate --transcript` consumes the file as-is
    code = cli.main(["investigate", "--transcript", path,
                     "--state", str(tmp_path / "state.json"),
                     "--format", "json"])
    assert code == 0
    result = json.loads(capsys.readouterr().out)
    assert result["kind"] == "investigate"
    assert result["verdict_status"]["mode"] == "transcript"
    # the transcript honesty gate held: overlap signals null with a reason
    assert result["event"]["verdict"]["talk_over_sec"] is None
    assert result["event"]["signals"]["barge_in"]["talk_over_reason"]


# --------------------------------------------------------------------------
# (c) measured latency places the agent turn
# --------------------------------------------------------------------------

def test_chat_measured_latency_places_agent_turn(tmp_path, capsys):
    delay_sec = 0.12

    def slow_first(body):
        if body["turn_index"] == 0:
            time.sleep(delay_sec)
        return _echo_reply(body)

    s = _write_scenario(tmp_path, _scenario())
    out = tmp_path / "chat"
    with _agent(slow_first) as url:
        code = cli.main(["simulate", s, "--chat", url,
                         "--out", str(out), "--format", "json"])
    assert code == 0
    payload = json.loads(capsys.readouterr().out)
    t0 = payload["turns"][0]
    # the measured HTTP round trip carries the stub's delay ...
    assert t0["latency_ms"] >= 100
    # ... and IS the gap that places the agent reply on the timeline
    assert t0["agent_start"] == pytest.approx(
        t0["caller_end"] + t0["latency_ms"] / 1000.0, abs=0.002)
    doc = json.loads((out / "chat-transcript.json").read_text(encoding="utf-8"))
    assert doc["chat"]["agent_latency_ms"][0] >= 100


# --------------------------------------------------------------------------
# (d) egress gate: local by default, refused BEFORE any request
# --------------------------------------------------------------------------

def test_chat_nonlocal_url_refused_without_egress_opt_in(tmp_path, capsys):
    s = _write_scenario(tmp_path, _scenario())
    code = cli.main(["simulate", s, "--chat", "http://agents.example.com/chat",
                     "--out", str(tmp_path / "chat")])
    assert code == 2
    err = capsys.readouterr().err
    assert "--egress-opt-in" in err
    assert "before any request" in err.lower()
    # refused before any request: nothing was written either
    assert not (tmp_path / "chat").exists()


def test_chat_url_gate_shapes():
    # localhost / 127.0.0.1 / ::1 need no opt-in
    chat_sim.check_chat_url("http://localhost:9/chat")
    chat_sim.check_chat_url("http://127.0.0.1:9/chat")
    # a non-local host passes ONLY with the explicit opt-in
    with pytest.raises(ValueError):
        chat_sim.check_chat_url("https://example.com/chat")
    chat_sim.check_chat_url("https://example.com/chat", egress_opt_in=True)
    # a non-http(s) scheme is refused even with the opt-in
    with pytest.raises(ValueError):
        chat_sim.check_chat_url("ftp://127.0.0.1/chat", egress_opt_in=True)


# --------------------------------------------------------------------------
# (e) a reply off the contract is a loud exit-2, naming the contract
# --------------------------------------------------------------------------

def test_chat_reply_missing_text_is_exit_2(tmp_path, capsys):
    s = _write_scenario(tmp_path, _scenario())
    with _agent(lambda body: json.dumps({"reply": "hi"}).encode()) as url:
        code = cli.main(["simulate", s, "--chat", url,
                         "--out", str(tmp_path / "chat")])
    assert code == 2
    assert "--chat contract" in capsys.readouterr().err


def test_chat_non_json_reply_is_exit_2(tmp_path, capsys):
    s = _write_scenario(tmp_path, _scenario())
    with _agent(lambda body: b"i am not json") as url:
        code = cli.main(["simulate", s, "--chat", url,
                         "--out", str(tmp_path / "chat")])
    assert code == 2
    assert "non-JSON" in capsys.readouterr().err


# --------------------------------------------------------------------------
# flag conflicts: --chat is the single live drive
# --------------------------------------------------------------------------

def test_chat_conflicts_with_matrix_and_repetitions(tmp_path):
    s = _write_scenario(tmp_path, _scenario())
    assert cli.main(["simulate", "--matrix", s,
                     "--chat", "http://127.0.0.1:9/chat"]) == 2
    assert cli.main(["simulate", s, "--repetitions", "3",
                     "--chat", "http://127.0.0.1:9/chat"]) == 2


# --------------------------------------------------------------------------
# (f) deterministic pacing + the printed next step on the text surface
# --------------------------------------------------------------------------

def test_chat_caller_pacing_is_deterministic_across_drives(tmp_path):
    doc = _scenario()
    with _agent(_echo_reply) as url:
        a = chat_sim.drive_chat(doc, 3, url)
        b = chat_sim.drive_chat(doc, 3, url)

    def caller_spans(r):
        return [(s["text"], round(s["end"] - s["start"], 3))
                for s in r["segments"] if s["role"] == "caller"]

    # the caller-side pacing derives from the scenario alone: same texts, same
    # durations, both drives (only the measured reply latency moves the
    # timeline between them)
    assert caller_spans(a) == caller_spans(b)
    assert a["segments"][0]["start"] == b["segments"][0]["start"]


def test_chat_text_surface_prints_next_step(tmp_path, capsys):
    s = _write_scenario(tmp_path, _scenario())
    out = tmp_path / "chat"
    with _agent(_echo_reply) as url:
        code = cli.main(["simulate", s, "--chat", url, "--out", str(out)])
    assert code == 0
    text = capsys.readouterr().out
    assert "origin=simulated (never real)" in text
    assert "next: hotato investigate --transcript" in text
    assert "latency" in text
