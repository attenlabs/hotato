from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from hotato import caller

ALL_CAPABILITIES = {
    "send_text": caller.SUPPORTED,
    "send_audio": caller.SUPPORTED,
    "receive": caller.SUPPORTED,
    "send_dtmf": caller.SUPPORTED,
    "wait": caller.SUPPORTED,
    "silence": caller.SUPPORTED,
    "impairment": caller.SUPPORTED,
    "observe_transfer": caller.SUPPORTED,
    "hangup": caller.SUPPORTED,
}


class FakeSession:
    def __init__(self, events=(), capabilities=None):
        self.events = list(events)
        self.operations = []
        self._capabilities = dict(ALL_CAPABILITIES if capabilities is None else capabilities)

    def capabilities(self):
        return self._capabilities

    def send_text(self, text, metadata):
        self.operations.append(("send_text", text, dict(metadata)))

    def send_audio(self, pcm_s16le, sample_rate_hz, metadata):
        self.operations.append(("send_audio", pcm_s16le, sample_rate_hz, dict(metadata)))

    def receive(self, timeout_ms):
        self.operations.append(("receive", timeout_ms))
        return self.events.pop(0) if self.events else None

    def send_dtmf(self, digits):
        self.operations.append(("send_dtmf", digits))

    def wait(self, duration_ms):
        self.operations.append(("wait", duration_ms))

    def silence(self, duration_ms):
        self.operations.append(("silence", duration_ms))

    def set_impairment(self, profile):
        self.operations.append(("impairment", dict(profile)))

    def hangup(self, reason):
        self.operations.append(("hangup", reason))


class FakeModel:
    def __init__(self, proposal=None, cost=0):
        self.calls = []
        self.proposal = proposal or {"action": "say", "text": "I need the refund receipt."}
        self.cost = cost

    def propose(self, request):
        self.calls.append(json.loads(json.dumps(request)))
        return {
            "proposal": self.proposal,
            "raw": json.dumps(self.proposal, sort_keys=True),
            "provider": "fixture",
            "model": "caller-fixture-v1",
            "parameters": {"temperature": 0, "seed": 7},
            "usage": {"input_tokens": 9, "output_tokens": 5, "cost_microusd": self.cost},
        }


class PoisonModel:
    def propose(self, request):
        raise AssertionError("frozen replay invoked a model")


class FakeTTS:
    def __init__(self):
        self.calls = []

    def synthesize(self, text):
        self.calls.append(text)
        return {
            "pcm_s16le": b"\x01\x00\xff\xff" * 20,
            "sample_rate_hz": 16000,
            "provider": "fixture",
            "model": "tone-free-fixture-v1",
            "voice": "participant-a",
            "settings": {"speed": 1.0},
        }


def plan(nodes, *, mode="scripted", start=None, **values):
    return {
        "schema": caller.PLAN_SCHEMA,
        "id": "caller-test",
        "mode": mode,
        "start": start or nodes[0]["id"],
        "nodes": nodes,
        **values,
    }


def node(node_id, kind, next_id=None, **values):
    result = {"id": node_id, "type": kind, **values}
    if next_id is not None:
        result["next"] = next_id
    return result


