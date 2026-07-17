"""Read-only workspace projection of the separate production evidence DB."""

from __future__ import annotations

import json
import os
import sqlite3
import threading
import urllib.request

import pytest

import hotato.cli as cli_module
import hotato.serve as serve_module
import hotato.serve.production_bridge as bridge_module
from hotato.cli import build_parser
from hotato.fleet.registry import Registry
from hotato.production import ProductionStore
from hotato.serve import build_server
from hotato.serve.app import ServeContext
from hotato.serve.production_bridge import (
    ProductionBridgeError,
    read_production_snapshot,
)
from hotato.serve.security import AuditLog, SessionStore

_TOKEN = "tok_workspace_production_bridge_0123456789"
_SECRET_PAYLOAD = "PAYLOAD_MUST_NEVER_REACH_WORKSPACE"


def _event(event_id, event_type, sequence, *, data=None, authority="adapter_reported"):
    return {
        "specversion": "1.0",
        "id": event_id,
        "source": "livekit-adapter",
        "type": event_type,
        "subject": "prod-call-7",
        "time": "2026-07-17T12:00:00Z",
        "sequence": sequence,
        "data": {} if data is None else data,
        "authority": {
            "kind": authority,
            "eligible_for_execution_claim": authority
            in ("measured", "signed_attestation"),
        },
    }


def _production_db(tmp_path):
    path = str(tmp_path / "production.sqlite3")
    store = ProductionStore(path, clock=lambda: 100.0)
    store.ingest(_event("start", "session.started", 0))
    store.ingest(
        _event(
            "audio",
            "media.asset.available",
            1,
            data={"availability": "available"},
            authority="measured",
        )
    )
    # Persist a source payload in the DB to prove the bridge never selects it.
    store.ingest(
        _event(
            "transcript",
            "transcript.segment",
            2,
            data={"text": _SECRET_PAYLOAD},
        ),
        redact_payloads=False,
    )
    store.ingest(_event("end", "session.ended", 3))
    store.finalize(quiescence_seconds=0, now=101.0)
    store.evaluate_alerts(
        [{"id": "tool-evidence-required", "condition": "missing_tool_evidence"}]
    )
    expected = store.manifest("prod-call-7")
    store.close()
    return path, expected


def test_snapshot_matches_writer_manifest_without_reading_payload_or_writing(tmp_path):
    path, expected = _production_db(tmp_path)
    before = os.stat(path).st_mtime_ns

    snapshot = read_production_snapshot(path)

    assert os.stat(path).st_mtime_ns == before
    assert snapshot["source"] == {
        "kind": "hotato.production.sqlite3",
        "path": os.path.abspath(path),
        "schema_version": "1",
        "access": "sqlite-mode-ro",
        "workspace_scope": "not_encoded_by_production_schema",
        "payload_columns_read": False,
        "fleet_rows_written": False,
    }
    assert snapshot["sessions"][0]["manifest"] == expected
    assert snapshot["sessions"][0]["event_sources"] == ["livekit-adapter"]
    assert snapshot["sessions"][0]["missing_required_lanes"] == [
        "model_trace",
        "tool_calls",
        "backend_state",
    ]
    assert expected["evidence"]["participant_audio"]["authority"] == "measured"
    assert expected["evidence"]["transcript"]["authority"] == "adapter_reported"
    assert snapshot["alerts"][0]["state"] == "FIRING"
    assert snapshot["alerts"][0]["rule_id"] == "tool-evidence-required"
    assert _SECRET_PAYLOAD not in json.dumps(snapshot)

    db = bridge_module._open_read_only(os.path.abspath(path))
    try:
        with pytest.raises(sqlite3.DatabaseError, match="prohibited|authoriz"):
            db.execute("SELECT payload_json FROM events").fetchall()
        with pytest.raises(sqlite3.DatabaseError):
            db.execute("UPDATE sessions SET state='OPEN'")
    finally:
        db.close()


