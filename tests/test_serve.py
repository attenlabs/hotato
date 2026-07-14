"""``hotato serve`` -- the self-hosted team workspace (Phase 4, GOAL §6).

Starts the real threaded server on an ephemeral port over a registry seeded with
the fleet's OWN ``add_*`` methods + a real conversation artifact, then proves:

* default bind is 127.0.0.1;
* an unauthenticated request is 401 and never routed; a valid bearer token 200s;
* the token compare is constant-time (``hmac.compare_digest`` semantics);
* each of the five views renders 200 with its real content, and each has a
  valid ``?format=json`` mirror;
* every authenticated request is recorded in the append-only audit log (who =
  token prefix, never the secret);
* ``text_redacted`` never leaks into the conversation inspector HTML;
* NO blended/overall score string appears in any view (honesty invariant 1);
* origin real|simulated is never merged in any aggregate (invariant 5);
* zero egress -- exercising every view makes no outbound (non-loopback) socket
  connection.
"""
from __future__ import annotations

import json
import os
import socket
import threading
import time
import urllib.error
import urllib.request

import pytest

from hotato.conversation import build_manifest, write_conversation
from hotato.fleet.registry import Registry
from hotato.fleet.store import ArtifactStore
from hotato.serve import build_server
from hotato.serve.app import ServeContext, _safe_dirname
from hotato.serve.security import AuditLog, SessionStore, constant_time_eq

# A sentinel that lives ONLY inside a redacted transcript segment + redacted
# trace span; it must never reach any rendered HTML.
_SECRET = "SUPERSECRETPIN1234"
_TOKEN = "tok_abcdefgh_TEST_0123456789_xyz"

# Two distinct UTC calendar days, so production-health has >= 2 days of history.
_DAY1 = 1704067200.0   # 2024-01-01T00:00:00Z
_DAY2 = 1704153600.0   # 2024-01-02T00:00:00Z


# =========================================================================
# fixture: seed a rich workspace + start the server
# =========================================================================

