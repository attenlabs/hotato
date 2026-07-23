"""``/calls`` -- the console's daily surface over the derived score sidecar.

Starts the real threaded server over an evidence database built through
production.py's own ingest API plus a sidecar built by the deterministic
rebuild, then proves:

* the feed renders SCORED, NOT_SCORABLE, and ERROR rows distinctly -- a
  refused or errored session is never hidden and never reads as OK (I2);
* filters (session state, scorability, time window) are query-param driven and
  a malformed filter/cursor is a 400, never a silently dropped filter;
* pagination is keyset from the first page (no OFFSET anywhere on the path)
  and pages are disjoint and complete (I3);
* the per-call view renders per-dimension observations (never blended), the
  ranked candidate moments with measured magnitudes, the timing waterfall with
  derived-vs-reported authority rendered rather than flattened (I5), scorer
  version + config hash, evidence-lane completeness, and the local audio path
  as recorded -- with no invented file-serving route;
* the JSON mirror carries an ETag and a matching ``If-None-Match`` gets a
  body-less 304 (R7), while changed evidence changes the ETag;
* ``hotato console`` is serve + production-db + score-on-arrival in one
  command, landing on ``/calls``;
* every ``/calls`` path stays token-gated, and no view grows a blended score.
"""
from __future__ import annotations

import json
import os
import shutil
import threading
import urllib.error
import urllib.request
from importlib import resources
from urllib.parse import quote

import pytest

from hotato import __version__, cli
from hotato import console_worker as console_worker_mod
from hotato.console_store import ConsoleStore
from hotato.console_worker import default_console_path, rebuild_sidecar
from hotato.fleet.registry import Registry
from hotato.production import ProductionStore
from hotato.serve import build_server
from hotato.serve.app import ServeContext
from hotato.serve.security import AuditLog, SessionStore

_TOKEN = "tok_calls_TEST_0123456789_xyz"


# =========================================================================
# fixture: an evidence db + sidecar with one row per score state
# =========================================================================

def _stereo_fixture() -> str:
    return str(
        resources.files("hotato").joinpath(
            "data", "audio", "01-hard-interruption.example.wav"
        )
    )


def _event(event_id, event_type, *, subject, time_value, data=None,
           authority="adapter_reported"):
    return {
        "specversion": "1.0",
        "id": event_id,
        "source": "console-fixture",
        "type": event_type,
        "subject": subject,
        "time": time_value,
        "data": {} if data is None else data,
        "authority": {
            "kind": authority,
            "eligible_for_execution_claim": authority
            in ("measured", "signed_attestation"),
        },
    }


def _ingest_call(store, subject, *, audio_path=None, with_media_event=True):
    """One quiesced call session through the production plane's own ingest API:
    lifecycle + audio asset + one model hop + one timed turn."""
    events = [
        _event(f"{subject}-start", "session.started", subject=subject,
               time_value="2026-07-17T12:00:00Z"),
        _event(f"{subject}-turn-a", "turn.started", subject=subject,
               time_value="2026-07-17T12:00:01Z"),
        _event(f"{subject}-model", "model.operation", subject=subject,
               time_value="2026-07-17T12:00:02Z",
               data={"latency_ms": 210.0, "availability": "available"}),
        _event(f"{subject}-turn-b", "turn.ended", subject=subject,
               time_value="2026-07-17T12:00:03.500Z",
               data={"yield_latency_ms": 480.0}),
        _event(f"{subject}-end", "session.ended", subject=subject,
               time_value="2026-07-17T12:00:06Z"),
    ]
    if with_media_event:
        data = {"availability": "available", "channels": 2}
        if audio_path is not None:
            data["path"] = audio_path
        events.insert(1, _event(
            f"{subject}-audio", "media.asset.available", subject=subject,
            time_value="2026-07-17T12:00:00.500Z", data=data,
            authority="measured"))
    for event in events:
        store.ingest(event, redact_payloads=False)


