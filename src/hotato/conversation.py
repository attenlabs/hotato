"""``hotato.conversation.v1``: build + verify the Conversation Artifact manifest.

A Conversation Artifact (Phase-1 design D) is a directory that binds ALL
evidence for one conversation -- audio, transcript, trace, timing, assertions --
by sha256, so the manifest is a tamper-evident index of its children. This
module builds that manifest and verifies it:

* :func:`build_manifest` hashes each supplied child file with the SAME
  content-addressing sha256 the fleet store uses
  (:meth:`hotato.fleet.store.ArtifactStore._digest_bytes`), so a digest bound
  here resolves against the store, and records ``{sha256, path, bytes}`` per
  child under the closed artifact set (audio/transcript/trace/timing/
  assertions). ``created_at`` is REQUIRED and caller-supplied -- never
  ``Date.now()`` on this deterministic path.

* :func:`verify` re-hashes every referenced child and REFUSES on any mismatch
  or missing file (the evidence-kernel refuse-not-downgrade posture: a tampered
  child is refused, never silently accepted). It returns a verdict dict whose
  ``ok`` is ``False`` and ``refused`` is ``True`` on any discrepancy -- it does
  not quietly return the manifest as if intact.

HONESTY INVARIANT (invariant 5): ``origin.kind`` ('real'|'simulated') is
REQUIRED so synthetic is never conflated with real, and a 'simulated' origin
MUST carry its simulator block. No ``overall_score`` anywhere. A structurally
malformed manifest raises ``ValueError`` (the usage-error / exit-2 path); a
digest MISMATCH is a verify-time refusal, not a validation error.
"""

from __future__ import annotations

import json
import os
from typing import Any, Dict, List, Optional

from .errors import (
    check_kind_version as _check_kind_version,
    load_json_file as _load_json_file,
    open_regular as _open_regular,
    reject_overall_score as _reject_overall_score,
)
from .fleet.store import ArtifactStore

__all__ = [
    "KIND",
    "VERSION",
    "ARTIFACT_NAMES",
    "ORIGIN_KINDS",
    "MANIFEST_NAME",
    "sha256_file",
    "validate_conversation_doc",
    "build_manifest",
    "write_conversation",
    "load_manifest",
    "verify",
]

KIND = "hotato.conversation"
VERSION = 1
MANIFEST_NAME = "conversation.json"

# The CLOSED set of evidence children a manifest may bind, each optional.
ARTIFACT_NAMES = ("audio", "transcript", "trace", "timing", "assertions")
# The REQUIRED synthetic/real axis (invariant 5).
ORIGIN_KINDS = ("real", "simulated")


def sha256_file(path: str) -> str:
    """sha256 hex of a file's bytes, computed with the SAME digest function the
    fleet content-addressed store uses
    (:meth:`hotato.fleet.store.ArtifactStore._digest_bytes`), so a digest bound
    in a conversation manifest is identical to the one the store would mint for
    the same bytes. Routed through :func:`hotato.errors.open_regular`, so a
    FIFO/named-pipe path raises immediately instead of blocking forever."""
    with _open_regular(path, "rb") as fh:
        data = fh.read()
    return ArtifactStore._digest_bytes(data)


# =========================================================================
# Validation (structural -> ValueError). A digest mismatch is NOT validated
# here; it is a verify-time refusal (see `verify`).
# =========================================================================

def _validate_origin(origin: Any) -> None:
    if not isinstance(origin, dict):
        raise ValueError("'origin' is required and must be a mapping")
    kind = origin.get("kind")
    if kind not in ORIGIN_KINDS:
        raise ValueError(
            f"origin.kind is REQUIRED and must be one of {ORIGIN_KINDS} "
            f"(synthetic is never conflated with real), got {kind!r}"
        )
    if kind == "simulated":
        sim = origin.get("simulator")
        if not isinstance(sim, dict):
            raise ValueError(
                "a 'simulated' origin must carry a simulator block "
                "{model_id, scenario_id, seed}"
            )
        for field in ("model_id", "scenario_id", "seed"):
            if field not in sim or sim[field] in (None, ""):
                raise ValueError(f"origin.simulator is missing {field!r}")


