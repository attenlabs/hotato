"""``POST /calls/<id>/pin`` -- serve's one write route (Wave A R9-R11).

Starts the real threaded server over an evidence database + score sidecar and
proves, against the live HTTP surface:

* pin success mints a contract through the EXISTING fleet label/contract
  machinery, and the bundle verifies under ``hotato.contract.verify_contracts``
  (re-scored, authenticated, and green on its own recording);
* every refusal -- unknown call, bad candidate ref, bad expect, an unscorable
  (NOT_SCORABLE) call, a stale ``evidence_sha256``, a recording missing from
  disk -- is an HTTP 4xx carrying its reason and leaves NO artifact: no
  bundle on disk, no contracts/labels row, candidate status untouched;
* an unauthenticated POST is 401 and never routed;
* the CSRF fence: a cookie-authenticated POST without a same-origin
  ``Origin``/``Referer`` header (a forged cross-site form) is refused 403,
  while the same POST with the page's own origin succeeds; bearer-token
  POSTs need no origin (a cross-site attacker cannot set that header);
* accepted AND refused attempts each land one line in the append-only audit
  log;
* the ``/calls`` feed header counts contracts protecting this agent from the
  fleet registry's contracts table, and a pin moves the count;
* R10: the nav reads Calls · Suite health · Failure clusters · Failure
  records · Release readiness, and ``/health`` is labeled Suite health;
* R11: a finalized (COMPLETE) session's per-call view carries the exact
  ``hotato production export-regression`` command; a non-finalized session's
  view carries none.
"""
from __future__ import annotations

import glob
import json
import os
import re
import shutil
import threading
import time
import urllib.error
import urllib.request
from importlib import resources
from urllib.parse import urlencode

import pytest

from hotato.console_store import ConsoleStore
from hotato.console_worker import default_console_path, rebuild_sidecar
from hotato.contract import verify_contracts
from hotato.fleet.registry import Registry
from hotato.production import ProductionStore
from hotato.serve import build_server
from hotato.serve.app import ServeContext
from hotato.serve.security import AuditLog, SessionStore

_TOKEN = "tok_pin_TEST_0123456789_xyz"
_KEY = "test-pin-signing-key-abc123"


# =========================================================================
# fixture: an evidence db + sidecar with a pinnable call
# =========================================================================

def _stereo_fixture() -> str:
    return str(
        resources.files("hotato").joinpath(
            "data", "audio", "01-hard-interruption.example.wav"
        )
    )


def _event(event_id, event_type, *, subject, time_value, sequence, data=None,
           authority="adapter_reported"):
    return {
        "specversion": "1.0",
        "id": event_id,
        "source": "pin-fixture",
        "type": event_type,
        "subject": subject,
        "time": time_value,
        "sequence": sequence,
        "data": {} if data is None else data,
        "authority": {
            "kind": authority,
            "eligible_for_execution_claim": authority
            in ("measured", "signed_attestation"),
        },
    }


def _ingest_call(store, subject, *, seq_base, audio_path=None,
                 with_media_event=True):
    """One fully-sequenced call session (lifecycle + audio asset + a timed
    turn), so ``finalize`` can reach COMPLETE."""
    seq = iter(range(seq_base, seq_base + 10))
    events = [
        _event(f"{subject}-start", "session.started", subject=subject,
               time_value="2026-07-17T12:00:00Z", sequence=next(seq)),
    ]
    if with_media_event:
        data = {"availability": "available", "channels": 2}
        if audio_path is not None:
            data["path"] = audio_path
        events.append(_event(
            f"{subject}-audio", "media.asset.available", subject=subject,
            time_value="2026-07-17T12:00:00.500Z", sequence=next(seq),
            data=data, authority="measured"))
    events.extend([
        _event(f"{subject}-turn-a", "turn.started", subject=subject,
               time_value="2026-07-17T12:00:01Z", sequence=next(seq)),
        _event(f"{subject}-turn-b", "turn.ended", subject=subject,
               time_value="2026-07-17T12:00:03.500Z", sequence=next(seq)),
        _event(f"{subject}-end", "session.ended", subject=subject,
               time_value="2026-07-17T12:00:06Z", sequence=next(seq)),
    ])
    for event in events:
        store.ingest(event, redact_payloads=False)


