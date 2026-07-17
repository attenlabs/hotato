from __future__ import annotations

import hashlib
import json
import os
import time
from pathlib import Path

import pytest
from jsonschema import Draft7Validator

from hotato import loadtest
from hotato.telephony import CallHandle


class FakeClient:
    def __init__(self, *, delay: float = 0.0, complete_evidence: bool = True):
        self.delay = delay
        self.complete_evidence = complete_evidence
        self.faulted = []

    def create(self, spec):
        call_id = spec["id"]
        return CallHandle("local", call_id, "queued", "queued", "2026-07-17T00:00:00Z", {
            "schema": "hotato.telephony-receipt.v1", "operation": "create",
            "call_id": call_id, "authority": "provider_reported",
        })

    def wait(self, handle, **_kwargs):
        if self.delay:
            time.sleep(self.delay)
        return CallHandle("local", handle.call_id, "completed", "completed", handle.created_at, {
            "schema": "hotato.telephony-receipt.v1", "operation": "terminal",
            "call_id": handle.call_id, "authority": "provider_reported",
        })

    def evidence(self, handle):
        state = "PRESENT" if self.complete_evidence else "UNOBSERVABLE"
        authority = "measured" if self.complete_evidence else "unverified"
        call_id_sha256 = loadtest._call_id_hash(handle.provider, handle.call_id)
        return {
            "schema": "hotato.load-evidence.v1",
            "provider": handle.provider,
            "call_id_sha256": call_id_sha256,
            "lanes": {
                name: {
                    "state": state,
                    "authority": authority,
                    "sha256": (
                        "sha256:"
                        + hashlib.sha256(
                            f"{handle.call_id}:{name}".encode("utf-8")
                        ).hexdigest()
                        if self.complete_evidence
                        else None
                    ),
                    "eligible_for_execution_claim": self.complete_evidence,
                }
                for name in ("delivered_audio", "tool_trace", "backend_state")
            },
        }

    def inject_fault(self, kind, spec):
        self.faulted.append((kind, spec["id"]))
        raise RuntimeError("scheduled provider fault")


def _plan(*, stages, slos=None, safety=None, faults=None):
    return {
        "schema": "hotato.load-plan.v2", "id": "load-a",
        "call": {"schema": "hotato.telephony-call.v1", "id": "base", "provider": "local", "to": "local:test"},
        "stages": stages, "terminal_timeout_seconds": 1, "poll_seconds": 0.001,
        "slos": slos or {}, "safety": safety or {"max_calls": 100},
        "faults": faults or [],
    }


def _write(path: Path, value) -> Path:
    path.write_text(json.dumps(value), encoding="utf-8")
    return path


@pytest.mark.skipif(not hasattr(os, "mkfifo"), reason="FIFOs require POSIX")
def test_load_plan_refuses_writerless_fifo_without_blocking(tmp_path):
    path = tmp_path / "plan.json"
    os.mkfifo(path)
    with pytest.raises(ValueError, match="regular non-symlink"):
        loadtest.load_plan(str(path))


def test_load_plan_binds_precheck_to_opened_inode(tmp_path, monkeypatch):
    path = _write(
        tmp_path / "plan.json",
        _plan(
            stages=[
                {
                    "name": "stress",
                    "phase": "stress",
                    "model": "closed",
                    "concurrency": 1,
                    "calls": 1,
                }
            ]
        ),
    )
    replacement = _write(
        tmp_path / "replacement.json",
        _plan(
            stages=[
                {
                    "name": "other",
                    "phase": "stress",
                    "model": "closed",
                    "concurrency": 1,
                    "calls": 1,
                }
            ]
        ),
    )
    original_open = loadtest.os.open

    def swapped(raw_path, flags, *args):
        if os.fspath(raw_path) == os.fspath(path):
            return original_open(replacement, flags, *args)
        return original_open(raw_path, flags, *args)

    monkeypatch.setattr(loadtest.os, "open", swapped)
    with pytest.raises(ValueError, match="changed while it was opened"):
        loadtest.load_plan(str(path))


