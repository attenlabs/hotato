"""``hotato.state_adapter``: the post-call STATE ADAPTER (Authority 2).

Pins the mock adapter's contract -- the one method the ``state`` /
``state_change`` assertion kinds depend on:

* ``query(resource, **filters) -> dict | None`` returns the first matching
  record, or ``None`` when nothing matches (never a fabricated row);
* a reserved ``when`` filter selects the before/after SNAPSHOT for a delta;
* a returned dict is a COPY (an assertion can never mutate the sandbox);
* JSON and SQLite sandboxes load to the same shape and drive the ``state`` /
  ``state_change`` kinds identically -- deterministic, offline, no model.
"""

from __future__ import annotations

import json
import sqlite3

import pytest

from hotato import assert_ as A
from hotato.state_adapter import MockStateAdapter, StateAdapter


# --- the interface / query contract ----------------------------------------

def test_query_returns_first_matching_record():
    ad = MockStateAdapter({"orders": [
        {"id": "A1", "status": "open"},
        {"id": "A2", "status": "refunded"},
    ]})
    assert ad.query("orders", id="A2") == {"id": "A2", "status": "refunded"}


def test_query_returns_none_when_nothing_matches():
    ad = MockStateAdapter({"orders": [{"id": "A1"}]})
    assert ad.query("orders", id="ZZ") is None
    assert ad.query("nonexistent_resource") is None       # unknown resource -> None


def test_query_no_filters_returns_first_record():
    ad = MockStateAdapter({"orders": [{"id": "A1"}, {"id": "A2"}]})
    assert ad.query("orders") == {"id": "A1"}


def test_single_dict_record_is_sugar_for_one_row():
    ad = MockStateAdapter({"config": {"flag": True}})
    assert ad.query("config", flag=True) == {"flag": True}
    assert ad.query("config", flag=False) is None


def test_returned_record_is_a_copy_sandbox_is_immutable():
    ad = MockStateAdapter({"orders": [{"id": "A1", "status": "open"}]})
    rec = ad.query("orders", id="A1")
    rec["status"] = "TAMPERED"
    assert ad.query("orders", id="A1")["status"] == "open"   # sandbox untouched


def test_when_selects_before_after_snapshots():
    ad = MockStateAdapter({"account": {
        "before": [{"id": "u1", "balance": 100}],
        "after": [{"id": "u1", "balance": 0}],
    }})
    assert ad.query("account", when="before", id="u1")["balance"] == 100
    assert ad.query("account", when="after", id="u1")["balance"] == 0
    assert ad.query("account", id="u1")["balance"] == 0       # default snapshot = after


def test_missing_snapshot_returns_none():
    ad = MockStateAdapter({"account": {"after": [{"id": "u1"}]}})
    assert ad.query("account", when="before", id="u1") is None


def test_bad_sandbox_shape_is_a_usage_error():
    with pytest.raises(ValueError):
        MockStateAdapter(["not", "a", "mapping"])


def test_is_a_state_adapter():
    assert isinstance(MockStateAdapter({}), StateAdapter)


# --- JSON / SQLite loaders --------------------------------------------------

def test_from_json_file(tmp_path):
    p = tmp_path / "sandbox.json"
    p.write_text(json.dumps({"orders": [{"id": "A1", "status": "refunded"}]}),
                 encoding="utf-8")
    ad = MockStateAdapter.from_json_file(str(p))
    assert ad.query("orders", id="A1")["status"] == "refunded"


def test_from_json_file_rejects_malformed(tmp_path):
    p = tmp_path / "bad.json"
    p.write_text("{not json", encoding="utf-8")
    with pytest.raises(ValueError):
        MockStateAdapter.from_json_file(str(p))


def test_from_sqlite_file_tables_become_resources(tmp_path):
    db = tmp_path / "state.db"
    conn = sqlite3.connect(str(db))
    conn.execute("CREATE TABLE orders (id TEXT, status TEXT)")
    conn.execute("INSERT INTO orders VALUES ('A1', 'refunded')")
    # before/after snapshot tables fold into one snapshotted resource.
    conn.execute("CREATE TABLE account__before (id TEXT, balance INT)")
    conn.execute("INSERT INTO account__before VALUES ('u1', 100)")
    conn.execute("CREATE TABLE account__after (id TEXT, balance INT)")
    conn.execute("INSERT INTO account__after VALUES ('u1', 0)")
    conn.commit()
    conn.close()

    ad = MockStateAdapter.from_sqlite_file(str(db))
    assert ad.query("orders", id="A1")["status"] == "refunded"
    assert ad.query("account", when="before", id="u1")["balance"] == 100
    assert ad.query("account", when="after", id="u1")["balance"] == 0


# --- the adapter drives the state / state_change assertion kinds ------------

def test_mock_adapter_drives_state_kind_pass_and_fail():
    ad = MockStateAdapter({"orders": [{"id": "A1", "status": "refunded", "amount": 50}]})
    ctx = A.build_context(state_adapter=ad)
    ok = A.evaluate_assertion({"id": "a", "kind": "state", "resource": "orders",
                               "filters": {"id": "A1"},
                               "expect": {"status": "refunded", "amount": 50}}, ctx)
    assert ok["status"] == "PASS"
    bad = A.evaluate_assertion({"id": "a", "kind": "state", "resource": "orders",
                                "filters": {"id": "A1"},
                                "expect": {"status": "open"}}, ctx)
    assert bad["status"] == "FAIL"


def test_mock_adapter_drives_state_change_kind(tmp_path):
    # Same delta expressed through a SQLite sandbox, end to end.
    db = tmp_path / "s.db"
    conn = sqlite3.connect(str(db))
    conn.execute("CREATE TABLE account__before (id TEXT, balance INT)")
    conn.execute("INSERT INTO account__before VALUES ('u1', 100)")
    conn.execute("CREATE TABLE account__after (id TEXT, balance INT)")
    conn.execute("INSERT INTO account__after VALUES ('u1', 0)")
    conn.commit()
    conn.close()

    ctx = A.build_context(state_adapter=MockStateAdapter.from_sqlite_file(str(db)))
    r = A.evaluate_assertion({"id": "a", "kind": "state_change", "resource": "account",
                              "filters": {"id": "u1"}, "field": "balance",
                              "from": 100, "to": 0}, ctx)
    assert r["status"] == "PASS"
    assert r["delta"] == {"field": "balance", "before": 100, "after": 0}


def test_state_kinds_inconclusive_without_an_adapter():
    # No adapter at all -> INCONCLUSIVE (no way to query state), never a guess.
    ctx = A.build_context()
    for a in (
        {"id": "a", "kind": "state", "resource": "orders", "expect": {"x": 1}},
        {"id": "a", "kind": "state_change", "resource": "orders", "field": "x", "to": 1},
    ):
        assert A.evaluate_assertion(a, ctx)["status"] == "INCONCLUSIVE"