def _validate_artifacts(artifacts: Any) -> None:
    if not isinstance(artifacts, dict):
        raise ValueError("'artifacts' is required and must be a mapping")
    for name, ref in artifacts.items():
        if name not in ARTIFACT_NAMES:
            raise ValueError(
                f"unknown artifact {name!r}; the closed set is {ARTIFACT_NAMES}"
            )
        if not isinstance(ref, dict):
            raise ValueError(f"artifacts.{name} must be a mapping with a 'sha256'")
        digest = ref.get("sha256")
        if not isinstance(digest, str) or len(digest) != 64 or any(
            c not in "0123456789abcdef" for c in digest
        ):
            raise ValueError(
                f"artifacts.{name}.sha256 must be a 64-hex-char sha256 digest"
            )


def validate_conversation_doc(doc: Any) -> Dict[str, Any]:
    """Validate a ``conversation.v1`` manifest's STRUCTURE and return it
    unchanged. Raises ``ValueError`` on: a wrong ``kind``/``version``; a missing
    ``conversation_id``/``agent_id``/``created_at``; a missing or bad
    ``origin.kind``; a 'simulated' origin without its simulator block; a
    malformed artifact ref; or a forbidden ``overall_score``. This is pure
    structural validation -- a digest MISMATCH is not checked here; that is a
    verify-time refusal (:func:`verify`)."""
    if not isinstance(doc, dict):
        raise ValueError("conversation manifest must be a mapping")
    _reject_overall_score(doc, "'overall_score' is forbidden in a conversation manifest")
    _check_kind_version(doc, kind=KIND, version=VERSION, subject="conversation")
    for field in ("conversation_id", "agent_id", "created_at"):
        if not doc.get(field) or not isinstance(doc[field], str):
            raise ValueError(f"conversation manifest is missing a string {field!r}")
    _validate_origin(doc.get("origin"))
    _validate_artifacts(doc.get("artifacts", {}))
    return doc


# =========================================================================
# Build
# =========================================================================

def build_manifest(
    *,
    conversation_id: str,
    agent_id: str,
    origin: Dict[str, Any],
    created_at: str,
    artifact_files: Optional[Dict[str, str]] = None,
    base_dir: Optional[str] = None,
    scenario_digest: Optional[str] = None,
    release_digest: Optional[str] = None,
) -> Dict[str, Any]:
    """Build a ``hotato.conversation.v1`` manifest, binding each supplied child
    file by sha256.

    ``artifact_files`` maps a name from the closed :data:`ARTIFACT_NAMES` set to
    a path; each file is hashed with :func:`sha256_file` and recorded as
    ``{sha256, path, bytes}``. ``path`` is stored relative to ``base_dir`` when
    given (so the manifest travels with its directory), else the path as passed.
    ``origin`` is validated (``kind`` required; a 'simulated' origin must carry
    its simulator block) and ``created_at`` is REQUIRED and caller-supplied --
    never ``Date.now()`` on this deterministic path. Returns the validated
    manifest dict; raises ``ValueError`` on a malformed origin, an unknown
    artifact name, or a missing child file."""
    artifacts: Dict[str, Any] = {}
    for name, path in (artifact_files or {}).items():
        if name not in ARTIFACT_NAMES:
            raise ValueError(
                f"unknown artifact {name!r}; the closed set is {ARTIFACT_NAMES}"
            )
        if not os.path.isfile(path):
            raise ValueError(f"artifact {name!r} file not found: {path!r}")
        rel = os.path.relpath(path, base_dir) if base_dir is not None else path
        artifacts[name] = {
            "sha256": sha256_file(path),
            "path": rel.replace(os.sep, "/"),
            "bytes": os.path.getsize(path),
        }

    manifest: Dict[str, Any] = {
        "kind": KIND,
        "version": VERSION,
        "conversation_id": conversation_id,
        "agent_id": agent_id,
        "origin": origin,
        "created_at": created_at,
        "artifacts": artifacts,
    }
    if scenario_digest is not None:
        manifest["scenario_digest"] = scenario_digest
    if release_digest is not None:
        manifest["release_digest"] = release_digest

    return validate_conversation_doc(manifest)


