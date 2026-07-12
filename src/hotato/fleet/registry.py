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

# Primary-key columns per table, used to build a real ON CONFLICT upsert in
# _insert(replace=True) instead of the old INSERT OR REPLACE (which SQLite
# implements as DELETE-then-INSERT and therefore nulls every column the caller
# did not mention). Keep in sync with the PRIMARY KEY clauses in _SCHEMA.
TABLE_PK = {
    "workspaces": ("workspace_id",),
    "connections": ("workspace_id", "connection_id"),
    "agents": ("workspace_id", "agent_id"),
    "deployments": ("workspace_id", "deployment_id"),
    "calls": ("workspace_id", "call_id"),
    "recordings": ("workspace_id", "recording_id"),
    "candidates": ("workspace_id", "candidate_id"),
    "labels": ("workspace_id", "label_id"),
    "contracts": ("workspace_id", "contract_id"),
    "trials": ("workspace_id", "trial_id"),
    "decisions": ("workspace_id", "decision_id"),
    "contract_sets": ("workspace_id", "set_id"),
    "deployment_receipts": ("workspace_id", "receipt_id"),
    "attestations": ("workspace_id", "attestation_id"),
    "variants": ("workspace_id", "variant_id"),
    "watermarks": ("workspace_id", "agent_id", "source"),
}