def test_stateful_hybrid_graph_executes_every_operation_and_binds_evidence(tmp_path):
    graph = plan([
        node("hello", "say", "heard", text="Hello."),
        node("heard", "listen", "expect-refund", timeout_ms=100, max_events=2,
             until={"event": "transcript", "text_regex": "refund"}),
        node("expect-refund", "expect", "remember", when={"event": "transcript", "text_regex": "refund"}),
        node("remember", "set_state", "route", key="journey.intent", value="refund"),
        node("route", "branch", cases=[{"when": {"actor_state": {"key": "journey.intent", "equals": "refund"}}, "next": "keys"}]),
        node("keys", "dtmf", "pause", digits="12#"),
        node("pause", "wait", "quiet", duration_ms=25),
        node("quiet", "silence", "network", duration_ms=40),
        node("network", "impairment", "transfer", profile={"id": "g711", "codec": "pcmu"}),
        node("transfer", "transfer_expect", "generate", timeout_ms=100, max_events=2),
        node("generate", "generate", "loop", prompt="Respond as the caller.", allowed_actions=["say"]),
        node("loop", "repeat_bounded", "done", target="echo", max_iterations=2),
        node("echo", "say", "loop", text="Still here."),
        node("done", "hangup", reason="scenario_complete"),
    ], mode="hybrid", limits={"max_cost_microusd": 10, "max_visits_per_node": 20})
    session = FakeSession([
        {"kind": "transcript", "text": "Please describe the refund you need."},
        {"kind": "transfer", "status": "completed", "target": "billing"},
    ])
    model = FakeModel(cost=4)

    run = caller.run_caller(
        graph, session, str(tmp_path / "run"), model=model,
        created_at="2026-07-17T00:00:00Z",
    )

    assert run.exit_code == 0
    assert run.result["status"] == "HUNG_UP"
    assert run.result["actor_state"] == {"journey": {"intent": "refund"}}
    assert run.result["authority"]["outcome"] == "not_evaluated"
    assert run.result["authority"]["caller_model"] == "proposal_only"
    assert [operation[0] for operation in session.operations] == [
        "send_text", "receive", "send_dtmf", "wait", "silence", "impairment",
        "receive", "send_text", "send_text", "send_text", "hangup",
    ]
    assert run.result["repeat_counts"] == {"loop": 2}
    assert run.result["model_calls"][0]["request_sha256"].startswith("sha256:")
    assert Path(run.output_dir, run.result["model_calls"][0]["request_path"]).is_file()
    assert run.verification["ok"]


@pytest.mark.parametrize(
    "event,trigger",
    [
        ({"kind": "tool_result", "tool": "refund", "status": "success"}, {"event": "tool_result", "tool": "refund", "status": "success"}),
        ({"kind": "state_snapshot", "data": {"subscription": {"status": "cancelled"}}}, {"event": "state_snapshot", "path": "subscription.status", "equals": "cancelled"}),
        ({"kind": "dtmf", "digits": "1"}, {"event": "dtmf", "digits": "1"}),
        ({"kind": "lifecycle", "status": "connected"}, {"event": "lifecycle", "status": "connected"}),
        ({"kind": "hold", "status": "started"}, {"event": "hold", "status": "started"}),
        ({"kind": "timing", "metric": "agent_latency_ms", "value": 450}, {"event": "timing", "metric": "agent_latency_ms", "gte": 400, "lte": 500}),
    ],
)
def test_event_trigger_families_route_without_model_judgment(tmp_path, event, trigger):
    graph = plan([
        node("listen", "listen", "expect", timeout_ms=10),
        node("expect", "expect", "pass", when=trigger, on_miss="fail"),
        node("pass", "hangup", reason="matched"),
        node("fail", "hangup", reason="missed"),
    ])
    session = FakeSession([event])
    run = caller.run_caller(graph, session, str(tmp_path / event["kind"]))
    assert run.exit_code == 0
    assert session.operations[-1] == ("hangup", "matched")


def test_model_can_only_propose_allowlisted_participant_action(tmp_path):
    graph = plan([
        node("model", "generate", prompt="Choose the next caller action.", allowed_actions=["say"]),
    ], mode="generative")
    model = FakeModel({"action": "set_state", "outcome": "passed"})
    session = FakeSession()

    run = caller.run_caller(graph, session, str(tmp_path / "refused"), model=model)

    assert run.exit_code == 1
    assert run.result["status"] == "BLOCKED"
    assert run.result["error"]["code"] == "MODEL_ACTION_REFUSED"
    assert session.operations == []
    assert run.result["actor_state"] == {}
    assert run.result["model_calls"][0]["status"] == "received"
    assert Path(run.output_dir, run.result["model_calls"][0]["raw_path"]).is_file()
    assert run.verification["ok"]