def _seed_evidence(tmp_path, monkeypatch) -> str:
    """Three sessions at three distinct arrival stamps -- one lands SCORED,
    one NOT_SCORABLE (no audio lane), one ERROR (a scorer crash pinned to one
    recording during the rebuild) -- then the sidecar is built by the
    deterministic rebuild. Returns the evidence db path."""
    db_path = str(tmp_path / "production.sqlite3")
    crashing = str(tmp_path / "crash.wav")
    shutil.copy(_stereo_fixture(), crashing)

    clock = [1000.0]
    evidence = ProductionStore(db_path, clock=lambda: clock[0])
    _ingest_call(evidence, "call-scored", audio_path=_stereo_fixture())
    clock[0] = 2000.0
    _ingest_call(evidence, "call-noaudio", with_media_event=False)
    clock[0] = 3000.0
    _ingest_call(evidence, "call-crash", audio_path=crashing)
    evidence.close()

    real_scan = console_worker_mod.scan_recording

    def crash_on_marked(path, **kwargs):
        if path == crashing:
            raise RuntimeError("boom")
        return real_scan(path, **kwargs)

    monkeypatch.setattr(console_worker_mod, "scan_recording", crash_on_marked)
    store = ConsoleStore(default_console_path(db_path))
    try:
        summary = rebuild_sidecar(db_path, store)
    finally:
        store.close()
    monkeypatch.setattr(console_worker_mod, "scan_recording", real_scan)
    assert summary["scored"] == 1
    assert summary["not_scorable"] == 1
    assert summary["errors"] == 1
    return db_path


class _Live:
    def __init__(self, base, token, home, production_db, server, thread):
        self.base = base
        self.token = token
        self.home = home
        self.production_db = production_db
        self._server = server
        self._thread = thread

    def stop(self):
        self._server.shutdown()
        self._thread.join(timeout=5)
        self._server.server_close()


def _start_server(home: str, production_db):
    os.makedirs(home, exist_ok=True)
    Registry(home=home).close()
    state_dir = os.path.join(home, "serve", "default")
    os.makedirs(state_dir, exist_ok=True)
    ctx = ServeContext(
        home=home, workspace="default",
        store_root=os.path.join(home, "artifacts"), token=_TOKEN,
        state_dir=state_dir,
        audit=AuditLog(os.path.join(state_dir, "audit.jsonl")),
        sessions=SessionStore(), bind_host="127.0.0.1",
        production_db=production_db)
    server = build_server(ctx, "127.0.0.1", 0)
    port = server.server_address[1]
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return _Live("http://127.0.0.1:%d" % port, _TOKEN, home, production_db,
                 server, thread)


@pytest.fixture()
def live(tmp_path, monkeypatch):
    production_db = _seed_evidence(tmp_path, monkeypatch)
    server = _start_server(str(tmp_path / "fleet"), production_db)
    try:
        yield server
    finally:
        server.stop()


def _req(base, path, *, token=None, headers=None):
    req = urllib.request.Request(base + path)
    if token is not None:
        req.add_header("Authorization", "Bearer " + token)
    for key, value in (headers or {}).items():
        req.add_header(key, value)
    try:
        resp = urllib.request.urlopen(req, timeout=5)
        return resp.getcode(), resp.read().decode("utf-8"), dict(resp.headers)
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read().decode("utf-8"), dict(exc.headers)


def _rows(base, path, token):
    code, body, _h = _req(base, path, token=token)
    assert code == 200, path
    return json.loads(body)


# =========================================================================
# auth
# =========================================================================

def test_calls_routes_require_token(live):
    for path in ["/calls", "/calls?format=json", "/calls/call-scored",
                 "/calls/call-scored?format=json"]:
        code, body, headers = _req(live.base, path)
        assert code == 401, path
        assert "bearer" in headers.get("WWW-Authenticate", "").lower(), path
        assert "call-scored" not in body or path.startswith("/calls/call-"), path
        assert "NOT_SCORABLE" not in body, path


