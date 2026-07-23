"""The console sidecar store: derived per-session score records, never authority.

``console.sqlite3`` sits BESIDE the production evidence database and holds only
DERIVED data: per-session score records the console worker computed from the
evidence plane with the existing deterministic scorer.  Its invariants:

* the evidence database stays the single authority; every row here is
  rebuildable from it (``hotato serve --production-db DB --rebuild-scores``),
  so a schema change ships as a rebuild, never a migration;
* the schema is versioned (``console_schema_version`` metadata row); a store
  written by a different schema version is refused with a rebuild instruction
  rather than half-migrated in place;
* a write commits durably (WAL + ``synchronous=FULL``) before the caller may
  report success -- the worker treats any exception from :meth:`upsert_score`
  as a persist failure to surface, never a success;
* every scored value derives from evidence timestamps; the only wall-clock
  column is ``created_at``, and :meth:`canonical_dump` excludes it, so the
  same evidence database always produces a byte-identical canonical dump.
"""

from __future__ import annotations

import json
import os
import sqlite3
import threading
import time
from typing import Any, Dict, List, Optional, Tuple

from .errors import safe_json_dumps
from .production import _prepare_private_sqlite_file

__all__ = [
    "SCHEMA_VERSION",
    "SCORE_STATES",
    "ConsoleStoreError",
    "ConsoleStore",
]

SCHEMA_VERSION = "1"
SCORE_STATES = ("SCORED", "NOT_SCORABLE", "ERROR")

_RECORD_FIELDS = (
    "subject",
    "state",
    "reason",
    "session_state",
    "evidence_sha256",
    "event_count",
    "scorer_version",
    "config_sha256",
    "config",
    "dimensions",
    "candidates",
    "timing",
    "audio",
    "hops",
)


class ConsoleStoreError(RuntimeError):
    """The console sidecar cannot be opened or written safely."""


