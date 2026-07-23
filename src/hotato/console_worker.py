"""Score-on-arrival for the production evidence plane (the console engine).

A supervisor-pattern background worker polls the production evidence database
for sessions that reached ``COMPLETE``/``QUIESCENT``, scores each one with the
existing deterministic scorer (:mod:`hotato.scan` over the session's recorded
audio evidence), and durably writes the derived record to the console sidecar
(:mod:`hotato.console_store`).  Invariants:

* **the evidence database is opened strictly read-only** -- SQLite ``mode=ro``
  plus ``query_only`` plus a write-denying authorizer, the same discipline as
  :mod:`hotato.serve.production_bridge`.  Unlike that metadata-only bridge,
  this reader does select stored event payloads: the (already default-deny
  redacted) payload fields ARE the scoring evidence -- audio asset paths,
  per-hop latency numbers, turn timing;
* **one session at a time** -- sessions and events are walked with keyset
  queries; no all-sessions list is ever materialized;
* **a score is claimed only after a durable sidecar commit** -- a persist
  failure becomes a visible ERROR record (through the minimal error write
  path) plus a counter, never a success return;
* **refusal is a first-class state** -- absent/unavailable/mono/unreadable
  audio is the scorer's NOT SCORABLE refusal, recorded with its reason; a
  scorer crash on one session becomes an ERROR record and the loop continues
  to the next session;
* **no wall-clock values in scored content** -- every timing figure derives
  from evidence event timestamps and carries the reporting event's declared
  authority, so a rebuild from the same evidence database reproduces the
  sidecar byte-for-byte (canonical dump).
"""

from __future__ import annotations

import json
import os
import sqlite3
import stat
import threading
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from ._engine.score import ScoreConfig
from .core import _config_block
from .console_store import ConsoleStore
from .production import _canonical, _parse_rfc3339, _sha
from .scan import DEFAULT_MIN_GAP_SEC, KINDS, candidate_plain_english, scan_recording

__all__ = [
    "SCORABLE_SESSION_STATES",
    "ConsoleWorkerError",
    "ConsoleScoreWorker",
    "default_console_path",
    "rebuild_sidecar",
    "run_rebuild",
    "score_session",
    "scorer_provenance",
]

# The arrival condition (spec R1): a session has quiesced (``session.ended``
# observed) or was finalized COMPLETE.  DEGRADED/OPEN/EXPIRED/DELETED sessions
# are not scored; a scored session that later leaves this set (a late event
# degraded it, or it was deleted) has its derived row pruned so the sidecar
# always equals a fresh rebuild at steady state.
SCORABLE_SESSION_STATES = ("COMPLETE", "QUIESCENT")

_EVIDENCE_SCHEMA_VERSION = "1"
_HOP_EVENT_TYPES = ("model.operation", "tool.result")
# Timing-span provenance label: the value was computed from two evidence event
# timestamps, not reported by any vendor field.
_DERIVED_AUTHORITY = "derived:event_timestamps"


class ConsoleWorkerError(ValueError):
    """The evidence database cannot be scored safely."""


def default_console_path(production_db: str) -> str:
    """The sidecar's canonical home: ``console.sqlite3`` beside the evidence db."""

    resolved = os.path.abspath(os.path.expanduser(production_db))
    return os.path.join(os.path.dirname(resolved) or ".", "console.sqlite3")


# ---------------------------------------------------------------------------
# read-only evidence access
# ---------------------------------------------------------------------------

