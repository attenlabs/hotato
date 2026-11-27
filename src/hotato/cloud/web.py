"""Browser pages. Every route that touches a project goes through
``deps.project_ctx`` (membership-checked); every POST carries ``csrf_guard``."""

from __future__ import annotations

import datetime as dt
import json
import re
from typing import Optional

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import PlainTextResponse
from sqlalchemy import func, select
from sqlalchemy.orm import Session as DbSession

from . import connections, queries, schemas
from . import db as m
from .deps import (
    ANON_COOKIE,
    SESSION_COOKIE,
    ProjectCtx,
    client_ip,
    csrf_guard,
    current_user,
    get_db,
    note_attempt,
    owner_ctx,
    pretty_json,
    project_ctx,
    redirect,
    render,
    require_user,
    safe_next,
    start_session,
    throttled,
    user_projects,
)
from .security import (
    MIN_PASSWORD_LEN,
    TOKEN_PREFIX,
    hash_password,
    new_secret_token,
    sha256_hex,
    verify_password,
)

router = APIRouter()
_EMAIL = re.compile(r"^[^@\s]{1,64}@[^@\s]{1,255}\.[^@\s]{2,}$")

EXAMPLE_RULES = [{
    "id": "barge-in-spike",
    "when": {"incident_type": "barge_in", "severity_at_least": "high"},
    "window": {"calls": 20, "min_hits": 3},
    "channels": ["slack"],
    "cooldown_min": 60,
}, {
    "id": "low-score",
    "when": {"metric": "score", "op": "<", "value": 0.5},
    "window": None,
    "channels": ["webhook"],
    "cooldown_min": 30,
}]


# ------------------------------------------------------------- static + health

@router.get("/healthz")
def healthz(db: DbSession = Depends(get_db)):
    db.execute(select(1))
    return {"ok": True}


@router.get("/static/hotato.css")
def css(request: Request):
    return PlainTextResponse(request.app.state.css, media_type="text/css",
                             headers={"Cache-Control": "public, max-age=3600"})


@router.get("/static/app.js")
def js(request: Request):
    return PlainTextResponse(request.app.state.js, media_type="application/javascript",
                             headers={"Cache-Control": "public, max-age=3600"})


# ----------------------------------------------------------------- accounts

def _email_ok(email: str) -> bool:
    return bool(_EMAIL.match(email))


def _users_exist(db: DbSession) -> bool:
    return (db.scalar(select(func.count()).select_from(m.User)) or 0) > 0


@router.get("/login")
def login_form(request: Request, next: str = "/", user=Depends(current_user)):
    if user:
        return redirect(safe_next(next))
    return render(request, "login.html", next=safe_next(next), error=None)


@router.post("/login", dependencies=[Depends(csrf_guard)])
def login(request: Request, email: str = Form(""), password: str = Form(""),
          next: str = Form("/"), db: DbSession = Depends(get_db)):
    email = email.strip().lower()[:320]
    ip_key, em_key = f"login-ip:{client_ip(request)}", "login-em:" + sha256_hex(email)[:40]
    if throttled(db, ip_key, 20, 15) or throttled(db, em_key, 8, 15):
        return render(request, "login.html", 429, next=safe_next(next),
                      error="Too many sign-in attempts. Wait 15 minutes and try again.")
    u = db.scalar(select(m.User).where(m.User.email == email))
    ok = verify_password(u.pw_hash if u else None, password)
    if not (u and ok):
        note_attempt(db, ip_key)
        note_attempt(db, em_key)
        return render(request, "login.html", 401, next=safe_next(next),
                      error="Email or password is wrong.")
    resp = redirect(safe_next(next))
    start_session(request, db, u, resp)
    return resp


