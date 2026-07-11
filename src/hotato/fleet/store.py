"""Content-addressed artifact store for Fleet (local mode).

Blobs (recordings, envelopes, manifests, cards, reports, receipts) are stored
under a sha256 of their bytes, so identical content is stored once and every
reference is verifiable. Lineage (which artifact derived from which) is recorded
alongside, giving the trace spine the plan asks for without a database.

Scope of workspace isolation (honest by construction): this is a SHARED,
content-addressed blob store keyed by digest -- a digest IS a capability. The
reads (get_bytes/get_json/verify/path_for) take only a digest and are NOT
workspace-scoped; identical bytes from two workspaces collapse to one blob that
either can read given its digest. ``workspace_id`` recorded at write time is
lineage metadata, not a read-access boundary. Workspace isolation is enforced at
the REGISTRY/reference layer (the row that NAMES a digest is workspace-scoped),
so a workspace that never received a digest cannot obtain it through any
workspace-scoped query -- see tests/test_fleet_security.py.

Zero-dependency. Audio is stored SEPARATELY from any HTML/UI so a fleet report
never embeds raw customer audio by default (privacy reversal, plan §4/§14).
"""
from __future__ import annotations

import hashlib
import json
import os
import shutil
from typing import Optional

SCHEMA_VERSION = "1"


class ArtifactStore:
    def __init__(self, root: str):
        self.root = os.path.abspath(root)
        self.blobs = os.path.join(self.root, "blobs")
        self.lineage_path = os.path.join(self.root, "lineage.jsonl")
        os.makedirs(self.blobs, exist_ok=True)

    # --- addressing -----------------------------------------------------
    @staticmethod
    def _digest_bytes(data: bytes) -> str:
        return hashlib.sha256(data).hexdigest()

    def _blob_path(self, digest: str) -> str:
        # fan out by first two hex chars to avoid one giant directory
        return os.path.join(self.blobs, digest[:2], digest)

    def has(self, digest: str) -> bool:
        return os.path.isfile(self._blob_path(digest))

    # --- writes ---------------------------------------------------------
    def put_bytes(self, data: bytes, *, kind: str = "blob",
                  workspace_id: Optional[str] = None,
                  parents: Optional[list] = None,
                  meta: Optional[dict] = None) -> str:
        digest = self._digest_bytes(data)
        dest = self._blob_path(digest)
        if not os.path.isfile(dest):
            os.makedirs(os.path.dirname(dest), exist_ok=True)
            tmp = dest + ".tmp"
            with open(tmp, "wb") as fh:
                fh.write(data)
            os.replace(tmp, dest)
        self._record_lineage(digest, kind=kind, workspace_id=workspace_id,
                             parents=parents or [], meta=meta or {})
        return digest

    def put_file(self, path: str, *, kind: str = "blob",
                 workspace_id: Optional[str] = None,
                 parents: Optional[list] = None,
                 meta: Optional[dict] = None) -> str:
        """Stream a file into the store without a second in-memory copy."""
        h = hashlib.sha256()
        with open(path, "rb") as fh:
            for chunk in iter(lambda: fh.read(1 << 20), b""):
                h.update(chunk)
        digest = h.hexdigest()
        dest = self._blob_path(digest)
        if not os.path.isfile(dest):
            os.makedirs(os.path.dirname(dest), exist_ok=True)
            tmp = dest + ".tmp"
            shutil.copyfile(path, tmp)
            os.replace(tmp, dest)
        m = dict(meta or {}); m.setdefault("source_name", os.path.basename(path))
        self._record_lineage(digest, kind=kind, workspace_id=workspace_id,
                             parents=parents or [], meta=m)
        return digest

    def put_json(self, obj, **kw) -> str:
        data = (json.dumps(obj, sort_keys=True, separators=(",", ":")) + "\n").encode("utf-8")
        return self.put_bytes(data, **kw)

    # --- reads ----------------------------------------------------------
    # Reads are by digest only: a digest is a capability. There is deliberately
    # no workspace_id parameter here -- cross-workspace isolation is a property
    # of the registry/reference layer that hands out digests, not of this CAS.
    def get_bytes(self, digest: str) -> bytes:
        with open(self._blob_path(digest), "rb") as fh:
            return fh.read()

    def get_json(self, digest: str):
        return json.loads(self.get_bytes(digest).decode("utf-8"))

    def verify(self, digest: str) -> bool:
        """Re-hash the stored blob; content addressing must hold."""
        if not self.has(digest):
            return False
        return self._digest_bytes(self.get_bytes(digest)) == digest

    def path_for(self, digest: str) -> str:
        return self._blob_path(digest)

    # --- lineage --------------------------------------------------------
    def _record_lineage(self, digest, *, kind, workspace_id, parents, meta):
        rec = {"digest": digest, "kind": kind, "workspace_id": workspace_id,
               "parents": parents, "meta": meta}
        with open(self.lineage_path, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(rec, sort_keys=True) + "\n")

    def lineage(self, digest: str) -> list:
        """All lineage records naming this digest as subject or parent."""
        out = []
        if not os.path.isfile(self.lineage_path):
            return out
        with open(self.lineage_path, encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    rec = json.loads(line)
                except ValueError:
                    continue
                if rec.get("digest") == digest or digest in (rec.get("parents") or []):
                    out.append(rec)
        return out


__all__ = ["ArtifactStore", "SCHEMA_VERSION"]
