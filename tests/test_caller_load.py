from __future__ import annotations

import hashlib
import json
import multiprocessing
import os
import time
from pathlib import Path

import pytest

from hotato import caller, caller_load
from hotato.caller_session import PROTOCOL_SCHEMA, WebSocketCallerSession
from hotato.websocket_transport import WebSocketMessage, WebSocketTimeout


class Session:
    def __init__(self, *, context=None, delay=0.0, evidence="PRESENT", supported=True):
        self.context = context or {}
        self.delay = delay
        self.evidence_state = evidence
        self.supported = supported
        self.submitted = []

    def capabilities(self):
        state = caller.SUPPORTED if self.supported else caller.UNSUPPORTED
        return {"send_text": state, "send_audio": state, "hangup": state}

    def send_text(self, text, metadata):
        if self.delay:
            time.sleep(self.delay)

    def hangup(self, reason):
        return None

    def send_audio(self, pcm_s16le, sample_rate_hz, metadata):
        self.submitted.append(metadata["pcm_sha256"])
        if self.delay:
            time.sleep(self.delay)

    def evidence(self):
        if self.evidence_state != "PRESENT":
            return {"delivery_evidence": {"state": self.evidence_state}}
        submitted = self.submitted[0] if self.submitted else "sha256:" + "0" * 64
        return {
            "delivery_evidence": {
                "schema": caller_load.DELIVERED_AUDIO_EVENT,
                "state": self.evidence_state,
                "authority": "target_boundary",
                "submitted_sha256": submitted,
                "delivered_sha256": "sha256:" + "2" * 64,
                "workload_child_id": self.context.get("child_id"),
                "workload_plan_sha256": self.context.get("workload_plan_sha256"),
            }
        }


class EventDeliverySession(Session):
    def __init__(self, context):
        super().__init__(context=context, evidence="UNOBSERVABLE")
        self.pending = []

    def send_audio(self, pcm_s16le, sample_rate_hz, metadata):
        super().send_audio(pcm_s16le, sample_rate_hz, metadata)
        self.pending.append({
            "kind": "custom",
            "custom_type": caller_load.DELIVERED_AUDIO_EVENT,
            "authority": "target_boundary",
            "submitted_sha256": metadata["pcm_sha256"],
            "delivered_sha256": "sha256:" + "d" * 64,
            "workload_child_id": self.context["child_id"],
            "workload_plan_sha256": self.context["workload_plan_sha256"],
        })

    def drain_events(self):
        events, self.pending = self.pending, []
        return events


class EndpointSession(Session):
    def __init__(self, context, *, mismatch=False):
        super().__init__(context=context, evidence="UNOBSERVABLE")
        expected = context["expected_session_boundary"]["endpoint_sha256"]
        self.endpoint_digest = "sha256:" + "f" * 64 if mismatch else expected

    def evidence(self):
        return {
            "availability": caller.UNOBSERVABLE,
            "connected_endpoint_sha256": self.endpoint_digest,
        }


class LoadWebSocket:
    def __init__(self, context, nonce):
        self.context = context
        self.incoming = [WebSocketMessage("text", json.dumps({
            "schema": PROTOCOL_SCHEMA, "type": "ready", "nonce": nonce,
            "capabilities": {
                "send_text": caller.UNSUPPORTED,
                "send_audio": caller.SUPPORTED,
                "receive": caller.SUPPORTED,
                "send_dtmf": caller.UNSUPPORTED,
                "wait": caller.UNSUPPORTED,
                "silence": caller.UNSUPPORTED,
                "impairment": caller.UNSUPPORTED,
                "observe_transfer": caller.UNOBSERVABLE,
                "hangup": caller.SUPPORTED,
            },
            "adapter": {"name": "load-fixture", "version": "1"},
        }))]

    def send_text(self, value):
        parsed = json.loads(value)
        if parsed.get("type") != "command":
            return
        if parsed["command"] == "send_audio":
            self.incoming.append(WebSocketMessage("text", json.dumps({
                "schema": PROTOCOL_SCHEMA, "type": "event", "event": {
                    "kind": "custom",
                    "custom_type": caller_load.DELIVERED_AUDIO_EVENT,
                    "authority": "target_boundary",
                    "submitted_sha256": parsed["payload"]["sha256"],
                    "delivered_sha256": "sha256:" + "e" * 64,
                    "workload_child_id": self.context["child_id"],
                    "workload_plan_sha256": self.context["workload_plan_sha256"],
                },
            })))
        self.incoming.append(WebSocketMessage("text", json.dumps({
            "schema": PROTOCOL_SCHEMA, "type": "command_result",
            "sequence": parsed["sequence"], "command": parsed["command"],
            "status": "completed", "receipt": {"accepted": True},
        })))

    def send_binary(self, value):
        return None

    def receive(self):
        if not self.incoming:
            raise WebSocketTimeout("no fixture message")
        return self.incoming.pop(0)

    def set_timeout(self, value):
        return None

    def close(self, *args):
        return None

    def abort(self):
        return None


