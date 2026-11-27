"""hotato cloud (the optional team app). Skips cleanly without the extra."""

from __future__ import annotations

import datetime as dt
import re

import pytest

for _mod in ("fastapi", "sqlalchemy", "jinja2", "argon2", "cryptography", "httpx", "multipart"):
    pytest.importorskip(_mod)

from fastapi.testclient import TestClient  # noqa: E402

from hotato.cloud import db as m  # noqa: E402
from hotato.cloud.app import create_app  # noqa: E402
from hotato.cloud.config import ConfigError, Settings  # noqa: E402

SECRET = "test-secret-test-secret-test-secret"
PW = "correct horse battery"
CSRF_RE = re.compile(r'name="csrf_token" value="([0-9a-f]+)"')


def make_app(**kw):
    return create_app(Settings.from_env(secret=SECRET, database_url="sqlite://",
                                        allow_signup=kw.pop("allow_signup", True)), **kw)


class Browser:
    def __init__(self, app, base="http://testserver"):
        self.app = app
        self.c = TestClient(app, base_url=base, follow_redirects=False)

    def csrf(self, path="/login"):
        r = self.c.get(path)
        return CSRF_RE.search(r.text).group(1)

    def post(self, path, data=None, form_page=None, **kw):
        data = dict(data or {})
        data.setdefault("csrf_token", self.csrf(form_page or path.split("?")[0]))
        return self.c.post(path, data=data, **kw)

    def signup(self, email, org="Acme"):
        r = self.post("/signup", {"email": email, "password": PW, "org_name": org})
        assert r.status_code == 303, r.text
        return re.search(r"/p/([0-9a-f]+)/", r.headers["location"]).group(1)

    def page(self, path):
        return self.c.get(path)


def now_iso(delta_h=0):
    return (dt.datetime.now(dt.timezone.utc) - dt.timedelta(hours=delta_h)).isoformat()


def make_event(call_id="c1", **kw):
    ev = {"schema": "hotato.monitorevent.v1", "call_id": call_id, "stack": "vapi",
          "agent_id": "agent-a", "started_at": now_iso(1), "duration_s": 61.5, "score": 0.8,
          "incidents": [{"type": "barge_in", "severity": "high", "at_s": 12.5, "detail": "talked over"}],
          "alerts_fired": ["rule-1"], "report_path": "/tmp/report.html", "scored_at": now_iso(0)}
    ev.update(kw)
    return ev


def make_testrun(run_id="run-1"):
    return {
        "schema": "hotato.testrun.v1", "run_id": run_id, "started_at": now_iso(2),
        "finished_at": now_iso(1), "agent": {"stack": "vapi", "id": "agent-a"},
        "judge": {"provider": "ollama", "model": "m1"},
        "cases": [{
            "case_id": "refund", "name": "Refund request", "persona": "impatient caller",
            "goal": "get a refund", "repeats": 2, "passed": 1, "pass_rate": 0.5, "pass_all": False,
            "criteria": [{"name": "confirms refund", "pass_rate": 0.5}],
            "runs": [{"run_index": 0, "tool_calls": [],
                      "transcript": [{"role": "caller", "text": "I want a refund", "t": 0.5},
                                     {"role": "agent", "text": "Sure, done", "t": 2.0}],
                      "verdicts": [{"criterion": "confirms refund", "verdict": "pass",
                                    "confidence": 0.9, "evidence": "Sure, done",
                                    "explanation": "agent confirmed"}], "error": None},
                     {"run_index": 1, "tool_calls": [], "transcript": [],
                      "verdicts": [{"criterion": "confirms refund", "verdict": "fail",
                                    "confidence": 0.7, "evidence": "",
                                    "explanation": "never confirmed"}], "error": "timeout"}]}],
        "summary": {"cases": 1, "pass_rate": 0.5, "pass_all": 0}}


def make_token(b, pid, name="ci"):
    r = b.post(f"/p/{pid}/settings/tokens", {"name": name}, form_page=f"/p/{pid}/settings")
    assert r.status_code == 200
    return re.search(r"hto_[A-Za-z0-9_\-]+", r.text).group(0)


def auth(tok):
    return {"Authorization": f"Bearer {tok}"}


@pytest.fixture()
def app():
    return make_app()


