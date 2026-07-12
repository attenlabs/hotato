"""Test helper for the Evidence Kernel v2 (K5) signed label-record.

A test that needs a genuinely human-attested label (``label_authority ==
"human"``) must mint a REAL signed label-record instead of asserting the tier
from a bare expectation field -- that bare-expectation shortcut is exactly the
bug K5 closes. This module signs one with a fresh, in-memory Ed25519 keypair
(no filesystem key registry, no HOME/tmp_path juggling needed) and attaches it
to a battery envelope's event in place, exactly where ``core.run_suite`` would
have put a fixture-minted one.
"""
from __future__ import annotations

from hotato import labelrecord as lr
from hotato import manifest as m
from hotato import sign


def sign_event_human(event: dict, *, reviewer: str = "test-reviewer",
                     decision=None, rationale=None) -> dict:
    """Mint a real Ed25519-signed label-record bound to ``event``'s own
    stimulus PCM hash, attach it to ``event["label_record"]`` in place, and
    return the minted record. ``event`` must be a battery envelope event (the
    same shape ``manifest.build_manifest`` consumes) with an
    ``audio_provenance`` block, as every scored suite event carries.

    ``decision`` defaults to the event's own ``expected_yield`` (yield/hold),
    matching "a human ran the workflow and confirmed this expectation."
    """
    pcm = m._stimulus_pcm(event)
    assert pcm, "event has no stimulus_pcm to bind the label-record to"
    if decision is None:
        decision = "yield" if event.get("expected_yield", True) else "hold"
    priv, pub, key_id = sign.keygen()
    record = lr.mint_label_record(
        reviewer_principal=reviewer,
        event_audio_pcm_sha256=pcm,
        decision=decision,
        rationale=rationale,
        private_key=priv,
        key_id=key_id,
    )
    # sanity: this must genuinely verify as "human" against the public half,
    # so a test that uses this helper is exercising the real signature path,
    # not a shortcut.
    check = lr.verify_label_record(record, pubkey_or_key=pub, event_pcm_sha256=pcm)
    assert check["ok"] and check["authority"] == "human", check
    event["label_record"] = record
    return record


def sign_event_human_shared(event: dict, *, key: bytes = b"test-shared-hmac-key",
                            reviewer: str = "test-reviewer", decision=None,
                            rationale=None) -> dict:
    """Same as :func:`sign_event_human` but HMAC-signed (the "human-shared"
    tier), for a test that exercises the shared-secret path specifically."""
    pcm = m._stimulus_pcm(event)
    assert pcm, "event has no stimulus_pcm to bind the label-record to"
    if decision is None:
        decision = "yield" if event.get("expected_yield", True) else "hold"
    record = lr.mint_label_record(
        reviewer_principal=reviewer,
        event_audio_pcm_sha256=pcm,
        decision=decision,
        rationale=rationale,
        hmac_key=key,
    )
    check = lr.verify_label_record(record, pubkey_or_key=key, event_pcm_sha256=pcm)
    assert check["ok"] and check["authority"] == "human-shared", check
    event["label_record"] = record
    return record