def _seed(home: str) -> None:
    reg = Registry(home=home)
    ws = "default"
    reg.ensure_workspace(ws, "Test Co")
    reg.add_agent(ws, "appt-agent", name="Appointment Agent", stack="vapi")
    # two releases: rc-2 is newer (current), rc-1 is the baseline
    reg.add_release(ws, "rc-1", agent_id="appt-agent", model="gpt-x",
                    prompt_digest="p1", created_at=1000.0)
    reg.add_release(ws, "rc-2", agent_id="appt-agent", model="gpt-y",
                    prompt_digest="p2", created_at=2000.0)
    reg.set_agent_release(ws, "appt-agent", current_release_id="rc-2")
    reg.add_suite(ws, "smoke", name="Smoke", purpose="release gate",
                  required_for_release=True, inconclusive_policy="fail",
                  created_at=500.0)
    reg.add_scenario(ws, "cancel-after-cutoff", suite_id="smoke",
                     goal="cancel appointment after the cutoff", created_at=600.0)
    reg.add_scenario(ws, "book-table", suite_id="smoke",
                     goal="book a table for four", created_at=610.0)

    # runs: cancel-after-cutoff has 3 reps on rc-2 (reliability) + 1 on rc-1
    reg.add_run(ws, "run-a0", scenario_id="cancel-after-cutoff", release_id="rc-1",
                status="completed")
    for r in ("run-a1", "run-a2", "run-a3"):
        reg.add_run(ws, r, scenario_id="cancel-after-cutoff", release_id="rc-2",
                    status="completed")
    reg.add_run(ws, "run-b0", scenario_id="book-table", release_id="rc-1",
                status="completed")
    reg.add_run(ws, "run-b1", scenario_id="book-table", release_id="rc-2",
                status="completed")

    # conversations: BOTH real and simulated, across two days
    reg.add_conversation(ws, "conv-a0", run_id="run-a0", agent_id="appt-agent",
                         origin="real", created_at=_DAY1)
    reg.add_conversation(ws, "conv-a1", run_id="run-a1", agent_id="appt-agent",
                         origin="simulated", created_at=_DAY1)  # artifact bound below
    reg.add_conversation(ws, "conv-a2", run_id="run-a2", agent_id="appt-agent",
                         origin="simulated", created_at=_DAY1)
    reg.add_conversation(ws, "conv-a3", run_id="run-a3", agent_id="appt-agent",
                         origin="simulated", created_at=_DAY2)
    reg.add_conversation(ws, "conv-b0", run_id="run-b0", agent_id="appt-agent",
                         origin="real", created_at=_DAY1)
    reg.add_conversation(ws, "conv-b1", run_id="run-b1", agent_id="appt-agent",
                         origin="real", created_at=_DAY2)

    def ev(eid, conv, dim, status, ca):
        reg.add_evaluation(ws, eid, conversation_id=conv, evaluator_id="assert.v1",
                           dimension=dim, status=status, created_at=ca)

    # rc-1 baseline (conv-a0): cancel-after-cutoff passes outcome+policy
    ev("e-a0-o", "conv-a0", "outcome", "PASS", _DAY1)
    ev("e-a0-p", "conv-a0", "policy", "PASS", _DAY1)
    ev("e-a0-s", "conv-a0", "speech", "PASS", _DAY1)
    # rc-2 (conv-a1): policy REGRESSES to FAIL; a speech INCONCLUSIVE
    ev("e-a1-o", "conv-a1", "outcome", "PASS", _DAY1)
    ev("e-a1-p", "conv-a1", "policy", "FAIL", _DAY1)
    ev("e-a1-c", "conv-a1", "conversation", "PASS", _DAY1)
    ev("e-a1-s", "conv-a1", "speech", "INCONCLUSIVE", _DAY1)
    # conv-a2: policy FAIL again (a second failing rep)
    ev("e-a2-o", "conv-a2", "outcome", "PASS", _DAY1)
    ev("e-a2-p", "conv-a2", "policy", "FAIL", _DAY1)
    # conv-a3 (day 2): everything passes (the reliable rep)
    ev("e-a3-o", "conv-a3", "outcome", "PASS", _DAY2)
    ev("e-a3-p", "conv-a3", "policy", "PASS", _DAY2)
    # book-table: FAILS on rc-1 (conv-b0), FIXED on rc-2 (conv-b1)
    ev("e-b0-o", "conv-b0", "outcome", "FAIL", _DAY1)
    ev("e-b1-o", "conv-b1", "outcome", "PASS", _DAY2)

    # a review on the regressed policy evaluation
    reg.add_review(ws, "rev-1", evaluation_id="e-a1-p", reviewer="alice",
                   decision="confirmed-fail", rationale="disclosure was skipped",
                   adjudication_state="final")

    # assertion_runs on conv-a1: a deterministic FAIL + a model-judged FAIL,
    # so failure-clustering + the inspector's separate lanes have real data.
    reg.add_assertion_run(ws, assertion_id="required_disclosure", agent_id="appt-agent",
                          call_id="conv-a1", conversation_id="conv-a1",
                          kind="required_disclosure", dimension="policy",
                          deterministic=True, status="FAIL",
                          reason="required_disclosure cancellation_policy_v2 not "
                                 "spoken before tool cancel_appointment (appt_772)")
    reg.add_assertion_run(ws, assertion_id="explained_cutoff", agent_id="appt-agent",
                          call_id="conv-a1", conversation_id="conv-a1",
                          kind="judge_rubric", dimension="conversation",
                          deterministic=False, status="FAIL",
                          reason="the cutoff explanation was unclear")
    reg.close()

    # a real conversation artifact for conv-a1 (manifest + transcript + trace),
    # with a redacted transcript segment + a redacted trace span carrying _SECRET
    _seed_artifact(home)


