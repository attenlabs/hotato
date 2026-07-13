"""Auth + audit primitives for ``hotato serve`` (the local team workspace).

The workspace server is a NEW local listening socket, so it carries its own
minimal, self-hosted security posture (no framework, stdlib only, per GOAL §6 +
the THREAT-MODEL row this adds):

* **Bearer token.** One shared secret authenticates every request. It is either
  operator-supplied (``--token`` / ``--token-file``) or generated on first start
  with :func:`secrets.token_urlsafe` and persisted 0600 under the workspace state
  dir so a restart keeps the same URL. It is NEVER printed to the audit log or
  echoed back in any response body -- only its short prefix is recorded.
* **Constant-time compare.** Every check runs through :func:`constant_time_eq`
  (``hmac.compare_digest`` on bytes), so a wrong token cannot be recovered by
  timing the comparison. Unequal-length inputs are handled without leaking.
* **Browser sessions.** A person opens the printed ``/?token=...`` URL once; the
  server mints an in-memory, unguessable session id and hands back an HttpOnly
  cookie so subsequent navigation carries no secret in the URL. Sessions live in
  memory only (never persisted, never cross-tenant) -- the "auth-session
  bookkeeping" the read-only server is explicitly allowed to keep.
* **Append-only audit.** :class:`AuditLog` records one JSONL line per
  authenticated request -- who (token/session prefix, never the secret), what
  (method + path, token stripped from the query), when (UTC ISO-8601), and the
  response status. It is the ONLY file the server writes; the workspace data is
  never mutated.

Zero-dependency: nothing here opens a network socket or calls out.
"""
from __future__ import annotations

import hmac
import json
import os
import secrets
import threading
import time
from typing import Optional, Tuple

from ..errors import open_regular as _open_regular

__all__ = [
    "constant_time_eq",
    "generate_token",
    "token_prefix",
    "resolve_token",
    "SessionStore",
    "AuditLog",
]

# The stored token file + audit log live under the per-workspace state dir; both
# are owner-only. The token is a capability, the audit log may name activity.
_TOKEN_FILENAME = "token"
_AUDIT_FILENAME = "audit.jsonl"
_OWNER_ONLY_FILE = 0o600
_OWNER_ONLY_DIR = 0o700


def constant_time_eq(a: str, b: str) -> bool:
    """Timing-safe string equality. Both sides are utf-8 encoded so a non-ASCII
    input can never raise inside :func:`hmac.compare_digest` (which rejects
    non-ASCII ``str``), and unequal lengths return ``False`` without leaking the
    length via early exit. Used for every token/session comparison so a partial
    match is not distinguishable by wall-clock time."""
    try:
        return hmac.compare_digest(a.encode("utf-8"), b.encode("utf-8"))
    except (AttributeError, TypeError):
        return False


def generate_token() -> str:
    """A fresh URL-safe bearer token (256 bits of ``secrets`` entropy). Printed
    once on first start and persisted 0600; used verbatim in the ``?token=`` URL
    and the ``Authorization: Bearer`` header."""
    return secrets.token_urlsafe(32)


def token_prefix(token: str) -> str:
    """A short, non-reversible label for the audit log: the first 8 characters
    plus an ellipsis. Enough to correlate a session's requests, never enough to
    replay the token."""
    if not token:
        return "-"
    return token[:8] + "…"