# =========================================================================
# the feed: three first-class states, never hidden, never OK
# =========================================================================

def test_feed_renders_all_three_score_states_distinctly(live):
    code, body, _h = _req(live.base, "/calls", token=live.token)
    assert code == 200
    for subject in ("call-scored", "call-noaudio", "call-crash"):
        assert subject in body
    assert "SCORED" in body
    assert "NOT_SCORABLE" in body
    assert "ERROR" in body
    # the refusal and the crash each carry their reason on the row
    assert "participant_audio evidence lane is missing" in body
    assert "RuntimeError: boom" in body
    # the measured failure-reason sentence for the scored call
    assert "the caller took the floor" in body

    m = _rows(live.base, "/calls?format=json", live.token)
    states = {r["subject"]: r["score_state"] for r in m["rows"]}
    assert states == {"call-scored": "SCORED", "call-noaudio": "NOT_SCORABLE",
                      "call-crash": "ERROR"}
    # a NOT_SCORABLE row keeps its reason in the mirror too
    by_subject = {r["subject"]: r for r in m["rows"]}
    assert "participant_audio" in by_subject["call-noaudio"]["reason"]
    # newest arrival first
    assert [r["subject"] for r in m["rows"]] == [
        "call-crash", "call-noaudio", "call-scored"]


def test_feed_rows_carry_provenance_labeled_timing_and_evidence(live):
    m = _rows(live.base, "/calls?format=json", live.token)
    scored = next(r for r in m["rows"] if r["subject"] == "call-scored")
    # evidence-clock anchor and arrival stamp each carry their clock label (I5)
    assert scored["evidence_time_authority"] == "derived:event_timestamps"
    assert scored["received_authority"] == "received:arrival_clock"
    assert scored["received_at"] == 1000.0
    # duration derives from evidence event timestamps
    assert scored["duration_ms"] == 6000.0
    assert scored["duration_authority"] == "derived:event_timestamps"
    # per-call hop percentiles state the declared authorities behind them
    assert scored["latency"]["hop_count"] == 2
    assert scored["latency"]["p50_ms"] == 210.0
    assert scored["latency"]["p95_ms"] == 2500.0
    assert set(scored["latency"]["authorities"]) == {
        "adapter_reported", "derived:event_timestamps"}
    # worst dimension is a per-call worst case, not a blend
    assert scored["worst"]["dimension"] == "overlap_while_agent_talking"
    assert scored["worst"]["worst_sec"] > 0
    # evidence completeness counts required lanes and names the missing ones
    assert scored["evidence"]["status"] == "incomplete"
    assert "transcript" in scored["evidence"]["missing"]


# =========================================================================
# filters + keyset pagination (I3: no OFFSET walks)
# =========================================================================

def test_feed_filters_by_scorability_state_and_window(live):
    m = _rows(live.base, "/calls?scorability=NOT_SCORABLE&format=json",
              live.token)
    assert [r["subject"] for r in m["rows"]] == ["call-noaudio"]
    assert m["trends"]["volume"] == 1

    m = _rows(live.base, "/calls?state=QUIESCENT&format=json", live.token)
    assert m["row_count"] == 3  # every fixture session quiesced

    m = _rows(live.base, "/calls?since=2500&format=json", live.token)
    assert [r["subject"] for r in m["rows"]] == ["call-crash"]
    m = _rows(live.base, "/calls?until=1500&format=json", live.token)
    assert [r["subject"] for r in m["rows"]] == ["call-scored"]


def test_feed_bad_filter_or_cursor_is_400_never_ignored(live):
    for path in ["/calls?since=yesterday", "/calls?cursor=zzz",
                 "/calls?limit=0", "/calls?limit=snack"]:
        code, _body, _h = _req(live.base, path, token=live.token)
        assert code == 400, path
        code, body, _h = _req(live.base, path + "&format=json",
                              token=live.token)
        assert code == 400, path
        assert json.loads(body)["error"] == "bad request"


