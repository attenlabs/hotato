"""The REAL state adapters (Authority 2): ``HttpStateAdapter`` against a live
local ``http.server``, ``SqlStateAdapter`` against a real SQLite file, and the
``load_state_adapter`` config seam.

These exercise the SHIPPED network / DB paths -- not doubles: a stdlib HTTP
server is spun up in-process and a real SQLite DB is written to disk, so the
adapters make genuine requests / queries. What stays a test-only detail is the
SERVER (a customer's real system of record cannot be hit deterministically in
CI); the adapter code under test is the production code.

Pins the honesty + safety properties that are the whole point:

* a reachable system of record with no such record -> ``None`` -> a grounded
  FAIL; a system of record we could NOT reach / read (network error, timeout,
  5xx, non-JSON) -> ``StateAdapterError`` -> INCONCLUSIVE, never a fabricated
  verdict, never a crash;
* HTTPS by default -- a plain-http base URL is refused without ``allow_http``;
* SQL is PARAMETERIZED (an injection string is bound as data, never executed)
  and READ-ONLY (a non-SELECT mapped query is rejected);
* the config loader REFUSES a network adapter (http / non-local sql) unless the
  config opts into egress.
"""

from __future__ import annotations

import json
import sqlite3
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from hotato import assert_ as A
from hotato.state_adapter import (
    HttpStateAdapter,
    MockStateAdapter,
    SqlStateAdapter,
    StateAdapterError,
    load_state_adapter,
)


# ===========================================================================
# A real local HTTP system of record (stdlib http.server)
# ===========================================================================

def _make_handler(seen):
    class _Handler(BaseHTTPRequestHandler):
        def log_message(self, *a):  # keep the test output quiet
            pass

        def _record(self, body=b""):
            seen["auth"] = self.headers.get("Authorization")
            seen["method"] = self.command
            seen["path"] = self.path
            seen["body"] = body

        def _json(self, code, obj):
            payload = json.dumps(obj).encode("utf-8")
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

        def do_GET(self):  # noqa: N802 (stdlib name)
            self._record()
            if self.path.startswith("/orders/A1"):
                self._json(200, {"data": {"order": {
                    "id": "A1", "status": "refunded", "amount": 50}}})
            elif self.path.startswith("/orders/EMPTY"):
                # 200, well-formed, but the pointer target is absent -> no record
                self._json(200, {"data": {}})
            elif self.path.startswith("/slow/"):
                time.sleep(2.0)  # force a client read timeout
                self._json(200, {"data": {"order": {"id": "S"}}})
            elif self.path.startswith("/boom/"):
                self._json(500, {"error": "internal"})
            elif self.path.startswith("/garbage/"):
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(b"<html>not json</html>")
            else:
                self._json(404, {"error": "not found"})

        def do_POST(self):  # noqa: N802
            length = int(self.headers.get("Content-Length") or 0)
            self._record(self.rfile.read(length) if length else b"")
            self._json(200, {"result": {"id": "P1", "state": "done"}})

    return _Handler


@pytest.fixture
def http_server():
    """Yield ``(base_url, seen, stop)`` for a live local HTTP server; ``seen``
    captures the last request, ``stop`` fully releases the port (for the
    server-down case)."""
    seen: dict = {}
    srv = ThreadingHTTPServer(("127.0.0.1", 0), _make_handler(seen))
    port = srv.server_address[1]
    thread = threading.Thread(target=srv.serve_forever, daemon=True)
    thread.start()

    stopped = {"done": False}

    def stop():
        if not stopped["done"]:
            srv.shutdown()
            srv.server_close()
            stopped["done"] = True

    try:
        yield f"http://127.0.0.1:{port}", seen, stop
    finally:
        stop()


_ORDER_RES = {
    "orders": {"path_template": "/orders/{id}", "response_pointer": "data/order"},
    "empty": {"path_template": "/orders/EMPTY", "response_pointer": "data/order"},
    "slow": {"path_template": "/slow/{id}", "response_pointer": "data/order"},
    "boom": {"path_template": "/boom/{id}", "response_pointer": "data/order"},
    "garbage": {"path_template": "/garbage/{id}", "response_pointer": "data/order"},
    "book": {"path_template": "/book", "method": "POST",
             "params_map": {"who": "customer"}, "response_pointer": "result"},
}


def _http(base_url, **kw):
    kw.setdefault("allow_http", True)  # the test server is plain http on loopback
    return HttpStateAdapter(base_url=base_url, resources=_ORDER_RES, **kw)


# --- HttpStateAdapter: success + response_pointer extraction ----------------

