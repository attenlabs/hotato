"""Contract authenticity: the canonical-digest binding and optional signature.

The .hotato ``MANIFEST.sha256.json`` proves only INTERNAL byte-consistency and
is recomputed on every ``contract pack``, so unpacking a bundle, loosening
``policy.pass_conditions`` in ``contract.json``, and re-packing yields a fresh
self-consistent manifest that ``verify`` / ``unpack`` would accept. These tests
pin the additional authenticity axis that closes that hole:

  (a) a freshly created contract embeds a canonical digest that recomputes
      identically (unsigned, but internally consistent);
  (b) editing ``policy.pass_conditions`` WITHOUT updating the embedded digest is
      caught as ``tampered`` by verify (and unpack refuses a tampered re-pack);
  (c) with an HMAC key, ``sign`` + ``verify_attestation`` authenticates, and a
      wrong key does not;
  (d) with no key, the bundle is ``unsigned, internally consistent evidence``,
      never ``authenticated``.
"""
from __future__ import annotations

import json
import os
from importlib import resources

import pytest

from hotato import attest as _attest
from hotato import contract as _contract

HARD = str(resources.files("hotato").joinpath(
    "data", "audio", "01-hard-interruption.example.wav"))   # yields at 2.40


def _isolate_key(monkeypatch, tmp_path):
    """No ambient signing key: env var cleared and ~ pointed at an empty dir, so
    load_attest_key() returns None and creation produces an UNSIGNED marker."""
    monkeypatch.delenv(_attest.ATTEST_KEY_ENV, raising=False)
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("USERPROFILE", str(tmp_path))


def _make(tmp_path, cid="ct-attest-001", expect="yield", onset=2.40, **kw):
    return _contract.create_contract(
        stereo=HARD, contract_id=cid, expect=expect, out_dir=str(tmp_path),
        onset_sec=onset, **kw,
    )


# --- (a) a fresh contract's embedded digest verifies -----------------------

def test_created_contract_embeds_a_verifying_canonical_digest(tmp_path, monkeypatch):
    _isolate_key(monkeypatch, tmp_path)
    res = _make(tmp_path)
    c = res["contract"]

    assert "attestation" in c
    embedded = c["attestation"]["canonical_digest"]
    recomputed, fields = _attest.canonical_contract_digest(c)
    assert recomputed == embedded
    assert c["attestation"]["digest_fields"] == fields
    # the digest binds the semantic identity the task enumerates
    for f in ("contract_schema", "label.expected_behavior", "policy",
              "source.source_audio_sha256", "scorer.package_version",
              "created_at"):
        assert f in fields

    a = _attest.assess_contract(c, bundle_dir=res["dir"], key=None)
    assert a["authenticity"] == "unsigned"
    assert a["ok"] is True
    assert a["authenticated"] is False


def test_created_bundle_writes_a_detached_attestation_json(tmp_path, monkeypatch):
    _isolate_key(monkeypatch, tmp_path)
    res = _make(tmp_path)
    detached = _attest.load_detached_attestation(res["dir"])
    assert detached is not None
    assert detached["algorithm"] == "none"          # unsigned marker
    assert detached["signature"] == ""
    assert detached["subject_digest"] == res["contract"]["attestation"]["canonical_digest"]
    assert detached["schema_version"] == "1"


def test_verify_reports_unsigned_for_a_fresh_bundle(tmp_path, monkeypatch, capsys):
    _isolate_key(monkeypatch, tmp_path)
    assert _make(tmp_path)  # created
    v = _contract.verify_contracts(str(tmp_path))
    r = v["results"][0]
    assert r["authenticity"] == "unsigned"
    assert r["authenticated"] is False
    assert v["exit_code"] == 0          # unsigned does NOT fail the batch
    text = _contract.render_verify_text(v)
    assert "integrity: intact" in text
    # never mislabels an unsigned bundle as authenticated
    assert "integrity: signed" not in text  # unsigned bundle must not read as signed


# --- (b) a loosened policy without a re-digest is caught as tampered --------

