#!/usr/bin/env python3
"""Compatibility entry point for a file-secret production gateway.

Compose executes ``hotato`` directly. This helper remains usable by runtimes
that require a script entry point and never reads or copies the secret itself.
"""

from __future__ import annotations

import os
from pathlib import Path

SECRET_FILE = Path("/run/secrets/hotato-production-token")


def command() -> list[str]:
    return [
        "hotato",
        "production",
        "serve",
        "--db",
        "/data/production.sqlite",
        "--host",
        "127.0.0.1",
        "--port",
        "8432",
        "--token-file",
        str(SECRET_FILE),
    ]


def main() -> None:
    os.execvp("hotato", command())


if __name__ == "__main__":
    main()