def test_http_success_extracts_record_via_response_pointer(http_server):
    base, _seen, _stop = http_server
    ad = _http(base)
    rec = ad.query("orders", id="A1")
    assert rec == {"id": "A1", "status": "refunded", "amount": 50}
    assert ad.last_error is None


def test_http_response_pointer_absent_target_is_a_grounded_none(http_server):
    base, _seen, _stop = http_server
    ad = _http(base)
    assert ad.query("empty") is None      # reachable + parsed, no such record
    assert ad.last_error is None          # not an error -> grounded FAIL upstream


# --- HttpStateAdapter: 404 -> None + last_error -----------------------------

def test_http_404_returns_none_with_structured_last_error(http_server):
    base, _seen, _stop = http_server
    ad = _http(base)
    assert ad.query("orders", id="NOPE") is None   # addressed record absent
    assert ad.last_error is not None
    assert ad.last_error["kind"] == "http_status"
    assert ad.last_error["status"] == 404


# --- HttpStateAdapter: timeout -> None-of-verdict via raise + last_error -----

def test_http_timeout_raises_stateadaptererror_with_last_error(http_server):
    base, _seen, _stop = http_server
    ad = _http(base, timeout=0.3)
    with pytest.raises(StateAdapterError):
        ad.query("slow", id="X")
    assert ad.last_error is not None
    assert ad.last_error["kind"] == "timeout"


def test_http_5xx_raises_so_the_state_kind_is_inconclusive(http_server):
    base, _seen, _stop = http_server
    ad = _http(base)
    with pytest.raises(StateAdapterError):
        ad.query("boom", id="X")
    assert ad.last_error["kind"] == "http_status"
    assert ad.last_error["status"] == 500


def test_http_non_json_body_raises(http_server):
    base, _seen, _stop = http_server
    ad = _http(base)
    with pytest.raises(StateAdapterError):
        ad.query("garbage", id="X")
    assert ad.last_error["kind"] == "bad_response"


# --- HttpStateAdapter: auth header sent from an env var ----------------------

def test_http_bearer_auth_header_is_sent_from_env_var(http_server, monkeypatch):
    base, seen, _stop = http_server
    monkeypatch.setenv("HOTATO_TEST_BEARER", "sekret-42")
    ad = _http(base, auth={"type": "bearer", "token_env": "HOTATO_TEST_BEARER"})
    ad.query("orders", id="A1")
    assert seen["auth"] == "Bearer sekret-42"


def test_http_header_auth_mixes_env_secret_and_literal(http_server, monkeypatch):
    base, seen, _stop = http_server
    monkeypatch.setenv("HOTATO_TEST_APIKEY", "abc123")
    ad = _http(base, auth={"type": "header", "headers": {
        "X-Api-Key": {"env": "HOTATO_TEST_APIKEY"}}})
    ad.query("orders", id="A1")
    # the request went out with the key header (server echoes the whole set via
    # seen); a direct assertion on the outgoing header:
    assert ad._auth_headers == {"X-Api-Key": "abc123"}


def test_http_missing_credential_env_fails_fast(monkeypatch):
    monkeypatch.delenv("HOTATO_ABSENT_TOKEN", raising=False)
    with pytest.raises(ValueError, match="HOTATO_ABSENT_TOKEN"):
        HttpStateAdapter(
            base_url="https://api.example.com", resources=_ORDER_RES,
            auth={"type": "bearer", "token_env": "HOTATO_ABSENT_TOKEN"},
        )


# --- HttpStateAdapter: https-default enforcement ----------------------------

def test_http_plain_http_refused_without_allow_http():
    with pytest.raises(ValueError, match="cleartext|plain-http|allow_http"):
        HttpStateAdapter(base_url="http://api.example.com", resources=_ORDER_RES)


def test_https_base_url_needs_no_allow_http():
    # constructs fine (no request made) -- https is the default-safe path
    ad = HttpStateAdapter(base_url="https://api.example.com", resources=_ORDER_RES)
    assert ad._base_url == "https://api.example.com"


# --- HttpStateAdapter: POST body path ---------------------------------------

def test_http_post_sends_mapped_body_and_reads_pointer(http_server):
    base, seen, _stop = http_server
    ad = _http(base)
    rec = ad.query("book", who="alice")
    assert rec == {"id": "P1", "state": "done"}
    assert seen["method"] == "POST"
    assert json.loads(seen["body"]) == {"customer": "alice"}  # params_map applied


# --- HttpStateAdapter: state_change 'before' has no live snapshot ------------

def test_http_before_snapshot_is_absent_not_fabricated(http_server):
    base, _seen, _stop = http_server
    ad = _http(base)
    assert ad.query("orders", id="A1", when="before") is None
    assert ad.query("orders", id="A1", when="after") == {
        "id": "A1", "status": "refunded", "amount": 50}