class ConsoleStore:
    """A single-process durable sidecar for derived console score records."""

    def __init__(self, path: str, *, clock=time.time) -> None:
        self.path = os.path.abspath(path)
        self.clock = clock
        self._lock = threading.RLock()
        guard, expected_identity = _prepare_private_sqlite_file(self.path)
        try:
            self.db = sqlite3.connect(
                self.path, check_same_thread=False, timeout=30, isolation_level=None
            )
            observed = os.stat(self.path, follow_symlinks=False)
            if (observed.st_dev, observed.st_ino) != expected_identity:
                self.db.close()
                raise ConsoleStoreError(
                    "console sidecar changed while SQLite was opening it"
                )
        finally:
            os.close(guard)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute("PRAGMA synchronous=FULL")
        self.db.execute("PRAGMA foreign_keys=ON")
        self.db.execute("PRAGMA busy_timeout=30000")
        self._schema()
        self._verify_schema_version()

    def _schema(self) -> None:
        with self._lock:
            self.db.executescript(
                """
                BEGIN IMMEDIATE;
                CREATE TABLE IF NOT EXISTS metadata(
                  key TEXT PRIMARY KEY,
                  value TEXT NOT NULL
                );
                INSERT OR IGNORE INTO metadata(key,value)
                  VALUES('console_schema_version','1');
                CREATE TABLE IF NOT EXISTS scores(
                  subject TEXT PRIMARY KEY,
                  state TEXT NOT NULL
                    CHECK(state IN ('SCORED','NOT_SCORABLE','ERROR')),
                  reason TEXT,
                  session_state TEXT NOT NULL,
                  evidence_sha256 TEXT NOT NULL,
                  event_count INTEGER NOT NULL,
                  scorer_version TEXT NOT NULL,
                  config_sha256 TEXT NOT NULL,
                  config_json TEXT NOT NULL,
                  dimensions_json TEXT NOT NULL,
                  candidates_json TEXT NOT NULL,
                  timing_json TEXT NOT NULL,
                  audio_json TEXT,
                  created_at REAL NOT NULL
                );
                CREATE TABLE IF NOT EXISTS latency_hops(
                  subject TEXT NOT NULL,
                  seq INTEGER NOT NULL,
                  kind TEXT NOT NULL,
                  at TEXT NOT NULL,
                  latency_ms REAL,
                  authority TEXT NOT NULL,
                  source TEXT NOT NULL,
                  event_id TEXT NOT NULL,
                  PRIMARY KEY(subject,seq)
                );
                CREATE INDEX IF NOT EXISTS latency_hops_kind_at
                  ON latency_hops(kind,at);
                COMMIT;
                """
            )

    def _verify_schema_version(self) -> None:
        row = self.db.execute(
            "SELECT value FROM metadata WHERE key='console_schema_version'"
        ).fetchone()
        if row is None or row[0] != SCHEMA_VERSION:
            observed = None if row is None else row[0]
            self.db.close()
            raise ConsoleStoreError(
                "console sidecar schema version "
                + repr(observed)
                + " does not match "
                + repr(SCHEMA_VERSION)
                + "; the sidecar is derived data -- regenerate it with "
                "hotato serve --production-db DB --rebuild-scores"
            )

    def _begin(self) -> None:
        self.db.execute("BEGIN IMMEDIATE")

    def _commit(self) -> None:
        self.db.execute("COMMIT")

    def _rollback(self) -> None:
        if self.db.in_transaction:
            self.db.execute("ROLLBACK")

    def upsert_score(self, record: Dict[str, Any]) -> None:
        """Durably commit one full score record (row + hop rows) atomically.

        Raises on any storage failure; the caller must treat an exception as a
        persist failure to surface, never as a stored score.
        """

        missing = sorted(set(_RECORD_FIELDS) - set(record))
        if missing:
            raise ConsoleStoreError(
                "console score record missing: " + ", ".join(missing)
            )
        if record["state"] not in SCORE_STATES:
            raise ConsoleStoreError(
                "console score state must be one of " + ", ".join(SCORE_STATES)
            )
        subject = str(record["subject"])
        now = float(self.clock())
        with self._lock:
            self._begin()
            try:
                self.db.execute(
                    "INSERT INTO scores(subject,state,reason,session_state,"
                    "evidence_sha256,event_count,scorer_version,config_sha256,"
                    "config_json,dimensions_json,candidates_json,timing_json,"
                    "audio_json,created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?) "
                    "ON CONFLICT(subject) DO UPDATE SET state=excluded.state,"
                    "reason=excluded.reason,session_state=excluded.session_state,"
                    "evidence_sha256=excluded.evidence_sha256,"
                    "event_count=excluded.event_count,"
                    "scorer_version=excluded.scorer_version,"
                    "config_sha256=excluded.config_sha256,"
                    "config_json=excluded.config_json,"
                    "dimensions_json=excluded.dimensions_json,"
                    "candidates_json=excluded.candidates_json,"
                    "timing_json=excluded.timing_json,"
                    "audio_json=excluded.audio_json,"
                    "created_at=excluded.created_at",
                    (
                        subject,
                        record["state"],
                        record["reason"],
                        record["session_state"],
                        record["evidence_sha256"],
                        int(record["event_count"]),
                        record["scorer_version"],
                        record["config_sha256"],
                        safe_json_dumps(record["config"], sort_keys=True),
                        safe_json_dumps(record["dimensions"], sort_keys=True),
                        safe_json_dumps(record["candidates"], sort_keys=True),
                        safe_json_dumps(record["timing"], sort_keys=True),
                        (
                            None
                            if record["audio"] is None
                            else safe_json_dumps(record["audio"], sort_keys=True)
                        ),
                        now,
                    ),
                )
                self.db.execute(
                    "DELETE FROM latency_hops WHERE subject=?", (subject,)
                )
                for seq, hop in enumerate(record["hops"]):
                    self.db.execute(
                        "INSERT INTO latency_hops(subject,seq,kind,at,latency_ms,"
                        "authority,source,event_id) VALUES(?,?,?,?,?,?,?,?)",
                        (
                            subject,
                            seq,
                            hop["kind"],
                            hop["at"],
                            hop["latency_ms"],
                            hop["authority"],
                            hop["source"],
                            hop["event_id"],
                        ),
                    )
                self._commit()
            except BaseException:
                self._rollback()
                raise

    def record_error(
        self,
        subject: str,
        *,
        reason: str,
        session_state: str,
        evidence_sha256: str,
        event_count: int,
        scorer_version: str,
        config_sha256: str,
    ) -> None:
        """The deliberately minimal ERROR write path.

        Used when persisting a full record failed: this single-row write is the
        smallest durable way to make that failure visible in the sidecar
        instead of silently skipping the session (a full re-score is retried
        on the next cycle because ERROR rows are never treated as settled).
        """

        self.upsert_score(
            {
                "subject": subject,
                "state": "ERROR",
                "reason": reason,
                "session_state": session_state,
                "evidence_sha256": evidence_sha256,
                "event_count": int(event_count),
                "scorer_version": scorer_version,
                "config_sha256": config_sha256,
                "config": {},
                "dimensions": {},
                "candidates": [],
                "timing": {},
                "audio": None,
                "hops": [],
            }
        )

    def score_identity(self, subject: str) -> Optional[Tuple[str, str]]:
        """``(state, evidence_sha256)`` for one subject, or ``None``."""

        with self._lock:
            row = self.db.execute(
                "SELECT state,evidence_sha256 FROM scores WHERE subject=?",
                (subject,),
            ).fetchone()
        return None if row is None else (row["state"], row["evidence_sha256"])

    def get_score(self, subject: str) -> Optional[Dict[str, Any]]:
        with self._lock:
            row = self.db.execute(
                "SELECT * FROM scores WHERE subject=?", (subject,)
            ).fetchone()
            if row is None:
                return None
            hops = self.db.execute(
                "SELECT seq,kind,at,latency_ms,authority,source,event_id "
                "FROM latency_hops WHERE subject=? ORDER BY seq",
                (subject,),
            ).fetchall()
        return self._record_from_rows(row, hops)

    def next_subject(self, after: str) -> Optional[str]:
        """The next scored subject strictly after ``after`` in canonical
        (subject-ascending) order -- one row at a time, for bounded sweeps."""

        with self._lock:
            row = self.db.execute(
                "SELECT subject FROM scores WHERE subject>? ORDER BY subject "
                "LIMIT 1",
                (after,),
            ).fetchone()
        return None if row is None else row["subject"]

    def delete_score(self, subject: str) -> None:
        with self._lock:
            self._begin()
            try:
                self.db.execute(
                    "DELETE FROM latency_hops WHERE subject=?", (subject,)
                )
                self.db.execute("DELETE FROM scores WHERE subject=?", (subject,))
                self._commit()
            except BaseException:
                self._rollback()
                raise

    def clear(self) -> None:
        """Delete every derived row (rebuild always starts from empty)."""

        with self._lock:
            self._begin()
            try:
                self.db.execute("DELETE FROM latency_hops")
                self.db.execute("DELETE FROM scores")
                self._commit()
            except BaseException:
                self._rollback()
                raise

    def counts(self) -> Dict[str, int]:
        with self._lock:
            observed = {
                row[0]: int(row[1])
                for row in self.db.execute(
                    "SELECT state,COUNT(*) FROM scores GROUP BY state"
                )
            }
        return {state: observed.get(state, 0) for state in SCORE_STATES}

    def _record_from_rows(
        self, row: sqlite3.Row, hops: List[sqlite3.Row]
    ) -> Dict[str, Any]:
        return {
            "subject": row["subject"],
            "state": row["state"],
            "reason": row["reason"],
            "session_state": row["session_state"],
            "evidence_sha256": row["evidence_sha256"],
            "event_count": row["event_count"],
            "scorer_version": row["scorer_version"],
            "config_sha256": row["config_sha256"],
            "config": json.loads(row["config_json"]),
            "dimensions": json.loads(row["dimensions_json"]),
            "candidates": json.loads(row["candidates_json"]),
            "timing": json.loads(row["timing_json"]),
            "audio": (
                None if row["audio_json"] is None else json.loads(row["audio_json"])
            ),
            "hops": [
                {
                    "kind": hop["kind"],
                    "at": hop["at"],
                    "latency_ms": hop["latency_ms"],
                    "authority": hop["authority"],
                    "source": hop["source"],
                    "event_id": hop["event_id"],
                }
                for hop in hops
            ],
        }

    def canonical_dump(self) -> str:
        """Every derived record in canonical order as canonical JSON.

        Ordering is subject-ascending (rows) and hop sequence (per row);
        ``created_at`` -- the one wall-clock column -- is excluded, so the same
        evidence database always dumps to identical bytes.  This is the
        rebuild-determinism comparison surface.
        """

        records: List[Dict[str, Any]] = []
        with self._lock:
            rows = self.db.execute(
                "SELECT * FROM scores ORDER BY subject"
            ).fetchall()
            for row in rows:
                hops = self.db.execute(
                    "SELECT seq,kind,at,latency_ms,authority,source,event_id "
                    "FROM latency_hops WHERE subject=? ORDER BY seq",
                    (row["subject"],),
                ).fetchall()
                records.append(self._record_from_rows(row, hops))
        return safe_json_dumps(
            {
                "schema": "hotato.console-scores-dump.v1",
                "schema_version": SCHEMA_VERSION,
                "records": records,
            },
            sort_keys=True,
            separators=(",", ":"),
        )

    def close(self) -> None:
        with self._lock:
            self.db.close()
