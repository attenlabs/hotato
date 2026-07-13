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
# v2 (Phase-1 §F): the 8-entity conversation-QA model (releases, suites,
# scenarios, runs, conversations, evaluations, reviews) + the assertion_runs
# index, all ADDITIVE. A v1 store upgrades in place: the new CREATE TABLE IF NOT
# EXISTS statements and the agents _ensure_column backfills run on open, then the
# stored marker advances 1 -> 2 (the existing `stored < SCHEMA_VERSION` branch).
# No existing table/column/behavior is dropped or rewritten.
SCHEMA_VERSION = 2

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
    # Phase-1 §F 8-entity conversation-QA model (additive).
    "releases": ("workspace_id", "release_id"),
    "suites": ("workspace_id", "suite_id"),
    "scenarios": ("workspace_id", "scenario_id"),
    "runs": ("workspace_id", "run_id"),
    "conversations": ("workspace_id", "conversation_id"),
    "evaluations": ("workspace_id", "evaluation_id"),
    "reviews": ("workspace_id", "review_id"),
    "assertion_runs": ("workspace_id", "assertion_run_id"),
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
  current_release_id TEXT,    -- additive (§F Agent): the release currently under test
  configuration_digest TEXT,  -- additive (§F Agent): digest of the agent's pinned config
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

-- =====================================================================
-- Phase-1 §F: the 8-entity conversation-QA model. ADDITIVE -- these are
-- NEW tables alongside the existing ones; nothing is dropped or reshaped in
-- place. Conversation/Review carry the lineage of recordings/calls and
-- labels/decisions (§F) but are separate tables so no existing row/behavior
-- moves. The DB only INDEXES evidence artifacts by digest + stores
-- relationships; the immutable evidence system stays the source of truth. No
-- overall_score column anywhere (honesty invariant 1); deterministic vs
-- model-judged stay separate lanes (a `deterministic` flag, invariant 2);
-- an absent input is recorded as INCONCLUSIVE, never a fabricated FAIL
-- (invariant 3); nothing here phones home (invariant 4); origin real|simulated
-- keeps synthetic distinct from real (invariant 5).
-- =====================================================================

CREATE TABLE IF NOT EXISTS releases (
  workspace_id TEXT NOT NULL,
  release_id TEXT NOT NULL,
  agent_id TEXT,
  prompt_digest TEXT,             -- content-addressed SNAPSHOT of what was tested
  model TEXT,
  voice TEXT,
  tool_schema_digest TEXT,
  workflow_digest TEXT,
  provider_config_digest TEXT,
  created_at REAL,
  PRIMARY KEY (workspace_id, release_id)
);

CREATE TABLE IF NOT EXISTS suites (
  workspace_id TEXT NOT NULL,
  suite_id TEXT NOT NULL,
  name TEXT,
  purpose TEXT,
  required_for_release INTEGER DEFAULT 0,
  inconclusive_policy TEXT DEFAULT 'report',  -- reuse the Phase-0 field semantics
  created_at REAL,
  PRIMARY KEY (workspace_id, suite_id)
);

CREATE TABLE IF NOT EXISTS scenarios (
  workspace_id TEXT NOT NULL,
  scenario_id TEXT NOT NULL,
  suite_id TEXT,
  goal TEXT,
  facts_json TEXT,                -- caller's known ground truth
  caller_policy_json TEXT,        -- persona / behavior
  environment_matrix_json TEXT,
  assertions_json TEXT,           -- indexed from the conversation-test file
  rubrics_json TEXT,
  created_at REAL,
  PRIMARY KEY (workspace_id, scenario_id)
);

CREATE TABLE IF NOT EXISTS runs (
  workspace_id TEXT NOT NULL,
  run_id TEXT NOT NULL,
  scenario_id TEXT,
  release_id TEXT,
  seed TEXT,
  provider_route TEXT,
  status TEXT DEFAULT 'created',  -- created | running | completed | refused
  started_at REAL,
  completed_at REAL,
  created_at REAL,
  PRIMARY KEY (workspace_id, run_id)
);