_WRITE_ACTIONS = frozenset(
    action
    for action in (
        getattr(sqlite3, name, None)
        for name in (
            "SQLITE_INSERT",
            "SQLITE_UPDATE",
            "SQLITE_DELETE",
            "SQLITE_CREATE_INDEX",
            "SQLITE_CREATE_TABLE",
            "SQLITE_CREATE_TEMP_INDEX",
            "SQLITE_CREATE_TEMP_TABLE",
            "SQLITE_CREATE_TEMP_TRIGGER",
            "SQLITE_CREATE_TEMP_VIEW",
            "SQLITE_CREATE_TRIGGER",
            "SQLITE_CREATE_VIEW",
            "SQLITE_CREATE_VTABLE",
            "SQLITE_DROP_INDEX",
            "SQLITE_DROP_TABLE",
            "SQLITE_DROP_TEMP_INDEX",
            "SQLITE_DROP_TEMP_TABLE",
            "SQLITE_DROP_TEMP_TRIGGER",
            "SQLITE_DROP_TEMP_VIEW",
            "SQLITE_DROP_TRIGGER",
            "SQLITE_DROP_VIEW",
            "SQLITE_DROP_VTABLE",
            "SQLITE_ALTER_TABLE",
            "SQLITE_REINDEX",
            "SQLITE_ANALYZE",
            "SQLITE_ATTACH",
            "SQLITE_DETACH",
        )
    )
    if action is not None
)


def _deny_writes_authorizer(
    action: int,
    argument_1: Any,
    argument_2: Any,
    database_name: Any,
    trigger_name: Any,
) -> int:
    del argument_1, argument_2, database_name, trigger_name
    if action in _WRITE_ACTIONS:
        return sqlite3.SQLITE_DENY
    return sqlite3.SQLITE_OK


def _open_evidence_ro(path: str) -> sqlite3.Connection:
    """Open the evidence database read-only (mode=ro + query_only + authorizer)."""

    if not isinstance(path, str) or not path.strip() or "\x00" in path:
        raise ConsoleWorkerError("production database path must be a non-empty path")
    resolved = os.path.abspath(os.path.expanduser(path))
    try:
        before = os.lstat(resolved)
    except OSError as exc:
        raise ConsoleWorkerError(
            f"production database is not readable: {resolved!r} ({exc})"
        ) from exc
    if not stat.S_ISREG(before.st_mode):
        raise ConsoleWorkerError(
            f"production database must be a regular file: {resolved!r}"
        )
    uri = Path(resolved).as_uri() + "?mode=ro"
    try:
        db = sqlite3.connect(uri, uri=True, timeout=5, isolation_level=None)
    except sqlite3.Error as exc:
        raise ConsoleWorkerError(
            f"could not open production database read-only: {exc}"
        ) from exc
    db.row_factory = sqlite3.Row
    try:
        after = os.lstat(resolved)
        if (
            not stat.S_ISREG(after.st_mode)
            or (before.st_dev, before.st_ino) != (after.st_dev, after.st_ino)
        ):
            raise ConsoleWorkerError(
                "production database changed while SQLite was opening it"
            )
        db.execute("PRAGMA query_only=ON")
        db.execute("PRAGMA busy_timeout=5000")
        db.set_authorizer(_deny_writes_authorizer)
        _verify_evidence_schema(db)
    except BaseException:
        db.close()
        raise
    return db


def _verify_evidence_schema(db: sqlite3.Connection) -> None:
    try:
        row = db.execute(
            "SELECT value FROM metadata WHERE key='production_schema_version'"
        ).fetchone()
    except sqlite3.Error as exc:
        raise ConsoleWorkerError(
            "selected database is not a hotato production evidence store"
        ) from exc
    if row is None or row[0] != _EVIDENCE_SCHEMA_VERSION:
        observed = None if row is None else row[0]
        raise ConsoleWorkerError(
            "unsupported production database schema version: " + repr(observed)
        )
    required = {
        "sessions": {"subject", "state", "evidence_json", "event_count"},
        "events": {
            "subject",
            "source",
            "event_id",
            "type",
            "source_time",
            "received",
            "stored_sha256",
            "payload_json",
        },
    }
    for table, expected in required.items():
        observed_columns = {
            item[1] for item in db.execute(f"PRAGMA table_info({table})")
        }
        missing = sorted(expected - observed_columns)
        if missing:
            raise ConsoleWorkerError(
                f"incompatible production database table {table!r}; missing "
                + ", ".join(missing)
            )


