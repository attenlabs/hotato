"""Leased job queue for Fleet (local mode; the same shape ports to Postgres
SKIP LOCKED in distributed mode).

Every job has a deterministic idempotency key, so duplicate webhooks, scheduler
retries, and worker crashes converge on ONE logical result instead of duplicate
candidates and experiments (plan §7.3). Claiming is lease-based with heartbeats;
a lease that expires without a heartbeat is reclaimable; a job that exhausts its
retries lands in a dead-letter state with a structured refusal reason.

No Redis/Celery/Kubernetes required for the first pilot -- a SQLite table with
leases and heartbeats is sufficient (plan §7.2).
"""
from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import time
from typing import Optional

_JOBS_SCHEMA = """
CREATE TABLE IF NOT EXISTS jobs (
  job_id TEXT PRIMARY KEY,          -- = idempotency key
  workspace_id TEXT NOT NULL,
  agent_id TEXT,
  capability TEXT NOT NULL,         -- score | capture | discover | experiment | report | retention
  input_json TEXT,
  input_hashes TEXT,
  state TEXT NOT NULL DEFAULT 'queued',  -- queued | leased | done | failed | dead
  lease_owner TEXT,
  lease_expires_at REAL,
  heartbeat_at REAL,
  attempts INTEGER NOT NULL DEFAULT 0,
  max_attempts INTEGER NOT NULL DEFAULT 3,
  output_hashes TEXT,
  refusal_reason TEXT,
  created_at REAL,
  updated_at REAL
);
CREATE INDEX IF NOT EXISTS idx_jobs_claimable ON jobs (capability, state, lease_expires_at);
"""


def idempotency_key(*, workspace_id, agent_id, operation, source_pcm_hash="",
                    policy_hash="", scorer_hash="", contract_set_hash="") -> str:
    """Deterministic key: the same logical work always maps to the same job
    (plan §7.3 basis). No timestamps/randomness enter the key.

    The field list is JSON-serialized rather than joined on a bare delimiter:
    a delimiter character living INSIDE a field can never masquerade as a field
    boundary. A bare '|' join collided -- workspace_id='a|b',agent_id='c' and
    workspace_id='a',agent_id='b|c' both produced 'a|b|c|...', so two different
    workspaces' jobs deduped to one global job_id. JSON quoting keeps them
    distinct (["a|b","c",...] != ["a","b|c",...])."""
    basis = json.dumps([workspace_id or "", agent_id or "", operation or "",
                        source_pcm_hash or "", policy_hash or "", scorer_hash or "",
                        contract_set_hash or ""], separators=(",", ":"))
    return hashlib.sha256(basis.encode("utf-8")).hexdigest()


def _busy_wrap(fn, *, conn=None, attempts=12):
    """Run a write, retrying transient SQLite lock contention with bounded
    exponential backoff before translating a persistent busy lock into the
    shared errors.HANDLED OSError contract (the CLI and every MCP fleet tool
    then emit the clean, structured, exit-code-2 error envelope instead of
    leaking a raw traceback).

    ``fn`` is a self-contained unit of work -- a single autocommit statement, or
    a full BEGIN IMMEDIATE..COMMIT closure that rolls itself back on failure --
    so re-running it after a rolled-back busy attempt is safe and idempotent
    (enqueue re-checks the row, claim re-selects). Between attempts any half-open
    transaction on ``conn`` is rolled back so the retry starts clean: a BEGIN
    IMMEDIATE that lost the race leaves no transaction, a COMMIT that itself
    raised busy leaves one. A write never partially applies -- SQLite raises
    before any bytes are committed. Only genuine lock/busy errors are retried;
    any other OperationalError propagates immediately."""
    delay = 0.02
    last = None
    for _ in range(attempts):
        try:
            return fn()
        except sqlite3.OperationalError as exc:
            msg = str(exc).lower()
            if "locked" not in msg and "busy" not in msg:
                raise
            last = exc
            if conn is not None and conn.in_transaction:
                try:
                    conn.rollback()
                except sqlite3.OperationalError:
                    pass
            time.sleep(delay)
            delay = min(delay * 2, 0.5)
    raise OSError(
        f"fleet database busy (concurrent writers): {last}. Retry the command."
    ) from last