def resolve_token(
    state_dir: str,
    *,
    token: Optional[str] = None,
    token_file: Optional[str] = None,
) -> Tuple[str, str, bool]:
    """Resolve the bearer token by precedence and return
    ``(token, source, generated)``.

    Precedence (first non-empty wins):

    1. ``token`` -- an explicit ``--token`` value.
    2. ``token_file`` -- read the first line of ``--token-file``.
    3. the persisted ``<state_dir>/token`` from a previous start (so a restart
       keeps the same URL).
    4. a freshly generated token, persisted 0600 under ``state_dir``.

    ``generated`` is ``True`` only in case 4, so the caller can print the new
    token prominently on first start. A supplied token is NOT written back to
    disk (the operator owns its lifetime); only a generated one is persisted.
    Raises ``ValueError`` on an empty/whitespace ``--token`` or an unreadable
    ``--token-file`` (a usage error -> exit 2)."""
    if token is not None:
        tok = token.strip()
        if not tok:
            raise ValueError("--token was empty; supply a non-empty bearer token")
        return tok, "flag", False

    if token_file is not None:
        try:
            # open_regular: --token-file is an external, user-supplied path, so
            # a FIFO/named-pipe there raises immediately instead of blocking.
            with _open_regular(token_file, "r", encoding="utf-8") as fh:
                tok = fh.readline().strip()
        except OSError as exc:
            raise ValueError(f"could not read --token-file {token_file!r}: {exc}") from exc
        if not tok:
            raise ValueError(f"--token-file {token_file!r} contained no token")
        return tok, "file", False

    os.makedirs(state_dir, mode=_OWNER_ONLY_DIR, exist_ok=True)
    stored = os.path.join(state_dir, _TOKEN_FILENAME)
    if os.path.isfile(stored):
        # open_regular keeps this FIFO-safe even though the path is one the
        # server wrote under its own 0600 state dir.
        with _open_regular(stored, "r", encoding="utf-8") as fh:
            tok = fh.readline().strip()
        if tok:
            return tok, "stored", False

    tok = generate_token()
    # Write 0600 from the start: create with a restrictive mode, then chmod in
    # case the file already existed with a looser mode.
    fd = os.open(stored, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, _OWNER_ONLY_FILE)
    try:
        os.write(fd, (tok + "\n").encode("utf-8"))
    finally:
        os.close(fd)
    try:
        os.chmod(stored, _OWNER_ONLY_FILE)
    except OSError:
        pass
    return tok, "generated", True


class SessionStore:
    """A thread-safe set of live browser session ids (in memory only).

    A session id is minted when a request authenticates via the ``?token=`` URL
    and handed back as an HttpOnly cookie. It is unguessable
    (:func:`secrets.token_urlsafe`) and never persisted, so it dies with the
    process and cannot leak across restarts or tenants. Lookup is by exact
    membership; there is no expiry beyond process lifetime (a local single-node
    dev tool, not a public auth service)."""

    def __init__(self) -> None:
        self._ids: set = set()
        self._lock = threading.Lock()

    def mint(self) -> str:
        sid = secrets.token_urlsafe(24)
        with self._lock:
            self._ids.add(sid)
        return sid

    def valid(self, sid: str) -> bool:
        if not sid:
            return False
        with self._lock:
            # Membership check over a set of unguessable 192-bit ids; the id
            # itself is the secret, so a plain `in` does not leak useful timing.
            return sid in self._ids

    def count(self) -> int:
        with self._lock:
            return len(self._ids)


class AuditLog:
    """Append-only JSONL audit of authenticated requests.

    Every authenticated request appends exactly one line::

        {"ts": "2026-07-12T18:04:11Z", "who": "Ab3xQ_p1…", "method": "GET",
         "path": "/scenarios", "query": "status=FAIL", "status": 200,
         "remote": "127.0.0.1"}

    ``who`` is a token/session PREFIX, never the secret. ``query`` has any
    ``token`` parameter stripped so the audit trail never records the bearer
    secret. The file is created 0600 and only ever appended to -- it is the one
    file the server writes; the workspace registry + evidence are read-only."""

    def __init__(self, path: str) -> None:
        self.path = path
        self._lock = threading.Lock()
        d = os.path.dirname(path)
        if d:
            os.makedirs(d, mode=_OWNER_ONLY_DIR, exist_ok=True)
        # Touch 0600 so the very first append lands on an owner-only file.
        if not os.path.exists(path):
            try:
                fd = os.open(path, os.O_WRONLY | os.O_CREAT, _OWNER_ONLY_FILE)
                os.close(fd)
            except OSError:
                pass

    def record(
        self,
        *,
        who: str,
        method: str,
        path: str,
        query: str = "",
        status: int = 0,
        remote: str = "",
    ) -> None:
        rec = {
            "ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "who": who,
            "method": method,
            "path": path,
            "query": query,
            "status": status,
            "remote": remote,
        }
        line = json.dumps(rec, sort_keys=True) + "\n"
        # One writer at a time so interleaved requests never tear a line; the OS
        # append + our lock keep the JSONL well-formed under the threaded server.
        with self._lock:
            with open(self.path, "a", encoding="utf-8") as fh:
                fh.write(line)