# ---------------------------------------------------------------------------
# deterministic scoring of one session
# ---------------------------------------------------------------------------


def scorer_provenance() -> Tuple[str, str, Dict[str, Any]]:
    """``(scorer_version, config_sha256, config)`` for every record written.

    The config snapshot is the scorer's full self-describing threshold block
    (the same one ``dump-frames`` embeds) plus the scan-walk parameter, hashed
    canonically, so a verdict row always says exactly which scorer produced it.
    """

    from . import __version__

    config = dict(_config_block(ScoreConfig()))
    config["min_gap_sec"] = DEFAULT_MIN_GAP_SEC
    config["scorer"] = "hotato.scan"
    return __version__, _sha(_canonical(config)), config


def _evidence_sha256(db: sqlite3.Connection, subject: str) -> Tuple[str, int]:
    """The session's stored-event-log digest (the manifest's algorithm)."""

    digests = [
        row[0]
        for row in db.execute(
            "SELECT stored_sha256 FROM events WHERE subject=? "
            "ORDER BY received,source,event_id",
            (subject,),
        )
    ]
    return _sha(_canonical(digests)), len(digests)


def _session_events(db: sqlite3.Connection, subject: str) -> List[sqlite3.Row]:
    """One session's events in evidence-time order (deterministic: the stored
    ``source_time`` string, then source, then event id -- no arrival clock)."""

    return db.execute(
        "SELECT type,source,event_id,source_time,sequence,payload_json "
        "FROM events WHERE subject=? ORDER BY source_time,source,event_id",
        (subject,),
    ).fetchall()


def _payload(row: sqlite3.Row) -> Dict[str, Any]:
    try:
        value = json.loads(row["payload_json"])
    except (TypeError, ValueError):
        return {}
    return value if isinstance(value, dict) else {}


def _number(value: Any) -> Optional[float]:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return float(value)


def _elapsed_ms(start: str, end: str) -> Optional[float]:
    try:
        delta = _parse_rfc3339(end) - _parse_rfc3339(start)
    except (TypeError, ValueError):
        return None
    return round(delta.total_seconds() * 1000.0, 3)


def _timing(events: List[sqlite3.Row]) -> Tuple[Dict[str, Any], List[Dict[str, Any]]]:
    """Timing spans + per-hop latency, derived purely from evidence.

    Per-hop rows carry the reporting event's declared ``authority`` kind (so a
    vendor-reported latency never masquerades as a measurement); turn spans and
    the end-to-end figure are computed from event timestamps and labeled
    ``derived:event_timestamps``.
    """

    hops: List[Dict[str, Any]] = []
    spans: List[Dict[str, Any]] = []
    open_turns: List[Tuple[str, str]] = []
    session_started: Optional[str] = None
    session_ended: Optional[str] = None
    started_count = 0
    ended_count = 0
    for row in events:
        payload = _payload(row)
        data = payload.get("data") if isinstance(payload.get("data"), dict) else {}
        authority = payload.get("authority")
        authority_kind = (
            authority.get("kind") if isinstance(authority, dict) else None
        ) or "submitted"
        event_type = row["type"]
        if event_type == "session.started":
            started_count += 1
            session_started = row["source_time"] if started_count == 1 else None
        elif event_type == "session.ended":
            ended_count += 1
            session_ended = row["source_time"] if ended_count == 1 else None
        elif event_type in _HOP_EVENT_TYPES:
            latency = _number(data.get("latency_ms"))
            if latency is None:
                latency = _number(data.get("duration_ms"))
            hops.append(
                {
                    "kind": event_type,
                    "at": row["source_time"],
                    "latency_ms": latency,
                    "authority": authority_kind,
                    "source": row["source"],
                    "event_id": row["event_id"],
                }
            )
        elif event_type == "turn.started":
            open_turns.append((row["source_time"], row["event_id"]))
        elif event_type == "turn.ended":
            start = open_turns.pop() if open_turns else None
            span: Dict[str, Any] = {
                "started_at": start[0] if start else None,
                "ended_at": row["source_time"],
                "duration_ms": (
                    _elapsed_ms(start[0], row["source_time"]) if start else None
                ),
                "authority": _DERIVED_AUTHORITY,
                "started_event_id": start[1] if start else None,
                "ended_event_id": row["event_id"],
            }
            for key in ("yield_latency_ms", "overlap_ms", "duration_ms"):
                reported = _number(data.get(key))
                if reported is not None:
                    span.setdefault("reported", {})[key] = reported
            spans.append(span)
    for span in spans:
        if span["duration_ms"] is not None:
            hops.append(
                {
                    "kind": "turn",
                    "at": span["ended_at"],
                    "latency_ms": span["duration_ms"],
                    "authority": _DERIVED_AUTHORITY,
                    "source": "console",
                    "event_id": span["ended_event_id"],
                }
            )
    end_to_end_ms = (
        _elapsed_ms(session_started, session_ended)
        if session_started and session_ended
        else None
    )
    timing = {
        "provenance": (
            "derived from evidence event timestamps; per-hop rows carry the "
            "reporting event's declared authority"
        ),
        "end_to_end_ms": end_to_end_ms,
        "turn_count": len(spans),
        "turn_spans": spans,
        "hop_count": len(hops),
    }
    return timing, hops