class JobQueue:
    def __init__(self, conn: sqlite3.Connection):
        self.conn = conn
        # A concurrent enqueue()/claim() from another connection on the same
        # fleet.db can hold the write lock briefly (BEGIN IMMEDIATE below).
        # Uses the same env-overridable window as Registry
        # (HOTATO_FLEET_DB_TIMEOUT_SEC, default 30s) rather than a shorter
        # hardcoded value, so a JobQueue built over a Registry connection
        # (FleetAPI's normal wiring) is never clobbered back down to a
        # shorter busy window, and a JobQueue built over a bare connection
        # still gets the same generous default.
        timeout_sec = float(os.environ.get("HOTATO_FLEET_DB_TIMEOUT_SEC", "30"))
        # Manual BEGIN IMMEDIATE (enqueue/claim) needs autocommit so sqlite3
        # never injects its own implicit transaction. Registry already opens its
        # connection this way; setting it here too keeps a JobQueue built over a
        # bare connection out of the fragile default isolation_level mode.
        self.conn.isolation_level = None
        self.conn.execute(f"PRAGMA busy_timeout={int(timeout_sec * 1000)}")
        self.conn.executescript(_JOBS_SCHEMA)
        self.conn.commit()

    def _now(self):
        return time.time()

    def enqueue(self, *, workspace_id, capability, operation, agent_id=None,
                input_obj=None, input_hashes=None, max_attempts=3,
                source_pcm_hash="", policy_hash="", scorer_hash="",
                contract_set_hash="") -> dict:
        """Idempotent enqueue: a job with the same idempotency key is returned
        as-is (deduped), never inserted twice.

        The existence check and the insert run inside one BEGIN IMMEDIATE
        transaction (the same atomic pattern claim() uses) so two concurrent
        enqueue() calls for the same idempotency key are serialized: the
        second call blocks (up to the busy timeout, via _busy_wrap) until the
        first commits, then sees the row via get() and returns the deduped
        result instead of racing into the INSERT. sqlite3.IntegrityError
        around the INSERT is further defense-in-depth for any residual race
        (e.g. a writer outside this serialization) -- it re-fetches and
        returns the deduped result instead of letting the raw error escape
        to the caller."""
        jid = idempotency_key(workspace_id=workspace_id, agent_id=agent_id,
                              operation=operation, source_pcm_hash=source_pcm_hash,
                              policy_hash=policy_hash, scorer_hash=scorer_hash,
                              contract_set_hash=contract_set_hash)
        now = self._now()

        def _write():
            self.conn.execute("BEGIN IMMEDIATE")
            try:
                existing = self.get(jid)
                if existing is not None:
                    self.conn.execute("COMMIT")
                    return {"job_id": jid, "deduped": True, "state": existing["state"]}
                try:
                    self.conn.execute(
                        "INSERT INTO jobs (job_id, workspace_id, agent_id, capability, input_json, "
                        "input_hashes, state, attempts, max_attempts, created_at, updated_at) "
                        "VALUES (?,?,?,?,?,?, 'queued', 0, ?, ?, ?)",
                        (jid, workspace_id, agent_id, capability,
                         json.dumps(input_obj or {}, sort_keys=True),
                         json.dumps(input_hashes or [], sort_keys=True), max_attempts, now, now))
                except sqlite3.IntegrityError:
                    # The transaction is still open here -- sqlite does not
                    # abort the whole transaction on a constraint violation
                    # the way Postgres does, only the failed statement is
                    # undone. Re-check for the row that must now exist before
                    # deciding whether to commit (dedup) or roll back and
                    # re-raise (unexplained).
                    existing = self.get(jid)
                    if existing is not None:
                        self.conn.execute("COMMIT")
                        return {"job_id": jid, "deduped": True, "state": existing["state"]}
                    raise
                self.conn.execute("COMMIT")
                return {"job_id": jid, "deduped": False, "state": "queued"}
            except Exception:
                # Only an actually-open transaction can be rolled back. If the
                # failing statement was BEGIN IMMEDIATE itself (lost the write
                # lock), no transaction is open and a bare ROLLBACK would raise
                # "no transaction is active", masking the real busy error and
                # defeating the _busy_wrap retry.
                if self.conn.in_transaction:
                    try:
                        self.conn.execute("ROLLBACK")
                    except sqlite3.OperationalError:
                        # A ROLLBACK that itself hits a transient lock must not
                        # mask the original error; the real exception re-raises
                        # below and _busy_wrap cleans up before any retry.
                        pass
                raise

        return _busy_wrap(_write, conn=self.conn)

    def claim(self, *, capability, owner, lease_sec=120) -> Optional[dict]:
        """Claim the oldest claimable job for a capability: queued, or leased
        with an expired lease (a crashed worker). Atomic via an immediate
        transaction so two workers never claim the same job."""
        now = self._now()

        def _write():
            self.conn.execute("BEGIN IMMEDIATE")
            try:
                cur = self.conn.execute(
                    "SELECT job_id FROM jobs WHERE capability=? AND "
                    "(state='queued' OR (state='leased' AND lease_expires_at < ?)) "
                    "ORDER BY created_at LIMIT 1", (capability, now))
                row = cur.fetchone()
                if row is None:
                    self.conn.execute("COMMIT")
                    return None
                jid = row["job_id"]
                self.conn.execute(
                    "UPDATE jobs SET state='leased', lease_owner=?, lease_expires_at=?, "
                    "heartbeat_at=?, attempts=attempts+1, updated_at=? WHERE job_id=?",
                    (owner, now + lease_sec, now, now, jid))
                self.conn.execute("COMMIT")
                return jid
            except Exception:
                # Only an actually-open transaction can be rolled back. If the
                # failing statement was BEGIN IMMEDIATE itself (lost the write
                # lock), no transaction is open and a bare ROLLBACK would raise
                # "no transaction is active", masking the real busy error and
                # defeating the _busy_wrap retry.
                if self.conn.in_transaction:
                    try:
                        self.conn.execute("ROLLBACK")
                    except sqlite3.OperationalError:
                        # A ROLLBACK that itself hits a transient lock must not
                        # mask the original error; the real exception re-raises
                        # below and _busy_wrap cleans up before any retry.
                        pass
                raise

        jid = _busy_wrap(_write, conn=self.conn)
        if jid is None:
            return None
        return self.get(jid)

    def heartbeat(self, job_id, *, owner, lease_sec=120) -> bool:
        now = self._now()

        def _write():
            cur = self.conn.execute(
                "UPDATE jobs SET heartbeat_at=?, lease_expires_at=?, updated_at=? "
                "WHERE job_id=? AND lease_owner=? AND state='leased'",
                (now, now + lease_sec, now, job_id, owner))
            self.conn.commit()
            return cur.rowcount == 1

        return _busy_wrap(_write, conn=self.conn)

    def complete(self, job_id, *, owner, output_hashes=None) -> bool:
        now = self._now()

        def _write():
            cur = self.conn.execute(
                "UPDATE jobs SET state='done', output_hashes=?, refusal_reason=NULL, updated_at=? "
                "WHERE job_id=? AND lease_owner=?",
                (json.dumps(output_hashes or [], sort_keys=True), now, job_id, owner))
            self.conn.commit()
            return cur.rowcount == 1

        return _busy_wrap(_write, conn=self.conn)

    def fail(self, job_id, *, owner, reason) -> dict:
        """Fail a lease. Requeue if attempts remain, else dead-letter."""
        now = self._now()
        job = self.get(job_id)
        if job is None or job["lease_owner"] != owner:
            return {"ok": False, "reason": "not lease owner"}

        def _write():
            if job["attempts"] >= job["max_attempts"]:
                self.conn.execute(
                    "UPDATE jobs SET state='dead', refusal_reason=?, updated_at=? WHERE job_id=?",
                    (reason, now, job_id))
                state = "dead"
            else:
                self.conn.execute(
                    "UPDATE jobs SET state='queued', lease_owner=NULL, lease_expires_at=NULL, "
                    "refusal_reason=?, updated_at=? WHERE job_id=?", (reason, now, job_id))
                state = "queued"
            self.conn.commit()
            return state

        state = _busy_wrap(_write, conn=self.conn)
        return {"ok": True, "state": state}

    def record_start(self, **kw) -> dict:
        """In-process idempotent operation record: enqueue the job and report
        whether an identical operation already completed. Callers execute the work
        synchronously and call ``record_done``; a duplicate webhook / scheduler
        retry / crash-replay maps to the SAME job_id and short-circuits when it is
        already 'done' -- one logical result, never a duplicate (plan §7.3)."""
        r = self.enqueue(**kw)
        j = self.get(r["job_id"])
        return {"job_id": r["job_id"], "deduped": r["deduped"],
                "already_done": bool(j and j["state"] == "done")}

    def record_done(self, job_id, *, output_hashes=None) -> None:
        """Mark an in-process job done (no lease dance; the caller ran it inline)."""
        now = self._now()

        def _write():
            self.conn.execute(
                "UPDATE jobs SET state='done', output_hashes=?, refusal_reason=NULL, updated_at=? "
                "WHERE job_id=?",
                (json.dumps(output_hashes or [], sort_keys=True), now, job_id))
            self.conn.commit()

        _busy_wrap(_write, conn=self.conn)

    def get(self, job_id):
        cur = self.conn.execute("SELECT * FROM jobs WHERE job_id=?", (job_id,))
        row = cur.fetchone()
        return dict(row) if row else None

    def stats(self, workspace_id=None) -> dict:
        q = "SELECT state, COUNT(*) c FROM jobs"
        args = ()
        if workspace_id:
            q += " WHERE workspace_id=?"; args = (workspace_id,)
        q += " GROUP BY state"
        return {r["state"]: r["c"] for r in self.conn.execute(q, args).fetchall()}


__all__ = ["JobQueue", "idempotency_key"]
