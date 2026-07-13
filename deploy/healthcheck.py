#!/usr/bin/env python3
"""Container HEALTHCHECK for ``hotato serve``: authenticate to the running
workspace and confirm a view returns 200.

The workspace requires a bearer token on every request, so a health probe must
present one. The token is resolved with the SAME precedence the entrypoint uses,
so a supplied-token and a generated-token deployment both health-check correctly:

  1. the Docker secret file ``/run/secrets/hotato_token`` (if mounted),
  2. ``$HOTATO_SERVE_TOKEN`` (if set via the env file),
  3. the token the server generated + stored 0600 at
     ``<registry>/serve/<workspace>/token`` on first start.

Stdlib only (``urllib``); it opens exactly one loopback connection to the
in-container server and never reaches off-box. Exit 0 = healthy (HTTP 200),
1 = unhealthy (any other status, no token yet, or a connection error).
"""
from __future__ import annotations

import os
import sys
import urllib.error
import urllib.request

_SECRET_FILE = "/run/secrets/hotato_token"


def _resolve_token(registry: str, workspace: str) -> str:
    if os.path.isfile(_SECRET_FILE):
        try:
            with open(_SECRET_FILE, "r", encoding="utf-8") as fh:
                tok = fh.readline().strip()
            if tok:
                return tok
        except OSError:
            pass
    env = (os.environ.get("HOTATO_SERVE_TOKEN") or "").strip()
    if env:
        return env
    stored = os.path.join(registry, "serve", workspace, "token")
    try:
        with open(stored, "r", encoding="utf-8") as fh:
            return fh.readline().strip()
    except OSError:
        return ""


def main() -> int:
    port = os.environ.get("HOTATO_SERVE_PORT", "8321")
    workspace = os.environ.get("HOTATO_WORKSPACE", "default")
    registry = os.environ.get("HOTATO_REGISTRY", "/data")

    token = _resolve_token(registry, workspace)
    if not token:
        print("healthcheck: no bearer token available yet (server may still be "
              "starting)", file=sys.stderr)
        return 1

    url = "http://127.0.0.1:%s/?format=json" % port
    req = urllib.request.Request(url, headers={"Authorization": "Bearer " + token})
    try:
        with urllib.request.urlopen(req, timeout=4) as resp:  # noqa: S310 (loopback only)
            if resp.status == 200:
                return 0
            print("healthcheck: HTTP %s from %s" % (resp.status, url), file=sys.stderr)
            return 1
    except urllib.error.HTTPError as exc:
        print("healthcheck: HTTP %s from %s" % (exc.code, url), file=sys.stderr)
        return 1
    except Exception as exc:  # connection refused / timeout while starting
        print("healthcheck: %s" % exc, file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