@router.post("/logout", dependencies=[Depends(csrf_guard)])
def logout(request: Request, db: DbSession = Depends(get_db)):
    raw = request.cookies.get(SESSION_COOKIE)
    if raw:
        row = db.get(m.Session, sha256_hex(raw))
        if row:
            db.delete(row)
            db.commit()
    resp = redirect("/login")
    resp.delete_cookie(SESSION_COOKIE, path="/")
    resp.delete_cookie(ANON_COOKIE, path="/")
    return resp


def _create_account(db: DbSession, email: str, password: str):
    u = m.User(email=email, pw_hash=hash_password(password))
    db.add(u)
    db.flush()
    return u


def create_org_with_project(db: DbSession, user: m.User, org_name: str, project_name="Default"):
    org = m.Org(name=org_name)
    db.add(org)
    db.flush()
    db.add(m.Membership(org_id=org.id, user_id=user.id, role="owner"))
    proj = m.Project(org_id=org.id, name=project_name)
    db.add(proj)
    db.flush()
    return org, proj


def _signup_open(request: Request, db: DbSession) -> bool:
    return request.app.state.settings.allow_signup or not _users_exist(db)


@router.get("/signup")
def signup_form(request: Request, db: DbSession = Depends(get_db)):
    return render(request, "signup.html", open=_signup_open(request, db), error=None,
                  first=not _users_exist(db))


@router.post("/signup", dependencies=[Depends(csrf_guard)])
def signup(request: Request, email: str = Form(""), password: str = Form(""),
           org_name: str = Form(""), db: DbSession = Depends(get_db)):
    if not _signup_open(request, db):
        raise HTTPException(403, "signup is closed")
    key = f"signup:{client_ip(request)}"
    email, org_name = email.strip().lower()[:320], org_name.strip()[:200]
    err = None
    if throttled(db, key, 10, 60):
        err = "Too many sign-ups from this address. Try again later."
    elif not _email_ok(email):
        err = "Enter a valid email address."
    elif len(password) < MIN_PASSWORD_LEN:
        err = f"Use a password of at least {MIN_PASSWORD_LEN} characters."
    elif not org_name:
        err = "Name your organisation."
    elif db.scalar(select(m.User).where(m.User.email == email)):
        err = "That email already has an account. Sign in instead."
    if err:
        note_attempt(db, key)
        return render(request, "signup.html", 400, open=True, error=err, first=not _users_exist(db))
    note_attempt(db, key)
    u = _create_account(db, email, password)
    _, proj = create_org_with_project(db, u, org_name)
    resp = redirect(f"/p/{proj.id}/")
    start_session(request, db, u, resp)
    return resp


def _invite(db: DbSession, token: str) -> Optional[m.Invite]:
    inv = db.scalar(select(m.Invite).where(m.Invite.token_hash == sha256_hex(token)))
    if inv is None or inv.used_at is not None or inv.expires_at < m.utcnow():
        return None
    return inv


@router.get("/invite/{token}")
def invite_page(token: str, request: Request, db: DbSession = Depends(get_db),
                user=Depends(current_user)):
    inv = _invite(db, token)
    org = db.get(m.Org, inv.org_id) if inv else None
    return render(request, "invite.html", user=user, invite=inv, org=org, error=None,
                  token=token)