def test_closed_workload_writes_and_verifies_one_child_per_call(tmp_path):
    plan = _plan(
        stages=[{"name": "stress", "phase": "stress", "model": "closed", "concurrency": 2, "calls": 4}],
        slos={
            "min_lifecycle_completion_rate": 1,
            "min_evidence_complete_rate": 1,
            "max_dropped_start_rate": 0,
        },
    )
    result = loadtest.run(str(_write(tmp_path / "plan.json", plan)), str(tmp_path / "out"), client=FakeClient())
    assert result.exit_code == 0
    assert result.summary["status"] == "PASS"
    assert result.summary["metrics"]["lifecycle_completion_rate"] == 1
    assert result.summary["metrics"]["evidence_complete_rate"] == 1
    assert len(list((tmp_path / "out" / "calls").iterdir())) == 4
    assert loadtest.verify(str(tmp_path / "out"))["ok"] is True
    schema_root = Path(loadtest.__file__).with_name("schema")
    child = sorted((tmp_path / "out" / "calls").iterdir())[0]
    Draft7Validator(
        json.loads(
            (schema_root / "load-call-package.v2.json").read_text(encoding="utf-8")
        )
    ).validate(json.loads((child / "manifest.json").read_text(encoding="utf-8")))
    Draft7Validator(
        json.loads(
            (schema_root / "load-evidence.v1.json").read_text(encoding="utf-8")
        )
    ).validate(
        json.loads((child / "provider-export.json").read_text(encoding="utf-8"))
    )


def test_provider_completed_is_separate_from_evidence_complete(tmp_path):
    plan = _plan(
        stages=[{"name": "stress", "phase": "stress", "model": "closed", "concurrency": 1, "calls": 2}],
        slos={"min_lifecycle_completion_rate": 1, "min_evidence_complete_rate": 1},
    )
    result = loadtest.run(str(_write(tmp_path / "plan.json", plan)), str(tmp_path / "out"), client=FakeClient(complete_evidence=False))
    assert result.exit_code == 1
    assert result.summary["metrics"]["lifecycle_completion_rate"] == 1
    assert result.summary["metrics"]["evidence_complete_rate"] == 0
    assert {row["status"] for row in result.summary["slos"]} == {"PASS", "FAIL"}


def test_open_arrival_drops_instead_of_hiding_generator_saturation(tmp_path):
    plan = _plan(
        stages=[{
            "name": "spike", "phase": "spike", "model": "open",
            "arrival_rate_per_second": 1000, "duration_seconds": 0.006,
            "max_in_flight": 1,
        }],
        slos={"max_dropped_start_rate": 0},
    )
    result = loadtest.run(str(_write(tmp_path / "plan.json", plan)), str(tmp_path / "out"), client=FakeClient(delay=0.03))
    assert result.exit_code == 1
    assert result.summary["metrics"]["scheduled"] == 6
    assert result.summary["metrics"]["dropped_starts"] >= 1
    rows = [json.loads(line) for line in (tmp_path / "out" / "observations.jsonl").read_text().splitlines()]
    assert "generator_saturated" in {row["drop_reason"] for row in rows}


def test_no_slo_cannot_be_reported_as_pass(tmp_path):
    plan = _plan(stages=[{"name": "warm", "phase": "warmup", "model": "closed", "concurrency": 1, "calls": 1}])
    result = loadtest.run(str(_write(tmp_path / "plan.json", plan)), str(tmp_path / "out"), client=FakeClient())
    assert result.exit_code == 2
    assert result.summary["status"] == "INCONCLUSIVE"


def test_cost_and_destination_guardrails_refuse_before_execution(tmp_path):
    plan = _plan(
        stages=[{"name": "stress", "phase": "stress", "model": "closed", "concurrency": 1, "calls": 3}],
        safety={
            "max_calls": 3, "estimated_cost_per_call_usd": 2,
            "max_estimated_cost_usd": 5, "allowed_destinations": ["+1555"],
        },
    )
    plan["call"]["to"] = "+15551234567"
    with pytest.raises(ValueError, match="estimated plan cost"):
        loadtest.validate_plan(plan)
    plan["safety"]["max_estimated_cost_usd"] = 6
    plan["call"]["to"] = "+14441234567"
    with pytest.raises(ValueError, match="outside"):
        loadtest.validate_plan(plan)


