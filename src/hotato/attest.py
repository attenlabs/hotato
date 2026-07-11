"""Contract authenticity: a canonical semantic digest plus an optional signature.

The ``.hotato`` bundle's ``MANIFEST.sha256.json`` proves only INTERNAL byte
consistency, and ``contract pack`` recomputes it on every pack. So an attacker
can unpack a bundle, loosen ``policy.pass_conditions`` in ``contract.json``, and
re-pack: the fresh manifest is self-consistent and ``contract verify`` /
``contract unpack`` accept it, because verify reads policy straight from the
mutable ``contract.json`` and never consults the manifest.

This module adds the missing binding. :func:`canonical_contract_digest` hashes
the contract's SEMANTIC identity -- schema, label, policy, source-audio hash,
scorer version + config marker, creator identity, timestamp -- into one sha256.
That digest is embedded at creation time. Recomputing it at verify/unpack time
and comparing to the embedded value detects a body edited after creation (a
loosened policy changes the digest). :func:`sign` optionally covers the digest
with an HMAC-SHA256 signature so an AUTHENTICATED bundle is distinguishable from
one that is merely internally consistent.

Authenticity is an ADDITIONAL axis, orthogonal to the pass/fail re-scoring:

  * ``tampered``    -- embedded digest present but does not match the recomputed
                       one; the contract body was edited after creation. FAILS.
  * ``unattested``  -- no embedded digest at all (a legacy bundle built before
                       attestation). Usable, but never authenticated.
  * ``unsigned``    -- digest matches, but there is no verifying signature.
                       "unsigned, internally consistent evidence", NEVER
                       "authenticated".
  * ``authenticated`` -- digest matches AND an HMAC-SHA256 signature verifies
                       under the caller's key.

An unsigned bundle is ALWAYS reported ``unsigned`` (or ``unattested``), never
``authenticated`` -- so even an attacker who recomputes the embedded digest over
a loosened policy cannot forge authentication without the signing key.

Zero third-party dependencies: stdlib ``hashlib`` / ``hmac`` / ``json`` / ``os``
only (mirrors the manifest module's zero-dependency posture).
"""
from __future__ import annotations

import hashlib
import hmac
import json
import os
from datetime import datetime, timezone
from typing import Optional, Tuple

from . import __version__

SCHEMA_VERSION = "1"
STATEMENT = "hotato contract canonical identity digest v1"
ATTESTATION_NAME = "attestation.json"

# Optional HMAC key sources; signing stays entirely optional (absent -> unsigned).
ATTEST_KEY_ENV = "HOTATO_ATTEST_KEY"
ATTEST_KEY_FILE = os.path.join("~", ".hotato", "attest.key")

# `create_contract` always scores with the default ScoreConfig(); a contract
# that starts recording a non-default scorer config can override this by storing
# contract["scorer"]["config_marker"]. Binding the marker means a bundle rescored
# under a different scorer config no longer matches its embedded digest.
DEFAULT_SCORER_CONFIG_MARKER = "default-score-config"


def _now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def canonical_json(obj) -> str:
    """Deterministic JSON: sorted keys, no insignificant whitespace, so two
    equal objects hash identically regardless of key order. Kept local (mirrors
    ``manifest.canonical_json``) so this module stays self-contained."""
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=True)


def _get(d, *path, default=None):
    """Nested ``dict.get`` that returns ``default`` on any missing/None hop."""
    cur = d
    for key in path:
        if not isinstance(cur, dict):
            return default
        cur = cur.get(key)
        if cur is None:
            return default
    return cur


def _scorer_version(contract: dict) -> str:
    """The scorer package version bound by the digest. Read from the contract's
    stamped ``scorer`` block (so a bundle created under one hotato version keeps
    verifying after an upgrade); the running ``__version__`` is only the
    fallback used to STAMP a contract that has none yet."""
    return _get(contract, "scorer", "package_version", default=None) or __version__


def _scorer_config_marker(contract: dict) -> str:
    return _get(contract, "scorer", "config_marker", default=None) or DEFAULT_SCORER_CONFIG_MARKER


