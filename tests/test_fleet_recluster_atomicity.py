"""Regression: FleetAPI.recluster_agent must rewrite candidate rows atomically.

The Registry connection runs in autocommit (isolation_level=None) so that the
manual BEGIN IMMEDIATE in JobQueue.enqueue/claim is the sole, version-stable
transaction control. A side effect is that a bare loop of UPDATEs followed by a
trailing .commit() is NO LONGER atomic -- each UPDATE would commit on its own and
the trailing commit becomes a no-op. recluster_agent therefore wraps its
per-candidate rewrite in an explicit BEGIN IMMEDIATE..COMMIT; a failure part-way
through (e.g. `database is locked`) must roll the WHOLE batch back, never leave
some candidates reclustered and the rest stale.
"""
import json
import sqlite3

import pytest

import hotato.fleet.api as api_mod
from hotato.fleet.api import FleetAPI


def _seed(api, n=4):
    for i in range(n):
        api.registry.add_candidate(
            "ws1", f"c{i}", agent_id="a1",
            measured_json=json.dumps({"kind": "talkover", "components": {}}))


def _reclustered_count(api):
    rows = api.registry.list_candidates("ws1", agent_id="a1", limit=100)
    return sum(
        1 for r in rows
        if "recurrence" in (json.loads(r["measured_json"] or "{}").get("components") or {}))


def test_recluster_agent_is_atomic_on_midloop_failure(tmp_path, monkeypatch):
    with FleetAPI(str(tmp_path / "home")) as api:
        _seed(api, n=4)
        assert _reclustered_count(api) == 0

        # Fail on the 2nd per-candidate serialization inside the transaction,
        # standing in for a mid-batch `database is locked`. By then candidate #1's
        # UPDATE has already run inside the (uncommitted) transaction.
        real_dumps = json.dumps
        state = {"n": 0}

        def flaky_dumps(obj, *a, **k):
            if isinstance(obj, dict) and "components" in obj:
                state["n"] += 1
                if state["n"] == 2:
                    raise sqlite3.OperationalError("database is locked")
            return real_dumps(obj, *a, **k)

        monkeypatch.setattr(api_mod.json, "dumps", flaky_dumps)
        with pytest.raises(sqlite3.OperationalError):
            api.recluster_agent("ws1", "a1")
        monkeypatch.undo()

        # All-or-nothing: the batch rolled back, so NO candidate carries a
        # half-written recurrence component (pre-fix, candidate #1 was already
        # durably reclustered -- a partial write).
        assert _reclustered_count(api) == 0
        # No dangling open transaction left on the shared connection.
        assert api.registry.conn.in_transaction is False


def test_recluster_agent_commits_all_on_success(tmp_path):
    with FleetAPI(str(tmp_path / "home")) as api:
        _seed(api, n=4)
        out = api.recluster_agent("ws1", "a1")
        assert out["candidates"] == 4
        assert _reclustered_count(api) == 4
