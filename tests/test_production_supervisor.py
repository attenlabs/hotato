from __future__ import annotations

import json
import os

import pytest

from hotato.production import ProductionStore
from hotato.production_supervisor import (
    ProductionSupervisor,
    load_policy,
    validate_policy,
)


def _event(event_id: str, kind: str, sequence: int, now: float = 10.0) -> dict:
    return {
        "specversion": "1.0",
        "id": event_id,
        "source": "supervisor-fixture",
        "type": kind,
        "subject": "call-supervised",
        "time": "2026-07-17T12:00:00Z",
        "sequence": sequence,
        "data": {},
        "authority": {
            "kind": "adapter_reported",
            "eligible_for_execution_claim": False,
        },
        "_fixture_now": now,
    }


def _without_fixture(value: dict) -> dict:
    result = dict(value)
    result.pop("_fixture_now", None)
    return result


def _policy(**overrides):
    value = {
        "schema": "hotato.production-maintenance.v1",
        "interval_seconds": 30,
        "quiescence_seconds": 0,
        "required_lanes": ["participant_audio"],
        "alert_rules": [{"id": "degraded-session", "condition": "degraded"}],
        "retention_seconds": None,
    }
    value.update(overrides)
    return value


def test_policy_validation_and_bounded_file_loader(tmp_path):
    path = tmp_path / "maintenance.json"
    path.write_text(json.dumps(_policy()), encoding="utf-8")
    loaded = load_policy(str(path))
    assert loaded.required_lanes == ("participant_audio",)
    assert loaded.public()["alert_rules"][0]["condition"] == "degraded"

    with pytest.raises(ValueError, match="unknown field"):
        validate_policy({**_policy(), "surprise": True})
    with pytest.raises(ValueError, match="unique"):
        validate_policy(_policy(alert_rules=[
            {"id": "same", "condition": "degraded"},
            {"id": "same", "condition": "conflict"},
        ]))
    with pytest.raises(ValueError, match="interval_seconds"):
        validate_policy(_policy(interval_seconds=0))


@pytest.mark.skipif(not hasattr(os, "mkfifo"), reason="FIFOs require POSIX")
def test_policy_loader_refuses_fifo(tmp_path):
    fifo = tmp_path / "maintenance-fifo"
    os.mkfifo(fifo)
    with pytest.raises(ValueError, match="regular"):
        load_policy(str(fifo))


def test_run_once_finalizes_then_alerts_and_records_status(tmp_path):
    now = [100.0]
    store = ProductionStore(str(tmp_path / "production.sqlite"), clock=lambda: now[0])
    store.ingest(_without_fixture(_event("start", "session.started", 0)))
    store.ingest(_without_fixture(_event("end", "session.ended", 1)))
    supervisor = ProductionSupervisor(
        store, validate_policy(_policy()), clock=lambda: now[0], autostart=False
    )
    result = supervisor.run_once()
    assert result["finalized_count"] == 1
    assert result["finalized"][0]["status"] == "DEGRADED"
    assert result["alert_transition_count"] == 1
    assert result["alert_transitions"][0]["state"] == "FIRING"
    status = supervisor.status()
    assert status["state"] == "IDLE"
    assert status["cycles"] == 1
    assert status["last_error"] is None
    supervisor.close()
    assert supervisor.status()["state"] == "STOPPED"
    store.close()


def test_retention_runs_after_alert_evaluation(tmp_path):
    now = [100.0]
    store = ProductionStore(str(tmp_path / "production.sqlite"), clock=lambda: now[0])
    store.ingest(_without_fixture(_event("start", "session.started", 0)))
    store.ingest(_without_fixture(_event("end", "session.ended", 1)))
    store.finalize(quiescence_seconds=0, required_lanes=("participant_audio",))
    now[0] = 200.0
    supervisor = ProductionSupervisor(
        store,
        validate_policy(_policy(retention_seconds=10)),
        clock=lambda: now[0],
        autostart=False,
    )
    result = supervisor.run_once()
    assert result["retention_deletion_count"] == 1
    with pytest.raises(KeyError):
        store.manifest("call-supervised")
    assert store.verify_audit_chain()["valid"] is True
    supervisor.close()
    store.close()


def test_loop_captures_error_and_can_retry_without_dying(tmp_path, monkeypatch):
    store = ProductionStore(str(tmp_path / "production.sqlite"))
    supervisor = ProductionSupervisor(
        store,
        validate_policy(_policy()),
        autostart=False,
    )
    original = store.finalize
    calls = [0]

    def failing_once(**kwargs):
        calls[0] += 1
        if calls[0] == 1:
            raise RuntimeError("temporary storage failure")
        return original(**kwargs)

    monkeypatch.setattr(store, "finalize", failing_once)
    with pytest.raises(RuntimeError, match="temporary"):
        supervisor.run_once()
    assert supervisor.status()["state"] == "ERROR"
    assert supervisor.run_once()["finalized_count"] == 0
    assert supervisor.status()["state"] == "IDLE"
    supervisor.close()
    store.close()
