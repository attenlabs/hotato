"""Ed25519 signing layer (hotato.sign): keygen/sign/verify + key management.

Covers:
  * a fresh keypair signs a payload and verifies under its own public key;
  * a WRONG key fails verify (never silently accepted);
  * a TAMPERED payload fails verify against the original signature;
  * with `cryptography` unavailable, keygen()/sign()/verify() raise a clean
    BackendUnavailable naming the [sign] extra -- never a bare ImportError,
    never a silent no-signature fallback (forced deterministically via a
    builtins.__import__ block, so this path is exercised regardless of
    whether cryptography happens to be installed in the test environment);
  * a saved private key file lands 0600;
  * save/load round-trips for both the private-key store and the trust
    registry, and load_trust() on an unknown key_id is a clean None (refused,
    never silently trusted).
"""
from __future__ import annotations

import builtins
import os
import stat

import pytest

from hotato import sign as S
from hotato._engine.vad import BackendUnavailable


def _cryptography_installed() -> bool:
    try:
        import cryptography  # noqa: F401
        return True
    except Exception:
        return False


CRYPTO = pytest.mark.skipif(
    not _cryptography_installed(),
    reason="the 'sign' extra (cryptography) is not installed here",
)


# --- keygen / sign / verify --------------------------------------------------

@CRYPTO
def test_keygen_sign_verify_roundtrip():
    private_bytes, public_bytes, key_id = S.keygen()
    assert isinstance(private_bytes, bytes) and len(private_bytes) == 32
    assert isinstance(public_bytes, bytes) and len(public_bytes) == 32
    assert isinstance(key_id, str) and len(key_id) == S.KEY_ID_LEN
    # key_id is a deterministic function of the public key, not random per call
    assert key_id == S._key_id_for(public_bytes)

    payload = b"label-record: event 7f3a... yield=True"
    sig_hex = S.sign(payload, private_bytes)
    assert isinstance(sig_hex, str)
    bytes.fromhex(sig_hex)  # a valid hex string

    assert S.verify(payload, sig_hex, public_bytes) is True


@CRYPTO
def test_wrong_key_fails_verify():
    priv_a, pub_a, _ = S.keygen()
    _priv_b, pub_b, _ = S.keygen()
    payload = b"label-record: event abc123 yield=False"
    sig_hex = S.sign(payload, priv_a)

    assert S.verify(payload, sig_hex, pub_a) is True
    assert S.verify(payload, sig_hex, pub_b) is False


@CRYPTO
def test_tampered_payload_fails_verify():
    priv, pub, _ = S.keygen()
    payload = b"label-record: event abc123 yield=False"
    sig_hex = S.sign(payload, priv)

    assert S.verify(payload, sig_hex, pub) is True
    assert S.verify(payload + b"\x00", sig_hex, pub) is False
    assert S.verify(b"a totally different payload", sig_hex, pub) is False


@CRYPTO
def test_malformed_signature_hex_is_a_clean_false_not_a_crash():
    _priv, pub, _ = S.keygen()
    assert S.verify(b"payload", "not-hex-at-all!!", pub) is False


# --- missing extra: clean BackendUnavailable, no silent fallback -----------

def _block_cryptography_import(monkeypatch):
    """Force every import of `cryptography` (or any submodule) to fail, so the
    missing-extra path is exercised deterministically regardless of whether
    `cryptography` happens to be installed in this environment."""
    real_import = builtins.__import__

    def fake_import(name, *args, **kwargs):
        if name == "cryptography" or name.startswith("cryptography."):
            raise ImportError(f"No module named {name!r} (blocked for test)")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", fake_import)


def test_missing_extra_raises_clean_backend_unavailable_on_keygen(monkeypatch):
    _block_cryptography_import(monkeypatch)
    with pytest.raises(BackendUnavailable) as ei:
        S.keygen()
    msg = str(ei.value)
    assert "hotato[sign]" in msg
    assert "extra" in msg.lower() or "install" in msg.lower()


def test_missing_extra_raises_clean_backend_unavailable_on_sign(monkeypatch):
    _block_cryptography_import(monkeypatch)
    with pytest.raises(BackendUnavailable):
        S.sign(b"payload", b"\x00" * 32)


def test_missing_extra_raises_clean_backend_unavailable_on_verify(monkeypatch):
    _block_cryptography_import(monkeypatch)
    with pytest.raises(BackendUnavailable):
        S.verify(b"payload", "00" * 64, b"\x00" * 32)


# --- key management: save/load, permissions, trust registry ----------------

@CRYPTO
def test_saved_private_key_file_is_0600(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("USERPROFILE", str(tmp_path))
    private_bytes, _public_bytes, key_id = S.keygen()
    path = S.save_signing_key(key_id, private_bytes)

    assert os.path.isfile(path)
    mode = stat.S_IMODE(os.stat(path).st_mode)
    assert mode == 0o600


@CRYPTO
def test_save_and_load_signing_key_roundtrip_explicit_key_id(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("USERPROFILE", str(tmp_path))
    private_bytes, _public_bytes, key_id = S.keygen()
    S.save_signing_key(key_id, private_bytes)

    loaded = S.load_signing_key(key_id)
    assert loaded == (key_id, private_bytes)


@CRYPTO
def test_load_signing_key_resolves_the_sole_saved_key_with_no_explicit_id(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("USERPROFILE", str(tmp_path))
    monkeypatch.delenv(S.SIGN_KEY_ID_ENV, raising=False)
    private_bytes, _public_bytes, key_id = S.keygen()
    S.save_signing_key(key_id, private_bytes)

    assert S.load_signing_key() == (key_id, private_bytes)


@CRYPTO
def test_load_signing_key_is_ambiguous_with_multiple_saved_keys_and_no_explicit_id(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("USERPROFILE", str(tmp_path))
    monkeypatch.delenv(S.SIGN_KEY_ID_ENV, raising=False)
    priv_a, _pub_a, id_a = S.keygen()
    priv_b, _pub_b, id_b = S.keygen()
    S.save_signing_key(id_a, priv_a)
    S.save_signing_key(id_b, priv_b)

    assert S.load_signing_key() is None
    # but an explicit id still resolves cleanly
    assert S.load_signing_key(id_a) == (id_a, priv_a)


def test_load_signing_key_missing_is_a_clean_none(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("USERPROFILE", str(tmp_path))
    assert S.load_signing_key("no-such-key-id") is None
    assert S.load_signing_key() is None


@CRYPTO
def test_save_and_load_trust_roundtrip(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("USERPROFILE", str(tmp_path))
    _private_bytes, public_bytes, key_id = S.keygen()
    S.save_trust(key_id, public_bytes)

    assert S.load_trust(key_id) == public_bytes


def test_load_trust_unknown_key_id_is_a_clean_none_refused(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("USERPROFILE", str(tmp_path))
    assert S.load_trust("never-registered") is None


def test_key_id_rejects_path_traversal():
    with pytest.raises(ValueError):
        S.load_trust("../../etc/passwd")
    with pytest.raises(ValueError):
        S.load_signing_key("../escape")


@CRYPTO
def test_signer_identity_shape():
    _private_bytes, public_bytes, key_id = S.keygen()
    ident = S.signer_identity(key_id, public_bytes, signer_role="reviewer")
    assert ident == {
        "key_id": key_id,
        "public_fingerprint": S._fingerprint(public_bytes),
        "signer_role": "reviewer",
    }