@pytest.fixture()
def alice(app):
    b = Browser(app)
    b.pid = b.signup("alice@example.com", "Alice Org")
    return b


# --------------------------------------------------------------------- config

def test_refuses_to_start_without_secret(monkeypatch):
    monkeypatch.delenv("HOTATO_CLOUD_SECRET", raising=False)
    with pytest.raises(ConfigError):
        create_app()
    monkeypatch.setenv("HOTATO_CLOUD_SECRET", "short")
    with pytest.raises(ConfigError):
        create_app()


def test_healthz_and_security_headers(app):
    r = TestClient(app).get("/healthz")
    assert r.status_code == 200 and r.json() == {"ok": True}
    assert "frame-ancestors 'none'" in r.headers["content-security-policy"]
    assert r.headers["x-frame-options"] == "DENY"
    assert r.headers["x-content-type-options"] == "nosniff"


# ----------------------------------------------------------------------- auth

def test_signup_then_every_page_renders_empty(alice):
    p = f"/p/{alice.pid}"
    for path in ("/", f"{p}/", f"{p}/calls", f"{p}/tests", f"{p}/alerts", f"{p}/settings"):
        r = alice.page(path)
        assert r.status_code == 200, path
    assert "hotato cloud push" in alice.page(f"{p}/").text
    assert "hotato cloud push" in alice.page(f"{p}/calls").text
    assert "hotato cloud push" in alice.page(f"{p}/tests").text
    assert alice.page("/static/hotato.css").status_code == 200


def test_anonymous_redirects_to_login(app):
    c = TestClient(app, follow_redirects=False)
    r = c.get("/")
    assert r.status_code == 303 and r.headers["location"].startswith("/login")


def test_login_logout_and_wrong_password(app, alice):
    anon = Browser(app)
    r = anon.post("/login", {"email": "alice@example.com", "password": "nope-nope-nope"})
    assert r.status_code == 401
    r = anon.post("/login", {"email": "alice@example.com", "password": PW, "next": "/"})
    assert r.status_code == 303
    assert anon.page("/").status_code in (200, 303)
    assert anon.post("/logout", form_page="/").status_code == 303
    assert anon.page("/").status_code == 303


def test_open_redirect_blocked(app, alice):
    anon = Browser(app)
    r = anon.post("/login", {"email": "alice@example.com", "password": PW,
                             "next": "https://evil.example/"})
    assert r.headers["location"] == "/"
    r = Browser(app).post("/login", {"email": "alice@example.com", "password": PW,
                                     "next": "//evil.example"})
    assert r.headers["location"] == "/"


def test_login_is_rate_limited(app, alice):
    anon = Browser(app)
    codes = [anon.post("/login", {"email": "alice@example.com", "password": f"bad-pass-{i}-xxx"},
                       form_page="/login").status_code for i in range(10)]
    assert 429 in codes
    # even the right password is refused while throttled
    assert anon.post("/login", {"email": "alice@example.com", "password": PW}).status_code == 429


def test_session_cookie_flags(app):
    b = Browser(app, base="https://testserver")
    r = b.post("/signup", {"email": "s@example.com", "password": PW, "org_name": "S"})
    cookie = r.headers["set-cookie"].lower()
    assert "httponly" in cookie and "samesite=lax" in cookie and "secure" in cookie
    assert "strict-transport-security" in r.headers
    plain = Browser(make_app())
    r = plain.post("/signup", {"email": "p@example.com", "password": PW, "org_name": "P"})
    assert "secure" not in r.headers["set-cookie"].lower()


def test_signup_closed_when_disabled_after_first_user():
    app = make_app(allow_signup=False)
    first = Browser(app)
    first.signup("first@example.com")
    r = Browser(app).c.get("/signup")
    assert "closed" in r.text
    b = Browser(app)
    b.c.get("/login")
    assert b.c.post("/signup", data={"email": "x@example.com", "password": PW, "org_name": "X",
                                     "csrf_token": b.csrf("/login")}).status_code == 403


def test_weak_password_and_duplicate_email(app, alice):
    b = Browser(app)
    r = b.post("/signup", {"email": "w@example.com", "password": "short", "org_name": "W"})
    assert r.status_code == 400
    r = b.post("/signup", {"email": "ALICE@example.com", "password": PW, "org_name": "W"})
    assert r.status_code == 400


