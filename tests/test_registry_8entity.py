"""Phase-1 §F: the fleet registry migrated to the 8-entity conversation-QA model.

Proves the migration is ADDITIVE, IDEMPOTENT, and CONCURRENCY-SAFE:

* it runs on a FRESH db and on a db created by the PRIOR (v1) schema (backfill),
  and running it twice over either is a no-op (no error, no duplicate row);
* all 8 entity tables (agents, releases, suites, scenarios, runs, conversations,
  evaluations, reviews) + the assertion_runs index are present;
* two Registry() constructors racing on a fresh db both succeed (the same shape
  test_fleet_jobs_concurrency guards -- the enlarged v2 schema-init must not
  regress it);
* an assertion_run write + read round-trips, and re-writing the same logical
  work dedups to ONE row via the jobs.py idempotency-key pattern;
* nothing existing is dropped: a row written under the v1 shape survives the open.
"""
from __future__ import annotations

import json
import sqlite3
import threading

from hotato.fleet.registry import Registry, SCHEMA_VERSION


# The 8 entities of §F (Agent..Review) + the assertion_runs index.
_ENTITY_TABLES = ("agents", "releases", "suites", "scenarios", "runs",
                  "conversations", "evaluations", "reviews")
_ALL_NEW = _ENTITY_TABLES + ("assertion_runs",)