@router.post("/invite/{token}", dependencies=[Depends(csrf_guard)])
def invite_accept(token: str, request: Request, email: str = Form(""), password: str = Form(""),
                  db: DbSession = Depends(get_db), user=Depends(current_user)):
    inv = _invite(db, token)
    if inv is None:
        raise HTTPException(404, "this invite link is invalid, used or expired")
    org = db.get(m.Org, inv.org_id)
    resp = None
    if user is None:
        key = f"invite:{client_ip(request)}"
        email = email.strip().lower()[:320]
        err = None
        if throttled(db, key, 10, 60):
            err = "Too many attempts. Try again later."
        elif not _email_ok(email):
            err = "Enter a valid email address."
        elif len(password) < MIN_PASSWORD_LEN:
            err = f"Use a password of at least {MIN_PASSWORD_LEN} characters."
        elif db.scalar(select(m.User).where(m.User.email == email)):
            err = "That email already has an account. Sign in, then open this link again."
        if err:
            note_attempt(db, key)
            return render(request, "invite.html", 400, user=None, invite=inv, org=org,
                          error=err, token=token)
        user = _create_account(db, email, password)
        resp = True
    if not db.scalar(select(m.Membership).where(m.Membership.org_id == inv.org_id,
                                                m.Membership.user_id == user.id)):
        db.add(m.Membership(org_id=inv.org_id, user_id=user.id, role=inv.role))
    inv.used_at = m.utcnow()
    db.commit()
    first = db.scalar(select(m.Project).where(m.Project.org_id == inv.org_id)
                      .order_by(m.Project.created_at))
    out = redirect(f"/p/{first.id}/" if first else "/")
    if resp:
        start_session(request, db, user, out)
    return out


# --------------------------------------------------------------- home + projects

@router.get("/")
def home(request: Request, user=Depends(require_user), db: DbSession = Depends(get_db)):
    rows = user_projects(db, user)
    if len(rows) == 1 and rows[0][2] == "member":
        return redirect(f"/p/{rows[0][0].id}/")
    orgs = {}
    for p, o, role in rows:
        orgs.setdefault(o.id, {"org": o, "role": role, "projects": []})["projects"].append(p)
    for mem, o in db.execute(select(m.Membership, m.Org).join(m.Org, m.Org.id == m.Membership.org_id)
                             .where(m.Membership.user_id == user.id)):
        orgs.setdefault(o.id, {"org": o, "role": mem.role, "projects": []})
    return render(request, "home.html", user=user, orgs=list(orgs.values()), projects=rows,
                  error=None)


@router.post("/orgs/{org_id}/projects", dependencies=[Depends(csrf_guard)])
def create_project(org_id: str, name: str = Form(""), user=Depends(require_user),
                   db: DbSession = Depends(get_db)):
    mem = db.scalar(select(m.Membership).where(m.Membership.org_id == org_id,
                                               m.Membership.user_id == user.id))
    if mem is None:
        raise HTTPException(404, "not found")
    if mem.role != "owner":
        raise HTTPException(403, "only an organisation owner can do this")
    name = name.strip()[:200]
    if not name:
        raise HTTPException(400, "name the project")
    p = m.Project(org_id=org_id, name=name)
    db.add(p)
    db.commit()
    return redirect(f"/p/{p.id}/settings")


def _base(request, ctx: ProjectCtx, user, db, section, **extra):
    return dict(user=user, ctx=ctx, project=ctx.project, org=ctx.org, section=section,
                projects=user_projects(db, user), **extra)


# ------------------------------------------------------------------- overview

@router.get("/p/{pid}/")
def overview(request: Request, ctx: ProjectCtx = Depends(project_ctx),
             user=Depends(require_user), db: DbSession = Depends(get_db)):
    ov = queries.overview(db, ctx.project.id)
    return render(request, "overview.html", **_base(request, ctx, user, db, "overview", ov=ov))


# ---------------------------------------------------------------------- calls

def _float_or_none(v: str) -> Optional[float]:
    try:
        return float(v) if v.strip() else None
    except ValueError:
        return None


