"""Privacy, retention, and redaction for Fleet.

Fleet reverses Hotato's local sharing default: audio is stored separately from
any UI, sharing is explicit, retention is a policy object, deletion leaves an
audit receipt, and audio redaction produces a DERIVED artifact (new PCM hash,
parent lineage, downgraded evidence) rather than silently replacing the
original evidence (plan §4/§14).

"Self-contained" and "offline" do not mean safe to share -- a report can embed
spoken PII. Nothing here is called a compliance guarantee; these are the
mechanisms a workspace uses to enforce its own policy.

Zero-dependency: stdlib only. Redaction here is a lossless-container derived
copy with silenced spans (a real derived medium, honestly labelled), never a
claim that the original was preserved.
"""
from __future__ import annotations

from ..errors import wav_read as _wav_read

import hashlib
import os
import struct
import wave
from typing import List, Optional

# sha256-of-canonical for the deletion receipt reuses the shared manifest
# primitives (finding #2); ``hashlib`` stays for the streaming PCM hash below.
from .. import manifest as _manifest


# --- retention / consent policy -------------------------------------------
def retention_policy(*, consent_basis: str, allowed_purposes: List[str],
                     retention_days: Optional[int], storage_location: str = "local",
                     export_allowed: bool = False, playback_allowed: bool = False,
                     transcript_allowed: bool = False, hosted_egress_allowed: bool = False,
                     pii_class: str = "unknown", public_sharing: bool = False,
                     legal_hold: bool = False) -> dict:
    """A retention/consent policy object attached to a recording or workspace.

    Defaults are conservative: no export, no playback outside the tool, no
    transcript, no hosted-model egress, not public. A workspace opts IN."""
    return {
        "schema_version": "1",
        "consent_basis": consent_basis,
        "allowed_purposes": list(allowed_purposes),
        "retention_days": retention_days,
        "storage_location": storage_location,
        "export_allowed": export_allowed,
        "playback_allowed": playback_allowed,
        "transcript_allowed": transcript_allowed,
        "hosted_egress_allowed": hosted_egress_allowed,
        "pii_class": pii_class,               # none | pii | phi | unknown
        "public_sharing": public_sharing,     # a high-stakes prohibition when False
        "legal_hold": legal_hold,
    }


def is_expired(policy: dict, captured_at: float, now: float) -> bool:
    """Retention expiry. A legal hold blocks expiry regardless of age."""
    if policy.get("legal_hold"):
        return False
    days = policy.get("retention_days")
    if days is None:
        return False
    return (now - captured_at) > (days * 86400)


def enforce_share(policy: dict, action: str) -> dict:
    """Gate a sharing action (export/playback/transcript/hosted_egress/public)
    against the policy. Returns {allowed, reason}."""
    key = {
        "export": "export_allowed",
        "playback": "playback_allowed",
        "transcript": "transcript_allowed",
        "hosted_egress": "hosted_egress_allowed",
    }.get(action)
    if action == "public":
        allowed = bool(policy.get("public_sharing"))
        return {"allowed": allowed,
                "reason": "public sharing is prohibited by policy" if not allowed else "permitted"}
    if key is None:
        return {"allowed": False, "reason": f"unknown share action {action!r}"}
    allowed = bool(policy.get(key))
    return {"allowed": allowed,
            "reason": f"{action} not permitted by policy" if not allowed else "permitted"}


# --- deletion with an audit receipt ---------------------------------------
def deletion_receipt(*, subject_id: str, subject_kind: str, pcm_sha256: str,
                     reason: str, actor: str, at: float,
                     legal_hold: bool = False) -> dict:
    """A durable, hashable record that a specific artifact was deleted (or
    blocked by legal hold), so a workspace can prove what happened without
    keeping the audio."""
    body = {
        "schema_version": "1",
        "subject_id": subject_id,
        "subject_kind": subject_kind,       # recording | candidate | report
        "pcm_sha256": pcm_sha256,
        "reason": reason,
        "actor": actor,
        "at": at,
        "blocked_by_legal_hold": legal_hold,
    }
    body["receipt_digest"] = _manifest._sha256_str(_manifest.canonical_json(body))
    return body


# --- redaction as a DERIVED artifact --------------------------------------
def redact_audio(src_wav: str, spans_sec: List[tuple], out_wav: str, *,
                 hop_ms: float = 10.0) -> dict:
    """Silence the given [start,end) second spans, writing a NEW file. Returns a
    derivation record: a NEW PCM hash, the parent's hash, the redacted spans, and
    an explicitly DOWNGRADED evidence statement. The original is never touched;
    the derived copy must never be presented as the original evidence."""
    with _wav_read(src_wav) as wf:
        nch, width, rate, n = (wf.getnchannels(), wf.getsampwidth(),
                               wf.getframerate(), wf.getnframes())
        raw = wf.readframes(n)
    if width != 2:
        raise ValueError("redaction supports 16-bit PCM WAV only")
    samples = bytearray(raw)
    frame_bytes = width * nch
    for (s, e) in spans_sec:
        a = max(0, int(s * rate)) * frame_bytes
        b = min(n, int(e * rate)) * frame_bytes
        for i in range(a, b):
            samples[i] = 0
    with wave.open(out_wav, "wb") as wf:
        wf.setnchannels(nch)
        wf.setsampwidth(width)
        wf.setframerate(rate)
        wf.writeframes(bytes(samples))
    parent_pcm = _pcm_sha256(src_wav)
    child_pcm = _pcm_sha256(out_wav)
    return {
        "schema_version": "1",
        "derived": True,
        "kind": "redacted-audio",
        "parent_pcm_sha256": parent_pcm,
        "pcm_sha256": child_pcm,
        "redacted_spans_sec": [list(sp) for sp in spans_sec],
        "evidence_statement": (
            "DERIVED redacted copy: spans were silenced. This is not the original "
            "recording and must not be scored as evidence of the original call; its "
            "evidence tier is downgraded and its lineage points at the parent."),
    }


def _pcm_sha256(path: str) -> str:
    h = hashlib.sha256()
    with _wav_read(path) as wf:
        while True:
            chunk = wf.readframes(1 << 16)
            if not chunk:
                break
            h.update(chunk)
    return h.hexdigest()


__all__ = ["retention_policy", "is_expired", "enforce_share", "deletion_receipt",
           "redact_audio"]