def canonical_contract_digest(contract: dict) -> Tuple[str, list]:
    """sha256 over the contract's canonical semantic identity.

    Returns ``(digest_hex, digest_fields)`` where ``digest_fields`` is the sorted
    list of field names that fed the digest (for transparency). The embedded
    ``attestation`` block is deliberately NOT a digest input, so recomputing the
    digest over a bundle that already carries one is stable and non-circular.
    """
    subject = {
        "contract_schema": contract.get("schema"),
        "kind": contract.get("kind"),
        "label.expected_behavior": _get(contract, "label", "expected_behavior"),
        "label.label_source": _get(contract, "label", "label_source"),
        "label.label_revision": _get(contract, "label", "label_revision"),
        "label.rationale": _get(contract, "label", "rationale"),
        # The whole policy object (pass_conditions + opposite_risk_required +
        # anything future): a loosened bound anywhere in it moves the digest.
        "policy": contract.get("policy"),
        "source.source_audio_sha256": _get(contract, "source", "source_audio_sha256"),
        "source.decoded_pcm_sha256": _get(contract, "source", "decoded_pcm_sha256"),
        # The BUNDLED clip's raw + decoded-PCM identity: signing these binds the
        # actual audio/event.wav that ships in the bundle to the signature, so
        # verify can refuse a bundle whose audio was replaced after creation.
        "source.bundle_audio_sha256": _get(contract, "source", "bundle_audio_sha256"),
        "source.bundle_pcm_sha256": _get(contract, "source", "bundle_pcm_sha256"),
        "source.recording_type": _get(contract, "source", "recording_type"),
        "source.channels": _get(contract, "source", "channels"),
        "scorer.package_version": _scorer_version(contract),
        "scorer.config_marker": _scorer_config_marker(contract),
        "identity.created_by": contract.get("created_by"),
        "identity.creator": _get(contract, "identity", "creator"),
        "identity.reviewer": _get(contract, "identity", "reviewer"),
        "created_at": contract.get("created_at"),
        "repo_commit": contract.get("repo_commit"),
    }
    digest_fields = sorted(subject.keys())
    canonical = {k: subject[k] for k in digest_fields}
    digest_hex = hashlib.sha256(canonical_json(canonical).encode("utf-8")).hexdigest()
    return digest_hex, digest_fields


def _hmac_hex(key: bytes, digest_hex: str) -> str:
    return hmac.new(key, digest_hex.encode("utf-8"), hashlib.sha256).hexdigest()


def sign(digest_hex: str, *, key: Optional[bytes], signer: str = "hotato",
         digest_fields: Optional[list] = None, repo_commit: Optional[str] = None,
         statement: str = STATEMENT, created_at: Optional[str] = None) -> dict:
    """Build a detached attestation over ``digest_hex``.

    ``key is None`` -> an UNSIGNED marker: algorithm ``"none"``, empty signature,
    but still recording the digest (so the bundle is distinguishable from one
    with no attestation at all). A key present -> algorithm ``"hmac-sha256"`` and
    ``signature = hmac_sha256(key, digest_hex)``. Shape matches
    ``schema/attestation.v1.json``.
    """
    if key is None:
        algorithm, signature = "none", ""
    else:
        algorithm, signature = "hmac-sha256", _hmac_hex(key, digest_hex)
    return {
        "schema_version": SCHEMA_VERSION,
        "subject_digest": digest_hex,
        "digest_fields": list(digest_fields or []),
        "statement": statement,
        "signer": signer,
        "algorithm": algorithm,
        "signature": signature,
        "created_at": created_at or _now_iso(),
        "repo_commit": repo_commit,
    }


