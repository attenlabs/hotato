"""CAS deletion + store-boundary regression coverage (P0 shared-blob data loss).

Ported/adapted from the hardening bundle's ``test_security_boundaries_round6.py``
(CAS/store/symlink/digest parts + the serve /evidence read-authz confirmation).
The redaction / query-secret parts of that bundle file belong to other strands
and are intentionally not duplicated here.
"""
from __future__ import annotations

import hashlib
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


# --- CAS write-path integrity ------------------------------------------------
# P0.4 (single-stream publish -- put_file must content-address the bytes it
# actually stores, never a first stream's digest over a second stream's bytes)
# and P0.5 (write containment -- put_bytes/put_file must not escape a symlinked
# fan-out even though the existing test above only checks path_for()).


def _content_hashing_to_prefix(prefix: str, tag: bytes = b"hotato-cas") -> bytes:
    """Smallest deterministic byte string (over ``tag``) whose sha256 hexdigest
    starts with ``prefix``, for planting content into a chosen fan-out bucket.
    ``tag`` varies the search so two distinct bodies can share one prefix."""
    i = 0
    while i < (1 << 22):
        body = b"%s-%d" % (tag, i)
        if hashlib.sha256(body).hexdigest().startswith(prefix):
            return body
        i += 1
    raise AssertionError("no content found for prefix %r" % (prefix,))


def _plant_fanout_symlink(store_root, outside, two: str = "ab") -> None:
    fanout = store_root / "blobs" / two
    try:
        fanout.symlink_to(outside, target_is_directory=True)
    except (OSError, NotImplementedError):
        pytest.skip("directory symlinks unavailable on this platform")


def test_put_file_digest_matches_stored_bytes_under_source_substitution(
        tmp_path, monkeypatch):
    """P0.4 path-substitution: the OLD put_file() hashed one descriptor, closed
    it, then REOPENED the path via shutil.copyfile -- so a source swapped in that
    gap was published under the FIRST stream's digest (returned digest !=
    stored-content digest, verify() false). The fixed single-stream put_file()
    has no such gap. We fire the swap at the exact seam the old code used
    (shutil.copyfile); the fixed code never calls it and reads the source once,
    so whatever digest is returned always addresses the bytes actually stored."""
    import hotato.fleet.store as store_mod

    store = ArtifactStore(str(tmp_path / "store"))
    src = tmp_path / "capture.bin"
    original = b"ORIGINAL-CAPTURE-" * 4096
    replacement = b"SWAPPED-EVIDENCE-" * 4096
    src.write_bytes(original)

    real_copyfile = getattr(getattr(store_mod, "shutil", None), "copyfile", None)
    if real_copyfile is not None:  # exercises the old hash/copy gap only
        def _swap_then_copy(s, d, *a, **k):
            src.write_bytes(replacement)
            return real_copyfile(s, d, *a, **k)
        monkeypatch.setattr(store_mod.shutil, "copyfile", _swap_then_copy)

    digest = store.put_file(str(src))
    # Whatever digest was returned, the stored bytes MUST hash to it.
    assert store.verify(digest)
    assert hashlib.sha256(store.get_bytes(digest)).hexdigest() == digest


def test_put_file_opens_the_source_exactly_once(tmp_path, monkeypatch):
    """P0.4 no-reopen: the fixed put_file() opens the source path exactly once
    (a single validated descriptor) and streams it -- it never reopens the path.
    The old code opened it twice (hash pass + shutil.copyfile), the TOCTOU this
    closes."""
    import hotato.fleet.store as store_mod

    store = ArtifactStore(str(tmp_path / "store"))
    src = tmp_path / "capture.bin"
    src.write_bytes(b"z" * (3 << 20))
    src_str = str(src)

    real_open = os.open
    source_opens = []

    def counting_open(path, *args, **kwargs):
        if path == src_str and "dir_fd" not in kwargs:
            source_opens.append(path)
        return real_open(path, *args, **kwargs)

    monkeypatch.setattr(store_mod.os, "open", counting_open)
    digest = store.put_file(src_str)
    monkeypatch.undo()

    assert source_opens == [src_str]  # opened once, never reopened
    assert store.verify(digest)


def test_put_file_refuses_symlink_source(tmp_path):
    """P0.4: a symlinked source is refused (O_NOFOLLOW + fstat) rather than
    silently dereferenced and stored under a name the caller cannot re-derive."""
    store = ArtifactStore(str(tmp_path / "store"))
    real = tmp_path / "real.bin"
    real.write_bytes(b"payload")
    link = tmp_path / "link.bin"
    try:
        os.symlink(str(real), str(link))
    except (OSError, NotImplementedError):
        pytest.skip("symlinks unavailable on this platform")
    with pytest.raises((ValueError, OSError)):
        store.put_file(str(link))


