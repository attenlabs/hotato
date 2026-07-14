"""Temporal precommit + replay-defense ledger for capture receipts (Evidence
Kernel v2, K4).

A capture receipt (:mod:`hotato.receipt`) proves a recording's ORIGIN once it
exists, but nothing so far stops the origin metadata itself from being
crafted after the fact, or the same finished call from being bound into more
than one trial. This module closes that gap with two steps:

  1. BEFORE a scored recapture, the runner generates an unpredictable
     ``challenge`` (:func:`generate_challenge` -- ``os.urandom``-based, never
     derivable from the battery, the manifest, or any other input) and
     commits ``{manifest_hash, nonce, trial_id, challenge, ts}`` to an
     append-only, Merkle-chained local log (:func:`commit_challenge`).
     Committing BEFORE the call means a runner cannot pick the challenge to
     fit a call it already placed.
  2. AFTER capture, the finished :mod:`hotato.receipt` (which now carries the
     same ``nonce``/``challenge``) is bound back to its precommit
     (:func:`bind_receipt`). Binding refuses a ``provider_call_id`` or
     ``nonce`` already bound to an earlier receipt (replay), and refuses a
     receipt whose challenge does not match what was actually committed.

The ledger itself is a plain JSON-lines file, ``~/.hotato/ledger/commit.log``:
each line is one entry; each entry's ``entry_hash`` covers its own body
(including ``prev_hash``), so a line depends on every line before it -- an
append-only Merkle chain (:func:`verify_chain` detects a tampered/removed/
reordered entry anywhere in the chain, not just at the tip).

A signed head (``~/.hotato/ledger/commit.head``) is written on every append:
Ed25519-signed (the ``[sign]`` extra, :mod:`hotato.sign`) when a local signing
key is configured, else HMAC-signed with the shared ``hotato.receipt`` key
when THAT is configured, else left unsigned (the chain hash-linking alone
still makes tampering detectable; signing the head is an additional,
OPTIONAL layer on top). Entirely offline: no network call, ever.

Zero third-party dependencies at import time (mirrors ``sign.py``): only
stdlib (``hashlib``/``json``/``os``/``time``) is touched unless a caller
actually has an Ed25519 key configured, in which case ``hotato.sign`` lazily
reaches for ``cryptography`` exactly as it always has.

Additive and OPTIONAL: nothing here changes ``hotato.receipt``'s existing
behaviour. A receipt built with no ``challenge`` is exactly what it always
was; this module only strengthens things when a caller opts in to precommit
+ bind around it.
"""
from __future__ import annotations

import hashlib
import json
import os
import time
from typing import Optional

from ._engine.vad import BackendUnavailable
from .errors import open_regular as _open_regular
from .manifest import canonical_json

SCHEMA_VERSION = "1"

LEDGER_DIR = os.path.join("~", ".hotato", "ledger")
LEDGER_LOG_NAME = "commit.log"
LEDGER_HEAD_NAME = "commit.head"

# The hash a genesis entry (the first line in the log) chains to: there is no
# real previous entry, so ``prev_hash`` is pinned to a fixed, obviously-not-a-
# real-hash sentinel rather than left null (null would make an attacker's
# forged "first" entry with prev_hash=null indistinguishable from a genuine
# genesis).
GENESIS_HASH = "0" * 64


class ReplayError(ValueError):
    """A capture receipt reuses a ``provider_call_id`` or ``nonce`` already
    bound to an earlier receipt in the ledger. A ``ValueError`` subclass (like
    :class:`hotato.errors.ChannelRangeError`) so every existing ``except
    ValueError`` / the CLI's ``HANDLED`` contract still catches it (exit 2,
    structured error), but a distinct type so a caller can tell a replay
    attempt apart from any other malformed-binding ``ValueError``."""


def _ledger_dir() -> str:
    return os.path.expanduser(LEDGER_DIR)


def _log_path(path: Optional[str] = None) -> str:
    return path or os.path.join(_ledger_dir(), LEDGER_LOG_NAME)