@router.get("/p/{pid}/calls")
def calls(request: Request, agent: str = "", since: str = "7d", incident_type: str = "",
          only_incidents: str = "", score_below: str = "", page: int = 1,
          ctx: ProjectCtx = Depends(project_ctx), user=Depends(require_user),
          db: DbSession = Depends(get_db)):
    page = max(1, min(page, 10000))
    rows, total = queries.calls_page(
        db, ctx.project.id, agent=agent or None, since=since, incident_type=incident_type or None,
        only_incidents=bool(only_incidents), score_below=_float_or_none(score_below), page=page)
    f = dict(agent=agent, since=since, incident_type=incident_type, only_incidents=only_incidents,
             score_below=score_below)
    return render(request, "calls.html", **_base(
        request, ctx, user, db, "calls", rows=rows, total=total, page=page, f=f,
        pages=max(1, -(-total // queries.PAGE)), agents=queries.call_filters(db, ctx.project.id),
        since_options=list(queries.SINCE)))


@router.get("/p/{pid}/calls/{call_pk}")
def call_detail(call_pk: str, request: Request, ctx: ProjectCtx = Depends(project_ctx),
                user=Depends(require_user), db: DbSession = Depends(get_db)):
    c = db.scalar(select(m.Call).where(m.Call.id == call_pk, m.Call.project_id == ctx.project.id))
    if c is None:
        raise HTTPException(404, "not found")
    return render(request, "call.html", **_base(request, ctx, user, db, "calls", call=c))


# ---------------------------------------------------------------------- tests

@router.get("/p/{pid}/tests")
def tests(request: Request, page: int = 1, ctx: ProjectCtx = Depends(project_ctx),
          user=Depends(require_user), db: DbSession = Depends(get_db)):
    page = max(1, min(page, 10000))
    q = select(m.TestRun).where(m.TestRun.project_id == ctx.project.id)
    total = db.scalar(select(func.count()).select_from(q.subquery())) or 0
    rows = db.scalars(q.order_by(m.TestRun.started_at.desc()).offset((page - 1) * 50).limit(50)).all()
    return render(request, "tests.html", **_base(
        request, ctx, user, db, "tests", rows=rows, page=page, pages=max(1, -(-total // 50))))


def _run(db, ctx, run_pk) -> m.TestRun:
    r = db.scalar(select(m.TestRun).where(m.TestRun.id == run_pk,
                                          m.TestRun.project_id == ctx.project.id))
    if r is None:
        raise HTTPException(404, "not found")
    return r


@router.get("/p/{pid}/tests/{run_pk}")
def test_run(run_pk: str, request: Request, ctx: ProjectCtx = Depends(project_ctx),
             user=Depends(require_user), db: DbSession = Depends(get_db)):
    r = _run(db, ctx, run_pk)
    cases = db.scalars(select(m.TestCaseRow).where(
        m.TestCaseRow.test_run_pk == r.id, m.TestCaseRow.project_id == ctx.project.id)
        .order_by(m.TestCaseRow.name)).all()
    return render(request, "testrun.html", **_base(request, ctx, user, db, "tests", run=r,
                                                   cases=cases))


@router.get("/p/{pid}/tests/{run_pk}/cases/{case_pk}")
def test_case(run_pk: str, case_pk: str, request: Request, ctx: ProjectCtx = Depends(project_ctx),
              user=Depends(require_user), db: DbSession = Depends(get_db)):
    r = _run(db, ctx, run_pk)
    c = db.scalar(select(m.TestCaseRow).where(
        m.TestCaseRow.id == case_pk, m.TestCaseRow.test_run_pk == r.id,
        m.TestCaseRow.project_id == ctx.project.id))
    if c is None:
        raise HTTPException(404, "not found")
    return render(request, "case.html", **_base(request, ctx, user, db, "tests", run=r, case=c))


# --------------------------------------------------------------------- alerts

def _rules_row(db, ctx):
    return db.get(m.ProjectRules, ctx.project.id)


@router.get("/p/{pid}/alerts")
def alerts(request: Request, ctx: ProjectCtx = Depends(project_ctx),
           user=Depends(require_user), db: DbSession = Depends(get_db)):
    row = _rules_row(db, ctx)
    return render(request, "alerts.html", **_base(
        request, ctx, user, db, "alerts", text=pretty_json(row.rules if row else []),
        rules=row.rules if row else [], error=None, saved=request.query_params.get("saved"),
        example=pretty_json(EXAMPLE_RULES), base_url=str(request.base_url).rstrip("/")))


@router.post("/p/{pid}/alerts", dependencies=[Depends(csrf_guard)])
def alerts_save(request: Request, rules_json: str = Form(""),
                ctx: ProjectCtx = Depends(project_ctx), user=Depends(require_user),
                db: DbSession = Depends(get_db)):
    err, rules = None, None
    if len(rules_json) > 200_000:
        err = "The rules are larger than 200 KB."
    else:
        try:
            rules = schemas.validate_rules(json.loads(rules_json or "[]"))
        except json.JSONDecodeError as exc:
            err = f"That is not valid JSON: {exc.msg} (line {exc.lineno})."
        except schemas.SchemaError as exc:
            err = str(exc)
    if err:
        return render(request, "alerts.html", 400, **_base(
            request, ctx, user, db, "alerts", text=rules_json, rules=[], error=err, saved=None,
            example=pretty_json(EXAMPLE_RULES), base_url=str(request.base_url).rstrip("/")))
    row = _rules_row(db, ctx)
    if row is None:
        row = m.ProjectRules(project_id=ctx.project.id, rules=rules)
        db.add(row)
    row.rules, row.updated_at, row.updated_by = rules, m.utcnow(), user.id
    db.commit()
    return redirect(f"/p/{ctx.project.id}/alerts?saved=1")


# ------------------------------------------------------------------- settings

def _settings_ctx(request, ctx, user, db, **extra):
    members = db.execute(select(m.Membership, m.User).join(m.User, m.User.id == m.Membership.user_id)
                         .where(m.Membership.org_id == ctx.org.id).order_by(m.User.email)).all()
    tokens = db.scalars(select(m.ApiToken).where(m.ApiToken.project_id == ctx.project.id)
                        .order_by(m.ApiToken.created_at.desc())).all()
    conns = {c.provider: c for c in db.scalars(select(m.Connection).where(
        m.Connection.project_id == ctx.project.id))}
    return _base(request, ctx, user, db, "settings", members=members, tokens=tokens, conns=conns,
                 providers=connections.PROVIDERS, base_url=str(request.base_url).rstrip("/"),
                 new_token=None, invite_link=None, error=None, **extra)


@router.get("/p/{pid}/settings")
def settings(request: Request, ctx: ProjectCtx = Depends(project_ctx),
             user=Depends(require_user), db: DbSession = Depends(get_db)):
    return render(request, "settings.html", **_settings_ctx(request, ctx, user, db))


@router.post("/p/{pid}/settings/tokens", dependencies=[Depends(csrf_guard)])
def token_create(request: Request, name: str = Form(""), ctx: ProjectCtx = Depends(owner_ctx),
                 user=Depends(require_user), db: DbSession = Depends(get_db)):
    raw = new_secret_token(TOKEN_PREFIX)
    db.add(m.ApiToken(project_id=ctx.project.id, name=(name.strip() or "token")[:100],
                      token_hash=sha256_hex(raw), hint=raw[:8]))
    db.commit()
    c = _settings_ctx(request, ctx, user, db)
    c["new_token"] = raw
    return render(request, "settings.html", **c)


@router.post("/p/{pid}/settings/tokens/{tok_id}/revoke", dependencies=[Depends(csrf_guard)])
def token_revoke(tok_id: str, ctx: ProjectCtx = Depends(owner_ctx), db: DbSession = Depends(get_db)):
    t = db.scalar(select(m.ApiToken).where(m.ApiToken.id == tok_id,
                                           m.ApiToken.project_id == ctx.project.id))
    if t is None:
        raise HTTPException(404, "not found")
    t.revoked_at = t.revoked_at or m.utcnow()
    db.commit()
    return redirect(f"/p/{ctx.project.id}/settings")


@router.post("/p/{pid}/settings/connections", dependencies=[Depends(csrf_guard)])
def connection_save(request: Request, provider: str = Form(""), api_key: str = Form(""),
                    ctx: ProjectCtx = Depends(owner_ctx), db: DbSession = Depends(get_db)):
    if provider not in connections.PROVIDERS:
        raise HTTPException(400, "unknown provider")
    api_key = api_key.strip()
    if not api_key or len(api_key) > 500:
        raise HTTPException(400, "paste the API key")
    enc = request.app.state.vault.encrypt(api_key)
    row = db.scalar(select(m.Connection).where(m.Connection.project_id == ctx.project.id,
                                               m.Connection.provider == provider))
    if row is None:
        row = m.Connection(project_id=ctx.project.id, provider=provider, secret_enc=enc)
        db.add(row)
    row.secret_enc, row.last_test_at, row.last_test_ok, row.last_test_detail = enc, None, None, None
    db.commit()
    return redirect(f"/p/{ctx.project.id}/settings")


@router.post("/p/{pid}/settings/connections/{provider}/test", dependencies=[Depends(csrf_guard)])
def connection_test(provider: str, request: Request, ctx: ProjectCtx = Depends(owner_ctx),
                    db: DbSession = Depends(get_db)):
    row = db.scalar(select(m.Connection).where(m.Connection.project_id == ctx.project.id,
                                               m.Connection.provider == provider))
    if row is None:
        raise HTTPException(404, "not found")
    try:
        key = request.app.state.vault.decrypt(row.secret_enc)
        ok, detail = request.app.state.probe(provider, key)
    except ValueError as exc:
        ok, detail = False, str(exc)
    row.last_test_at, row.last_test_ok, row.last_test_detail = m.utcnow(), ok, detail[:300]
    db.commit()
    return redirect(f"/p/{ctx.project.id}/settings")


@router.post("/p/{pid}/settings/connections/{provider}/delete", dependencies=[Depends(csrf_guard)])
def connection_delete(provider: str, ctx: ProjectCtx = Depends(owner_ctx),
                      db: DbSession = Depends(get_db)):
    row = db.scalar(select(m.Connection).where(m.Connection.project_id == ctx.project.id,
                                               m.Connection.provider == provider))
    if row is not None:
        db.delete(row)
        db.commit()
    return redirect(f"/p/{ctx.project.id}/settings")


@router.post("/p/{pid}/settings/invites", dependencies=[Depends(csrf_guard)])
def invite_create(request: Request, role: str = Form("member"), ctx: ProjectCtx = Depends(owner_ctx),
                  user=Depends(require_user), db: DbSession = Depends(get_db)):
    if role not in ("owner", "member"):
        raise HTTPException(400, "role must be owner or member")
    raw = new_secret_token()
    db.add(m.Invite(org_id=ctx.org.id, token_hash=sha256_hex(raw), role=role, created_by=user.id,
                    expires_at=m.utcnow() + dt.timedelta(days=7)))
    db.commit()
    c = _settings_ctx(request, ctx, user, db)
    c["invite_link"] = f"{c['base_url']}/invite/{raw}"
    return render(request, "settings.html", **c)


@router.post("/p/{pid}/settings/members/{member_user_id}/remove", dependencies=[Depends(csrf_guard)])
def member_remove(member_user_id: str, request: Request, ctx: ProjectCtx = Depends(owner_ctx),
                  user=Depends(require_user), db: DbSession = Depends(get_db)):
    mem = db.scalar(select(m.Membership).where(m.Membership.org_id == ctx.org.id,
                                               m.Membership.user_id == member_user_id))
    if mem is None:
        raise HTTPException(404, "not found")
    owners = db.scalar(select(func.count()).select_from(m.Membership).where(
        m.Membership.org_id == ctx.org.id, m.Membership.role == "owner")) or 0
    if mem.role == "owner" and owners <= 1:
        c = _settings_ctx(request, ctx, user, db)
        c["error"] = "An organisation needs at least one owner."
        return render(request, "settings.html", 400, **c)
    db.delete(mem)
    db.commit()
    return redirect(f"/p/{ctx.project.id}/settings" if member_user_id != user.id else "/")