def verify_attestation(contract: dict, attestation: dict, *,
                       key: Optional[bytes]) -> dict:
    """Recompute the contract's digest and check it against ``attestation``.

    Returns ``{ok, authenticated, reason, tier, recomputed_digest,
    subject_digest}``:

      * a digest that does not match the attestation's ``subject_digest`` -> the
        body was edited after signing: ``tier="tampered"``, ``ok=False``;
      * algorithm ``"hmac-sha256"`` + a key whose HMAC matches -> ``tier=
        "authenticated"``, ``authenticated=True``;
      * algorithm ``"none"``, no key available, or a signature that does not
        verify -> ``tier="unsigned"``, ``authenticated=False``, ``ok=True``,
        "unsigned, internally consistent evidence".
    """
    recomputed, _ = canonical_contract_digest(contract)
    attestation = attestation or {}
    subject = attestation.get("subject_digest")
    algorithm = attestation.get("algorithm", "none")
    signature = attestation.get("signature") or ""

    if subject is not None and recomputed != subject:
        return {
            "ok": False,
            "authenticated": False,
            "tier": "tampered",
            "reason": (
                "canonical digest does not match the attestation's subject "
                "digest; the contract body was edited after it was signed"
            ),
            "recomputed_digest": recomputed,
            "subject_digest": subject,
        }

    if algorithm == "hmac-sha256":
        if key is None:
            return {
                "ok": True,
                "authenticated": False,
                "tier": "unsigned",
                "reason": (
                    "signed with hmac-sha256 but no key is available to verify "
                    "it; unsigned, internally consistent evidence"
                ),
                "recomputed_digest": recomputed,
                "subject_digest": subject,
            }
        expected = _hmac_hex(key, recomputed)
        if hmac.compare_digest(expected, signature):
            return {
                "ok": True,
                "authenticated": True,
                "tier": "authenticated",
                "reason": (
                    "canonical digest matches and the hmac-sha256 signature "
                    "verifies"
                ),
                "recomputed_digest": recomputed,
                "subject_digest": subject,
            }
        return {
            "ok": True,
            "authenticated": False,
            "tier": "unsigned",
            "reason": (
                "signature did not verify (wrong key or altered signature); "
                "cannot authenticate, treated as unsigned, internally "
                "consistent evidence"
            ),
            "recomputed_digest": recomputed,
            "subject_digest": subject,
        }

    # algorithm == "none" (or anything non-signing): an explicit unsigned marker.
    return {
        "ok": True,
        "authenticated": False,
        "tier": "unsigned",
        "reason": "unsigned, internally consistent evidence",
        "recomputed_digest": recomputed,
        "subject_digest": subject,
    }


def load_attest_key() -> Optional[bytes]:
    """Optional HMAC key from ``$HOTATO_ATTEST_KEY`` or ``~/.hotato/attest.key``;
    ``None`` when neither is set (so signing/authentication stays opt-in)."""
    raw = os.environ.get(ATTEST_KEY_ENV)
    if raw:
        return raw.encode("utf-8")
    path = os.path.expanduser(ATTEST_KEY_FILE)
    try:
        with open(path, "rb") as fh:
            data = fh.read().strip()
    except OSError:
        return None
    return data or None


def _stamp_scorer(contract: dict) -> None:
    """Record the scorer identity that the digest binds, if not already present.
    Additive: a contract built before this simply gets it stamped at digest time
    with the CURRENT scorer, which is what produced its measurement."""
    contract.setdefault("scorer", {
        "package_version": __version__,
        "config_marker": DEFAULT_SCORER_CONFIG_MARKER,
    })


def embed_attestation(contract: dict, *, bundle_dir: Optional[str] = None,
                      key: Optional[bytes] = None, signer: str = "hotato",
                      repo_commit: Optional[str] = None) -> dict:
    """Stamp the scorer identity, compute the canonical digest, embed it into
    ``contract["attestation"]``, and (when ``bundle_dir`` is given) write the
    detached :data:`ATTESTATION_NAME`.

    Signing is optional: ``key`` defaults to :func:`load_attest_key`, so an
    environment with no key produces an UNSIGNED marker (algorithm ``"none"``)
    and the contract still loads. Returns the detached attestation dict.
    """
    _stamp_scorer(contract)
    digest_hex, digest_fields = canonical_contract_digest(contract)
    if key is None:
        key = load_attest_key()
    attestation = sign(
        digest_hex, key=key, signer=signer,
        digest_fields=digest_fields, repo_commit=repo_commit,
    )
    contract["attestation"] = {
        "schema_version": SCHEMA_VERSION,
        "canonical_digest": digest_hex,
        "digest_fields": digest_fields,
        "algorithm": attestation["algorithm"],
        "signed": attestation["algorithm"] != "none",
        "statement": STATEMENT,
    }
    if bundle_dir is not None:
        path = os.path.join(bundle_dir, ATTESTATION_NAME)
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(json.dumps(attestation, indent=2, sort_keys=True) + "\n")
    return attestation