# ----------------------------------------------------------------------- CSRF

def test_csrf_required_on_every_form(app, alice):
    anon = TestClient(app, follow_redirects=False)
    assert anon.post("/login", data={"email": "a", "password": "b"}).status_code == 403
    assert alice.c.post("/login", data={"email": "a", "password": "b",
                                        "csrf_token": "0" * 64}).status_code == 403
    p = f"/p/{alice.pid}"
    for path, data in ((f"{p}/settings/tokens", {"name": "x"}),
                       (f"{p}/alerts", {"rules_json": "[]"}),
                       (f"{p}/settings/invites", {"role": "member"}),
                       (f"{p}/settings/connections", {"provider": "vapi", "api_key": "k"}),
                       ("/logout", {})):
        assert alice.c.post(path, data=data).status_code == 403, path


def test_csrf_token_from_another_session_rejected(app, alice):
    other = Browser(app)
    other.signup("other@example.com", "Other")
    tok = other.csrf("/")
    r = alice.c.post(f"/p/{alice.pid}/settings/tokens", data={"name": "x", "csrf_token": tok})
    assert r.status_code == 403


def test_cross_origin_post_refused(app, alice):
    t = alice.csrf(f"/p/{alice.pid}/settings")
    r = alice.c.post(f"/p/{alice.pid}/settings/tokens", data={"name": "x", "csrf_token": t},
                     headers={"Origin": "https://evil.example"})
    assert r.status_code == 403


# --------------------------------------------------------------------- ingest

def test_ingest_testrun_and_pages(alice):
    p = f"/p/{alice.pid}"
    tok = make_token(alice, alice.pid)
    r = alice.c.post(f"/api/v1/projects/{alice.pid}/testruns", json=make_testrun(), headers=auth(tok))
    assert r.status_code == 201 and r.json()["replaced"] is False
    # replaying the same run replaces it
    r = alice.c.post(f"/api/v1/projects/{alice.pid}/testruns", json=make_testrun(), headers=auth(tok))
    assert r.json()["replaced"] is True
    listing = alice.page(f"{p}/tests").text
    assert "run-1" in listing and "50%" in listing
    run_pk = re.search(rf"{p}/tests/([0-9a-f]+)", listing).group(1)
    detail = alice.page(f"{p}/tests/{run_pk}").text
    assert "Refund request" in detail
    case_pk = re.search(rf"{p}/tests/{run_pk}/cases/([0-9a-f]+)", detail).group(1)
    case = alice.page(f"{p}/tests/{run_pk}/cases/{case_pk}").text
    for needle in ("I want a refund", "never confirmed", "Sure, done", "90%", "timeout",
                   "confirms refund", "agent confirmed"):
        assert needle in case, needle
    ov = alice.page(f"{p}/").text
    assert "50%" in ov


def test_ingest_events_and_call_pages(alice):
    p = f"/p/{alice.pid}"
    tok = make_token(alice, alice.pid)
    url = f"/api/v1/projects/{alice.pid}/events"
    assert alice.c.post(url, json=make_event("c1"), headers=auth(tok)).json()["created"] == 1
    r = alice.c.post(url, json=[make_event("c2", score=None, duration_s=None, agent_id=None,
                                           started_at=None, incidents=[], report_path=None),
                                make_event("c1", score=0.1)], headers=auth(tok))
    assert r.json() == {"ok": True, "created": 1, "updated": 1}
    calls = alice.page(f"{p}/calls").text
    assert "agent-a" in calls and "barge_in" in calls
    assert "0.10" in calls
    cid = re.search(rf"{p}/calls/([0-9a-f]+)", calls).group(1)
    assert alice.page(f"{p}/calls/{cid}").status_code == 200
    ov = alice.page(f"{p}/").text
    assert "barge_in" in ov and "<svg" in ov or "not enough data" in ov
    # filters
    only = alice.page(f"{p}/calls?only_incidents=1&incident_type=barge_in&score_below=0.5").text
    assert "agent-a" in only
    none = alice.page(f"{p}/calls?incident_type=nothing_like_this").text
    assert "No calls here" in none
    assert alice.page(f"{p}/calls?agent=agent-a&since=all&page=3").status_code == 200