_MAGNITUDE_KEYS = ("overlap_sec", "gap_sec", "trailing_silence_sec", "activity_sec")


def _candidate_magnitude(candidate: Dict[str, Any]) -> Optional[float]:
    durations = candidate.get("durations") or {}
    for key in _MAGNITUDE_KEYS:
        value = _number(durations.get(key))
        if value is not None:
            return value
    return None


def _dimensions(candidates: List[Dict[str, Any]]) -> Dict[str, Any]:
    """Per-dimension observations: one entry per scan kind, counts plus the
    worst measured magnitude in seconds.  Never blended into one score."""

    output: Dict[str, Any] = {}
    for kind in KINDS:
        matching = [c for c in candidates if c.get("kind") == kind]
        magnitudes = [
            m for m in (_candidate_magnitude(c) for c in matching) if m is not None
        ]
        output[kind] = {
            "candidate_count": len(matching),
            "worst_sec": max(magnitudes) if magnitudes else None,
        }
    return output


def _audio_path(events: List[sqlite3.Row]) -> Optional[str]:
    """The first recorded local audio path, in evidence-time order.

    ``media.asset.available`` events may carry a ``data.path`` string naming
    the local recording.  Default-deny redaction reduces that field to a
    descriptor, so a redacted store yields no path here -- which the caller
    records as the scorer's NOT SCORABLE refusal, never a guess.
    """

    for row in events:
        if row["type"] != "media.asset.available":
            continue
        payload = _payload(row)
        data = payload.get("data") if isinstance(payload.get("data"), dict) else {}
        path = data.get("path")
        if isinstance(path, str) and path and "\x00" not in path:
            return path
    return None