def _head_path(path: Optional[str] = None) -> str:
    return path or os.path.join(_ledger_dir(), LEDGER_HEAD_NAME)


def _now_iso() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def generate_challenge() -> str:
    """A fresh unpredictable challenge: 32 bytes of ``os.urandom``, hex
    encoded. Takes no inputs -- it cannot be derived from the battery, the
    manifest, the trial id, or anything else the caller supplies, and two
    calls (even for the identical trial/manifest) never produce the same
    value. This is what makes the precommit a real temporal commitment rather
    than a value a runner could predict or replay ahead of time."""
    return os.urandom(32).hex()


def _entry_hash(entry_without_hash: dict) -> str:
    return hashlib.sha256(canonical_json(entry_without_hash).encode("utf-8")).hexdigest()


def _read_entries(log_path: str) -> list:
    """Every line of the ledger log, parsed. An absent log is a fresh,
    empty chain -- not an error (the ledger is created lazily on first
    commit)."""
    try:
        with _open_regular(log_path, "r", encoding="utf-8") as fh:
            data = fh.read()
    except OSError:
        return []
    entries = []
    for line in data.splitlines():
        line = line.strip()
        if not line:
            continue
        entries.append(json.loads(line))
    return entries


def _resolve_signer():
    """Best available local signer for the head, strongest tier first:
    Ed25519 (a saved key, :func:`hotato.sign.load_signing_key`), else the
    shared HMAC key (:func:`hotato.receipt.load_key`), else ``None`` (the
    head is then left unsigned; the chain's hash-linking is still fully
    tamper-evident on its own). Never raises: a missing ``[sign]`` extra with
    no HMAC key configured either is just "no signer available", not an
    error -- signing the head is additive, never required."""
    try:
        from . import sign as _sign
        saved = _sign.load_signing_key()
    except Exception:
        saved = None
    if saved is not None:
        return ("ed25519", saved)
    try:
        from . import receipt as _receipt
        shared = _receipt.load_key()
    except Exception:
        shared = None
    if shared is not None:
        return ("hmac", shared)
    return None


def _sign_head(head_body: dict) -> dict:
    """Sign ``head_body`` with the best available local signer. Returns the
    ``signature`` block to attach (``None`` if no signer is configured, or if
    an Ed25519 signer is configured but the ``[sign]`` extra is missing --
    never raises for either)."""
    signer = _resolve_signer()
    if signer is None:
        return None
    algo, material = signer
    payload = canonical_json(head_body).encode("utf-8")
    if algo == "ed25519":
        key_id, priv_bytes = material
        try:
            from . import sign as _sign
            sig_hex = _sign.sign(payload, priv_bytes)
            pub_bytes = _sign.derive_public_key(priv_bytes)
        except BackendUnavailable:
            return None
        return {
            "algorithm": "ed25519",
            "signer": _sign.signer_identity(key_id, pub_bytes, signer_role="ledger"),
            "value": sig_hex,
        }
    # algo == "hmac"
    import hashlib as _hashlib
    import hmac as _hmac
    sig_hex = _hmac.new(material, payload, _hashlib.sha256).hexdigest()
    key_id = _hashlib.sha256(material).hexdigest()[:16]
    return {"algorithm": "hmac-sha256", "signer": {"key_id": key_id}, "value": sig_hex}


def _write_head(entry: dict, *, head_path: str) -> dict:
    head_body = {
        "schema_version": SCHEMA_VERSION,
        "seq": entry["seq"],
        "entry_hash": entry["entry_hash"],
        "chain_length": entry["seq"] + 1,
        "ts": _now_iso(),
    }
    head = dict(head_body)
    head["signature"] = _sign_head(head_body)
    directory = os.path.dirname(head_path)
    os.makedirs(directory, exist_ok=True, mode=0o700)
    with open(head_path, "w", encoding="utf-8") as fh:  # open-ok: write mode, our own head file
        fh.write(canonical_json(head) + "\n")
    os.chmod(head_path, 0o600)
    return head