def test_model_and_tts_provenance_pcm_and_text_are_content_addressed(tmp_path):
    graph = plan([
        node("model", "generate", "done", prompt="Ask for a receipt.", allowed_actions=["say"]),
        node("done", "hangup", reason="complete"),
    ], mode="generative", limits={"max_cost_microusd": 0})
    model, tts, session = FakeModel(), FakeTTS(), FakeSession()

    run = caller.run_caller(graph, session, str(tmp_path / "audio"), model=model, tts=tts)
    action = run.result["actions"][0]

    assert run.exit_code == 0
    assert action["delivery"] == "audio"
    assert action["pcm_sha256"].startswith("sha256:")
    assert action["text_sha256"].startswith("sha256:")
    assert action["tts"] == {
        "provider": "fixture", "model": "tone-free-fixture-v1",
        "voice": "participant-a", "settings": {"speed": 1.0},
    }
    assert run.result["model_calls"][0]["parameters"] == {"temperature": 0, "seed": 7}
    assert session.operations[0][0] == "send_audio"
    assert run.verification["ok"]


def test_local_ollama_adapter_is_loopback_only_and_returns_auditable_proposal(tmp_path):
    requests = []

    def post(url, body, timeout):
        requests.append((url, json.loads(body), timeout))
        return json.dumps({
            "model": "caller-local:1",
            "message": {"role": "assistant", "content": json.dumps({"action": "say", "text": "Please repeat that."})},
            "prompt_eval_count": 20,
            "eval_count": 6,
        }).encode()

    model = caller.OllamaCallerModel(
        "caller-local:1", endpoint="http://localhost:11434", seed=11, post=post
    )
    run = caller.run_caller(
        plan([node("model", "generate", prompt="Ask for repetition.", allowed_actions=["say"])], mode="generative"),
        FakeSession(), str(tmp_path / "ollama"), model=model,
    )
    assert run.exit_code == 0
    assert requests[0][0] == "http://127.0.0.1:11434/api/chat"
    assert requests[0][1]["stream"] is False
    assert run.result["model_calls"][0]["usage"] == {
        "input_tokens": 20, "output_tokens": 6, "cost_microusd": 0,
    }
    assert run.result["model_calls"][0]["provider"] == "ollama-local"

    with pytest.raises(ValueError, match="loopback"):
        caller.OllamaCallerModel("model", endpoint="https://models.example.com")


def test_frozen_replay_uses_bound_pcm_and_never_invokes_model_or_tts(tmp_path):
    source_plan = plan([
        node("model", "generate", "done", prompt="Speak.", allowed_actions=["say"]),
        node("done", "hangup", reason="complete"),
    ], mode="generative")
    source_session = FakeSession()
    source = caller.run_caller(
        source_plan, source_session, str(tmp_path / "source"),
        model=FakeModel(), tts=FakeTTS(),
    )
    source_pcm = source_session.operations[0][1]
    replay_plan = {
        "schema": caller.PLAN_SCHEMA, "id": "frozen", "mode": "frozen_replay",
        "frozen_package": source.output_dir,
    }
    replay_session = FakeSession()

    replay = caller.run_caller(
        replay_plan, replay_session, str(tmp_path / "replay"), model=PoisonModel(),
    )

    assert replay.exit_code == 0
    assert replay.result["model_calls"] == []
    assert replay_session.operations[0][0:2] == ("send_audio", source_pcm)
    assert replay.result["source_package_id"] == source.verification["package_id"]
    assert replay.verification["ok"]


