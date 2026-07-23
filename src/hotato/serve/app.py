"""The ``hotato serve`` HTTP server: a threaded, token-authenticated, read-only
local web app over the fleet registry + conversation artifacts (GOAL §6).

Stdlib only -- ``http.server.ThreadingHTTPServer`` + ``sqlite3`` -- no framework
dependency. Design points:

* **Default bind 127.0.0.1.** The server binds loopback unless the operator
  explicitly passes ``--host``; a non-loopback bind prints a prominent warning
  (it exposes the workspace to the local network).
* **Bearer-token auth on every request.** Constant-time compared. A browser
  bootstraps a session from the printed ``/?token=…`` URL (server mints an
  HttpOnly cookie, then redirects to strip the secret from the address bar);
  agents/API clients send ``Authorization: Bearer``. Every request is checked;
  an unauthenticated request gets 401 and is never routed.
* **Append-only audit.** Every request (authenticated or not) appends one JSONL
  line -- who (token/session prefix), what (method + path, token stripped from
  the query), when, and the response status. It is the ONLY thing the server
  writes.
* **Zero egress.** The server only binds a listening socket; it never opens an
  outbound connection and imports nothing that phones home. Evidence stays local.
* **Read-only.** Each request opens its own :class:`Registry` connection (the
  registry is single-thread per connection) and only ``SELECT``s. Reviews and
  labels remain CLI-driven; there are no write endpoints.

Every view has a ``?format=json`` machine mirror built from the SAME model dict
the HTML renderer formats, so the two can never diverge.
"""
from __future__ import annotations

import http.cookies
import json
import os
import re
import sys
import threading
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Optional, Tuple
from urllib.parse import (
    parse_qs,
    parse_qsl,
    quote,
    unquote,
    urlencode,
    urlsplit,
    urlunsplit,
)

from ..fleet.registry import DEFAULT_HOME, Registry
from . import data as _data
from . import render as _render
from .security import (
    AuditLog,
    SessionStore,
    constant_time_eq,
    resolve_token,
    token_prefix,
)

__all__ = ["ServeContext", "build_server", "run_serve"]

_LOOPBACK = {"127.0.0.1", "::1", "localhost", ""}
# Cap the drill-to-evidence raw endpoint so a stray large blob is not streamed
# through the text viewer; the digests the UI links (manifest/transcript/trace)
# are small JSON/text. Audio is stored separately and never linked here.
_EVIDENCE_MAX_BYTES = 5 * 1024 * 1024
_HEX64 = re.compile(r"^[0-9a-f]{64}$")


def _safe_dirname(workspace: str) -> str:
    """A filesystem-safe state-dir name for a workspace id. The id itself is used
    verbatim in parameterized SQL (safe), but as a directory component it must
    not enable traversal, so anything outside ``[A-Za-z0-9._-]`` is replaced and
    ``.``/``..``/empty collapse to ``default``."""
    name = re.sub(r"[^A-Za-z0-9._-]", "_", workspace or "")
    if name in ("", ".", ".."):
        return "default"
    return name


# =========================================================================
# server context (shared, thread-safe; per-request state opens fresh)
# =========================================================================

