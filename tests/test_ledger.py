"""Temporal precommit + replay-defense ledger (hotato.ledger), Evidence
Kernel v2 K4.

Covers:
  * generate_challenge() is unpredictable: os.urandom-based, varies across
    calls even with identical trial/manifest inputs (NOT derivable from
    battery+inputs);
  * commit_challenge() -> bind_receipt() is an append-only, Merkle-chained
    log: verify_chain() holds after several commit/bind pairs;
  * a TAMPERED middle entry is detected by verify_chain();
  * bind_receipt() refuses a duplicate provider_call_id or a reused nonce
    (replay defense), and refuses a receipt with no matching precommit or a
    mismatched challenge;
  * a signed head (Ed25519 when [sign] is configured; HMAC fallback; clean
    unsigned when neither is configured -- never a crash either way).
"""
from __future__ import annotations

import pytest

from hotato import ledger as L
from hotato import receipt as R
from hotato import sign as S


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


def _paths(tmp_path):
    return (
        str(tmp_path / "commit.log"),
        str(tmp_path / "commit.head"),
    )


def _receipt_for(commit_entry, *, provider_call_id="call-1", pcm_sha256="deadbeef"):
    return R.build_receipt(
        trial_id=commit_entry["trial_id"],
        nonce=commit_entry["nonce"],
        challenge=commit_entry["challenge"],
        recording_locator="rec.wav",
        raw_sha256="rawhash",
        pcm_sha256=pcm_sha256,
        runner="test-runner",
        provider_call_id=provider_call_id,
    )


# --- unpredictable challenge -------------------------------------------------

def test_generate_challenge_varies_across_calls():
    a = L.generate_challenge()
    b = L.generate_challenge()
    assert a != b
    assert len(a) == 64  # 32 bytes hex-encoded
    bytes.fromhex(a)  # valid hex


def test_challenge_not_derivable_from_identical_inputs(tmp_path):
    log_path, head_path = _paths(tmp_path)
    e1 = L.commit_challenge(trial_id="t", manifest_hash="m", ledger_path=log_path, head_path=head_path)
    e2 = L.commit_challenge(trial_id="t", manifest_hash="m", ledger_path=log_path, head_path=head_path)
    # Same trial_id/manifest_hash, but the challenge (and default nonce) still
    # differ every time: nothing here is a deterministic function of the
    # supplied inputs.
    assert e1["challenge"] != e2["challenge"]
    assert e1["nonce"] != e2["nonce"]


# --- append-only, Merkle-chained log -----------------------------------------

def test_commit_then_bind_chain_verifies(tmp_path):
    log_path, head_path = _paths(tmp_path)
    commit = L.commit_challenge(trial_id="t1", manifest_hash="m1", ledger_path=log_path, head_path=head_path)
    receipt = _receipt_for(commit)
    bound = L.bind_receipt(receipt, ledger_path=log_path, head_path=head_path)

    assert commit["seq"] == 0
    assert bound["seq"] == 1
    assert bound["prev_hash"] == commit["entry_hash"]

    result = L.verify_chain(ledger_path=log_path)
    assert result == {"ok": True, "length": 2, "reason": result["reason"]}


def test_chain_grows_over_multiple_commit_bind_pairs(tmp_path):
    log_path, head_path = _paths(tmp_path)
    for i in range(4):
        commit = L.commit_challenge(trial_id=f"t{i}", manifest_hash="m", ledger_path=log_path, head_path=head_path)
        receipt = _receipt_for(commit, provider_call_id=f"call-{i}")
        L.bind_receipt(receipt, ledger_path=log_path, head_path=head_path)

    result = L.verify_chain(ledger_path=log_path)
    assert result["ok"] is True
    assert result["length"] == 8


