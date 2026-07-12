"""Asymmetric signing: Ed25519 keypairs, sign/verify, and key management.

``attest.py`` already gives a contract an optional HMAC-SHA256 signature, but
that is a SHARED-SECRET tier: whoever holds the key to verify a signature also
holds the key to forge one, so it cannot tell a genuine signer apart from
anyone who learned the key. This module adds the tier above it -- an
asymmetric Ed25519 signature, where the verifying party only ever needs the
PUBLIC key, and cannot forge a signature even though it can check one. Per the
Evidence Kernel v2 spec (K3), the two tiers are named distinctly wherever a
verdict cites them: an Ed25519 signature is ``human`` / TIER_ATTESTED evidence;
an HMAC signature stays the strictly lower ``human-shared`` / TIER_PAIRED
"shared-secret integrity" tier. Neither tier is ever inferred from a field's
mere presence -- only a signature that actually verifies earns its tier.

This module is entirely OPT-IN. Core hotato stays zero-runtime-dependency and
fully offline: ``cryptography`` is imported lazily, inside each call that
needs it, never at module import time, so importing ``hotato.sign`` costs
nothing when the ``[sign]`` extra is not installed. The extra absent ->
:class:`~hotato._engine.vad.BackendUnavailable` is raised (never a bare
``ImportError``, never a silent no-signature fallback), naming
``pip install 'hotato[sign]'``.

Key management (also opt-in, not required to call :func:`sign`/:func:`verify`
directly with in-memory bytes):

  * a private key is written to ``~/.hotato/keys/<key_id>.ed25519`` (0600,
    written atomically so a concurrent reader never observes a partial file);
  * a public key is published to a local trust registry,
    ``~/.hotato/trust/<key_id>.pub``, from which a verifier loads it by
    ``key_id`` alone -- it never needs the private key;
  * ``key_id`` is a short, stable fingerprint of the public key (the first 16
    hex characters of its sha256), so the same keypair always resolves to the
    same identity across machines and re-derivations.

Zero third-party dependencies at import time: only ``hashlib`` / ``os`` /
``tempfile`` from the standard library are touched until a call actually needs
``cryptography``.
"""
from __future__ import annotations

import hashlib
import os
import tempfile
from typing import Optional, Tuple

from .errors import open_regular as _open_regular
from ._engine.vad import BackendUnavailable

# Directories (under the user's home; expanded lazily so tests can point HOME
# at an isolated tmp_path). Mirrors attest.py's ATTEST_KEY_FILE convention.
KEYS_DIR = os.path.join("~", ".hotato", "keys")
TRUST_DIR = os.path.join("~", ".hotato", "trust")
PRIVATE_KEY_EXT = ".ed25519"
PUBLIC_KEY_EXT = ".pub"

# A short, stable identity derived from a public key: the first KEY_ID_LEN hex
# characters of its full sha256 fingerprint. Short enough to use as a filename
# and to cite in a label-record's signer block; long enough (8 bytes / 64 bits)
# that two distinct keypairs colliding is not a practical concern for a local
# trust registry.
KEY_ID_LEN = 16

# Optional override for which saved key load_signing_key() picks with no
# explicit key_id, mirroring attest.py's env-var-first convention.
SIGN_KEY_ID_ENV = "HOTATO_SIGN_KEY_ID"

DEFAULT_SIGNER_ROLE = "runner"


def _keys_dir() -> str:
    return os.path.expanduser(KEYS_DIR)


def _trust_dir() -> str:
    return os.path.expanduser(TRUST_DIR)


def _fingerprint(public_key_bytes: bytes) -> str:
    """The full sha256 fingerprint of a public key, hex-encoded."""
    return hashlib.sha256(public_key_bytes).hexdigest()


def _key_id_for(public_key_bytes: bytes) -> str:
    return _fingerprint(public_key_bytes)[:KEY_ID_LEN]


def _validate_key_id(key_id: str) -> str:
    """``key_id`` becomes a filename component; refuse anything that is not a
    plain identifier (in particular a path separator or ``..``), so a caller
    that passes an externally supplied ``key_id`` (e.g. from a label-record's
    signer block) can never make key/trust lookups escape their directory."""
    if not key_id or os.path.basename(key_id) != key_id or key_id in (".", ".."):
        raise ValueError(
            f"{key_id!r} is not a valid key_id (expected a plain identifier, "
            "no path separators)."
        )
    return key_id