class ServeContext:
    """Immutable-ish shared state for the running server. Everything mutable
    inside it (the session store, the audit log) is independently thread-safe;
    per-request database/store handles are opened fresh in the handler thread
    because a registry connection is single-thread."""

    def __init__(self, *, home: str, workspace: str, store_root: str, token: str,
                 state_dir: str, audit: AuditLog, sessions: SessionStore,
                 bind_host: str, production_db: Optional[str] = None) -> None:
        self.home = home
        self.workspace = workspace
        self.store_root = store_root
        self.token = token
        self.state_dir = state_dir
        self.audit = audit
        self.sessions = sessions
        self.bind_host = bind_host
        self.production_db = production_db

    def open_registry(self) -> Registry:
        """A fresh registry connection for this request's thread. Read-only in
        practice: only ``SELECT``s run; the open re-asserts the (idempotent)
        schema, writing no workspace data."""
        return Registry(home=self.home)

    def open_store(self):
        """The content-addressed artifact store, or ``None`` if this workspace
        has no store yet. Only opened when it already exists (guarded on the
        ``blobs`` dir) so a read-only view never creates store directories."""
        from ..fleet.store import ArtifactStore
        if not os.path.isdir(os.path.join(self.store_root, "blobs")):
            return None
        return ArtifactStore(self.store_root)

    def read_production_evidence(self):
        """Read the optional, separate production DB through its mode=ro adapter.

        No production row is imported into the fleet registry.  Opening the
        writer ``ProductionStore`` here would violate the server's read-only
        contract because that constructor creates/migrates schema; the bridge
        uses a fresh SQLite read-only connection for each request instead.
        """
        if self.production_db is None:
            return None
        from .production_bridge import read_production_snapshot

        return read_production_snapshot(self.production_db)

    def authenticate(self, headers, query) -> Tuple[bool, str, Optional[str], str]:
        """Authenticate a request. Returns ``(ok, who, set_cookie, via)`` where
        ``who`` is a non-reversible prefix for the audit log, ``set_cookie`` is a
        Set-Cookie value to emit (only when a session was just minted), and
        ``via`` is one of ``bearer|cookie|query|none``.

        Order: ``Authorization: Bearer`` header, then a valid session cookie,
        then a ``?token=`` query (which mints a session). Every token comparison
        is constant-time."""
        authz = headers.get("Authorization", "") or ""
        if authz[:7].lower() == "bearer ":
            tok = authz[7:].strip()
            if constant_time_eq(tok, self.token):
                return True, token_prefix(tok), None, "bearer"
        sid = _cookie_value(headers.get("Cookie", ""), "hotato_session")
        if sid and self.sessions.valid(sid):
            return True, "sess:" + sid[:8] + "…", None, "cookie"
        qtok = (query.get("token") or [None])[0]
        if qtok and constant_time_eq(qtok, self.token):
            new_sid = self.sessions.mint()
            cookie = ("hotato_session=%s; HttpOnly; SameSite=Strict; Path=/"
                      % new_sid)
            return True, token_prefix(qtok), cookie, "query"
        return False, "-", None, "none"


def _cookie_value(header: str, name: str) -> Optional[str]:
    if not header:
        return None
    try:
        jar = http.cookies.SimpleCookie()
        jar.load(header)
    except http.cookies.CookieError:
        return None
    morsel = jar.get(name)
    return morsel.value if morsel else None


def _strip_token_qs(raw_query: str) -> str:
    """A query string with any ``token`` parameter removed, for the audit log and
    the post-login redirect (the bearer secret must never be recorded or kept in
    the address bar).

    The key is percent-DECODED before matching, exactly as authentication decodes
    it (``parse_qs``), so a bearer token smuggled under a percent-encoded key
    spelling (``%74oken=...``) is stripped rather than silently logged/echoed. The
    kept pairs are re-encoded canonically from their decoded form, so the output
    can never re-expose an encoded ``token`` key."""
    if not raw_query:
        return ""
    kept = [
        (key, value)
        for key, value in parse_qsl(raw_query, keep_blank_values=True)
        if key != "token"
    ]
    return urlencode(kept)


def _q(query, name) -> Optional[str]:
    v = (query.get(name) or [None])[0]
    v = v.strip() if isinstance(v, str) else v
    return v or None


def _json_bytes(obj) -> bytes:
    return (json.dumps(obj, indent=2, sort_keys=True, default=str) + "\n").encode("utf-8")


# =========================================================================
# request handler
# =========================================================================

