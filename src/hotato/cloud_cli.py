"""``hotato cloud``: the command surface for the optional team app.

This module is part of the stdlib-only core. ``push`` needs nothing beyond the
standard library (so a CI box can post artifacts without installing the extra);
``serve`` and ``admin`` import the ``hotato.cloud`` package lazily and say what
to install when it is missing.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Callable, List, Optional, Tuple

from . import errors as _errors

TESTRUN = "hotato.testrun.v1"
EVENT = "hotato.monitorevent.v1"
BATCH = 200

EXIT_CODES = {
    "cloud": ((2, "no subcommand given (see hotato cloud serve --help)"),),
    "cloud serve": (
        (0, "the server ran and shut down cleanly"),
        (2, "the [cloud] extra is not installed, or HOTATO_CLOUD_SECRET is missing or too short"),
    ),
    "cloud push": (
        (0, "every artifact was accepted by the server"),
        (1, "the server rejected at least one artifact (the reason is printed)"),
        (2, "nothing to push, an unreadable file, an unknown schema, or the server was "
            "unreachable / refused the token"),
    ),
    "cloud admin": (
        (2, "no subcommand given (see hotato cloud admin create-user --help)"),
    ),
    "cloud admin create-user": (
        (0, "the account, its organisation and a first project were created"),
        (2, "the [cloud] extra is missing, the secret is not set, the email exists, or the "
            "password is too short"),
    ),
}


def _need_extra() -> Optional[int]:
    try:
        import importlib.util as u
        missing = [n for n in ("fastapi", "sqlalchemy", "jinja2", "uvicorn", "argon2",
                               "cryptography", "multipart") if u.find_spec(n) is None]
    except Exception:  # pragma: no cover
        missing = []
    if missing:
        print("error: the team app needs the optional extra. Install it with:\n"
              "  pip install 'hotato[cloud]'\n"
              f"(missing: {', '.join(missing)})", file=sys.stderr)
        return 2
    return None


def _cmd_serve(args) -> int:
    rc = _need_extra()
    if rc:
        return rc
    import importlib
    return importlib.import_module("hotato.cloud.cli").serve(args.host, args.port)


def _cmd_create_user(args) -> int:
    rc = _need_extra()
    if rc:
        return rc
    import importlib
    password = os.environ.get("HOTATO_CLOUD_NEW_PASSWORD") or None
    return importlib.import_module("hotato.cloud.cli").create_user(
        args.email, args.org, password)


# ----------------------------------------------------------------------- push

def _is_loopback(host: str) -> bool:
    return host in ("localhost", "127.0.0.1", "::1", "[::1]")


def _read_artifacts(path: Path) -> List[Tuple[str, object]]:
    """(label, parsed JSON value) for a file or a directory tree of .json/.jsonl."""
    files = [path] if path.is_file() else sorted(
        p for p in path.rglob("*") if p.is_file() and p.suffix in (".json", ".jsonl"))
    out = []
    for f in files:
        with _errors.open_regular(f, "r", encoding="utf-8") as fh:  # FIFO-safe
            text = fh.read()
        if f.suffix == ".jsonl":
            for n, line in enumerate(text.splitlines(), 1):
                if line.strip():
                    out.append((f"{f}:{n}", json.loads(line)))
        else:
            out.append((str(f), json.loads(text)))
    return out


def _schema_of(v) -> Optional[str]:
    if isinstance(v, dict):
        return v.get("schema")
    if isinstance(v, list) and v and isinstance(v[0], dict):
        return v[0].get("schema")
    return None


def _post(url: str, token: str, body) -> Tuple[int, str]:
    req = urllib.request.Request(
        url, data=json.dumps(body).encode(), method="POST",
        headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json",
                 "User-Agent": "hotato-cloud-push"})
    try:
        with urllib.request.urlopen(req, timeout=60) as r:  # noqa: S310 operator-supplied URL
            return r.status, r.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read().decode("utf-8", "replace")


def push(path: str, url: str, project: str, token: str, *,
         allow_insecure: bool = False, out=print, poster: Callable = _post) -> int:
    parsed = urllib.parse.urlparse(url)
    if parsed.scheme not in ("http", "https") or not parsed.netloc:
        out("error: --url must be an http(s) address such as https://hotato.example.com")
        return 2
    if parsed.scheme == "http" and not _is_loopback(parsed.hostname or "") and not allow_insecure:
        out("error: refusing to send the token over plain http to a non-local host "
            "(use https, or --allow-insecure)")
        return 2
    p = Path(path)
    if not p.exists():
        out(f"error: {path} does not exist")
        return 2
    try:
        items = _read_artifacts(p)
    except (OSError, ValueError) as exc:
        out(f"error: cannot read artifacts: {exc}")
        return 2
    base = url.rstrip("/") + f"/api/v1/projects/{urllib.parse.quote(project, safe='')}"
    testruns = [(lbl, v) for lbl, v in items if _schema_of(v) == TESTRUN]
    events = [(lbl, v) for lbl, v in items if _schema_of(v) == EVENT]
    unknown = [lbl for lbl, v in items if _schema_of(v) not in (TESTRUN, EVENT)]
    for lbl in unknown:
        out(f"skipped {lbl}: not a {TESTRUN} or {EVENT} artifact")
    if not testruns and not events:
        out(f"error: nothing to push under {path}")
        return 2
    rejected = failed = ok = 0
    for lbl, v in testruns:
        status, body = poster(base + "/testruns", token, v)
        if status in (401, 404):
            out(f"error: the server refused the token or project (HTTP {status})")
            return 2
        if status == 201:
            ok += 1
            out(f"pushed test run from {lbl}")
        else:
            rejected += 1
            out(f"rejected {lbl}: HTTP {status} {body[:300]}")
    flat = []
    for lbl, v in events:
        flat.extend((lbl, e) for e in (v if isinstance(v, list) else [v]))
    for i in range(0, len(flat), BATCH):
        chunk = flat[i:i + BATCH]
        status, body = poster(base + "/events", token, [e for _, e in chunk])
        if status in (401, 404):
            out(f"error: the server refused the token or project (HTTP {status})")
            return 2
        if status == 201:
            ok += len(chunk)
        else:
            rejected += len(chunk)
            out(f"rejected {len(chunk)} events starting at {chunk[0][0]}: HTTP {status} {body[:300]}")
    out(f"pushed {ok} artifact(s); rejected {rejected}; skipped {len(unknown)}")
    return 1 if rejected else (2 if failed else 0)


def _cmd_push(args) -> int:
    url = args.url or os.environ.get("HOTATO_CLOUD_URL", "")
    project = args.project or os.environ.get("HOTATO_CLOUD_PROJECT", "")
    token = args.token or os.environ.get("HOTATO_CLOUD_TOKEN", "")
    if not (url and project and token):
        print("error: --url, --project and --token are required "
              "(or HOTATO_CLOUD_URL, HOTATO_CLOUD_PROJECT, HOTATO_CLOUD_TOKEN)", file=sys.stderr)
        return 2
    try:
        return push(args.path, url, project, token, allow_insecure=args.allow_insecure)
    except (urllib.error.URLError, OSError) as exc:
        print(f"error: could not reach {url}: {getattr(exc, 'reason', exc)}", file=sys.stderr)
        return 2


# ------------------------------------------------------------------- register

def register(sub, epilog_factory: Callable[[str], str]) -> None:
    fmt = argparse.RawDescriptionHelpFormatter
    cl = sub.add_parser(
        "cloud", help="the team web app: serve it, push artifacts to it, create accounts",
        description="The team web app (optional extra: pip install 'hotato[cloud]'). One docker "
                    "compose command self-hosts it; the same app runs hosted. See docs/CLOUD.md.",
        epilog=epilog_factory("cloud"), formatter_class=fmt)
    csub = cl.add_subparsers(dest="cloud_command", required=True, metavar="serve|push|admin")

    sv = csub.add_parser(
        "serve", help="run the web app",
        description="Run the app with uvicorn. Needs HOTATO_CLOUD_SECRET; DATABASE_URL picks the "
                    "database (SQLite file by default, Postgres in production).",
        epilog=epilog_factory("cloud serve"), formatter_class=fmt)
    sv.add_argument("--host", default="127.0.0.1", help="bind address (default 127.0.0.1)")
    sv.add_argument("--port", type=int, default=8080, help="port (default 8080)")
    sv.set_defaults(func=_cmd_serve)

    ps = csub.add_parser(
        "push", help="post test-run and monitor-event artifacts to a server",
        description="Read one artifact file or a directory of .json/.jsonl files and post each "
                    f"{TESTRUN} and {EVENT} to the app. Other files are skipped and named. "
                    "Needs no extra install.",
        epilog=epilog_factory("cloud push") + "\n\nExamples:\n"
        "  hotato cloud push ./out --url https://hotato.example.com --project PROJECT_ID --token $T",
        formatter_class=fmt)
    ps.add_argument("path", metavar="FILE_OR_DIR")
    ps.add_argument("--url", default=None, help="server address (or HOTATO_CLOUD_URL)")
    ps.add_argument("--project", default=None, help="project id from Settings (or HOTATO_CLOUD_PROJECT)")
    ps.add_argument("--token", default=None, help="project API token (or HOTATO_CLOUD_TOKEN)")
    ps.add_argument("--allow-insecure", action="store_true",
                    help="allow sending the token over plain http to a non-local host")
    ps.set_defaults(func=_cmd_push)

    ad = csub.add_parser(
        "admin", help="server administration", description="Server administration commands.",
        epilog=epilog_factory("cloud admin"), formatter_class=fmt)
    asub = ad.add_subparsers(dest="admin_command", required=True, metavar="create-user")
    cu = asub.add_parser(
        "create-user", help="create an account with its own organisation and first project",
        description="Create an owner account directly in the database. The password is prompted "
                    "for (or taken from HOTATO_CLOUD_NEW_PASSWORD in scripts).",
        epilog=epilog_factory("cloud admin create-user"), formatter_class=fmt)
    cu.add_argument("email")
    cu.add_argument("--org", default="My organisation", help="organisation name")
    cu.set_defaults(func=_cmd_create_user)