def test_tampered_middle_entry_is_detected(tmp_path):
    log_path, head_path = _paths(tmp_path)
    for i in range(3):
        commit = L.commit_challenge(trial_id=f"t{i}", manifest_hash="m", ledger_path=log_path, head_path=head_path)
        receipt = _receipt_for(commit, provider_call_id=f"call-{i}")
        L.bind_receipt(receipt, ledger_path=log_path, head_path=head_path)

    with open(log_path, "r", encoding="utf-8") as fh:
        lines = fh.read().splitlines()
    assert len(lines) == 6

    # Tamper with a MIDDLE entry's manifest_hash, without touching its stored
    # entry_hash -- this is the "attacker forgot to also forge the hash" case.
    import json as _json
    entry = _json.loads(lines[2])
    entry["manifest_hash"] = "forged"
    lines[2] = _json.dumps(entry)
    with open(log_path, "w", encoding="utf-8") as fh:
        fh.write("\n".join(lines) + "\n")

    result = L.verify_chain(ledger_path=log_path)
    assert result["ok"] is False
    assert "entry 2" in result["reason"]


def test_tampered_entry_with_recomputed_hash_still_breaks_the_chain(tmp_path):
    log_path, head_path = _paths(tmp_path)
    for i in range(3):
        commit = L.commit_challenge(trial_id=f"t{i}", manifest_hash="m", ledger_path=log_path, head_path=head_path)
        receipt = _receipt_for(commit, provider_call_id=f"call-{i}")
        L.bind_receipt(receipt, ledger_path=log_path, head_path=head_path)

    with open(log_path, "r", encoding="utf-8") as fh:
        lines = fh.read().splitlines()

    import json as _json
    entry = _json.loads(lines[2])
    entry["manifest_hash"] = "forged"
    # Recompute entry_hash so THIS entry's own hash matches its new content --
    # the chain must still catch it via the NEXT entry's now-stale prev_hash.
    recompute = {k: v for k, v in entry.items() if k != "entry_hash"}
    entry["entry_hash"] = L._entry_hash(recompute)
    lines[2] = _json.dumps(entry)
    with open(log_path, "w", encoding="utf-8") as fh:
        fh.write("\n".join(lines) + "\n")

    result = L.verify_chain(ledger_path=log_path)
    assert result["ok"] is False
    assert "entry 3" in result["reason"]  # the FOLLOWING entry's prev_hash breaks


def test_empty_ledger_verifies_trivially(tmp_path):
    log_path, _head_path = _paths(tmp_path)
    result = L.verify_chain(ledger_path=log_path)
    assert result == {"ok": True, "length": 0, "reason": result["reason"]}


# --- replay defense -----------------------------------------------------

def test_bind_without_precommit_raises(tmp_path):
    log_path, head_path = _paths(tmp_path)
    receipt = R.build_receipt(
        trial_id="ghost", nonce="never-committed", challenge="also-never-committed",
        recording_locator="rec.wav", raw_sha256="x", pcm_sha256="y", runner="r",
        provider_call_id="call-1",
    )
    with pytest.raises(ValueError, match="no temporal precommit"):
        L.bind_receipt(receipt, ledger_path=log_path, head_path=head_path)


def test_bind_requires_a_challenge_on_the_receipt(tmp_path):
    log_path, head_path = _paths(tmp_path)
    commit = L.commit_challenge(trial_id="t", manifest_hash="m", ledger_path=log_path, head_path=head_path)
    receipt = R.build_receipt(
        trial_id=commit["trial_id"], nonce=commit["nonce"],  # no challenge
        recording_locator="rec.wav", raw_sha256="x", pcm_sha256="y", runner="r",
        provider_call_id="call-1",
    )
    with pytest.raises(ValueError, match="no 'challenge'"):
        L.bind_receipt(receipt, ledger_path=log_path, head_path=head_path)


def test_bind_mismatched_challenge_raises(tmp_path):
    log_path, head_path = _paths(tmp_path)
    commit = L.commit_challenge(trial_id="t", manifest_hash="m", ledger_path=log_path, head_path=head_path)
    receipt = R.build_receipt(
        trial_id=commit["trial_id"], nonce=commit["nonce"], challenge="wrong-challenge",
        recording_locator="rec.wav", raw_sha256="x", pcm_sha256="y", runner="r",
        provider_call_id="call-1",
    )
    with pytest.raises(ValueError, match="does not match the committed challenge"):
        L.bind_receipt(receipt, ledger_path=log_path, head_path=head_path)