class _Handler(BaseHTTPRequestHandler):
    # Minimal server banner: do not leak the Python version (threat-model hygiene)
    server_version = "hotato-serve"
    sys_version = ""

    # We keep our own append-only audit; silence the default stderr access log.
    def log_message(self, fmt, *args):  # noqa: A003 - matches base signature
        return

    def do_GET(self):  # noqa: N802 - http.server dispatch name
        ctx: ServeContext = self.server.context  # type: ignore[attr-defined]
        parsed = urlsplit(self.path)
        path = parsed.path
        query = parse_qs(parsed.query, keep_blank_values=True)
        fmt = ((query.get("format") or ["text"])[0] or "text").lower()
        remote = self.client_address[0] if self.client_address else ""
        clean_qs = _strip_token_qs(parsed.query)

        if path == "/favicon.ico":
            self._send(204, b"", "image/x-icon")
            return
        if path == "/robots.txt":
            self._send(200, b"User-agent: *\nDisallow: /\n", "text/plain; charset=utf-8")
            return

        ok, who, cookie, via = ctx.authenticate(self.headers, query)
        if not ok:
            # Courtesy landing page for the workspace HOME opened in a browser
            # without a token: a clean, on-brand page that explains how to get in,
            # served 200 so a new user never hits a bare 401 wall. It shares no
            # workspace data and reveals no token. EVERY other path (and any
            # ?format=json) stays token-gated with a 401 below. A client that
            # explicitly presented a bearer credential (right or wrong) is an API
            # auth attempt, so it gets an honest 401, never the landing.
            presented_bearer = (self.headers.get("Authorization", "") or ""
                                )[:7].lower() == "bearer "
            if path == "/" and fmt != "json" and not presented_bearer:
                ctx.audit.record(who="-", method=self.command, path=path,
                                 query=clean_qs, status=200, remote=remote)
                self._send(200, _render.render_landing_html(
                    workspace=ctx.workspace, host_display=self._display_host()
                ).encode("utf-8"), "text/html; charset=utf-8")
                return
            ctx.audit.record(who="-", method=self.command, path=path,
                             query=clean_qs, status=401, remote=remote)
            if fmt == "json":
                self._send(401, _json_bytes({
                    "error": "unauthenticated",
                    "hint": "send Authorization: Bearer <token> or open /?token=<token>",
                }), "application/json; charset=utf-8", {"WWW-Authenticate": "Bearer"})
            else:
                self._send(401, _render.render_401_html(
                    host_display=self._display_host()).encode("utf-8"),
                    "text/html; charset=utf-8", {"WWW-Authenticate": "Bearer"})
            return

        extra = {}
        if cookie:
            extra["Set-Cookie"] = cookie

        # A token-in-URL browser navigation: set the cookie and redirect to the
        # same path with the token stripped, so the secret leaves the address bar.
        if via == "query" and fmt != "json":
            location = urlunsplit(("", "", path, clean_qs, ""))
            ctx.audit.record(who=who, method=self.command, path=path,
                             query=clean_qs, status=302, remote=remote)
            self._send(302, b"", "text/html; charset=utf-8",
                       dict(extra, Location=location or "/"))
            return

        try:
            status, body, ctype, rextra = self._route(ctx, path, query, fmt)
        except Exception as exc:  # never leak a stack trace to the client
            status, body, ctype, rextra = self._server_error(exc, fmt)
        extra.update(rextra or {})
        ctx.audit.record(who=who, method=self.command, path=path,
                         query=clean_qs, status=status, remote=remote)
        self._send(status, body, ctype, extra)

    # -- routing ----------------------------------------------------------

    def _route(self, ctx, path, query, fmt):
        if path == "/":
            return self._view(ctx, "readiness", query, fmt)
        if path == "/scenarios":
            return self._view(ctx, "matrix", query, fmt)
        if path == "/clusters":
            return self._view(ctx, "clusters", query, fmt)
        if path in ("/health", "/production"):
            return self._view(ctx, "health", query, fmt)
        if path == "/records":
            return self._records_list(ctx, fmt)
        if path.startswith("/records/"):
            rid = unquote(path.split("/", 2)[2]) if path.count("/") >= 2 else ""
            return self._record_detail(ctx, rid, fmt)
        if path.startswith("/conversation/") or path.startswith("/conversations/"):
            cid = unquote(path.split("/", 2)[2]) if path.count("/") >= 2 else ""
            return self._inspector(ctx, cid, fmt)
        if path.startswith("/evidence/"):
            return self._evidence(ctx, path.split("/", 2)[2] if path.count("/") >= 2 else "")
        return self._not_found(ctx, fmt, "No such page: " + path)

    def _view(self, ctx, name, query, fmt):
        reg = ctx.open_registry()
        try:
            ws = ctx.workspace
            if name == "readiness":
                model = _data.build_release_readiness(reg, ws)
                title, active = "Release readiness", "/"
                html = _render.render_release_readiness(model)
            elif name == "matrix":
                model = _data.build_scenario_matrix(
                    reg, ws, agent=_q(query, "agent"), release=_q(query, "release"),
                    suite=_q(query, "suite"), status=_q(query, "status"))
                title, active = "Scenario matrix", "/scenarios"
                html = _render.render_scenario_matrix(model)
            elif name == "clusters":
                model = _data.build_failure_clusters(
                    reg, ws, dimension=_q(query, "dimension"), kind=_q(query, "kind"))
                title, active = "Failure clusters", "/clusters"
                html = _render.render_failure_clusters(model)
            else:  # health
                model = _data.build_production_health(
                    reg,
                    ws,
                    production_evidence=ctx.read_production_evidence(),
                )
                title, active = "Production health", "/health"
                html = _render.render_production_health(model)
        finally:
            reg.close()
        if fmt == "json":
            return 200, _json_bytes(model), "application/json; charset=utf-8", {}
        doc = _render.page(title, active, html, workspace=ctx.workspace)
        return 200, doc.encode("utf-8"), "text/html; charset=utf-8", {}

    def _inspector(self, ctx, conversation_id, fmt):
        reg = ctx.open_registry()
        try:
            store = ctx.open_store()
            model = _data.build_conversation_inspector(
                reg, ctx.workspace, conversation_id, store=store)
        finally:
            reg.close()
        if model is None:
            return self._not_found(
                ctx, fmt, "No conversation %r in workspace %r." % (
                    conversation_id, ctx.workspace),
                extra_json={"conversation_id": conversation_id})
        if fmt == "json":
            return 200, _json_bytes(model), "application/json; charset=utf-8", {}
        doc = _render.page("Conversation", "conversation",
                           _render.render_conversation_inspector(model),
                           workspace=ctx.workspace)
        return 200, doc.encode("utf-8"), "text/html; charset=utf-8", {}

    def _records_list(self, ctx, fmt):
        """The read-only Failure Record index. Reads ``<home>/records`` fresh
        (never writes it); an absent/empty directory renders an explicit empty
        state, never a fabricated record."""
        model = _data.build_records_list(ctx.home, ctx.workspace)
        if fmt == "json":
            return 200, _json_bytes(model), "application/json; charset=utf-8", {}
        doc = _render.page("Failure records", "/records",
                           _render.render_records_list(model),
                           workspace=ctx.workspace)
        return 200, doc.encode("utf-8"), "text/html; charset=utf-8", {}

    def _record_detail(self, ctx, record_id, fmt):
        """One Failure Record by its URL-safe id. The id is validated and
        contained to the records root (traversal/symlink escape refused) in the
        data layer; an unknown/unsafe/invalid id renders a 404. The JSON mirror
        returns the canonical ``hotato.failure-record.v1`` -- the exact object the
        HTML view renders, so the two can never diverge."""
        record = _data.build_record_detail(ctx.home, record_id)
        if record is None:
            return self._not_found(
                ctx, fmt, "No such failure record: %r" % record_id,
                extra_json={"record_id": record_id})
        if fmt == "json":
            return 200, _json_bytes(record), "application/json; charset=utf-8", {}
        doc = _render.page("Failure record", "/records",
                           _render.render_record_detail(record),
                           workspace=ctx.workspace)
        return 200, doc.encode("utf-8"), "text/html; charset=utf-8", {}

    def _evidence(self, ctx, digest):
        """Serve a raw evidence blob by digest (drill-to-evidence). The
        content-addressed store is a SHARED blob pool keyed by digest, so blob
        presence is NOT authority: the read is refused unless ``digest`` is
        reachable from a LIVE registry ROOT scoped to THIS workspace (named by a
        workspace-scoped reference-edge row, or declared by a rooted manifest's
        own artifacts). CAS lineage is never consulted -- a foreign-rooted or
        orphaned blob 404s. Authorized blobs are served ``text/plain`` with
        ``nosniff`` so a crafted blob can never execute in the browser."""
        if not _HEX64.match(digest or ""):
            return 400, _json_bytes({"error": "a 64-hex sha256 digest is required"}), \
                "application/json; charset=utf-8", {}
        store = ctx.open_store()
        authorized = False
        if store is not None:
            reg = ctx.open_registry()
            try:
                authorized = _data.evidence_digest_authorized(
                    reg, ctx.workspace, store, digest)
            finally:
                reg.close()
        # Refuse (404) both an unreachable digest and an authorized-but-absent
        # one, with one indistinguishable message: never confirm a blob the caller
        # is not entitled to, and never fabricate one it is.
        if store is None or not authorized or not store.has(digest):
            return 404, _json_bytes({"error": "no such artifact in this workspace",
                                     "digest": digest}), \
                "application/json; charset=utf-8", {}
        path = store.path_for(digest)
        try:
            size = os.path.getsize(path)
        except OSError:
            size = 0
        if size > _EVIDENCE_MAX_BYTES:
            return 413, _json_bytes({
                "error": "artifact too large to view here",
                "digest": digest, "bytes": size,
                "hint": "large/binary evidence is not streamed through the viewer"}), \
                "application/json; charset=utf-8", {}
        # Verified read at the serving boundary: re-hash the stored bytes and
        # refuse (500) if they do not match the requested digest. ``has()`` above
        # only proves a file EXISTS at the address; a blob poisoned out-of-band
        # (or bit-rotted) must never be streamed to a viewer as authentic
        # evidence. Fail closed rather than serve mismatched bytes.
        from ..fleet.store import BlobIntegrityError
        try:
            data = store.get_bytes(digest, verify=True)
        except BlobIntegrityError:
            return 500, _json_bytes({
                "error": "artifact failed integrity verification",
                "digest": digest}), \
                "application/json; charset=utf-8", {}
        return 200, data, "text/plain; charset=utf-8", {
            "Content-Disposition": "inline; filename=\"%s.txt\"" % digest[:16]}

    def _not_found(self, ctx, fmt, message, extra_json=None):
        if fmt == "json":
            payload = {"error": "not found", "message": message}
            if extra_json:
                payload.update(extra_json)
            return 404, _json_bytes(payload), "application/json; charset=utf-8", {}
        doc = _render.page("Not found", "not-found",
                           _render.render_404(message), workspace=ctx.workspace)
        return 404, doc.encode("utf-8"), "text/html; charset=utf-8", {}

    def _server_error(self, exc, fmt):
        msg = "internal error while rendering this view"
        if fmt == "json":
            return 500, _json_bytes({"error": msg}), "application/json; charset=utf-8", {}
        html = ('<h2 class="vh">Something went wrong</h2>'
                '<div class="notice">%s.</div>' % msg)
        try:
            doc = _render.page("Error", "error", html, workspace="?")
        except Exception:
            doc = "<!doctype html><h1>500</h1>"
        return 500, doc.encode("utf-8"), "text/html; charset=utf-8", {}

    # -- helpers ----------------------------------------------------------

    def _display_host(self) -> str:
        """A loopback-safe ``host:port`` to echo into the unauthenticated pages.
        Derived from the server's own bind address (never a client-supplied Host
        header), so a wildcard bind shows a usable ``127.0.0.1`` link."""
        ctx: ServeContext = self.server.context  # type: ignore[attr-defined]
        port = self.server.server_address[1]
        host = ctx.bind_host
        disp = "127.0.0.1" if host in ("", "0.0.0.0", "::") else host
        return "%s:%d" % (disp, port)

    # -- response ---------------------------------------------------------

    def _send(self, code, body, content_type, extra_headers=None):
        self.send_response(code)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("Cache-Control", "no-store")
        for k, v in (extra_headers or {}).items():
            self.send_header(k, v)
        self.end_headers()
        if self.command != "HEAD" and body:
            try:
                self.wfile.write(body)
            except (BrokenPipeError, ConnectionResetError):
                pass