def _load_ed25519_backend():
    """Lazily import cryptography's Ed25519 primitives. Raises
    :class:`BackendUnavailable` -- never a bare ``ImportError`` -- if the
    optional ``[sign]`` extra is not installed, or the install is broken."""
    try:
        from cryptography.exceptions import InvalidSignature
        from cryptography.hazmat.primitives.asymmetric.ed25519 import (
            Ed25519PrivateKey,
            Ed25519PublicKey,
        )
        from cryptography.hazmat.primitives.serialization import (
            Encoding,
            NoEncryption,
            PrivateFormat,
            PublicFormat,
        )
    except Exception as exc:  # ImportError, or a partial/broken install
        raise BackendUnavailable(
            "Ed25519 signing requires the optional extra: "
            "pip install 'hotato[sign]'  (missing dependency: "
            f"{exc}). Core hotato stays dependency-free and offline; "
            "signing/verification is opt-in and nothing falls back silently "
            "to an unsigned or shared-secret tier."
        ) from exc
    return {
        "InvalidSignature": InvalidSignature,
        "Ed25519PrivateKey": Ed25519PrivateKey,
        "Ed25519PublicKey": Ed25519PublicKey,
        "Encoding": Encoding,
        "NoEncryption": NoEncryption,
        "PrivateFormat": PrivateFormat,
        "PublicFormat": PublicFormat,
    }


def keygen() -> Tuple[bytes, bytes, str]:
    """Generate a fresh Ed25519 keypair.

    Returns ``(private_bytes, public_bytes, key_id)``: both keys as their raw
    32-byte encodings, plus the stable short ``key_id`` fingerprint of the
    public key (see module docstring). Raises :class:`BackendUnavailable` if
    the ``[sign]`` extra is not installed.
    """
    backend = _load_ed25519_backend()
    private_key = backend["Ed25519PrivateKey"].generate()
    public_key = private_key.public_key()
    private_bytes = private_key.private_bytes(
        encoding=backend["Encoding"].Raw,
        format=backend["PrivateFormat"].Raw,
        encryption_algorithm=backend["NoEncryption"](),
    )
    public_bytes = public_key.public_bytes(
        encoding=backend["Encoding"].Raw,
        format=backend["PublicFormat"].Raw,
    )
    return private_bytes, public_bytes, _key_id_for(public_bytes)


def sign(payload: bytes, private_key_bytes: bytes) -> str:
    """Sign ``payload`` with a raw Ed25519 private key. Returns the signature
    as a hex string. Raises :class:`BackendUnavailable` if the ``[sign]``
    extra is not installed; raises ``ValueError`` if ``private_key_bytes`` is
    not a valid raw Ed25519 private key."""
    backend = _load_ed25519_backend()
    try:
        private_key = backend["Ed25519PrivateKey"].from_private_bytes(private_key_bytes)
    except Exception as exc:
        raise ValueError(f"not a valid Ed25519 private key: {exc}") from exc
    signature = private_key.sign(payload)
    return signature.hex()


def verify(payload: bytes, signature_hex: str, public_key_bytes: bytes) -> bool:
    """Verify ``signature_hex`` over ``payload`` under the raw Ed25519 public
    key ``public_key_bytes``. Returns ``True``/``False`` -- never raises for a
    signature that fails to verify, a tampered payload, a wrong key, or a
    malformed hex string; those are all simply "not verified". Raises
    :class:`BackendUnavailable` if the ``[sign]`` extra is not installed."""
    backend = _load_ed25519_backend()
    try:
        signature = bytes.fromhex(signature_hex)
    except (ValueError, TypeError):
        return False
    try:
        public_key = backend["Ed25519PublicKey"].from_public_bytes(public_key_bytes)
        public_key.verify(signature, payload)
    except (backend["InvalidSignature"], ValueError, TypeError):
        return False
    return True


def _atomic_write_bytes(path: str, data: bytes, mode: int) -> None:
    """Write ``data`` to ``path`` atomically: a temp file in the same
    directory, chmod to ``mode``, then ``os.replace`` over the destination --
    so a concurrent reader never observes a partially written key file, and
    the file never has looser permissions than ``mode`` at any point after it
    lands at ``path``."""
    directory = os.path.dirname(path)
    os.makedirs(directory, exist_ok=True, mode=0o700)
    fd, tmp_path = tempfile.mkstemp(dir=directory, prefix=".tmp-", suffix=".partial")
    try:
        with os.fdopen(fd, "wb") as fh:  # open-ok: fd from mkstemp, write-mode
            fh.write(data)
        os.chmod(tmp_path, mode)
        os.replace(tmp_path, path)
    except BaseException:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise


def save_signing_key(key_id: str, private_key_bytes: bytes, *, path: Optional[str] = None) -> str:
    """Save a raw Ed25519 private key to ``~/.hotato/keys/<key_id>.ed25519``
    (or ``path`` if given), 0600, written atomically. Returns the path."""
    key_id = _validate_key_id(key_id)
    dest = path or os.path.join(_keys_dir(), f"{key_id}{PRIVATE_KEY_EXT}")
    _atomic_write_bytes(dest, private_key_bytes, mode=0o600)
    return dest