def _seed_artifact(home: str) -> None:
    conv_dir = os.path.join(home, "conv-a1-artifact")
    os.makedirs(conv_dir, exist_ok=True)
    transcript = {"segments": [
        {"start": 0.0, "end": 2.0, "speaker": "caller", "text": "I need to cancel"},
        {"start": 2.0, "end": 4.0, "speaker": "agent", "text": "Let me check that"},
        {"start": 4.0, "end": 6.0, "speaker": "caller", "text": _SECRET,
         "redacted": True},
    ]}
    tpath = os.path.join(conv_dir, "transcript.json")
    with open(tpath, "w", encoding="utf-8") as fh:
        json.dump(transcript, fh)
    trace = [
        {"type": "tool_call", "name": "cancel_appointment", "start": 3.0,
         "end": 3.4, "detail": "{\"appointment_id\": \"appt_772\"}"},
        {"type": "asr", "name": "caller_utt", "start": 4.0, "end": 6.0,
         "text": _SECRET, "text_redacted": True},
    ]
    trpath = os.path.join(conv_dir, "trace.jsonl")
    with open(trpath, "w", encoding="utf-8") as fh:
        fh.write("\n".join(json.dumps(s) for s in trace) + "\n")

    manifest = build_manifest(
        conversation_id="conv-a1", agent_id="appt-agent",
        origin={"kind": "simulated", "simulator": {
            "model_id": "caller-v1", "scenario_id": "cancel-after-cutoff", "seed": "7"}},
        created_at="2024-01-01T00:00:00Z",
        artifact_files={"transcript": tpath, "trace": trpath},
        base_dir=conv_dir)
    write_conversation(manifest, conv_dir)

    store = ArtifactStore(os.path.join(home, "artifacts"))
    store.put_file(tpath, kind="transcript", workspace_id="default")
    store.put_file(trpath, kind="trace", workspace_id="default")
    digest = store.put_json(manifest, kind="conversation", workspace_id="default")

    reg = Registry(home=home)
    reg.add_conversation("default", "conv-a1", run_id="run-a1", agent_id="appt-agent",
                         origin="simulated", artifact_digest=digest, created_at=_DAY1)
    reg.close()


class _Live:
    def __init__(self, base, token, audit_path, home, server, thread):
        self.base = base
        self.token = token
        self.audit_path = audit_path
        self.home = home
        self._server = server
        self._thread = thread

    def stop(self):
        self._server.shutdown()
        self._thread.join(timeout=5)
        self._server.server_close()


@pytest.fixture()
def live(tmp_path):
    home = str(tmp_path / "fleet")
    os.makedirs(home, exist_ok=True)
    _seed(home)
    state_dir = os.path.join(home, "serve", "default")
    os.makedirs(state_dir, exist_ok=True)
    audit_path = os.path.join(state_dir, "audit.jsonl")
    ctx = ServeContext(
        home=home, workspace="default",
        store_root=os.path.join(home, "artifacts"), token=_TOKEN,
        state_dir=state_dir, audit=AuditLog(audit_path), sessions=SessionStore(),
        bind_host="127.0.0.1")
    server = build_server(ctx, "127.0.0.1", 0)
    port = server.server_address[1]
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    l = _Live("http://127.0.0.1:%d" % port, _TOKEN, audit_path, home, server, thread)
    try:
        yield l
    finally:
        l.stop()


# =========================================================================
# helpers
# =========================================================================

def _req(base, path, *, token=None, cookie=None):
    req = urllib.request.Request(base + path)
    if token is not None:
        req.add_header("Authorization", "Bearer " + token)
    if cookie is not None:
        req.add_header("Cookie", cookie)
    try:
        resp = urllib.request.urlopen(req, timeout=5)
        return resp.getcode(), resp.read().decode("utf-8"), dict(resp.headers)
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read().decode("utf-8"), dict(exc.headers)


_VIEWS = ["/", "/scenarios", "/clusters", "/health"]


# =========================================================================
# default bind
# =========================================================================

def test_default_bind_is_loopback(live):
    assert live._server.server_address[0] == "127.0.0.1"


def test_cli_serve_default_host_is_loopback():
    from hotato.cli import build_parser
    args = build_parser().parse_args(["serve"])
    assert args.host == "127.0.0.1"
    assert args.port == 8321
    assert args.workspace == "default"


def test_cli_serve_no_open_flag():
    from hotato.cli import build_parser
    assert build_parser().parse_args(["serve"]).no_open is False
    assert build_parser().parse_args(["serve", "--no-open"]).no_open is True


def test_browser_open_respects_disable_signals(monkeypatch):
    # the browser is never dispatched when disabled by --no-open, or when the
    # environment says not to (CI / $HOTATO_NO_BROWSER); webbrowser.open is not
    # even reached, so CI and the test suite never spawn a browser.
    import hotato.serve.app as app_mod
    from hotato.serve.app import _maybe_open_browser
    calls = []
    monkeypatch.setattr(app_mod.webbrowser, "open",
                        lambda *a, **k: calls.append(a) or True)

    assert _maybe_open_browser("http://x/?token=t", enabled=False) is False
    monkeypatch.setenv("HOTATO_NO_BROWSER", "1")
    assert _maybe_open_browser("http://x/?token=t", enabled=True) is False
    monkeypatch.delenv("HOTATO_NO_BROWSER", raising=False)
    monkeypatch.setenv("CI", "true")
    assert _maybe_open_browser("http://x/?token=t", enabled=True) is False
    assert calls == []


