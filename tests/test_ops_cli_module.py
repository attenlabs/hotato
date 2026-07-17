from __future__ import annotations

import argparse
import json
import os
import pickle

import pytest

from hotato import caller, ops_cli


class _LocalCallerSession:
    def capabilities(self):
        return {
            "send_text": caller.SUPPORTED,
            "send_audio": caller.SUPPORTED,
            "hangup": caller.SUPPORTED,
        }

    def send_text(self, text, metadata):
        return None

    def send_audio(self, pcm_s16le, sample_rate_hz, metadata):
        return None

    def hangup(self, reason):
        return None

    def evidence(self):
        return {"availability": caller.UNOBSERVABLE, "reason": "fixture"}


def _parser():
    parser = argparse.ArgumentParser()
    subparsers = parser.add_subparsers(dest="command", required=True)
    ops_cli.register(subparsers)
    return parser, subparsers


def _caller_plan():
    return {
        "schema": caller.PLAN_SCHEMA,
        "id": "ops-cli-fixture",
        "mode": "scripted",
        "start": "say",
        "nodes": [
            {"id": "say", "type": "say", "text": "Hello", "next": "done"},
            {"id": "done", "type": "hangup", "reason": "complete"},
        ],
        "limits": {"max_duration_ms": 1_000, "max_cost_microusd": 0},
    }


def test_registers_four_command_families_and_refuses_partial_collision():
    parser, subparsers = _parser()
    assert set(subparsers.choices) == {"telephony", "caller", "load", "production"}
    with pytest.raises(ValueError, match="already registered"):
        ops_cli.register(subparsers)
    parsed = parser.parse_args(
        ["load", "caller", "run", "plan.json", "--out", "run", "--target-ws", "ws://127.0.0.1:1"]
    )
    assert parsed.func is not None
    assert parsed.load_family == "caller"
    assert parsed.livekit_url is None


def test_representative_direct_livekit_and_production_commands_parse():
    parser, _ = _parser()
    direct = parser.parse_args(
        [
            "caller",
            "run",
            "plan.json",
            "--out",
            "run",
            "--livekit-url",
            "ws://127.0.0.1:7880",
            "--livekit-target-identity",
            "agent",
            "--livekit-token-env",
            "LIVEKIT_TOKEN",
            "--piper-model",
            "voice.onnx",
            "--piper-config",
            "voice.onnx.json",
            "--ollama-model",
            "qwen3:4b",
        ]
    )
    assert direct.livekit_target_identity == "agent"
    assert direct.ollama_model == "qwen3:4b"
    maintenance = parser.parse_args(
        ["production", "maintain", "policy.json", "--db", "events.sqlite3"]
    )
    assert maintenance.production_command == "maintain"
    assert maintenance.db == "events.sqlite3"


def test_caller_load_factories_are_spawn_pickleable_without_secret_values(monkeypatch):
    monkeypatch.setenv("OPS_CLI_TOKEN", "secret")
    websocket = ops_cli._WebSocketFactory(
        "ws://127.0.0.1:9000", ("Authorization=OPS_CLI_TOKEN",), False, 2.0, 3.0
    )
    model = ops_cli._OllamaFactory("qwen3:4b", "http://127.0.0.1:11434", 0.0, 1, 10.0)
    piper = ops_cli._PiperFactory("voice.onnx", "voice.json", "piper", "a", 10.0, 4096)
    serialized = pickle.dumps(websocket)
    assert b"secret" not in serialized
    assert pickle.loads(serialized) == websocket
    assert pickle.loads(pickle.dumps(model)) == model
    assert pickle.loads(pickle.dumps(piper)) == piper


def test_local_telephony_create_is_credential_free_and_json_stable(tmp_path, capsys):
    parser, _ = _parser()
    spec = tmp_path / "call.json"
    spec.write_text(
        json.dumps(
            {
                "schema": "hotato.telephony-call.v1",
                "id": "local-test",
                "provider": "local",
                "to": "fixture",
                "record": False,
            }
        ),
        encoding="utf-8",
    )
    saved = tmp_path / "handle.json"
    args = parser.parse_args(
        [
            "telephony",
            "create",
            str(spec),
            "--format",
            "json",
            "--save-handle",
            str(saved),
        ]
    )
    assert args.func(args) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["provider"] == "local"
    assert payload["normalized_status"] == "queued"
    assert "credential" not in json.dumps(payload).lower()
    assert json.loads(saved.read_text(encoding="utf-8")) == payload


