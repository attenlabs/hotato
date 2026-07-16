"""Load / chaos resilience for the fleet job queue and store (plan Wave 3).

Real production hits duplicate webhooks, concurrent workers, crashes, retry
storms, and partial uploads. These prove the leased job table converges on ONE
logical result and the content-addressed store never yields a torn artifact."""
import hashlib
import os
import sqlite3
import threading
import time

import pytest

from hotato.fleet.jobs import JobQueue
from hotato.fleet.registry import Registry
from hotato.fleet.store import ArtifactStore, BlobIntegrityError


def _queue(home):
    reg = Registry(home=home)
    return reg, JobQueue(reg.conn)


def test_concurrent_workers_never_double_claim(tmp_path):
    """Many workers, each on its OWN connection (real multi-process shape), claim
    from a shared queue; no job is ever claimed twice."""
    home = str(tmp_path)
    reg, q = _queue(home)
    for i in range(60):
        q.enqueue(workspace_id="ws1", capability="score", operation=f"op{i}",
                  source_pcm_hash=f"h{i}")
    reg.close()

    claimed = []
    lock = threading.Lock()

    def worker(name):
        conn = sqlite3.connect(str(tmp_path / "fleet.db"), timeout=10)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        jq = JobQueue(conn)
        while True:
            try:
                job = jq.claim(capability="score", owner=name)
            except sqlite3.OperationalError:
                continue
            if job is None:
                break
            with lock:
                claimed.append(job["job_id"])
            jq.complete(job["job_id"], owner=name)
        conn.close()

    threads = [threading.Thread(target=worker, args=(f"w{i}",)) for i in range(6)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert len(claimed) == 60                 # every job ran
    assert len(set(claimed)) == 60            # NONE ran twice


def test_duplicate_webhooks_converge_on_one_job(tmp_path):
    reg, q = _queue(str(tmp_path))
    ids = set()
    for _ in range(25):   # the same webhook delivered 25 times
        r = q.enqueue(workspace_id="ws1", capability="score", operation="score_call",
                      source_pcm_hash="deadbeef")
        ids.add(r["job_id"])
    assert len(ids) == 1
    assert q.stats("ws1").get("queued") == 1
    reg.close()


def test_crashed_worker_lease_is_reclaimed(tmp_path):
    reg, q = _queue(str(tmp_path))
    q.enqueue(workspace_id="ws1", capability="score", operation="x", source_pcm_hash="h")
    job = q.claim(capability="score", owner="crasher", lease_sec=-1)  # already expired
    assert job is not None
    # a second worker can reclaim the expired lease
    reclaimed = q.claim(capability="score", owner="healthy")
    assert reclaimed is not None and reclaimed["job_id"] == job["job_id"]
    assert reclaimed["attempts"] == 2
    reg.close()


def test_retry_storm_lands_in_dead_letter(tmp_path):
    reg, q = _queue(str(tmp_path))
    q.enqueue(workspace_id="ws1", capability="cap", operation="c",
              source_pcm_hash="z", max_attempts=3)
    last = None
    for _ in range(5):
        jb = q.claim(capability="cap", owner="w")
        if jb is None:
            break
        last = q.fail(jb["job_id"], owner="w", reason="boom")
    assert last["state"] == "dead"
    assert q.stats("ws1").get("dead") == 1
    reg.close()


def test_store_partial_write_never_yields_torn_blob(tmp_path):
    """A blob is written via a temp file + atomic replace, so a concurrent reader
    never sees a half-written artifact; the digest always verifies."""
    st = ArtifactStore(str(tmp_path))
    data = b"x" * (2 * 1024 * 1024)
    digest = st.put_bytes(data, kind="recording", workspace_id="ws1")
    assert st.verify(digest)                      # content addressing holds
    # writing identical content again is idempotent and still verifies
    assert st.put_bytes(data) == digest and st.verify(digest)


def test_verified_read_refuses_out_of_band_poisoned_blob(tmp_path):
    """CAS trust boundary: a blob poisoned out-of-band (its on-disk bytes no
    longer hash to the digest that names it) must fail CLOSED on a verified read.

    Reproduces the audit exploit: plant mismatched bytes at the on-disk CAS path
    of an honest blob, then read it back. ``get_bytes(digest, verify=True)``
    re-hashes and raises ``BlobIntegrityError`` instead of returning the poison;
    ``verify()`` reports False. Before the fix the ONLY integrity check was the
    opt-in ``verify()`` and every serving read used raw ``get_bytes``, so the
    poison was served verbatim as authentic evidence."""
    st = ArtifactStore(str(tmp_path))
    honest = b'{"evidence": "authentic", "verdict": "improved"}'
    digest = st.put_bytes(honest, kind="conversation", workspace_id="ws1")
    assert st.verify(digest)                       # honest blob is sound

    # Poison the on-disk leaf out-of-band (bit-rot / filesystem tamper): overwrite
    # the content-addressed file with DIFFERENT bytes of the same length.
    poison = b'{"evidence": "FORGED!!", "verdict": "improved"}!'
    assert len(poison) == len(honest)
    leaf = st._blob_path(digest)
    with open(leaf, "wb") as fh:
        fh.write(poison)

    # The address still resolves and the file is present, but the bytes lie.
    assert st.has(digest) is True
    assert st.verify(digest) is False              # designated integrity check
    # Raw read (internal hot path) still returns whatever is on disk...
    assert st.get_bytes(digest) == poison
    # ...but the VERIFIED read every trust/serving boundary uses fails closed.
    with pytest.raises(BlobIntegrityError):
        st.get_bytes(digest, verify=True)
    with pytest.raises(BlobIntegrityError):
        st.get_json(digest, verify=True)


def test_concurrent_duplicate_writes_never_share_tmp_path(tmp_path):
    """Regression for finding #9. Drives TWO racing writers of the SAME
    digest through the exact stall/finish pattern that reproduced the
    original defect: writer A opens its tmp file, writes half, stalls;
    writer B opens, writes, and replaces first while A is stalled; A then
    resumes and replaces too.

    Before the fix, both writers built `tmp = dest + ".tmp"` -- a path
    derived only from the digest, with no per-writer uniqueness -- so they
    shared one inode. B's os.replace silently stole A's still-open tmp file
    out from under it, and A's own later os.replace raised an unhandled
    FileNotFoundError once B had already renamed the shared path away (this
    exact scenario: hotato.fleet.api.ingest_recording() calls put_file() on
    every call and documents duplicate webhook / re-pull delivery as an
    expected case). After the fix each writer gets a unique tempfile.mkstemp
    path, so the two can never collide regardless of scheduling: neither
    raises, and the published blob's bytes still hash to its own digest."""
    st = ArtifactStore(str(tmp_path))
    data = (b"A" * (1 << 16)) + (b"B" * (1 << 16))
    digest = hashlib.sha256(data).hexdigest()
    dest = st._blob_path(digest)
    half = len(data) // 2

    errors = []

    def make_writer(stall):
        def _writer(tmp_path_):
            with open(tmp_path_, "wb") as fh:
                fh.write(data[:half])
                if stall:
                    time.sleep(0.05)
                fh.write(data[half:])
        return _writer

    def run(stall):
        try:
            st._write_via_tmp(dest, make_writer(stall))
        except Exception as exc:  # pragma: no cover - failure path under test
            errors.append(exc)

    t_slow = threading.Thread(target=run, args=(True,))   # stalls mid-write
    t_fast = threading.Thread(target=run, args=(False,))  # finishes and replaces first
    t_slow.start()
    time.sleep(0.01)  # let the slow writer open its tmp file first, like the repro
    t_fast.start()
    t_slow.join()
    t_fast.join()

    assert errors == []  # neither writer raised (was: unhandled FileNotFoundError)
    with open(dest, "rb") as fh:
        assert hashlib.sha256(fh.read()).hexdigest() == digest  # never corrupted


def test_write_failure_cleans_up_its_own_tmp_file(tmp_path):
    """Part of the #9/#12 fix: a failure mid-write (disk full, permission
    error, ...) must not leak a `.tmp` fragment. The tmp file created for
    that attempt is removed before the exception propagates, and the
    original exception is still raised (fail closed, not swallowed)."""
    st = ArtifactStore(str(tmp_path))
    dest = st._blob_path("cafebabe")

    def _boom(tmp_path_):
        assert os.path.exists(tmp_path_)
        raise OSError("disk full (simulated)")

    with pytest.raises(OSError):
        st._write_via_tmp(dest, _boom)

    blob_dir = os.path.dirname(dest)
    leftovers = [f for f in os.listdir(blob_dir) if f.endswith(".tmp")]
    assert leftovers == []


def test_stale_tmp_orphan_is_swept_on_store_open(tmp_path):
    """Regression for finding #12. A writer killed (SIGKILL / OOM / power
    loss) after its tmp file is created but before os.replace publishes it
    leaves a permanent orphan with no lineage record (the crash happens
    before _record_lineage runs), and nothing else in the repo ever cleans
    it up. Opening a new ArtifactStore on the same root must sweep old
    orphans so they cannot accumulate forever on a long-running fleet node,
    while leaving a recent tmp file (a genuinely in-flight writer) alone."""
    ArtifactStore(str(tmp_path))  # first open: creates blobs/
    blob_dir = os.path.join(str(tmp_path), "blobs", "de")
    os.makedirs(blob_dir, exist_ok=True)
    stale = os.path.join(blob_dir, "deadbeef.stale.tmp")
    fresh = os.path.join(blob_dir, "deadbeef.fresh.tmp")
    for p in (stale, fresh):
        with open(p, "wb") as fh:
            fh.write(b"orphan")
    two_hours_ago = time.time() - 7200
    os.utime(stale, (two_hours_ago, two_hours_ago))
    # `fresh` keeps its just-now mtime, well under the sweep's age threshold.

    ArtifactStore(str(tmp_path))  # re-open: triggers the startup sweep

    assert not os.path.exists(stale)  # old orphan reclaimed
    assert os.path.exists(fresh)      # recent tmp file spared (might be in-flight)
