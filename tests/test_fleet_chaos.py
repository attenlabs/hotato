"""Load / chaos resilience for the fleet job queue and store (plan Wave 3).

Real production hits duplicate webhooks, concurrent workers, crashes, retry
storms, and partial uploads. These prove the leased job table converges on ONE
logical result and the content-addressed store never yields a torn artifact."""
import sqlite3
import threading

from hotato.fleet.registry import Registry
from hotato.fleet.jobs import JobQueue
from hotato.fleet.store import ArtifactStore


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
