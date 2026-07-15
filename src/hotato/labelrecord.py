"""Label-record: the signed artifact that replaces expected_yield-PRESENCE as
"proof a human authored this fixture's label" (Evidence Kernel v2, K5).

Before this module, ``manifest.build_manifest`` inferred human label authority
from whether a scored event carried an ``expected_yield`` field at all. That
field is written into EVERY scored event by ``core.py`` regardless of who (or
what default) supplied the expectation, so the inference was always true --
label_authority silently, permanently read "human" (TIER_ATTESTED) for any
battery, labelled or not.

A label-record is the real proof: a signed statement, bound to the EXACT
decoded audio it was made about (``event_audio_pcm_sha256``), that a named
reviewer looked at this event and decided ``yield`` or ``hold``. Only a
label-record that verifies -- signature valid, bound to the audio actually
being scored -- earns a "human" (Ed25519) or "human-shared" (HMAC) authority.
An explicit expectation with no such record is merely "asserted": someone
wrote a label, but nothing proves who, or that it was ever reviewed against
this exact recording.

Two signing tiers, mirroring :mod:`hotato.sign` / :mod:`hotato.receipt`:

  * Ed25519 (the ``[sign]`` extra) -- verifies with only the PUBLIC key;
    cannot be forged by whoever can check it. Tier: "human" / TIER_ATTESTED.
  * HMAC-SHA256 (:func:`hotato.receipt.load_key`, always available, zero
    third-party dependency) -- a SHARED secret: whoever can verify it could
    also have forged it. Tier: "human-shared" / TIER_PAIRED, strictly below
    Ed25519. This is the offline, zero-dependency default.

Zero-dependency, offline. ``cryptography`` is imported lazily by
:mod:`hotato.sign` only when an Ed25519 key is actually used; nothing here
imports it at module load time.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import os
import time
from typing import Optional

from . import receipt as _receipt
from . import sign as _sign
from ._engine.vad import BackendUnavailable
from .errors import open_regular as _open_regular
from .manifest import _sha256_str, canonical_json

SCHEMA = "hotato.label-record.v1"
TAXONOMY_VERSION_DEFAULT = "1"

_DECISIONS = ("yield", "hold")


class NoSigningKeyConfigured(RuntimeError):
    """Neither an Ed25519 signing key nor a shared HMAC key is configured, so
    :func:`mint_label_record` has no way to produce a signature. Minting an
    UNSIGNED label-record would have no defined authority (this schema always
    carries a signer + signature); rather than emit a hollow artifact that
    later silently refuses, minting itself refuses, with an actionable message.
    A caller that wants graceful degradation (e.g. `hotato fixture create` on
    a machine with no key configured yet) catches this and simply does not
    attach a label-record -- the fixture still gets created, its label just
    stays an "asserted" (operator-only) expectation, never falsely "human".
    """


def _now_iso() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def _hmac_key_id(key: bytes) -> str:
    """A short, stable, non-secret identifier for an HMAC shared-secret key:
    the first 16 hex chars of its own sha256 (mirrors sign.py's Ed25519
    key_id convention). Safe to disclose: a one-way hash of the secret, not
    the secret itself, and it lets a reader distinguish key rotations."""
    return hashlib.sha256(key).hexdigest()[:16]


def _record_subject(record: dict) -> str:
    """Canonical digest of the record body EXCLUDING signer/signature, so a
    signature can never be lifted onto a different body (mirrors
    manifest._manifest_subject / receipt._receipt_subject)."""
    body = {k: v for k, v in record.items() if k not in ("signer", "signature")}
    return _sha256_str(canonical_json(body))


def _normalize_decision(decision: str) -> str:
    d = str(decision).strip().lower()
    if d not in _DECISIONS:
        raise ValueError(
            f"decision must be one of {_DECISIONS!r}, got {decision!r}"
        )
    return d


def mint_label_record(
    *,
    reviewer_principal: str,
    event_audio_pcm_sha256: str,
    decision: str,
    taxonomy_version: str = TAXONOMY_VERSION_DEFAULT,
    rationale: Optional[str] = None,
    reviewed_at: Optional[str] = None,
    private_key: Optional[bytes] = None,
    key_id: Optional[str] = None,
    hmac_key: Optional[bytes] = None,
) -> dict:
    """Mint a signed ``hotato.label-record.v1``: a reviewer's yield/hold
    decision, bound to the EXACT decoded PCM of the event it is about.

    Signing key resolution, strongest first:
      1. an explicit ``private_key`` (+ matching ``key_id``) -- Ed25519-signs
         directly, no filesystem lookup (this is what lets a caller, or a
         test, sign without touching the local trust registry).
      2. else a saved Ed25519 key (:func:`hotato.sign.load_signing_key`) --
         Ed25519-signs using the local key store.
      3. else an explicit ``hmac_key`` -- HMAC-signs directly.
      4. else the shared HMAC key (:func:`hotato.receipt.load_key`, env
         ``HOTATO_ATTEST_KEY`` or ``~/.hotato/attest.key``) -- HMAC-signs.
      5. else raises :class:`NoSigningKeyConfigured`: never mints an
         unsigned record.

    Raises ``ValueError`` for a bad ``decision``, or if ``private_key`` is
    given without ``key_id`` (there is no way to name a stable signer
    identity for a bare private key otherwise).
    """
    decision = _normalize_decision(decision)
    if not reviewer_principal or not str(reviewer_principal).strip():
        raise ValueError("reviewer_principal is required and cannot be blank")
    if not event_audio_pcm_sha256:
        raise ValueError("event_audio_pcm_sha256 is required (bind the label to real audio)")
    body = {
        "schema": SCHEMA,
        "reviewer_principal": str(reviewer_principal),
        "reviewed_at": reviewed_at or _now_iso(),
        "event_audio_pcm_sha256": event_audio_pcm_sha256,
        "decision": decision,
        "taxonomy_version": taxonomy_version,
        "rationale": rationale,
    }

    if private_key is not None and key_id is None:
        raise ValueError("private_key was given without key_id; both are required together")

    if private_key is not None:
        subject = _record_subject(body)
        sig = _sign.sign(subject.encode("ascii"), private_key)
        return {**body, "signer": {"key_id": key_id, "algo": "ed25519"}, "signature": sig}

    saved = _sign.load_signing_key(key_id)
    if saved is not None:
        kid, priv = saved
        subject = _record_subject(body)
        sig = _sign.sign(subject.encode("ascii"), priv)
        return {**body, "signer": {"key_id": kid, "algo": "ed25519"}, "signature": sig}

    if hmac_key is not None:
        subject = _record_subject(body)
        sig = hmac.new(hmac_key, subject.encode("ascii"), hashlib.sha256).hexdigest()
        return {**body, "signer": {"key_id": _hmac_key_id(hmac_key), "algo": "hmac"},
                "signature": sig}

    shared = _receipt.load_key()
    if shared is not None:
        subject = _record_subject(body)
        sig = hmac.new(shared, subject.encode("ascii"), hashlib.sha256).hexdigest()
        return {**body, "signer": {"key_id": _hmac_key_id(shared), "algo": "hmac"},
                "signature": sig}

    raise NoSigningKeyConfigured(
        "no signing key configured: generate an Ed25519 keypair "
        "(pip install 'hotato[sign]', then hotato.sign.keygen() + "
        "save_signing_key()/save_trust()), or set a shared HMAC key "
        "(the HOTATO_ATTEST_KEY env var, or ~/.hotato/attest.key) before "
        "minting a label-record. Without either, a label stays an explicit, "
        "operator-asserted expectation (label_authority='asserted'), never a "
        "falsely-elevated signed 'human' attestation."
    )


def verify_label_record(record: dict, *, pubkey_or_key, event_pcm_sha256=None) -> dict:
    """Verify a label-record's signature (given the caller-supplied key
    material) AND that it is bound to the exact decoded audio named by
    ``event_pcm_sha256``. The audio binding is MANDATORY: a positive
    ("human" / "human-shared") authority is NEVER granted on signature alone.
    Without a non-None ``event_pcm_sha256`` to check the record's
    ``event_audio_pcm_sha256`` field against, this refuses (K5): a validly
    signed record could otherwise be lifted from the recording it was made
    about onto an unrelated (or audio-less) fixture, forging a "human"
    attestation.

    Returns ``{ok, authority, reason}``:
      * ``authority`` is ``"human"`` for a valid Ed25519 signature,
        ``"human-shared"`` for a valid HMAC signature, or ``None`` for
        anything that does not verify -- a tampered body, a wrong key, an
        unbound event (no binding hash supplied, or a hash that does not
        match), a malformed record, or an unknown/missing algo. A refusal is
        never silently downgraded to a weaker but still-positive authority;
        the caller decides what a refused record means for its own evidence
        vocabulary.

    Never raises: a missing ``[sign]`` extra needed to check an Ed25519
    signature comes back as a clean ``ok: False``, not a crash.
    """
    if not isinstance(record, dict):
        return {"ok": False, "authority": None, "reason": "not a label-record object"}
    if record.get("schema") != SCHEMA:
        return {"ok": False, "authority": None,
                "reason": f"unexpected schema {record.get('schema')!r}"}
    if event_pcm_sha256 is None:
        # The audio binding is mandatory, not opportunistic: with no event
        # audio hash to check against, we cannot confirm this signed record was
        # made about THIS recording, so it earns no positive authority. Skipping
        # the check here (the pre-fix behaviour) let any validly signed record
        # be trusted as "human"/"human-shared" for a fixture whose audio it was
        # never bound to -- the K5 signature-reuse forgery this module exists to
        # prevent.
        return {"ok": False, "authority": None,
                "reason": "cannot confirm the label-record is bound to this event's "
                          "decoded audio (no event audio hash supplied to check "
                          "event_audio_pcm_sha256 against)"}
    if record.get("event_audio_pcm_sha256") != event_pcm_sha256:
        return {"ok": False, "authority": None,
                "reason": "label-record is not bound to this event's decoded audio "
                          "(event_audio_pcm_sha256 mismatch)"}
    signer = record.get("signer") or {}
    algo = signer.get("algo")
    sig = record.get("signature")
    if not sig or not isinstance(sig, str):
        return {"ok": False, "authority": None, "reason": "no signature present"}
    subject = _record_subject(record)
    if algo == "ed25519":
        try:
            ok = _sign.verify(subject.encode("ascii"), sig, pubkey_or_key)
        except BackendUnavailable as exc:
            return {"ok": False, "authority": None,
                    "reason": f"cannot verify an Ed25519 label-record: {exc}"}
        if not ok:
            return {"ok": False, "authority": None,
                    "reason": "Ed25519 signature does not verify (tampered body or wrong key)"}
        return {"ok": True, "authority": "human",
                "reason": "Ed25519-signed label-record verified"}
    if algo == "hmac":
        if not isinstance(pubkey_or_key, (bytes, bytearray)):
            return {"ok": False, "authority": None,
                    "reason": "no shared-secret key available to verify an HMAC label-record"}
        expected = hmac.new(bytes(pubkey_or_key), subject.encode("ascii"), hashlib.sha256).hexdigest()
        if not hmac.compare_digest(expected, sig):
            return {"ok": False, "authority": None,
                    "reason": "HMAC signature does not verify (tampered body or wrong key)"}
        return {"ok": True, "authority": "human-shared",
                "reason": "HMAC-signed label-record verified (shared-secret tier)"}
    return {"ok": False, "authority": None, "reason": f"unknown signer algo {algo!r}"}


def verify_label_record_local(record: dict, *, event_pcm_sha256=None) -> dict:
    """Verify a label-record using ONLY locally resolvable key material: the
    local Ed25519 trust registry (``~/.hotato/trust/<key_id>.pub``) for an
    ed25519 signer, or the shared HMAC key (:func:`hotato.receipt.load_key`)
    for an hmac signer. This is what :func:`hotato.manifest.build_manifest`
    calls -- it never has a caller-supplied key, only what this machine
    already trusts.

    The audio binding is MANDATORY here too: ``event_pcm_sha256`` is forwarded
    unchanged to :func:`verify_label_record`, so a caller that cannot supply the
    event's decoded-audio hash gets a clean refusal (authority ``None``), never
    a signature-only "human"/"human-shared".

    Never raises: an unresolvable key_id, an untrusted signer, a missing
    ``[sign]`` extra, or a malformed record are all a clean refusal."""
    if not isinstance(record, dict):
        return {"ok": False, "authority": None, "reason": "not a label-record object"}
    signer = record.get("signer") or {}
    algo = signer.get("algo")
    if algo == "ed25519":
        key_id = signer.get("key_id")
        if not key_id:
            return {"ok": False, "authority": None,
                    "reason": "ed25519 label-record has no signer.key_id to look up"}
        try:
            pub = _sign.load_trust(key_id)
        except ValueError as exc:
            return {"ok": False, "authority": None, "reason": str(exc)}
        if pub is None:
            return {"ok": False, "authority": None,
                    "reason": f"no locally trusted Ed25519 key for key_id {key_id!r}"}
        return verify_label_record(record, pubkey_or_key=pub, event_pcm_sha256=event_pcm_sha256)
    if algo == "hmac":
        key = _receipt.load_key()
        if key is None:
            return {"ok": False, "authority": None,
                    "reason": "no shared HMAC key configured to verify this label-record"}
        return verify_label_record(record, pubkey_or_key=key, event_pcm_sha256=event_pcm_sha256)
    return {"ok": False, "authority": None,
            "reason": f"unknown or missing signer algo {algo!r}"}


def label_record_path(labels_dir: str, fixture_id: str) -> str:
    return os.path.join(labels_dir, f"{fixture_id}.label.json")


def save_label_record(path: str, record: dict) -> None:
    """Write a label-record JSON file. Not secret material (unlike a private
    key): a plain write is fine, no special permissions needed."""
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(record, fh, indent=2)
        fh.write("\n")


def load_label_record(path: str) -> Optional[dict]:
    """Read a label-record JSON file. Returns ``None`` if the path does not
    exist; a malformed file raises ``ValueError`` (exit 2, honest reason)."""
    try:
        with _open_regular(path, "r", encoding="utf-8") as fh:
            data = fh.read()
    except OSError:
        return None
    if not data.strip():
        return None
    try:
        return json.loads(data)
    except json.JSONDecodeError as exc:
        raise ValueError(f"{path!r} is not a valid label-record JSON file: {exc}") from exc


__all__ = [
    "SCHEMA",
    "TAXONOMY_VERSION_DEFAULT",
    "NoSigningKeyConfigured",
    "mint_label_record",
    "verify_label_record",
    "verify_label_record_local",
    "label_record_path",
    "save_label_record",
    "load_label_record",
]