# ===========================================================================
# SqlStateAdapter against a real SQLite file
# ===========================================================================

@pytest.fixture
def refunds_db(tmp_path):
    path = tmp_path / "records.sqlite3"
    conn = sqlite3.connect(str(path))
    conn.execute("CREATE TABLE refunds (order_id TEXT, status TEXT, amount INTEGER)")
    conn.executemany(
        "INSERT INTO refunds VALUES (?, ?, ?)",
        [("O1", "issued", 50), ("O2", "pending", 0)],
    )
    conn.commit()
    conn.close()
    return str(path)


_SQL_RES = {
    "refund": {
        "query": "SELECT order_id, status, amount FROM refunds "
                 "WHERE order_id = ? AND status = ?",
        "params_order": ["order_id", "status"],
    },
}


def test_sql_success_returns_first_row_as_dict(refunds_db):
    ad = SqlStateAdapter(sqlite_path=refunds_db, resources=_SQL_RES)
    assert ad.query("refund", order_id="O1", status="issued") == {
        "order_id": "O1", "status": "issued", "amount": 50}
    ad.close()


def test_sql_missing_row_returns_none(refunds_db):
    ad = SqlStateAdapter(sqlite_path=refunds_db, resources=_SQL_RES)
    assert ad.query("refund", order_id="O1", status="pending") is None
    ad.close()


def test_sql_injection_string_is_bound_as_data_not_executed(refunds_db):
    """The classic proof: a filter value carrying SQL is a LITERAL, not code.
    The malicious string matches no row, and the table it tries to drop is
    still there afterward."""
    ad = SqlStateAdapter(sqlite_path=refunds_db, resources=_SQL_RES)
    evil = "O1'; DROP TABLE refunds; --"
    assert ad.query("refund", order_id=evil, status="issued") is None
    ad.close()
    # the table survived: the injection was never executed
    conn = sqlite3.connect(refunds_db)
    n = conn.execute("SELECT COUNT(*) FROM refunds").fetchone()[0]
    conn.close()
    assert n == 2


def test_sql_non_select_query_is_rejected_at_construction(refunds_db):
    for bad in ("DELETE FROM refunds",
                "UPDATE refunds SET status = 'x'",
                "SELECT 1; DROP TABLE refunds",
                "DROP TABLE refunds"):
        with pytest.raises(ValueError, match="read-only|multi-statement"):
            SqlStateAdapter(
                sqlite_path=refunds_db,
                resources={"r": {"query": bad, "params_order": []}},
            )


def test_sql_with_select_cte_is_allowed(refunds_db):
    ad = SqlStateAdapter(
        sqlite_path=refunds_db,
        resources={"r": {
            "query": "WITH issued AS (SELECT * FROM refunds WHERE status = ?) "
                     "SELECT order_id, amount FROM issued",
            "params_order": ["status"]}},
    )
    assert ad.query("r", status="issued") == {"order_id": "O1", "amount": 50}
    ad.close()


def test_sql_accepts_a_caller_supplied_connection(refunds_db):
    conn = sqlite3.connect(refunds_db)
    ad = SqlStateAdapter(connection=conn, resources=_SQL_RES)
    assert ad.query("refund", order_id="O2", status="pending") == {
        "order_id": "O2", "status": "pending", "amount": 0}
    ad.close()               # caller owns the connection -> not closed here
    assert conn.execute("SELECT 1").fetchone() == (1,)  # still usable
    conn.close()


def test_sql_missing_filter_named_in_params_order_is_inconclusive(refunds_db):
    ad = SqlStateAdapter(sqlite_path=refunds_db, resources=_SQL_RES)
    with pytest.raises(StateAdapterError):
        ad.query("refund", order_id="O1")   # 'status' not supplied
    ad.close()


def test_sql_unknown_driver_raises_clean_error():
    with pytest.raises(ValueError, match="not installed"):
        SqlStateAdapter(
            dsn="postgresql://localhost/x",
            driver="hotato_no_such_driver_xyz",
            resources=_SQL_RES,
        )


def test_sql_needs_exactly_one_connection_source(refunds_db):
    with pytest.raises(ValueError, match="exactly one"):
        SqlStateAdapter(resources=_SQL_RES)  # none
    with pytest.raises(ValueError, match="exactly one"):
        SqlStateAdapter(sqlite_path=refunds_db, dsn="x", resources=_SQL_RES)  # two


# ===========================================================================
# load_state_adapter: dispatch + egress refusal
# ===========================================================================

def _write(tmp_path, name, obj):
    p = tmp_path / name
    p.write_text(json.dumps(obj), encoding="utf-8")
    return str(p)


