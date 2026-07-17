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
import io
import json
import os
import stat
import tempfile
import time
import uuid
from typing import Optional

# Shared canonical-JSON producer (finding #2). ``manifest`` imports no fleet
# module (adapters/api already import it this way), so this is non-circular even
# though fleet/__init__ imports this store first. ``put_json`` keeps its trailing
# newline; ``canonical_json`` (ensure_ascii=True, sorted keys, compact
# separators) is byte-identical to the inline dump it replaces, so every stored
# blob's content-addressed digest is unchanged.
from .. import manifest as _manifest

SCHEMA_VERSION = "1"


class BlobIntegrityError(Exception):
    """A stored blob's bytes do not hash to their content address.

    Raised by a VERIFIED read (``get_bytes(digest, verify=True)``) when the
    bytes on disk no longer match the digest that names them -- i.e. the blob
    was corrupted (bit-rot) or tampered with out-of-band (a filesystem write
    that bypassed the ingest path, which always binds digest to content). Every
    trust/serving boundary reads verified and lets this fail CLOSED (refuse the
    read) rather than serving poisoned bytes as authentic evidence."""

# How old an orphaned "*.tmp" write must be before the startup sweep will
# remove it. Age-gated (not "any .tmp we see") so we never race a slower
# writer's still-in-flight tmp file that just happens to be open when a
# second ArtifactStore opens the same root.
_STALE_TMP_AGE_SECONDS = 3600

# A content address is exactly 64 LOWERCASE hexadecimal characters (a sha256
# hexdigest). A digest is this store's capability token and is joined straight
# into a filesystem path, so anything that is not canonical -- a path fragment
# ("../x", "/tmp/x"), the wrong length, uppercase, or a non-hex character --
# must be refused BEFORE it can traverse the store. There is never a blob at a
# non-canonical address, so refusing is loss-free.
_HEX64_CHARS = frozenset("0123456789abcdef")

# openat-style flags for the ONE containment primitive shared by every read and
# write below. A digest is joined into a fan-out path (``blobs/<ab>/<digest>``);
# a symlink planted at any segment (classically ``blobs/<ab> -> /outside``) must
# never be followed, or a write lands outside the store while a read of the same
# digest refuses it (internally inconsistent, unsafe under a writable/shared
# root). Resolving every I/O relative to a trusted ``blobs`` directory descriptor
# with O_NOFOLLOW on each segment makes a planted symlink fail closed (ELOOP)
# instead of redirecting the operation. O_NONBLOCK lets the source-file open of a
# FIFO/named pipe return instead of blocking, so ``fstat`` can reject it (it is a
# no-op for the regular-file reads that follow).
_O_NOFOLLOW = getattr(os, "O_NOFOLLOW", 0)
_O_DIRECTORY = getattr(os, "O_DIRECTORY", 0)
_O_NONBLOCK = getattr(os, "O_NONBLOCK", 0)

# O_BINARY (zero on POSIX) keeps a Windows ``os.open`` descriptor out of the
# CRT's default TEXT translation mode, which would rewrite b"\r\n" <-> b"\n"
# and stop reading at an 0x1A byte -- either one silently breaks content
# addressing. ``tempfile`` ORs the same flag into its own open flags for the
# same reason.
_O_BINARY = getattr(os, "O_BINARY", 0)

# Whether the openat-style primitive above is usable at all: a directory opened
# as a trusted descriptor plus dir_fd-relative opens/renames/unlinks. POSIX has
# both; Windows has NEITHER -- ``os.open`` of a DIRECTORY raises
# PermissionError (errno 13; the CRT open cannot take a directory) and every
# ``dir_fd`` argument raises NotImplementedError (``os.supports_dir_fd`` is
# empty there, per the os docs) -- so Windows takes the path-based branches
# below. Their containment rests on ``_resolved_blob_path``'s realpath prefix
# check: a planted symlink is RESOLVED before the comparison, so an escape
# still fails closed (refused) rather than redirecting the I/O.
_HAS_DIR_FD = {os.open, os.mkdir, os.stat, os.rename,
               os.unlink} <= os.supports_dir_fd


