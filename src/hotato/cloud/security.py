"""Password hashing, at-rest encryption, API tokens, CSRF, login throttling."""

from __future__ import annotations

import base64
import hashlib
import hmac
import secrets

from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerificationError
from cryptography.fernet import Fernet, InvalidToken

_hasher = PasswordHasher()
# A real hash to verify against when the account does not exist, so a login for
# an unknown email costs the same as one for a known email.
_DUMMY_HASH = _hasher.hash("hotato-dummy-password")

MIN_PASSWORD_LEN = 10
TOKEN_PREFIX = "hto_"


def hash_password(password: str) -> str:
    return _hasher.hash(password)


def verify_password(stored: str | None, password: str) -> bool:
    try:
        return _hasher.verify(stored or _DUMMY_HASH, password) and stored is not None
    except (VerificationError, InvalidHashError):
        return False


def _derive(secret: str, purpose: str) -> bytes:
    return hmac.new(secret.encode(), purpose.encode(), hashlib.sha256).digest()


class Vault:
    """Fernet encryption of connection secrets, key derived from the app secret."""

    def __init__(self, secret: str):
        self._f = Fernet(base64.urlsafe_b64encode(_derive(secret, "hotato-cloud/fernet")))
        self._csrf_key = _derive(secret, "hotato-cloud/csrf")

    def encrypt(self, plaintext: str) -> str:
        return self._f.encrypt(plaintext.encode()).decode()

    def decrypt(self, blob: str) -> str:
        try:
            return self._f.decrypt(blob.encode()).decode()
        except InvalidToken as exc:
            raise ValueError("stored secret cannot be decrypted with the current "
                             "HOTATO_CLOUD_SECRET") from exc

    def csrf_token(self, binding: str) -> str:
        return hmac.new(self._csrf_key, binding.encode(), hashlib.sha256).hexdigest()

    def csrf_ok(self, binding: str, supplied: str) -> bool:
        return bool(binding) and hmac.compare_digest(self.csrf_token(binding), supplied or "")


def sha256_hex(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


def new_secret_token(prefix: str = "") -> str:
    return prefix + secrets.token_urlsafe(32)


def new_id() -> str:
    return secrets.token_hex(12)