def normal_session(context):
    return Session(context=context)


def unobservable_session(context):
    return Session(context=context, evidence="UNOBSERVABLE")


def blocked_session(context):
    return Session(context=context, supported=False)


def slow_session(context):
    return Session(context=context, delay=0.25)


def event_delivery_session(context):
    return EventDeliverySession(context)


def endpoint_session(context):
    return EndpointSession(context)


def mismatched_endpoint_session(context):
    return EndpointSession(context, mismatch=True)


def websocket_delivery_session(context):
    nonce = "load-" + context["child_id"][:32]
    fake = LoadWebSocket(context, nonce)
    return WebSocketCallerSession(
        "ws://127.0.0.1:9000/caller",
        nonce=nonce,
        connector=lambda _endpoint, **_kwargs: fake,
    )


# Worker factories cross a multiprocessing.Process boundary.  Windows has no
# fork, so the spawn start method pickles them by module-qualified reference
# (multiprocessing "Programming guidelines"): every factory and session class
# a child receives must live at module level, never nested in a test body.
class NoDigestSession(Session):
    def evidence(self):
        return {"delivery_evidence": {"state": "PRESENT"}}


def no_digest_session(_context):
    return NoDigestSession()


class MisleadingDigestSession(Session):
    def evidence(self):
        return {
            "delivery_evidence": {
                "state": "PRESENT",
                "configured_impairment_sha256": "sha256:" + "3" * 64,
            },
            "adapter_sha256": "sha256:" + "4" * 64,
        }


def misleading_digest_session(_context):
    return MisleadingDigestSession()


class MalformedEventSession(EventDeliverySession):
    def send_audio(self, pcm_s16le, sample_rate_hz, metadata):
        super().send_audio(pcm_s16le, sample_rate_hz, metadata)
        self.pending[0].pop("workload_child_id")


def malformed_event_session(context):
    return MalformedEventSession(context)


class Model:
    def propose(self, request):
        return {
            "proposal": {"action": "say", "text": "I need to change my reservation."},
            "raw": "{\"action\":\"say\",\"text\":\"I need to change my reservation.\"}",
            "provider": "fixture", "model": "caller-model-v1",
            "parameters": {"temperature": 0, "seed": 1},
            "usage": {"input_tokens": 4, "output_tokens": 6, "cost_microusd": 0},
        }


class TTS:
    def synthesize(self, text):
        return {
            "pcm_s16le": b"\x01\x00" * 100,
            "sample_rate_hz": 16000,
            "provider": "fixture", "model": "tts-v1", "voice": "caller-a",
            "settings": {"speed": 1},
        }


def model_factory(context):
    return Model()


def tts_factory(context):
    return TTS()


def _caller_plan(*, duration_ms=2_000, cost=0):
    return {
        "schema": caller.PLAN_SCHEMA,
        "id": "load-caller",
        "mode": "scripted",
        "start": "say",
        "nodes": [
            {"id": "say", "type": "say", "text": "Please check my booking.", "next": "done"},
            {"id": "done", "type": "hangup", "reason": "scenario_complete"},
        ],
        "limits": {"max_duration_ms": duration_ms, "max_cost_microusd": cost},
    }


def _plan(stages, *, slos=None, safety=None, caller_plan=None):
    return {
        "schema": caller_load.PLAN_SCHEMA,
        "id": "checkout-load",
        "caller_plan": caller_plan or _caller_plan(),
        "stages": stages,
        "slos": slos or {},
        "safety": safety or {},
    }


def _run_caller_load(*args, **kwargs):
    kwargs.setdefault(
        "execution_scope", "remote" if kwargs.get("remote_endpoint") else "local"
    )
    return caller_load.run_caller_load(*args, **kwargs)