def _append_entry(fields: dict, *, ledger_path: Optional[str], head_path: Optional[str]) -> dict:
    log_path = _log_path(ledger_path)
    directory = os.path.dirname(log_path)
    os.makedirs(directory, exist_ok=True, mode=0o700)

    entries = _read_entries(log_path)
    seq = len(entries)
    prev_hash = entries[-1]["entry_hash"] if entries else GENESIS_HASH

    entry = dict(fields)
    entry["seq"] = seq
    entry["prev_hash"] = prev_hash
    entry["entry_hash"] = _entry_hash(entry)

    with open(log_path, "a", encoding="utf-8") as fh:  # open-ok: append mode, our own ledger file
        fh.write(canonical_json(entry) + "\n")
    os.chmod(log_path, 0o600)

    _write_head(entry, head_path=_head_path(head_path))
    return entry


def _find_commit(entries: list, *, trial_id, nonce) -> Optional[dict]:
    for e in entries:
        if e.get("kind") == "commit" and e.get("trial_id") == trial_id and e.get("nonce") == nonce:
            return e
    return None


def commit_challenge(
    *,
    trial_id: str,
    manifest_hash: str,
    nonce: Optional[str] = None,
    challenge: Optional[str] = None,
    ts: Optional[str] = None,
    ledger_path: Optional[str] = None,
    head_path: Optional[str] = None,
) -> dict:
    """Commit a temporal precommit BEFORE capture: ``{manifest_hash, nonce,
    trial_id, challenge, ts}`` appended to the Merkle-chained ledger.

    ``challenge`` and ``nonce`` default to a fresh :func:`generate_challenge`
    value each (independently) when not supplied, so a caller does not have
    to wire its own randomness through -- but can still pass an explicit
    ``nonce`` to correlate with an id it already tracks elsewhere. Returns the
    appended, hash-chained entry (including its ``entry_hash``/``seq``).
    """
    if not trial_id:
        raise ValueError("trial_id is required to commit a precommit challenge")
    if not manifest_hash:
        raise ValueError("manifest_hash is required to commit a precommit challenge")
    fields = {
        "schema_version": SCHEMA_VERSION,
        "kind": "commit",
        "ts": ts or _now_iso(),
        "trial_id": trial_id,
        "nonce": nonce or generate_challenge(),
        "challenge": challenge or generate_challenge(),
        "manifest_hash": manifest_hash,
    }
    return _append_entry(fields, ledger_path=ledger_path, head_path=head_path)