def write_conversation(manifest: Dict[str, Any], dir_path: str) -> str:
    """Write ``manifest`` to ``<dir_path>/conversation.json`` (validating it
    first) and return the manifest path. The manifest's artifact ``path``
    values are interpreted relative to ``dir_path`` by :func:`verify`."""
    validate_conversation_doc(manifest)
    os.makedirs(dir_path, exist_ok=True)
    out = os.path.join(dir_path, MANIFEST_NAME)
    data = json.dumps(manifest, indent=2, sort_keys=True) + "\n"
    with open(out, "w", encoding="utf-8") as fh:  # open-ok: path this fn built
        fh.write(data)
    return out


def load_manifest(path: str) -> Dict[str, Any]:
    """Load and structurally validate a ``conversation.json`` manifest from a
    file. Raises ``ValueError`` on invalid JSON or a malformed manifest."""
    doc = _load_json_file(path)
    return validate_conversation_doc(doc)


# =========================================================================
# Verify (re-hash children, REFUSE on mismatch)
# =========================================================================

def verify(dir_or_manifest: Any, base_dir: Optional[str] = None) -> Dict[str, Any]:
    """Re-hash every child referenced by a conversation manifest and REFUSE on
    any digest mismatch or missing file.

    ``dir_or_manifest`` may be: a conversation directory (containing
    ``conversation.json``); a path to a ``conversation.json`` file; or an
    already-loaded manifest dict (``base_dir`` then locates its children,
    default the current directory). Each artifact's recorded ``path`` is
    resolved relative to the base directory and re-hashed with
    :func:`sha256_file`; the stored ``sha256`` is the binding.

    Returns a verdict dict::

        {"ok": bool, "refused": bool, "conversation_id": str,
         "verified": [names...], "mismatches": [...], "missing": [...],
         "reason": str}

    A single mismatch or missing child makes ``ok`` ``False`` and ``refused``
    ``True`` -- the evidence-kernel posture: refuse, never silently accept a
    tampered artifact. A structurally malformed manifest raises ``ValueError``
    (that is a usage error, distinct from a digest refusal)."""
    if isinstance(dir_or_manifest, dict):
        manifest = validate_conversation_doc(dir_or_manifest)
        root = base_dir if base_dir is not None else "."
    elif isinstance(dir_or_manifest, str):
        if os.path.isdir(dir_or_manifest):
            root = dir_or_manifest
            manifest = load_manifest(os.path.join(dir_or_manifest, MANIFEST_NAME))
        else:
            manifest = load_manifest(dir_or_manifest)
            root = base_dir if base_dir is not None else os.path.dirname(
                os.path.abspath(dir_or_manifest)
            )
    else:
        raise ValueError(
            "verify() takes a conversation dir, a conversation.json path, or a "
            "manifest dict"
        )

    verified: List[str] = []
    mismatches: List[Dict[str, Any]] = []
    missing: List[Dict[str, Any]] = []
    for name, ref in (manifest.get("artifacts") or {}).items():
        want = ref.get("sha256")
        rel = ref.get("path")
        if not rel:
            # A bound child with no path cannot be located to re-hash: refuse
            # rather than assume it is intact.
            missing.append({"artifact": name, "reason": "no 'path' recorded to re-hash"})
            continue
        child = os.path.join(root, rel)
        if not os.path.isfile(child):
            missing.append({"artifact": name, "path": rel, "reason": "child file not found"})
            continue
        got = sha256_file(child)
        if got != want:
            mismatches.append(
                {"artifact": name, "path": rel, "expected": want, "actual": got}
            )
        else:
            verified.append(name)

    refused = bool(mismatches or missing)
    if refused:
        parts = []
        if mismatches:
            parts.append(f"{len(mismatches)} digest mismatch(es)")
        if missing:
            parts.append(f"{len(missing)} missing child(ren)")
        reason = (
            "REFUSED: " + ", ".join(parts) + " -- a tampered or absent artifact "
            "is refused, never silently accepted"
        )
    else:
        reason = f"all {len(verified)} bound artifact(s) re-hashed to their recorded digest"

    return {
        "ok": not refused,
        "refused": refused,
        "conversation_id": manifest.get("conversation_id"),
        "verified": verified,
        "mismatches": mismatches,
        "missing": missing,
        "reason": reason,
    }
