"""Small, strict RFC 6455 client used by Hotato's local audio harness.

The core package deliberately has no runtime dependencies.  This module keeps
that property while providing the narrow WebSocket surface the scripted-call
harness needs: one reader, one writer, text/binary messages, fragmentation,
ping/pong, close, TLS verification, and explicit message/header size limits.

It is a WebSocket client, not a SIP, PSTN, WebRTC, or provider adapter.  Remote
destinations are refused by default.  Callers must set ``allow_remote=True``
explicitly to permit network egress beyond a loopback address.
"""

from __future__ import annotations

import base64
import hashlib
import ipaddress
import os
import re
import socket
import ssl
import struct
import threading
import time
from dataclasses import dataclass
from typing import Callable, Dict, Optional, Tuple
from urllib.parse import urlsplit

from .errors import sanitize_url

__all__ = [
    "WebSocketError",
    "WebSocketHandshakeError",
    "WebSocketProtocolError",
    "WebSocketTimeout",
    "WebSocketMessage",
    "WebSocketClient",
    "connect",
]

_GUID = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"
_MAX_HEADER_BYTES = 64 * 1024
_RESERVED_REQUEST_HEADERS = {
    "connection",
    "host",
    "sec-websocket-extensions",
    "sec-websocket-key",
    "sec-websocket-protocol",
    "sec-websocket-version",
    "upgrade",
}
_HTTP_TOKEN = re.compile(r"^[!#$%&'*+.^_`|~0-9A-Za-z-]+$")


class WebSocketError(RuntimeError):
    """Base error for the dependency-free WebSocket transport."""


class WebSocketHandshakeError(WebSocketError):
    """The peer did not complete a valid RFC 6455 opening handshake."""


class WebSocketProtocolError(WebSocketError):
    """The peer sent an invalid or unsupported RFC 6455 frame."""


class WebSocketTimeout(WebSocketError):
    """The connection exceeded its caller-supplied monotonic deadline."""


@dataclass(frozen=True)
class WebSocketMessage:
    """One reassembled WebSocket message.

    ``kind`` is ``"text"``, ``"binary"``, or ``"close"``.  Text messages
    carry a ``str``; binary and close messages carry ``bytes``.
    """

    kind: str
    data: object


def _validate_header(name: str, value: str) -> Tuple[str, str]:
    if not isinstance(name, str) or not _HTTP_TOKEN.fullmatch(name):
        raise ValueError("WebSocket header names must be RFC HTTP token strings")
    if name.lower() in _RESERVED_REQUEST_HEADERS:
        raise ValueError(f"WebSocket header {name!r} is transport-owned and cannot be overridden")
    if not isinstance(value, str) or any(
        ord(char) < 32 and char != "\t" or ord(char) == 127 or ord(char) > 126
        for char in value
    ):
        raise ValueError(
            f"WebSocket header {name!r} must contain printable ASCII (or horizontal tab) only"
        )
    return name, value


def _resolved_addresses(host: str, port: int) -> list:
    try:
        return socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
    except OSError as exc:
        raise WebSocketError(f"could not resolve WebSocket host {host!r}: {exc}") from exc


def _is_loopback(sockaddr: tuple) -> bool:
    try:
        return ipaddress.ip_address(sockaddr[0]).is_loopback
    except (ValueError, IndexError):
        return False


def _connect_socket(
    addresses: list,
    *,
    host: str,
    deadline: float,
    clock: Callable[[], float],
    use_tls: bool,
    ssl_context: Optional[ssl.SSLContext],
) -> socket.socket:
    last_error: Optional[Exception] = None
    for family, socktype, proto, _canonname, sockaddr in addresses:
        remaining = deadline - clock()
        if remaining <= 0:
            raise WebSocketTimeout("WebSocket connection deadline expired")
        raw = socket.socket(family, socktype, proto)
        raw.settimeout(remaining)
        try:
            raw.connect(sockaddr)
            if use_tls:
                remaining = deadline - clock()
                if remaining <= 0:
                    raise WebSocketTimeout("WebSocket TLS handshake deadline expired")
                raw.settimeout(remaining)
                context = ssl_context or ssl.create_default_context()
                return context.wrap_socket(raw, server_hostname=host)
            return raw
        except (OSError, ssl.SSLError, WebSocketError) as exc:
            last_error = exc
            raw.close()
    if clock() >= deadline:
        raise WebSocketTimeout("WebSocket connection deadline expired")
    raise WebSocketError(f"could not connect to WebSocket endpoint: {last_error}")