def save_trust(key_id: str, public_key_bytes: bytes, *, path: Optional[str] = None) -> str:
    """Publish a raw Ed25519 public key to the local trust registry,
    ``~/.hotato/trust/<key_id>.pub`` (or ``path`` if given), written
    atomically. Kept 0600 like the private key store: this registry is a
    LOCAL trust anchor (who this machine will accept signatures from), not a
    public bulletin board. Returns the path."""
    key_id = _validate_key_id(key_id)
    dest = path or os.path.join(_trust_dir(), f"{key_id}{PUBLIC_KEY_EXT}")
    _atomic_write_bytes(dest, public_key_bytes, mode=0o600)
    return dest


def _list_saved_key_ids() -> list:
    try:
        names = os.listdir(_keys_dir())
    except OSError:
        return []
    return sorted(n[: -len(PRIVATE_KEY_EXT)] for n in names if n.endswith(PRIVATE_KEY_EXT))


def load_signing_key(key_id: Optional[str] = None) -> Optional[Tuple[str, bytes]]:
    """Load a saved private key. Returns ``(key_id, private_key_bytes)``, or
    ``None`` if there is nothing to load -- signing stays opt-in, so a caller
    with no key configured gets a clean ``None`` rather than an exception.

    Resolution order for ``key_id`` when not given explicitly: the
    ``$HOTATO_SIGN_KEY_ID`` env var, then -- if exactly one key is saved in
    ``~/.hotato/keys/`` -- that one. Multiple saved keys with no explicit
    choice is ambiguous and returns ``None`` rather than guessing.
    """
    if key_id is None:
        key_id = os.environ.get(SIGN_KEY_ID_ENV)
    if key_id is None:
        candidates = _list_saved_key_ids()
        if len(candidates) != 1:
            return None
        key_id = candidates[0]
    key_id = _validate_key_id(key_id)
    path = os.path.join(_keys_dir(), f"{key_id}{PRIVATE_KEY_EXT}")
    try:
        with _open_regular(path, "rb") as fh:
            data = fh.read()
    except OSError:
        return None
    if not data:
        return None
    return key_id, data


def load_trust(key_id: str) -> Optional[bytes]:
    """Load a public key from the local trust registry by ``key_id``. Returns
    the raw public key bytes, or ``None`` if that ``key_id`` is not (or no
    longer) in the registry -- an absent entry is refused as untrusted by any
    caller checking a signature, never silently treated as trusted."""
    key_id = _validate_key_id(key_id)
    path = os.path.join(_trust_dir(), f"{key_id}{PUBLIC_KEY_EXT}")
    try:
        with _open_regular(path, "rb") as fh:
            data = fh.read()
    except OSError:
        return None
    return data or None


def derive_public_key(private_key_bytes: bytes) -> bytes:
    """Derive the raw 32-byte Ed25519 public key for a raw private key.

    Lets a caller that only has ``(key_id, private_key_bytes)`` from
    :func:`load_signing_key` still cite the matching public key (e.g. to build
    a :func:`signer_identity` block or sign a value the recipient will verify
    against the same key) without keeping a separate copy of it around. Raises
    :class:`BackendUnavailable` if the ``[sign]`` extra is not installed;
    raises ``ValueError`` if ``private_key_bytes`` is not a valid raw Ed25519
    private key.
    """
    backend = _load_ed25519_backend()
    try:
        private_key = backend["Ed25519PrivateKey"].from_private_bytes(private_key_bytes)
    except Exception as exc:
        raise ValueError(f"not a valid Ed25519 private key: {exc}") from exc
    public_key = private_key.public_key()
    return public_key.public_bytes(
        encoding=backend["Encoding"].Raw,
        format=backend["PublicFormat"].Raw,
    )


def signer_identity(key_id: str, public_key_bytes: bytes, signer_role: str = DEFAULT_SIGNER_ROLE) -> dict:
    """The identity block a signed artifact cites: ``{key_id,
    public_fingerprint, signer_role}``. ``key_id`` is the short filename-safe
    fingerprint; ``public_fingerprint`` is the full sha256 hex digest of the
    public key, kept alongside it for an unambiguous long-form audit trail.
    """
    return {
        "key_id": key_id,
        "public_fingerprint": _fingerprint(public_key_bytes),
        "signer_role": signer_role,
    }


__all__ = [
    "KEYS_DIR",
    "TRUST_DIR",
    "PRIVATE_KEY_EXT",
    "PUBLIC_KEY_EXT",
    "KEY_ID_LEN",
    "SIGN_KEY_ID_ENV",
    "DEFAULT_SIGNER_ROLE",
    "keygen",
    "sign",
    "verify",
    "derive_public_key",
    "save_signing_key",
    "save_trust",
    "load_signing_key",
    "load_trust",
    "signer_identity",
]