def _canonical_digest(digest):
    if not (isinstance(digest, str) and len(digest) == 64
            and _HEX64_CHARS.issuperset(digest)):
        raise ValueError(
            "a canonical content digest (64 lowercase hexadecimal characters) "
            "is required, got %r" % (digest,))
    return digest


class ArtifactStore:
    def __init__(self, root: str, *, registry=None):
        self.root = os.path.abspath(root)
        self.blobs = os.path.join(self.root, "blobs")
        self.lineage_path = os.path.join(self.root, "lineage.jsonl")
        # Optional durable reference source (a fleet Registry). When wired,
        # ``referencing_workspaces`` / ``workspace_has_reference`` answer from
        # the workspace-scoped reference-edge rows (the authority). A bare store
        # (e.g. the read-only serve view) has none and falls back to lineage as
        # a PROVENANCE view only -- lineage is never an ACL.
        self._registry = registry
        os.makedirs(self.blobs, exist_ok=True)
        try:
            self._cleanup_stale_tmp()
        except OSError:
            # Best-effort GC; a permissions hiccup on some blob subdir must
            # never block store startup.
            pass

    # --- addressing -----------------------------------------------------
    @staticmethod
    def _digest_bytes(data: bytes) -> str:
        return hashlib.sha256(data).hexdigest()

    def _blob_path(self, digest: str) -> str:
        # fan out by first two hex chars to avoid one giant directory. This is a
        # pure path builder (no validation): callers that resolve a blob for a
        # read/GC go through _resolved_blob_path, which validates the digest and
        # enforces realpath-containment. Internal writers pass a freshly computed
        # sha256 hexdigest, so it is canonical by construction.
        return os.path.join(self.blobs, digest[:2], digest)

    def _resolved_blob_path(self, digest: str) -> str:
        """Validated, contained blob path for every read and the GC unlink.

        The digest MUST be canonical (64 lowercase hex) and the resolved path
        MUST stay inside this store's blob root. A planted directory symlink in
        the fan-out (e.g. ``blobs/ab -> /etc``) would otherwise let a
        valid-looking digest read or unlink a file OUTSIDE the store; such a path
        is refused. Routing every read and the GC unlink through here keeps
        validation + containment uniform."""
        _canonical_digest(digest)
        path = self._blob_path(digest)
        root = os.path.realpath(self.blobs)
        resolved = os.path.realpath(path)
        if resolved != root and not resolved.startswith(root + os.sep):
            raise ValueError(
                "blob path for %s resolves outside the content store" % digest)
        return path

    # --- containment primitive (ONE openat-style guard for reads AND writes) --
    def _open_dir_fd(self) -> int:
        """Trusted directory descriptor for the blob root, opened no-follow.

        Every read and write below resolves the fan-out relative to THIS
        descriptor with no-follow opens, so a symlink anywhere in the fan-out
        cannot redirect an I/O outside the store. O_NOFOLLOW here also refuses a
        ``blobs`` root that has itself been swapped for a symlink."""
        return os.open(self.blobs, os.O_RDONLY | _O_DIRECTORY | _O_NOFOLLOW)

    def _open_fanout_dir(self, root_fd: int, two: str, *, create: bool) -> int:
        """No-follow open of the two-hex fan-out subdir relative to ``root_fd``.

        A directory symlink planted at ``blobs/<two>`` -- the write-escape defect
        -- is refused here by O_NOFOLLOW|O_DIRECTORY (ELOOP/ENOTDIR) rather than
        followed out of the store, so reads and writes fail closed instead of
        touching an out-of-store path. With ``create`` the subdir is created
        first (idempotently) relative to the same trusted descriptor."""
        if create:
            try:
                os.mkdir(two, 0o755, dir_fd=root_fd)
            except FileExistsError:
                pass
        return os.open(two, os.O_RDONLY | _O_DIRECTORY | _O_NOFOLLOW,
                       dir_fd=root_fd)

    @staticmethod
    def _leaf_is_regular(dir_fd: int, name: str) -> bool:
        """Whether ``name`` under ``dir_fd`` is a REGULAR file, checked without
        following a symlink (a planted leaf symlink reads as absent)."""
        try:
            st = os.stat(name, dir_fd=dir_fd, follow_symlinks=False)
        except OSError:
            return False
        return stat.S_ISREG(st.st_mode)

    def _blob_present(self, digest: str) -> bool:
        """Contained existence check: a REGULAR blob file at the fan-out leaf,
        resolved through no-follow directory descriptors so a symlinked fan-out
        (or leaf) reads as absent rather than escaping the store."""
        if not _HAS_DIR_FD:
            # Windows (no dir_fd): realpath containment instead of descriptors;
            # lstat (not stat) keeps a symlink leaf reading as absent rather
            # than followed, and a path that resolves outside the store reads
            # as absent too (fail closed), mirroring the refused open above.
            try:
                st = os.lstat(self._resolved_blob_path(digest))
            except (ValueError, OSError):
                return False
            return stat.S_ISREG(st.st_mode)
        root_fd = self._open_dir_fd()
        try:
            try:
                fanout_fd = self._open_fanout_dir(root_fd, digest[:2],
                                                  create=False)
            except OSError:
                return False
            try:
                return self._leaf_is_regular(fanout_fd, digest)
            finally:
                os.close(fanout_fd)
        finally:
            os.close(root_fd)

    def has(self, digest: str) -> bool:
        return self._blob_present(_canonical_digest(digest))

    # --- writes ---------------------------------------------------------
    def _write_via_tmp(self, dest: str, writer) -> None:
        """Publish ``dest`` atomically via a PRIVATE, unique tmp file.

        Lower-level path-based helper for direct-write tests; the public writers
        (``put_bytes`` / ``put_file``) go through ``_publish_stream``, which
        resolves the fan-out no-follow so a symlinked fan-out cannot redirect the
        write out of the store.

        ``writer(tmp_path)`` must populate ``tmp_path`` with the final bytes.
        Every call gets its own ``tempfile.mkstemp`` path (never a shared
        ``dest + ".tmp"``), so two concurrent writers of the SAME
        content-addressed digest never share an inode/path. That sharing was
        the defect: a faster writer's ``os.replace`` could rename the slower
        writer's still-open tmp file out from under it, so the slower writer's
        own later ``os.replace`` raised an unhandled ``FileNotFoundError`` --
        or, worse, both writers' in-flight fds kept writing into whichever
        inode ended up at ``dest``, racing bytes into it with no guarantee the
        result matched either writer's content (silent CAS corruption).
        ``os.replace(tmp, dest)`` is still atomic and ``dest`` is content
        addressed, so whichever writer wins the replace is correct by
        construction -- no locking needed, only tmp-path uniqueness.
        """
        os.makedirs(os.path.dirname(dest), exist_ok=True)
        fd, tmp = tempfile.mkstemp(prefix=os.path.basename(dest) + ".",
                                   suffix=".tmp", dir=os.path.dirname(dest))
        os.close(fd)
        try:
            writer(tmp)
            os.replace(tmp, dest)
        except Exception:
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise

    def put_bytes(self, data: bytes, *, kind: str = "blob",
                  workspace_id: Optional[str] = None,
                  parents: Optional[list] = None,
                  meta: Optional[dict] = None) -> str:
        digest = self._publish_stream(io.BytesIO(data))
        self._record_lineage(digest, kind=kind, workspace_id=workspace_id,
                             parents=parents or [], meta=meta or {})
        return digest

    def put_file(self, path: str, *, kind: str = "blob",
                 workspace_id: Optional[str] = None,
                 parents: Optional[list] = None,
                 meta: Optional[dict] = None) -> str:
        """Stream a file into the store in a single pass -- hashing and storing
        the SAME bytes -- without reopening the source path or making a second
        in-memory copy."""
        source_name = os.path.basename(path)
        fd = self._open_source_regular(path)
        with os.fdopen(fd, "rb", closefd=True) as src:
            digest = self._publish_stream(src)
        m = dict(meta or {}); m.setdefault("source_name", source_name)
        self._record_lineage(digest, kind=kind, workspace_id=workspace_id,
                             parents=parents or [], meta=m)
        return digest

    def _open_source_regular(self, path) -> int:
        """Open ``path`` as ONE validated regular-file descriptor.

        O_NOFOLLOW refuses a symlink at the final path component; O_NONBLOCK
        makes the open of a FIFO/named pipe return immediately (a blocking open
        would hang the process) so ``fstat`` can reject it, and is a no-op for
        the regular-file reads that follow. Validation is on the OPEN descriptor
        (``fstat``), not a pre-open path ``stat`` that a later open could race,
        so the bytes hashed and stored come from exactly this inode -- there is
        no second lookup of ``path`` to substitute (the put_file TOCTOU).
        O_BINARY (zero on POSIX) keeps the Windows CRT from text-translating
        the bytes hashed and stored."""
        fd = os.open(path, os.O_RDONLY | _O_NOFOLLOW | _O_NONBLOCK | _O_BINARY)
        try:
            st = os.fstat(fd)
            if not _O_NOFOLLOW and stat.S_ISLNK(
                    os.lstat(path).st_mode):
                # Windows (no O_NOFOLLOW): the open above FOLLOWED a symlink
                # and ``fstat`` reports its TARGET as a regular file, so a
                # link at ``path`` must be refused explicitly -- ``lstat``
                # never follows -- to keep the same fail-closed outcome the
                # ELOOP from O_NOFOLLOW gives POSIX at open time.
                st = None
        except OSError:
            os.close(fd)
            raise
        if st is None or not stat.S_ISREG(st.st_mode):
            os.close(fd)
            raise ValueError(
                "%r is not a regular file (found a named pipe/FIFO, device, "
                "directory, or symlink); hotato stores a plain file only." %
                (path,))
        return fd

    def _publish_stream(self, src) -> str:
        """Stream ``src`` once into a private temp blob WHILE hashing it, derive
        the content address from that same stream, fsync, then atomically publish
        it into the contained fan-out.

        Single pass: the digest that names the destination is computed from the
        exact bytes written to disk, so the returned digest always equals the
        stored content's digest -- there is no hash-one-stream / copy-another gap
        (closes the put_file publish-under-the-wrong-digest defect). Every
        directory and file open is no-follow and relative to a trusted ``blobs``
        descriptor (the same containment primitive the reads use), so a symlinked
        fan-out cannot redirect the write out of the store; a planted fan-out
        symlink makes the publish fail closed (ELOOP) after cleaning up its
        private temp, rather than writing outside. The staging temp lives in the
        blob root (the fan-out subdir is unknown until the stream is hashed) and
        is renamed across trusted descriptors into ``blobs/<ab>/`` -- an atomic
        rename within one filesystem. Duplicate content is idempotent: an
        already-present leaf keeps the existing blob and drops the temp."""
        if not _HAS_DIR_FD:
            return self._publish_stream_pathwise(src)
        root_fd = self._open_dir_fd()
        try:
            tmp_name = ".ingest-%d-%s.tmp" % (os.getpid(), uuid.uuid4().hex)
            tfd = os.open(tmp_name,
                          os.O_WRONLY | os.O_CREAT | os.O_EXCL | _O_NOFOLLOW,
                          0o644, dir_fd=root_fd)
            renamed = False
            try:
                h = hashlib.sha256()
                with os.fdopen(tfd, "wb", closefd=True) as out:
                    for chunk in iter(lambda: src.read(1 << 20), b""):
                        h.update(chunk)
                        out.write(chunk)
                    out.flush()
                    os.fsync(out.fileno())
                digest = h.hexdigest()
                fanout_fd = self._open_fanout_dir(root_fd, digest[:2],
                                                  create=True)
                try:
                    if not self._leaf_is_regular(fanout_fd, digest):
                        os.rename(tmp_name, digest,
                                  src_dir_fd=root_fd, dst_dir_fd=fanout_fd)
                        renamed = True
                        try:
                            os.fsync(fanout_fd)
                        except OSError:
                            pass
                finally:
                    os.close(fanout_fd)
                return digest
            finally:
                if not renamed:
                    try:
                        os.unlink(tmp_name, dir_fd=root_fd)
                    except OSError:
                        pass
        finally:
            os.close(root_fd)

    def _publish_stream_pathwise(self, src) -> str:
        """Path-based publish for platforms without the openat primitive
        (Windows -- see ``_HAS_DIR_FD``). The same single pass: hash WHILE
        writing a private temp in the blob root, fsync, then publish under the
        digest just computed, so the returned digest always equals the stored
        content's digest here too; containment comes from
        ``_resolved_blob_path``'s realpath prefix check on the destination.
        ``os.replace`` is the documented cross-platform atomic-publish
        primitive ("If dst exists and is a file, it will be replaced silently
        if the user has permission" -- os docs), so concurrent writers of the
        SAME digest keep the whichever-writer-wins-is-correct property of the
        openat branch's rename. Duplicate content stays idempotent: a present
        leaf keeps the existing blob and drops the temp."""
        tmp = os.path.join(self.blobs,
                           ".ingest-%d-%s.tmp" % (os.getpid(), uuid.uuid4().hex))
        # O_BINARY: without it the Windows CRT would expand b"\n" to b"\r\n"
        # UNDER the digest already computed from the untranslated chunks.
        tfd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | _O_BINARY,
                      0o644)
        published = False
        try:
            h = hashlib.sha256()
            with os.fdopen(tfd, "wb", closefd=True) as out:
                for chunk in iter(lambda: src.read(1 << 20), b""):
                    h.update(chunk)
                    out.write(chunk)
                out.flush()
                os.fsync(out.fileno())
            digest = h.hexdigest()
            dest = self._resolved_blob_path(digest)
            os.makedirs(os.path.dirname(dest), exist_ok=True)
            if not self._blob_present(digest):
                try:
                    os.replace(tmp, dest)
                    published = True
                except OSError:
                    # A concurrent writer of the SAME digest can publish in
                    # the present-check gap, and on Windows the losing
                    # ``os.replace`` may then be refused (MoveFileEx denies a
                    # replace racing another rename of the same destination;
                    # POSIX rename never fails this way). The store is content
                    # addressed, so whichever writer won is correct by
                    # construction: losing is success IFF the blob is now
                    # present; anything else stays an error.
                    if not self._blob_present(digest):
                        raise
            return digest
        finally:
            if not published:
                try:
                    os.unlink(tmp)
                except OSError:
                    pass

    def put_json(self, obj, **kw) -> str:
        data = (_manifest.canonical_json(obj) + "\n").encode("utf-8")
        return self.put_bytes(data, **kw)

    # --- reads ----------------------------------------------------------
    # Reads are by digest only: a digest is a capability. There is deliberately
    # no workspace_id parameter here -- cross-workspace isolation is a property
    # of the registry/reference layer that hands out digests, not of this CAS.
    def get_bytes(self, digest: str, *, verify: bool = False) -> bytes:
        # Read through the SAME no-follow containment primitive the writes use:
        # canonical digest, then a no-follow open of the regular leaf inside a
        # no-follow fan-out descriptor. A planted symlink at the fan-out or the
        # leaf is refused (ELOOP) rather than followed out of the content store,
        # and a missing blob raises FileNotFoundError exactly as a plain open did.
        #
        # ``verify=True`` re-hashes the bytes actually read and raises
        # ``BlobIntegrityError`` if they do not match ``digest``. A validated
        # digest FORMAT alone does not prove the bytes at that address are the
        # bytes that hash to it -- an out-of-band write or bit-rot can leave a
        # digest-named file whose content no longer matches. Every read that
        # crosses a TRUST/serving boundary (evidence HTTP endpoint, fleet
        # contract/redaction reads, conversation-inspector rendering) passes
        # verify=True so poisoned bytes fail CLOSED instead of being served as
        # authentic. Raw ``get_bytes()`` stays for internal HOT paths where
        # ``verify()`` is called separately (rubric/transcribe caches).
        _canonical_digest(digest)
        if not _HAS_DIR_FD:
            # Windows (no dir_fd): ``_resolved_blob_path`` supplies the
            # containment (realpath resolves a planted symlink BEFORE the
            # prefix check and refuses an escape); O_BINARY keeps the CRT from
            # translating the bytes read, so verify=True hashes the stored
            # bytes. A missing blob raises FileNotFoundError exactly as below.
            fd = os.open(self._resolved_blob_path(digest),
                         os.O_RDONLY | _O_NOFOLLOW | _O_BINARY)
            with os.fdopen(fd, "rb", closefd=True) as fh:
                data = fh.read()
        else:
            root_fd = self._open_dir_fd()
            try:
                fanout_fd = self._open_fanout_dir(root_fd, digest[:2],
                                                  create=False)
                try:
                    # open-ok: no-follow leaf open inside the validated fan-out fd
                    fd = os.open(digest, os.O_RDONLY | _O_NOFOLLOW,
                                 dir_fd=fanout_fd)
                finally:
                    os.close(fanout_fd)
                with os.fdopen(fd, "rb", closefd=True) as fh:
                    data = fh.read()
            finally:
                os.close(root_fd)
        if verify and self._digest_bytes(data) != digest:
            raise BlobIntegrityError(
                "stored blob for %s does not match its content address "
                "(corruption or out-of-band tampering); refusing to serve it "
                "as authentic evidence" % digest)
        return data

    def get_json(self, digest: str, *, verify: bool = False):
        return json.loads(self.get_bytes(digest, verify=verify).decode("utf-8"))

    def verify(self, digest: str) -> bool:
        """Re-hash the stored blob; content addressing must hold."""
        if not self.has(digest):
            return False
        return self._digest_bytes(self.get_bytes(digest)) == digest

    def path_for(self, digest: str) -> str:
        return self._resolved_blob_path(digest)

    # --- garbage collection (shared-blob safe) --------------------------
    def remove(self, digest: str) -> bool:
        """Unlink the shared content-addressed blob for ``digest``. Returns True
        if a blob was removed, False if none was present (or the unlink failed).

        LOW-LEVEL GC primitive: it validates the digest and resolves the unlink
        through the SAME no-follow containment primitive every read and write
        uses -- a no-follow ``blobs`` root descriptor, a no-follow fan-out
        descriptor, then ``os.unlink(digest, dir_fd=fanout_fd)``. Each segment is
        opened with O_NOFOLLOW|O_DIRECTORY and the final unlink is a directory-fd
        relative operation, so a symlink planted at ANY segment (root, fan-out, or
        leaf) -- including one SWAPPED IN after a check but before the unlink --
        fails closed (ELOOP/ENOTDIR/FileNotFoundError) instead of following the
        symlink and deleting a file OUTSIDE the store. This closes the
        check-then-use gap of the old ``realpath``-check-then-``os.remove(path)``
        path, whose intermediate directory symlinks ``os.remove`` still followed.

        It does NOT check references -- the CALLER must first confirm no live
        reference survives anywhere (see ``referencing_workspaces`` /
        ``Registry.referencing_workspaces``). Because the store is a SHARED,
        content-addressed pool, unlinking a still-referenced blob would destroy
        another workspace's evidence; that check is the caller's contract.
        ``lineage.jsonl`` is left intact -- provenance survives the bytes and is
        never treated as an authorization record."""
        _canonical_digest(digest)
        if not _HAS_DIR_FD:
            # Windows (no dir_fd): path-based unlink behind the same realpath
            # containment the fallback reads use -- a resolved escape is
            # refused (False, fail closed), and ``os.unlink`` of a symlink
            # leaf removes the link itself, never its target.
            try:
                path = self._resolved_blob_path(digest)
            except ValueError:
                return False
            try:
                os.unlink(path)
                return True
            except OSError:
                return False
        try:
            root_fd = self._open_dir_fd()
        except OSError:
            return False
        try:
            try:
                fanout_fd = self._open_fanout_dir(root_fd, digest[:2],
                                                  create=False)
            except OSError:
                # No fan-out subdir (nothing to remove) or a planted symlink at
                # the fan-out refused no-follow: fail closed either way.
                return False
            try:
                os.unlink(digest, dir_fd=fanout_fd)
                return True
            except OSError:
                # FileNotFoundError -> nothing to remove; any other OSError
                # leaves the blob present. Either way it was not removed.
                return False
            finally:
                os.close(fanout_fd)
        finally:
            os.close(root_fd)

    # --- reference queries (durable authority = the Registry) -----------
    def referencing_workspaces(self, digest: str) -> "set[str]":
        """Every workspace that holds a LIVE reference to ``digest``.

        A shared blob is safe to GC only when this is EMPTY. The durable
        authority is the Registry's workspace-scoped reference-edge rows
        (recordings/contracts/trials/conversations + the non-recording and JSON
        reference tables); a bare store with no registry falls back to the
        store's own lineage workspace_ids, a PROVENANCE view only. Validates the
        digest first (a non-canonical digest references nothing)."""
        _canonical_digest(digest)
        if self._registry is not None:
            return set(self._registry.referencing_workspaces(digest))
        return self._lineage_workspaces(digest)

    def workspace_has_reference(self, digest: str, workspace_id: str) -> bool:
        """Whether ``workspace_id`` holds a live reference to ``digest`` in the
        durable Registry. A bare store (no registry) reports its own lineage
        provenance instead -- and a lineage record is NOT an ACL: read
        authorization is gated on live registry roots
        (``hotato.serve.data.evidence_digest_authorized``), never on lineage."""
        _canonical_digest(digest)
        if self._registry is not None:
            return bool(self._registry.has_artifact_reference(workspace_id, digest))
        return workspace_id in self._lineage_workspaces(digest)

    def _lineage_workspaces(self, digest: str) -> "set[str]":
        """Workspaces named as the WRITER of ``digest`` in this store's lineage
        (provenance only). Records naming the digest merely as a parent are not
        writers and are excluded; a ``None`` writer contributes nothing."""
        out: "set[str]" = set()
        for rec in self.lineage(digest):
            if rec.get("digest") != digest:
                continue
            ws = rec.get("workspace_id")
            if ws is not None:
                out.add(ws)
        return out

    # --- maintenance ------------------------------------------------------
    def _cleanup_stale_tmp(self, max_age_seconds: int = _STALE_TMP_AGE_SECONDS) -> None:
        """Best-effort sweep of orphaned ``*.tmp`` files.

        A writer killed (SIGKILL / OOM / power loss) between creating its tmp
        file and the ``os.replace`` that publishes it leaves a permanent
        orphan with no lineage record, since there is no periodic GC pass
        elsewhere in the repo. Content addressing stays sound even with
        orphans present -- ``get_bytes``/``has``/``verify`` all resolve
        through ``_blob_path``, which never carries the ``.tmp`` suffix, so an
        orphan can never be read as a valid blob or corrupt a real entry. This
        is disk-space hygiene only, run once per store open, age-gated so it
        never races a slower writer's still-in-flight tmp file.
        """
        now = time.time()
        for entry in os.scandir(self.blobs):
            if not entry.is_dir(follow_symlinks=False):
                # A crash mid-``_publish_stream`` can strand a staging temp in
                # the blob root itself (the fan-out subdir is not known until the
                # stream has been hashed), so sweep root-level ``*.tmp`` orphans
                # too -- same age gate, so an in-flight writer is never raced.
                if entry.name.endswith(".tmp"):
                    try:
                        age = now - entry.stat(follow_symlinks=False).st_mtime
                    except OSError:
                        continue
                    if age > max_age_seconds:
                        try:
                            os.unlink(entry.path)
                        except OSError:
                            pass
                continue
            try:
                sub = list(os.scandir(entry.path))
            except OSError:
                continue
            for f in sub:
                if not f.name.endswith(".tmp"):
                    continue
                try:
                    age = now - f.stat().st_mtime
                except OSError:
                    continue
                if age > max_age_seconds:
                    try:
                        os.unlink(f.path)
                    except OSError:
                        pass

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
        # open-ok: the store's own lineage file at a path this store controls
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


__all__ = ["ArtifactStore", "BlobIntegrityError", "SCHEMA_VERSION"]