def score_session(
    db: sqlite3.Connection,
    subject: str,
    *,
    session_state: str,
    evidence_sha256: str,
    event_count: int,
    scorer_version: str,
    config_sha256: str,
    config: Dict[str, Any],
) -> Dict[str, Any]:
    """Score one session's evidence into a full sidecar record.

    A scorer refusal (no/unavailable audio lane, no recorded path, missing or
    mono or unreadable recording) returns a NOT_SCORABLE record carrying the
    refusal reason.  Only unexpected exceptions propagate (the worker turns
    them into an ERROR record).
    """

    session = db.execute(
        "SELECT evidence_json FROM sessions WHERE subject=?", (subject,)
    ).fetchone()
    if session is None:
        raise KeyError(subject)
    try:
        evidence = json.loads(session["evidence_json"])
    except (TypeError, ValueError):
        evidence = {}
    events = _session_events(db, subject)
    timing, hops = _timing(events)

    record: Dict[str, Any] = {
        "subject": subject,
        "state": "NOT_SCORABLE",
        "reason": None,
        "session_state": session_state,
        "evidence_sha256": evidence_sha256,
        "event_count": event_count,
        "scorer_version": scorer_version,
        "config_sha256": config_sha256,
        "config": config,
        "dimensions": {},
        "candidates": [],
        "timing": timing,
        "audio": None,
        "hops": hops,
    }

    lane = evidence.get("participant_audio") or {}
    availability = lane.get("availability", "missing")
    if availability != "available":
        record["reason"] = (
            f"participant_audio evidence lane is {availability}; scoring reads "
            "a recorded two-channel call"
        )
        return record
    path = _audio_path(events)
    if path is None:
        record["reason"] = (
            "participant_audio evidence carries no local audio path; scoring "
            "reads the recording named by media.asset.available data.path"
        )
        return record
    try:
        scan = scan_recording(path)
    except (ValueError, OSError) as exc:
        # The scorer's own refusal (mono/one-channel, truncated, corrupt, or
        # unreadable recording): a first-class NOT_SCORABLE with its reason.
        record["reason"] = str(exc)
        return record
    candidates = scan["candidates"]
    record["state"] = "SCORED"
    record["reason"] = (
        candidate_plain_english(candidates[0]) if candidates else None
    )
    record["dimensions"] = _dimensions(candidates)
    record["candidates"] = candidates
    record["audio"] = {
        "path": path,
        "source": scan["source"],
        "duration_sec": scan["duration_sec"],
        "sample_rate": scan["sample_rate"],
        "total_candidates": scan["total_candidates"],
    }
    return record


# ---------------------------------------------------------------------------
# the supervisor-pattern worker
# ---------------------------------------------------------------------------