class RegistrySchemaVersionError(ValueError):
    """The on-disk fleet registry's meta.schema_version is missing/unparseable
    or newer than this install's SCHEMA_VERSION. A ValueError subclass so it
    free-rides on the existing errors.HANDLED contract (exit 2, clean
    structured error) with no plumbing changes needed in cli.py/mcp_server.py.
    Fails closed: an install must never silently read or write a store shaped
    by a schema version it does not understand."""

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
        # A generous, overridable busy window instead of sqlite3's implicit
        # 5.0s default connect timeout. Concurrent multi-process writers are an
        # in-spec shape here (JobQueue.claim's BEGIN IMMEDIATE, every CLI/MCP
        # call opening its own Registry), so 5s is an undocumented ceiling that
        # surfaces as a raw sqlite3.OperationalError. `timeout=` sets SQLite's
        # own internal busy-retry window; PRAGMA busy_timeout below restates it
        # explicitly for clarity/consistency with the other PRAGMA calls.
        timeout_sec = float(os.environ.get("HOTATO_FLEET_DB_TIMEOUT_SEC", "30"))
        # isolation_level=None (autocommit): the concurrent write paths
        # (JobQueue.enqueue/claim) drive transactions manually with explicit
        # BEGIN IMMEDIATE / COMMIT / ROLLBACK. Under sqlite3's DEFAULT
        # isolation_level ("") the module injects its own implicit BEGIN before
        # DML and manages the transaction itself, fighting the manual control --
        # an interaction whose timing changed across CPython 3.11/3.12 and
        # deadlocked two same-key writers (one held the write lock in a half-open
        # implicit transaction it never committed; the others timed out on
        # `database is locked`). Autocommit makes the explicit statements the
        # ONLY transaction control -- the documented, version-stable way to run
        # manual BEGIN IMMEDIATE. Every non-manual write here is a single
        # statement, so its atomicity is unchanged and the trailing .commit()
        # calls become harmless no-ops.
        self.conn = sqlite3.connect(self.db_path, timeout=timeout_sec,
                                    isolation_level=None)
        self.conn.row_factory = sqlite3.Row
        # Concurrent Registry construction on a FRESH db races on the schema-init
        # writes -- most sharply `PRAGMA journal_mode=WAL`, which needs a brief
        # exclusive lock to rewrite the db header and (unlike ordinary DML) is not
        # reliably covered by the connect busy timeout, so a co-launched second
        # constructor can see `database is locked`. The whole sequence is idempotent
        # (CREATE IF NOT EXISTS, additive migrations, meta upsert), so it is retried
        # with bounded backoff before the busy error is surfaced as the shared
        # errors.HANDLED OSError contract; the connection is closed on the terminal
        # failure path. (Was the CI hang: two threads constructing a Registry at
        # once, the loser's uncaught OSError killing its thread before a 2-party
        # test barrier, leaving the survivor blocked on that barrier forever.)
        for _attempt in range(12):
            try:
                self.conn.execute("PRAGMA journal_mode=WAL")
                self.conn.execute("PRAGMA foreign_keys=ON")
                self.conn.execute(f"PRAGMA busy_timeout={int(timeout_sec * 1000)}")
                self.conn.executescript(_SCHEMA)
                # Additive column migrations. A fresh DB already has these from the
                # CREATE TABLE above (IF NOT EXISTS never rewrites an existing
                # table), so this backfills only older DB files. Each is guarded
                # by table_info.
                self._ensure_column("recordings", "retention_policy_json", "TEXT")
                self._ensure_column("recordings", "pii_class", "TEXT")
                self._ensure_column("variants", "agent_id", "TEXT")
                self._ensure_column("variants", "expected_json", "TEXT")
                self._ensure_column("variants", "observed_json", "TEXT")
                self._ensure_column("variants", "rank", "INTEGER")
                cur = self.conn.execute("SELECT value FROM meta WHERE key='schema_version'")
                row = cur.fetchone()
                if row is None:
                    # OR IGNORE: a co-launched constructor on the same fresh db
                    # can win the race to seed schema_version between our SELECT
                    # and INSERT; both write the identical value, so the loser's
                    # duplicate is a no-op, never a UNIQUE-constraint IntegrityError.
                    self.conn.execute(
                        "INSERT OR IGNORE INTO meta (key, value) VALUES ('schema_version', ?)",
                        (str(SCHEMA_VERSION),))
                else:
                    try:
                        stored = int(row["value"])
                    except (TypeError, ValueError):
                        stored = None
                    # Fail closed on any parse ambiguity (garbage/non-numeric
                    # value): treat it the same as "newer/unknown" rather than
                    # proceeding.
                    if stored is None or stored > SCHEMA_VERSION:
                        raise RegistrySchemaVersionError(
                            f"fleet registry at {self.db_path} was written by a "
                            f"newer hotato (schema {row['value']!r}) than this "
                            f"install understands (schema {SCHEMA_VERSION}); "
                            "upgrade hotato (`pip install -U hotato`) or point "
                            "--home at a fresh directory.")
                    if stored < SCHEMA_VERSION:
                        # Additive migrations above already ran; record that the
                        # store now tracks the current schema instead of staying
                        # stale.
                        self.conn.execute(
                            "UPDATE meta SET value=? WHERE key='schema_version'",
                            (str(SCHEMA_VERSION),))
                self.conn.commit()
                break
            except sqlite3.OperationalError as exc:
                _m = str(exc).lower()
                if ("locked" in _m or "busy" in _m) and _attempt < 11:
                    if self.conn.in_transaction:
                        try:
                            self.conn.rollback()
                        except sqlite3.OperationalError:
                            pass
                    time.sleep(min(0.02 * (2 ** _attempt), 0.5))
                    continue
                self.conn.close()
                raise OSError(
                    f"fleet database busy (concurrent writers): {exc}. "
                    "Retry the command."
                ) from exc
            except Exception:
                self.conn.close()
                raise

    def close(self):
        self.conn.close()

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()

    # --- generic helpers ------------------------------------------------
    def _busy_wrap(self, fn):
        """Run a write and translate a busy-database sqlite3.OperationalError
        (lock contention past the busy_timeout window) into the shared
        errors.HANDLED OSError contract, so the CLI and every MCP fleet tool
        emit the existing clean, structured, exit-code-2 error envelope
        instead of leaking a raw traceback. The write never partially applies:
        SQLite raises before any bytes are committed."""
        try:
            return fn()
        except sqlite3.OperationalError as exc:
            raise OSError(
                f"fleet database busy (concurrent writers): {exc}. "
                "Retry the command."
            ) from exc

    def _insert(self, table: str, row: dict, *, replace: bool = False):
        cols = list(row.keys())
        col_list = ",".join(cols)
        marks = ",".join("?" for _ in cols)
        if not replace:
            sql = f"INSERT INTO {table} ({col_list}) VALUES ({marks})"
        else:
            # A real UPSERT keyed on the table's primary key, NOT the old
            # INSERT OR REPLACE (SQLite implements that as DELETE-then-INSERT,
            # which reverts every column absent from `row` to NULL/default).
            # Only columns present in `row` appear in the SET clause, so a
            # column the caller did not mention (e.g. retention_policy_json on
            # a re-add_recording) is left untouched on conflict.
            pk = TABLE_PK.get(table)
            if not pk:
                raise ValueError(f"_insert(replace=True) needs a TABLE_PK entry for {table!r}")
            update_cols = [c for c in cols if c not in pk]
            conflict = ",".join(pk)
            if update_cols:
                set_clause = ", ".join(f"{c}=excluded.{c}" for c in update_cols)
                sql = (f"INSERT INTO {table} ({col_list}) VALUES ({marks}) "
                      f"ON CONFLICT({conflict}) DO UPDATE SET {set_clause}")
            else:
                # every passed column IS a PK column: nothing to update on
                # conflict, so a plain idempotent insert-or-ignore.
                sql = (f"INSERT INTO {table} ({col_list}) VALUES ({marks}) "
                      f"ON CONFLICT({conflict}) DO NOTHING")
        self._busy_wrap(lambda: (self.conn.execute(sql, tuple(row.values())),
                                 self.conn.commit()))

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
            try:
                self.conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {decl}")
            except sqlite3.OperationalError as exc:
                # A concurrent constructor may have added the same column between
                # the table_info check and here -- that is exactly the desired end
                # state, so a duplicate-column race is a no-op. Any other error
                # (including a genuine 'database is locked') re-raises for the
                # __init__ retry loop / busy translation to handle.
                if "duplicate column" not in str(exc).lower():
                    raise

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
        self._busy_wrap(lambda: (
            self.conn.execute(
                "UPDATE candidates SET status=? WHERE workspace_id=? AND candidate_id=?",
                (status, workspace_id, candidate_id)),
            self.conn.commit()))

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
        sql = f"UPDATE recordings SET {', '.join(sets)} WHERE workspace_id=? AND recording_id=?"
        self._busy_wrap(lambda: (self.conn.execute(sql, tuple(args)), self.conn.commit()))

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


__all__ = ["Registry", "DEFAULT_HOME", "SCHEMA_VERSION", "TABLE_PK",
          "RegistrySchemaVersionError"]
