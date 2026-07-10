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

CREATE INDEX IF NOT EXISTS idx_calls_ws_deploy ON calls (workspace_id, deployment_id);
CREATE INDEX IF NOT EXISTS idx_cand_ws_agent ON candidates (workspace_id, agent_id, status);
CREATE INDEX IF NOT EXISTS idx_rec_ws_call ON recordings (workspace_id, call_id);
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