def test_feed_keyset_pagination_is_disjoint_and_complete(live):
    seen = []
    path = "/calls?limit=1&format=json"
    for _ in range(4):
        m = _rows(live.base, path, live.token)
        seen.extend(r["subject"] for r in m["rows"])
        cursor = m["page"]["next_cursor"]
        if cursor is None:
            break
        path = "/calls?limit=1&cursor=%s&format=json" % quote(cursor, safe="")
    assert seen == ["call-crash", "call-noaudio", "call-scored"]
    assert m["page"]["next_cursor"] is None


def test_feed_sql_uses_no_offset(live, monkeypatch):
    # I3 at the statement level: walking every page issues no OFFSET; each
    # page is one LIMIT-bounded keyset query.
    from hotato.serve import data as data_mod

    statements = []
    real_open = data_mod._open_console_ro

    def traced_open(console_path, production_path):
        db = real_open(console_path, production_path)
        db.set_trace_callback(statements.append)
        return db

    monkeypatch.setattr(data_mod, "_open_console_ro", traced_open)
    path = "/calls?limit=1&format=json"
    while True:
        m = _rows(live.base, path, live.token)
        cursor = m["page"]["next_cursor"]
        if cursor is None:
            break
        path = "/calls?limit=1&cursor=%s&format=json" % quote(cursor, safe="")
    assert statements, "the feed reads through the traced connection"
    assert all("OFFSET" not in sql.upper() for sql in statements)
    assert any("LIMIT" in sql.upper() for sql in statements)


# =========================================================================
# trends strip (R8)
# =========================================================================

def test_trends_strip_over_the_filtered_window(live):
    m = _rows(live.base, "/calls?format=json", live.token)
    trends = m["trends"]
    assert trends["volume"] == 3
    assert trends["states"] == {"SCORED": 1, "NOT_SCORABLE": 1, "ERROR": 1}
    assert trends["with_candidate_moments"] == 1
    assert trends["candidate_share"] == 1.0
    # per-kind hop percentiles, never pooled across kinds, each stating the
    # declared authorities behind its values (I5)
    hop = trends["hop_latency_ms"]
    assert hop["model.operation"]["p50_ms"] == 210.0
    assert hop["model.operation"]["authorities"] == ["adapter_reported"]
    assert hop["turn"]["p50_ms"] == 2500.0
    assert hop["turn"]["authorities"] == ["derived:event_timestamps"]
    # a window filter narrows the trends too
    m = _rows(live.base, "/calls?since=2500&format=json", live.token)
    assert m["trends"]["volume"] == 1
    assert m["trends"]["states"]["ERROR"] == 1


# =========================================================================
# per-call view (R6)
# =========================================================================

def test_call_detail_renders_observations_moments_and_provenance(live):
    code, body, _h = _req(live.base, "/calls/call-scored", token=live.token)
    assert code == 200
    # per-dimension observations, each on its own
    assert "Per-dimension observations" in body
    assert "overlap_while_agent_talking" in body
    assert "long_response_gap" in body
    # ranked candidate moments with measured magnitudes
    assert "Ranked candidate moments" in body
    assert "overlap_sec" in body
    # the timing waterfall keeps derived and reported apart (I5)
    assert "Timing waterfall" in body
    assert "derived:event_timestamps" in body
    assert "adapter_reported" in body
    assert "yield_latency_ms" in body
    # scorer provenance is shown
    assert __version__ in body
    assert "sha256:" in body
    # evidence lanes + the local audio path, shown as recorded (no new
    # file-serving route: the page carries no audio element)
    assert "participant_audio" in body
    assert "01-hard-interruption.example.wav" in body
    assert "<audio" not in body.lower()
    # drill-through to the raw session surface
    assert "/health" in body