class ConsoleScoreWorker:
    """Run one serialized score-on-arrival cycle at a bounded fixed interval."""

    def __init__(
        self,
        production_db: str,
        store: ConsoleStore,
        *,
        interval_seconds: float = 5.0,
        clock=time.time,
        autostart: bool = True,
    ) -> None:
        if (
            isinstance(interval_seconds, bool)
            or not isinstance(interval_seconds, (int, float))
            or not 0.1 <= float(interval_seconds) <= 86_400
        ):
            raise ValueError("interval_seconds must be in [0.1, 86400]")
        self.production_db = os.path.abspath(os.path.expanduser(production_db))
        self.store = store
        self.interval_seconds = float(interval_seconds)
        self.clock = clock
        self._stop = threading.Event()
        self._cycle_lock = threading.Lock()
        self._state_lock = threading.Lock()
        self._last: Dict[str, Any] = {
            "schema": "hotato.console-score-status.v1",
            "state": "STARTING" if autostart else "IDLE",
            "cycles": 0,
            "last_started_at": None,
            "last_completed_at": None,
            "last_error": None,
            "last_result": None,
        }
        self.thread = threading.Thread(
            target=self._loop,
            name="hotato-console-score",
            daemon=True,
        )
        if autostart:
            self.thread.start()

    def run_once(self) -> Dict[str, Any]:
        if not self._cycle_lock.acquire(blocking=False):
            raise RuntimeError("a console scoring cycle is already running")
        started = float(self.clock())
        with self._state_lock:
            self._last["state"] = "RUNNING"
            self._last["last_started_at"] = started
        try:
            db = _open_evidence_ro(self.production_db)
            try:
                result = self._cycle(db)
            finally:
                db.close()
            result.update(
                {
                    "schema": "hotato.console-score-cycle.v1",
                    "started_at": started,
                    "completed_at": float(self.clock()),
                }
            )
            with self._state_lock:
                self._last.update(
                    {
                        "state": "IDLE",
                        "cycles": int(self._last["cycles"]) + 1,
                        "last_completed_at": result["completed_at"],
                        "last_error": None,
                        "last_result": result,
                    }
                )
            return result
        except Exception as exc:
            with self._state_lock:
                self._last.update(
                    {
                        "state": "ERROR",
                        "cycles": int(self._last["cycles"]) + 1,
                        "last_completed_at": float(self.clock()),
                        "last_error": {
                            "type": type(exc).__name__,
                            "message": str(exc)[:1000],
                        },
                    }
                )
            raise
        finally:
            self._cycle_lock.release()

    def _cycle(self, db: sqlite3.Connection) -> Dict[str, Any]:
        version, config_sha, config = scorer_provenance()
        counts = {
            "scored": 0,
            "not_scorable": 0,
            "errors": 0,
            "skipped": 0,
            "persist_failures": 0,
            "pruned": 0,
        }
        placeholders = ",".join("?" for _ in SCORABLE_SESSION_STATES)
        after = ""
        while not self._stop.is_set():
            row = db.execute(
                f"SELECT subject,state FROM sessions WHERE state IN ({placeholders}) "
                "AND subject>? ORDER BY subject LIMIT 1",
                (*SCORABLE_SESSION_STATES, after),
            ).fetchone()
            if row is None:
                break
            after = row["subject"]
            self._score_one(db, row["subject"], row["state"], counts,
                            version, config_sha, config)
        counts["pruned"] = self._prune(db)
        return counts

    def _score_one(
        self,
        db: sqlite3.Connection,
        subject: str,
        session_state: str,
        counts: Dict[str, int],
        version: str,
        config_sha: str,
        config: Dict[str, Any],
    ) -> None:
        sha, event_count = _evidence_sha256(db, subject)
        existing = self.store.score_identity(subject)
        # An ERROR row is never settled: it is retried every cycle until it
        # either scores or keeps its (deterministic) error reason.
        if existing is not None and existing[1] == sha and existing[0] != "ERROR":
            counts["skipped"] += 1
            return
        try:
            record = score_session(
                db,
                subject,
                session_state=session_state,
                evidence_sha256=sha,
                event_count=event_count,
                scorer_version=version,
                config_sha256=config_sha,
                config=config,
            )
        except Exception as exc:
            # A scorer crash on one session must not kill the loop or block
            # other sessions: it becomes its own visible ERROR record.
            record = {
                "subject": subject,
                "state": "ERROR",
                "reason": f"{type(exc).__name__}: {exc}",
                "session_state": session_state,
                "evidence_sha256": sha,
                "event_count": event_count,
                "scorer_version": version,
                "config_sha256": config_sha,
                "config": config,
                "dimensions": {},
                "candidates": [],
                "timing": {},
                "audio": None,
                "hops": [],
            }
        try:
            self.store.upsert_score(record)
        except Exception as exc:
            # I1: a persist failure is never a silent skip and never a claimed
            # score.  Surface it through the minimal error write path; if even
            # that fails, the counter still records it and the next cycle
            # retries the session.
            counts["persist_failures"] += 1
            try:
                self.store.record_error(
                    subject,
                    reason=f"persist_failure: {type(exc).__name__}: {exc}",
                    session_state=session_state,
                    evidence_sha256=sha,
                    event_count=event_count,
                    scorer_version=version,
                    config_sha256=config_sha,
                )
            except Exception:
                pass
            counts["errors"] += 1
            return
        if record["state"] == "SCORED":
            counts["scored"] += 1
        elif record["state"] == "NOT_SCORABLE":
            counts["not_scorable"] += 1
        else:
            counts["errors"] += 1

    def _prune(self, db: sqlite3.Connection) -> int:
        """Drop derived rows whose session left the scorable set (deleted, or
        degraded by a late event), so the sidecar equals a fresh rebuild."""

        placeholders = ",".join("?" for _ in SCORABLE_SESSION_STATES)
        pruned = 0
        after = ""
        while True:
            subject = self.store.next_subject(after)
            if subject is None:
                break
            after = subject
            row = db.execute(
                f"SELECT 1 FROM sessions WHERE subject=? AND state IN ({placeholders})",
                (subject, *SCORABLE_SESSION_STATES),
            ).fetchone()
            if row is None:
                self.store.delete_score(subject)
                pruned += 1
        return pruned

    def status(self) -> Dict[str, Any]:
        with self._state_lock:
            return json.loads(json.dumps(self._last, allow_nan=False))

    def _loop(self) -> None:
        while not self._stop.is_set():
            try:
                self.run_once()
            except Exception:
                # Status retains the bounded error; the next interval retries
                # because a transient SQLite/disk failure must not silently
                # kill scoring.
                pass
            self._stop.wait(self.interval_seconds)

    def close(self, timeout: float = 10.0) -> None:
        self._stop.set()
        if self.thread.is_alive():
            self.thread.join(timeout=timeout)
        if self.thread.is_alive():
            raise RuntimeError("console scoring thread did not stop")
        with self._state_lock:
            if self._last["state"] not in {"ERROR", "RUNNING"}:
                self._last["state"] = "STOPPED"


