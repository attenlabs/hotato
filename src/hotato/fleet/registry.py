"""Fleet registry: workspace-scoped metadata for every voice agent, deployment,
call, candidate, label, contract, trial, and decision.

Local mode is one SQLite file. There is NO product-level cap on registered
agents; physical capacity is bounded only by call volume, storage, and workers.
Every row carries ``workspace_id``: no globally addressed audio path or provider
call id is sufficient to reach another workspace's artifact (plan §7.1).

Stdlib ``sqlite3`` only. The same domain shape is implemented against Postgres in
distributed mode behind an optional extra; callers use this class either way.
"""
from __future__ import annotations

import json
import os
import sqlite3
import time
from typing import Optional

DEFAULT_HOME = os.path.expanduser("~/.hotato/fleet")
SCHEMA_VERSION = 1

_SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT);

CREATE TABLE IF NOT EXISTS workspaces (
  workspace_id TEXT PRIMARY KEY,
  name TEXT,
  created_at REAL
);

CREATE TABLE IF NOT EXISTS connections (
  workspace_id TEXT NOT NULL,
  connection_id TEXT NOT NULL,
  stack TEXT NOT NULL,
  secret_ref TEXT,
  created_at REAL,
  PRIMARY KEY (workspace_id, connection_id)
);

CREATE TABLE IF NOT EXISTS agents (
  workspace_id TEXT NOT NULL,
  agent_id TEXT NOT NULL,
  name TEXT,
  stack TEXT NOT NULL,
  connection_id TEXT,
  external_ref TEXT,          -- e.g. vapi assistant id
  created_at REAL,
  PRIMARY KEY (workspace_id, agent_id)
);

CREATE TABLE IF NOT EXISTS deployments (
  workspace_id TEXT NOT NULL,
  deployment_id TEXT NOT NULL,
  agent_id TEXT NOT NULL,
  environment TEXT,
  config_hash TEXT,
  revision INTEGER,
  created_at REAL,
  PRIMARY KEY (workspace_id, deployment_id)
);

CREATE TABLE IF NOT EXISTS calls (
  workspace_id TEXT NOT NULL,
  call_id TEXT NOT NULL,
  deployment_id TEXT,
  agent_id TEXT,
  provider_locator TEXT,
  started_at REAL,
  ingested_at REAL,
  PRIMARY KEY (workspace_id, call_id)
);

CREATE TABLE IF NOT EXISTS recordings (
  workspace_id TEXT NOT NULL,
  recording_id TEXT NOT NULL,
  call_id TEXT,
  raw_sha256 TEXT,
  pcm_sha256 TEXT,
  artifact_digest TEXT,       -- content-addressed store digest
  channel_layout TEXT,
  captured_at REAL,
  retention_policy_json TEXT, -- optional per-recording retention policy (plan §14)
  pii_class TEXT,             -- optional PII/PHI classification (plan §14)
  PRIMARY KEY (workspace_id, recording_id)
);

CREATE TABLE IF NOT EXISTS candidates (
  workspace_id TEXT NOT NULL,
  candidate_id TEXT NOT NULL,
  recording_id TEXT,
  agent_id TEXT,
  onset_sec REAL,
  measured_json TEXT,
  severity REAL,
  cluster TEXT,
  status TEXT DEFAULT 'new',  -- new | reviewing | labeled | dismissed
  created_at REAL,
  PRIMARY KEY (workspace_id, candidate_id)
);

CREATE TABLE IF NOT EXISTS labels (
  workspace_id TEXT NOT NULL,
  label_id TEXT NOT NULL,
  candidate_id TEXT,
  reviewer TEXT,
  decision TEXT,              -- yield | hold | not_a_useful_event | bad_input
  rationale TEXT,
  revision INTEGER DEFAULT 1,
  created_at REAL,
  PRIMARY KEY (workspace_id, label_id)
);