def load_detached_attestation(bundle_dir: str) -> Optional[dict]:
    """Read ``<bundle>/attestation.json`` if present, else ``None``."""
    path = os.path.join(bundle_dir, ATTESTATION_NAME)
    try:
        with open(path, encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, json.JSONDecodeError):
        return None
    return data if isinstance(data, dict) else None


def assess_contract(contract: dict, *, bundle_dir: Optional[str] = None,
                    key: Optional[bytes] = None) -> dict:
    """The authenticity axis for one loaded contract.

    Recomputes the canonical digest and compares it to the digest embedded at
    creation (``contract["attestation"]["canonical_digest"]``); if a detached
    :data:`ATTESTATION_NAME` and a key are available, also checks the signature.

    Returns ``{authenticity, ok, authenticated, reason, recomputed_digest,
    embedded_digest, signature_present}`` where ``authenticity`` is one of
    ``tampered`` / ``unattested`` / ``unsigned`` / ``authenticated``. ``ok`` is
    False only for ``tampered``; ``authenticated`` is True only for
    ``authenticated``.
    """
    recomputed, _ = canonical_contract_digest(contract)
    embedded = _get(contract, "attestation", "canonical_digest", default=None)

    if embedded is None:
        return {
            "authenticity": "unattested",
            "ok": True,
            "authenticated": False,
            "reason": (
                "no embedded canonical digest (a bundle created before contract "
                "attestation); usable but unattested, never authenticated"
            ),
            "recomputed_digest": recomputed,
            "embedded_digest": None,
            "signature_present": False,
        }

    if recomputed != embedded:
        return {
            "authenticity": "tampered",
            "ok": False,
            "authenticated": False,
            "reason": (
                "the contract body was edited after creation: its canonical "
                "digest no longer matches the embedded one (a loosened policy, "
                "relabeled expectation, or swapped source would do this)"
            ),
            "recomputed_digest": recomputed,
            "embedded_digest": embedded,
            "signature_present": False,
        }

    attestation = load_detached_attestation(bundle_dir) if bundle_dir else None
    signature_present = bool(
        attestation and attestation.get("algorithm") == "hmac-sha256"
        and attestation.get("signature")
    )
    if attestation is not None:
        if key is None:
            key = load_attest_key()
        v = verify_attestation(contract, attestation, key=key)
        if v["authenticated"]:
            return {
                "authenticity": "authenticated",
                "ok": True,
                "authenticated": True,
                "reason": v["reason"],
                "recomputed_digest": recomputed,
                "embedded_digest": embedded,
                "signature_present": signature_present,
            }
        # A tampered detached attestation (subject mismatch) is still caught: the
        # embedded digest already matched here, but if the detached file disagrees
        # surface it rather than silently trusting.
        if v["tier"] == "tampered":
            return {
                "authenticity": "tampered",
                "ok": False,
                "authenticated": False,
                "reason": v["reason"],
                "recomputed_digest": recomputed,
                "embedded_digest": embedded,
                "signature_present": signature_present,
            }

    return {
        "authenticity": "unsigned",
        "ok": True,
        "authenticated": False,
        "reason": "unsigned, internally consistent evidence",
        "recomputed_digest": recomputed,
        "embedded_digest": embedded,
        "signature_present": signature_present,
    }


__all__ = [
    "SCHEMA_VERSION",
    "STATEMENT",
    "ATTESTATION_NAME",
    "ATTEST_KEY_ENV",
    "ATTEST_KEY_FILE",
    "DEFAULT_SCORER_CONFIG_MARKER",
    "canonical_json",
    "canonical_contract_digest",
    "sign",
    "verify_attestation",
    "load_attest_key",
    "embed_attestation",
    "load_detached_attestation",
    "assess_contract",
]