# ---------------------------------------------------------------------------
# deterministic rebuild
# ---------------------------------------------------------------------------


def rebuild_sidecar(production_db: str, store: ConsoleStore) -> Dict[str, Any]:
    """Regenerate the ENTIRE sidecar from the evidence database.

    Starts from empty and scores every scorable session in canonical (subject-
    ascending) order, one at a time.  The same evidence database always
    produces the same canonical dump (no wall-clock value enters any scored
    field), which is the migration story: a sidecar schema change ships as
    this rebuild.
    """

    version, config_sha, config = scorer_provenance()
    db = _open_evidence_ro(production_db)
    try:
        store.clear()
        counts = {"scored": 0, "not_scorable": 0, "errors": 0}
        placeholders = ",".join("?" for _ in SCORABLE_SESSION_STATES)
        after = ""
        while True:
            row = db.execute(
                f"SELECT subject,state FROM sessions WHERE state IN ({placeholders}) "
                "AND subject>? ORDER BY subject LIMIT 1",
                (*SCORABLE_SESSION_STATES, after),
            ).fetchone()
            if row is None:
                break
            after = row["subject"]
            sha, event_count = _evidence_sha256(db, row["subject"])
            try:
                record = score_session(
                    db,
                    row["subject"],
                    session_state=row["state"],
                    evidence_sha256=sha,
                    event_count=event_count,
                    scorer_version=version,
                    config_sha256=config_sha,
                    config=config,
                )
            except Exception as exc:
                record = {
                    "subject": row["subject"],
                    "state": "ERROR",
                    "reason": f"{type(exc).__name__}: {exc}",
                    "session_state": row["state"],
                    "evidence_sha256": sha,
                    "event_count": event_count,
                    "scorer_version": version,
                    "config_sha256": config_sha,
                    "config": config,
                    "dimensions": {},
                    "candidates": [],
                    "timing": {},
                    "audio": None,
                    "hops": [],
                }
            store.upsert_score(record)
            counts[
                {"SCORED": "scored", "NOT_SCORABLE": "not_scorable", "ERROR": "errors"}[
                    record["state"]
                ]
            ] += 1
    finally:
        db.close()
    return {
        "schema": "hotato.console-rebuild.v1",
        "sidecar": store.path,
        "scored": counts["scored"],
        "not_scorable": counts["not_scorable"],
        "errors": counts["errors"],
    }


def run_rebuild(
    production_db: str, *, console_db: Optional[str] = None
) -> Dict[str, Any]:
    """CLI entry: rebuild the sidecar beside the evidence db and return the
    summary.  Raises ``ValueError``/``OSError`` (HANDLED -> exit 2) on an
    unusable evidence database or sidecar path."""

    sidecar_path = console_db or default_console_path(production_db)
    store = ConsoleStore(sidecar_path)
    try:
        return rebuild_sidecar(production_db, store)
    finally:
        store.close()