def test_loosening_policy_without_updating_digest_is_tampered(tmp_path, monkeypatch):
    _isolate_key(monkeypatch, tmp_path)
    res = _make(tmp_path)
    bundle = res["dir"]
    cpath = os.path.join(bundle, "contract.json")
    with open(cpath, encoding="utf-8") as fh:
        c = json.load(fh)

    # loosen the pass condition, leaving the embedded digest stale
    c["policy"]["pass_conditions"]["max_talk_over_sec"] = 999.0
    with open(cpath, "w", encoding="utf-8") as fh:
        json.dump(c, fh)

    a = _attest.assess_contract(c, bundle_dir=bundle)
    assert a["authenticity"] == "tampered"
    assert a["ok"] is False
    assert a["authenticated"] is False

    v = _contract.verify_contracts(bundle)
    assert v["results"][0]["authenticity"] == "tampered"
    assert v["exit_code"] == 1                 # tampering fails the batch
    assert v["tampered"] == 1
    assert "TAMPERED" in _contract.render_verify_text(v)


def test_unpack_refuses_a_tampered_repack(tmp_path, monkeypatch):
    _isolate_key(monkeypatch, tmp_path)
    res = _make(tmp_path)
    bundle = res["dir"]
    cpath = os.path.join(bundle, "contract.json")
    with open(cpath, encoding="utf-8") as fh:
        c = json.load(fh)
    c["policy"]["pass_conditions"]["max_talk_over_sec"] = 999.0
    with open(cpath, "w", encoding="utf-8") as fh:
        json.dump(c, fh)

    # re-pack: the manifest is recomputed, so it is internally self-consistent...
    pack = _contract.pack_contract(
        bundle, out_path=str(tmp_path / "tampered.hotato.pack"))
    out = str(tmp_path / "unpacked")
    # ...but the canonical-digest check catches the edited body, fail-closed.
    with pytest.raises(ValueError) as ei:
        _contract.unpack_contract(pack["path"], out)
    msg = str(ei.value).lower()
    assert "tamper" in msg or "authenticity" in msg
    assert not os.path.exists(out)     # nothing extracted


def test_unpack_accepts_an_untampered_repack(tmp_path, monkeypatch):
    _isolate_key(monkeypatch, tmp_path)
    res = _make(tmp_path)
    pack = _contract.pack_contract(
        res["dir"], out_path=str(tmp_path / "clean.hotato.pack"))
    out = str(tmp_path / "unpacked")
    result = _contract.unpack_contract(pack["path"], out)
    assert os.path.isdir(out)
    assert result["authenticity"] == "unsigned"
    assert result["authenticated"] is False


# --- (c) HMAC sign + verify authenticates; a wrong key does not ------------

def test_hmac_sign_and_verify_attestation(tmp_path, monkeypatch):
    _isolate_key(monkeypatch, tmp_path)
    c = _make(tmp_path)["contract"]
    digest, fields = _attest.canonical_contract_digest(c)
    key = b"super-secret-signing-key"

    att = _attest.sign(digest, key=key, signer="tester", digest_fields=fields)
    assert att["algorithm"] == "hmac-sha256"
    assert att["signature"]
    assert att["subject_digest"] == digest

    good = _attest.verify_attestation(c, att, key=key)
    assert good["authenticated"] is True
    assert good["tier"] == "authenticated"
    assert good["ok"] is True

    # K7: a CLAIMED signature that does not verify under the given key is an
    # authenticity FAILURE (wrong key or forged/altered signature). It must fail
    # closed -- never ok=True/unsigned, which previously let forged evidence pass.
    wrong = _attest.verify_attestation(c, att, key=b"the-wrong-key")
    assert wrong["authenticated"] is False
    assert wrong["ok"] is False
    assert wrong["tier"] == "tampered"

    # A claimed signature with NO key to check it is honestly "unverified"
    # (claimed but not checked), distinct from explicitly unsigned; it is not
    # authenticated and must not be treated as verified.
    absent = _attest.verify_attestation(c, att, key=None)
    assert absent["authenticated"] is False
    assert absent["tier"] == "unverified"
    assert absent["ok"] is True


def test_signed_then_body_edited_is_tampered(tmp_path, monkeypatch):
    _isolate_key(monkeypatch, tmp_path)
    c = _make(tmp_path)["contract"]
    digest, fields = _attest.canonical_contract_digest(c)
    key = b"k"
    att = _attest.sign(digest, key=key, signer="tester", digest_fields=fields)

    # edit the body after signing -> recomputed digest no longer matches subject
    c["policy"]["pass_conditions"]["max_talk_over_sec"] = 999.0
    tampered = _attest.verify_attestation(c, att, key=key)
    assert tampered["tier"] == "tampered"
    assert tampered["ok"] is False
    assert tampered["authenticated"] is False


