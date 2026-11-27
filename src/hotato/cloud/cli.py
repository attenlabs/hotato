"""Server-side commands (need the extra): serve and admin create-user."""

from __future__ import annotations

import getpass
import sys
from typing import Optional


def serve(host: str, port: int) -> int:
    import uvicorn

    from .app import create_app
    from .config import ConfigError

    try:
        app = create_app()
    except ConfigError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    uvicorn.run(app, host=host, port=port, proxy_headers=False, access_log=False,
                log_level="warning")
    return 0


def create_user(email: str, org_name: str, password: Optional[str]) -> int:
    from sqlalchemy import select

    from . import db as m
    from .app import create_app
    from .config import ConfigError
    from .security import MIN_PASSWORD_LEN
    from .web import _create_account, create_org_with_project

    try:
        app = create_app()
    except ConfigError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    email = email.strip().lower()
    if password is None:
        password = getpass.getpass("Password: ")
        if password != getpass.getpass("Repeat password: "):
            print("error: passwords differ", file=sys.stderr)
            return 2
    if len(password) < MIN_PASSWORD_LEN:
        print(f"error: use a password of at least {MIN_PASSWORD_LEN} characters", file=sys.stderr)
        return 2
    with app.state.sessions() as db:
        if db.scalar(select(m.User).where(m.User.email == email)):
            print(f"error: {email} already has an account", file=sys.stderr)
            return 2
        user = _create_account(db, email, password)
        _, proj = create_org_with_project(db, user, org_name)
        db.commit()
        print(f"created {email} as owner of {org_name!r}; project id {proj.id}")
    return 0


def main(argv=None) -> int:
    import argparse
    p = argparse.ArgumentParser(prog="python -m hotato.cloud")
    p.add_argument("--host", default="127.0.0.1")
    p.add_argument("--port", type=int, default=8080)
    a = p.parse_args(argv)
    return serve(a.host, a.port)