CREATE TABLE IF NOT EXISTS conversations (
  workspace_id TEXT NOT NULL,
  conversation_id TEXT NOT NULL,
  run_id TEXT,
  agent_id TEXT,
  origin TEXT,                    -- real | simulated (invariant 5)
  artifact_digest TEXT,           -- content-addressed conversation.v1 manifest
  capture_receipt TEXT,
  created_at REAL,
  PRIMARY KEY (workspace_id, conversation_id)
);

CREATE TABLE IF NOT EXISTS evaluations (
  workspace_id TEXT NOT NULL,
  evaluation_id TEXT NOT NULL,
  conversation_id TEXT,
  evaluator_id TEXT,
  dimension TEXT,                 -- outcome | policy | conversation | speech | reliability
  status TEXT,                    -- PASS | FAIL | INCONCLUSIVE (never a blended score)
  evidence_refs TEXT,             -- JSON: authenticated evidence digests/spans
  provenance TEXT,
  created_at REAL,
  PRIMARY KEY (workspace_id, evaluation_id)
);

CREATE TABLE IF NOT EXISTS reviews (
  workspace_id TEXT NOT NULL,
  review_id TEXT NOT NULL,
  evaluation_id TEXT,
  reviewer TEXT,
  decision TEXT,
  rationale TEXT,
  revision INTEGER DEFAULT 1,
  adjudication_state TEXT,
  created_at REAL,
  PRIMARY KEY (workspace_id, review_id)
);

-- assertion_runs: persist ONE assert.v1 result against a fleet-registered
-- call/agent (closes the Phase-0 gap where an evaluation was computed but never
-- indexed, so a dashboard could not read it back). Writes reuse the jobs.py
-- leased-write + idempotency-key pattern: assertion_run_id is a deterministic
-- key, so a duplicate CLI run / retried job dedups to ONE row.
CREATE TABLE IF NOT EXISTS assertion_runs (
  workspace_id TEXT NOT NULL,
  assertion_run_id TEXT NOT NULL, -- deterministic idempotency key
  agent_id TEXT,
  call_id TEXT,                   -- the fleet-registered call this ran against
  conversation_id TEXT,           -- optional link to a conversation artifact
  assertion_id TEXT,
  kind TEXT,                      -- assert.v1 kind
  dimension TEXT,                 -- outcome | policy | conversation | speech | reliability
  deterministic INTEGER DEFAULT 1,-- 1 deterministic lane, 0 model-judged lane (invariant 2)
  status TEXT,                    -- PASS | FAIL | INCONCLUSIVE
  reason TEXT,
  evidence_refs TEXT,             -- JSON: authenticated trace/state refs consumed
  result_json TEXT,               -- the full assert.v1 result envelope
  created_at REAL,
  PRIMARY KEY (workspace_id, assertion_run_id)
);

