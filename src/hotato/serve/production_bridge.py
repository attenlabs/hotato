"""Read-only projection of the separate production-evidence SQLite store.

The team workspace and the production evidence plane intentionally have
different storage authorities.  The fleet registry owns curated workspace
objects; ``hotato.production.sqlite3`` owns received production observations.
This adapter joins neither database and copies no rows.  It opens the explicitly
selected production database with SQLite ``mode=ro`` for each snapshot and
projects only session/alert metadata plus evidence-lane authority.  Event payload
JSON is never selected.

Keeping this boundary in a small module makes the read path auditable and avoids
constructing :class:`hotato.production.ProductionStore`, whose constructor is a
writer (it creates/migrates schema and enables WAL).
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import sqlite3
import stat
from collections import Counter
from pathlib import Path
from typing import Any, Dict, List

from ..production import EVIDENCE_LANES

__all__ = ["ProductionBridgeError", "read_production_snapshot"]

_SCHEMA_VERSION = "1"
_DEFAULT_SESSION_LIMIT = 200
_DEFAULT_ALERT_LIMIT = 500
_EVENT_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/-]{0,499}$")
_AUTHORITIES = frozenset(
    {"submitted", "adapter_reported", "provider_export", "signed_attestation", "measured"}
)

_WRITE_ACTIONS = frozenset(
    action
    for action in (
        getattr(sqlite3, "SQLITE_INSERT", None),
        getattr(sqlite3, "SQLITE_UPDATE", None),
        getattr(sqlite3, "SQLITE_DELETE", None),
        getattr(sqlite3, "SQLITE_CREATE_INDEX", None),
        getattr(sqlite3, "SQLITE_CREATE_TABLE", None),
        getattr(sqlite3, "SQLITE_CREATE_TEMP_INDEX", None),
        getattr(sqlite3, "SQLITE_CREATE_TEMP_TABLE", None),
        getattr(sqlite3, "SQLITE_CREATE_TEMP_TRIGGER", None),
        getattr(sqlite3, "SQLITE_CREATE_TEMP_VIEW", None),
        getattr(sqlite3, "SQLITE_CREATE_TRIGGER", None),
        getattr(sqlite3, "SQLITE_CREATE_VIEW", None),
        getattr(sqlite3, "SQLITE_CREATE_VTABLE", None),
        getattr(sqlite3, "SQLITE_DROP_INDEX", None),
        getattr(sqlite3, "SQLITE_DROP_TABLE", None),
        getattr(sqlite3, "SQLITE_DROP_TEMP_INDEX", None),
        getattr(sqlite3, "SQLITE_DROP_TEMP_TABLE", None),
        getattr(sqlite3, "SQLITE_DROP_TEMP_TRIGGER", None),
        getattr(sqlite3, "SQLITE_DROP_TEMP_VIEW", None),
        getattr(sqlite3, "SQLITE_DROP_TRIGGER", None),
        getattr(sqlite3, "SQLITE_DROP_VIEW", None),
        getattr(sqlite3, "SQLITE_DROP_VTABLE", None),
        getattr(sqlite3, "SQLITE_ALTER_TABLE", None),
        getattr(sqlite3, "SQLITE_REINDEX", None),
        getattr(sqlite3, "SQLITE_ANALYZE", None),
        getattr(sqlite3, "SQLITE_ATTACH", None),
        getattr(sqlite3, "SQLITE_DETACH", None),
    )
    if action is not None
)

_REQUIRED_COLUMNS = {
    "metadata": {"key", "value"},
    "sessions": {
        "subject",
        "state",
        "started",
        "ended",
        "last_event",
        "finalized_at",
        "event_count",
        "duplicate_count",
        "conflict_count",
        "out_of_order_count",
        "unsequenced_count",
        "highest_sequence",
        "evidence_json",
        "required_evidence_json",
        "finalization_reason",
    },
    "events": {"subject", "source", "type", "stored_sha256", "redacted"},
    "alerts": {
        "id",
        "subject",
        "rule_id",
        "state",
        "opened",
        "updated",
        "generation",
        "observed_json",
    },
}


class ProductionBridgeError(ValueError):
    """The selected production database cannot be projected safely."""


def _canonical(value: Any) -> bytes:
    return (
        json.dumps(
            value,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
            allow_nan=False,
        )
        + "\n"
    ).encode("utf-8")


def _sha(value: bytes) -> str:
    return "sha256:" + hashlib.sha256(value).hexdigest()


def _bounded_limit(value: int, label: str, maximum: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not 1 <= value <= maximum:
        raise ProductionBridgeError(f"{label} must be an integer from 1 to {maximum}")
    return value


def _resolved_regular_path(path: str) -> str:
    if not isinstance(path, str) or not path.strip() or "\x00" in path:
        raise ProductionBridgeError("production database path must be a non-empty path")
    resolved = os.path.abspath(os.path.expanduser(path))
    try:
        mode = os.lstat(resolved).st_mode
    except OSError as exc:
        raise ProductionBridgeError(
            f"production database is not readable: {resolved!r} ({exc})"
        ) from exc
    # Refuse a terminal symlink.  This is an operator-selected local path, but
    # resolving a link here would make the banner/JSON source identity differ
    # from the file SQLite actually opened.
    if not stat.S_ISREG(mode):
        raise ProductionBridgeError(
            f"production database must be a regular file, not a link/device: {resolved!r}"
        )
    return resolved


def _open_read_only(path: str) -> sqlite3.Connection:
    # ``Path.as_uri`` handles spaces, URI metacharacters, Windows drive letters,
    # and UNC paths without hand-built escaping.  mode=ro makes a missing path
    # fail instead of creating a database.
    try:
        before = os.lstat(path)
    except OSError as exc:
        raise ProductionBridgeError(
            f"production database is not readable: {path!r} ({exc})"
        ) from exc
    if not stat.S_ISREG(before.st_mode):
        raise ProductionBridgeError("production database must remain a regular file")
    uri = Path(path).as_uri() + "?mode=ro"
    try:
        db = sqlite3.connect(uri, uri=True, timeout=5, isolation_level=None)
    except sqlite3.Error as exc:
        raise ProductionBridgeError(
            f"could not open production database read-only: {exc}"
        ) from exc
    db.row_factory = sqlite3.Row
    try:
        after = os.lstat(path)
        if (
            not stat.S_ISREG(after.st_mode)
            or (before.st_dev, before.st_ino) != (after.st_dev, after.st_ino)
        ):
            raise ProductionBridgeError(
                "production database changed while SQLite was opening it"
            )
        db.execute("PRAGMA query_only=ON")
        db.execute("PRAGMA busy_timeout=5000")
        db.set_authorizer(_metadata_only_authorizer)
    except BaseException:
        db.close()
        raise
    return db


def _metadata_only_authorizer(
    action: int,
    argument_1: Any,
    argument_2: Any,
    database_name: Any,
    trigger_name: Any,
) -> int:
    """Second enforcement wall behind SQLite mode=ro/query_only.

    Any future bridge query that attempts to read the production payload column
    is refused by SQLite itself.  Mutating SQL is likewise denied even if a
    caller later weakens the URI flags by mistake.
    """

    del database_name, trigger_name
    if action in _WRITE_ACTIONS:
        return sqlite3.SQLITE_DENY
    if (
        action == sqlite3.SQLITE_READ
        and argument_1 == "events"
        and argument_2 == "payload_json"
    ):
        return sqlite3.SQLITE_DENY
    return sqlite3.SQLITE_OK


def _verify_schema(db: sqlite3.Connection) -> None:
    try:
        row = db.execute(
            "SELECT value FROM metadata WHERE key='production_schema_version'"
        ).fetchone()
    except sqlite3.Error as exc:
        raise ProductionBridgeError(
            "selected database is not a hotato production evidence store"
        ) from exc
    if row is None or row[0] != _SCHEMA_VERSION:
        observed = None if row is None else row[0]
        raise ProductionBridgeError(
            "unsupported production database schema version: " + repr(observed)
        )
    for table, required in _REQUIRED_COLUMNS.items():
        try:
            observed = {item[1] for item in db.execute(f"PRAGMA table_info({table})")}
        except sqlite3.Error as exc:
            raise ProductionBridgeError(
                f"could not inspect production table {table!r}: {exc}"
            ) from exc
        missing = sorted(required - observed)
        if missing:
            raise ProductionBridgeError(
                f"incompatible production database table {table!r}; missing "
                + ", ".join(missing)
            )


def _json_object(raw: str, label: str) -> Dict[str, Any]:
    try:
        value = json.loads(raw)
    except (TypeError, ValueError) as exc:
        raise ProductionBridgeError(f"invalid {label} JSON in production database") from exc
    if not isinstance(value, dict):
        raise ProductionBridgeError(f"{label} must be a JSON object")
    return value


def _required_lanes(raw: Any) -> List[str]:
    if raw is None:
        return list(EVIDENCE_LANES)
    try:
        value = json.loads(raw)
    except (TypeError, ValueError) as exc:
        raise ProductionBridgeError(
            "invalid required-evidence JSON in production database"
        ) from exc
    if (
        not isinstance(value, list)
        or not value
        or len(value) != len(set(value))
        or any(item not in EVIDENCE_LANES for item in value)
    ):
        raise ProductionBridgeError("production session has invalid required evidence lanes")
    return value


def _evidence(raw: str) -> Dict[str, Dict[str, Any]]:
    value = _json_object(raw, "session evidence")
    if set(value) != set(EVIDENCE_LANES):
        raise ProductionBridgeError(
            "production session evidence must contain exactly the declared evidence lanes"
        )
    output: Dict[str, Dict[str, Any]] = {}
    for lane in EVIDENCE_LANES:
        item = value[lane]
        if not isinstance(item, dict):
            raise ProductionBridgeError(f"production evidence lane {lane!r} is malformed")
        required = {
            "availability",
            "authority",
            "eligible_for_execution_claim",
            "event_ids",
        }
        if set(item) != required or not isinstance(item.get("event_ids"), list):
            raise ProductionBridgeError(f"production evidence lane {lane!r} is malformed")
        availability = item["availability"]
        authority = item["authority"]
        eligible = item["eligible_for_execution_claim"]
        event_ids = item["event_ids"]
        if availability not in {"missing", "available", "unavailable", "unsupported"}:
            raise ProductionBridgeError(
                f"production evidence lane {lane!r} has invalid availability"
            )
        if not isinstance(eligible, bool):
            raise ProductionBridgeError(
                f"production evidence lane {lane!r} has non-boolean eligibility"
            )
        invalid_event_ids = len(event_ids) > 100_000 or any(
            not isinstance(event_id, str) or not _EVENT_ID.fullmatch(event_id)
            for event_id in event_ids
        )
        if invalid_event_ids or len(event_ids) != len(set(event_ids)):
            raise ProductionBridgeError(
                f"production evidence lane {lane!r} has invalid event ids"
            )
        if availability == "available":
            if authority not in _AUTHORITIES or eligible is not (
                authority in {"measured", "signed_attestation"}
            ):
                raise ProductionBridgeError(
                    f"production evidence lane {lane!r} has contradictory authority"
                )
        elif authority != "unavailable" or eligible:
            raise ProductionBridgeError(
                f"production evidence lane {lane!r} has contradictory authority"
            )
        # Copy only the already-normalized manifest fields.  The event payload
        # column is deliberately absent from every query in this module.
        output[lane] = {
            "availability": availability,
            "authority": authority,
            "eligible_for_execution_claim": eligible,
            "event_ids": list(event_ids),
        }
    return output


def _session_projection(db: sqlite3.Connection, row: sqlite3.Row) -> Dict[str, Any]:
    subject = row["subject"]
    evidence = _evidence(row["evidence_json"])
    required = _required_lanes(row["required_evidence_json"])
    event_rows = db.execute(
        "SELECT source,type,stored_sha256,redacted FROM events "
        "WHERE subject=? ORDER BY received,source,event_id",
        (subject,),
    ).fetchall()
    if row["event_count"] != len(event_rows):
        raise ProductionBridgeError(
            "production session event count does not match stored events"
        )
    event_sources = sorted({item["source"] for item in event_rows})
    lifecycle = Counter(
        item["type"]
        for item in event_rows
        if item["type"] in ("session.started", "session.ended")
    )
    redacted_count = sum(int(item["redacted"]) for item in event_rows)
    if not event_rows or redacted_count == len(event_rows):
        payload_storage = "redacted"
    elif redacted_count == 0:
        payload_storage = "unredacted"
    else:
        payload_storage = "mixed"
    manifest = {
        "schema": "hotato.production-session.v1",
        "session_id": subject,
        "status": row["state"],
        "event_count": row["event_count"],
        "duplicate_count": row["duplicate_count"],
        "conflict_count": row["conflict_count"],
        "out_of_order_count": row["out_of_order_count"],
        "unsequenced_count": row["unsequenced_count"],
        "highest_sequence": row["highest_sequence"],
        "lifecycle": {
            "session_started_events": lifecycle.get("session.started", 0),
            "session_ended_events": lifecycle.get("session.ended", 0),
            "unambiguous": (
                lifecycle.get("session.started", 0) == 1
                and lifecycle.get("session.ended", 0) == 1
            ),
        },
        "evidence": evidence,
        "required_evidence_lanes": required,
        "payload_storage": payload_storage,
        "stored_event_log_sha256": _sha(
            _canonical([item["stored_sha256"] for item in event_rows])
        ),
        "finalized_at": row["finalized_at"],
        "finalization_reason": row["finalization_reason"],
    }
    missing = [
        lane
        for lane in required
        if evidence[lane]["availability"] != "available"
    ]
    return {
        "manifest": manifest,
        "event_sources": event_sources,
        "missing_required_lanes": missing,
        "last_event_at": row["last_event"],
    }


def _alert_projection(row: sqlite3.Row) -> Dict[str, Any]:
    observed = _json_object(row["observed_json"], "alert observation")
    # Alert observations are deliberately bounded by ProductionStore to the
    # condition name.  Refuse unexpected fields rather than surfacing an
    # accidental future payload through this metadata-only bridge.
    if set(observed) != {"condition"} or not isinstance(observed["condition"], str):
        raise ProductionBridgeError("production alert observation is not metadata-only")
    return {
        "alert_id": row["id"],
        "session_id": row["subject"],
        "rule_id": row["rule_id"],
        "state": row["state"],
        "opened_at": row["opened"],
        "updated_at": row["updated"],
        "generation": row["generation"],
        "condition": observed["condition"],
    }


def read_production_snapshot(
    path: str,
    *,
    session_limit: int = _DEFAULT_SESSION_LIMIT,
    alert_limit: int = _DEFAULT_ALERT_LIMIT,
) -> Dict[str, Any]:
    """Return a bounded metadata projection from an explicitly selected DB.

    The returned object stays separate from fleet counts and origin buckets.
    The production schema has no workspace id, so the bridge states that fact
    rather than assigning sessions to the workspace currently being served.
    """

    sessions_cap = _bounded_limit(session_limit, "session_limit", 10_000)
    alerts_cap = _bounded_limit(alert_limit, "alert_limit", 10_000)
    resolved = _resolved_regular_path(path)
    db = _open_read_only(resolved)
    try:
        # One explicit read transaction prevents a concurrent writer from
        # mixing counts, session rows, and per-session event digests from
        # different WAL snapshots.
        db.execute("BEGIN")
        _verify_schema(db)
        total_sessions = int(db.execute("SELECT COUNT(*) FROM sessions").fetchone()[0])
        total_alerts = int(db.execute("SELECT COUNT(*) FROM alerts").fetchone()[0])
        session_rows = db.execute(
            "SELECT subject,state,started,ended,last_event,finalized_at,event_count,"
            "duplicate_count,conflict_count,out_of_order_count,unsequenced_count,"
            "highest_sequence,evidence_json,required_evidence_json,finalization_reason "
            "FROM sessions WHERE state!='DELETED' "
            "ORDER BY last_event DESC,subject LIMIT ?",
            (sessions_cap,),
        ).fetchall()
        alert_rows = db.execute(
            "SELECT id,subject,rule_id,state,opened,updated,generation,observed_json "
            "FROM alerts ORDER BY (state='FIRING') DESC,updated DESC,id LIMIT ?",
            (alerts_cap,),
        ).fetchall()
        sessions = [_session_projection(db, row) for row in session_rows]
        alerts = [_alert_projection(row) for row in alert_rows]
        session_states = dict(
            sorted(
                (str(row[0]), int(row[1]))
                for row in db.execute(
                    "SELECT state,COUNT(*) FROM sessions GROUP BY state"
                )
            )
        )
        alert_states = dict(
            sorted(
                (str(row[0]), int(row[1]))
                for row in db.execute(
                    "SELECT state,COUNT(*) FROM alerts GROUP BY state"
                )
            )
        )
    except sqlite3.Error as exc:
        raise ProductionBridgeError(
            f"could not read production evidence metadata: {exc}"
        ) from exc
    finally:
        db.close()

    return {
        "schema": "hotato.production-workspace-bridge.v1",
        "source": {
            "kind": "hotato.production.sqlite3",
            "path": resolved,
            "schema_version": _SCHEMA_VERSION,
            "access": "sqlite-mode-ro",
            "workspace_scope": "not_encoded_by_production_schema",
            "payload_columns_read": False,
            "fleet_rows_written": False,
        },
        "summary": {
            "sessions_total": total_sessions,
            "sessions_returned": len(sessions),
            "sessions_truncated": total_sessions > len(sessions),
            "alerts_total": total_alerts,
            "alerts_returned": len(alerts),
            "alerts_truncated": total_alerts > len(alerts),
            "session_states": session_states,
            "alert_states": alert_states,
        },
        "sessions": sessions,
        "alerts": alerts,
    }