# =========================================================================
# auth
# =========================================================================

def test_unauthenticated_is_401_and_not_routed(live):
    # every data/view path stays token-gated (the courtesy landing at "/" is the
    # ONLY unauthenticated 200 and is covered by test_root_without_token_*). The
    # root JSON mirror is included to prove data at "/" is still gated.
    gated = ["/scenarios", "/clusters", "/health", "/conversation/conv-a1",
             "/?format=json"]
    for path in gated:
        code, body, headers = _req(live.base, path)
        assert code == 401, path
        assert "bearer" in headers.get("WWW-Authenticate", "").lower()
        # the 401 body is the auth page, never workspace content
        assert "Release readiness" not in body
        assert "conv-a1" not in body


def test_root_without_token_is_friendly_landing(live):
    # opening the workspace HOME in a browser without a token returns a clean 200
    # landing page (not a bare 401), with NO workspace data and NO token in it.
    code, body, headers = _req(live.base, "/")
    assert code == 200
    assert "text/html" in headers.get("Content-Type", "")
    assert "hotato workspace" in body
    assert "hotato serve" in body               # tells the user how to get in
    assert "Release readiness" not in body      # shares no workspace data
    assert "cancel-after-cutoff" not in body
    assert live.token not in body               # never reveals the token
    # the machine mirror at the root is still token-gated
    code_json, _b, _h = _req(live.base, "/?format=json")
    assert code_json == 401


def test_wrong_token_is_401(live):
    code, _body, _h = _req(live.base, "/", token="not-the-token")
    assert code == 401


def test_valid_bearer_token_is_200(live):
    code, body, _h = _req(live.base, "/", token=live.token)
    assert code == 200
    assert "Release readiness" in body


def test_constant_time_compare_semantics():
    # correct on equal, false on unequal, false on unequal length (no throw)
    assert constant_time_eq("abc", "abc") is True
    assert constant_time_eq("abc", "abd") is False
    assert constant_time_eq("abc", "abcd") is False
    assert constant_time_eq("", "") is True
    # it is the hmac.compare_digest path (timing-safe), not ==
    import inspect

    import hotato.serve.security as sec
    src = inspect.getsource(sec.constant_time_eq)
    assert "compare_digest" in src


def test_query_token_sets_session_cookie_and_redirects(live):
    # a browser opening /?token=... gets a Set-Cookie + a redirect that strips
    # the token, then the cookie alone authenticates subsequent navigation.
    req = urllib.request.Request(live.base + "/?token=" + live.token)
    opener = urllib.request.build_opener(_NoRedirect())
    # a non-followed 302 surfaces as HTTPError in urllib; its .headers carry the
    # Set-Cookie + Location we want to inspect.
    try:
        resp = opener.open(req, timeout=5)
        code, headers = resp.getcode(), resp.headers
    except urllib.error.HTTPError as exc:
        code, headers = exc.code, exc.headers
    assert code == 302
    set_cookie = headers.get("Set-Cookie", "")
    assert "hotato_session=" in set_cookie and "HttpOnly" in set_cookie
    assert "token" not in headers.get("Location", "")
    sid = set_cookie.split("hotato_session=", 1)[1].split(";", 1)[0]
    code, body, _h = _req(live.base, "/", cookie="hotato_session=" + sid)
    assert code == 200 and "Release readiness" in body


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *a, **k):
        return None


# =========================================================================
# the five views render their real content
# =========================================================================

def test_release_readiness_renders_real_content(live):
    code, body, _h = _req(live.base, "/", token=live.token)
    assert code == 200
    assert "Release readiness" in body
    assert "rc-2" in body                      # current release id
    assert "failures by dimension" in body
    assert "cancel-after-cutoff" in body       # the new regression
    assert "book-table" in body                # the fixed scenario


def test_scenario_matrix_renders_real_content(live):
    code, body, _h = _req(live.base, "/scenarios", token=live.token)
    assert code == 200
    assert "Scenario matrix" in body
    assert "cancel-after-cutoff" in body
    assert "book-table" in body
    assert "pass^" in body                     # reliability, where reps exist