def _seed_evidence(tmp_path) -> str:
    """Three sessions: ``call-pin`` (SCORED, finalized COMPLETE, pinnable),
    ``call-open`` (SCORED, still QUIESCENT), and ``call-noaudio``
    (NOT_SCORABLE). Returns the evidence db path."""
    db_path = str(tmp_path / "production.sqlite3")
    movable = str(tmp_path / "movable.wav")
    shutil.copy(_stereo_fixture(), movable)

    clock = [1000.0]
    evidence = ProductionStore(db_path, clock=lambda: clock[0])
    _ingest_call(evidence, "call-pin", seq_base=1,
                 audio_path=_stereo_fixture())
    # finalize BEFORE the later arrivals so only call-pin quiesces to COMPLETE
    evidence.finalize(quiescence_seconds=0, now=1500.0,
                      required_lanes=("participant_audio",))
    clock[0] = 2000.0
    _ingest_call(evidence, "call-open", seq_base=101, audio_path=movable)
    clock[0] = 3000.0
    _ingest_call(evidence, "call-noaudio", seq_base=201,
                 with_media_event=False)
    evidence.close()

    store = ConsoleStore(default_console_path(db_path))
    try:
        summary = rebuild_sidecar(db_path, store)
    finally:
        store.close()
    assert summary["scored"] == 2
    assert summary["not_scorable"] == 1
    return db_path


class _Live:
    def __init__(self, base, token, home, production_db, audit_path, server,
                 thread):
        self.base = base
        self.token = token
        self.home = home
        self.production_db = production_db
        self.audit_path = audit_path
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
    audit_path = os.path.join(state_dir, "audit.jsonl")
    ctx = ServeContext(
        home=home, workspace="default",
        store_root=os.path.join(home, "artifacts"), token=_TOKEN,
        state_dir=state_dir, audit=AuditLog(audit_path),
        sessions=SessionStore(), bind_host="127.0.0.1",
        production_db=production_db)
    server = build_server(ctx, "127.0.0.1", 0)
    port = server.server_address[1]
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return _Live("http://127.0.0.1:%d" % port, _TOKEN, home, production_db,
                 audit_path, server, thread)


@pytest.fixture()
def live(tmp_path, monkeypatch):
    monkeypatch.setenv("HOTATO_ATTEST_KEY", _KEY)
    monkeypatch.setenv("HOTATO_REVIEWER", "pin-reviewer")
    production_db = _seed_evidence(tmp_path)
    server = _start_server(str(tmp_path / "fleet"), production_db)
    try:
        yield server
    finally:
        server.stop()


# =========================================================================
# helpers
# =========================================================================

def _req(base, path, *, method="GET", token=None, cookie=None, headers=None,
         form=None):
    body = urlencode(form).encode("utf-8") if form is not None else None
    req = urllib.request.Request(base + path, data=body, method=method)
    if form is not None:
        req.add_header("Content-Type", "application/x-www-form-urlencoded")
    if token is not None:
        req.add_header("Authorization", "Bearer " + token)
    if cookie is not None:
        req.add_header("Cookie", cookie)
    for key, value in (headers or {}).items():
        req.add_header(key, value)
    try:
        resp = urllib.request.urlopen(req, timeout=10)
        return resp.getcode(), resp.read().decode("utf-8"), dict(resp.headers)
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read().decode("utf-8"), dict(exc.headers)


def _json(base, path, token):
    code, body, _h = _req(base, path, token=token)
    assert code == 200, path
    return json.loads(body)


def _score(live, subject):
    return _json(live.base, "/calls/%s?format=json" % subject,
                 live.token)["score"]


def _pin_form(live, subject, *, candidate="0", expect="yield", sha=None):
    if sha is None:
        sha = _score(live, subject)["evidence_sha256"]
    return {"candidate": candidate, "expect": expect, "evidence_sha256": sha}


def _bundles(live):
    return sorted(glob.glob(os.path.join(live.home, "contracts", "default",
                                         "*.hotato")))


