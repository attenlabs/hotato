"""Request-scoped helpers: db session, current user, tenant-checked lookups,
CSRF, throttling, rendering."""

from __future__ import annotations

import datetime as dt
import json
from typing import Optional
from urllib.parse import urlparse

from fastapi import Depends, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from markupsafe import Markup
from sqlalchemy import func, select
from sqlalchemy.orm import Session as DbSession

from . import db as m
from .security import new_secret_token, sha256_hex

SESSION_COOKIE = "hotato_session"
ANON_COOKIE = "hotato_anon"


class LoginRedirect(Exception):
    def __init__(self, nxt: str):
        self.next = nxt


def get_db(request: Request):
    s = request.app.state.sessions()
    try:
        yield s
    finally:
        s.close()


def client_ip(request: Request) -> str:
    if request.app.state.settings.trust_proxy:
        fwd = request.headers.get("x-forwarded-for", "")
        if fwd:
            return fwd.split(",")[-1].strip()[:45]
    return (request.client.host if request.client else "unknown")[:45]


def is_https(request: Request) -> bool:
    st = request.app.state.settings
    if st.secure_cookies is not None:
        return st.secure_cookies
    if st.trust_proxy and request.headers.get("x-forwarded-proto", "").lower() == "https":
        return True
    return request.url.scheme == "https"


def set_cookie(request: Request, resp, name: str, value: str, *, max_age: Optional[int] = None):
    resp.set_cookie(name, value, max_age=max_age, httponly=True, samesite="lax",
                    secure=is_https(request), path="/")


# ------------------------------------------------------------------ session

def current_user(request: Request, db: DbSession = Depends(get_db)) -> Optional[m.User]:
    raw = request.cookies.get(SESSION_COOKIE)
    if not raw:
        return None
    row = db.get(m.Session, sha256_hex(raw))
    if row is None or row.expires_at < m.utcnow():
        return None
    return db.get(m.User, row.user_id)


def require_user(request: Request, user: Optional[m.User] = Depends(current_user)) -> m.User:
    if user is None:
        nxt = request.url.path + (("?" + request.url.query) if request.url.query else "")
        raise LoginRedirect(nxt)
    return user


def start_session(request: Request, db: DbSession, user: m.User, resp) -> None:
    old = request.cookies.get(SESSION_COOKIE)
    if old:
        prev = db.get(m.Session, sha256_hex(old))
        if prev is not None:
            db.delete(prev)
    raw = new_secret_token()
    days = request.app.state.settings.session_days
    db.add(m.Session(id_hash=sha256_hex(raw), user_id=user.id,
                     expires_at=m.utcnow() + dt.timedelta(days=days)))
    db.commit()
    set_cookie(request, resp, SESSION_COOKIE, raw, max_age=days * 86400)


def safe_next(nxt: Optional[str]) -> str:
    if not nxt:
        return "/"
    p = urlparse(nxt)
    if p.scheme or p.netloc or not nxt.startswith("/") or nxt.startswith("//") or "\\" in nxt:
        return "/"
    return nxt


# --------------------------------------------------------------------- CSRF

def csrf_binding(request: Request) -> str:
    return request.cookies.get(SESSION_COOKIE) or request.cookies.get(ANON_COOKIE) or ""


def csrf_guard(request: Request, csrf_token: str = Form("")) -> None:
    origin = request.headers.get("origin")
    if origin and origin != "null":
        host = request.headers.get("host", "")
        if urlparse(origin).netloc != host:
            raise HTTPException(403, "cross-origin form post refused")
    if not request.app.state.vault.csrf_ok(csrf_binding(request), csrf_token):
        raise HTTPException(403, "CSRF check failed: reload the page and try again")


# ----------------------------------------------------------------- throttle

def throttled(db: DbSession, key: str, limit: int, minutes: int) -> bool:
    since = m.utcnow() - dt.timedelta(minutes=minutes)
    n = db.scalar(select(func.count()).select_from(m.LoginAttempt)
                  .where(m.LoginAttempt.key == key, m.LoginAttempt.at > since))
    return (n or 0) >= limit


def note_attempt(db: DbSession, key: str) -> None:
    db.add(m.LoginAttempt(key=key))
    old = m.utcnow() - dt.timedelta(days=2)
    db.query(m.LoginAttempt).filter(m.LoginAttempt.at < old).delete()
    db.commit()


# ------------------------------------------------------------ tenant access

class ProjectCtx:
    def __init__(self, project: m.Project, org: m.Org, role: str):
        self.project, self.org, self.role = project, org, role

    @property
    def is_owner(self) -> bool:
        return self.role == "owner"


