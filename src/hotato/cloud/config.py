"""Environment-driven settings. The secret is mandatory: no default, no dev key."""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Optional

MIN_SECRET_LEN = 16


class ConfigError(RuntimeError):
    pass


def _flag(name: str, default: bool) -> bool:
    raw = os.environ.get(name)
    if raw is None:
        return default
    return raw.strip().lower() in ("1", "true", "yes", "on")


def normalize_database_url(url: str) -> str:
    """Accept the URL shapes hosts hand out and name the driver we ship."""
    if url.startswith("postgres://"):
        url = "postgresql://" + url[len("postgres://"):]
    if url.startswith("postgresql://"):
        url = "postgresql+psycopg://" + url[len("postgresql://"):]
    return url


@dataclass(frozen=True)
class Settings:
    secret: str
    database_url: str
    secure_cookies: Optional[bool]   # None = decide per request from the scheme
    trust_proxy: bool
    allow_signup: bool
    max_body_bytes: int
    session_days: int = 14

    @classmethod
    def from_env(cls, **overrides) -> "Settings":
        secret = overrides.get("secret", os.environ.get("HOTATO_CLOUD_SECRET", ""))
        if not secret or len(secret) < MIN_SECRET_LEN:
            raise ConfigError(
                "HOTATO_CLOUD_SECRET is required (at least %d characters). It "
                "encrypts stored API keys and signs CSRF tokens; generate one "
                "with: python -c \"import secrets; print(secrets.token_urlsafe(48))\""
                % MIN_SECRET_LEN
            )
        db = overrides.get("database_url") or os.environ.get(
            "DATABASE_URL", "sqlite:///hotato-cloud.sqlite3")
        sec = os.environ.get("HOTATO_CLOUD_SECURE_COOKIES")
        return cls(
            secret=secret,
            database_url=normalize_database_url(db),
            secure_cookies=None if sec is None else _flag("HOTATO_CLOUD_SECURE_COOKIES", False),
            trust_proxy=_flag("HOTATO_CLOUD_TRUST_PROXY", False),
            allow_signup=overrides.get(
                "allow_signup", _flag("HOTATO_CLOUD_ALLOW_SIGNUP", False)),
            max_body_bytes=int(os.environ.get("HOTATO_CLOUD_MAX_BODY_BYTES", 8 * 1024 * 1024)),
        )