def _contract_rows(live):
    reg = Registry(home=live.home)
    try:
        rows = reg._all(
            "SELECT contract_id, label_id, agent_id FROM contracts "
            "WHERE workspace_id='default'", ())
        labels = reg._all(
            "SELECT label_id FROM labels WHERE workspace_id='default'", ())
    finally:
        reg.close()
    return [dict(r) for r in rows], [dict(r) for r in labels]


def _audit_lines(live):
    time.sleep(0.05)  # give the append a beat under the threaded server
    with open(live.audit_path, "r", encoding="utf-8") as fh:
        return [json.loads(line) for line in fh if line.strip()]


def _assert_nothing_written(live):
    rows, labels = _contract_rows(live)
    assert rows == [] and labels == []
    assert _bundles(live) == []


# =========================================================================
# auth + CSRF fence (I7)
# =========================================================================

def test_unauthenticated_pin_post_is_401_and_not_routed(live):
    code, body, headers = _req(live.base, "/calls/call-pin/pin", method="POST",
                               form=_pin_form(live, "call-pin"))
    assert code == 401
    assert "bearer" in headers.get("WWW-Authenticate", "").lower()
    assert "contract" not in body.lower() or "token" in body.lower()
    _assert_nothing_written(live)


def _session_cookie(live):
    """Mint a browser session exactly as a person does: open ``/?token=…``
    once and keep the HttpOnly cookie from the redirect."""
    class _NoRedirect(urllib.request.HTTPRedirectHandler):
        def redirect_request(self, *a, **k):
            return None

    req = urllib.request.Request(live.base + "/?token=" + live.token)
    opener = urllib.request.build_opener(_NoRedirect())
    try:
        resp = opener.open(req, timeout=5)
        headers = resp.headers
    except urllib.error.HTTPError as exc:
        headers = exc.headers
    set_cookie = headers.get("Set-Cookie", "")
    assert "hotato_session=" in set_cookie
    sid = set_cookie.split("hotato_session=", 1)[1].split(";", 1)[0]
    return "hotato_session=" + sid


def test_forged_cross_origin_cookie_post_is_refused(live):
    cookie = _session_cookie(live)
    form = _pin_form(live, "call-pin")

    # a cross-site form post: cookie present, Origin absent -> refused closed
    code, body, _h = _req(live.base, "/calls/call-pin/pin", method="POST",
                          cookie=cookie, form=form)
    assert code == 403
    assert "cross-origin" in body.lower()
    # a cross-site form post from a foreign origin -> refused
    code, _b, _h = _req(live.base, "/calls/call-pin/pin", method="POST",
                        cookie=cookie, form=form,
                        headers={"Origin": "http://evil.example"})
    assert code == 403
    _assert_nothing_written(live)
    # the refusals are audited
    assert [r for r in _audit_lines(live)
            if r["method"] == "POST" and r["status"] == 403]

    # the page's own origin passes the fence and pins
    origin = live.base
    code, body, _h = _req(live.base, "/calls/call-pin/pin", method="POST",
                          cookie=cookie, form=form,
                          headers={"Origin": origin})
    assert code == 200
    assert "ct-cand-" in body


# =========================================================================
# pin success (R9): the existing machinery, a verifiable artifact
# =========================================================================

def test_pin_mints_a_contract_the_existing_verify_logic_proves(live):
    code, body, _h = _req(live.base, "/calls/call-pin/pin?format=json",
                          method="POST", token=live.token,
                          form=_pin_form(live, "call-pin"))
    assert code == 200, body
    result = json.loads(body)
    assert result["view"] == "pin_result"
    assert result["delegated_to"] == "fleet.contract_from_candidate"
    assert result["contract_id"].startswith("ct-cand-")
    assert os.path.isdir(result["dir"])

    # the bundle verifies under the EXISTING contract-verify logic: re-scored
    # green on its own recording and authenticated (HMAC key in the fixture)
    proof = verify_contracts(result["dir"])
    assert proof["count"] == 1
    assert proof["exit_code"] == 0
    checked = proof["results"][0]
    assert checked["passed"] is True
    assert checked["scorable"] is True
    assert checked.get("authenticity") == "authenticated"

    # the sealed contract carries the label identity the fleet row does
    with open(os.path.join(result["dir"], "contract.json"),
              encoding="utf-8") as fh:
        contract = json.load(fh)
    assert contract["identity"]["reviewer"] == "pin-reviewer"
    assert contract["source"]["candidate_ref"] == \
        result["contract_id"][len("ct-"):]

    # registered in the fleet registry: one contract, one label
    rows, labels = _contract_rows(live)
    assert [r["contract_id"] for r in rows] == [result["contract_id"]]
    assert rows[0]["label_id"] == result["label_id"]
    assert rows[0]["agent_id"] == "production"
    assert [l["label_id"] for l in labels] == [result["label_id"]]

    # the accepted attempt is audited through the same append-only path
    assert [r for r in _audit_lines(live)
            if r["method"] == "POST" and r["path"] == "/calls/call-pin/pin"
            and r["status"] == 200]