@pytest.mark.parametrize(
    "missing",
    [
        "estimated_cost_per_call_usd",
        "max_estimated_cost_usd",
        "allowed_destinations",
    ],
)
def test_remote_load_requires_explicit_billable_safety_gates(missing):
    safety = {
        "max_calls": 1,
        "estimated_cost_per_call_usd": 0.01,
        "max_estimated_cost_usd": 0.01,
        "allowed_destinations": ["+15551234567"],
    }
    safety.pop(missing)
    plan = _plan(
        stages=[
            {
                "name": "remote",
                "phase": "stress",
                "model": "closed",
                "concurrency": 1,
                "calls": 1,
            }
        ],
        safety=safety,
    )
    plan["call"] = {
        "schema": "hotato.telephony-call.v1",
        "id": "remote-call",
        "provider": "vapi",
        "to": "+15551234567",
        "agent_id": "agent-a",
        "phone_number_id": "phone-a",
    }
    with pytest.raises(ValueError):
        loadtest.validate_plan(plan)


def test_legacy_v1_remote_load_is_refused_but_local_remains_supported():
    remote = {
        "schema": "hotato.load-plan.v1",
        "id": "legacy-remote",
        "call": {
            "schema": "hotato.telephony-call.v1",
            "id": "remote-call",
            "provider": "vapi",
            "to": "+15551234567",
            "agent_id": "agent-a",
            "phone_number_id": "phone-a",
        },
        "stages": [{"concurrency": 1, "calls": 1}],
        "slos": {"min_completion_rate": 1},
    }
    with pytest.raises(ValueError, match="billable safety"):
        loadtest.validate_plan(remote)

    local = dict(remote)
    local["call"] = {
        "schema": "hotato.telephony-call.v1",
        "id": "local-call",
        "provider": "local",
        "to": "fixture://agent",
    }
    assert loadtest.validate_plan(local)["call"]["provider"] == "local"


def test_bare_present_evidence_cannot_satisfy_evidence_slo(tmp_path):
    class BareEvidenceClient(FakeClient):
        def evidence(self, handle):
            return {
                "evidence": {
                    "delivered_audio": "PRESENT",
                    "tool_trace": "PRESENT",
                    "backend_state": "PRESENT",
                }
            }

    plan = _plan(
        stages=[
            {
                "name": "stress",
                "phase": "stress",
                "model": "closed",
                "concurrency": 1,
                "calls": 1,
            }
        ],
        slos={"min_evidence_complete_rate": 1},
    )
    result = loadtest.run(
        str(_write(tmp_path / "plan.json", plan)),
        str(tmp_path / "out"),
        client=BareEvidenceClient(),
    )
    assert result.summary["status"] == "FAIL"
    assert result.summary["metrics"]["evidence_complete_rate"] == 0
    row = json.loads((tmp_path / "out" / "observations.jsonl").read_text())
    assert row["error_type"] == "LoadError"


def test_unverified_present_evidence_cannot_satisfy_execution_evidence_slo(
    tmp_path,
):
    class UnverifiedEvidenceClient(FakeClient):
        def evidence(self, handle):
            call_id_sha256 = loadtest._call_id_hash(
                handle.provider, handle.call_id
            )
            return {
                "schema": "hotato.load-evidence.v1",
                "provider": handle.provider,
                "call_id_sha256": call_id_sha256,
                "lanes": {
                    name: {
                        "state": "PRESENT",
                        "authority": "unverified",
                        "sha256": (
                            "sha256:" + hashlib.sha256(
                                f"{handle.call_id}:{name}".encode()
                            ).hexdigest()
                        ),
                        "eligible_for_execution_claim": False,
                    }
                    for name in (
                        "delivered_audio", "tool_trace", "backend_state"
                    )
                },
            }

    plan = _plan(
        stages=[{
            "name": "stress", "phase": "stress", "model": "closed",
            "concurrency": 1, "calls": 1,
        }],
        slos={"min_evidence_complete_rate": 1},
    )
    result = loadtest.run(
        str(_write(tmp_path / "plan.json", plan)),
        str(tmp_path / "out-unverified"),
        client=UnverifiedEvidenceClient(),
    )
    assert result.summary["status"] == "FAIL"
    assert result.summary["metrics"]["evidence_complete_rate"] == 0
    row = json.loads(
        (tmp_path / "out-unverified" / "observations.jsonl").read_text()
    )
    assert row["error_type"] is None
    assert row["evidence"] == {
        "backend_state": "PRESENT",
        "call_lifecycle": "PRESENT",
        "delivered_audio": "PRESENT",
        "tool_trace": "PRESENT",
    }
    assert row["evidence_complete"] is False