def _tables(conn) -> set:
    return {r["name"] for r in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table'")}


def _columns(conn, table) -> set:
    return {r["name"] for r in conn.execute(f"PRAGMA table_info({table})")}


# --- all tables present, schema marker at v2 -------------------------------

def test_fresh_db_has_all_8_entity_tables_and_assertion_runs(tmp_path):
    reg = Registry(home=str(tmp_path))
    tables = _tables(reg.conn)
    assert set(_ALL_NEW) <= tables, f"missing tables: {set(_ALL_NEW) - tables}"
    # Agent extended in place (§F): the two new columns exist on the existing table.
    assert {"current_release_id", "configuration_digest"} <= _columns(reg.conn, "agents")
    # the marker advanced to the new version
    row = reg._one("SELECT value FROM meta WHERE key='schema_version'")
    assert int(row["value"]) == SCHEMA_VERSION == 2
    reg.close()


def test_construction_is_idempotent_on_a_fresh_db(tmp_path):
    """Opening the same fresh store twice runs the whole (additive) migration
    again: no error, no duplicate tables, marker stable."""
    Registry(home=str(tmp_path)).close()
    reg = Registry(home=str(tmp_path))  # second open re-runs schema-init
    assert set(_ALL_NEW) <= _tables(reg.conn)
    # exactly ONE schema_version row (the INSERT OR IGNORE never duplicated it)
    n = reg._one("SELECT COUNT(*) c FROM meta WHERE key='schema_version'")["c"]
    assert n == 1
    assert int(reg._one("SELECT value FROM meta WHERE key='schema_version'")["value"]) == 2
    reg.close()


# --- backfill from a PRIOR (v1) schema -------------------------------------

def _make_v1_store(home) -> str:
    """Hand-build a v1-shaped fleet.db: the OLD agents table (no
    current_release_id / configuration_digest), NONE of the §F tables, and
    meta.schema_version='1' -- exactly what a pre-Phase-1 install left on disk."""
    home.mkdir()
    db = str(home / "fleet.db")
    con = sqlite3.connect(db)
    con.executescript("""
        CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT);
        CREATE TABLE workspaces (workspace_id TEXT PRIMARY KEY, name TEXT, created_at REAL);
        CREATE TABLE agents (
            workspace_id TEXT NOT NULL, agent_id TEXT NOT NULL, name TEXT,
            stack TEXT NOT NULL, connection_id TEXT, external_ref TEXT, created_at REAL,
            PRIMARY KEY (workspace_id, agent_id));
    """)
    con.execute("INSERT INTO meta (key, value) VALUES ('schema_version', '1')")
    con.execute("INSERT INTO workspaces (workspace_id, name, created_at) VALUES ('wsA','Acme',1.0)")
    con.execute("INSERT INTO agents (workspace_id, agent_id, name, stack, created_at) "
                "VALUES ('wsA','bot-a','Bot A','vapi',1.0)")
    con.commit()
    con.close()
    return db


def test_migration_backfills_a_prior_schema_db_without_dropping_data(tmp_path):
    home = tmp_path / "old"
    _make_v1_store(home)

    reg = Registry(home=str(home))
    # the additive column backfill landed on the existing agents table
    assert {"current_release_id", "configuration_digest"} <= _columns(reg.conn, "agents")
    # every new §F table now exists
    assert set(_ALL_NEW) <= _tables(reg.conn)
    # the pre-existing v1 rows survived (nothing dropped/rewritten)
    assert reg.get_workspace("wsA") is not None
    agent = reg._one("SELECT * FROM agents WHERE workspace_id=? AND agent_id=?", ("wsA", "bot-a"))
    assert agent["name"] == "Bot A" and agent["stack"] == "vapi"
    assert agent["current_release_id"] is None  # backfilled column defaults NULL
    # the marker advanced 1 -> 2
    assert int(reg._one("SELECT value FROM meta WHERE key='schema_version'")["value"]) == 2
    reg.close()


def test_migration_from_prior_schema_is_idempotent(tmp_path):
    """Opening the migrated v1 store AGAIN is a clean no-op: no error, the agent
    row is not duplicated, the marker stays at 2."""
    home = tmp_path / "old"
    _make_v1_store(home)
    Registry(home=str(home)).close()   # first open performs the migration
    reg = Registry(home=str(home))     # second open re-runs it harmlessly
    assert reg._one("SELECT COUNT(*) c FROM agents WHERE workspace_id='wsA'")["c"] == 1
    assert int(reg._one("SELECT value FROM meta WHERE key='schema_version'")["value"]) == 2
    # the new entity methods work against the upgraded store
    reg.set_agent_release("wsA", "bot-a", current_release_id="rel-1")
    assert reg._one("SELECT current_release_id FROM agents WHERE workspace_id='wsA' AND agent_id='bot-a'"
                    )["current_release_id"] == "rel-1"
    reg.close()


# --- concurrent construction on a fresh db (mirrors jobs concurrency) -------

def test_concurrent_registry_construction_on_fresh_db_both_succeed(tmp_path):
    """Two threads construct a Registry over the SAME fresh fleet.db at once.
    The enlarged v2 schema-init (more CREATE TABLE + _ensure_column writes) must
    still let both constructors win -- the exact race the __init__ retry loop and
    test_fleet_jobs_concurrency defend. daemon=True so a regression surfaces as a
    fast failure, never a hang."""
    home = str(tmp_path)
    errors = []
    barrier = threading.Barrier(2)

    def worker():
        try:
            barrier.wait()
            reg = Registry(home=home)
            # a real write after open, to force the write lock under contention
            reg.ensure_workspace("ws1")
            reg.close()
        except Exception as exc:  # the defect under guard
            errors.append(exc)

    threads = [threading.Thread(target=worker, daemon=True),
               threading.Thread(target=worker, daemon=True)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=10)

    assert not errors, f"concurrent Registry construction leaked: {errors!r}"
    reg = Registry(home=home)
    assert reg.get_workspace("ws1") is not None
    # exactly one schema_version row despite the race
    assert reg._one("SELECT COUNT(*) c FROM meta WHERE key='schema_version'")["c"] == 1
    reg.close()


# --- assertion_runs: round-trip + idempotent dedup -------------------------

def test_assertion_run_write_read_round_trips(tmp_path):
    reg = Registry(home=str(tmp_path))
    reg.ensure_workspace("wsA")
    res = reg.add_assertion_run(
        "wsA", assertion_id="a1", agent_id="bot-a", call_id="call-1",
        kind="tool_result", dimension="outcome", deterministic=True, status="PASS",
        reason=None, evidence_refs=json.dumps(["trace:sha256:abc"]),
        result_json=json.dumps({"id": "a1", "kind": "tool_result", "status": "PASS",
                                "deterministic": True}))
    assert res["deduped"] is False
    got = reg.get_assertion_run("wsA", res["assertion_run_id"])
    assert got is not None
    assert got["agent_id"] == "bot-a" and got["call_id"] == "call-1"
    assert got["kind"] == "tool_result" and got["dimension"] == "outcome"
    assert got["status"] == "PASS" and got["deterministic"] == 1
    assert json.loads(got["result_json"])["kind"] == "tool_result"
    # list surfaces it, filterable by the fleet-registered call/agent
    listed = reg.list_assertion_runs("wsA", agent_id="bot-a", call_id="call-1")
    assert [r["assertion_run_id"] for r in listed] == [res["assertion_run_id"]]
    reg.close()


def test_assertion_run_is_idempotent_on_same_logical_work(tmp_path):
    """The same assertion evaluated against the same fleet-registered call maps
    to ONE row (jobs.py idempotency-key pattern): a re-record dedups, never a
    second row and never an IntegrityError."""
    reg = Registry(home=str(tmp_path))
    reg.ensure_workspace("wsA")
    r1 = reg.add_assertion_run("wsA", assertion_id="a1", agent_id="bot-a",
                               call_id="call-1", kind="phrase", status="PASS")
    r2 = reg.add_assertion_run("wsA", assertion_id="a1", agent_id="bot-a",
                               call_id="call-1", kind="phrase", status="PASS")
    assert r1["assertion_run_id"] == r2["assertion_run_id"]
    assert r1["deduped"] is False and r2["deduped"] is True
    n = reg._one("SELECT COUNT(*) c FROM assertion_runs WHERE workspace_id='wsA'")["c"]
    assert n == 1
    # a DIFFERENT assertion / call is a distinct logical work -> distinct row
    r3 = reg.add_assertion_run("wsA", assertion_id="a2", agent_id="bot-a",
                               call_id="call-1", kind="phrase", status="FAIL")
    assert r3["assertion_run_id"] != r1["assertion_run_id"]
    assert reg._one("SELECT COUNT(*) c FROM assertion_runs WHERE workspace_id='wsA'")["c"] == 2
    reg.close()


def test_assertion_run_preserves_inconclusive_verbatim(tmp_path):
    """An absent-input INCONCLUSIVE is stored as-is, never coerced to a FAIL
    (honesty invariant 3)."""
    reg = Registry(home=str(tmp_path))
    reg.ensure_workspace("wsA")
    res = reg.add_assertion_run("wsA", assertion_id="a1", agent_id="bot-a",
                                call_id="call-1", kind="state", status="INCONCLUSIVE",
                                reason="no state adapter was provided")
    got = reg.get_assertion_run("wsA", res["assertion_run_id"])
    assert got["status"] == "INCONCLUSIVE"
    assert got["reason"] == "no state adapter was provided"
    reg.close()


# --- the other §F entities add/get/list + workspace isolation --------------

def test_release_suite_scenario_run_conversation_eval_review_round_trip(tmp_path):
    reg = Registry(home=str(tmp_path))
    reg.add_release("wsA", "rel-1", agent_id="bot-a", prompt_digest="pd", model="m",
                    voice="v", tool_schema_digest="ts", workflow_digest="wf",
                    provider_config_digest="pc")
    reg.add_suite("wsA", "suite-1", name="Smoke", purpose="release gate",
                  required_for_release=True, inconclusive_policy="fail")
    reg.add_scenario("wsA", "scn-1", suite_id="suite-1", goal="book a table",
                     facts_json=json.dumps({"party": 4}),
                     assertions_json=json.dumps([{"id": "a1", "kind": "phrase"}]))
    reg.add_run("wsA", "run-1", scenario_id="scn-1", release_id="rel-1", seed="7",
                provider_route="vapi", status="completed")
    reg.add_conversation("wsA", "conv-1", run_id="run-1", agent_id="bot-a",
                         origin="simulated", artifact_digest="cd")
    reg.add_evaluation("wsA", "eval-1", conversation_id="conv-1", evaluator_id="assert.v1",
                       dimension="outcome", status="PASS")
    reg.add_review("wsA", "rev-1", evaluation_id="eval-1", reviewer="alice",
                   decision="accept", adjudication_state="final")

    assert reg.get_release("wsA", "rel-1")["prompt_digest"] == "pd"
    assert reg.get_suite("wsA", "suite-1")["inconclusive_policy"] == "fail"
    assert reg.get_suite("wsA", "suite-1")["required_for_release"] == 1
    assert json.loads(reg.get_scenario("wsA", "scn-1")["facts_json"]) == {"party": 4}
    assert reg.get_run("wsA", "run-1")["status"] == "completed"
    assert reg.get_conversation("wsA", "conv-1")["origin"] == "simulated"
    assert reg.list_evaluations("wsA", conversation_id="conv-1")[0]["status"] == "PASS"
    assert reg.list_reviews("wsA", evaluation_id="eval-1")[0]["reviewer"] == "alice"

    # workspace isolation: none of wsA's rows are visible to wsB
    assert reg.get_release("wsB", "rel-1") is None
    assert reg.list_runs("wsB") == []
    assert reg.list_conversations("wsB") == []
    reg.close()


def test_conversation_origin_filter_keeps_real_and_simulated_separate(tmp_path):
    """origin real|simulated is a queryable axis, so synthetic conversations are
    never silently mixed with real ones (invariant 5)."""
    reg = Registry(home=str(tmp_path))
    reg.add_conversation("wsA", "c-real", agent_id="bot-a", origin="real")
    reg.add_conversation("wsA", "c-sim", agent_id="bot-a", origin="simulated")
    real = reg.list_conversations("wsA", origin="real")
    sim = reg.list_conversations("wsA", origin="simulated")
    assert [c["conversation_id"] for c in real] == ["c-real"]
    assert [c["conversation_id"] for c in sim] == ["c-sim"]
    reg.close()
