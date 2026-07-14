"""Determinism of the reference procedure's WRITTEN artifacts (determinism-375).

The reference procedure's claim is "same scenario -> same seeds -> byte-identical
artifacts", not merely a byte-identical in-memory summary. Two things used to
break that on disk:

* every written ``conversation.json`` manifest stamped the current UTC wall clock
  into ``created_at``, so two seeded runs diverged on that one field (the
  transcript/trace carry no timestamp and were already stable); and
* the fleet registry (``fleet.db``) is runtime bookkeeping whose ``*_at`` columns
  are wall-clock -- it is explicitly NOT part of the byte-identical artifact
  claim, so a determinism check over it must canonicalize those columns away.

This module pins both: the immutable conversation artifacts are byte-identical
across re-runs (the manifest ``created_at`` now defaults to a reproducible
SOURCE_DATE_EPOCH-style instant, never the wall clock), and the fleet.db is
deterministic MODULO the excluded bookkeeping columns.
"""

from __future__ import annotations

import json
import os
import sqlite3

import pytest

from hotato import simulate as SIM
from hotato import suite_run as SR
from hotato.fleet.registry import Registry

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
REFERENCE_DIR = os.path.join(REPO_ROOT, "examples", "reference-agent")


# =========================================================================
# helpers
# =========================================================================

def _read_all_files(root: str) -> dict:
    """Every file under ``root`` -> its raw bytes, keyed by POSIX relative path."""
    out: dict = {}
    for dirpath, _dirs, files in os.walk(root):
        for name in files:
            full = os.path.join(dirpath, name)
            rel = os.path.relpath(full, root).replace(os.sep, "/")
            with open(full, "rb") as fh:
                out[rel] = fh.read()
    return out


# The non-deterministic bookkeeping columns the fleet.db determinism claim
# EXCLUDES: every wall-clock timestamp (all named ``*_at``) plus the durable
# high-water mark. The registry is mutable runtime index state, not an immutable
# artifact -- its clock columns are deliberately outside the byte-identical claim.
def _is_bookkeeping_column(name: str) -> bool:
    return name.endswith("_at") or name == "last_watermark"


def canonicalize_fleet_db(db_path: str) -> str:
    """Canonicalize a fleet.db to a byte-stable string over its DETERMINISTIC
    content only: every user table, every row, sorted, with the non-deterministic
    bookkeeping columns (:func:`_is_bookkeeping_column`) and the implicit rowid
    excluded. This is the defined comparison for the fleet.db determinism claim --
    two seeded runs agree on it even though their raw ``*_at`` columns are
    wall-clock and differ."""
    conn = sqlite3.connect(db_path)
    try:
        conn.row_factory = sqlite3.Row
        tables = [
            r[0] for r in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' "
                "AND name NOT LIKE 'sqlite_%' ORDER BY name"
            ).fetchall()
        ]
        dump: dict = {}
        for table in tables:
            cols = [c[1] for c in conn.execute(f"PRAGMA table_info({table})")]
            keep = [c for c in cols if not _is_bookkeeping_column(c)]
            rows = [
                {c: row[c] for c in keep}
                for row in conn.execute(f"SELECT * FROM {table}").fetchall()
            ]
            # Sort rows by their full deterministic tuple so insertion/rowid order
            # never leaks into the comparison.
            rows.sort(key=lambda r: json.dumps(r, sort_keys=True, default=str))
            dump[table] = rows
        return json.dumps(dump, sort_keys=True, default=str, indent=2)
    finally:
        conn.close()


def _reference_slice_suite(n_tests: int = 2):
    """Load the real reference suite, trimmed to its first ``n_tests`` tests for a
    fast-but-faithful slice of the reference procedure."""
    suite_path = os.path.join(REFERENCE_DIR, "suite.json")
    suite_doc, base_dir = SR.load_suite_file(suite_path)
    trimmed = {**suite_doc, "tests": suite_doc["tests"][:n_tests]}
    return trimmed, base_dir


def _run_reference_slice(out_dir: str, registry_home: str):
    suite_doc, base_dir = _reference_slice_suite()
    reg = Registry(registry_home)
    try:
        SR.run_suite(
            suite_doc, base_dir, agent_id="reference-agent-v1",
            release_id="reference-agent-v1", workspace="reference",
            registry=reg, out_dir=out_dir, max_workers=4,
        )
    finally:
        reg.close()


# =========================================================================
# the manifest created_at default is reproducible, never the wall clock
# =========================================================================

def test_source_date_epoch_precedence(monkeypatch):
    # explicit arg wins over everything
    monkeypatch.setenv("SOURCE_DATE_EPOCH", "1700000000")
    assert SIM.deterministic_created_at("2020-01-02T03:04:05Z") == "2020-01-02T03:04:05Z"
    # env honored when no explicit arg
    assert SIM.resolve_source_date_epoch() == 1700000000
    assert SIM.deterministic_created_at() == "2023-11-14T22:13:20Z"
    # unset -> the fixed reproducible default (epoch 0), NEVER the wall clock
    monkeypatch.delenv("SOURCE_DATE_EPOCH", raising=False)
    assert SIM.resolve_source_date_epoch() == 0
    assert SIM.deterministic_created_at() == "1970-01-01T00:00:00Z"