CREATE TABLE IF NOT EXISTS contracts (
  workspace_id TEXT NOT NULL,
  contract_id TEXT NOT NULL,
  label_id TEXT,
  agent_id TEXT,
  policy_hash TEXT,
  canonical_digest TEXT,
  artifact_digest TEXT,
  high_stakes INTEGER DEFAULT 0,
  created_at REAL,
  PRIMARY KEY (workspace_id, contract_id)
);

CREATE TABLE IF NOT EXISTS trials (
  workspace_id TEXT NOT NULL,
  trial_id TEXT NOT NULL,
  agent_id TEXT,
  manifest_hash TEXT,
  manifest_digest TEXT,       -- store digest of the full manifest
  verdict TEXT,
  evidence_tier INTEGER,
  created_at REAL,
  PRIMARY KEY (workspace_id, trial_id)
);

CREATE TABLE IF NOT EXISTS variants (
  workspace_id TEXT NOT NULL,
  variant_id TEXT NOT NULL,
  trial_id TEXT,
  config_delta_json TEXT,
  expected_effect TEXT,
  observed_effect TEXT,
  eligible INTEGER,
  created_at REAL,
  agent_id TEXT,              -- additive: variant's owning agent
  expected_json TEXT,        -- additive: structured expected effect
  observed_json TEXT,        -- additive: structured observed effect
  rank INTEGER,              -- additive: variant rank within a trial
  PRIMARY KEY (workspace_id, variant_id)
);

CREATE TABLE IF NOT EXISTS decisions (
  workspace_id TEXT NOT NULL,
  decision_id TEXT NOT NULL,
  trial_id TEXT,
  recommendation TEXT,
  hard_gate_json TEXT,
  approved INTEGER DEFAULT 0,
  approver TEXT,
  created_at REAL,
  PRIMARY KEY (workspace_id, decision_id)
);

CREATE TABLE IF NOT EXISTS observations (
  workspace_id TEXT NOT NULL,
  observation_id TEXT NOT NULL,
  deployment_id TEXT,
  call_id TEXT,
  evidence_json TEXT,
  created_at REAL,
  PRIMARY KEY (workspace_id, observation_id)
);

CREATE TABLE IF NOT EXISTS contract_sets (
  workspace_id TEXT NOT NULL,
  set_id TEXT NOT NULL,
  member_contract_hashes TEXT,   -- immutable ORDERED JSON array of contract hashes
  created_at REAL,
  PRIMARY KEY (workspace_id, set_id)
);

CREATE TABLE IF NOT EXISTS deployment_receipts (
  workspace_id TEXT NOT NULL,
  receipt_id TEXT NOT NULL,
  agent_id TEXT,
  kind TEXT,                      -- clone | canary | rollback
  variant_id TEXT,
  config_hash TEXT,
  prior_revision INTEGER,
  detail_json TEXT,
  receipt_digest TEXT,
  created_at REAL,
  PRIMARY KEY (workspace_id, receipt_id)
);

CREATE TABLE IF NOT EXISTS attestations (
  workspace_id TEXT NOT NULL,
  attestation_id TEXT NOT NULL,
  subject_kind TEXT,
  subject_id TEXT,
  signer TEXT,
  subject_digest TEXT,
  statement TEXT,
  algorithm TEXT,
  created_at REAL,
  PRIMARY KEY (workspace_id, attestation_id)
);

CREATE TABLE IF NOT EXISTS watermarks (
  workspace_id TEXT NOT NULL,
  agent_id TEXT NOT NULL,
  source TEXT NOT NULL,
  last_watermark REAL,            -- durable high-water mark for batch discovery (plan §9.1)
  updated_at REAL,
  PRIMARY KEY (workspace_id, agent_id, source)
);