def _set_socket_deadline(
    sock: socket.socket,
    deadline: float,
    clock: Callable[[], float],
    subject: str,
) -> None:
    remaining = deadline - clock()
    if remaining <= 0:
        raise WebSocketTimeout(f"{subject} deadline expired")
    sock.settimeout(remaining)


def _read_http_headers(
    sock: socket.socket,
    max_bytes: int,
    *,
    deadline: float,
    clock: Callable[[], float],
) -> bytes:
    data = bytearray()
    while b"\r\n\r\n" not in data:
        _set_socket_deadline(sock, deadline, clock, "WebSocket opening handshake")
        try:
            chunk = sock.recv(min(4096, max_bytes + 1 - len(data)))
        except socket.timeout as exc:
            raise WebSocketTimeout("WebSocket opening handshake deadline expired") from exc
        if not chunk:
            raise WebSocketHandshakeError("WebSocket peer closed during opening handshake")
        data.extend(chunk)
        if len(data) > max_bytes:
            raise WebSocketHandshakeError(
                f"WebSocket handshake exceeds the {max_bytes}-byte header limit"
            )
    head, remainder = bytes(data).split(b"\r\n\r\n", 1)
    if remainder:
        # A peer may pipeline a frame behind the response.  The transport's
        # frame reader cannot safely push bytes back into a socket, so refuse
        # this uncommon shape instead of silently dropping evidence bytes.
        raise WebSocketHandshakeError(
            "WebSocket peer pipelined frame bytes with the opening handshake; unsupported"
        )
    return head


def _parse_http_response(head: bytes) -> Tuple[int, Dict[str, str]]:
    try:
        lines = head.decode("iso-8859-1").split("\r\n")
    except UnicodeDecodeError as exc:  # pragma: no cover - iso-8859-1 decodes all bytes
        raise WebSocketHandshakeError("WebSocket handshake headers are not decodable") from exc
    parts = lines[0].split(" ", 2)
    if len(parts) < 2 or not parts[0].startswith("HTTP/"):
        raise WebSocketHandshakeError("WebSocket handshake has an invalid HTTP status line")
    try:
        status = int(parts[1])
    except ValueError as exc:
        raise WebSocketHandshakeError("WebSocket handshake has an invalid HTTP status") from exc
    headers: Dict[str, str] = {}
    for line in lines[1:]:
        if not line or ":" not in line:
            raise WebSocketHandshakeError("WebSocket handshake contains a malformed header")
        name, value = line.split(":", 1)
        key = name.strip().lower()
        if not _HTTP_TOKEN.fullmatch(key):
            raise WebSocketHandshakeError("WebSocket handshake contains an invalid header name")
        value = value.strip()
        if any(ord(char) < 32 and char != "\t" or ord(char) == 127 for char in value):
            raise WebSocketHandshakeError("WebSocket handshake contains a control character")
        headers[key] = f"{headers[key]}, {value}" if key in headers else value
    return status, headers


def _token_present(value: str, token: str) -> bool:
    return token.lower() in {part.strip().lower() for part in value.split(",")}