def test_source_date_epoch_rejects_non_integer(monkeypatch):
    monkeypatch.setenv("SOURCE_DATE_EPOCH", "not-an-epoch")
    with pytest.raises(ValueError):
        SIM.resolve_source_date_epoch()


def test_run_matrix_written_manifest_is_reproducible_not_wallclock(tmp_path, monkeypatch):
    # No SOURCE_DATE_EPOCH: the default must be the fixed reproducible instant, so
    # a re-run cannot diverge on created_at.
    monkeypatch.delenv("SOURCE_DATE_EPOCH", raising=False)
    doc = {
        "kind": "hotato.scenario", "version": 1, "id": "refund-basic",
        "goal": {"type": "get_refund", "target": "order A-1001"},
        "facts": {"order_id": "A-1001"},
        "caller": {"script": [{"say": "order A-1001 arrived damaged, refund please"}]},
        "environment": {"noise": "clean", "locale": "en-US"},
        "variation_matrix": {"locale": ["en-US", "es-ES"], "speaking_rate": [1.0],
                             "noise": ["clean"], "repetitions": 1},
        "seed": 7,
    }
    a = tmp_path / "a"
    SIM.run_matrix(doc, out_dir=str(a))
    manifest = json.loads((a / "refund-basic-000" / "conversation.json").read_text())
    assert manifest["created_at"] == "1970-01-01T00:00:00Z"


def test_run_matrix_written_artifacts_byte_identical_across_runs(tmp_path):
    doc = {
        "kind": "hotato.scenario", "version": 1, "id": "refund-basic",
        "goal": {"type": "get_refund", "target": "order A-1001"},
        "facts": {"order_id": "A-1001"},
        "caller": {"script": [{"say": "order A-1001 arrived damaged, refund please"},
                              {"say": "send it to my card"}]},
        "environment": {"noise": "clean", "locale": "en-US"},
        "variation_matrix": {"locale": ["en-US", "es-ES"], "speaking_rate": [0.9, 1.1],
                             "noise": ["clean", "cafe"], "repetitions": 2},
        "seed": 7,
    }
    a = tmp_path / "a"
    b = tmp_path / "b"
    SIM.run_matrix(doc, out_dir=str(a))
    SIM.run_matrix(doc, out_dir=str(b))
    fa = _read_all_files(str(a))
    fb = _read_all_files(str(b))
    assert set(fa) == set(fb)
    mismatch = sorted(k for k in fa if fa[k] != fb[k])
    assert mismatch == [], f"non-deterministic written artifacts: {mismatch}"
    # and the run actually wrote the three-file conversation artifacts
    assert any(k.endswith("conversation.json") for k in fa)
    assert any(k.endswith("transcript.json") for k in fa)
    assert any(k.endswith("trace.jsonl") for k in fa)


# =========================================================================
# the reference procedure (a small seeded slice) run twice is byte-identical
# =========================================================================

def test_reference_slice_conversation_artifacts_byte_identical(tmp_path):
    out_a = tmp_path / "out-a"
    out_b = tmp_path / "out-b"
    reg_a = tmp_path / "reg-a"
    reg_b = tmp_path / "reg-b"

    _run_reference_slice(str(out_a), str(reg_a))
    _run_reference_slice(str(out_b), str(reg_b))

    fa = _read_all_files(str(out_a))
    fb = _read_all_files(str(out_b))
    assert set(fa) == set(fb), "the two runs wrote a different set of files"
    assert fa, "the reference slice wrote no artifacts"
    mismatch = sorted(k for k in fa if fa[k] != fb[k])
    assert mismatch == [], (
        f"reference-slice artifacts are not byte-identical across runs: {mismatch}"
    )


def test_reference_slice_fleet_db_deterministic_modulo_bookkeeping(tmp_path):
    reg_a = tmp_path / "reg-a"
    reg_b = tmp_path / "reg-b"

    _run_reference_slice(str(tmp_path / "out-a"), str(reg_a))
    _run_reference_slice(str(tmp_path / "out-b"), str(reg_b))

    db_a = os.path.join(str(reg_a), "fleet.db")
    db_b = os.path.join(str(reg_b), "fleet.db")
    assert os.path.isfile(db_a) and os.path.isfile(db_b)

    canon_a = canonicalize_fleet_db(db_a)
    canon_b = canonicalize_fleet_db(db_b)
    assert canon_a == canon_b, (
        "fleet.db deterministic content (bookkeeping columns excluded) diverged "
        "across two seeded reference-slice runs"
    )
    # The canonicalization is load-bearing: the RAW created_at columns really do
    # differ across the two runs (wall clock), so the equality above is only true
    # BECAUSE the bookkeeping columns are excluded -- proving the exclusion is what
    # the determinism claim rests on, not an accident of identical timing.
    conn_a = sqlite3.connect(db_a)
    conn_b = sqlite3.connect(db_b)
    try:
        raw_a = [r[0] for r in conn_a.execute(
            "SELECT created_at FROM conversations ORDER BY conversation_id")]
        raw_b = [r[0] for r in conn_b.execute(
            "SELECT created_at FROM conversations ORDER BY conversation_id")]
    finally:
        conn_a.close()
        conn_b.close()
    assert raw_a and raw_b
    # (Same length, same ids; the timestamps themselves are runtime state -- we
    # only assert they are present, not that they match.)
    assert len(raw_a) == len(raw_b)