class _WorkspaceServer(ThreadingHTTPServer):
    daemon_threads = True         # a request thread never blocks shutdown
    allow_reuse_address = True    # fast restart on the same port

    def __init__(self, address, handler, context: ServeContext):
        super().__init__(address, handler)
        self.context = context


def build_server(context: ServeContext, host: str, port: int) -> _WorkspaceServer:
    """Bind and return the threaded workspace server. Raises ``OSError`` (a
    HANDLED usage error -> exit 2) if the address/port is unavailable."""
    try:
        return _WorkspaceServer((host, port), _Handler, context)
    except OSError as exc:
        raise OSError("could not bind %s:%d (%s)" % (host, port, exc)) from exc


# =========================================================================
# CLI entry point
# =========================================================================

def run_serve(*, workspace: str, host: str = "127.0.0.1", port: int = 8321,
              registry: Optional[str] = None, token: Optional[str] = None,
              token_file: Optional[str] = None, open_browser: bool = True,
              production_db: Optional[str] = None,
              score_production: bool = False) -> int:
    """Start the workspace server and serve until interrupted. Returns 0 on a
    clean shutdown. Raises ``ValueError``/``OSError`` (HANDLED -> exit 2) on a bad
    registry, an unusable token, or an unavailable port.

    On start, unless ``open_browser`` is false (or the environment says not to;
    see :func:`_maybe_open_browser`), the default browser is pointed at the
    tokenised URL so the first thing a new user sees is a working workspace, not
    an auth wall."""
    home = os.path.abspath(os.path.expanduser(registry)) if registry else DEFAULT_HOME

    # Validate the registry opens (schema-version gate); read-only thereafter.
    Registry(home=home).close()

    resolved_production_db = None
    if production_db is not None:
        # Validate before binding or printing a success banner.  The snapshot is
        # discarded; requests reopen mode=ro so the page reflects current state.
        from .production_bridge import read_production_snapshot

        snapshot = read_production_snapshot(production_db)
        resolved_production_db = snapshot["source"]["path"]

    if score_production and resolved_production_db is None:
        raise ValueError(
            "--score-production needs --production-db (the evidence database "
            "the scoring worker reads)"
        )

    state_dir = os.path.join(home, "serve", _safe_dirname(workspace))
    os.makedirs(state_dir, mode=0o700, exist_ok=True)

    tok, source, generated = resolve_token(state_dir, token=token, token_file=token_file)
    audit = AuditLog(os.path.join(state_dir, "audit.jsonl"))
    ctx = ServeContext(
        home=home, workspace=workspace, store_root=_data.store_root_for(home),
        token=tok, state_dir=state_dir, audit=audit, sessions=SessionStore(),
        bind_host=host, production_db=resolved_production_db,
    )
    server = build_server(ctx, host, port)

    # Optional score-on-arrival: a background worker (never a second server;
    # the bind and auth above are unchanged) reads the evidence db mode=ro and
    # writes derived score records to the console sidecar beside it.
    score_worker = None
    score_store = None
    if score_production:
        from ..console_store import ConsoleStore
        from ..console_worker import ConsoleScoreWorker, default_console_path

        score_store = ConsoleStore(default_console_path(resolved_production_db))
        score_worker = ConsoleScoreWorker(resolved_production_db, score_store)

    display_host = "127.0.0.1" if host in ("", "0.0.0.0", "::") else host
    # The one line that carries the secret: the tokenised URL a browser can open
    # directly. It is printed once (below) and never written to the audit log.
    url = "http://%s:%d/?token=%s" % (display_host, port, quote(tok, safe=""))

    _print_banner(ctx, host, port, source=source, generated=generated,
                  state_dir=state_dir, url=url, display_host=display_host)
    if score_store is not None:
        print("  scoring:   %s   (derived sidecar, rebuildable from the "
              "evidence db with --rebuild-scores; completed sessions are "
              "scored one at a time in the background)"
              % score_store.path, file=sys.stderr)

    # The listening socket is already bound; a browser opened now queues onto the
    # accept backlog and is served the moment `serve_forever` runs.
    if _maybe_open_browser(url, enabled=open_browser):
        print("  browser:   opening http://%s:%d in your default browser."
              % (display_host, port), file=sys.stderr)  # no token on this line
    else:
        print("  browser:   auto-open off. Open the link above to get in.",
              file=sys.stderr)
    print("  stop:      press Ctrl-C", file=sys.stderr)
    sys.stderr.flush()

    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nhotato serve: shutting down.", file=sys.stderr)
    finally:
        server.server_close()
        if score_worker is not None:
            try:
                score_worker.close()
            finally:
                score_store.close()
    return 0