def test_scenario_matrix_status_filter(live):
    code, body, _h = _req(live.base, "/scenarios?status=FAIL", token=live.token)
    assert code == 200
    # cancel-after-cutoff aggregates to FAIL on rc-2 (policy regressed)
    assert "cancel-after-cutoff" in body


def test_conversation_inspector_renders_real_content(live):
    code, body, _h = _req(live.base, "/conversation/conv-a1", token=live.token)
    assert code == 200
    assert "conv-a1" in body
    assert "simulated" in body
    assert "cancel_appointment" in body        # a real trace span
    assert "required_disclosure" in body       # the deterministic assertion
    assert "deterministic" in body and "advisory" in body  # separate lanes


def test_failure_clusters_render_real_content(live):
    code, body, _h = _req(live.base, "/clusters", token=live.token)
    assert code == 200
    assert "clusters by observable signature" in body
    assert "root cause" not in body.lower()    # never claim causality
    assert "required_disclosure" in body       # a clustered kind
    assert "/conversation/conv-a1" in body     # drill-through link


def test_production_health_renders_real_content(live):
    code, body, _h = _req(live.base, "/health", token=live.token)
    assert code == 200
    assert "Production health" in body
    assert "real" in body and "simulated" in body


def test_unknown_conversation_is_404(live):
    code, _body, _h = _req(live.base, "/conversation/nope", token=live.token)
    assert code == 404


# =========================================================================
# JSON mirror on every view
# =========================================================================

def test_json_mirror_for_every_view(live):
    expect = {
        "/?format=json": "release_readiness",
        "/scenarios?format=json": "scenario_matrix",
        "/clusters?format=json": "failure_clusters",
        "/health?format=json": "production_health",
        "/conversation/conv-a1?format=json": "conversation_inspector",
    }
    for path, view in expect.items():
        code, body, headers = _req(live.base, path, token=live.token)
        assert code == 200, path
        assert "application/json" in headers.get("Content-Type", ""), path
        obj = json.loads(body)                 # must be valid JSON
        assert obj["view"] == view, path


# =========================================================================
# honesty invariants
# =========================================================================

def test_no_blended_or_overall_score_in_any_view(live):
    for path in _VIEWS + ["/conversation/conv-a1"]:
        _c, body, _h = _req(live.base, path, token=live.token)
        low = body.lower()
        assert "overall_score" not in low, path
        assert "overall score" not in low, path
        assert "blended" not in low, path
        assert "composite score" not in low, path
    # the JSON models carry no overall_score key either
    for path in ["/?format=json", "/health?format=json",
                 "/conversation/conv-a1?format=json"]:
        _c, body, _h = _req(live.base, path, token=live.token)
        assert "overall_score" not in body


def test_origin_real_and_simulated_never_merged(live):
    # readiness JSON: origin split keeps real and simulated distinct
    _c, body, _h = _req(live.base, "/?format=json", token=live.token)
    m = json.loads(body)
    split = m["current"]["origin_split"]
    assert "real" in split and "simulated" in split
    assert split["real"] != 0 or split["simulated"] != 0

    # health JSON: separate per-origin buckets, each with its OWN counts
    _c, body, _h = _req(live.base, "/health?format=json", token=live.token)
    h = json.loads(body)
    origins = h["origins"]
    assert "real" in origins and "simulated" in origins
    assert origins["real"]["ingested"] > 0
    assert origins["simulated"]["ingested"] > 0
    # the two buckets are not the same object / not summed into one
    assert origins["real"]["ingested"] + origins["simulated"]["ingested"] \
        <= h["ingested_total"]


def test_redacted_text_never_leaks_into_inspector(live):
    for path in ["/conversation/conv-a1", "/conversation/conv-a1?format=json"]:
        _c, body, _h = _req(live.base, path, token=live.token)
        assert _SECRET not in body, path
    # and the redaction marker IS shown (proves the span was rendered, redacted)
    _c, html, _h = _req(live.base, "/conversation/conv-a1", token=live.token)
    assert "[redacted]" in html


# =========================================================================
# audit log
# =========================================================================