def test_load_dispatches_mock_from_inline_data(tmp_path):
    cfg = _write(tmp_path, "s.json", {
        "adapter": "mock", "data": {"orders": [{"id": "A1", "status": "ok"}]}})
    ad = load_state_adapter(cfg)
    assert isinstance(ad, MockStateAdapter)
    assert ad.query("orders", id="A1") == {"id": "A1", "status": "ok"}


def test_load_bare_sandbox_without_adapter_key_is_a_mock(tmp_path):
    cfg = _write(tmp_path, "s.json", {"orders": [{"id": "A1", "status": "ok"}]})
    ad = load_state_adapter(cfg)
    assert isinstance(ad, MockStateAdapter)
    assert ad.query("orders", id="A1") == {"id": "A1", "status": "ok"}


def test_load_mock_from_json_file_relative_to_config(tmp_path):
    (tmp_path / "sandbox.json").write_text(
        json.dumps({"orders": [{"id": "A1", "status": "ok"}]}), encoding="utf-8")
    cfg = _write(tmp_path, "s.json", {"adapter": "mock", "json_file": "sandbox.json"})
    ad = load_state_adapter(cfg)
    assert ad.query("orders", id="A1") == {"id": "A1", "status": "ok"}


def test_load_dispatches_http_with_opt_in(tmp_path):
    cfg = _write(tmp_path, "s.json", {
        "adapter": "http", "egress_opt_in": True,
        "base_url": "https://api.example.com",
        "resources": {"orders": {"path_template": "/orders/{id}"}}})
    ad = load_state_adapter(cfg)
    assert isinstance(ad, HttpStateAdapter)


def test_load_refuses_http_without_egress_opt_in(tmp_path):
    cfg = _write(tmp_path, "s.json", {
        "adapter": "http", "base_url": "https://api.example.com",
        "resources": {"orders": {"path_template": "/orders/{id}"}}})
    with pytest.raises(ValueError, match="egress_opt_in"):
        load_state_adapter(cfg)


def test_load_dispatches_local_sqlite_sql_without_opt_in(tmp_path, refunds_db):
    cfg = _write(tmp_path, "s.json", {
        "adapter": "sql", "sqlite_path": refunds_db, "resources": _SQL_RES})
    ad = load_state_adapter(cfg)
    assert isinstance(ad, SqlStateAdapter)
    assert ad.query("refund", order_id="O1", status="issued")["amount"] == 50
    ad.close()


def test_load_refuses_sql_dsn_without_egress_opt_in(tmp_path):
    cfg = _write(tmp_path, "s.json", {
        "adapter": "sql", "dsn": "postgresql://localhost/x", "driver": "psycopg2",
        "resources": _SQL_RES})
    with pytest.raises(ValueError, match="egress_opt_in"):
        load_state_adapter(cfg)


def test_load_unknown_adapter_is_a_clean_error(tmp_path):
    cfg = _write(tmp_path, "s.json", {"adapter": "carrier-pigeon"})
    with pytest.raises(ValueError, match="unknown state adapter"):
        load_state_adapter(cfg)


# ===========================================================================
# End-to-end: a `state` assertion through build_context(HttpStateAdapter)
# ===========================================================================

def _state_assertion():
    return {"id": "refund-recorded", "kind": "state", "resource": "orders",
            "filters": {"id": "A1"}, "expect": {"status": "refunded"},
            "dimension": "outcome"}


def test_e2e_state_assertion_passes_against_live_server(http_server):
    base, _seen, _stop = http_server
    ctx = A.build_context(state_adapter=_http(base))
    r = A.evaluate_assertion(_state_assertion(), ctx)
    assert r["status"] == "PASS"
    assert r["deterministic"] is True


def test_e2e_state_assertion_fails_when_field_mismatches(http_server):
    base, _seen, _stop = http_server
    a = _state_assertion()
    a["expect"] = {"status": "denied"}      # server says 'refunded'
    r = A.evaluate_assertion(a, A.build_context(state_adapter=_http(base)))
    assert r["status"] == "FAIL"


def test_e2e_state_assertion_inconclusive_when_server_is_down(http_server):
    base, _seen, stop = http_server
    ad = _http(base, timeout=0.5)
    # sanity: PASS while up
    assert A.evaluate_assertion(
        _state_assertion(), A.build_context(state_adapter=ad))["status"] == "PASS"
    stop()  # take the system of record offline
    r = A.evaluate_assertion(_state_assertion(), A.build_context(state_adapter=ad))
    assert r["status"] == "INCONCLUSIVE"
    assert r.get("reason")  # a real reason, not a fabricated verdict