def test_put_file_concurrent_writers_agree_on_digest(tmp_path):
    """P0.4 concurrent-writer regression: several threads store the SAME source
    concurrently. Each gets its own private staging temp, the publish is
    idempotent, and every writer returns the one true content digest with no
    error and a blob that verifies."""
    store = ArtifactStore(str(tmp_path / "store"))
    data = os.urandom(1 << 20)
    src = tmp_path / "capture.bin"
    src.write_bytes(data)
    expected = hashlib.sha256(data).hexdigest()

    results = []
    errors = []

    def run():
        try:
            results.append(store.put_file(str(src)))
        except Exception as exc:  # pragma: no cover - failure path under test
            errors.append(exc)

    threads = [threading.Thread(target=run) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert errors == []
    assert set(results) == {expected}
    assert store.verify(expected)


def test_put_bytes_refuses_symlinked_fanout_escape(tmp_path):
    """P0.5, put_bytes() arm: content whose digest lands in a symlinked fan-out
    bucket must NOT be written through the symlink to an out-of-store target. The
    old writer used unchecked _blob_path()+makedirs and produced
    ``outside/<digest>``; the fixed writer resolves the fan-out no-follow and
    fails closed."""
    store_root = tmp_path / "store"
    store = ArtifactStore(str(store_root))
    outside = tmp_path / "outside"
    outside.mkdir()
    _plant_fanout_symlink(store_root, outside, "ab")
    content = _content_hashing_to_prefix("ab")
    before = set(os.listdir(outside))

    with pytest.raises((OSError, ValueError)):
        store.put_bytes(content)
    assert set(os.listdir(outside)) == before  # nothing written outside


def test_put_file_refuses_symlinked_fanout_escape(tmp_path):
    """P0.5, put_file() arm of the same fan-out escape."""
    store_root = tmp_path / "store"
    store = ArtifactStore(str(store_root))
    outside = tmp_path / "outside"
    outside.mkdir()
    _plant_fanout_symlink(store_root, outside, "ab")
    content = _content_hashing_to_prefix("ab")
    src = tmp_path / "capture.bin"
    src.write_bytes(content)
    before = set(os.listdir(outside))

    with pytest.raises((OSError, ValueError)):
        store.put_file(str(src))
    assert set(os.listdir(outside)) == before


def test_remove_fanout_swapped_to_symlink_between_check_and_unlink_never_escapes(
        tmp_path, monkeypatch):
    """P0 CAS-delete TOCTOU: the OLD ``remove()`` did a check-time
    ``os.path.realpath`` containment check and then, at USE time, called
    ``os.remove(path)`` over a SEPARATE, un-resolved path lookup. ``os.remove``
    follows intermediate DIRECTORY symlinks, so swapping the fan-out dir for a
    symlink to an out-of-store directory AFTER the realpath check but BEFORE the
    unlink made the delete escape the store and destroy an out-of-store victim.

    We fire the swap at the exact unlink/remove seam. The fixed ``remove()``
    resolves the fan-out through a no-follow directory descriptor and unlinks
    ``os.unlink(digest, dir_fd=fanout_fd)`` relative to that trusted fd, so the
    swap has no effect and the out-of-store victim survives. Reverting to the old
    ``realpath``-then-``os.remove(path)`` implementation makes this fail (the
    victim is deleted)."""
    import hotato.fleet.store as store_mod

    store_root = tmp_path / "store"
    store = ArtifactStore(str(store_root))
    # A legitimately stored blob whose digest lands in the ``ab`` fan-out bucket.
    content = _content_hashing_to_prefix("ab")
    digest = store.put_bytes(content)
    assert store.verify(digest)

    fanout_real = store_root / "blobs" / "ab"
    fanout_aside = store_root / "blobs" / "ab__real"
    outside = tmp_path / "outside"
    outside.mkdir()
    # The out-of-store victim shares the digest's BASENAME, so an escaped
    # ``os.remove(blobs/ab/<digest>)`` that follows a swapped fan-out symlink
    # resolves to ``outside/<digest>`` and deletes it.
    victim = outside / digest
    victim.write_bytes(b"OUT-OF-STORE-VICTIM")

    swapped: list = []

    def _swap_fanout_to_symlink():
        if swapped:
            return
        swapped.append(True)
        os.rename(str(fanout_real), str(fanout_aside))
        try:
            fanout_real.symlink_to(outside, target_is_directory=True)
        except (OSError, NotImplementedError):
            pytest.skip("directory symlinks unavailable on this platform")

    def _make_seam(real_fn):
        def wrapper(*args, **kwargs):
            _swap_fanout_to_symlink()
            return real_fn(*args, **kwargs)
        return wrapper

    # Cover both the old seam (os.remove over a plain path) and the fixed seam
    # (os.unlink relative to a dir fd); whichever the implementation reaches,
    # the fan-out is swapped to a symlink the instant before it runs.
    monkeypatch.setattr(store_mod.os, "remove", _make_seam(os.remove))
    monkeypatch.setattr(store_mod.os, "unlink", _make_seam(os.unlink))

    store.remove(digest)
    monkeypatch.undo()

    assert swapped == [True]  # the swap seam was actually exercised
    assert victim.exists()  # fixed remove() unlinks relative to a trusted fd
    assert victim.read_bytes() == b"OUT-OF-STORE-VICTIM"


def test_fanout_replaced_by_symlink_between_writes_never_escapes(tmp_path):
    """P0.5 fan-out replacement race: a fan-out dir created legitimately by one
    write is then swapped for a symlink to an out-of-store dir before the next
    write to the SAME bucket. Because every write re-resolves the fan-out
    no-follow, the second write fails closed instead of escaping."""
    import shutil as _shutil

    store_root = tmp_path / "store"
    store = ArtifactStore(str(store_root))
    first = _content_hashing_to_prefix("ab", tag=b"first")
    digest1 = store.put_bytes(first)
    assert store.verify(digest1)

    outside = tmp_path / "outside"
    outside.mkdir()
    fanout = store_root / "blobs" / "ab"
    _shutil.rmtree(fanout)
    try:
        fanout.symlink_to(outside, target_is_directory=True)
    except (OSError, NotImplementedError):
        pytest.skip("directory symlinks unavailable on this platform")

    second = _content_hashing_to_prefix("ab", tag=b"second")
    assert second != first
    before = set(os.listdir(outside))
    with pytest.raises((OSError, ValueError)):
        store.put_bytes(second)
    assert set(os.listdir(outside)) == before