def test_call_detail_json_mirror_is_the_same_model(live):
    m = _rows(live.base, "/calls/call-scored?format=json", live.token)
    assert m["view"] == "call_detail"
    score = m["score"]
    assert score["state"] == "SCORED"
    assert score["scorer_version"] == __version__
    assert score["config_sha256"].startswith("sha256:")
    assert score["dimensions"]["overlap_while_agent_talking"]["candidate_count"] >= 1
    assert score["candidates"][0]["plain_english"].startswith(
        "the caller took the floor")
    hops = {hop["kind"]: hop for hop in score["hops"]}
    assert hops["model.operation"]["authority"] == "adapter_reported"
    assert hops["turn"]["authority"] == "derived:event_timestamps"
    assert score["timing"]["turn_spans"][0]["reported"]["yield_latency_ms"] == 480.0
    assert score["audio"]["path"].endswith(".wav")
    assert m["session"]["state"] == "QUIESCENT"
    assert m["session"]["completeness"]["status"] == "incomplete"


def test_call_detail_error_row_shows_its_reason(live):
    code, body, _h = _req(live.base, "/calls/call-crash", token=live.token)
    assert code == 200
    assert "ERROR" in body
    assert "RuntimeError: boom" in body


def test_unknown_call_is_404(live):
    for path in ["/calls/nope", "/calls/nope?format=json"]:
        code, _body, _h = _req(live.base, path, token=live.token)
        assert code == 404, path


# =========================================================================
# ETag + 304 (R7)
# =========================================================================

def test_feed_etag_304_and_change_detection(live, tmp_path):
    code, body, headers = _req(live.base, "/calls?format=json",
                               token=live.token)
    assert code == 200
    etag = headers.get("ETag")
    assert etag

    # a matching If-None-Match is a body-less 304 carrying the same ETag
    code, body, headers = _req(live.base, "/calls?format=json",
                               token=live.token,
                               headers={"If-None-Match": etag})
    assert code == 304
    assert body == ""
    assert headers.get("ETag") == etag

    # new evidence -> new sidecar content -> a fresh ETag and a 200
    evidence = ProductionStore(live.production_db, clock=lambda: 4000.0)
    _ingest_call(evidence, "call-later", audio_path=_stereo_fixture())
    evidence.close()
    store = ConsoleStore(default_console_path(live.production_db))
    try:
        rebuild_sidecar(live.production_db, store)
    finally:
        store.close()
    code, body, headers = _req(live.base, "/calls?format=json",
                               token=live.token,
                               headers={"If-None-Match": etag})
    assert code == 200
    assert headers.get("ETag") != etag
    assert "call-later" in body


def test_feed_page_degrades_without_javascript(live):
    # the HTML page is complete as served: every row and the trends strip are
    # server-rendered; the polling script only revalidates and swaps
    code, body, _h = _req(live.base, "/calls", token=live.token)
    assert code == 200
    assert 'id="calls-live"' in body
    assert 'id="calls-updated"' in body
    assert "If-None-Match" in body            # the poll revalidates, not re-reads
    for subject in ("call-scored", "call-noaudio", "call-crash"):
        assert subject in body


# =========================================================================
# honesty invariants
# =========================================================================

def test_no_blended_or_overall_score_on_calls_surfaces(live):
    for path in ["/calls", "/calls/call-scored"]:
        _c, body, _h = _req(live.base, path, token=live.token)
        # the pages echo the operator-selected db paths; this test's own tmp
        # directory is named after the test, so mask those before scanning
        low = body.replace(os.path.dirname(live.production_db), "").lower()
        assert "overall_score" not in low, path
        assert "overall score" not in low, path
        assert "blended" not in low, path
        assert "composite score" not in low, path
    for path in ["/calls?format=json", "/calls/call-scored?format=json"]:
        _c, body, _h = _req(live.base, path, token=live.token)
        assert "overall_score" not in body, path