def test_tampered_source_package_is_refused_before_replay(tmp_path):
    source = caller.run_caller(
        plan([node("one", "say", text="Bound text.")]), FakeSession(), str(tmp_path / "source")
    )
    text_path = Path(source.output_dir, source.result["actions"][0]["text_path"])
    text_path.write_text("changed", encoding="utf-8")
    assert not caller.verify_package(source.output_dir)["ok"]
    replay_session = FakeSession()
    replay = caller.run_caller(
        {"schema": caller.PLAN_SCHEMA, "id": "replay", "mode": "frozen_replay", "frozen_package": source.output_dir},
        replay_session, str(tmp_path / "replay"),
    )
    assert replay.result["status"] == "BLOCKED"
    assert replay.result["error"]["code"] == "FROZEN_PACKAGE_INVALID"
    assert replay_session.operations == []


@pytest.mark.skipif(not hasattr(os, "mkfifo"), reason="FIFOs require POSIX")
def test_load_plan_and_package_reader_refuse_writerless_fifo(tmp_path):
    plan_fifo = tmp_path / "plan.json"
    os.mkfifo(plan_fifo)
    with pytest.raises(ValueError, match="regular file"):
        caller.load_plan(str(plan_fifo))

    artifact_fifo = tmp_path / "artifact.bin"
    os.mkfifo(artifact_fifo)
    with pytest.raises(ValueError, match="regular file"):
        caller._read_regular_bytes_no_follow(artifact_fifo, max_bytes=1024)


def test_package_reader_detects_path_replacement_between_check_and_open(
    tmp_path, monkeypatch
):
    target = tmp_path / "artifact.bin"
    replacement = tmp_path / "replacement.bin"
    target.write_bytes(b"first")
    replacement.write_bytes(b"second")
    real_open = caller.os.open
    swapped = False

    def swap_then_open(path, flags):
        nonlocal swapped
        if not swapped and os.fspath(path) == os.fspath(target):
            swapped = True
            target.unlink()
            replacement.rename(target)
        return real_open(path, flags)

    monkeypatch.setattr(caller.os, "open", swap_then_open)
    with pytest.raises(ValueError, match="changed while it was being opened"):
        caller._read_regular_bytes_no_follow(target, max_bytes=1024)


def test_run_and_verify_refuse_symlink_package_root(tmp_path):
    target = tmp_path / "target"
    target.mkdir()
    link = tmp_path / "package-link"
    try:
        link.symlink_to(target, target_is_directory=True)
    except (OSError, NotImplementedError):
        pytest.skip("symlink creation unavailable")
    graph = plan([node("one", "say", text="Bound text.")])
    with pytest.raises(ValueError, match="non-symlink directory"):
        caller.run_caller(graph, FakeSession(), str(link))
    verification = caller.verify_package(str(link))
    assert verification == {
        "ok": False,
        "errors": [{"code": "PACKAGE_ROOT_INVALID"}],
    }


def test_verify_rejects_unlisted_symlink_directory(tmp_path):
    run = caller.run_caller(
        plan([node("one", "say", text="Bound text.")]),
        FakeSession(),
        str(tmp_path / "package"),
    )
    outside = tmp_path / "outside"
    outside.mkdir()
    link = Path(run.output_dir) / "unlisted-link"
    try:
        link.symlink_to(outside, target_is_directory=True)
    except (OSError, NotImplementedError):
        pytest.skip("symlink creation unavailable")
    verification = caller.verify_package(run.output_dir)
    assert not verification["ok"]
    assert any(
        item["code"] == "UNEXPECTED_SYMLINK" for item in verification["errors"]
    )


def test_limits_stop_before_second_turn_and_preserve_partial_package(tmp_path):
    graph = plan([
        node("one", "say", "two", text="one"),
        node("two", "say", text="two"),
    ], limits={"max_turns": 1})
    session = FakeSession()
    run = caller.run_caller(graph, session, str(tmp_path / "limited"))
    assert run.exit_code == 1
    assert run.result["error"]["code"] == "LIMIT_REACHED"
    assert run.result["error"]["details"]["limit"] == "max_turns"
    assert len(run.result["actions"]) == 1
    assert len(session.operations) == 1
    assert run.verification["ok"]