def test_ingest_validation_rejects(alice):
    tok = make_token(alice, alice.pid)
    ev_url = f"/api/v1/projects/{alice.pid}/events"
    tr_url = f"/api/v1/projects/{alice.pid}/testruns"
    h = auth(tok)
    bad = [
        (ev_url, make_event(schema="hotato.monitorevent.v2")),
        (ev_url, {k: v for k, v in make_event().items() if k != "call_id"}),
        (ev_url, make_event(score="high")),
        (ev_url, make_event(duration_s=True)),
        (ev_url, make_event(scored_at="yesterday")),
        (ev_url, make_event(incidents=[{"type": "x"}])),
        (ev_url, [make_event("partial-zzq"), make_event("bad", score="x")]),
        (ev_url, "text"),
        (tr_url, make_testrun() | {"schema": "hotato.testrun.v9"}),
        (tr_url, make_event()),
    ]
    t = make_testrun()
    t["cases"][0]["runs"][0]["verdicts"][0]["verdict"] = "maybe"
    bad.append((tr_url, t))
    t = make_testrun()
    t["cases"][0]["pass_rate"] = 3
    bad.append((tr_url, t))
    t = make_testrun()
    del t["judge"]
    bad.append((tr_url, t))
    t = make_testrun()
    t["cases"][0]["runs"][0]["transcript"][0]["role"] = "robot"
    bad.append((tr_url, t))
    for url, body in bad:
        r = alice.c.post(url, json=body, headers=h)
        assert r.status_code == 422, (url, body, r.text)
        assert r.json()["error"] == "invalid_artifact"
    # the all-or-nothing batch left nothing behind
    assert "No calls here" in alice.page(f"/p/{alice.pid}/calls?since=all").text
    assert alice.c.post(ev_url, content=b"{not json", headers=h).status_code == 400


def test_ingest_requires_valid_project_token(app, alice):
    url = f"/api/v1/projects/{alice.pid}/events"
    c = TestClient(app)
    assert c.post(url, json=make_event()).status_code == 401
    assert c.post(url, json=make_event(), headers=auth("hto_nope")).status_code == 401
    # a browser session is not an API credential
    assert alice.c.post(url, json=make_event()).status_code == 401
    tok = make_token(alice, alice.pid)
    tid = re.search(r"settings/tokens/([0-9a-f]+)/revoke", alice.page(f"/p/{alice.pid}/settings").text).group(1)
    assert alice.post(f"/p/{alice.pid}/settings/tokens/{tid}/revoke",
                      form_page=f"/p/{alice.pid}/settings").status_code == 303
    assert c.post(url, json=make_event(), headers=auth(tok)).status_code == 401


def test_body_size_limit(alice):
    tok = make_token(alice, alice.pid)
    alice.app.state.settings = Settings.from_env(secret=SECRET, database_url="sqlite://",
                                                 allow_signup=True)
    object.__setattr__(alice.app.state.settings, "max_body_bytes", 1000)
    r = alice.c.post(f"/api/v1/projects/{alice.pid}/events", json=[make_event(f"c{i}") for i in range(20)],
                     headers=auth(tok))
    assert r.status_code == 413


def test_tokens_are_hashed_at_rest(app, alice):
    tok = make_token(alice, alice.pid)
    with app.state.sessions() as db:
        rows = db.query(m.ApiToken).all()
        assert rows and all(tok not in (r.token_hash + r.hint) for r in rows)
    assert tok not in alice.page(f"/p/{alice.pid}/settings").text


