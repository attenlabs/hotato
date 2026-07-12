"""Regression test for the enqueue() concurrent-race defect (finding #8).

Two separate sqlite3 connections to the SAME fleet.db (the real production
shape: two processes/threads, each with its own connection) call
JobQueue.enqueue() with the identical idempotency key at the same instant.
Before the fix, enqueue() did a bare SELECT (get) then a bare INSERT with no
atomic BEGIN IMMEDIATE, so the two steps were not serialized and the loser of
the race got a raw, unhandled sqlite3.IntegrityError (or, under tighter
contention, sqlite3.OperationalError: database is locked) instead of the
documented dedup contract. The fix wraps the check-then-insert in the same
BEGIN IMMEDIATE pattern claim() already uses, plus a busy timeout and an
IntegrityError catch as defense-in-depth.
"""
import threading

from hotato.fleet.jobs import JobQueue
from hotato.fleet.registry import Registry


def test_enqueue_concurrent_same_idempotency_key_never_raises(tmp_path):
    home = str(tmp_path)
    # Each worker opens its OWN Registry (own sqlite3 connection object,
    # created and used inside that same thread) to the SAME fleet.db file --
    # exactly the "two threads/processes, each with its own sqlite3
    # connection" shape the finding reproduced live. A shared connection
    # object cannot be used across threads (sqlite3 forbids it outright), so
    # sharing one would not reproduce the real defect.
    results = []
    errors = []
    barrier = threading.Barrier(2)

    def worker():
        reg = Registry(home=home)
        q = JobQueue(reg.conn)
        barrier.wait()  # line both threads up to hit enqueue() at once
        try:
            r = q.enqueue(workspace_id="ws1", capability="score", operation="s",
                          source_pcm_hash="race-key")
            results.append(r)
        except Exception as exc:  # the exact defect under test
            errors.append(exc)
        finally:
            reg.close()

    # daemon=True so that if a worker ever dies before reaching the 2-party
    # barrier (e.g. a regression in Registry construction), the surviving
    # thread's block on barrier.wait() can never keep the interpreter alive at
    # shutdown -- the failure surfaces as a fast assertion, not a 95-minute CI
    # hang (which is exactly how this test's own regression first manifested).
    threads = [threading.Thread(target=worker, daemon=True),
               threading.Thread(target=worker, daemon=True)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=10)

    assert not errors, (
        f"enqueue() leaked an exception under concurrent same-key contention: {errors!r}")
    assert len(results) == 2

    # Both calls converge on the ONE logical job (the documented dedup
    # contract), never a raw traceback, and never two rows for one key.
    assert results[0]["job_id"] == results[1]["job_id"]
    assert sorted(r["deduped"] for r in results) == [False, True]

    jid = results[0]["job_id"]
    reg = Registry(home=home)
    row_count = reg.conn.execute(
        "SELECT COUNT(*) c FROM jobs WHERE job_id=?", (jid,)).fetchone()["c"]
    assert row_count == 1  # no double-insert / corruption regardless of race outcome
    reg.close()


def test_enqueue_repeated_races_stay_clean(tmp_path):
    """Run the race many times over fresh keys to make the repro reliable
    (a single race can get lucky/unlucky depending on OS thread scheduling)."""
    home = str(tmp_path)
    errors = []
    for i in range(20):
        barrier = threading.Barrier(2)
        results = []

        def worker(i=i):
            reg = Registry(home=home)
            q = JobQueue(reg.conn)
            barrier.wait()
            try:
                r = q.enqueue(workspace_id="ws1", capability="score", operation="s",
                              source_pcm_hash=f"race-key-{i}")
                results.append(r)
            except Exception as exc:
                errors.append(exc)
            finally:
                reg.close()

        threads = [threading.Thread(target=worker, daemon=True),  # daemon: see above
               threading.Thread(target=worker, daemon=True)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=10)

        if not errors:
            assert len(results) == 2
            assert results[0]["job_id"] == results[1]["job_id"]

    assert not errors, f"enqueue() leaked exceptions across repeated races: {errors!r}"