CREATE INDEX IF NOT EXISTS idx_calls_ws_deploy ON calls (workspace_id, deployment_id);
CREATE INDEX IF NOT EXISTS idx_cand_ws_agent ON candidates (workspace_id, agent_id, status);
CREATE INDEX IF NOT EXISTS idx_rec_ws_call ON recordings (workspace_id, call_id);
CREATE INDEX IF NOT EXISTS idx_deprcpt_ws_agent ON deployment_receipts (workspace_id, agent_id, kind);
"""


class Registry:
    def __init__(self, home: str = DEFAULT_HOME):
        self.home = os.path.abspath(home)
        os.makedirs(self.home, exist_ok=True)
        self.db_path = os.path.join(self.home, "fleet.db")
        self.conn = sqlite3.connect(self.db_path)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.execute("PRAGMA foreign_keys=ON")
        self.conn.executescript(_SCHEMA)
        # Additive column migrations. A fresh DB already has these from the
        # CREATE TABLE above (IF NOT EXISTS never rewrites an existing table),
        # so this backfills only older DB files. Each is guarded by table_info.
        self._ensure_column("recordings", "retention_policy_json", "TEXT")
        self._ensure_column("recordings", "pii_class", "TEXT")
        self._ensure_column("variants", "agent_id", "TEXT")
        self._ensure_column("variants", "expected_json", "TEXT")
        self._ensure_column("variants", "observed_json", "TEXT")
        self._ensure_column("variants", "rank", "INTEGER")
        cur = self.conn.execute("SELECT value FROM meta WHERE key='schema_version'")
        row = cur.fetchone()
        if row is None:
            self.conn.execute("INSERT INTO meta (key, value) VALUES ('schema_version', ?)",
                              (str(SCHEMA_VERSION),))
        self.conn.commit()

    def close(self):
        self.conn.close()

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()

    # --- generic helpers ------------------------------------------------
    def _insert(self, table: str, row: dict, *, replace: bool = False):
        cols = ",".join(row.keys())
        marks = ",".join("?" for _ in row)
        verb = "INSERT OR REPLACE" if replace else "INSERT"
        self.conn.execute(f"{verb} INTO {table} ({cols}) VALUES ({marks})",
                          tuple(row.values()))
        self.conn.commit()

    def _now(self):
        # time.time() is allowed here (registry is runtime state, not a
        # deterministic kernel artifact); callers may pass explicit timestamps.
        return time.time()

    def _ensure_column(self, table: str, column: str, decl: str):
        """Additive migration: ALTER a table to add ``column`` if an existing DB
        file lacks it. Fresh DBs already carry the column from CREATE TABLE, so
        this is a no-op there. Guarded by a PRAGMA table_info check."""
        have = {r["name"] for r in self.conn.execute(f"PRAGMA table_info({table})")}
        if column not in have:
            self.conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {decl}")

    # --- entities -------------------------------------------------------
    def ensure_workspace(self, workspace_id: str, name: Optional[str] = None):
        if not self.get_workspace(workspace_id):
            self._insert("workspaces", {"workspace_id": workspace_id,
                                        "name": name or workspace_id,
                                        "created_at": self._now()})
        return workspace_id

    def get_workspace(self, workspace_id: str):
        return self._one("SELECT * FROM workspaces WHERE workspace_id=?", (workspace_id,))

    def add_connection(self, workspace_id, connection_id, stack, secret_ref=None):
        self.ensure_workspace(workspace_id)
        self._insert("connections", {"workspace_id": workspace_id,
                                     "connection_id": connection_id, "stack": stack,
                                     "secret_ref": secret_ref, "created_at": self._now()},
                     replace=True)

    def add_agent(self, workspace_id, agent_id, *, name=None, stack, connection_id=None,
                  external_ref=None):
        self.ensure_workspace(workspace_id)
        self._insert("agents", {"workspace_id": workspace_id, "agent_id": agent_id,
                                "name": name or agent_id, "stack": stack,
                                "connection_id": connection_id, "external_ref": external_ref,
                                "created_at": self._now()}, replace=True)

    def list_agents(self, workspace_id):
        return self._all("SELECT * FROM agents WHERE workspace_id=? ORDER BY created_at",
                         (workspace_id,))

    def add_deployment(self, workspace_id, deployment_id, agent_id, *, environment=None,
                       config_hash=None, revision=1):
        self._insert("deployments", {"workspace_id": workspace_id,
                                     "deployment_id": deployment_id, "agent_id": agent_id,
                                     "environment": environment, "config_hash": config_hash,
                                     "revision": revision, "created_at": self._now()},
                     replace=True)

    def add_call(self, workspace_id, call_id, *, deployment_id=None, agent_id=None,
                 provider_locator=None, started_at=None):
        self._insert("calls", {"workspace_id": workspace_id, "call_id": call_id,
                               "deployment_id": deployment_id, "agent_id": agent_id,
                               "provider_locator": provider_locator,
                               "started_at": started_at, "ingested_at": self._now()},
                     replace=True)

    def has_call(self, workspace_id, call_id) -> bool:
        return self._one("SELECT 1 FROM calls WHERE workspace_id=? AND call_id=?",
                         (workspace_id, call_id)) is not None

    def add_recording(self, workspace_id, recording_id, *, call_id=None, raw_sha256=None,
                      pcm_sha256=None, artifact_digest=None, channel_layout=None):
        self._insert("recordings", {"workspace_id": workspace_id, "recording_id": recording_id,
                                    "call_id": call_id, "raw_sha256": raw_sha256,
                                    "pcm_sha256": pcm_sha256, "artifact_digest": artifact_digest,
                                    "channel_layout": channel_layout, "captured_at": self._now()},
                     replace=True)

    def add_candidate(self, workspace_id, candidate_id, *, recording_id=None, agent_id=None,
                      onset_sec=None, measured_json=None, severity=None, cluster=None,
                      status="new"):
        self._insert("candidates", {"workspace_id": workspace_id, "candidate_id": candidate_id,
                                    "recording_id": recording_id, "agent_id": agent_id,
                                    "onset_sec": onset_sec, "measured_json": measured_json,
                                    "severity": severity, "cluster": cluster, "status": status,
                                    "created_at": self._now()}, replace=True)

    def list_candidates(self, workspace_id, *, agent_id=None, status=None, limit=50):
        q = "SELECT * FROM candidates WHERE workspace_id=?"
        args = [workspace_id]
        if agent_id:
            q += " AND agent_id=?"; args.append(agent_id)
        if status:
            q += " AND status=?"; args.append(status)
        q += " ORDER BY COALESCE(severity,0) DESC, created_at DESC LIMIT ?"
        args.append(limit)
        return self._all(q, tuple(args))

    def has_candidate(self, workspace_id, candidate_id) -> bool:
        """Whether a candidate exists in this workspace (labels must reference a
        real candidate; an orphan label is rejected upstream)."""
        return self._one(
            "SELECT 1 FROM candidates WHERE workspace_id=? AND candidate_id=?",
            (workspace_id, candidate_id)) is not None

    def set_candidate_status(self, workspace_id, candidate_id, status):
        self.conn.execute("UPDATE candidates SET status=? WHERE workspace_id=? AND candidate_id=?",
                          (status, workspace_id, candidate_id))
        self.conn.commit()

    def add_label(self, workspace_id, label_id, *, candidate_id=None, reviewer=None,
                  decision=None, rationale=None, revision=1):
        self._insert("labels", {"workspace_id": workspace_id, "label_id": label_id,
                                "candidate_id": candidate_id, "reviewer": reviewer,
                                "decision": decision, "rationale": rationale,
                                "revision": revision, "created_at": self._now()}, replace=True)

    def add_contract(self, workspace_id, contract_id, *, label_id=None, agent_id=None,
                     policy_hash=None, canonical_digest=None, artifact_digest=None,
                     high_stakes=0):
        self._insert("contracts", {"workspace_id": workspace_id, "contract_id": contract_id,
                                   "label_id": label_id, "agent_id": agent_id,
                                   "policy_hash": policy_hash, "canonical_digest": canonical_digest,
                                   "artifact_digest": artifact_digest, "high_stakes": high_stakes,
                                   "created_at": self._now()}, replace=True)

    def add_trial(self, workspace_id, trial_id, *, agent_id=None, manifest_hash=None,
                  manifest_digest=None, verdict=None, evidence_tier=None):
        self._insert("trials", {"workspace_id": workspace_id, "trial_id": trial_id,
                               "agent_id": agent_id, "manifest_hash": manifest_hash,
                               "manifest_digest": manifest_digest, "verdict": verdict,
                               "evidence_tier": evidence_tier, "created_at": self._now()},
                     replace=True)

    def add_decision(self, workspace_id, decision_id, *, trial_id=None, recommendation=None,
                     hard_gate_json=None, approved=0, approver=None):
        self._insert("decisions", {"workspace_id": workspace_id, "decision_id": decision_id,
                                   "trial_id": trial_id, "recommendation": recommendation,
                                   "hard_gate_json": hard_gate_json, "approved": approved,
                                   "approver": approver, "created_at": self._now()}, replace=True)

    def set_recording_privacy(self, workspace_id, recording_id, *,
                              retention_policy_json=None, pii_class=None):
        """Attach a retention policy and/or a PII/PHI classification to a
        recording (plan §14). Only the fields passed are updated."""
        sets, args = [], []
        if retention_policy_json is not None:
            sets.append("retention_policy_json=?"); args.append(retention_policy_json)
        if pii_class is not None:
            sets.append("pii_class=?"); args.append(pii_class)
        if not sets:
            return
        args.extend([workspace_id, recording_id])
        self.conn.execute(
            f"UPDATE recordings SET {', '.join(sets)} "
            "WHERE workspace_id=? AND recording_id=?", tuple(args))
        self.conn.commit()

    def add_contract_set(self, workspace_id, set_id, member_contract_hashes, *,
                         created_at=None):
        """Record an immutable ORDERED contract-set membership (plan §7.1). The
        membership is written once; re-inserting the same set_id raises (a
        contract set never mutates)."""
        self.ensure_workspace(workspace_id)
        hashes = member_contract_hashes
        if not isinstance(hashes, str):
            hashes = json.dumps(list(hashes), separators=(",", ":"))
        self._insert("contract_sets", {"workspace_id": workspace_id, "set_id": set_id,
                                       "member_contract_hashes": hashes,
                                       "created_at": created_at if created_at is not None
                                       else self._now()})

    def get_contract_set(self, workspace_id, set_id):
        return self._one("SELECT * FROM contract_sets WHERE workspace_id=? AND set_id=?",
                         (workspace_id, set_id))

    def add_deployment_receipt(self, workspace_id, receipt_id, *, agent_id=None, kind=None,
                               variant_id=None, config_hash=None, prior_revision=None,
                               detail_json=None, receipt_digest=None, created_at=None):
        """Persist a clone/canary/rollback record (plan §7.1). The receipt itself
        routes no traffic; it records what was prepared or restored."""
        self.ensure_workspace(workspace_id)
        self._insert("deployment_receipts",
                     {"workspace_id": workspace_id, "receipt_id": receipt_id,
                      "agent_id": agent_id, "kind": kind, "variant_id": variant_id,
                      "config_hash": config_hash, "prior_revision": prior_revision,
                      "detail_json": detail_json, "receipt_digest": receipt_digest,
                      "created_at": created_at if created_at is not None else self._now()},
                     replace=True)

    def list_deployment_receipts(self, workspace_id, *, agent_id=None, kind=None, limit=50):
        q = "SELECT * FROM deployment_receipts WHERE workspace_id=?"
        args = [workspace_id]
        if agent_id:
            q += " AND agent_id=?"; args.append(agent_id)
        if kind:
            q += " AND kind=?"; args.append(kind)
        q += " ORDER BY created_at DESC LIMIT ?"
        args.append(limit)
        return self._all(q, tuple(args))

    def add_attestation(self, workspace_id, attestation_id, *, subject_kind=None,
                        subject_id=None, signer=None, subject_digest=None, statement=None,
                        algorithm=None, created_at=None):
        """Persist a detached attestation over a subject digest (plan §7.1)."""
        self.ensure_workspace(workspace_id)
        self._insert("attestations",
                     {"workspace_id": workspace_id, "attestation_id": attestation_id,
                      "subject_kind": subject_kind, "subject_id": subject_id,
                      "signer": signer, "subject_digest": subject_digest,
                      "statement": statement, "algorithm": algorithm,
                      "created_at": created_at if created_at is not None else self._now()},
                     replace=True)

    def list_attestations(self, workspace_id, *, subject_kind=None, subject_id=None, limit=50):
        q = "SELECT * FROM attestations WHERE workspace_id=?"
        args = [workspace_id]
        if subject_kind:
            q += " AND subject_kind=?"; args.append(subject_kind)
        if subject_id:
            q += " AND subject_id=?"; args.append(subject_id)
        q += " ORDER BY created_at DESC LIMIT ?"
        args.append(limit)
        return self._all(q, tuple(args))

    def add_variant(self, workspace_id, variant_id, *, trial_id=None, agent_id=None,
                    config_delta_json=None, expected_json=None, observed_json=None,
                    eligible=None, rank=None, created_at=None):
        """Persist a configuration variant and its expected/observed effect."""
        self.ensure_workspace(workspace_id)
        self._insert("variants",
                     {"workspace_id": workspace_id, "variant_id": variant_id,
                      "trial_id": trial_id, "agent_id": agent_id,
                      "config_delta_json": config_delta_json, "expected_json": expected_json,
                      "observed_json": observed_json, "eligible": eligible, "rank": rank,
                      "created_at": created_at if created_at is not None else self._now()},
                     replace=True)

    def list_variants(self, workspace_id, trial_id=None):
        q = "SELECT * FROM variants WHERE workspace_id=?"
        args = [workspace_id]
        if trial_id:
            q += " AND trial_id=?"; args.append(trial_id)
        q += " ORDER BY COALESCE(rank, 0), created_at"
        return self._all(q, tuple(args))

    def get_watermark(self, workspace_id, agent_id, source):
        """The durable per-(workspace,agent,source) high-water mark, or None
        if none has been recorded yet (plan §9.1)."""
        row = self._one("SELECT last_watermark FROM watermarks "
                        "WHERE workspace_id=? AND agent_id=? AND source=?",
                        (workspace_id, agent_id, source))
        return row["last_watermark"] if row else None

    def set_watermark(self, workspace_id, agent_id, source, last_watermark):
        self.ensure_workspace(workspace_id)
        self._insert("watermarks",
                     {"workspace_id": workspace_id, "agent_id": agent_id, "source": source,
                      "last_watermark": last_watermark, "updated_at": self._now()},
                     replace=True)

    def counts(self, workspace_id) -> dict:
        out = {}
        for t in ("agents", "deployments", "calls", "recordings", "candidates",
                  "labels", "contracts", "trials", "decisions"):
            row = self._one(f"SELECT COUNT(*) c FROM {t} WHERE workspace_id=?", (workspace_id,))
            out[t] = row["c"] if row else 0
        return out

    # --- low-level ------------------------------------------------------
    def _one(self, q, args=()):
        cur = self.conn.execute(q, args)
        return cur.fetchone()

    def _all(self, q, args=()):
        cur = self.conn.execute(q, args)
        return [dict(r) for r in cur.fetchall()]


__all__ = ["Registry", "DEFAULT_HOME", "SCHEMA_VERSION"]