def test_stored_xss_is_escaped(alice):
    tok = make_token(alice, alice.pid)
    xss = '<script>alert(1)</script>'
    alice.c.post(f"/api/v1/projects/{alice.pid}/events", headers=auth(tok), json=make_event(
        "x1", agent_id=xss, incidents=[{"type": xss, "severity": xss, "at_s": 1, "detail": xss}],
        alerts_fired=[xss], report_path="javascript:alert(1)"))
    t = make_testrun()
    t["cases"][0]["name"] = xss
    t["cases"][0]["runs"][0]["transcript"][0]["text"] = xss
    alice.c.post(f"/api/v1/projects/{alice.pid}/testruns", headers=auth(tok), json=t)
    p = f"/p/{alice.pid}"
    pages = [f"{p}/", f"{p}/calls?since=all", f"{p}/tests"]
    calls = alice.page(f"{p}/calls?since=all").text
    pages.append(re.search(rf"{p}/calls/[0-9a-f]+", calls).group(0))
    tl = alice.page(f"{p}/tests").text
    run_pk = re.search(rf"{p}/tests/([0-9a-f]+)", tl).group(1)
    pages.append(f"{p}/tests/{run_pk}")
    dt_ = alice.page(f"{p}/tests/{run_pk}").text
    pages.append(re.search(rf"{p}/tests/{run_pk}/cases/[0-9a-f]+", dt_).group(0))
    for path in pages:
        body = alice.page(path).text
        assert "<script>alert(1)" not in body, path
    assert 'href="javascript:' not in alice.page(pages[3]).text


# ---------------------------------------------------------------------- rules

def test_rules_save_and_serve(alice):
    p = f"/p/{alice.pid}"
    rules = [{"id": "r1", "when": {"metric": "score", "op": "<", "value": 0.5}, "window": None,
              "channels": ["slack"], "cooldown_min": 30},
             {"id": "r2", "when": {"incident_type": "barge_in", "severity_at_least": "high"},
              "window": {"calls": 10, "min_hits": 2}, "channels": ["webhook", "pagerduty"],
              "cooldown_min": 0}]
    import json
    r = alice.post(f"{p}/alerts", {"rules_json": json.dumps(rules)}, form_page=f"{p}/alerts")
    assert r.status_code == 303
    page = alice.page(f"{p}/alerts?saved=1").text
    assert "Rules saved" in page and "r2" in page and "2 of the last 10 calls" in page
    tok = make_token(alice, alice.pid)
    got = alice.c.get(f"/api/v1/projects/{alice.pid}/rules", headers=auth(tok))
    assert got.status_code == 200 and got.json() == rules
    assert TestClient(alice.app).get(f"/api/v1/projects/{alice.pid}/rules").status_code == 401


@pytest.mark.parametrize("text", [
    "not json", "{}", '[{"id": "a"}]',
    '[{"id":"a","when":{"metric":"s","op":"~","value":1},"window":null,"channels":[],"cooldown_min":1}]',
    '[{"id":"a","when":{"metric":"s","op":"<","value":1},"window":null,"channels":["sms"],"cooldown_min":1}]',
    '[{"id":"a","when":{"metric":"s","op":"<","value":1},"window":{"calls":2,"min_hits":5},"channels":[],"cooldown_min":1}]',
    '[{"id":"a","when":{"metric":"s","op":"<","value":1},"window":null,"channels":[],"cooldown_min":1},'
    '{"id":"a","when":{"metric":"s","op":"<","value":1},"window":null,"channels":[],"cooldown_min":1}]',
])
def test_rules_validation(alice, text):
    p = f"/p/{alice.pid}"
    r = alice.post(f"{p}/alerts", {"rules_json": text}, form_page=f"{p}/alerts")
    assert r.status_code == 400 and 'role="alert"' in r.text


def test_rules_default_empty(alice):
    tok = make_token(alice, alice.pid)
    assert alice.c.get(f"/api/v1/projects/{alice.pid}/rules", headers=auth(tok)).json() == []


# ---------------------------------------------------------------- connections

def test_connection_encrypted_never_shown_and_testable(app):
    seen = {}

    def fake_probe(provider, key):
        seen[provider] = key
        return True, "Vapi answered HTTP 200"

    app = make_app(probe=fake_probe)
    b = Browser(app)
    pid = b.signup("c@example.com")
    p = f"/p/{pid}"
    key = "sk-live-SUPER-SECRET-KEY-123"
    assert b.post(f"{p}/settings/connections", {"provider": "vapi", "api_key": key},
                  form_page=f"{p}/settings").status_code == 303
    page = b.page(f"{p}/settings").text
    assert key not in page and "key saved" in page
    with app.state.sessions() as db:
        row = db.query(m.Connection).one()
        assert key not in row.secret_enc and app.state.vault.decrypt(row.secret_enc) == key
    assert b.post(f"{p}/settings/connections/vapi/test", form_page=f"{p}/settings").status_code == 303
    assert seen["vapi"] == key
    page = b.page(f"{p}/settings").text
    assert "connected" in page and key not in page
    assert b.post(f"{p}/settings/connections", {"provider": "other", "api_key": "x"},
                  form_page=f"{p}/settings").status_code == 400
    assert b.post(f"{p}/settings/connections/vapi/delete", form_page=f"{p}/settings").status_code == 303
    assert "no key saved" in b.page(f"{p}/settings").text


