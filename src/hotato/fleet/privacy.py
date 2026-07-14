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
import math
import os
import struct
import tempfile
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
def _validate_spans(spans_sec) -> None:
    """Structurally validate the requested spans BEFORE any file is read or the
    destination is touched, so a bad request is a clean ``ValueError`` (never an
    ``OverflowError`` from ``int(inf * rate)``, a silent no-op, or a mutated
    destination). Each span is a real, finite, ordered ``[start, end)`` interval
    with ``0 <= start < end``; the list is non-empty and the spans are sorted and
    non-overlapping in REQUESTED seconds (adjacent spans that share a boundary
    are allowed; frame realization may still round them onto a shared edge)."""
    if not isinstance(spans_sec, (list, tuple)) or len(spans_sec) == 0:
        raise ValueError("redaction requires at least one [start, end) span.")
    prev_end = None
    for span in spans_sec:
        if not isinstance(span, (list, tuple)) or len(span) != 2:
            raise ValueError(
                "each redaction span must be a (start_sec, end_sec) pair.")
        s, e = span
        for value in (s, e):
            # bool is an int subclass; a truthy flag is never a valid second.
            if isinstance(value, bool) or not isinstance(value, (int, float)):
                raise ValueError(
                    "redaction span bounds must be real numbers of seconds.")
            if not math.isfinite(value):
                raise ValueError(
                    "redaction span bounds must be finite (no NaN/Infinity).")
        if s < 0:
            raise ValueError("a redaction span cannot start before 0 seconds.")
        if e <= s:
            raise ValueError(
                "a redaction span must have end > start (a positive duration).")
        if prev_end is not None and s < prev_end:
            raise ValueError(
                "redaction spans must be sorted and non-overlapping in seconds.")
        prev_end = e


def redact_audio(src_wav: str, spans_sec: List[tuple], out_wav: str, *,
                 hop_ms: float = 10.0) -> dict:
    """Silence the given [start,end) second spans, writing a NEW file. Returns a
    derivation record: a NEW PCM hash, the parent's hash, the requested and
    realized spans, and an explicitly DOWNGRADED evidence statement. The original
    is never touched; the derived copy must never be presented as the original
    evidence.

    Every failure -- an aliased destination (same path, a symlink, or a hardlink
    to the source), an invalid/empty/out-of-range/overlapping span, truncated
    source PCM, or a request that would silence nothing (identical PCM) -- is a
    clean ``ValueError`` raised BEFORE the destination is modified, so an existing
    ``out_wav`` is preserved on refusal. The derived copy is written to a sibling
    temp file and atomically ``os.replace``d into place, so a failed publish never
    leaves a half-written or partially-clobbered destination."""
    src_abs = os.path.abspath(src_wav)
    out_abs = os.path.abspath(out_wav)
    # A redaction is a DERIVED copy: it must never overwrite, alias, or share
    # storage identity with the source it derives from.
    if src_abs == out_abs:
        raise ValueError(
            "redaction must write to a new file distinct from the source; it "
            "produces a derived copy and never touches the original.")
    if os.path.islink(out_wav):
        raise ValueError(
            "redaction output must not be a symlink; write to a new regular "
            "file so the derived copy cannot alias the source or any other path.")
    if os.path.lexists(out_wav) and os.path.samefile(src_wav, out_wav):
        raise ValueError(
            "redaction output must not be a hardlink to the source; write to a "
            "new regular file so the original PCM is never mutated in place.")

    _validate_spans(spans_sec)

    with _wav_read(src_wav) as wf:
        nch, width, rate, n = (wf.getnchannels(), wf.getsampwidth(),
                               wf.getframerate(), wf.getnframes())
        raw = wf.readframes(n)
    if width != 2:
        raise ValueError("redaction supports 16-bit PCM WAV only")
    frame_bytes = width * nch
    if len(raw) != n * frame_bytes:
        raise ValueError(
            "source has truncated PCM data (its byte length is not a whole "
            "number of frames); refusing to derive a redacted copy from it.")
    duration_sec = n / rate if rate else 0.0

    samples = bytearray(raw)
    realized_frame_ranges = []
    realized_sample_ranges = []
    for (s, e) in spans_sec:
        # A span may not extend past the recording. Realize onto frames with a
        # floored start and CEILED end so every frame TOUCHED by the requested
        # interval is silenced (no leaked partial edge frame).
        if math.ceil(e * rate) > n:
            raise ValueError(
                "a redaction span extends past the end of the recording "
                f"({duration_sec:.6f} seconds); check the requested spans.")
        start_frame = max(0, math.floor(s * rate))
        end_frame = min(n, math.ceil(e * rate))
        realized_frame_ranges.append([start_frame, end_frame])
        realized_sample_ranges.append([start_frame * nch, end_frame * nch])
        for i in range(start_frame * frame_bytes, end_frame * frame_bytes):
            samples[i] = 0

    child_bytes = bytes(samples)
    parent_pcm = hashlib.sha256(raw).hexdigest()
    child_pcm = hashlib.sha256(child_bytes).hexdigest()
    if child_pcm == parent_pcm:
        # The requested spans were already silent: the derived PCM is identical
        # to the parent. Publishing it would be an already-silent no-op that
        # falsely claims a redaction happened.
        raise ValueError(
            "the requested spans are already-silent; redaction would be a no-op "
            "(identical PCM) and is refused so a derived copy is never published "
            "with no change from its parent.")

    dest_dir = os.path.dirname(out_abs) or "."
    fd, tmp_path = tempfile.mkstemp(
        prefix=".hotato-redact-", suffix=".wav", dir=dest_dir)
    os.close(fd)
    try:
        with wave.open(tmp_path, "wb") as wf:
            wf.setnchannels(nch)
            wf.setsampwidth(width)
            wf.setframerate(rate)
            wf.writeframes(child_bytes)
        os.replace(tmp_path, out_wav)
    except BaseException:
        try:
            os.remove(tmp_path)
        except OSError:
            pass
        raise

    spans_as_lists = [list(sp) for sp in spans_sec]
    return {
        "schema_version": "1",
        "derived": True,
        "kind": "redacted-audio",
        "parent_pcm_sha256": parent_pcm,
        "pcm_sha256": child_pcm,
        "requested_spans_sec": spans_as_lists,
        "redacted_spans_sec": spans_as_lists,
        "realized_frame_ranges": realized_frame_ranges,
        "realized_interleaved_sample_ranges": realized_sample_ranges,
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