def project_ctx(pid: str, user: m.User = Depends(require_user),
                db: DbSession = Depends(get_db)) -> ProjectCtx:
    """The ONLY way a page reaches a project: the row must be reachable through
    one of the caller's memberships, else 404 (never 403, so ids do not leak)."""
    row = db.execute(
        select(m.Project, m.Org, m.Membership.role)
        .join(m.Org, m.Org.id == m.Project.org_id)
        .join(m.Membership, m.Membership.org_id == m.Org.id)
        .where(m.Project.id == pid, m.Membership.user_id == user.id)).first()
    if row is None:
        raise HTTPException(404, "not found")
    return ProjectCtx(*row)


def owner_ctx(ctx: ProjectCtx = Depends(project_ctx)) -> ProjectCtx:
    if not ctx.is_owner:
        raise HTTPException(403, "only an organisation owner can do this")
    return ctx


def user_projects(db: DbSession, user: m.User):
    return db.execute(
        select(m.Project, m.Org, m.Membership.role)
        .join(m.Org, m.Org.id == m.Project.org_id)
        .join(m.Membership, m.Membership.org_id == m.Org.id)
        .where(m.Membership.user_id == user.id)
        .order_by(m.Org.name, m.Project.name)).all()


def token_project(request: Request, pid: str, db: DbSession) -> m.Project:
    """API auth: a bearer token is valid for exactly one project."""
    auth = request.headers.get("authorization", "")
    if not auth.lower().startswith("bearer "):
        raise HTTPException(401, "missing bearer token",
                            headers={"WWW-Authenticate": "Bearer"})
    tok = db.scalar(select(m.ApiToken).where(
        m.ApiToken.token_hash == sha256_hex(auth[7:].strip()), m.ApiToken.revoked_at.is_(None)))
    if tok is None:
        raise HTTPException(401, "invalid or revoked token",
                            headers={"WWW-Authenticate": "Bearer"})
    if tok.project_id != pid:
        raise HTTPException(404, "not found")
    now = m.utcnow()
    if tok.last_used_at is None or now - tok.last_used_at > dt.timedelta(minutes=1):
        tok.last_used_at = now
        db.commit()
    project = db.get(m.Project, pid)
    if project is None:
        raise HTTPException(404, "not found")
    return project


# ---------------------------------------------------------------- rendering

def spark(values, width: int = 160, height: int = 36, label: str = "trend") -> Markup:
    """Inline SVG sparkline. ``values`` may contain None (gaps are skipped)."""
    pts = [(i, v) for i, v in enumerate(values) if v is not None]
    if len(pts) < 2:
        return Markup('<span class="muted">not enough data for a trend yet</span>')
    lo, hi = min(v for _, v in pts), max(v for _, v in pts)
    span = (hi - lo) or 1.0
    n = max(len(values) - 1, 1)
    pad = 3
    coords = " ".join(
        f"{pad + (width - 2 * pad) * i / n:.1f},{height - pad - (height - 2 * pad) * (v - lo) / span:.1f}"
        for i, v in pts)
    last = coords.split(" ")[-1].split(",")
    desc = f"{label}: from {pts[0][1]:.2f} to {pts[-1][1]:.2f}, range {lo:.2f} to {hi:.2f}"
    return Markup(
        f'<svg class="spark" viewBox="0 0 {width} {height}" width="{width}" height="{height}" '
        f'role="img" aria-label="{Markup.escape(desc)}"><polyline fill="none" stroke="currentColor" '
        f'stroke-width="2" stroke-linejoin="round" stroke-linecap="round" points="{coords}"/>'
        f'<circle cx="{last[0]}" cy="{last[1]}" r="3" fill="currentColor"/></svg>')


def fmt_ts(t) -> str:
    return t.strftime("%Y-%m-%d %H:%M") + " UTC" if t else "-"


def fmt_pct(x) -> str:
    return "-" if x is None else f"{x * 100:.0f}%"


def fmt_num(x, nd=2) -> str:
    return "-" if x is None else f"{x:.{nd}f}"


def fmt_dur(s) -> str:
    if s is None:
        return "-"
    s = int(round(s))
    return f"{s // 60}m {s % 60:02d}s" if s >= 60 else f"{s}s"


def safe_href(url: Optional[str]) -> Optional[str]:
    if url and urlparse(url).scheme in ("http", "https"):
        return url
    return None


def render(request: Request, name: str, status_code: int = 200, **ctx) -> HTMLResponse:
    st = request.app.state
    binding = csrf_binding(request)
    fresh_anon = None
    if not binding:
        fresh_anon = new_secret_token()
        binding = fresh_anon
    ctx.setdefault("user", None)
    ctx["csrf"] = st.vault.csrf_token(binding)
    ctx["request"] = request
    html = st.templates.get_template(name).render(**ctx)
    resp = HTMLResponse(html, status_code=status_code)
    if fresh_anon:
        set_cookie(request, resp, ANON_COOKIE, fresh_anon, max_age=86400)
    return resp


def redirect(url: str, status: int = 303) -> RedirectResponse:
    return RedirectResponse(url, status_code=status)


def pretty_json(v) -> str:
    return json.dumps(v, indent=2, ensure_ascii=False)