def test_connection_unreadable_after_secret_change():
    app = make_app()
    b = Browser(app)
    pid = b.signup("d@example.com")
    p = f"/p/{pid}"
    b.post(f"{p}/settings/connections", {"provider": "retell", "api_key": "k-1"}, form_page=f"{p}/settings")
    from hotato.cloud.security import Vault
    app.state.vault = Vault("a-different-secret-entirely-xx")
    b.post(f"{p}/settings/connections/retell/test", form_page=f"{p}/settings")
    page = b.page(f"{p}/settings").text
    assert "cannot be decrypted" in page


# --------------------------------------------------------- tenant isolation

def _seed(b):
    tok = make_token(b, b.pid)
    b.c.post(f"/api/v1/projects/{b.pid}/events", json=make_event("secret-call"), headers=auth(tok))
    b.c.post(f"/api/v1/projects/{b.pid}/testruns", json=make_testrun("secret-run"), headers=auth(tok))
    return tok


def test_one_org_cannot_read_or_write_another(app, alice):
    a_tok = _seed(alice)
    p = f"/p/{alice.pid}"
    calls = alice.page(f"{p}/calls?since=all").text
    call_pk = re.search(rf"{p}/calls/([0-9a-f]+)", calls).group(1)
    tl = alice.page(f"{p}/tests").text
    run_pk = re.search(rf"{p}/tests/([0-9a-f]+)", tl).group(1)
    case_pk = re.search(r"cases/([0-9a-f]+)", alice.page(f"{p}/tests/{run_pk}").text).group(1)

    bob = Browser(app)
    bob.pid = bob.signup("bob@example.com", "Bob Org")
    b_tok = _seed(bob)
    for path in (f"{p}/", f"{p}/calls", f"{p}/calls/{call_pk}", f"{p}/tests",
                 f"{p}/tests/{run_pk}", f"{p}/tests/{run_pk}/cases/{case_pk}", f"{p}/alerts",
                 f"{p}/settings"):
        r = bob.page(path)
        assert r.status_code == 404, path
        assert "secret-call" not in r.text
    # ids from A used under B's own project must not resolve either
    q = f"/p/{bob.pid}"
    assert bob.page(f"{q}/calls/{call_pk}").status_code == 404
    assert bob.page(f"{q}/tests/{run_pk}").status_code == 404
    assert bob.page(f"{q}/tests/{run_pk}/cases/{case_pk}").status_code == 404
    # B's listings hold only B's rows
    own = bob.page(f"{q}/calls?since=all").text
    assert own.count("agent-a") >= 1 and f"{p}/calls/" not in own
    assert "Alice Org" not in bob.page("/").text
    # B cannot mutate A's project
    assert bob.post(f"{p}/settings/tokens", {"name": "x"}, form_page=f"{q}/settings").status_code == 404
    assert bob.post(f"{p}/alerts", {"rules_json": "[]"}, form_page=f"{q}/alerts").status_code == 404
    assert bob.post(f"{p}/settings/invites", {"role": "owner"}, form_page=f"{q}/settings").status_code == 404
    assert bob.post(f"/orgs/{ 'x' * 24 }/projects", {"name": "n"}, form_page=f"{q}/settings").status_code == 404
    # tokens are bound to their project, both ways
    assert bob.c.post(f"/api/v1/projects/{alice.pid}/events", json=make_event("evil"),
                      headers=auth(b_tok)).status_code == 404
    assert bob.c.get(f"/api/v1/projects/{alice.pid}/rules", headers=auth(b_tok)).status_code == 404
    assert alice.c.post(f"/api/v1/projects/{bob.pid}/testruns", json=make_testrun("x"),
                        headers=auth(a_tok)).status_code == 404
    assert "evil" not in alice.page(f"{p}/calls?since=all").text
    # same call_id in two projects is two rows, not a collision
    alice.c.post(f"/api/v1/projects/{alice.pid}/events", json=make_event("shared", score=0.11),
                 headers=auth(a_tok))
    bob.c.post(f"/api/v1/projects/{bob.pid}/events", json=make_event("shared", score=0.99),
               headers=auth(b_tok))
    assert "0.11" in alice.page(f"{p}/calls?since=all").text
    assert "0.99" not in alice.page(f"{p}/calls?since=all").text