def test_verifier_refuses_unlisted_child_file(tmp_path):
    plan = _plan(
        stages=[
            {
                "name": "stress",
                "phase": "stress",
                "model": "closed",
                "concurrency": 1,
                "calls": 1,
            }
        ],
        slos={"min_lifecycle_completion_rate": 1},
    )
    loadtest.run(
        str(_write(tmp_path / "plan.json", plan)),
        str(tmp_path / "out"),
        client=FakeClient(),
    )
    child = next((tmp_path / "out" / "calls").iterdir())
    (child / "unlisted-secret.txt").write_text("must not verify", encoding="utf-8")
    verification = loadtest.verify(str(tmp_path / "out"))
    assert verification["ok"] is False
    assert any("unexpected:unlisted-secret.txt" in row for row in verification["mismatches"])


def test_fault_schedule_is_recorded_and_recovery_stays_separate(tmp_path):
    client = FakeClient()
    plan = _plan(
        stages=[{"name": "recovery", "phase": "recovery", "model": "closed", "concurrency": 1, "calls": 3}],
        faults=[{"stage": "recovery", "after_call": 0, "kind": "provider_error", "duration_calls": 1}],
        slos={"min_lifecycle_completion_rate": 0.6, "max_recovery_seconds": 10},
    )
    result = loadtest.run(str(_write(tmp_path / "plan.json", plan)), str(tmp_path / "out"), client=client)
    assert client.faulted and client.faulted[0][0] == "provider_error"
    assert result.summary["recovery"]["measurements"][0]["recovery_seconds"] is not None
    assert result.exit_code == 0


def test_verifier_rehashes_child_and_recomputes_observations(tmp_path):
    plan = _plan(
        stages=[{"name": "stress", "phase": "stress", "model": "closed", "concurrency": 1, "calls": 1}],
        slos={"min_lifecycle_completion_rate": 1},
    )
    loadtest.run(str(_write(tmp_path / "plan.json", plan)), str(tmp_path / "out"), client=FakeClient())
    child = next((tmp_path / "out" / "calls").iterdir())
    receipt = child / "create-receipt.json"
    receipt.write_text(receipt.read_text() + " ", encoding="utf-8")
    verdict = loadtest.verify(str(tmp_path / "out"))
    assert verdict["ok"] is False
    assert any("create-receipt.json" in item for item in verdict["mismatches"])


def test_v1_plan_normalizes_to_explicit_closed_stage():
    normalized = loadtest.validate_plan({
        "schema": "hotato.load-plan.v1", "id": "legacy",
        "call": {"schema": "hotato.telephony-call.v1", "id": "base", "provider": "local", "to": "local:test"},
        "stages": [{"concurrency": 2, "calls": 5}], "slos": {"min_completion_rate": 1},
    })
    assert normalized["stages"][0]["model"] == "closed"
    assert normalized["stages"][0]["calls"] == 5


def test_normalized_open_plan_and_published_result_conform_to_schemas(tmp_path):
    schema_root = Path(loadtest.__file__).with_name("schema")
    plan = _plan(
        stages=[{
            "name": "open", "phase": "spike", "model": "open",
            "arrival_rate_per_second": 10, "duration_seconds": 0.1,
            "max_in_flight": 2,
        }],
        slos={"min_lifecycle_completion_rate": 1},
    )
    normalized = loadtest.validate_plan(plan, str(tmp_path))
    plan_schema = json.loads((schema_root / "load-plan.v2.json").read_text(encoding="utf-8"))
    Draft7Validator(plan_schema).validate(normalized)

    result = loadtest.run(
        str(_write(tmp_path / "plan.json", plan)),
        str(tmp_path / "out"),
        client=FakeClient(),
    )
    result_schema = json.loads((schema_root / "load-result.v2.json").read_text(encoding="utf-8"))
    verification_schema = json.loads(
        (schema_root / "load-verification-plan.v2.json").read_text(encoding="utf-8")
    )
    Draft7Validator(result_schema).validate(result.summary)
    verification_plan = json.loads(
        (tmp_path / "out" / "verification-plan.json").read_text(encoding="utf-8")
    )
    Draft7Validator(verification_schema).validate(verification_plan)
    assert verification_plan["schema"] == "hotato.load-verification-plan.v2"
    assert result.summary["workload_plan_sha256"] == verification_plan["workload"][
        "normalized_plan_sha256"
    ]
    assert result.summary["call_spec_sha256"] == verification_plan["workload"][
        "call_spec_sha256"
    ]
