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
    (plan §7.3 basis). No timestamps/randomness enter the key."""
    basis = "|".join([workspace_id or "", agent_id or "", operation or "",
                      source_pcm_hash or "", policy_hash or "", scorer_hash or "",
                      contract_set_hash or ""])
    return hashlib.sha256(basis.encode("utf-8")).hexdigest()


class JobQueue:
    def __init__(self, conn: sqlite3.Connection):
        self.conn = conn
        self.conn.executescript(_JOBS_SCHEMA)
        self.conn.commit()

    def _now(self):
        return time.time()

    def enqueue(self, *, workspace_id, capability, operation, agent_id=None,
                input_obj=None, input_hashes=None, max_attempts=3,
                source_pcm_hash="", policy_hash="", scorer_hash="",
                contract_set_hash="") -> dict:
        """Idempotent enqueue: a job with the same idempotency key is returned
        as-is (deduped), never inserted twice."""
        jid = idempotency_key(workspace_id=workspace_id, agent_id=agent_id,
                              operation=operation, source_pcm_hash=source_pcm_hash,
                              policy_hash=policy_hash, scorer_hash=scorer_hash,
                              contract_set_hash=contract_set_hash)
        existing = self.get(jid)
        if existing is not None:
            return {"job_id": jid, "deduped": True, "state": existing["state"]}
        now = self._now()
        self.conn.execute(
            "INSERT INTO jobs (job_id, workspace_id, agent_id, capability, input_json, "
            "input_hashes, state, attempts, max_attempts, created_at, updated_at) "
            "VALUES (?,?,?,?,?,?, 'queued', 0, ?, ?, ?)",
            (jid, workspace_id, agent_id, capability,
             json.dumps(input_obj or {}, sort_keys=True),
             json.dumps(input_hashes or [], sort_keys=True), max_attempts, now, now))
        self.conn.commit()
        return {"job_id": jid, "deduped": False, "state": "queued"}

    def claim(self, *, capability, owner, lease_sec=120) -> Optional[dict]:
        """Claim the oldest claimable job for a capability: queued, or leased
        with an expired lease (a crashed worker). Atomic via an immediate
        transaction so two workers never claim the same job."""
        now = self._now()
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
        except Exception:
            self.conn.execute("ROLLBACK")
            raise
        return self.get(jid)

    def heartbeat(self, job_id, *, owner, lease_sec=120) -> bool:
        now = self._now()
        cur = self.conn.execute(
            "UPDATE jobs SET heartbeat_at=?, lease_expires_at=?, updated_at=? "
            "WHERE job_id=? AND lease_owner=? AND state='leased'",
            (now, now + lease_sec, now, job_id, owner))
        self.conn.commit()
        return cur.rowcount == 1

    def complete(self, job_id, *, owner, output_hashes=None) -> bool:
        now = self._now()
        cur = self.conn.execute(
            "UPDATE jobs SET state='done', output_hashes=?, refusal_reason=NULL, updated_at=? "
            "WHERE job_id=? AND lease_owner=?",
            (json.dumps(output_hashes or [], sort_keys=True), now, job_id, owner))
        self.conn.commit()
        return cur.rowcount == 1

    def fail(self, job_id, *, owner, reason) -> dict:
        """Fail a lease. Requeue if attempts remain, else dead-letter."""
        now = self._now()
        job = self.get(job_id)
        if job is None or job["lease_owner"] != owner:
            return {"ok": False, "reason": "not lease owner"}
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
        return {"ok": True, "state": state}

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
