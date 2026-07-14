"""CAS deletion + store-boundary regression coverage (P0 shared-blob data loss).

Ported/adapted from the hardening bundle's ``test_security_boundaries_round6.py``
(CAS/store/symlink/digest parts + the serve /evidence read-authz confirmation).
The redaction / query-secret parts of that bundle file belong to other strands
and are intentionally not duplicated here.
"""
from __future__ import annotations

import os
import threading
import urllib.error
import urllib.request

import pytest

from hotato.fleet.api import FleetAPI
from hotato.fleet.registry import Registry
from hotato.fleet.store import ArtifactStore
from hotato.serve.app import ServeContext, build_server
from hotato.serve.security import AuditLog, SessionStore
from tests import _trial_audio as ta


@pytest.mark.parametrize(
    "bad",
    [
        "../secret.json",
        "..\\secret.json",
        "/tmp/secret",
        "a" * 63,
        "a" * 65,
        "A" * 64,
        "g" * 64,
        "",
        None,
    ],
)
def test_artifact_store_rejects_every_noncanonical_digest(tmp_path, bad):
    store = ArtifactStore(str(tmp_path / "store"))
    for operation in (
        store.has,
        store.get_bytes,
        store.get_json,
        store.verify,
        store.path_for,
        store.remove,
        store.referencing_workspaces,
    ):
        with pytest.raises(ValueError, match="64 lowercase hexadecimal"):
            operation(bad)


def test_artifact_store_refuses_symlinked_fanout_escape(tmp_path):
    store = ArtifactStore(str(tmp_path / "store"))
    outside = tmp_path / "outside"
    outside.mkdir()
    fanout = tmp_path / "store" / "blobs" / "ab"
    try:
        fanout.symlink_to(outside, target_is_directory=True)
    except (OSError, NotImplementedError):
        pytest.skip("directory symlinks unavailable")
    with pytest.raises(ValueError, match="outside the content store"):
        store.path_for("ab" + "0" * 62)


def test_shared_cas_delete_revokes_one_workspace_without_breaking_the_other(tmp_path):
    wav = str(tmp_path / "same.wav")
    ta.yielding_call(wav)
    with FleetAPI(str(tmp_path / "home")) as api:
        for workspace in ("A", "B"):
            api.init_workspace(workspace)
            api.agent_add(workspace, "agent", stack="vapi")
        a = api.ingest_recording("A", "agent", wav)
        b = api.ingest_recording("B", "agent", wav)
        assert a["artifact_digest"] == b["artifact_digest"]
        digest = a["artifact_digest"]

        deleted_a = api.delete_recording(
            "A", a["recording_id"], reason="retention", actor="ops"
        )
        assert deleted_a["deleted"] is True
        assert deleted_a["blob_removed"] is False
        assert api.store.has(digest)
        assert api.store.get_bytes(digest) == open(wav, "rb").read()
        assert not api.store.workspace_has_reference(digest, "A")
        assert api.store.workspace_has_reference(digest, "B")
        a_row = api.registry._one(
            "SELECT artifact_digest, pii_class FROM recordings "
            "WHERE workspace_id=? AND recording_id=?",
            ("A", a["recording_id"]),
        )
        assert a_row["artifact_digest"] is None
        assert a_row["pii_class"] == "deleted"

        deleted_b = api.delete_recording(
            "B", b["recording_id"], reason="retention", actor="ops"
        )
        assert deleted_b["deleted"] is True
        assert deleted_b["blob_removed"] is True
        assert not api.store.has(digest)


def test_shared_cas_delete_preserved_by_a_non_recording_reference(tmp_path):
    """A surviving reference in ANOTHER owning table (here a conversation) keeps
    the shared blob alive even after the last recording pointer is revoked."""
    wav = str(tmp_path / "same.wav")
    ta.yielding_call(wav)
    with FleetAPI(str(tmp_path / "home")) as api:
        api.init_workspace("A")
        api.agent_add("A", "agent", stack="vapi")
        a = api.ingest_recording("A", "agent", wav)
        digest = a["artifact_digest"]
        # A different owning table (conversations) also roots the same digest.
        api.registry.add_conversation("A", "conv-1", artifact_digest=digest)

        deleted = api.delete_recording(
            "A", a["recording_id"], reason="retention", actor="ops"
        )
        assert deleted["deleted"] is True
        assert deleted["blob_removed"] is False
        assert api.store.has(digest)
        assert "A" in api.registry.referencing_workspaces(digest)


def test_legal_hold_blocks_pointer_revocation_and_preserves_blob(tmp_path):
    wav = str(tmp_path / "same.wav")
    ta.yielding_call(wav)
    with FleetAPI(str(tmp_path / "home")) as api:
        api.init_workspace("A")
        api.agent_add("A", "agent", stack="vapi")
        a = api.ingest_recording("A", "agent", wav)
        digest = a["artifact_digest"]
        api.set_retention(
            "A", a["recording_id"], consent_basis="contract",
            allowed_purposes=["eval"], legal_hold=True,
        )
        blocked = api.delete_recording(
            "A", a["recording_id"], reason="dsar", actor="ops"
        )
        assert blocked["deleted"] is False
        assert blocked["blocked_by_legal_hold"] is True
        assert api.store.has(digest)
        row = api.registry._one(
            "SELECT artifact_digest FROM recordings "
            "WHERE workspace_id=? AND recording_id=?",
            ("A", a["recording_id"]),
        )
        assert row["artifact_digest"] == digest


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None