def connect(
    url: str,
    *,
    subprotocol: Optional[str] = None,
    headers: Optional[Dict[str, str]] = None,
    timeout: float = 10.0,
    max_message_bytes: int = 16 * 1024 * 1024,
    allow_remote: bool = False,
    ssl_context: Optional[ssl.SSLContext] = None,
    deadline: Optional[float] = None,
    clock: Callable[[], float] = time.monotonic,
) -> "WebSocketClient":
    """Open and validate a WebSocket connection.

    Only ``ws://`` and ``wss://`` are accepted.  Userinfo and URL fragments are
    refused.  Every resolved destination must be loopback unless
    ``allow_remote`` is true, making accidental network egress a visible API
    decision.  Environment proxy variables are not consulted.
    """

    if not isinstance(url, str) or not url:
        raise ValueError("WebSocket URL must be a non-empty string")
    if isinstance(timeout, bool) or not isinstance(timeout, (int, float)) or timeout <= 0:
        raise ValueError("WebSocket timeout must be a positive number")
    if isinstance(max_message_bytes, bool) or not isinstance(max_message_bytes, int) or max_message_bytes < 1:
        raise ValueError("max_message_bytes must be a positive integer")
    if not callable(clock):
        raise TypeError("clock must be callable")
    if deadline is None:
        deadline = clock() + float(timeout)
    elif isinstance(deadline, bool) or not isinstance(deadline, (int, float)):
        raise ValueError("deadline must be an absolute monotonic number")
    if deadline <= clock():
        raise WebSocketTimeout("WebSocket connection deadline expired")
    try:
        parsed = urlsplit(url)
        port = parsed.port
    except ValueError as exc:
        raise ValueError(f"invalid WebSocket URL: {exc}") from exc
    if parsed.scheme.lower() not in ("ws", "wss"):
        raise ValueError("WebSocket URL scheme must be ws:// or wss://")
    if not parsed.hostname:
        raise ValueError("WebSocket URL must include a host")
    if parsed.username is not None or parsed.password is not None:
        raise ValueError("WebSocket URL userinfo is refused; pass authorization in a header")
    if parsed.fragment:
        raise ValueError("WebSocket URL fragments are not sent to servers and are refused")
    target = parsed.path or "/"
    if parsed.query:
        target += "?" + parsed.query
    if any(ord(char) < 0x21 or ord(char) > 0x7E for char in target):
        raise ValueError(
            "WebSocket URL path/query must be ASCII and percent-encoded without whitespace/control characters"
        )
    host = parsed.hostname
    port = port or (443 if parsed.scheme.lower() == "wss" else 80)
    addresses = _resolved_addresses(host, port)
    remote = not addresses or any(not _is_loopback(row[4]) for row in addresses)
    if not allow_remote and remote:
        raise ValueError(
            "remote WebSocket egress is disabled; use a loopback endpoint or set "
            "allow_remote=True explicitly"
        )
    if remote and parsed.scheme.lower() != "wss":
        raise ValueError("remote WebSocket connections require wss://")
    checked_headers = [_validate_header(k, v) for k, v in (headers or {}).items()]
    if subprotocol is not None:
        if not isinstance(subprotocol, str) or not _HTTP_TOKEN.fullmatch(subprotocol):
            raise ValueError("WebSocket subprotocol must be one non-empty token")

    sock = _connect_socket(
        addresses,
        host=host,
        deadline=min(float(deadline), clock() + float(timeout)),
        clock=clock,
        use_tls=parsed.scheme.lower() == "wss",
        ssl_context=ssl_context,
    )
    key = base64.b64encode(os.urandom(16)).decode("ascii")
    try:
        header_host = host.encode("idna").decode("ascii")
    except UnicodeError as exc:
        sock.close()
        raise ValueError("WebSocket host is not a valid IDNA name/address") from exc
    display_host = (
        f"[{header_host}]" if ":" in header_host and not header_host.startswith("[") else header_host
    )
    default_port = 443 if parsed.scheme.lower() == "wss" else 80
    host_header = display_host if port == default_port else f"{display_host}:{port}"
    lines = [
        f"GET {target} HTTP/1.1",
        f"Host: {host_header}",
        "Upgrade: websocket",
        "Connection: Upgrade",
        f"Sec-WebSocket-Key: {key}",
        "Sec-WebSocket-Version: 13",
    ]
    if subprotocol is not None:
        lines.append(f"Sec-WebSocket-Protocol: {subprotocol}")
    lines.extend(f"{name}: {value}" for name, value in checked_headers)
    request = ("\r\n".join(lines) + "\r\n\r\n").encode("ascii")
    try:
        _set_socket_deadline(sock, float(deadline), clock, "WebSocket opening handshake")
        try:
            sock.sendall(request)
        except socket.timeout as exc:
            raise WebSocketTimeout("WebSocket opening handshake deadline expired") from exc
        head = _read_http_headers(
            sock,
            _MAX_HEADER_BYTES,
            deadline=float(deadline),
            clock=clock,
        )
        status, response_headers = _parse_http_response(head)
        if status != 101:
            raise WebSocketHandshakeError(
                f"WebSocket endpoint {sanitize_url(url)} returned HTTP {status}, expected 101"
            )
        if not _token_present(response_headers.get("upgrade", ""), "websocket"):
            raise WebSocketHandshakeError("WebSocket handshake is missing Upgrade: websocket")
        if not _token_present(response_headers.get("connection", ""), "upgrade"):
            raise WebSocketHandshakeError("WebSocket handshake is missing Connection: Upgrade")
        expected = base64.b64encode(hashlib.sha1((key + _GUID).encode("ascii")).digest()).decode(
            "ascii"
        )
        if response_headers.get("sec-websocket-accept") != expected:
            raise WebSocketHandshakeError("WebSocket handshake Sec-WebSocket-Accept mismatch")
        if "sec-websocket-extensions" in response_headers:
            raise WebSocketHandshakeError(
                "WebSocket endpoint negotiated extensions; this strict client offers none"
            )
        selected = response_headers.get("sec-websocket-protocol")
        if subprotocol is not None and selected != subprotocol:
            raise WebSocketHandshakeError(
                f"WebSocket endpoint did not select required subprotocol {subprotocol!r}"
            )
        if subprotocol is None and selected is not None:
            raise WebSocketHandshakeError("WebSocket endpoint selected an unoffered subprotocol")
    except Exception:
        sock.close()
        raise
    return WebSocketClient(
        sock,
        max_message_bytes=max_message_bytes,
        deadline=float(deadline),
        clock=clock,
    )


