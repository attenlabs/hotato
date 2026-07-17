"""Adversarial checks for the load-result offline verifier.

These tests intentionally re-hash a modified ``summary.json``.  A content
digest alone proves self-consistency, so ``verify`` must independently derive
every claimed conclusion from bound observations and the bound normalized
plan.  Otherwise anyone who can edit the directory can relabel a failed run as
a passing one and produce a new, internally consistent digest.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from hotato import loadtest
from hotato.telephony import TelephonyClient


class _FaultCapableLocalClient(TelephonyClient):
    """Hermetic controller that records a declared injection as a no-op.

    The load engine requires an explicit ``inject_fault`` seam whenever a plan
    contains a fault.  The verifier tests only need a completed, reproducible
    package with a fault schedule; they do not claim the local lifecycle
    fixture suffered a transport fault.
    """

    def inject_fault(self, _kind, _call_doc):
        return None


def _plan(*, faults=None, slos=None):
    return {
        "schema": "hotato.load-plan.v2",
        "id": "adversarial-load-plan",
        "call": {
            "schema": "hotato.telephony-call.v1",
            "id": "base-call",
            "provider": "local",
            "to": "fixture://agent",
            "timeout_seconds": 1,
            "record": True,
            "metadata": {},
        },
        "stages": [
            {
                "name": "stress",
                "phase": "stress",
                "model": "closed",
                "concurrency": 1,
                "calls": 2,
            }
        ],
        "terminal_timeout_seconds": 1,
        "poll_seconds": 0.001,
        "safety": {"max_calls": 2},
        "faults": faults or [],
        "slos": slos or {"min_lifecycle_completion_rate": 1.0},
    }


def _run(tmp_path: Path, *, plan=None):
    plan_path = tmp_path / "plan.json"
    plan_path.write_text(json.dumps(plan or _plan()), encoding="utf-8")
    output = tmp_path / "result"
    result = loadtest.run(
        str(plan_path),
        str(output),
        client=_FaultCapableLocalClient(),
    )
    assert result.verification["ok"] is True
    return output, result.summary


def _rewrite_summary(output: Path, mutate):
    path = output / "summary.json"
    summary = json.loads(path.read_text(encoding="utf-8"))
    mutate(summary)
    unsigned = dict(summary)
    unsigned.pop("result_id", None)
    summary["result_id"] = loadtest._sha(loadtest._canonical(unsigned))
    path.write_bytes(loadtest._canonical(summary))
    return summary


def test_verify_refuses_forged_pass_and_exit_zero(tmp_path):
    output, original = _run(
        tmp_path,
        plan=_plan(slos={"min_evidence_complete_rate": 1.0}),
    )
    assert original["status"] == "FAIL"
    assert original["exit_code"] == 1

    _rewrite_summary(
        output,
        lambda summary: summary.update({"status": "PASS", "exit_code": 0}),
    )

    verification = loadtest.verify(str(output))
    assert verification["ok"] is False
    assert any(
        marker in verification["mismatches"]
        for marker in ("status:recompute", "exit_code:recompute")
    )


def test_verify_refuses_recovery_tamper_even_when_result_id_is_rehashed(tmp_path):
    output, original = _run(
        tmp_path,
        plan=_plan(
            faults=[
                {
                    "stage": "stress",
                    "after_call": 0,
                    "kind": "provider_unavailable",
                    "duration_calls": 1,
                }
            ]
        ),
    )
    assert original["recovery"]["measurements"]

    def mutate(summary):
        summary["recovery"] = {
            "measurements": [
                {
                    "stage": "stress",
                    "kind": "provider_unavailable",
                    "fault_end_call": 1,
                    "recovery_seconds": 999999.0,
                }
            ],
            "max_seconds": 999999.0,
        }

    _rewrite_summary(output, mutate)

    verification = loadtest.verify(str(output))
    assert verification["ok"] is False
    assert "recovery:recompute" in verification["mismatches"]


def test_verify_refuses_per_stage_metric_tamper_even_when_result_id_is_rehashed(
    tmp_path,
):
    output, _original = _run(tmp_path)

    def mutate(summary):
        summary["stages"][0]["metrics"]["scheduled"] = 2000000

    _rewrite_summary(output, mutate)

    verification = loadtest.verify(str(output))
    assert verification["ok"] is False
    assert "stages:recompute" in verification["mismatches"]


def test_verify_binds_result_to_normalized_plan_and_fault_schedule(tmp_path):
    output, _original = _run(
        tmp_path,
        plan=_plan(
            faults=[
                {
                    "stage": "stress",
                    "after_call": 0,
                    "kind": "provider_unavailable",
                    "duration_calls": 1,
                }
            ]
        ),
    )

    # Relabeling the package as a different plan currently needs no change to
    # observations because neither the normalized plan nor its digest is bound
    # into the published artifact set.
    _rewrite_summary(
        output,
        lambda summary: summary.update({"plan_id": "different-plan"}),
    )

    verification = loadtest.verify(str(output))
    assert verification["ok"] is False
    assert "plan_id:binding" in verification["mismatches"]


@pytest.mark.skipif(not hasattr(os, "mkfifo"), reason="FIFOs require POSIX")
def test_verify_refuses_fifo_summary_without_blocking(tmp_path):
    output, _ = _run(tmp_path)
    summary = output / "summary.json"
    summary.unlink()
    os.mkfifo(summary)
    verification = loadtest.verify(str(output))
    assert verification["ok"] is False
    assert verification["mismatches"] == ["summary.json:invalid"]


def test_verify_refuses_malformed_child_manifest_without_crashing(tmp_path):
    output, _ = _run(tmp_path)
    child = sorted((output / "calls").iterdir())[0]
    (child / "manifest.json").write_text("[]", encoding="utf-8")
    verification = loadtest.verify(str(output))
    assert verification["ok"] is False
    assert any("manifest-contract" in item for item in verification["mismatches"])


def test_verify_refuses_swapped_child_references_after_rehash(tmp_path):
    output, _ = _run(tmp_path)
    observations = output / "observations.jsonl"
    rows = [json.loads(line) for line in observations.read_text().splitlines()]
    rows[0]["child"], rows[1]["child"] = rows[1]["child"], rows[0]["child"]
    raw = b"".join(loadtest._canonical(row) for row in rows)
    observations.write_bytes(raw)

    def mutate(summary):
        summary["artifacts"]["observations_sha256"] = loadtest._sha(raw)

    _rewrite_summary(output, mutate)
    verification = loadtest.verify(str(output))
    assert verification["ok"] is False
    assert any("child-path" in item for item in verification["mismatches"])


def test_verify_recomputes_strict_evidence_after_all_outer_digests_are_rehashed(
    tmp_path,
):
    class _EvidenceClient(_FaultCapableLocalClient):
        def evidence(self, handle):
            call_hash = loadtest._call_id_hash(handle.provider, handle.call_id)
            return {
                "schema": "hotato.load-evidence.v1",
                "provider": handle.provider,
                "call_id_sha256": call_hash,
                "lanes": {
                    name: {
                        "state": "PRESENT",
                        "authority": "measured",
                        "sha256": "sha256:" + (str(index) * 64),
                        "eligible_for_execution_claim": True,
                    }
                    for index, name in enumerate(
                        ("delivered_audio", "tool_trace", "backend_state"), 1
                    )
                },
            }

    plan_path = tmp_path / "plan.json"
    plan_path.write_text(json.dumps(_plan()), encoding="utf-8")
    output = tmp_path / "result"
    loadtest.run(str(plan_path), str(output), client=_EvidenceClient())

    child = sorted((output / "calls").iterdir())[0]
    export_path = child / "provider-export.json"
    export_path.write_bytes(
        loadtest._canonical(
            {
                "evidence": {
                    "delivered_audio": "PRESENT",
                    "tool_trace": "PRESENT",
                    "backend_state": "PRESENT",
                }
            }
        )
    )
    manifest_path = child / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["artifacts"]["provider-export.json"] = loadtest._sha(
        export_path.read_bytes()
    )
    manifest.pop("package_id")
    manifest["package_id"] = loadtest._sha(loadtest._canonical(manifest))
    manifest_path.write_bytes(loadtest._canonical(manifest))

    observations = output / "observations.jsonl"
    rows = [json.loads(line) for line in observations.read_text().splitlines()]
    rows[0]["child"]["package_id"] = manifest["package_id"]
    observation_bytes = b"".join(loadtest._canonical(row) for row in rows)
    observations.write_bytes(observation_bytes)
    _rewrite_summary(
        output,
        lambda summary: summary["artifacts"].update(
            {"observations_sha256": loadtest._sha(observation_bytes)}
        ),
    )

    verification = loadtest.verify(str(output))
    assert verification["ok"] is False
    assert any("evidence-export" in item for item in verification["mismatches"])


def test_verify_binds_each_child_to_workload_and_call_spec_digests(tmp_path):
    output, _ = _run(tmp_path)
    verification_path = output / "verification-plan.json"
    verification_plan = json.loads(verification_path.read_text(encoding="utf-8"))
    forged_call_digest = "sha256:" + "f" * 64
    verification_plan["workload"]["call_spec_sha256"] = forged_call_digest
    verification_bytes = loadtest._canonical(verification_plan)
    verification_path.write_bytes(verification_bytes)

    def mutate(summary):
        summary["call_spec_sha256"] = forged_call_digest
        summary["artifacts"]["verification_plan_sha256"] = loadtest._sha(
            verification_bytes
        )

    _rewrite_summary(output, mutate)
    verification = loadtest.verify(str(output))
    assert verification["ok"] is False
    assert any("call-spec-binding" in item for item in verification["mismatches"])
