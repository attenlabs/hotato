from __future__ import annotations

import importlib.util
import json
import os
import stat
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
DEPLOY = ROOT / "deploy" / "control-plane"


def _module():
    spec = importlib.util.spec_from_file_location(
        "hotato_control_bootstrap", DEPLOY / "bootstrap.py"
    )
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


def _installer_module():
    spec = importlib.util.spec_from_file_location(
        "hotato_control_installer", DEPLOY / "install-runtime.py"
    )
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


def _runner_module():
    spec = importlib.util.spec_from_file_location(
        "hotato_control_runner", DEPLOY / "run-production.py"
    )
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


def _env(path: Path, *, digests: bool = True):
    image = "@sha256:" + "a" * 64 if digests else ":latest"
    path.write_text(
        "\n".join(
            [
                "LIVEKIT_SERVER_IMAGE=livekit/livekit-server" + image,
                "LIVEKIT_SIP_IMAGE=livekit/sip" + image,
                "VALKEY_IMAGE=valkey/valkey" + image,
                "OTEL_COLLECTOR_IMAGE=otel/opentelemetry-collector-contrib" + image,
                "LIVEKIT_API_KEY=abcdefgh",
                "LIVEKIT_API_SECRET=" + "s" * 32,
                "HOTATO_PRODUCTION_TOKEN=" + "t" * 32,
                "VALKEY_PASSWORD=" + "p" * 32,
                "MEDIA_NODE_IP=127.0.0.1",
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    path.chmod(0o600)


def test_bootstrap_renders_private_configs_without_secrets_in_manifest(tmp_path):
    module = _module()
    env = tmp_path / ".env"
    runtime = tmp_path / "runtime"
    _env(env)
    manifest = module.render(env, runtime)
    assert manifest["image_refs_immutable"] is True
    assert "s" * 32 not in str(manifest)
    assert "t" * 32 not in str(manifest)
    assert "p" * 32 not in str(manifest)
    for name in (
        "livekit.yaml",
        "sip.yaml",
        "valkey.conf",
        "valkey-password",
        "hotato-production-token",
        "hotato-production-maintenance.json",
        "otelcol.yaml",
        "bootstrap-manifest.json",
    ):
        path = runtime / name
        assert path.is_file()
        assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert "127.0.0.1:6379" in (runtime / "sip.yaml").read_text()
    assert "nat_1_to_1_ip" not in (runtime / "sip.yaml").read_text()
    assert "log_level: info" in (runtime / "sip.yaml").read_text()
    assert "use_external_ip: false" in (runtime / "livekit.yaml").read_text()
    assert 'node_ip: "127.0.0.1"' in (runtime / "livekit.yaml").read_text()
    assert 'bind_addresses: ["0.0.0.0"]' in (runtime / "livekit.yaml").read_text()
    assert "7880/tcp" in manifest["must_remain_firewalled"]
    assert {"4317/tcp", "4318/tcp", "8432/tcp", "8888/tcp"}.issubset(
        manifest["must_remain_firewalled"]
    )
    maintenance = (runtime / "hotato-production-maintenance.json").read_text()
    policy = json.loads(maintenance)
    assert policy["schema"] == "hotato.production-maintenance.v1"
    assert policy["retention_seconds"] is None
    assert policy["required_lanes"] == [
        "participant_audio", "transcript", "model_trace", "tool_calls",
        "backend_state",
    ]
    otel = (runtime / "otelcol.yaml").read_text(encoding="utf-8")
    assert "t" * 32 in otel
    assert "t" * 32 not in (runtime / "bootstrap-manifest.json").read_text(
        encoding="utf-8"
    )
    assert manifest["otel_buffer"] == {
        "input_protocols": ["otlp/grpc", "otlp/http"],
        "input_scope": "loopback-only",
        "persistence": "single-host-file-storage-fsync",
        "queue_capacity": 10000,
        "queue_unit": "requests",
        "overflow_behavior": "reject-and-count",
        "retry_max_elapsed_time_seconds": None,
        "internal_metrics": "127.0.0.1:8888/metrics",
    }


def test_bootstrap_renders_bounded_loopback_otel_collector(tmp_path):
    yaml = pytest.importorskip("yaml")
    module = _module()
    env = tmp_path / ".env"
    runtime = tmp_path / "runtime"
    _env(env)
    manifest = module.render(env, runtime)
    config = yaml.safe_load((runtime / "otelcol.yaml").read_text(encoding="utf-8"))

    receiver = config["receivers"]["otlp/hotato"]["protocols"]
    assert receiver["grpc"]["endpoint"] == "127.0.0.1:4317"
    assert receiver["http"]["endpoint"] == "127.0.0.1:4318"
    storage = config["extensions"]["file_storage/hotato"]
    assert storage["directory"] == "/var/lib/otelcol/hotato-wal"
    assert storage["fsync"] is True
    assert storage["create_directory"] is True
    assert storage["directory_permissions"] == "0700"
    assert storage["compaction"] == {
        "on_start": True,
        "on_rebound": True,
        "directory": "/var/lib/otelcol/hotato-compaction",
        "max_transaction_size": 65536,
        "cleanup_on_start": True,
        "rebound_needed_threshold_mib": 100,
        "rebound_trigger_threshold_mib": 10,
        "check_interval": "5s",
    }
    exporter = config["exporters"]["otlp_http/hotato"]
    assert exporter["traces_endpoint"] == "http://127.0.0.1:8432/v1/traces"
    assert exporter["encoding"] == "json"
    assert exporter["compression"] == "none"
    assert exporter["headers"] == {
        "Authorization": "Bearer " + "t" * 32,
        "X-Hotato-Source": "otel-collector",
    }
    queue = exporter["sending_queue"]
    assert queue == {
        "enabled": True,
        "storage": "file_storage/hotato",
        "sizer": "requests",
        "queue_size": 10000,
        "num_consumers": 4,
        "wait_for_result": False,
        "block_on_overflow": False,
    }
    assert exporter["retry_on_failure"]["max_elapsed_time"] == "0s"
    metrics = config["service"]["telemetry"]["metrics"]
    prometheus = metrics["readers"][0]["pull"]["exporter"]["prometheus"]
    assert prometheus["host"] == "127.0.0.1"
    assert prometheus["port"] == 8888
    assert "127.0.0.1:8888" in manifest["local_control_routes"]


def test_bootstrap_refuses_mutable_images_without_explicit_local_override(tmp_path):
    module = _module()
    env = tmp_path / ".env"
    _env(env, digests=False)
    with pytest.raises(ValueError, match="immutable"):
        module.render(env, tmp_path / "runtime")
    assert module.render(env, tmp_path / "local", allow_tags=True)["image_refs_immutable"] is False


def test_bootstrap_refuses_weak_secrets_and_symlink_env(tmp_path):
    module = _module()
    env = tmp_path / ".env"
    _env(env)
    env.write_text(env.read_text().replace("s" * 32, "short"), encoding="utf-8")
    with pytest.raises(ValueError, match="at least 32"):
        module.render(env, tmp_path / "runtime")
    target = tmp_path / "target"
    _env(target)
    link = tmp_path / "link"
    link.symlink_to(target)
    with pytest.raises(ValueError, match="non-symlink"):
        module.render(link, tmp_path / "runtime-2")
    assert module.main(["--env", str(link), "--runtime", str(tmp_path / "runtime-3")]) == 2


def test_bootstrap_refuses_group_or_world_readable_secret_env(tmp_path):
    if os.name != "posix":
        pytest.skip("POSIX permission bits are unavailable")
    module = _module()
    env = tmp_path / ".env"
    _env(env)
    env.chmod(0o644)
    with pytest.raises(ValueError, match="mode 0600"):
        module.render(env, tmp_path / "runtime")


def test_bootstrap_refuses_unchanged_public_placeholder_credentials(tmp_path):
    module = _module()
    env = tmp_path / ".env"
    env.write_bytes((DEPLOY / ".env.example").read_bytes())
    env.chmod(0o600)
    with pytest.raises(ValueError, match="public placeholder credentials"):
        module.render(env, tmp_path / "runtime", allow_tags=True)


def test_production_token_filename_is_consistent_across_runtime_layers():
    expected = "/run/secrets/hotato-production-token"
    compose = (DEPLOY / "compose.yaml").read_text(encoding="utf-8")
    assert expected in compose
    assert str(_runner_module().SECRET_FILE) == expected
    installer = _installer_module()
    installed = {
        str(destination)
        for source, destination, _mode, _owner, _limit in installer.COPIES
        if source == "hotato-production-token"
    }
    assert installed == {"/out/hotato/hotato-production-token"}


def test_bootstrap_refuses_fifo_and_check_open_swap(tmp_path, monkeypatch):
    module = _module()
    fifo = tmp_path / "fifo"
    os.mkfifo(fifo)
    with pytest.raises(ValueError, match="regular"):
        module.render(fifo, tmp_path / "runtime")

    env = tmp_path / ".env"
    replacement = tmp_path / "replacement"
    _env(env)
    _env(replacement)
    original_open = module.os.open

    def swapped(path, flags, *args):
        if Path(path) == env:
            return original_open(replacement, flags, *args)
        return original_open(path, flags, *args)

    monkeypatch.setattr(module.os, "open", swapped)
    with pytest.raises(ValueError, match="changed"):
        module.render(env, tmp_path / "runtime-2")


def test_bootstrap_uses_external_discovery_without_conflicting_node_ip(tmp_path):
    module = _module()
    env = tmp_path / ".env"
    _env(env)
    env.write_text(
        env.read_text(encoding="utf-8").replace("MEDIA_NODE_IP=127.0.0.1", "MEDIA_NODE_IP=auto"),
        encoding="utf-8",
    )
    runtime = tmp_path / "runtime"
    manifest = module.render(env, runtime)
    livekit = (runtime / "livekit.yaml").read_text(encoding="utf-8")
    assert manifest["livekit_media_address_mode"] == "stun-discovery"
    assert "use_external_ip: true" in livekit
    assert "node_ip:" not in livekit


def test_bootstrap_refuses_unsafe_valkey_password(tmp_path):
    module = _module()
    env = tmp_path / ".env"
    _env(env)
    env.write_text(
        env.read_text(encoding="utf-8").replace("p" * 32, "contains spaces"),
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="VALKEY_PASSWORD"):
        module.render(env, tmp_path / "runtime")


def test_compose_keeps_control_endpoints_loopback_and_media_on_host_network():
    compose = (DEPLOY / "compose.yaml").read_text(encoding="utf-8")
    assert "127.0.0.1:6379:6379" in compose
    assert compose.count("network_mode: host") == 4
    assert "livekit-runtime:/run/hotato:ro" in compose
    assert "sip-runtime:/run/hotato:ro" in compose
    assert "valkey-runtime:/run/hotato:ro" in compose
    assert "hotato-runtime:/run/secrets:ro" in compose
    assert "otel-runtime:/run/hotato:ro" in compose
    assert "otel-wal:/var/lib/otelcol" in compose
    assert "otel-wal:/out/otel-wal" in compose
    assert "./runtime:/source:ro" in compose
    assert "condition: service_completed_successfully" in compose
    assert 'entrypoint: ["hotato"]' in compose
    assert '"--token-file", "/run/secrets/hotato-production-token"' in compose
    assert '"--maintenance-policy", "/run/secrets/hotato-production-maintenance.json"' in compose
    assert "--token-env" not in compose
    assert "HOTATO_PRODUCTION_TOKEN" not in compose
    assert 'user: "10001:10001"' in compose
    assert "read_only: true" in compose
    assert 'cap_drop: ["ALL"]' in compose


def test_runtime_installer_separates_service_secrets_and_bounds_inputs():
    installer = (DEPLOY / "install-runtime.py").read_text(encoding="utf-8")
    assert 'Path("/out/valkey/valkey.conf")' in installer
    assert 'Path("/out/livekit/livekit.yaml")' in installer
    assert 'Path("/out/sip/sip.yaml")' in installer
    assert 'Path("/out/otel/otelcol.yaml")' in installer
    assert 'Path("/out/otel-wal")' in installer
    assert 'Path("/out/hotato/hotato-production-token")' in installer
    assert 'Path("/out/hotato/hotato-production-maintenance.json")' in installer
    assert "(10001, 10001)" in installer
    assert "O_NOFOLLOW" in installer


def test_runtime_installer_copies_atomically_and_refuses_symlink(tmp_path):
    module = _installer_module()
    source = tmp_path / "source"
    destination_dir = tmp_path / "destination"
    source.mkdir()
    destination_dir.mkdir()
    (source / "token").write_bytes(b"bounded-secret\n")
    destination = destination_dir / "token"
    module.COPIES = (("token", destination, 0o400, None, 64),)
    module.DIRECTORIES = ()
    module.install(source)
    assert destination.read_bytes() == b"bounded-secret\n"
    assert stat.S_IMODE(destination.stat().st_mode) == 0o400

    destination.unlink()
    (source / "token").unlink()
    (source / "target").write_bytes(b"secret")
    (source / "token").symlink_to(source / "target")
    with pytest.raises(ValueError, match="non-symlink"):
        module.install(source)


def test_runtime_installer_refuses_fifo_and_check_open_swap(tmp_path, monkeypatch):
    module = _installer_module()
    source = tmp_path / "source"
    destination_dir = tmp_path / "destination"
    source.mkdir()
    destination_dir.mkdir()
    fifo = source / "token"
    os.mkfifo(fifo)
    module.COPIES = (("token", destination_dir / "token", 0o400, None, 64),)
    module.DIRECTORIES = ()
    with pytest.raises(ValueError, match="regular"):
        module.install(source)

    fifo.unlink()
    fifo.write_bytes(b"first")
    replacement = source / "replacement"
    replacement.write_bytes(b"second")
    original_open = module.os.open

    def swapped(path, flags, *args):
        if Path(path) == fifo:
            return original_open(replacement, flags, *args)
        return original_open(path, flags, *args)

    monkeypatch.setattr(module.os, "open", swapped)
    with pytest.raises(ValueError, match="changed"):
        module.install(source)


def test_runtime_installer_prepares_otel_wal_without_following_symlinks(
    tmp_path, monkeypatch
):
    module = _installer_module()
    source = tmp_path / "source"
    source.mkdir()
    wal = tmp_path / "wal"
    wal.mkdir()
    module.COPIES = ()
    module.DIRECTORIES = ((wal, 0o700, None),)
    module.install(source)
    assert stat.S_IMODE(wal.stat().st_mode) == 0o700

    replacement = tmp_path / "replacement"
    replacement.mkdir()
    original_open = module.os.open

    def swapped(path, flags, *args):
        if Path(path) == wal:
            return original_open(replacement, flags, *args)
        return original_open(path, flags, *args)

    monkeypatch.setattr(module.os, "open", swapped)
    with pytest.raises(ValueError, match="changed"):
        module.install(source)

    module.DIRECTORIES = ()
    wal.rmdir()
    wal.symlink_to(replacement, target_is_directory=True)
    module.DIRECTORIES = ((wal, 0o700, None),)
    with pytest.raises(ValueError, match="non-symlink"):
        module.install(source)


def test_example_pins_current_collector_release_for_local_evaluation():
    example = (DEPLOY / ".env.example").read_text(encoding="utf-8")
    assert (
        "OTEL_COLLECTOR_IMAGE=otel/opentelemetry-collector-contrib:0.153.0"
        in example
    )


def test_deployment_files_do_not_contain_populated_credentials():
    for path in DEPLOY.iterdir():
        if path.is_file() and path.name != ".env.example":
            text = path.read_text(encoding="utf-8")
            assert "replace-with-at-least" not in text


def test_bootstrap_renders_explicit_retention_and_lane_subset(tmp_path):
    module = _module()
    env = tmp_path / ".env"
    _env(env)
    env.write_text(
        env.read_text(encoding="utf-8")
        + "HOTATO_RETENTION_DAYS=7\n"
        + "HOTATO_REQUIRED_LANES=participant_audio,backend_state\n"
        + "HOTATO_MAINTENANCE_INTERVAL_SECONDS=5\n",
        encoding="utf-8",
    )
    runtime = tmp_path / "runtime"
    manifest = module.render(env, runtime)
    policy = json.loads(
        (runtime / "hotato-production-maintenance.json").read_text()
    )
    assert policy["retention_seconds"] == 7 * 86_400
    assert policy["required_lanes"] == ["participant_audio", "backend_state"]
    assert manifest["production_maintenance"]["interval_seconds"] == 5


def test_bootstrap_refuses_invalid_maintenance_values(tmp_path):
    module = _module()
    env = tmp_path / ".env"
    _env(env)
    env.write_text(
        env.read_text(encoding="utf-8") + "HOTATO_RETENTION_DAYS=-1\n",
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="HOTATO_RETENTION_DAYS"):
        module.render(env, tmp_path / "runtime")