class WebSocketClient:
    """Strict RFC 6455 connection supporting one concurrent reader/writer."""

    def __init__(
        self,
        sock: socket.socket,
        *,
        max_message_bytes: int,
        deadline: Optional[float] = None,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._sock = sock
        self._max_message_bytes = max_message_bytes
        self._send_lock = threading.Lock()
        self._recv_lock = threading.Lock()
        self._closed = False
        self._close_sent = False
        self._deadline = deadline
        self._clock = clock

    def set_timeout(self, timeout: Optional[float]) -> None:
        """Set the underlying socket timeout (``None`` means blocking)."""

        self._sock.settimeout(timeout)

    def set_deadline(
        self,
        deadline: Optional[float],
        clock: Optional[Callable[[], float]] = None,
    ) -> None:
        """Set one absolute monotonic deadline for every later socket operation."""

        if deadline is not None and (
            isinstance(deadline, bool) or not isinstance(deadline, (int, float))
        ):
            raise ValueError("deadline must be an absolute monotonic number or None")
        if clock is not None:
            if not callable(clock):
                raise TypeError("clock must be callable")
            self._clock = clock
        self._deadline = float(deadline) if deadline is not None else None
        self._apply_deadline("WebSocket operation")

    def _apply_deadline(self, subject: str) -> None:
        if self._deadline is None:
            return
        _set_socket_deadline(self._sock, self._deadline, self._clock, subject)

    def _send_frame(self, opcode: int, payload: bytes, *, fin: bool = True) -> None:
        if self._closed:
            raise WebSocketError("WebSocket connection is closed")
        if opcode >= 0x8 and (not fin or len(payload) > 125):
            raise ValueError("WebSocket control frames must be final and at most 125 bytes")
        first = (0x80 if fin else 0) | opcode
        length = len(payload)
        if length < 126:
            header = bytearray((first, 0x80 | length))
        elif length <= 0xFFFF:
            header = bytearray((first, 0x80 | 126)) + bytearray(struct.pack("!H", length))
        else:
            header = bytearray((first, 0x80 | 127)) + bytearray(struct.pack("!Q", length))
        mask = os.urandom(4)
        header.extend(mask)
        masked = bytearray(payload)
        for index in range(length):
            masked[index] ^= mask[index % 4]
        with self._send_lock:
            self._apply_deadline("WebSocket write")
            try:
                self._sock.sendall(bytes(header) + bytes(masked))
            except socket.timeout as exc:
                raise WebSocketTimeout("WebSocket write deadline expired") from exc

    def send_text(self, text: str) -> None:
        if not isinstance(text, str):
            raise TypeError("WebSocket text payload must be a string")
        payload = text.encode("utf-8")
        if len(payload) > self._max_message_bytes:
            raise ValueError("WebSocket text message exceeds configured message limit")
        self._send_frame(0x1, payload)

    def send_binary(self, payload: bytes) -> None:
        if not isinstance(payload, (bytes, bytearray, memoryview)):
            raise TypeError("WebSocket binary payload must be bytes-like")
        raw = bytes(payload)
        if len(raw) > self._max_message_bytes:
            raise ValueError("WebSocket binary message exceeds configured message limit")
        self._send_frame(0x2, raw)

    def _read_exact(self, count: int) -> bytes:
        chunks = bytearray()
        while len(chunks) < count:
            self._apply_deadline("WebSocket read")
            try:
                part = self._sock.recv(count - len(chunks))
            except socket.timeout as exc:
                raise WebSocketTimeout("WebSocket read deadline expired") from exc
            if not part:
                raise WebSocketError("WebSocket peer closed without a close frame")
            chunks.extend(part)
        return bytes(chunks)

    def _recv_frame(self) -> Tuple[bool, int, bytes]:
        head = self._read_exact(2)
        first, second = head
        fin = bool(first & 0x80)
        rsv = first & 0x70
        opcode = first & 0x0F
        masked = bool(second & 0x80)
        length = second & 0x7F
        if rsv:
            raise WebSocketProtocolError("WebSocket peer set RSV bits without an extension")
        if masked:
            raise WebSocketProtocolError("WebSocket server frames must not be masked")
        if opcode not in (0x0, 0x1, 0x2, 0x8, 0x9, 0xA):
            raise WebSocketProtocolError(f"unsupported WebSocket opcode 0x{opcode:x}")
        if length == 126:
            length = struct.unpack("!H", self._read_exact(2))[0]
            if length < 126:
                raise WebSocketProtocolError("non-minimal WebSocket 16-bit length encoding")
        elif length == 127:
            length = struct.unpack("!Q", self._read_exact(8))[0]
            if length < 65536 or length & (1 << 63):
                raise WebSocketProtocolError("invalid/non-minimal WebSocket 64-bit length")
        if opcode >= 0x8 and (not fin or length > 125):
            raise WebSocketProtocolError("invalid fragmented/oversized WebSocket control frame")
        if length > self._max_message_bytes:
            raise WebSocketProtocolError(
                f"WebSocket frame exceeds the {self._max_message_bytes}-byte message limit"
            )
        return fin, opcode, self._read_exact(length)

    def receive(self) -> WebSocketMessage:
        """Read one complete data or close message, handling control frames."""

        with self._recv_lock:
            fragments = bytearray()
            message_opcode: Optional[int] = None
            while True:
                fin, opcode, payload = self._recv_frame()
                if opcode == 0x9:  # ping
                    self._send_frame(0xA, payload)
                    continue
                if opcode == 0xA:  # pong
                    continue
                if opcode == 0x8:
                    if len(payload) == 1:
                        raise WebSocketProtocolError("WebSocket close payload has an invalid length")
                    if len(payload) >= 2:
                        code = struct.unpack("!H", payload[:2])[0]
                        if code < 1000 or code in (1004, 1005, 1006, 1015) or code >= 5000:
                            raise WebSocketProtocolError(f"invalid WebSocket close code {code}")
                        try:
                            payload[2:].decode("utf-8")
                        except UnicodeDecodeError as exc:
                            raise WebSocketProtocolError("WebSocket close reason is not UTF-8") from exc
                    if not self._close_sent:
                        self._send_frame(0x8, payload)
                        self._close_sent = True
                    self._closed = True
                    try:
                        self._sock.shutdown(socket.SHUT_RDWR)
                    except OSError:
                        pass
                    self._sock.close()
                    return WebSocketMessage("close", payload)
                if opcode in (0x1, 0x2):
                    if message_opcode is not None:
                        raise WebSocketProtocolError(
                            "new WebSocket data message began before fragmented message completed"
                        )
                    message_opcode = opcode
                    fragments.extend(payload)
                elif opcode == 0x0:
                    if message_opcode is None:
                        raise WebSocketProtocolError("unexpected WebSocket continuation frame")
                    fragments.extend(payload)
                if len(fragments) > self._max_message_bytes:
                    raise WebSocketProtocolError(
                        f"WebSocket message exceeds the {self._max_message_bytes}-byte limit"
                    )
                if fin:
                    raw = bytes(fragments)
                    if message_opcode == 0x1:
                        try:
                            return WebSocketMessage("text", raw.decode("utf-8"))
                        except UnicodeDecodeError as exc:
                            raise WebSocketProtocolError("WebSocket text message is not UTF-8") from exc
                    return WebSocketMessage("binary", raw)

    def close(self, code: int = 1000, reason: str = "") -> None:
        """Send a close frame and close the socket.  Safe to call repeatedly."""

        if self._closed:
            return
        if not isinstance(code, int) or code < 1000 or code >= 5000 or code in (
            1004,
            1005,
            1006,
            1015,
        ):
            raise ValueError("invalid WebSocket close code")
        if not isinstance(reason, str):
            raise TypeError("WebSocket close reason must be a string")
        payload = struct.pack("!H", code) + reason.encode("utf-8")
        if len(payload) > 125:
            raise ValueError("WebSocket close reason is too long")
        try:
            if not self._close_sent:
                self._send_frame(0x8, payload)
                self._close_sent = True
        except (OSError, WebSocketError):
            pass
        finally:
            self.abort()

    def abort(self) -> None:
        """Close immediately, unblocking a reader (used on run timeout)."""

        if self._closed:
            return
        self._closed = True
        try:
            self._sock.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass
        self._sock.close()

    def __enter__(self) -> "WebSocketClient":
        return self

    def __exit__(self, _exc_type, _exc, _tb) -> None:
        self.close()