def test_feed_states_absent_sources_explicitly(tmp_path):
    # no --production-db: the feed says how to wire one, and shares no rows
    server = _start_server(str(tmp_path / "fleet"), None)
    try:
        code, body, _h = _req(server.base, "/calls", token=server.token)
        assert code == 200
        assert "hotato console" in body
        m = _rows(server.base, "/calls?format=json", server.token)
        assert m["configured"] is False
        assert m["rows"] == []
        # the per-call view has nothing to resolve -> 404, never a fabrication
        code, _body, _h = _req(server.base, "/calls/anything",
                               token=server.token)
        assert code == 404
    finally:
        server.stop()


def test_feed_states_missing_sidecar_explicitly(tmp_path):
    # an evidence db with no sidecar beside it yet: explicit state, zero rows
    db_path = str(tmp_path / "production.sqlite3")
    evidence = ProductionStore(db_path, clock=lambda: 1000.0)
    _ingest_call(evidence, "call-a", audio_path=_stereo_fixture())
    evidence.close()
    server = _start_server(str(tmp_path / "fleet"), db_path)
    try:
        m = _rows(server.base, "/calls?format=json", server.token)
        assert m["configured"] is True
        assert m["sidecar"]["present"] is False
        assert m["rows"] == []
        code, body, _h = _req(server.base, "/calls", token=server.token)
        assert "--rebuild-scores" in body
    finally:
        server.stop()


# =========================================================================
# the `hotato console` command (R4)
# =========================================================================

def test_console_command_defaults_mirror_serve():
    args = cli.build_parser().parse_args(
        ["console", "--production-db", "prod.sqlite3"])
    assert args.production_db == "prod.sqlite3"
    assert args.host == "127.0.0.1"
    assert args.port == 8321
    assert args.workspace == "default"
    assert args.no_open is False


def test_console_command_requires_the_production_db(capsys):
    with pytest.raises(SystemExit) as excinfo:
        cli.build_parser().parse_args(["console"])
    assert excinfo.value.code == 2


def test_console_command_is_serve_plus_worker_landing_on_calls(monkeypatch):
    import hotato.serve as serve_mod

    calls = {}

    def fake_run_serve(**kwargs):
        calls.update(kwargs)
        return 0

    monkeypatch.setattr(serve_mod, "run_serve", fake_run_serve)
    assert cli.main(["console", "--production-db", "prod.sqlite3",
                     "--no-open", "--port", "9999"]) == 0
    assert calls["production_db"] == "prod.sqlite3"
    assert calls["score_production"] is True
    assert calls["landing"] == "/calls"
    assert calls["port"] == 9999
    assert calls["open_browser"] is False


def test_console_landing_url_points_at_calls(tmp_path, monkeypatch, capsys):
    # the printed tokenised URL opens on the live call feed
    db_path = str(tmp_path / "production.sqlite3")
    evidence = ProductionStore(db_path, clock=lambda: 1000.0)
    _ingest_call(evidence, "call-a", audio_path=_stereo_fixture())
    evidence.close()

    import hotato.serve.app as app_mod

    class _StopServe(Exception):
        pass

    def bail(self):
        raise KeyboardInterrupt

    monkeypatch.setattr(app_mod._WorkspaceServer, "serve_forever", bail)
    monkeypatch.setenv("HOTATO_NO_BROWSER", "1")
    code = app_mod.run_serve(
        workspace="default", port=0, registry=str(tmp_path / "fleet"),
        token="tok-test", open_browser=False, production_db=db_path,
        score_production=True, landing="/calls")
    assert code == 0
    err = capsys.readouterr().err
    assert "/calls?token=" in err


def test_calls_tab_is_in_the_nav(live):
    code, body, _h = _req(live.base, "/", token=live.token)
    assert code == 200
    assert 'href="/calls"' in body
    assert ">Calls<" in body