CREATE INDEX IF NOT EXISTS idx_calls_ws_deploy ON calls (workspace_id, deployment_id);
CREATE INDEX IF NOT EXISTS idx_cand_ws_agent ON candidates (workspace_id, agent_id, status);
CREATE INDEX IF NOT EXISTS idx_rec_ws_call ON recordings (workspace_id, call_id);
CREATE INDEX IF NOT EXISTS idx_deprcpt_ws_agent ON deployment_receipts (workspace_id, agent_id, kind);
CREATE INDEX IF NOT EXISTS idx_runs_ws_scenario ON runs (workspace_id, scenario_id);
CREATE INDEX IF NOT EXISTS idx_conv_ws_run ON conversations (workspace_id, run_id);
CREATE INDEX IF NOT EXISTS idx_eval_ws_conv ON evaluations (workspace_id, conversation_id);
CREATE INDEX IF NOT EXISTS idx_review_ws_eval ON reviews (workspace_id, evaluation_id);
CREATE INDEX IF NOT EXISTS idx_arun_ws_agent ON assertion_runs (workspace_id, agent_id, call_id);
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
                # v2 (§F Agent): extend the existing agents table in place on an
                # older DB. A fresh DB already has these from CREATE TABLE above.
                self._ensure_column("agents", "current_release_id", "TEXT")
                self._ensure_column("agents", "configuration_digest", "TEXT")
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

    def _insert(self, table: str, row: dict, *, replace: bool = False, ignore: bool = False):
        cols = list(row.keys())
        col_list = ",".join(cols)
        marks = ",".join("?" for _ in cols)
        if ignore:
            # INSERT OR IGNORE: an idempotent seed. A concurrent constructor
            # (or a re-ensure of the same key) that already wrote this primary
            # key makes the duplicate a silent no-op, never a UNIQUE-constraint
            # IntegrityError -- the same first-writer-wins posture as the
            # schema_version meta row and assertion_runs. Unlike replace=, the
            # existing row's other columns are left untouched.
            sql = f"INSERT OR IGNORE INTO {table} ({col_list}) VALUES ({marks})"
        elif not replace:
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
        # INSERT OR IGNORE, not a bare INSERT: two threads can both pass the
        # get_workspace guard on a fresh db before either commits (the
        # test_concurrent_registry_construction race), so the loser's insert
        # must no-op rather than raise UNIQUE constraint failed.
        if not self.get_workspace(workspace_id):
            self._insert("workspaces", {"workspace_id": workspace_id,
                                        "name": name or workspace_id,
                                        "created_at": self._now()}, ignore=True)
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

    # --- §F 8-entity conversation-QA model (additive) -------------------
    def set_agent_release(self, workspace_id, agent_id, *, current_release_id=None,
                          configuration_digest=None):
        """Point an existing agent at the release it is currently testing and/or
        pin its configuration digest (§F Agent). Only the fields passed are
        updated -- the rest of the agent row is untouched."""
        sets, args = [], []
        if current_release_id is not None:
            sets.append("current_release_id=?"); args.append(current_release_id)
        if configuration_digest is not None:
            sets.append("configuration_digest=?"); args.append(configuration_digest)
        if not sets:
            return
        args.extend([workspace_id, agent_id])
        sql = f"UPDATE agents SET {', '.join(sets)} WHERE workspace_id=? AND agent_id=?"
        self._busy_wrap(lambda: (self.conn.execute(sql, tuple(args)), self.conn.commit()))

    def add_release(self, workspace_id, release_id, *, agent_id=None, prompt_digest=None,
                    model=None, voice=None, tool_schema_digest=None, workflow_digest=None,
                    provider_config_digest=None, created_at=None):
        """Record a content-addressed SNAPSHOT of what was tested (§E/§F Release):
        the pinned prompt/tool/workflow/provider digests, so `release compare` is
        digest-exact. The DB indexes the digests; it stores no config bytes."""
        self.ensure_workspace(workspace_id)
        self._insert("releases",
                     {"workspace_id": workspace_id, "release_id": release_id,
                      "agent_id": agent_id, "prompt_digest": prompt_digest, "model": model,
                      "voice": voice, "tool_schema_digest": tool_schema_digest,
                      "workflow_digest": workflow_digest,
                      "provider_config_digest": provider_config_digest,
                      "created_at": created_at if created_at is not None else self._now()},
                     replace=True)

    def get_release(self, workspace_id, release_id):
        return self._one("SELECT * FROM releases WHERE workspace_id=? AND release_id=?",
                         (workspace_id, release_id))

    def list_releases(self, workspace_id, *, agent_id=None, limit=50):
        q = "SELECT * FROM releases WHERE workspace_id=?"
        args = [workspace_id]
        if agent_id:
            q += " AND agent_id=?"; args.append(agent_id)
        q += " ORDER BY created_at DESC LIMIT ?"
        args.append(limit)
        return self._all(q, tuple(args))

    def add_suite(self, workspace_id, suite_id, *, name=None, purpose=None,
                  required_for_release=0, inconclusive_policy="report", created_at=None):
        """Register a named suite of conversation-tests (§E Suite). Reuses the
        Phase-0 inconclusive_policy semantics (report|fail|refuse)."""
        self.ensure_workspace(workspace_id)
        self._insert("suites",
                     {"workspace_id": workspace_id, "suite_id": suite_id, "name": name,
                      "purpose": purpose,
                      "required_for_release": 1 if required_for_release else 0,
                      "inconclusive_policy": inconclusive_policy,
                      "created_at": created_at if created_at is not None else self._now()},
                     replace=True)

    def get_suite(self, workspace_id, suite_id):
        return self._one("SELECT * FROM suites WHERE workspace_id=? AND suite_id=?",
                         (workspace_id, suite_id))

    def list_suites(self, workspace_id, *, limit=50):
        return self._all("SELECT * FROM suites WHERE workspace_id=? "
                         "ORDER BY created_at DESC LIMIT ?", (workspace_id, limit))

    def add_scenario(self, workspace_id, scenario_id, *, suite_id=None, goal=None,
                     facts_json=None, caller_policy_json=None, environment_matrix_json=None,
                     assertions_json=None, rubrics_json=None, created_at=None):
        """Index a conversation-test's scenario (§F Scenario): the goal, the caller's
        known facts, its assertions/rubrics. The test FILE stays the source of
        truth; this row is a queryable index over it, never a second copy of the
        evidence."""
        self.ensure_workspace(workspace_id)
        self._insert("scenarios",
                     {"workspace_id": workspace_id, "scenario_id": scenario_id,
                      "suite_id": suite_id, "goal": goal, "facts_json": facts_json,
                      "caller_policy_json": caller_policy_json,
                      "environment_matrix_json": environment_matrix_json,
                      "assertions_json": assertions_json, "rubrics_json": rubrics_json,
                      "created_at": created_at if created_at is not None else self._now()},
                     replace=True)

    def get_scenario(self, workspace_id, scenario_id):
        return self._one("SELECT * FROM scenarios WHERE workspace_id=? AND scenario_id=?",
                         (workspace_id, scenario_id))

    def list_scenarios(self, workspace_id, *, suite_id=None, limit=50):
        q = "SELECT * FROM scenarios WHERE workspace_id=?"
        args = [workspace_id]
        if suite_id:
            q += " AND suite_id=?"; args.append(suite_id)
        q += " ORDER BY created_at DESC LIMIT ?"
        args.append(limit)
        return self._all(q, tuple(args))

    def add_run(self, workspace_id, run_id, *, scenario_id=None, release_id=None, seed=None,
                provider_route=None, status="created", started_at=None, completed_at=None,
                created_at=None):
        """Record one execution of a scenario against a release (§F Run)."""
        self.ensure_workspace(workspace_id)
        self._insert("runs",
                     {"workspace_id": workspace_id, "run_id": run_id,
                      "scenario_id": scenario_id, "release_id": release_id, "seed": seed,
                      "provider_route": provider_route, "status": status,
                      "started_at": started_at, "completed_at": completed_at,
                      "created_at": created_at if created_at is not None else self._now()},
                     replace=True)

    def get_run(self, workspace_id, run_id):
        return self._one("SELECT * FROM runs WHERE workspace_id=? AND run_id=?",
                         (workspace_id, run_id))

    def list_runs(self, workspace_id, *, scenario_id=None, release_id=None, limit=50):
        q = "SELECT * FROM runs WHERE workspace_id=?"
        args = [workspace_id]
        if scenario_id:
            q += " AND scenario_id=?"; args.append(scenario_id)
        if release_id:
            q += " AND release_id=?"; args.append(release_id)
        q += " ORDER BY created_at DESC LIMIT ?"
        args.append(limit)
        return self._all(q, tuple(args))

    def set_run_status(self, workspace_id, run_id, status, *, completed_at=None):
        sets, args = ["status=?"], [status]
        if completed_at is not None:
            sets.append("completed_at=?"); args.append(completed_at)
        args.extend([workspace_id, run_id])
        sql = f"UPDATE runs SET {', '.join(sets)} WHERE workspace_id=? AND run_id=?"
        self._busy_wrap(lambda: (self.conn.execute(sql, tuple(args)), self.conn.commit()))

    def add_conversation(self, workspace_id, conversation_id, *, run_id=None, agent_id=None,
                         origin=None, artifact_digest=None, capture_receipt=None,
                         created_at=None):
        """Index a conversation.v1 artifact by digest (§D/§F Conversation). ``origin``
        (real|simulated) keeps synthetic distinct from real (invariant 5). The
        immutable artifact directory is the evidence; this row only points at it."""
        self.ensure_workspace(workspace_id)
        self._insert("conversations",
                     {"workspace_id": workspace_id, "conversation_id": conversation_id,
                      "run_id": run_id, "agent_id": agent_id, "origin": origin,
                      "artifact_digest": artifact_digest, "capture_receipt": capture_receipt,
                      "created_at": created_at if created_at is not None else self._now()},
                     replace=True)

    def get_conversation(self, workspace_id, conversation_id):
        return self._one("SELECT * FROM conversations WHERE workspace_id=? AND conversation_id=?",
                         (workspace_id, conversation_id))

    def list_conversations(self, workspace_id, *, run_id=None, agent_id=None, origin=None,
                           limit=50):
        q = "SELECT * FROM conversations WHERE workspace_id=?"
        args = [workspace_id]
        if run_id:
            q += " AND run_id=?"; args.append(run_id)
        if agent_id:
            q += " AND agent_id=?"; args.append(agent_id)
        if origin:
            q += " AND origin=?"; args.append(origin)
        q += " ORDER BY created_at DESC LIMIT ?"
        args.append(limit)
        return self._all(q, tuple(args))

    def add_evaluation(self, workspace_id, evaluation_id, *, conversation_id=None,
                       evaluator_id=None, dimension=None, status=None, evidence_refs=None,
                       provenance=None, created_at=None):
        """Persist one evaluator's verdict against a conversation (§F Evaluation):
        a per-dimension PASS/FAIL/INCONCLUSIVE, never a blended score. Deterministic
        and model-judged evaluators write to the same table but stay separable by
        their evaluator_id/dimension -- there is no scorer path here."""
        self.ensure_workspace(workspace_id)
        self._insert("evaluations",
                     {"workspace_id": workspace_id, "evaluation_id": evaluation_id,
                      "conversation_id": conversation_id, "evaluator_id": evaluator_id,
                      "dimension": dimension, "status": status,
                      "evidence_refs": evidence_refs, "provenance": provenance,
                      "created_at": created_at if created_at is not None else self._now()},
                     replace=True)

    def list_evaluations(self, workspace_id, *, conversation_id=None, dimension=None, limit=50):
        q = "SELECT * FROM evaluations WHERE workspace_id=?"
        args = [workspace_id]
        if conversation_id:
            q += " AND conversation_id=?"; args.append(conversation_id)
        if dimension:
            q += " AND dimension=?"; args.append(dimension)
        q += " ORDER BY created_at DESC LIMIT ?"
        args.append(limit)
        return self._all(q, tuple(args))

    def add_review(self, workspace_id, review_id, *, evaluation_id=None, reviewer=None,
                   decision=None, rationale=None, revision=1, adjudication_state=None,
                   created_at=None):
        """Record a HUMAN review of an evaluation (§F Review). No model may write a
        review -- the caller enforces that at the API layer, as label() already does."""
        self.ensure_workspace(workspace_id)
        self._insert("reviews",
                     {"workspace_id": workspace_id, "review_id": review_id,
                      "evaluation_id": evaluation_id, "reviewer": reviewer,
                      "decision": decision, "rationale": rationale, "revision": revision,
                      "adjudication_state": adjudication_state,
                      "created_at": created_at if created_at is not None else self._now()},
                     replace=True)

    def list_reviews(self, workspace_id, *, evaluation_id=None, limit=50):
        q = "SELECT * FROM reviews WHERE workspace_id=?"
        args = [workspace_id]
        if evaluation_id:
            q += " AND evaluation_id=?"; args.append(evaluation_id)
        q += " ORDER BY created_at DESC LIMIT ?"
        args.append(limit)
        return self._all(q, tuple(args))

    def add_assertion_run(self, workspace_id, *, assertion_id, agent_id=None, call_id=None,
                          conversation_id=None, kind=None, dimension=None, deterministic=True,
                          status=None, reason=None, evidence_refs=None, result_json=None,
                          assertion_run_id=None, created_at=None) -> dict:
        """Persist ONE assert.v1 result against a fleet-registered call/agent
        (closes the Phase-0 assertion_runs gap: an evaluation was computed but
        never indexed, so a dashboard could not read it back).

        Reuses the jobs.py leased-write + idempotency-key pattern: when the caller
        does not supply ``assertion_run_id`` a DETERMINISTIC key is derived from
        the logical work (workspace, agent, call/conversation, assertion, kind) via
        :func:`hotato.fleet.jobs.idempotency_key`, so a duplicate CLI run / retried
        job maps to the SAME row. The write is ``INSERT OR IGNORE`` and reports
        ``deduped`` from the row-count: two concurrent writers with the identical
        key both succeed (one inserts, the loser's duplicate is a no-op), never a
        UNIQUE-constraint IntegrityError -- the same concurrency-safe posture the
        schema_version seed uses. No overall_score is recorded; ``deterministic``
        tags the lane (invariant 2) and an absent-input INCONCLUSIVE is stored
        verbatim, never coerced to a FAIL (invariant 3)."""
        self.ensure_workspace(workspace_id)
        if assertion_run_id is None:
            from .jobs import idempotency_key
            assertion_run_id = idempotency_key(
                workspace_id=workspace_id, agent_id=agent_id,
                operation=f"assert:{assertion_id}",
                source_pcm_hash=str(call_id or conversation_id or ""),
                scorer_hash=str(kind or ""))
        row = {"workspace_id": workspace_id, "assertion_run_id": assertion_run_id,
               "agent_id": agent_id, "call_id": call_id, "conversation_id": conversation_id,
               "assertion_id": assertion_id, "kind": kind, "dimension": dimension,
               "deterministic": 1 if deterministic else 0, "status": status,
               "reason": reason, "evidence_refs": evidence_refs, "result_json": result_json,
               "created_at": created_at if created_at is not None else self._now()}
        cols = ",".join(row.keys())
        marks = ",".join("?" for _ in row)
        sql = f"INSERT OR IGNORE INTO assertion_runs ({cols}) VALUES ({marks})"

        def _write():
            cur = self.conn.execute(sql, tuple(row.values()))
            self.conn.commit()
            return cur.rowcount
        inserted = self._busy_wrap(_write)
        return {"assertion_run_id": assertion_run_id, "deduped": inserted == 0}

    def get_assertion_run(self, workspace_id, assertion_run_id):
        return self._one("SELECT * FROM assertion_runs WHERE workspace_id=? AND assertion_run_id=?",
                         (workspace_id, assertion_run_id))

    def list_assertion_runs(self, workspace_id, *, agent_id=None, call_id=None,
                            conversation_id=None, dimension=None, limit=50):
        q = "SELECT * FROM assertion_runs WHERE workspace_id=?"
        args = [workspace_id]
        if agent_id:
            q += " AND agent_id=?"; args.append(agent_id)
        if call_id:
            q += " AND call_id=?"; args.append(call_id)
        if conversation_id:
            q += " AND conversation_id=?"; args.append(conversation_id)
        if dimension:
            q += " AND dimension=?"; args.append(dimension)
        q += " ORDER BY created_at DESC LIMIT ?"
        args.append(limit)
        return self._all(q, tuple(args))

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