def test_audit_log_records_authenticated_requests(live):
    _c, _b, _h = _req(live.base, "/scenarios?status=FAIL", token=live.token)
    # give the append a beat under the threaded server
    time.sleep(0.05)
    with open(live.audit_path, "r", encoding="utf-8") as fh:
        lines = [json.loads(x) for x in fh if x.strip()]
    assert lines, "audit log is empty"
    rec = [r for r in lines if r["path"] == "/scenarios"][-1]
    assert rec["method"] == "GET"
    assert rec["status"] == 200
    # who is the token PREFIX, never the secret
    assert rec["who"].startswith(_TOKEN[:8])
    assert _TOKEN not in json.dumps(lines)
    # the token is stripped from the recorded query
    assert "token" not in rec["query"] or "status=FAIL" in rec["query"]


def test_audit_log_records_unauthenticated_denials(live):
    # a token-gated path without a token is a 401 and is audited; "/" would be the
    # courtesy landing (200), so exercise a data path here.
    _c, _b, _h = _req(live.base, "/health")
    time.sleep(0.05)
    with open(live.audit_path, "r", encoding="utf-8") as fh:
        lines = [json.loads(x) for x in fh if x.strip()]
    assert any(r["status"] == 401 for r in lines)


# =========================================================================
# zero egress
# =========================================================================

def test_zero_egress_no_external_connections(live, monkeypatch):
    """Exercising every view must make NO outbound connection to any non-loopback
    address. We wrap socket.connect to whitelist loopback (the test client + the
    server's own accepted sockets) and flag anything else."""
    external = []
    loopback = {"127.0.0.1", "::1", "localhost", "0.0.0.0", ""}
    real_connect = socket.socket.connect

    def guard(self, address):
        host = address[0] if isinstance(address, (tuple, list)) else str(address)
        if host not in loopback:
            external.append(address)
            raise AssertionError("blocked external connect to %r" % (address,))
        return real_connect(self, address)

    monkeypatch.setattr(socket.socket, "connect", guard)
    for path in _VIEWS + ["/conversation/conv-a1",
                          "/?format=json", "/health?format=json"]:
        code, _b, _h = _req(live.base, path, token=live.token)
        assert code == 200, path
    assert external == [], "server attempted an external connection: %r" % external


# =========================================================================
# drill-to-evidence + misc
# =========================================================================

def test_evidence_endpoint_serves_blob_as_plain_text(live):
    # find the manifest digest from the inspector JSON, then fetch a child blob
    _c, body, _h = _req(live.base, "/conversation/conv-a1?format=json", token=live.token)
    m = json.loads(body)
    digest = m["artifact_digest"]
    assert digest
    code, blob, headers = _req(live.base, "/evidence/" + digest, token=live.token)
    assert code == 200
    # served as text/plain with nosniff so a crafted blob cannot execute
    assert "text/plain" in headers.get("Content-Type", "")
    assert headers.get("X-Content-Type-Options") == "nosniff"
    # it is the conversation manifest
    assert "conversation" in blob or "conv-a1" in blob


def test_evidence_bad_digest_is_400(live):
    code, _b, _h = _req(live.base, "/evidence/not-a-digest", token=live.token)
    assert code == 400


def _serve_workspace(home, workspace):
    """Start a real loopback server for `workspace` over `home`'s SHARED,
    content-addressed store. Returns (base_url, token, stop). Used to prove that
    the content-addressed store being shared does NOT let one workspace read
    another's blob by digest."""
    state_dir = os.path.join(home, "serve", _safe_dirname(workspace))
    os.makedirs(state_dir, exist_ok=True)
    ctx = ServeContext(
        home=home, workspace=workspace,
        store_root=os.path.join(home, "artifacts"), token=_TOKEN,
        state_dir=state_dir, audit=AuditLog(os.path.join(state_dir, "audit.jsonl")),
        sessions=SessionStore(), bind_host="127.0.0.1")
    server = build_server(ctx, "127.0.0.1", 0)
    port = server.server_address[1]
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()

    def stop():
        server.shutdown()
        thread.join(timeout=5)
        server.server_close()

    return "http://127.0.0.1:%d" % port, _TOKEN, stop


def test_evidence_child_of_rooted_manifest_is_served(live):
    """The owning workspace can drill into a CHILD evidence blob (transcript)
    whose digest is not a top-level registry row but is declared by the rooted
    conversation manifest. Guards the fix from over-restricting: reachability is
    transitive through a workspace-rooted manifest's declared artifacts."""
    _c, body, _h = _req(live.base, "/conversation/conv-a1?format=json", token=live.token)
    m = json.loads(body)
    child_sha = m["manifest"]["artifacts"]["transcript"]["sha256"]
    assert len(child_sha) == 64
    code, _blob, _h = _req(live.base, "/evidence/" + child_sha, token=live.token)
    assert code == 200