def test_bundle_created_with_a_key_is_authenticated_end_to_end(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))                 # no competing key file
    monkeypatch.setenv("USERPROFILE", str(tmp_path))
    monkeypatch.setenv(_attest.ATTEST_KEY_ENV, "end-to-end-secret")
    res = _make(tmp_path)
    c = res["contract"]
    assert c["attestation"]["algorithm"] == "hmac-sha256"
    assert c["attestation"]["signed"] is True

    a = _attest.assess_contract(c, bundle_dir=res["dir"])     # key read from env
    assert a["authenticity"] == "authenticated"
    assert a["authenticated"] is True

    v = _contract.verify_contracts(res["dir"])
    assert v["results"][0]["authenticity"] == "authenticated"
    assert v["results"][0]["authenticated"] is True
    assert v["exit_code"] == 0


# --- (d) no key -> unsigned, internally consistent; never authenticated -----

def test_no_key_is_unsigned_never_authenticated(tmp_path, monkeypatch):
    _isolate_key(monkeypatch, tmp_path)
    c = _make(tmp_path)["contract"]
    digest, fields = _attest.canonical_contract_digest(c)

    att = _attest.sign(digest, key=None, signer="tester", digest_fields=fields)
    assert att["algorithm"] == "none"
    assert att["signature"] == ""

    v = _attest.verify_attestation(c, att, key=None)
    assert v["authenticated"] is False
    assert v["ok"] is True
    assert "unsigned, internally consistent" in v["reason"]


def test_load_attest_key_from_env_and_file(tmp_path, monkeypatch):
    monkeypatch.setenv(_attest.ATTEST_KEY_ENV, "from-env")
    assert _attest.load_attest_key() == b"from-env"

    monkeypatch.delenv(_attest.ATTEST_KEY_ENV, raising=False)
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("USERPROFILE", str(tmp_path))
    assert _attest.load_attest_key() is None          # neither source present

    keydir = tmp_path / ".hotato"
    keydir.mkdir()
    (keydir / "attest.key").write_bytes(b"from-file\n")
    assert _attest.load_attest_key() == b"from-file"  # trailing newline stripped


def test_legacy_contract_without_attestation_is_unattested(tmp_path, monkeypatch):
    _isolate_key(monkeypatch, tmp_path)
    c = _make(tmp_path)["contract"]
    # simulate a bundle created before attestation existed
    c.pop("attestation", None)
    a = _attest.assess_contract(c, bundle_dir=None)
    assert a["authenticity"] == "unattested"
    assert a["ok"] is True                 # still usable (backward compatible)
    assert a["authenticated"] is False     # but never authenticated


# --- K2/K7 regression: signature classification must fail closed -------------

def test_k7_wrong_key_attestation_is_refused_not_unsigned(tmp_path, monkeypatch):
    """A present hmac signature that fails under the verifier's key must be
    tampered/ok=False, never silently downgraded to unsigned (K7)."""
    _isolate_key(monkeypatch, tmp_path)
    c = _make(tmp_path)["contract"]
    digest, fields = _attest.canonical_contract_digest(c)
    att = _attest.sign(digest, key=b"real-key", signer="t", digest_fields=fields)
    v = _attest.verify_attestation(c, att, key=b"attacker-guess")
    assert v["ok"] is False and v["authenticated"] is False
    assert v["tier"] == "tampered"


def test_k7_altered_signature_bytes_is_refused(tmp_path, monkeypatch):
    """Flipping the signature bytes (forgery attempt) must not authenticate and
    must not read as unsigned."""
    _isolate_key(monkeypatch, tmp_path)
    c = _make(tmp_path)["contract"]
    digest, fields = _attest.canonical_contract_digest(c)
    key = b"real-key"
    att = _attest.sign(digest, key=key, signer="t", digest_fields=fields)
    att = dict(att); att["signature"] = "00" + att["signature"][2:]  # tamper
    v = _attest.verify_attestation(c, att, key=key)
    assert v["ok"] is False and v["tier"] == "tampered"
