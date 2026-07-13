"""``hotato serve`` -- the self-hosted local team workspace (GOAL §6, Phase 4).

A threaded, token-authenticated, read-only HTTP app over the fleet registry +
conversation artifacts, rendering the five conversation-QA views (release
readiness, scenario matrix, conversation inspector, failure clusters, production
health) in the report house style, with a ``?format=json`` mirror on every view.
Stdlib only (``http.server`` + ``sqlite3``): no framework, no build step, no
egress. See :mod:`hotato.serve.app` for the server and ``docs/WORKSPACE.md`` for
the operator guide.
"""
from __future__ import annotations

from .app import ServeContext, build_server, run_serve

__all__ = ["run_serve", "build_server", "ServeContext"]