def bind_receipt(
    receipt: dict,
    *,
    ledger_path: Optional[str] = None,
    head_path: Optional[str] = None,
) -> dict:
    """Bind a finished :mod:`hotato.receipt` capture receipt back to its
    temporal precommit, refusing replay.

    Requires the receipt to carry the same ``trial_id``/``nonce``/
    ``challenge`` a prior :func:`commit_challenge` committed for this trial:

      * no matching commit for this ``trial_id``/``nonce`` -> ``ValueError``
        (there was no precommit, so this cannot be a genuine fresh recapture
        under this scheme);
      * the receipt's ``challenge`` does not match the committed one ->
        ``ValueError`` (tampered or wrong precommit cited);
      * this receipt's ``provider_call_id`` or ``nonce`` was already bound by
        an earlier receipt -> :class:`ReplayError` (refused: a call id or
        nonce is one-shot).

    On success, appends a chained ``bind`` entry recording the receipt's
    origin fields and returns it.
    """
    trial_id = receipt.get("trial_id")
    nonce = receipt.get("nonce")
    challenge = receipt.get("challenge")
    provider_call_id = receipt.get("provider_call_id")

    if not challenge:
        raise ValueError(
            "receipt has no 'challenge': bind_receipt requires a receipt built "
            "with the challenge committed by ledger.commit_challenge() for "
            "this trial (pass challenge=... to hotato.receipt.build_receipt)."
        )

    log_path = _log_path(ledger_path)
    entries = _read_entries(log_path)

    commit_entry = _find_commit(entries, trial_id=trial_id, nonce=nonce)
    if commit_entry is None:
        raise ValueError(
            f"no temporal precommit found for trial_id={trial_id!r} "
            f"nonce={nonce!r}; call ledger.commit_challenge() BEFORE capture."
        )
    if commit_entry.get("challenge") != challenge:
        raise ValueError(
            "receipt's challenge does not match the committed challenge for "
            "this trial_id/nonce (mismatch or tamper); refusing to bind."
        )

    for e in entries:
        if e.get("kind") != "bind":
            continue
        if nonce is not None and e.get("nonce") == nonce:
            raise ReplayError(
                f"nonce {nonce!r} was already bound to an earlier capture "
                "receipt; reuse is refused (replay defense)."
            )
        if provider_call_id is not None and e.get("provider_call_id") == provider_call_id:
            raise ReplayError(
                f"provider_call_id {provider_call_id!r} was already bound to "
                "an earlier capture receipt; reuse is refused (replay defense)."
            )

    receipt_body = {k: v for k, v in receipt.items() if k != "signature"}
    receipt_hash = hashlib.sha256(canonical_json(receipt_body).encode("utf-8")).hexdigest()

    fields = {
        "schema_version": SCHEMA_VERSION,
        "kind": "bind",
        "ts": _now_iso(),
        "trial_id": trial_id,
        "nonce": nonce,
        "challenge": challenge,
        "provider_call_id": provider_call_id,
        "receipt_hash": receipt_hash,
    }
    return _append_entry(fields, ledger_path=ledger_path, head_path=head_path)


def verify_chain(*, ledger_path: Optional[str] = None) -> dict:
    """Walk every entry in the ledger log and confirm the Merkle chain holds:
    each entry's ``entry_hash`` matches its own recomputed content, and each
    entry's ``prev_hash`` matches the previous entry's ``entry_hash`` (the
    genesis entry chains to :data:`GENESIS_HASH`).

    Returns ``{ok, length, reason}``. A tampered, reordered, or removed entry
    anywhere in the chain (not just at the tip) is detected: either its own
    stored ``entry_hash`` no longer matches its (now-changed) body, or -- if
    an attacker also patched the hash to match -- every entry AFTER it has a
    ``prev_hash`` that no longer matches, so the break is still caught at the
    first point it was introduced.
    """
    entries = _read_entries(_log_path(ledger_path))
    prev_hash = GENESIS_HASH
    for i, entry in enumerate(entries):
        stored_prev = entry.get("prev_hash")
        stored_hash = entry.get("entry_hash")
        if stored_prev != prev_hash:
            return {"ok": False, "length": len(entries),
                    "reason": f"entry {i}: prev_hash does not match the previous "
                              "entry's hash (chain broken)"}
        recompute = {k: v for k, v in entry.items() if k != "entry_hash"}
        if _entry_hash(recompute) != stored_hash:
            return {"ok": False, "length": len(entries),
                    "reason": f"entry {i}: entry_hash does not match its "
                              "recomputed content (tampered)"}
        prev_hash = stored_hash
    return {"ok": True, "length": len(entries),
            "reason": "chain verifies: every entry hashes correctly and "
                      "chains to the previous entry"}


def read_head(*, head_path: Optional[str] = None) -> Optional[dict]:
    """The last-written signed head record, or ``None`` if nothing has been
    committed yet."""
    path = _head_path(head_path)
    try:
        with _open_regular(path, "r", encoding="utf-8") as fh:
            data = fh.read().strip()
    except OSError:
        return None
    if not data:
        return None
    return json.loads(data)


__all__ = [
    "SCHEMA_VERSION",
    "LEDGER_DIR",
    "LEDGER_LOG_NAME",
    "LEDGER_HEAD_NAME",
    "GENESIS_HASH",
    "ReplayError",
    "generate_challenge",
    "commit_challenge",
    "bind_receipt",
    "verify_chain",
    "read_head",
]