def test_health_json_and_html_keep_production_store_separate_from_fleet(tmp_path):
    production_path, _expected = _production_db(tmp_path)
    home = str(tmp_path / "fleet")
    reg = Registry(home=home)
    reg.ensure_workspace("default", "Bridge test")
    assert reg.list_conversations("default") == []
    reg.close()

    state = os.path.join(home, "serve", "default")
    os.makedirs(state, exist_ok=True)
    context = ServeContext(
        home=home,
        workspace="default",
        store_root=os.path.join(home, "artifacts"),
        token=_TOKEN,
        state_dir=state,
        audit=AuditLog(os.path.join(state, "audit.jsonl")),
        sessions=SessionStore(),
        bind_host="127.0.0.1",
        production_db=production_path,
    )
    server = build_server(context, "127.0.0.1", 0)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    base = "http://127.0.0.1:%d" % server.server_address[1]

    def get(path):
        request = urllib.request.Request(base + path)
        request.add_header("Authorization", "Bearer " + _TOKEN)
        with urllib.request.urlopen(request, timeout=5) as response:
            return response.read().decode("utf-8")

    try:
        model = json.loads(get("/health?format=json"))
        html = get("/health")
    finally:
        server.shutdown()
        thread.join(timeout=5)
        server.server_close()

    # The fleet read model stays empty; production data lives under an explicit
    # separate key and cannot affect real/simulated counts or release trends.
    assert model["ingested_total"] == 0
    assert all(item["ingested"] == 0 for item in model["origins"].values())
    bridge = model["production_evidence"]
    assert bridge["summary"]["sessions_total"] == 1
    assert bridge["sessions"][0]["manifest"]["session_id"] == "prod-call-7"
    assert bridge["alerts"][0]["condition"] == "missing_tool_evidence"
    assert _SECRET_PAYLOAD not in json.dumps(model)

    assert "Production evidence plane" in html
    assert "prod-call-7" in html
    assert "livekit-adapter" in html
    assert "tool-evidence-required" in html
    assert "participant_audio" in html and "measured" in html
    assert "missing required lanes" in html and "tool_calls" in html
    assert "not imported into fleet" in html
    assert _SECRET_PAYLOAD not in html

    reg = Registry(home=home)
    assert reg.list_conversations("default") == []
    reg.close()


def test_bridge_is_explicit_and_invalid_database_fails_closed(tmp_path):
    args = build_parser().parse_args(
        ["serve", "--production-db", str(tmp_path / "production.sqlite3")]
    )
    assert args.production_db.endswith("production.sqlite3")

    plain = tmp_path / "plain.sqlite3"
    db = sqlite3.connect(str(plain))
    db.execute("CREATE TABLE unrelated(value TEXT)")
    db.close()
    with pytest.raises(ProductionBridgeError, match="not a hotato production"):
        read_production_snapshot(str(plain))

    target, _expected = _production_db(tmp_path / "valid")
    link = tmp_path / "production-link.sqlite3"
    try:
        os.symlink(target, link)
    except (OSError, NotImplementedError):
        pytest.skip("symlink creation is unavailable")
    with pytest.raises(ProductionBridgeError, match="regular file"):
        read_production_snapshot(str(link))


def test_cli_passes_explicit_production_database_to_workspace(monkeypatch, tmp_path):
    observed = {}

    def fake_run_serve(**kwargs):
        observed.update(kwargs)
        return 0

    monkeypatch.setattr(serve_module, "run_serve", fake_run_serve)
    path = str(tmp_path / "production.sqlite3")
    assert cli_module.main(["serve", "--production-db", path, "--no-open"]) == 0
    assert observed["production_db"] == path
    assert observed["open_browser"] is False


@pytest.mark.parametrize(
    ("mutation", "message"),
    [
        ("string_eligibility", "non-boolean eligibility"),
        ("contradictory_authority", "contradictory authority"),
        ("invalid_availability", "invalid availability"),
        ("invalid_event_id", "invalid event ids"),
        ("duplicate_event_id", "invalid event ids"),
    ],
)
def test_bridge_refuses_malformed_authority_and_event_identity_metadata(
    tmp_path, mutation, message
):
    path, _expected = _production_db(tmp_path)
    db = sqlite3.connect(path)
    raw = db.execute(
        "SELECT evidence_json FROM sessions WHERE subject='prod-call-7'"
    ).fetchone()[0]
    evidence = json.loads(raw)
    lane = evidence["participant_audio"]
    if mutation == "string_eligibility":
        lane["eligible_for_execution_claim"] = "false"
    elif mutation == "contradictory_authority":
        lane["authority"] = "measured"
        lane["eligible_for_execution_claim"] = False
    elif mutation == "invalid_availability":
        lane["availability"] = "maybe"
    elif mutation == "invalid_event_id":
        lane["event_ids"] = [{}]
    else:
        lane["event_ids"] = ["audio", "audio"]
    db.execute(
        "UPDATE sessions SET evidence_json=? WHERE subject='prod-call-7'",
        (json.dumps(evidence),),
    )
    db.commit()
    db.close()

    with pytest.raises(ProductionBridgeError, match=message):
        read_production_snapshot(path)