def test_default_zero_spend_ceiling_refuses_metered_model_output(tmp_path):
    graph = plan([
        node("model", "generate", prompt="Speak.", allowed_actions=["say"]),
    ], mode="generative")
    run = caller.run_caller(
        graph, FakeSession(), str(tmp_path / "cost"), model=FakeModel(cost=1)
    )
    assert run.result["status"] == "BLOCKED"
    assert run.result["error"]["details"] == {
        "limit": "max_cost_microusd", "observed": 1, "maximum": 0,
    }
    assert run.verification["ok"]


def test_unobservable_transfer_is_not_collapsed_into_failure_or_support(tmp_path):
    capabilities = dict(ALL_CAPABILITIES)
    capabilities["observe_transfer"] = caller.UNOBSERVABLE
    run = caller.run_caller(
        plan([node("transfer", "transfer_expect", timeout_ms=10)]),
        FakeSession(capabilities=capabilities), str(tmp_path / "transfer"),
    )
    assert run.result["status"] == "BLOCKED"
    assert run.result["error"] == {
        "code": "CAPABILITY_UNOBSERVABLE",
        "message": "transfer observation is unobservable",
        "details": {"operation": "observe_transfer", "capability_state": "UNOBSERVABLE"},
    }


def test_capability_adapter_exception_becomes_verifiable_error_package(tmp_path):
    secret = "signed-url-token-should-not-be-stored"

    class BrokenSession(FakeSession):
        def capabilities(self):
            raise OSError(secret)

    run = caller.run_caller(
        plan([node("one", "say", text="hello")]), BrokenSession(),
        str(tmp_path / "adapter-error"),
    )
    assert run.result["status"] == "ERROR"
    assert run.result["error"] == {
        "code": "SESSION_OR_ADAPTER_ERROR",
        "message": "caller session or adapter failed",
        "exception_type": "OSError",
    }
    assert secret not in Path(run.output_dir, "caller-result.json").read_text()
    assert run.verification["ok"]


def test_validation_refuses_authority_state_unknown_fields_and_scripted_model():
    with pytest.raises(ValueError, match="reserved authority"):
        caller.validate_plan(plan([node("state", "set_state", key="outcome.passed", value=True)]))
    with pytest.raises(ValueError, match="unknown fields"):
        caller.validate_plan({**plan([node("one", "say", text="hello")]), "verdict": "pass"})
    with pytest.raises(ValueError, match="scripted mode"):
        caller.validate_plan(plan([node("model", "generate", prompt="speak")]))


def test_trigger_regex_refuses_backtracking_constructs_and_accepts_any_combinator():
    with pytest.raises(ValueError, match="groups or alternation"):
        caller.validate_plan(plan([
            node(
                "listen", "listen", timeout_ms=10,
                until={"event": "transcript", "text_regex": "(a+)+$"},
            )
        ]))
    with pytest.raises(ValueError, match="at most one unbounded"):
        caller.validate_plan(plan([
            node(
                "listen", "listen", timeout_ms=10,
                until={"event": "transcript", "text_regex": "a+b+"},
            )
        ]))
    with pytest.raises(ValueError, match="start-anchored"):
        caller.validate_plan(plan([
            node(
                "listen", "listen", timeout_ms=10,
                until={"event": "transcript", "text_regex": "a+$"},
            )
        ]))
    normalized = caller.validate_plan(plan([
        node(
            "listen", "listen", timeout_ms=10,
            until={"any": [
                {"event": "transcript", "text_regex": "refund"},
                {"event": "transcript", "text_regex": "chargeback"},
            ]},
        )
    ]))
    assert "any" in normalized["nodes"][0]["until"]