def _canonical(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()


def _digest(data):
    return "sha256:" + hashlib.sha256(data).hexdigest()


def _rebuild_manifest(root):
    root = Path(root)
    result = json.loads((root / "result.json").read_text())
    files = {}
    for path in sorted(root.rglob("*")):
        if path.is_file() and path != root / "package-manifest.json":
            data = path.read_bytes()
            files[path.relative_to(root).as_posix()] = {"bytes": len(data), "sha256": _digest(data)}
    manifest = {
        "schema": caller_load.PACKAGE_SCHEMA,
        "result_id": result["result_id"],
        "files": files,
    }
    manifest["package_id"] = _digest(_canonical(manifest))
    (root / "package-manifest.json").write_bytes(_canonical(manifest) + b"\n")


def _rewrite_result(root, transform):
    path = Path(root) / "result.json"
    result = json.loads(path.read_text())
    transform(result)
    result.pop("result_id", None)
    result["result_id"] = _digest(_canonical(result))
    path.write_bytes(_canonical(result) + b"\n")
    _rebuild_manifest(root)


def test_closed_concurrency_writes_one_bound_verified_package_per_start(tmp_path):
    plan = _plan(
        [{"id": "steady", "model": "closed", "concurrency": 2, "calls": 4}],
        slos={
            "min_completion_rate": 1,
            "min_child_verification_rate": 1,
            "min_evidence_complete_rate": 1,
        },
    )
    run = _run_caller_load(
        plan, str(tmp_path / "run"), normal_session,
        tts_factory=tts_factory,
        created_at="2026-07-17T00:00:00Z",
    )

    assert run.exit_code == 0
    assert run.result["status"] == "PASS"
    assert run.verification["ok"]
    metrics = run.result["metrics"]
    assert metrics["caller_status"] == {
        "completed": 0, "hung_up": 4, "blocked": 0, "error": 0,
    }
    assert metrics["child_verification"] == {"verified": 4, "unverified": 0}
    assert metrics["delivery_evidence"] == {
        "present": 4, "missing": 0, "unsupported": 0, "unobservable": 0,
    }
    children = list((Path(run.output_dir) / "children").iterdir())
    assert len(children) == 4
    for child in children:
        assert caller.verify_package(str(child))["ok"]
        child_plan = json.loads((child / "caller-plan.json").read_text())
        binding = child_plan["metadata"]["caller_load"]
        assert binding["child_id"] == child.name
        assert binding["workload_plan_sha256"] == run.result["workload_plan_sha256"]


def test_run_and_verify_refuse_symlink_output_root(tmp_path):
    target = tmp_path / "target"
    target.mkdir()
    link = tmp_path / "load-link"
    try:
        link.symlink_to(target, target_is_directory=True)
    except (OSError, NotImplementedError):
        pytest.skip("symlink creation unavailable")
    workload = _plan(
        [{"id": "steady", "model": "closed", "concurrency": 1, "calls": 1}]
    )
    with pytest.raises(ValueError, match="non-symlink directory"):
        _run_caller_load(workload, str(link), normal_session)
    assert caller_load.verify_caller_load(str(link)) == {
        "ok": False,
        "package_id": None,
        "mismatches": ["output:invalid"],
    }


def test_spawn_process_context_supports_module_level_factories(tmp_path, monkeypatch):
    monkeypatch.setattr(
        caller_load,
        "_process_context",
        lambda: multiprocessing.get_context("spawn"),
    )
    workload = _plan(
        [{"id": "portable", "model": "closed", "concurrency": 1, "calls": 1}]
    )
    run = _run_caller_load(
        workload,
        str(tmp_path / "spawn-run"),
        normal_session,
        created_at="2026-07-17T00:00:00Z",
    )
    assert run.verification["ok"]
    assert run.result["metrics"]["started"] == 1


def test_open_arrival_reports_capacity_drops_without_coordinated_omission(tmp_path):
    plan = _plan(
        [{
            "id": "burst", "model": "open", "arrival_rate_per_second": 100,
            "duration_seconds": 0.04, "max_in_flight": 1,
        }],
        safety={"max_call_duration_ms": 1_000, "max_start_delay_ms": 100},
    )
    run = _run_caller_load(plan, str(tmp_path / "open"), slow_session)

    metrics = run.result["metrics"]
    assert metrics["scheduled"] == 4
    assert metrics["started"] == 1
    assert metrics["dropped_starts"] == 3
    observations = [json.loads(line) for line in Path(run.output_dir, "observations.jsonl").read_text().splitlines()]
    assert {row.get("drop_reason") for row in observations if row["disposition"] == "DROPPED"} == {"MAX_IN_FLIGHT"}
    assert all("scheduled_offset_ms" in row and "scheduling_delay_ms" in row for row in observations)
    assert run.verification["ok"]


def test_completion_package_integrity_and_delivery_evidence_are_separate(tmp_path):
    run = _run_caller_load(
        _plan([{"id": "one", "model": "closed", "concurrency": 1, "calls": 1}]),
        str(tmp_path / "separate"), unobservable_session,
    )
    metrics = run.result["metrics"]
    assert metrics["caller_status"]["hung_up"] == 1
    assert metrics["child_verification"]["verified"] == 1
    assert metrics["delivery_evidence"]["unobservable"] == 1
    assert metrics["rates"]["evidence_complete_rate"] == 0
    assert run.result["semantics"]["caller_completion_is_agent_quality_pass"] is False
    assert run.result["semantics"]["blended_score"] is None


def test_model_and_tts_factories_run_per_isolated_child(tmp_path):
    generated = {
        "schema": caller.PLAN_SCHEMA, "id": "generated", "mode": "generative",
        "start": "respond",
        "nodes": [
            {"id": "respond", "type": "generate", "prompt": "Ask to reschedule.", "allowed_actions": ["say"], "next": "done"},
            {"id": "done", "type": "hangup"},
        ],
    }
    run = _run_caller_load(
        _plan(
            [{"id": "generated", "model": "closed", "concurrency": 1, "calls": 1}],
            caller_plan=generated,
        ),
        str(tmp_path / "generated"), normal_session,
        model_factory=model_factory, tts_factory=tts_factory,
    )
    observation = json.loads(Path(run.output_dir, "observations.jsonl").read_text())
    child_result = json.loads(Path(run.output_dir, observation["package_path"], "caller-result.json").read_text())
    assert child_result["model_calls"][0]["provider"] == "fixture"
    assert child_result["actions"][0]["delivery"] == "audio"
    assert child_result["actions"][0]["pcm_sha256"].startswith("sha256:")
    assert run.verification["ok"]


def test_declared_present_evidence_without_content_identity_counts_missing(tmp_path):
    run = _run_caller_load(
        _plan([{"id": "one", "model": "closed", "concurrency": 1, "calls": 1}]),
        str(tmp_path / "no-digest"), no_digest_session,
    )
    assert run.result["metrics"]["delivery_evidence"]["present"] == 0
    assert run.result["metrics"]["delivery_evidence"]["missing"] == 1
    assert run.verification["ok"]


def test_unrelated_digest_cannot_substitute_for_target_delivery_receipt(tmp_path):
    run = _run_caller_load(
        _plan([{"id": "one", "model": "closed", "concurrency": 1, "calls": 1}]),
        str(tmp_path / "unrelated-digest"),
        misleading_digest_session,
    )
    assert run.result["metrics"]["delivery_evidence"]["present"] == 0
    assert run.result["metrics"]["delivery_evidence"]["missing"] == 1
    assert run.verification["ok"]


def test_target_boundary_custom_event_can_supply_delivery_receipt(tmp_path):
    run = _run_caller_load(
        _plan(
            [{"id": "one", "model": "closed", "concurrency": 1, "calls": 1}],
            slos={"min_evidence_complete_rate": 1},
        ),
        str(tmp_path / "event-receipt"),
        event_delivery_session,
        tts_factory=tts_factory,
    )
    child = next(Path(run.output_dir, "children").iterdir())
    child_result = json.loads((child / "caller-result.json").read_text())
    assert child_result["events"][0]["custom_type"] == caller_load.DELIVERED_AUDIO_EVENT
    assert run.result["metrics"]["delivery_evidence"] == {
        "present": 1, "missing": 0, "unsupported": 0, "unobservable": 0,
    }
    assert run.result["status"] == "PASS"
    assert run.verification["ok"]


def test_builtin_websocket_preserves_interleaved_delivery_event_for_load(tmp_path):
    run = _run_caller_load(
        _plan(
            [{"id": "one", "model": "closed", "concurrency": 1, "calls": 1}],
            slos={"min_evidence_complete_rate": 1},
        ),
        str(tmp_path / "websocket-event-receipt"),
        websocket_delivery_session,
        tts_factory=tts_factory,
    )
    assert run.result["metrics"]["delivery_evidence"]["present"] == 1
    assert run.result["status"] == "PASS"
    assert run.verification["ok"]


def test_malformed_delivery_event_remains_missing(tmp_path):
    run = _run_caller_load(
        _plan([{"id": "one", "model": "closed", "concurrency": 1, "calls": 1}]),
        str(tmp_path / "malformed-event"),
        malformed_event_session,
        tts_factory=tts_factory,
    )
    assert run.result["metrics"]["delivery_evidence"]["missing"] == 1
    assert run.result["metrics"]["delivery_evidence"]["present"] == 0
    assert run.verification["ok"]


def test_slo_failure_is_recomputed_from_child_packages(tmp_path):
    run = _run_caller_load(
        _plan(
            [{"id": "blocked", "model": "closed", "concurrency": 1, "calls": 2}],
            slos={"min_completion_rate": 1, "max_blocked_error_rate": 0},
        ),
        str(tmp_path / "failed"), blocked_session,
    )
    assert run.result["status"] == "FAIL"
    assert run.exit_code == 1
    assert run.result["metrics"]["caller_status"]["blocked"] == 2
    assert run.verification["ok"]


def test_stop_file_prevents_starts_and_is_inconclusive(tmp_path):
    stop = tmp_path / "STOP"
    stop.write_text("stop")
    run = _run_caller_load(
        _plan(
            [{"id": "stop", "model": "closed", "concurrency": 2, "calls": 3}],
            safety={"stop_file": str(stop)},
        ),
        str(tmp_path / "stopped"), normal_session,
    )
    assert run.result["metrics"]["started"] == 0
    assert run.result["metrics"]["stopped_before_start"] == 3
    assert run.result["status"] == "INCONCLUSIVE"
    assert run.exit_code == 2
    assert run.verification["ok"]


def test_supervisor_timeout_produces_a_verified_error_child(tmp_path):
    run = _run_caller_load(
        _plan(
            [{"id": "timeout", "model": "closed", "concurrency": 1, "calls": 1}],
            safety={"max_call_duration_ms": 50},
            caller_plan=_caller_plan(duration_ms=100),
        ),
        str(tmp_path / "timeout"), slow_session,
    )
    assert run.result["metrics"]["caller_status"]["error"] == 1
    observation = json.loads(Path(run.output_dir, "observations.jsonl").read_text())
    assert observation["supervisor_timeout"] is True
    assert observation["child_package_verified"] is True
    assert run.verification["ok"]


def test_workload_without_declared_slos_is_inconclusive(tmp_path):
    run = _run_caller_load(
        _plan([{"id": "one", "model": "closed", "concurrency": 1, "calls": 1}]),
        str(tmp_path / "no-slos"), normal_session,
    )
    assert run.result["status"] == "INCONCLUSIVE"
    assert run.exit_code == 2
    assert run.verification["ok"]


@pytest.mark.parametrize(
    "mutate,expected",
    [
        (lambda result: result.update(status="PASS"), "result:status"),
        (lambda result: result.update(exit_code=0), "result:exit_code"),
    ],
)
def test_offline_verifier_rejects_rehashed_forged_status_and_exit(tmp_path, mutate, expected):
    run = _run_caller_load(
        _plan(
            [{"id": "bad", "model": "closed", "concurrency": 1, "calls": 1}],
            slos={"min_completion_rate": 1},
        ),
        str(tmp_path / expected.replace(":", "-")), blocked_session,
    )
    _rewrite_result(run.output_dir, mutate)
    verification = caller_load.verify_caller_load(run.output_dir)
    assert not verification["ok"]
    assert expected in verification["mismatches"]


def test_offline_verifier_rejects_rehashed_child_stage_swap(tmp_path):
    run = _run_caller_load(
        _plan([
            {"id": "a", "model": "closed", "concurrency": 1, "calls": 1},
            {"id": "b", "model": "closed", "concurrency": 1, "calls": 1},
        ]),
        str(tmp_path / "swap"), normal_session,
    )
    rows = [json.loads(line) for line in Path(run.output_dir, "observations.jsonl").read_text().splitlines()]
    left = Path(run.output_dir, rows[0]["package_path"])
    right = Path(run.output_dir, rows[1]["package_path"])
    temporary = Path(run.output_dir, "children", "temporary-swap")
    left.rename(temporary)
    right.rename(left)
    temporary.rename(right)
    _rebuild_manifest(run.output_dir)
    verification = caller_load.verify_caller_load(run.output_dir)
    assert not verification["ok"]
    assert any("plan-binding" in mismatch for mismatch in verification["mismatches"])


def test_offline_verifier_rejects_rehashed_extra_file(tmp_path):
    run = _run_caller_load(
        _plan([{"id": "one", "model": "closed", "concurrency": 1, "calls": 1}]),
        str(tmp_path / "extra"), normal_session,
    )
    Path(run.output_dir, "unclaimed.txt").write_text("not part of the contract")
    _rebuild_manifest(run.output_dir)
    verification = caller_load.verify_caller_load(run.output_dir)
    assert not verification["ok"]
    assert "output:unexpected-or-missing-file" in verification["mismatches"]


def test_verifier_refuses_fifo_without_blocking(tmp_path):
    if not hasattr(os, "mkfifo"):
        pytest.skip("FIFO unavailable")
    run = _run_caller_load(
        _plan([{"id": "one", "model": "closed", "concurrency": 1, "calls": 1}]),
        str(tmp_path / "fifo"), normal_session,
    )
    result_path = Path(run.output_dir, "result.json")
    result_path.unlink()
    os.mkfifo(result_path)
    started = time.monotonic()
    verification = caller_load.verify_caller_load(run.output_dir)
    assert time.monotonic() - started < 1
    assert not verification["ok"]


def test_hard_plan_bounds_refuse_calls_concurrency_duration_and_cost():
    with pytest.raises(ValueError, match="max_calls"):
        caller_load.validate_plan(_plan(
            [{"id": "x", "model": "closed", "concurrency": 1, "calls": 2}],
            safety={"max_calls": 1},
        ))
    with pytest.raises(ValueError, match="max_concurrency"):
        caller_load.validate_plan(_plan(
            [{"id": "x", "model": "closed", "concurrency": 2, "calls": 2}],
            safety={"max_concurrency": 1},
        ))
    with pytest.raises(ValueError, match="max_call_duration_ms"):
        caller_load.validate_plan(_plan(
            [{"id": "x", "model": "closed", "concurrency": 1, "calls": 1}],
            safety={"max_call_duration_ms": 2_001},
        ))
    with pytest.raises(ValueError, match="cost"):
        caller_load.validate_plan(_plan(
            [{"id": "x", "model": "closed", "concurrency": 1, "calls": 2}],
            safety={"max_cost_per_call_microusd": 10, "max_cost_microusd": 10},
            caller_plan=_caller_plan(cost=10),
        ))


def test_remote_load_requires_exact_endpoint_call_bound_and_fixed_ack(tmp_path):
    stages = [{"id": "remote", "model": "closed", "concurrency": 1, "calls": 2}]
    endpoint = "wss://voice.example.test/caller"
    remote = {
        "endpoint": endpoint,
        "max_calls": 2,
        "external_cost_state": "UNOBSERVABLE",
        "acknowledgement": caller_load.REMOTE_ACKNOWLEDGEMENT,
    }
    declared = _plan(stages, safety={"remote_execution": remote})

    with pytest.raises(ValueError, match="requires remote_endpoint"):
        caller_load.run_caller_load(
            declared,
            str(tmp_path / "missing-runtime"),
            normal_session,
            execution_scope="remote",
        )
    assert not (tmp_path / "missing-runtime").exists()

    with pytest.raises(ValueError, match="outside the plan allowlist"):
        _run_caller_load(
            declared,
            str(tmp_path / "wrong-endpoint"),
            normal_session,
            remote_endpoint="wss://other.example.test/caller",
        )
    assert not (tmp_path / "wrong-endpoint").exists()

    run = _run_caller_load(
        declared,
        str(tmp_path / "remote-bound"),
        normal_session,
        remote_endpoint=endpoint,
    )
    assert run.result["execution_boundary"] == {
        "transport": "websocket_sidecar",
        "network": "remote",
        "plan_declared_endpoint_sha256": _digest(endpoint.encode()),
        "runtime_configured_endpoint_sha256": _digest(endpoint.encode()),
        "external_provider_cost_state": "UNOBSERVABLE",
        "external_provider_cost_microusd": None,
    }
    assert run.result["safety"]["cost_bound_scope"] == "caller_model_reported_only"
    assert run.verification["ok"]


def test_remote_load_refuses_implicit_or_underbounded_execution(tmp_path):
    stages = [{"id": "remote", "model": "closed", "concurrency": 1, "calls": 2}]
    with pytest.raises(ValueError, match="requires safety.remote_execution"):
        _run_caller_load(
            _plan(stages),
            str(tmp_path / "implicit-remote"),
            normal_session,
            remote_endpoint="wss://voice.example.test/caller",
        )
    with pytest.raises(ValueError, match="execution_scope"):
        caller_load.run_caller_load(
            _plan(stages), str(tmp_path / "no-scope"), normal_session
        )
    with pytest.raises(ValueError, match="remote_execution.max_calls"):
        caller_load.validate_plan(_plan(stages, safety={
            "remote_execution": {
                "endpoint": "wss://voice.example.test/caller",
                "max_calls": 1,
                "external_cost_state": "UNOBSERVABLE",
                "acknowledgement": caller_load.REMOTE_ACKNOWLEDGEMENT,
            }
        }))
    with pytest.raises(ValueError, match="acknowledgement"):
        caller_load.validate_plan(_plan(stages, safety={
            "remote_execution": {
                "endpoint": "wss://voice.example.test/caller",
                "max_calls": 2,
                "external_cost_state": "UNOBSERVABLE",
                "acknowledgement": "yes",
            }
        }))


def test_remote_children_must_bind_the_connected_endpoint_and_verifier_recomputes(tmp_path):
    endpoint = "wss://voice.example.test/caller"
    remote = {
        "endpoint": endpoint,
        "max_calls": 1,
        "external_cost_state": "UNOBSERVABLE",
        "acknowledgement": caller_load.REMOTE_ACKNOWLEDGEMENT,
    }
    workload = _plan(
        [{"id": "remote", "model": "closed", "concurrency": 1, "calls": 1}],
        safety={"remote_execution": remote},
        slos={"min_completion_rate": 1},
    )
    matched = _run_caller_load(
        workload, str(tmp_path / "remote-matched"), endpoint_session,
        remote_endpoint=endpoint,
    )
    assert matched.result["metrics"]["session_endpoint_binding"] == {
        "matched": 1, "missing": 0, "mismatch": 0, "not_required": 0,
        "required": 1,
    }
    assert matched.result["metrics"]["rates"]["session_endpoint_match_rate"] == 1
    assert matched.result["status"] == "PASS"

    missing = _run_caller_load(
        workload, str(tmp_path / "remote-missing"), normal_session,
        remote_endpoint=endpoint,
    )
    assert missing.result["metrics"]["session_endpoint_binding"]["missing"] == 1
    assert missing.result["status"] == "INCONCLUSIVE"
    assert missing.exit_code == 2

    mismatch = _run_caller_load(
        workload, str(tmp_path / "remote-mismatch"), mismatched_endpoint_session,
        remote_endpoint=endpoint,
    )
    assert mismatch.result["metrics"]["session_endpoint_binding"]["mismatch"] == 1
    assert mismatch.result["status"] == "INCONCLUSIVE"

    observations = Path(matched.output_dir, "observations.jsonl")
    row = json.loads(observations.read_text())
    row["session_endpoint_state"] = "MISMATCH"
    observations.write_bytes(_canonical(row) + b"\n")
    _rebuild_manifest(matched.output_dir)
    verification = caller_load.verify_caller_load(matched.output_dir)
    assert not verification["ok"]
    assert "observation:0:session_endpoint_state" in verification["mismatches"]
def test_shipped_schemas_accept_normalized_plan_and_result(tmp_path):
    jsonschema = pytest.importorskip("jsonschema")
    root = Path(__file__).parents[1] / "src/hotato/schema"
    normalized = caller_load.validate_plan(_plan([
        {"id": "closed", "model": "closed", "concurrency": 1, "calls": 1},
        {"id": "open", "model": "open", "arrival_rate_per_second": 2, "duration_seconds": 0.5, "max_in_flight": 1},
    ]))
    plan_schema = json.loads((root / "caller-load-plan.v1.json").read_text())
    jsonschema.Draft7Validator(plan_schema).validate(normalized)
    run = _run_caller_load(normalized, str(tmp_path / "schema"), normal_session)
    result_schema = json.loads((root / "caller-load-result.v1.json").read_text())
    jsonschema.Draft7Validator(result_schema).validate(run.result)
    assert run.verification["ok"]