def test_feed_header_counts_contracts_from_the_registry(live):
    before = _json(live.base, "/calls?format=json", live.token)["contracts"]
    assert before == {"count": 0, "source": "fleet-registry:contracts",
                      "scope": "workspace"}
    code, body, _h = _req(live.base, "/calls", token=live.token)
    assert code == 200
    assert "contracts protecting this agent" in body

    code, _b, _h = _req(live.base, "/calls/call-pin/pin?format=json",
                        method="POST", token=live.token,
                        form=_pin_form(live, "call-pin"))
    assert code == 200

    after = _json(live.base, "/calls?format=json", live.token)["contracts"]
    assert after["count"] == 1
    code, body, _h = _req(live.base, "/calls", token=live.token)
    assert ">1</b> contracts protecting this agent" in body


def test_pin_is_idempotent_on_the_same_candidate(live):
    form = _pin_form(live, "call-pin")
    first = _req(live.base, "/calls/call-pin/pin?format=json", method="POST",
                 token=live.token, form=form)
    second = _req(live.base, "/calls/call-pin/pin?format=json", method="POST",
                  token=live.token, form=form)
    assert first[0] == 200 and second[0] == 200
    assert (json.loads(first[1])["contract_id"]
            == json.loads(second[1])["contract_id"])
    rows, labels = _contract_rows(live)
    assert len(rows) == 1 and len(labels) == 1


def test_pin_form_and_result_work_without_javascript(live):
    # the per-call page carries a plain HTML form per top-ranked moment
    code, body, _h = _req(live.base, "/calls/call-pin", token=live.token)
    assert code == 200
    assert 'method="post" action="/calls/call-pin/pin"' in body
    assert 'name="evidence_sha256"' in body
    assert 'name="candidate"' in body
    assert "Pin to contract" in body

    # a plain form POST (no JS, no ?format=json) renders the result page
    code, body, _h = _req(live.base, "/calls/call-pin/pin", method="POST",
                          token=live.token, form=_pin_form(live, "call-pin"))
    assert code == 200
    assert "Pinned to contract" in body
    assert "ct-cand-" in body
    assert ".hotato" in body            # the bundle path is shown


# =========================================================================
# refusals: 4xx with reason, never a partial artifact (I2 / fail-closed)
# =========================================================================