def _request(base, path, token):
    req = urllib.request.Request(base + path)
    req.add_header("Authorization", "Bearer " + token)
    try:
        with urllib.request.urlopen(req, timeout=5) as response:
            return response.status, response.read(), dict(response.headers)
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read(), dict(exc.headers)


def test_serve_evidence_requires_live_registry_root_not_cas_lineage(tmp_path):
    """serve /evidence authorizes on LIVE registry roots, never CAS lineage.

    Confirms the 1.5.2 read-authz gate stays intact and exercises the net-new
    ``store.workspace_has_reference`` / ``registry.clear_recording_artifact``
    surface: write-time lineage grants no HTTP access, a live registry root does
    (even with no lineage), a foreign root 404s, and revoking the root revokes
    HTTP reachability while the shared blob + lineage remain.
    """
    import json

    home = str(tmp_path / "fleet")
    registry = Registry(home=home)
    registry.ensure_workspace("A")
    registry.ensure_workspace("B")
    registry.close()

    store = ArtifactStore(os.path.join(home, "artifacts"))
    lineage_only = b"LINEAGE_ONLY_SECRET"
    lineage_only_digest = store.put_bytes(
        lineage_only, kind="evidence", workspace_id="A"
    )
    live_unlineaged = b"LIVE_UNLINEAGED_ROOT"
    live_unlineaged_digest = store.put_bytes(
        live_unlineaged, kind="evidence", workspace_id=None
    )
    wrong_workspace = b"WRONG_WORKSPACE_ROOT"
    wrong_workspace_digest = store.put_bytes(
        wrong_workspace, kind="evidence", workspace_id="A"
    )
    deleted_root = b"DELETED_ROOT_WITH_LIVE_LINEAGE"
    deleted_root_digest = store.put_bytes(
        deleted_root, kind="evidence", workspace_id="A"
    )
    # Model an upgraded/legacy CAS whose live blob + registry row predate
    # workspace lineage.  Authorization must not depend on reconstructing a
    # write-time side log.
    with open(store.lineage_path, encoding="utf-8") as fh:
        lineage_records = [json.loads(line) for line in fh if line.strip()]
    with open(store.lineage_path, "w", encoding="utf-8") as fh:
        for record in lineage_records:
            if record.get("digest") != live_unlineaged_digest:
                fh.write(json.dumps(record, sort_keys=True) + "\n")
    assert store.lineage(live_unlineaged_digest) == []

    registry = Registry(home=home)
    registry.add_recording("A", "live-root", artifact_digest=live_unlineaged_digest)
    registry.add_recording("B", "wrong-workspace-root",
                           artifact_digest=wrong_workspace_digest)
    registry.add_recording("A", "deleted-root", artifact_digest=deleted_root_digest)
    registry.clear_recording_artifact("A", "deleted-root")
    registry.close()

    state = os.path.join(home, "serve", "A")
    os.makedirs(state, exist_ok=True)
    audit_path = os.path.join(state, "audit.jsonl")
    token = "tok_encoded_query_secret_0123456789"
    context = ServeContext(
        home=home,
        workspace="A",
        store_root=os.path.join(home, "artifacts"),
        token=token,
        state_dir=state,
        audit=AuditLog(audit_path),
        sessions=SessionStore(),
        bind_host="127.0.0.1",
    )
    server = build_server(context, "127.0.0.1", 0)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    base = f"http://127.0.0.1:{server.server_address[1]}"
    try:
        # Write-time lineage is provenance only and grants no HTTP access.
        status, body, _headers = _request(
            base, "/evidence/" + lineage_only_digest, token
        )
        assert status == 404
        assert lineage_only not in body

        # A live registry root grants reachability even when the CAS write had no
        # workspace lineage (legacy/upgraded stores remain readable).
        assert not store.workspace_has_reference(live_unlineaged_digest, "A")
        status, body, _headers = _request(
            base, "/evidence/" + live_unlineaged_digest, token
        )
        assert status == 200 and body == live_unlineaged

        # A live root in B cannot be read through A, even if stale lineage happens
        # to mention A for the same digest.
        status, body, _headers = _request(
            base, "/evidence/" + wrong_workspace_digest, token
        )
        assert status == 404
        assert wrong_workspace not in body

        # Revoking the registry root immediately removes HTTP reachability while
        # the shared CAS bytes and old lineage remain present.
        assert store.has(deleted_root_digest)
        assert store.workspace_has_reference(deleted_root_digest, "A")
        status, body, _headers = _request(
            base, "/evidence/" + deleted_root_digest, token
        )
        assert status == 404
        assert deleted_root not in body
    finally:
        server.shutdown()
        thread.join(timeout=5)
        server.server_close()
