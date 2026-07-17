"""Adversarial protocol/security tests for the dependency-free WS client."""

from __future__ import annotations

import base64
import hashlib
import socket
import struct
import threading
import time

import pytest

from hotato import websocket_transport as WS


def _send_server_frame(sock, opcode, payload=b"", *, fin=True, masked=False, force16=False):
    first = (0x80 if fin else 0) | opcode
    length = len(payload)
    mask_bit = 0x80 if masked else 0
    if force16 or length >= 126:
        header = bytes((first, mask_bit | 126)) + struct.pack("!H", length)
    else:
        header = bytes((first, mask_bit | length))
    if masked:
        mask = b"abcd"
        encoded = bytes(byte ^ mask[index % 4] for index, byte in enumerate(payload))
        sock.sendall(header + mask + encoded)
    else:
        sock.sendall(header + payload)


def _pair(max_bytes=1024):
    client_sock, peer = socket.socketpair()
    return WS.WebSocketClient(client_sock, max_message_bytes=max_bytes), peer


def test_receive_reassembles_fragmentation_around_ping_and_sends_pong():
    client, peer = _pair()
    try:
        _send_server_frame(peer, 1, b"hel", fin=False)
        _send_server_frame(peer, 9, b"x")
        _send_server_frame(peer, 0, b"lo", fin=True)
        assert client.receive() == WS.WebSocketMessage("text", "hello")
        first, second = peer.recv(2)
        assert first == 0x8A and second & 0x80  # client pong is masked
        length = second & 0x7F
        mask = peer.recv(4)
        payload = bytearray(peer.recv(length))
        for index in range(length):
            payload[index] ^= mask[index % 4]
        assert bytes(payload) == b"x"
    finally:
        client.abort()
        peer.close()


def test_rejects_masked_server_frame_and_nonminimal_length():
    client, peer = _pair()
    try:
        _send_server_frame(peer, 1, b"x", masked=True)
        with pytest.raises(WS.WebSocketProtocolError, match="must not be masked"):
            client.receive()
    finally:
        client.abort()
        peer.close()

    client, peer = _pair()
    try:
        _send_server_frame(peer, 1, b"x", force16=True)
        with pytest.raises(WS.WebSocketProtocolError, match="non-minimal"):
            client.receive()
    finally:
        client.abort()
        peer.close()


def test_rejects_oversized_frame_before_reading_payload():
    client, peer = _pair(max_bytes=4)
    try:
        peer.sendall(bytes((0x82, 126)) + struct.pack("!H", 126))
        with pytest.raises(WS.WebSocketProtocolError, match="message limit"):
            client.receive()
    finally:
        client.abort()
        peer.close()


def test_close_frame_validates_code_and_closes_socket():
    client, peer = _pair()
    try:
        _send_server_frame(peer, 8, struct.pack("!H", 1005))
        with pytest.raises(WS.WebSocketProtocolError, match="close code"):
            client.receive()
    finally:
        client.abort()
        peer.close()


def test_absolute_deadline_interrupts_a_stalled_write():
    client_sock, peer = socket.socketpair()
    client_sock.setsockopt(socket.SOL_SOCKET, socket.SO_SNDBUF, 4096)
    client = WS.WebSocketClient(
        client_sock,
        max_message_bytes=4 * 1024 * 1024,
        deadline=time.monotonic() + 0.05,
    )
    started = time.monotonic()
    try:
        with pytest.raises(WS.WebSocketTimeout, match="write deadline"):
            client.send_binary(b"x" * (2 * 1024 * 1024))
        assert time.monotonic() - started < 1.0
    finally:
        client.abort()
        peer.close()


def test_remote_egress_and_header_injection_are_refused(monkeypatch):
    monkeypatch.setattr(
        WS,
        "_resolved_addresses",
        lambda _host, _port: [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("203.0.113.5", 80))],
    )
    with pytest.raises(ValueError, match="remote WebSocket egress"):
        WS.connect("ws://example.test/audio")
    with pytest.raises(ValueError, match="require wss://"):
        WS.connect("ws://example.test/audio", allow_remote=True)
    with pytest.raises(ValueError, match="userinfo"):
        WS.connect("ws://user:secret@example.test/audio", allow_remote=True)
    with pytest.raises(ValueError, match="fragments"):
        WS.connect("ws://example.test/audio#ignored", allow_remote=True)

    monkeypatch.setattr(
        WS,
        "_resolved_addresses",
        lambda _host, port: [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("127.0.0.1", port))],
    )
    with pytest.raises(ValueError, match="transport-owned"):
        WS.connect("ws://127.0.0.1:9", headers={"Host": "evil"})
    with pytest.raises(ValueError, match="printable ASCII"):
        WS.connect("ws://127.0.0.1:9", headers={"Authorization": "ok\r\nX-Evil: yes"})
    with pytest.raises(ValueError, match="HTTP token"):
        WS.connect("ws://127.0.0.1:9", headers={"Bad Name": "value"})
    with pytest.raises(ValueError, match="percent-encoded"):
        WS.connect("ws://127.0.0.1:9/path with space")


class _HandshakeServer:
    def __init__(self, *, bad_accept=False, extensions=False, pipelined=False):
        self.bad_accept = bad_accept
        self.extensions = extensions
        self.pipelined = pipelined
        self.error = None
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.sock.bind(("127.0.0.1", 0))
        self.sock.listen(1)
        self.url = f"ws://127.0.0.1:{self.sock.getsockname()[1]}/"
        self.thread = threading.Thread(target=self._serve, daemon=True)
        self.thread.start()

    def _serve(self):
        try:
            conn, _address = self.sock.accept()
            with conn:
                data = bytearray()
                while b"\r\n\r\n" not in data:
                    data.extend(conn.recv(4096))
                headers = {}
                for line in bytes(data).decode("iso-8859-1").split("\r\n")[1:]:
                    if ":" in line:
                        name, value = line.split(":", 1)
                        headers[name.lower()] = value.strip()
                value = base64.b64encode(
                    hashlib.sha1(
                        (headers["sec-websocket-key"] + "258EAFA5-E914-47DA-95CA-C5AB0DC85B11").encode()
                    ).digest()
                ).decode()
                if self.bad_accept:
                    value = "bad"
                extra = "Sec-WebSocket-Extensions: permessage-deflate\r\n" if self.extensions else ""
                response = (
                    "HTTP/1.1 101 Switching Protocols\r\n"
                    "Upgrade: websocket\r\n"
                    "Connection: Upgrade\r\n"
                    f"Sec-WebSocket-Accept: {value}\r\n"
                    f"{extra}\r\n"
                ).encode()
                if self.pipelined:
                    response += b"\x81\x00"
                conn.sendall(response)
        except BaseException as exc:
            self.error = exc
        finally:
            self.sock.close()

    def close(self):
        self.thread.join(timeout=3)
        assert not self.thread.is_alive()
        if self.error:
            raise self.error


@pytest.mark.parametrize(
    "kwargs,match",
    [
        ({"bad_accept": True}, "Accept mismatch"),
        ({"extensions": True}, "extensions"),
        ({"pipelined": True}, "pipelined"),
    ],
)
def test_handshake_refuses_unoffered_or_ambiguous_shapes(kwargs, match):
    server = _HandshakeServer(**kwargs)
    try:
        with pytest.raises(WS.WebSocketHandshakeError, match=match):
            WS.connect(server.url)
    finally:
        server.close()