def test_replay_duplicate_provider_call_id_refused(tmp_path):
    log_path, head_path = _paths(tmp_path)
    commit1 = L.commit_challenge(trial_id="t1", manifest_hash="m", ledger_path=log_path, head_path=head_path)
    receipt1 = _receipt_for(commit1, provider_call_id="same-call-id")
    L.bind_receipt(receipt1, ledger_path=log_path, head_path=head_path)

    commit2 = L.commit_challenge(trial_id="t2", manifest_hash="m", ledger_path=log_path, head_path=head_path)
    receipt2 = _receipt_for(commit2, provider_call_id="same-call-id")
    with pytest.raises(L.ReplayError, match="provider_call_id"):
        L.bind_receipt(receipt2, ledger_path=log_path, head_path=head_path)


def test_replay_reused_nonce_refused(tmp_path):
    log_path, head_path = _paths(tmp_path)
    commit = L.commit_challenge(trial_id="t1", manifest_hash="m", ledger_path=log_path, head_path=head_path)
    receipt = _receipt_for(commit, provider_call_id="call-a")
    L.bind_receipt(receipt, ledger_path=log_path, head_path=head_path)

    # Same nonce reused for a second (distinct) receipt -- even with a
    # different provider_call_id, the reused nonce alone is refused.
    receipt_again = R.build_receipt(
        trial_id=commit["trial_id"], nonce=commit["nonce"], challenge=commit["challenge"],
        recording_locator="rec2.wav", raw_sha256="x2", pcm_sha256="y2", runner="r",
        provider_call_id="call-b",
    )
    with pytest.raises(L.ReplayError, match="nonce"):
        L.bind_receipt(receipt_again, ledger_path=log_path, head_path=head_path)


def test_replay_error_is_a_value_error_subclass():
    assert issubclass(L.ReplayError, ValueError)


# --- signed head ---------------------------------------------------------

def test_head_unsigned_when_no_signer_configured(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.setenv("USERPROFILE", str(tmp_path / "home"))
    monkeypatch.delenv("HOTATO_ATTEST_KEY", raising=False)
    log_path, head_path = _paths(tmp_path)
    commit = L.commit_challenge(trial_id="t", manifest_hash="m", ledger_path=log_path, head_path=head_path)

    head = L.read_head(head_path=head_path)
    assert head is not None
    assert head["entry_hash"] == commit["entry_hash"]
    assert head["signature"] is None


def test_head_hmac_signed_when_shared_key_configured(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.setenv("USERPROFILE", str(tmp_path / "home"))
    monkeypatch.setenv("HOTATO_ATTEST_KEY", "sharedsecret")
    log_path, head_path = _paths(tmp_path)
    commit = L.commit_challenge(trial_id="t", manifest_hash="m", ledger_path=log_path, head_path=head_path)

    head = L.read_head(head_path=head_path)
    assert head["signature"]["algorithm"] == "hmac-sha256"
    assert head["entry_hash"] == commit["entry_hash"]


@CRYPTO
def test_head_ed25519_signed_when_local_key_configured(tmp_path, monkeypatch):
    home = tmp_path / "home"
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("USERPROFILE", str(home))
    monkeypatch.delenv("HOTATO_SIGN_KEY_ID", raising=False)
    priv, pub, key_id = S.keygen()
    S.save_signing_key(key_id, priv)

    log_path, head_path = _paths(tmp_path)
    commit = L.commit_challenge(trial_id="t", manifest_hash="m", ledger_path=log_path, head_path=head_path)

    head = L.read_head(head_path=head_path)
    sig = head["signature"]
    assert sig["algorithm"] == "ed25519"
    assert sig["signer"]["key_id"] == key_id
    head_body = {k: v for k, v in head.items() if k != "signature"}
    from hotato.manifest import canonical_json
    assert S.verify(canonical_json(head_body).encode("utf-8"), sig["value"], pub) is True