def test_trigger_regex_oversized_searched_text_returns_bounded_error(tmp_path):
    graph = plan([
        node(
            "listen", "listen", timeout_ms=10,
            until={"event": "transcript", "text_regex": "^a+$"},
        )
    ])
    session = FakeSession([{
        "kind": "transcript",
        "text": "a" * (caller.MAX_TRIGGER_SEARCH_CHARS + 1),
    }])
    run = caller.run_caller(graph, session, str(tmp_path / "bounded-regex"))
    assert caller.MAX_TRIGGER_SEARCH_CHARS == 1_024
    assert run.result["status"] == "ERROR"
    assert run.result["error"] == {
        "code": "REGEX_SEARCH_TEXT_LIMIT",
        "message": "trigger text exceeded the bounded regex search limit",
        "details": {
            "observed": caller.MAX_TRIGGER_SEARCH_CHARS + 1,
            "maximum": caller.MAX_TRIGGER_SEARCH_CHARS,
        },
    }
    assert run.verification["ok"]


def test_result_edit_and_unexpected_file_break_package_verification(tmp_path):
    run = caller.run_caller(plan([node("one", "say", text="hello")]), FakeSession(), str(tmp_path / "run"))
    result_path = Path(run.output_dir, "caller-result.json")
    result = json.loads(result_path.read_text(encoding="utf-8"))
    result["exit_code"] = 1
    result_path.write_text(json.dumps(result), encoding="utf-8")
    verification = caller.verify_package(run.output_dir)
    assert not verification["ok"]
    assert {error["code"] for error in verification["errors"]} >= {"FILE_DIGEST_MISMATCH", "RESULT_ID_MISMATCH"}

    second = caller.run_caller(plan([node("one", "say", text="hello")]), FakeSession(), str(tmp_path / "run2"))
    Path(second.output_dir, "extra.txt").write_text("not bound", encoding="utf-8")
    assert "UNEXPECTED_FILE" in {error["code"] for error in caller.verify_package(second.output_dir)["errors"]}


def test_self_consistent_package_cannot_point_frozen_replay_outside_root(tmp_path):
    source = caller.run_caller(
        plan([node("one", "say", text="hello")]), FakeSession(), str(tmp_path / "source-safe")
    )
    outside = tmp_path / "outside.txt"
    outside.write_text("outside", encoding="utf-8")
    result = source.result
    result["actions"][0]["text_path"] = "../../outside.txt"
    result["actions"][0]["text_sha256"] = caller._sha(outside.read_bytes())
    (Path(source.output_dir) / "caller-result.json").unlink()
    (Path(source.output_dir) / "package-manifest.json").unlink()
    caller._write_result_package(Path(source.output_dir), result)
    assert caller.verify_package(source.output_dir)["ok"]

    session = FakeSession()
    replay = caller.run_caller(
        {
            "schema": caller.PLAN_SCHEMA, "id": "safe-replay", "mode": "frozen_replay",
            "frozen_package": source.output_dir,
        },
        session, str(tmp_path / "safe-replay"),
    )
    assert replay.result["status"] == "BLOCKED"
    assert replay.result["error"]["code"] == "FROZEN_ARTIFACT_INVALID"
    assert session.operations == []


def test_shipped_plan_schema_accepts_normalized_plan(tmp_path):
    jsonschema = pytest.importorskip("jsonschema")
    schema_path = Path(__file__).parents[1] / "src/hotato/schema/caller-plan.v1.json"
    schema = json.loads(schema_path.read_text(encoding="utf-8"))
    normalized = caller.validate_plan(plan([node("one", "say", text="hello")]))
    jsonschema.Draft7Validator(schema).validate(normalized)

    frozen = caller.validate_plan({
        "schema": caller.PLAN_SCHEMA, "id": "frozen", "mode": "frozen_replay",
        "frozen_package": "package", "limits": {"max_steps": 10},
    })
    jsonschema.Draft7Validator(schema).validate(frozen)

    result_schema = json.loads(
        (Path(__file__).parents[1] / "src/hotato/schema/caller-result.v1.json").read_text(
            encoding="utf-8"
        )
    )
    generated = caller.run_caller(
        plan([node("one", "say", text="hello")]), FakeSession(),
        str(tmp_path / "caller-schema-run"),
    )
    jsonschema.Draft7Validator(result_schema).validate(generated.result)
