"""hotato cloud: the team web app (optional extra ``hotato[cloud]``).

Server-rendered FastAPI + Jinja2 + SQLAlchemy. The same app runs self-hosted
(``docker compose``) or hosted. Nothing in the hotato core imports this
package; ``hotato cloud serve`` imports it lazily and says what to install when
the extra is missing.
"""

from __future__ import annotations

__all__ = ["create_app"]


def create_app(*args, **kwargs):
    from .app import create_app as _create

    return _create(*args, **kwargs)