def test_evidence_cross_workspace_foreign_root_is_404(tmp_path):
    """CRITICAL: a digest rooted ONLY in workspace 'default' must NOT be readable
    by a different workspace, even though the content-addressed store is SHARED
    and store.has(digest) is True. CAS presence is not authority."""
    home = str(tmp_path / "fleet")
    os.makedirs(home, exist_ok=True)
    _seed(home)  # roots conv-a1's manifest digest in workspace 'default'
    reg = Registry(home=home)
    digest = reg.get_conversation("default", "conv-a1")["artifact_digest"]
    reg.close()
    assert digest
    # the blob is genuinely present in the shared CAS...
    assert ArtifactStore(os.path.join(home, "artifacts")).has(digest)
    # ...but an unrelated workspace never rooted it -> the read is refused 404.
    base, token, stop = _serve_workspace(home, "intruder-ws")
    try:
        code, _b, _h = _req(base, "/evidence/" + digest, token=token)
        assert code == 404
    finally:
        stop()


def test_evidence_orphaned_after_only_root_deleted_is_404(tmp_path):
    """CRITICAL: after a workspace's ONLY live registry root for a digest is
    deleted, the orphaned CAS blob must 404 even for that same workspace. CAS
    lineage/presence is never an ACL; authority lives at the registry root."""
    home = str(tmp_path / "fleet")
    os.makedirs(home, exist_ok=True)
    _seed(home)
    reg = Registry(home=home)
    digest = reg.get_conversation("default", "conv-a1")["artifact_digest"]
    # delete the ONLY row that roots this digest for the workspace
    reg.conn.execute(
        "DELETE FROM conversations WHERE workspace_id=? AND conversation_id=?",
        ("default", "conv-a1"))
    reg.conn.commit()
    reg.close()
    # the blob is still physically present in the store (orphaned), not GC'd
    assert ArtifactStore(os.path.join(home, "artifacts")).has(digest)
    base, token, stop = _serve_workspace(home, "default")
    try:
        code, _b, _h = _req(base, "/evidence/" + digest, token=token)
        assert code == 404
    finally:
        stop()


def test_safe_dirname_blocks_traversal():
    assert _safe_dirname("../../etc") not in ("../../etc",)
    assert "/" not in _safe_dirname("a/b/c")
    assert _safe_dirname("..") == "default"
    assert _safe_dirname("") == "default"
    assert _safe_dirname("team-alpha_1") == "team-alpha_1"


# =========================================================================
# read-only Failure Record viewer (/records + /records/<id>)
# =========================================================================

# A sentinel that would only exist if a raw evaluator payload leaked into a
# record; the share-safe projection never carries it, so it must never render.
_RECORD_PAYLOAD_SECRET = "PAYLOAD_LEAK_9f3a"


def _make_record():
    """Project ONE real, valid hotato.failure-record.v1 from a minimal failing
    test-run source (a policy FAIL). Returns the canonical record dict."""
    from hotato.failure_record import project
    doc = {
        "kind": "hotato.test-run", "version": "1",
        "test_id": "cancel-after-cutoff", "agent": "appt-agent", "exit_code": 1,
        "assertions": {"results": [
            {"id": "required_disclosure", "kind": "policy", "status": "FAIL",
             "dimension": "policy",
             "reason": "required_disclosure cancellation_policy_v2 not spoken "
                       "before tool cancel_appointment"},
        ]},
        "success": {"required": []},
    }
    return project(doc)


def _seed_record(home, record_id="rec-policy-fail"):
    """Write a validated record under ``<home>/records/<record_id>/`` in the
    ``hotato record render --out`` layout. Returns (record_id, record)."""
    from hotato.failure_render import render_json
    record = _make_record()
    rdir = os.path.join(home, "records", record_id)
    os.makedirs(rdir, exist_ok=True)
    with open(os.path.join(rdir, "failure-record.json"), "w", encoding="utf-8") as fh:
        fh.write(render_json(record))
    return record_id, record