def _maybe_open_browser(url: str, *, enabled: bool) -> bool:
    """Dispatch the default browser to ``url`` once, returning ``True`` if an open
    was started. It is skipped (returns ``False``) when disabled by ``--no-open``,
    when stdout is not a TTY (so CI and piped/headless runs never spawn one), when
    ``$CI`` or ``$HOTATO_NO_BROWSER`` is set, or when no usable browser is
    registered. The open runs on a daemon thread so a browser that runs in the
    foreground can never block the serve loop or delay shutdown."""
    if not enabled:
        return False
    if os.environ.get("HOTATO_NO_BROWSER"):
        return False
    if os.environ.get("CI"):
        return False
    if not (hasattr(sys.stdout, "isatty") and sys.stdout.isatty()):
        return False
    try:
        webbrowser.get()  # raises webbrowser.Error when nothing is registered
    except Exception:
        return False

    def _open() -> None:
        try:
            webbrowser.open(url, new=2, autoraise=True)
        except Exception:
            pass

    threading.Thread(target=_open, name="hotato-serve-open", daemon=True).start()
    return True


def _print_banner(ctx, host, port, *, source, generated, state_dir, url,
                  display_host):
    base = "http://%s:%d" % (display_host, port)
    out = sys.stderr
    bar = "  " + "-" * 60
    print("", file=out)
    print("  hotato workspace  ·  %r  ·  self-hosted, read-only" % ctx.workspace,
          file=out)
    print(bar, file=out)
    print("  Open this in your browser:", file=out)
    print("    %s" % url, file=out)
    print(bar, file=out)
    print("  listening: %s" % base, file=out)
    print("  registry:  %s" % ctx.home, file=out)
    if ctx.production_db:
        print("  production evidence: %s   (separate SQLite source, mode=ro; "
              "not imported into fleet)" % ctx.production_db, file=out)
    if host not in _LOOPBACK:
        print("  WARNING:   bound to %s (not loopback). This workspace is "
              "reachable from your network. Token auth still applies; stop the "
              "server if this was unintended." % host, file=out)
    if generated:
        print("  token:     %s   (generated, stored 0600 at %s)"
              % (ctx.token, os.path.join(state_dir, "token")), file=out)
    else:
        print("  token:     %s   (%s)" % (ctx.token, source), file=out)
    print("  audit log: %s   (append-only)"
          % os.path.join(state_dir, "audit.jsonl"), file=out)
    print("  read-only: the server issues only SELECTs; reviews and labels stay "
          "CLI-driven. No telemetry, no external calls.", file=out)
    out.flush()
