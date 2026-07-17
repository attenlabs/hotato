from __future__ import annotations

import hashlib
import json
import struct

import pytest

from hotato.caller_session import (
    PROTOCOL_SCHEMA,
    SUBPROTOCOL,
    CallerSessionError,
    WebSocketCallerSession,
)
from hotato.websocket_transport import (
    WebSocketMessage,
    WebSocketProtocolError,
    WebSocketTimeout,
)

CAPS = {
    "send_text": "SUPPORTED", "send_audio": "SUPPORTED", "receive": "SUPPORTED",
    "send_dtmf": "SUPPORTED", "wait": "SUPPORTED", "silence": "SUPPORTED",
    "impairment": "SUPPORTED", "observe_transfer": "UNOBSERVABLE",
    "hangup": "SUPPORTED",
}


class FakeSocket:
    def __init__(self, ready=None):
        self.text = []
        self.binary = []
        self.timeouts = []
        self.closed = False
        self.aborted = False
        self.incoming = [WebSocketMessage("text", json.dumps(ready))] if ready else []

    def send_text(self, value):
        self.text.append(value)
        parsed = json.loads(value)
        if parsed.get("type") == "command":
            self.incoming.append(WebSocketMessage("text", json.dumps({
                "schema": PROTOCOL_SCHEMA,
                "type": "command_result",
                "sequence": parsed["sequence"],
                "command": parsed["command"],
                "status": "completed",
                "receipt": {"accepted": True},
            })))
    def send_binary(self, value): self.binary.append(value)
    def receive(self):
        if not self.incoming:
            raise WebSocketTimeout("timeout")
        item = self.incoming.pop(0)
        if isinstance(item, Exception):
            raise item
        return item
    def set_timeout(self, value): self.timeouts.append(value)
    def close(self, *_args): self.closed = True
    def abort(self): self.aborted = True


def _session(**overrides):
    fake = FakeSocket()
    def connector(endpoint, **kwargs):
        assert endpoint == "ws://127.0.0.1:9000/caller"
        assert kwargs["subprotocol"] == SUBPROTOCOL
        hello_nonce = overrides.get("nonce", "nonce-1")
        fake.incoming.append(WebSocketMessage("text", json.dumps({
            "schema": PROTOCOL_SCHEMA, "type": "ready", "nonce": hello_nonce,
            "capabilities": overrides.get("capabilities", CAPS),
            "adapter": {"name": "fixture", "version": "1"},
        })))
        return fake
    return WebSocketCallerSession(
        "ws://127.0.0.1:9000/caller", connector=connector,
        nonce=overrides.get("nonce", "nonce-1"),
    ), fake


def test_nonce_bound_handshake_and_capabilities():
    session, fake = _session()
    hello = json.loads(fake.text[0])
    assert hello["type"] == "hello"
    assert hello["nonce"] == "nonce-1"
    assert session.capabilities() == CAPS
    assert session.adapter == {"name": "fixture", "version": "1"}
    assert session.evidence()["connected_endpoint_sha256"] == (
        "sha256:" + hashlib.sha256(
            b"ws://127.0.0.1:9000/caller"
        ).hexdigest()
    )


def test_remote_endpoint_evidence_uses_normalized_credential_free_identity():
    fake = FakeSocket()

    def connector(endpoint, **kwargs):
        assert endpoint == "WSS://Voice.Example.Test:443/caller"
        assert kwargs["allow_remote"] is True
        fake.incoming.append(WebSocketMessage("text", json.dumps({
            "schema": PROTOCOL_SCHEMA, "type": "ready", "nonce": "remote-nonce",
            "capabilities": CAPS,
            "adapter": {"name": "fixture", "version": "1"},
        })))
        return fake

    session = WebSocketCallerSession(
        "WSS://Voice.Example.Test:443/caller",
        connector=connector,
        allow_remote=True,
        nonce="remote-nonce",
    )
    assert session.evidence()["connected_endpoint_sha256"] == (
        "sha256:" + hashlib.sha256(
            b"wss://voice.example.test/caller"
        ).hexdigest()
    )


def test_text_audio_dtmf_impairment_and_hangup_wire_contract():
    session, fake = _session()
    session.send_text("hello", {"node": "n1"})
    session.send_audio(b"\x01\x00\x02\x00", 16000, {"node": "n2"})
    session.send_dtmf("1#a")
    session.wait(20)
    session.silence(30)
    session.set_impairment({"codec": "g711_ulaw"})
    commands = [json.loads(item)["command"] for item in fake.text[1:]]
    assert commands == ["send_text", "send_audio", "send_dtmf", "wait", "silence", "impairment"]
    magic, sequence = struct.unpack("!4sI", fake.binary[0][:8])
    assert magic == b"HTC1" and sequence == 2
    assert fake.binary[0][8:] == b"\x01\x00\x02\x00"
    evidence = session.evidence()
    assert evidence["authority"] == "sidecar_reported"
    assert [item["sequence"] for item in evidence["command_receipts"]] == list(
        range(1, 7)
    )
    session.hangup("finished")
    assert json.loads(fake.text[-1])["command"] == "hangup"
    assert fake.closed is True


def test_receive_returns_only_valid_event_and_timeout_is_none():
    session, fake = _session()
    fake.incoming.append(WebSocketMessage("text", json.dumps({
        "schema": PROTOCOL_SCHEMA, "type": "event",
        "event": {"kind": "transcript", "text": "agent response"},
    })))
    assert session.receive(10)["kind"] == "transcript"
    assert session.receive(10) is None
    assert fake.timeouts[-2:] == [0.01, None]


