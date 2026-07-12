"""``hotato.state_adapter``: the post-call STATE ADAPTER (Authority 2).

The ``state`` / ``state_change`` assertion kinds (see :mod:`hotato.assert_`)
never trust the agent's spoken claim ("I issued the refund"). They query a
post-call SYSTEM OF RECORD through this tiny, pluggable interface and compare
the ACTUAL state -- so an agent that says a thing happened but did not do it
fails the assertion. There is no model/LLM path here: a query is a plain
lookup + dict comparison, deterministic and offline.

Interface -- one method:

    ``query(resource, **filters) -> dict | None``

Return the first record of ``resource`` whose fields match every ``filters``
key==value, or ``None`` when no such record exists. A reserved ``when`` filter
selects a named SNAPSHOT (``"before"`` / ``"after"``, default ``"after"``) so
``state_change`` can read a before/after delta from the same interface.

Ships with :class:`MockStateAdapter`, backed by a local JSON or SQLite test
sandbox, for deterministic OFFLINE testing -- no network, byte-stable. Real
adapters (a customer's HTTP API or database) are opt-in and, when they touch
the network, carry an EGRESS / THREAT-MODEL row (Phase-1 design C); none is
built here. The post-call query runs AFTER scoring and is folded into the
conversation artifact's evaluations, never into the timing verdict.
"""

from __future__ import annotations

import json
from typing import Any, Dict, List, Optional

from .errors import open_regular as _open_regular

__all__ = ["StateAdapter", "MockStateAdapter"]


class StateAdapter:
    """The pluggable post-call state interface. A concrete adapter implements
    :meth:`query`; the ``state``/``state_change`` kinds depend only on this
    contract, so a customer adapter (HTTP/DB) drops in without touching the
    assertion engine."""

    def query(self, resource: str, **filters: Any) -> Optional[Dict[str, Any]]:
        raise NotImplementedError


class MockStateAdapter(StateAdapter):
    """A deterministic, offline state adapter backed by an in-memory sandbox.

    The sandbox is a mapping ``{resource: rows}`` where ``rows`` is either:

    * a list of record dicts (a single, post-call snapshot), or
    * a single record dict (sugar for a one-row list), or
    * a snapshotted mapping ``{"before": [...], "after": [...]}`` -- the shape
      ``state_change`` reads for a delta (``query(..., when="before")`` /
      ``query(..., when="after")``). A ``when`` for which no snapshot exists
      returns ``None`` (the assertion reports INCONCLUSIVE, never a guess).

    ``query`` pops the reserved ``when`` key, resolves the resource's rows for
    that snapshot, then returns the first row matching every remaining filter,
    or ``None``. A returned dict is a shallow COPY, so an assertion can never
    mutate the sandbox.
    """

    def __init__(self, data: Dict[str, Any]):
        if not isinstance(data, dict):
            raise ValueError(
                "MockStateAdapter data must be a mapping of {resource: rows}"
            )
        self._data = data

    @classmethod
    def from_json_file(cls, path: str) -> "MockStateAdapter":
        """Load a sandbox from a JSON file (``{resource: rows}``). A FIFO/named
        pipe path raises immediately (via :func:`hotato.errors.open_regular`)
        instead of blocking forever; malformed JSON raises ``ValueError``."""
        with _open_regular(path, "r", encoding="utf-8") as fh:
            try:
                data = json.load(fh)
            except json.JSONDecodeError as exc:
                raise ValueError(f"{path!r} is not a valid state sandbox JSON: {exc}") from exc
        return cls(data)

    @classmethod
    def from_sqlite_file(cls, path: str) -> "MockStateAdapter":
        """Build a sandbox from a local SQLite file: each table becomes a
        resource whose rows are its records. A table named ``<name>__before`` /
        ``<name>__after`` is folded into a snapshotted ``<name>`` resource
        (the before/after shape ``state_change`` reads). Read-only; the DB is
        never written."""
        import sqlite3

        data: Dict[str, Any] = {}
        conn = sqlite3.connect(path)
        try:
            conn.row_factory = sqlite3.Row
            tables = [
                r[0]
                for r in conn.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'"
                )
            ]
            for t in tables:
                # Table names come from the DB's own catalogue; quote defensively.
                rows = [
                    dict(r)
                    for r in conn.execute('SELECT * FROM "%s"' % t.replace('"', '""'))
                ]
                if t.endswith("__before"):
                    data.setdefault(t[: -len("__before")], {})["before"] = rows
                elif t.endswith("__after"):
                    data.setdefault(t[: -len("__after")], {})["after"] = rows
                else:
                    data[t] = rows
        finally:
            conn.close()
        return cls(data)

    def _rows(self, resource: str, when: Optional[str]) -> Optional[List[Dict[str, Any]]]:
        val = self._data.get(resource)
        if val is None:
            return None
        if isinstance(val, dict) and ("before" in val or "after" in val):
            rows = val.get(when or "after")
            if rows is None:
                return None
            return rows if isinstance(rows, list) else [rows]
        if isinstance(val, dict):
            return [val]
        if isinstance(val, list):
            return val
        return None

    def query(self, resource: str, **filters: Any) -> Optional[Dict[str, Any]]:
        when = filters.pop("when", None)
        rows = self._rows(resource, when)
        if rows is None:
            return None
        for row in rows:
            if isinstance(row, dict) and all(
                k in row and row[k] == v for k, v in filters.items()
            ):
                return dict(row)
        return None