def test_offline_caller_package_verification_through_registered_command(tmp_path, capsys):
    package = tmp_path / "caller-package"
    run = caller.run_caller(_caller_plan(), _LocalCallerSession(), str(package))
    assert run.verification["ok"]
    parser, _ = _parser()
    args = parser.parse_args(["caller", "verify", str(package), "--format", "json"])
    assert args.func(args) == 0
    assert json.loads(capsys.readouterr().out)["ok"] is True


def test_production_ingest_status_and_audit_use_one_local_store(tmp_path, capsys):
    parser, _ = _parser()
    database = tmp_path / "production.sqlite3"
    event_path = tmp_path / "event.json"
    event_path.write_text(
        json.dumps(
            {
                "specversion": "1.0",
                "id": "event-1",
                "source": "ops-cli-test",
                "type": "session.started",
                "subject": "call-1",
                "time": "2026-07-17T12:00:00Z",
                "sequence": 0,
                "data": {},
                "authority": {
                    "kind": "adapter_reported",
                    "eligible_for_execution_claim": False,
                },
            }
        ),
        encoding="utf-8",
    )
    ingest = parser.parse_args(["production", "ingest", str(event_path), "--db", str(database)])
    assert ingest.func(ingest) == 0
    assert json.loads(capsys.readouterr().out)["durability"] == "committed"
    status = parser.parse_args(["production", "status", "call-1", "--db", str(database)])
    assert status.func(status) == 0
    assert json.loads(capsys.readouterr().out)["session_id"] == "call-1"
    audit = parser.parse_args(["production", "audit", "--db", str(database)])
    assert audit.func(audit) == 0
    assert json.loads(capsys.readouterr().out)["valid"] is True


def test_offline_load_verifiers_refuse_missing_packages_without_network(tmp_path, capsys):
    parser, _ = _parser()
    telephony = parser.parse_args(
        ["load", "telephony", "verify", str(tmp_path / "missing-telephony")]
    )
    assert telephony.func(telephony) == 2
    assert json.loads(capsys.readouterr().out)["ok"] is False
    caller_load = parser.parse_args(["load", "caller", "verify", str(tmp_path / "missing-caller")])
    assert caller_load.func(caller_load) == 2
    assert json.loads(capsys.readouterr().out)["ok"] is False


def test_caller_load_parser_does_not_accept_static_direct_livekit_credentials():
    parser, _ = _parser()
    with pytest.raises(SystemExit):
        parser.parse_args(
            [
                "load",
                "caller",
                "run",
                "plan.json",
                "--out",
                "run",
                "--livekit-url",
                "ws://127.0.0.1:7880",
                "--livekit-token-env",
                "LIVEKIT_TOKEN",
            ]
        )


@pytest.mark.skipif(os.name != "posix", reason="POSIX file modes required")
def test_production_gateway_secret_file_must_be_private(tmp_path):
    secret = tmp_path / "gateway-token"
    secret.write_text("t" * 32 + "\n", encoding="utf-8")
    secret.chmod(0o644)
    with pytest.raises(ValueError, match="group or other permissions"):
        ops_cli._secret_from_file(str(secret), "gateway bearer token")
    secret.chmod(0o400)
    assert ops_cli._secret_from_file(str(secret), "gateway bearer token") == "t" * 32


def test_production_serve_parser_has_bounded_read_timeout_and_no_remote_escape():
    parser, _ = _parser()
    parsed = parser.parse_args(
        [
            "production",
            "serve",
            "--token-env",
            "HOTATO_TOKEN",
            "--request-timeout",
            "2.5",
        ]
    )
    assert parsed.request_timeout == 2.5
    with pytest.raises(SystemExit):
        parser.parse_args(
            [
                "production",
                "serve",
                "--token-env",
                "HOTATO_TOKEN",
                "--allow-remote",
            ]
        )