def test_ready_must_declare_every_capability_and_aborts():
    with pytest.raises(CallerSessionError, match="every"):
        _session(capabilities={"send_text": "SUPPORTED"})


def test_receive_refuses_non_event_control_message():
    session, fake = _session()
    fake.incoming.append(WebSocketMessage("text", json.dumps({
        "schema": PROTOCOL_SCHEMA, "type": "ready", "event": {},
    })))
    with pytest.raises(WebSocketProtocolError, match="non-event"):
        session.receive(10)


def test_command_requires_correlated_result_and_keeps_interleaved_event():
    session, fake = _session()
    # Replace the auto-generated acknowledgement with an event followed by the
    # same correlated acknowledgement.
    original_send = fake.send_text

    def event_then_ack(value):
        original_send(value)
        parsed = json.loads(value)
        if parsed.get("type") == "command":
            ack = fake.incoming.pop()
            fake.incoming.extend([
                WebSocketMessage("text", json.dumps({
                    "schema": PROTOCOL_SCHEMA,
                    "type": "event",
                    "event": {"kind": "timing", "name": "tts_started"},
                })),
                ack,
            ])

    fake.send_text = event_then_ack
    session.send_text("hello", {})
    assert session.receive(10) == {"kind": "timing", "name": "tts_started"}

    fake.send_text = lambda value: fake.text.append(value)
    with pytest.raises(CallerSessionError, match="acknowledge"):
        session.send_dtmf("1")


def test_interleaved_target_receipt_can_be_drained_into_caller_result():
    session, fake = _session()
    original_send = fake.send_text
    receipt = {
        "kind": "custom",
        "custom_type": "hotato.delivered-audio.v1",
        "authority": "target_boundary",
        "submitted_sha256": "sha256:" + "1" * 64,
        "delivered_sha256": "sha256:" + "2" * 64,
        "workload_child_id": "child-a",
        "workload_plan_sha256": "sha256:" + "3" * 64,
    }

    def event_then_ack(value):
        original_send(value)
        parsed = json.loads(value)
        if parsed.get("type") == "command":
            ack = fake.incoming.pop()
            fake.incoming.extend([
                WebSocketMessage("text", json.dumps({
                    "schema": PROTOCOL_SCHEMA, "type": "event", "event": receipt,
                })),
                ack,
            ])

    fake.send_text = event_then_ack
    session.send_audio(b"\x01\x00", 16_000, {})
    assert session.drain_events() == [receipt]
    assert session.drain_events() == []


def test_command_refuses_uncorrelated_or_failed_result():
    session, fake = _session()

    def bad_sequence(value):
        fake.text.append(value)
        parsed = json.loads(value)
        if parsed.get("type") == "command":
            fake.incoming.append(WebSocketMessage("text", json.dumps({
                "schema": PROTOCOL_SCHEMA,
                "type": "command_result",
                "sequence": parsed["sequence"] + 1,
                "command": parsed["command"],
                "status": "completed",
                "receipt": {},
            })))

    fake.send_text = bad_sequence
    with pytest.raises(WebSocketProtocolError, match="uncorrelated"):
        session.send_text("hello", {})


def test_headers_refuse_control_characters_before_connector():
    with pytest.raises(ValueError, match="control"):
        WebSocketCallerSession(
            "ws://127.0.0.1:9000", headers={"Authorization": "x\ny"},
            connector=lambda *_a, **_k: pytest.fail("must not connect"),
        )


def test_oversized_control_json_is_refused_before_parsing():
    fake = FakeSocket()
    fake.incoming.append(
        WebSocketMessage("text", "{" + " " * (1024 * 1024) + "}")
    )
    with pytest.raises(WebSocketProtocolError, match="exceeds 1 MiB"):
        WebSocketCallerSession(
            "ws://127.0.0.1:9000", connector=lambda *_a, **_k: fake,
        )
    assert fake.aborted


def test_cumulative_receipts_are_bounded(monkeypatch):
    session, fake = _session()
    monkeypatch.setattr("hotato.caller_session._MAX_STORED_EVIDENCE_BYTES", 1)

    def acknowledge(value):
        fake.text.append(value)
        parsed = json.loads(value)
        if parsed.get("type") == "command":
            fake.incoming.append(WebSocketMessage("text", json.dumps({
                "schema": PROTOCOL_SCHEMA,
                "type": "command_result",
                "sequence": parsed["sequence"],
                "command": parsed["command"],
                "status": "completed",
                "receipt": {"accepted": True},
            })))

    fake.send_text = acknowledge
    with pytest.raises(CallerSessionError, match="stored-evidence"):
        session.send_text("hello", {})


def test_headers_have_count_and_total_byte_bounds():
    def connector(*_args, **_kwargs):
        pytest.fail("must not connect")

    with pytest.raises(ValueError, match="header limit"):
        WebSocketCallerSession(
            "ws://127.0.0.1:9000",
            headers={f"X-{index}": "v" for index in range(65)},
            connector=connector,
        )
    with pytest.raises(ValueError, match="byte limit"):
        WebSocketCallerSession(
            "ws://127.0.0.1:9000",
            headers={"Authorization": "x" * (256 * 1024)},
            connector=connector,
        )