def test_pin_refusals_are_4xx_with_reason_and_no_artifact(live):
    sha = _score(live, "call-pin")["evidence_sha256"]

    def post(path, form):
        code, body, _h = _req(live.base, path + "?format=json", method="POST",
                              token=live.token, form=form)
        return code, json.loads(body)

    # unknown call
    code, m = post("/calls/nope/pin",
                   {"candidate": "0", "expect": "yield",
                    "evidence_sha256": "x"})
    assert code == 404 and "nope" in m["message"]

    # bad candidate refs
    code, m = post("/calls/call-pin/pin",
                   {"candidate": "99", "expect": "yield",
                    "evidence_sha256": sha})
    assert code == 404 and "no candidate #99" in m["message"]
    code, m = post("/calls/call-pin/pin",
                   {"candidate": "snack", "expect": "yield",
                    "evidence_sha256": sha})
    assert code == 400 and "candidate" in m["message"]

    # bad expect
    code, m = post("/calls/call-pin/pin",
                   {"candidate": "0", "expect": "maybe",
                    "evidence_sha256": sha})
    assert code == 400 and "yield" in m["message"]

    # an unscorable call refuses with the scorer's own state + reason
    noaudio = _score(live, "call-noaudio")
    code, m = post("/calls/call-noaudio/pin",
                   {"candidate": "0", "expect": "yield",
                    "evidence_sha256": noaudio["evidence_sha256"]})
    assert code == 409
    assert "NOT_SCORABLE" in m["message"]

    # a stale evidence binding refuses (the sidecar the page rendered from
    # is not the sidecar on disk any more)
    code, m = post("/calls/call-pin/pin",
                   {"candidate": "0", "expect": "yield",
                    "evidence_sha256": "sha256:" + "0" * 64})
    assert code == 409 and "changed" in m["message"]

    _assert_nothing_written(live)
    # every refusal above is audited with its status
    statuses = {r["status"] for r in _audit_lines(live)
                if r["method"] == "POST"}
    assert {404, 400, 409} <= statuses


def test_pin_refuses_when_the_recording_left_the_disk(live, tmp_path):
    form = _pin_form(live, "call-open")
    os.remove(str(tmp_path / "movable.wav"))
    code, body, _h = _req(live.base, "/calls/call-open/pin?format=json",
                          method="POST", token=live.token, form=form)
    assert code == 409
    assert "not a readable file" in json.loads(body)["message"]
    _assert_nothing_written(live)


def test_pin_bad_body_is_400(live):
    req = urllib.request.Request(
        live.base + "/calls/call-pin/pin?format=json",
        data=b'{"candidate": 0}', method="POST")
    req.add_header("Authorization", "Bearer " + live.token)
    req.add_header("Content-Type", "application/json")
    try:
        resp = urllib.request.urlopen(req, timeout=10)
        code = resp.getcode()
    except urllib.error.HTTPError as exc:
        code = exc.code
    assert code == 400
    _assert_nothing_written(live)


def test_get_views_stay_read_only(live):
    # exercising every read view leaves the fleet registry without a single
    # contract/label row -- the pin POST is the one write path
    for path in ["/calls", "/calls/call-pin", "/", "/health", "/clusters",
                 "/records", "/scenarios"]:
        code, _b, _h = _req(live.base, path, token=live.token)
        assert code == 200, path
    _assert_nothing_written(live)


# =========================================================================
# R10: naming honesty + nav order
# =========================================================================

def test_nav_reads_as_one_product_in_order(live):
    code, body, _h = _req(live.base, "/calls", token=live.token)
    assert code == 200
    nav = re.search(r'<nav class="tabs">(.*?)</nav>', body, re.S).group(1)
    labels = re.findall(r">([^<>]+)</a>", nav)
    assert labels == ["Calls", "Suite health", "Failure clusters",
                      "Failure records", "Release readiness"]


def test_suite_health_label_replaces_production_health(live):
    for path in ["/health", "/production"]:
        code, body, _h = _req(live.base, path, token=live.token)
        assert code == 200, path
        assert "Suite health" in body
        assert "Production health" not in body
    model = _json(live.base, "/health?format=json", live.token)
    assert model["view"] == "suite_health"


# =========================================================================
# R11: the export-regression path, surfaced only where it holds
# =========================================================================

def test_finalized_call_links_the_export_regression_command(live):
    model = _json(live.base, "/calls/call-pin?format=json", live.token)
    assert model["session"]["state"] == "COMPLETE"
    export = model["session"]["export_regression"]
    assert export["command"].startswith(
        "hotato production export-regression call-pin --out ")
    assert "--db" in export["command"]
    code, body, _h = _req(live.base, "/calls/call-pin", token=live.token)
    assert code == 200
    assert "export-regression call-pin" in body


def test_non_finalized_call_carries_no_export_reference(live):
    model = _json(live.base, "/calls/call-open?format=json", live.token)
    assert model["session"]["state"] == "QUIESCENT"
    assert "export_regression" not in model["session"]
    code, body, _h = _req(live.base, "/calls/call-open", token=live.token)
    assert code == 200
    assert "export-regression" not in body
