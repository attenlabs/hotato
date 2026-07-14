"""Capture receipt: machine-attested proof that a recording is a FRESH call.

A fresh-recapture claim needs more than distinct PCM (an old call can be
resampled, re-gained, or codec-converted into new bytes). A capture runner that
placed the call emits a signed receipt binding the recording to a trial, an
agent/deployment, a provider call id, timestamps, and the exact decoded-PCM
hash. Without a receipt a distinct WAV is 'operator-asserted', never
machine-verified fresh recapture.

Zero-dependency: stdlib ``hmac``/``hashlib`` only. Signing is OPTIONAL; an
unsigned receipt still records origin metadata but is labelled unsigned.

See :mod:`hotato.ledger` for the OPTIONAL temporal-precommit + replay-defense
layer (Evidence Kernel v2, K4) that builds on top of this receipt: a fresh
unpredictable challenge committed to a local Merkle-chained log BEFORE
capture, and a refusal of any receipt that reuses a nonce or provider call id
already bound in that log.
"""
from __future__ import annotations

import hashlib
import hmac
import os
from typing import Optional

from .errors import open_regular as _open_regular
from .manifest import canonical_json

SCHEMA_VERSION = "1"


def _receipt_subject(receipt: dict) -> str:
    """Canonical digest of the origin-binding fields (everything except the
    signature), so a signature cannot be lifted onto a different recording."""
    body = {k: v for k, v in receipt.items() if k != "signature"}
    return hashlib.sha256(canonical_json(body).encode("utf-8")).hexdigest()


def build_receipt(
    *,
    trial_id: str,
    nonce: str,
    challenge: Optional[str] = None,
    recording_locator: str,
    raw_sha256: str,
    pcm_sha256: str,
    runner: str,
    agent_id: Optional[str] = None,
    deployment_id: Optional[str] = None,
    provider_call_id: Optional[str] = None,
    started_at: Optional[str] = None,
    completed_at: Optional[str] = None,
    config_snapshot_hash: Optional[str] = None,
    scenario_stimulus_hash: Optional[str] = None,
    channel_layout: Optional[str] = None,
    transformations: Optional[list] = None,
    adapter: Optional[str] = None,
    adapter_version: Optional[str] = None,
    key: Optional[bytes] = None,
) -> dict:
    """Build a capture receipt. If ``key`` is given, HMAC-sign the subject
    digest; else leave it unsigned (still usable, labelled unsigned).

    ``challenge`` optionally carries the unpredictable value committed BEFORE
    capture by :func:`hotato.ledger.commit_challenge` (Evidence Kernel v2,
    K4): passing the same ``nonce``/``challenge`` here lets
    :func:`hotato.ledger.bind_receipt` bind this finished receipt back to its
    temporal precommit and refuse a duplicate ``provider_call_id`` or a reused
    ``nonce`` (replay). Optional: a receipt built with no ``challenge`` is
    unchanged from before -- this strengthens the receipt when the caller
    opts in, it does not require it."""
    receipt = {
        "schema_version": SCHEMA_VERSION,
        "trial_id": trial_id,
        "nonce": nonce,
        "challenge": challenge,
        "agent_id": agent_id,
        "deployment_id": deployment_id,
        "provider_call_id": provider_call_id,
        "started_at": started_at,
        "completed_at": completed_at,
        "config_snapshot_hash": config_snapshot_hash,
        "scenario_stimulus_hash": scenario_stimulus_hash,
        "recording": {
            "locator": recording_locator,
            "raw_sha256": raw_sha256,
            "pcm_sha256": pcm_sha256,
            "channel_layout": channel_layout,
            "transformations": list(transformations or []),
        },
        "adapter": adapter,
        "adapter_version": adapter_version,
        "runner": runner,
        "signature": None,
    }
    if key is not None:
        subject = _receipt_subject(receipt)
        sig = hmac.new(key, subject.encode("ascii"), hashlib.sha256).hexdigest()
        receipt["signature"] = {
            "algorithm": "hmac-sha256",
            "subject_digest": subject,
            "value": sig,
        }
    return receipt


def verify_receipt(receipt: dict, *, pcm_sha256: str, key: Optional[bytes] = None) -> dict:
    """Check a capture receipt against a recording's actual decoded-PCM hash and
    (optionally) its signature.

    Returns {ok, attested, reason}. ``attested`` (machine-verified fresh
    recapture) is True only when the PCM matches AND a valid HMAC signature is
    present under the given key. A receipt whose PCM matches but that is unsigned
    (or has no key to check) is ok-but-unattested: operator-asserted, not
    machine-verified.
    """
    rec = receipt.get("recording") or {}
    if rec.get("pcm_sha256") != pcm_sha256:
        return {"ok": False, "attested": False,
                "reason": "recording PCM does not match the receipt's recorded pcm_sha256"}
    sig = receipt.get("signature")
    if not sig or sig.get("algorithm") == "none":
        return {"ok": True, "attested": False,
                "reason": "unsigned receipt: operator-asserted origin, not machine-verified"}
    if key is None:
        return {"ok": True, "attested": False,
                "reason": "signed receipt present but no verification key supplied"}
    subject = _receipt_subject(receipt)
    if subject != sig.get("subject_digest"):
        return {"ok": False, "attested": False,
                "reason": "receipt body was altered after signing (subject digest mismatch)"}
    expected = hmac.new(key, subject.encode("ascii"), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(expected, sig.get("value", "")):
        return {"ok": False, "attested": False,
                "reason": "receipt signature does not verify under this key"}
    return {"ok": True, "attested": True, "reason": "machine-verified fresh recapture"}


def load_key() -> Optional[bytes]:
    """Optional HMAC key from env ``HOTATO_ATTEST_KEY`` or ``~/.hotato/attest.key``."""
    env = os.environ.get("HOTATO_ATTEST_KEY")
    if env:
        return env.encode("utf-8")
    path = os.path.expanduser("~/.hotato/attest.key")
    try:
        with _open_regular(path) as fh:
            data = fh.read().strip()
            return data or None
    except OSError:
        return None


__all__ = ["SCHEMA_VERSION", "build_receipt", "verify_receipt", "load_key"]