def test_records_empty_state_when_none(live):
    # the default fixture seeds NO records: the list route 200s with an explicit
    # empty state, never a fabricated record.
    code, body, _h = _req(live.base, "/records", token=live.token)
    assert code == 200
    assert "Failure records" in body
    assert "No failure records" in body


def test_records_routes_require_token(live):
    _seed_record(live.home)
    for path in ["/records", "/records/rec-policy-fail", "/records?format=json",
                 "/records/rec-policy-fail?format=json"]:
        code, body, headers = _req(live.base, path)
        assert code == 401, path
        assert "bearer" in headers.get("WWW-Authenticate", "").lower(), path
        assert "cancel-after-cutoff" not in body, path   # no record data leaks


def test_records_list_and_detail_render(live):
    rid, record = _seed_record(live.home)

    # list: the record row is present with its status + subject
    code, body, _h = _req(live.base, "/records", token=live.token)
    assert code == 200
    assert rid in body
    assert "cancel-after-cutoff" in body
    assert "/records/" + rid in body                 # drill-through link

    # detail: five lanes, evidence refs, reproduce -- no payload/secret/abs path
    code, body, _h = _req(live.base, "/records/" + rid, token=live.token)
    assert code == 200
    for lane in ("outcome", "policy", "conversation", "speech", "reliability"):
        assert lane in body
    assert record["record_id"] in body               # content address shown
    assert record["evidence"][0]["evidence_id"] in body   # an evidence ref
    assert "deterministic" in body.lower() or "gate" in body.lower()
    # the deterministic gate is shown APART from the model advisory
    assert "advisory" in body.lower()
    # share-safe: no absolute path (the registry home) and no raw payload leak
    assert live.home not in body
    assert _RECORD_PAYLOAD_SECRET not in body
    # inert: no script element, no remote asset
    low = body.lower()
    assert "<script" not in low
    assert "http://" not in body and "https://" not in body
    assert "src=" not in low
    # no blended/overall score
    assert "overall_score" not in low and "blended" not in low


def test_records_json_mirror(live):
    rid, record = _seed_record(live.home)
    # list mirror
    code, body, headers = _req(live.base, "/records?format=json", token=live.token)
    assert code == 200
    assert "application/json" in headers.get("Content-Type", "")
    m = json.loads(body)
    assert m["view"] == "failure_records"
    assert any(r["record_id_ref"] == rid for r in m["records"])
    # detail mirror IS the canonical record (one source of truth for both surfaces)
    code, body, headers = _req(live.base, "/records/" + rid + "?format=json",
                               token=live.token)
    assert code == 200
    assert "application/json" in headers.get("Content-Type", "")
    obj = json.loads(body)
    assert obj["kind"] == "hotato.failure-record.v1"
    assert obj["record_id"] == record["record_id"]
    assert set(obj["dimensions"]) == {
        "outcome", "policy", "conversation", "speech", "reliability"}


def test_unknown_record_is_404(live):
    code, _b, _h = _req(live.base, "/records/no-such-record", token=live.token)
    assert code == 404


def test_hostile_record_id_is_rejected(live):
    _seed_record(live.home)
    # percent-encoded traversal / separators never resolve outside the records
    # root: each is refused (404), never a 200 that discloses another file.
    for bad in ["/records/%2e%2e%2f%2e%2e%2fetc%2fpasswd",
                "/records/%2e%2e",
                "/records/a%2fb",
                "/records/%2e%2e%2fapp.py"]:
        code, body, _h = _req(live.base, bad, token=live.token)
        assert code == 404, bad
        assert "root:" not in body                   # no /etc/passwd contents
        assert "def _record_detail" not in body      # no source file contents


def test_record_symlink_escape_is_rejected(live, tmp_path):
    # a symlink inside the records root that points OUTSIDE it is not followed:
    # even a valid record behind the link is refused (realpath containment).
    from hotato.failure_render import render_json
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "failure-record.json").write_text(
        render_json(_make_record()), encoding="utf-8")
    root = os.path.join(live.home, "records")
    os.makedirs(root, exist_ok=True)
    os.symlink(str(outside), os.path.join(root, "escape"))

    code, _b, _h = _req(live.base, "/records/escape", token=live.token)
    assert code == 404
    # and the escaped record is excluded from the list too
    _c, body, _h = _req(live.base, "/records", token=live.token)
    assert "escape" not in body
