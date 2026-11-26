"""Application factory."""

from __future__ import annotations

from typing import Callable, Optional

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, RedirectResponse
from jinja2 import Environment, PackageLoader, select_autoescape
from starlette.exceptions import HTTPException

from . import api, connections, deps, web
from . import db as m
from .config import Settings
from .security import Vault
from .style import APP_CSS, APP_JS

CSP = ("default-src 'none'; style-src 'self'; script-src 'self'; img-src 'self' data:; "
       "form-action 'self'; frame-ancestors 'none'; base-uri 'none'; connect-src 'self'")


def create_app(settings: Optional[Settings] = None, *,
               probe: Optional[Callable] = None, **overrides) -> FastAPI:
    """Refuses to build without HOTATO_CLOUD_SECRET (see config.Settings)."""
    settings = settings or Settings.from_env(**overrides)
    app = FastAPI(title="hotato cloud", docs_url=None, redoc_url=None, openapi_url=None)
    engine = m.make_engine(settings.database_url)
    m.Base.metadata.create_all(engine)
    env = Environment(loader=PackageLoader("hotato.cloud", "templates"),
                      autoescape=select_autoescape(["html"]), trim_blocks=True, lstrip_blocks=True)
    env.globals.update(spark=deps.spark, fmt_ts=deps.fmt_ts, fmt_pct=deps.fmt_pct,
                       fmt_num=deps.fmt_num, fmt_dur=deps.fmt_dur, safe_href=deps.safe_href)
    st = app.state
    st.settings, st.engine, st.sessions = settings, engine, m.make_session_factory(engine)
    st.vault, st.templates = Vault(settings.secret), env
    st.probe = probe or connections.probe
    st.css, st.js = APP_CSS, APP_JS

    @app.middleware("http")
    async def security_headers(request: Request, call_next):
        resp = await call_next(request)
        h = resp.headers
        h.setdefault("Content-Security-Policy", CSP)
        h.setdefault("X-Content-Type-Options", "nosniff")
        h.setdefault("X-Frame-Options", "DENY")
        h.setdefault("Referrer-Policy", "same-origin")
        h.setdefault("Cross-Origin-Opener-Policy", "same-origin")
        if deps.is_https(request):
            h.setdefault("Strict-Transport-Security", "max-age=31536000")
        if not request.url.path.startswith("/static/"):
            h.setdefault("Cache-Control", "no-store")
        return resp

    @app.exception_handler(deps.LoginRedirect)
    async def _login(request: Request, exc: deps.LoginRedirect):
        if request.url.path.startswith("/api/"):
            return JSONResponse({"error": "unauthorized"}, status_code=401)
        from urllib.parse import quote
        return RedirectResponse("/login?next=" + quote(exc.next, safe="/"), status_code=303)

    @app.exception_handler(HTTPException)
    async def _http(request: Request, exc: HTTPException):
        if request.url.path.startswith("/api/"):
            return JSONResponse({"ok": False, "error": exc.detail}, status_code=exc.status_code,
                                headers=getattr(exc, "headers", None))
        return deps.render(request, "error.html", exc.status_code, status=exc.status_code,
                           message=str(exc.detail))

    app.include_router(web.router)
    app.include_router(api.router)
    return app