# ------------------------------------------------------------ invites + roles

def test_invite_flow_and_member_limits(app, alice):
    p = f"/p/{alice.pid}"
    r = alice.post(f"{p}/settings/invites", {"role": "member"}, form_page=f"{p}/settings")
    link = re.search(r"http://testserver(/invite/[A-Za-z0-9_\-]+)", r.text).group(1)
    carol = Browser(app)
    assert "Join Alice Org" in carol.page(link).text
    r = carol.post(link, {"email": "carol@example.com", "password": PW}, form_page=link)
    assert r.status_code == 303 and r.headers["location"] == f"{p}/"
    assert carol.page(f"{p}/").status_code == 200
    # single use
    assert Browser(app).page(link).text.count("Invite not valid") == 1
    # member: reads settings, cannot create tokens, invites, or connections
    st = carol.page(f"{p}/settings")
    assert st.status_code == 200 and "Only owners create tokens" in st.text
    for path, data in ((f"{p}/settings/tokens", {"name": "x"}), (f"{p}/settings/invites", {}),
                       (f"{p}/settings/connections", {"provider": "vapi", "api_key": "k"})):
        assert carol.post(path, data, form_page=f"{p}/settings").status_code == 403
    # member can edit alert rules (documented)
    assert carol.post(f"{p}/alerts", {"rules_json": "[]"}, form_page=f"{p}/alerts").status_code == 303
    # last owner cannot be removed
    with app.state.sessions() as db:
        uid = db.query(m.User).filter_by(email="alice@example.com").one().id
    r = alice.post(f"{p}/settings/members/{uid}/remove", form_page=f"{p}/settings")
    assert r.status_code == 400 and "at least one owner" in r.text


def test_expired_invite_rejected(app, alice):
    p = f"/p/{alice.pid}"
    r = alice.post(f"{p}/settings/invites", {"role": "member"}, form_page=f"{p}/settings")
    link = re.search(r"(/invite/[A-Za-z0-9_\-]+)", r.text).group(1)
    with app.state.sessions() as db:
        inv = db.query(m.Invite).one()
        inv.expires_at = m.utcnow() - dt.timedelta(days=1)
        db.commit()
    assert "Invite not valid" in Browser(app).page(link).text


def test_owner_can_create_project_and_switch(app, alice):
    with app.state.sessions() as db:
        org_id = db.query(m.Org).one().id
    assert alice.post(f"/orgs/{org_id}/projects", {"name": "Second"}, form_page="/").status_code == 303
    home = alice.page("/").text
    assert "Second" in home and "Default" in home
    assert "Switch project" in alice.page(f"/p/{alice.pid}/").text


def test_unknown_routes_are_404_pages(alice):
    r = alice.page("/p/does-not-exist/")
    assert r.status_code == 404 and "404" in r.text
    assert alice.page("/nope").status_code == 404


def test_pages_use_no_inline_style_or_script(alice):
    """The CSP forbids inline script and style, so no template may rely on them."""
    p = f"/p/{alice.pid}"
    tok = make_token(alice, alice.pid)
    alice.c.post(f"/api/v1/projects/{alice.pid}/events", json=make_event(), headers=auth(tok))
    alice.c.post(f"/api/v1/projects/{alice.pid}/testruns", json=make_testrun(), headers=auth(tok))
    for path in ("/", f"{p}/", f"{p}/calls", f"{p}/tests", f"{p}/alerts", f"{p}/settings",
                 "/login", "/signup"):
        body = alice.page(path).text
        assert ' style="' not in body, path
        assert not re.search(r"<script(?![^>]*(src=|type=\"text/plain\"))", body), path
        assert " onclick=" not in body
