#!/usr/bin/env python3
"""Render private LiveKit/SIP/Hotato configs and validate deployment bounds."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import stat
import tempfile
from pathlib import Path
from typing import Dict

_DIGEST_IMAGE = re.compile(r"^[^\s@]+@sha256:[0-9a-f]{64}$")
_HOST = re.compile(r"^(?:[A-Za-z0-9.-]+|[0-9a-fA-F:]+)$")
_VALKEY_SECRET = re.compile(r"^[A-Za-z0-9_-]{32,256}$")
_EVIDENCE_LANES = (
    "participant_audio",
    "transcript",
    "model_trace",
    "tool_calls",
    "backend_state",
)


def _read_env(path: Path) -> Dict[str, str]:
    info = path.lstat()
    if path.is_symlink() or not stat.S_ISREG(info.st_mode):
        raise ValueError(f"{path} must be a regular, non-symlink file")
    if os.name == "posix" and stat.S_IMODE(info.st_mode) & 0o077:
        raise ValueError(
            f"{path} contains deployment secrets and must use mode 0600"
        )
    if info.st_size > 1024 * 1024:
        raise ValueError(f"{path} exceeds the 1 MiB environment-file limit")
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0)
    descriptor = os.open(path, flags)
    try:
        opened = os.fstat(descriptor)
        if (
            not stat.S_ISREG(opened.st_mode)
            or (opened.st_dev, opened.st_ino) != (info.st_dev, info.st_ino)
        ):
            raise ValueError(f"{path} changed before it was opened")
        chunks = []
        total = 0
        while total <= 1024 * 1024:
            chunk = os.read(descriptor, min(65_536, 1024 * 1024 + 1 - total))
            if not chunk:
                break
            chunks.append(chunk)
            total += len(chunk)
        raw = b"".join(chunks)
        if len(raw) > 1024 * 1024:
            raise ValueError(f"{path} exceeds the 1 MiB environment-file limit")
        text = raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise ValueError(f"{path} must contain UTF-8 text") from exc
    finally:
        os.close(descriptor)
    values: Dict[str, str] = {}
    for number, line in enumerate(text.splitlines(), 1):
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        if "=" not in stripped:
            raise ValueError(f"{path.name}:{number}: expected NAME=value")
        name, value = stripped.split("=", 1)
        if not re.fullmatch(r"[A-Z][A-Z0-9_]*", name):
            raise ValueError(f"{path.name}:{number}: invalid variable name")
        if any(char in value for char in "\x00\r\n"):
            raise ValueError(f"{path.name}:{number}: control character in value")
        values[name] = value
    return values


def _quote(value: str) -> str:
    return json.dumps(value, ensure_ascii=True)


def _atomic_private(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    if path.parent.is_symlink():
        raise ValueError("runtime directory cannot be a symlink")
    descriptor, temporary = tempfile.mkstemp(prefix=".hotato-config-", dir=str(path.parent))
    try:
        # os.fchmod does not exist on Windows (POSIX permission bits are not a
        # Windows concept); mkstemp already creates the file 0o600 on POSIX.
        if hasattr(os, "fchmod"):
            os.fchmod(descriptor, 0o600)
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        os.chmod(path, 0o600)
    except BaseException:
        try:
            os.unlink(temporary)
        except OSError:
            pass
        raise


def _bounded_number(
    values: Dict[str, str], name: str, default: str, low: float, high: float
) -> float:
    raw = values.get(name, default)
    try:
        result = float(raw)
    except ValueError as exc:
        raise ValueError(f"{name} must be a number") from exc
    if result != result or result in (float("inf"), float("-inf")) or not low <= result <= high:
        raise ValueError(f"{name} must be in [{low:g}, {high:g}]")
    return result


def render(env_path: Path, runtime: Path, *, allow_tags: bool = False) -> Dict[str, object]:
    values = _read_env(env_path)
    required = {
        "LIVEKIT_SERVER_IMAGE",
        "LIVEKIT_SIP_IMAGE",
        "VALKEY_IMAGE",
        "OTEL_COLLECTOR_IMAGE",
        "LIVEKIT_API_KEY",
        "LIVEKIT_API_SECRET",
        "HOTATO_PRODUCTION_TOKEN",
        "VALKEY_PASSWORD",
        "MEDIA_NODE_IP",
    }
    missing = sorted(name for name in required if not values.get(name))
    if missing:
        raise ValueError("missing deployment values: " + ", ".join(missing))
    unchanged = sorted(
        name for name in (
            "LIVEKIT_API_KEY", "LIVEKIT_API_SECRET",
            "HOTATO_PRODUCTION_TOKEN", "VALKEY_PASSWORD",
        )
        if values[name].lower().startswith(("replace-with", "replace_with"))
    )
    if unchanged:
        raise ValueError(
            "refusing public placeholder credentials: " + ", ".join(unchanged)
        )
    if len(values["LIVEKIT_API_KEY"]) < 8 or len(values["LIVEKIT_API_KEY"]) > 256:
        raise ValueError("LIVEKIT_API_KEY must contain 8..256 characters")
    if len(values["LIVEKIT_API_SECRET"]) < 32:
        raise ValueError("LIVEKIT_API_SECRET must contain at least 32 characters")
    if len(values["HOTATO_PRODUCTION_TOKEN"]) < 32:
        raise ValueError("HOTATO_PRODUCTION_TOKEN must contain at least 32 characters")
    if not _VALKEY_SECRET.fullmatch(values["VALKEY_PASSWORD"]):
        raise ValueError(
            "VALKEY_PASSWORD must contain 32..256 URL-safe characters "
            "(letters, digits, underscore, or hyphen)"
        )
    if values["MEDIA_NODE_IP"] != "auto" and not _HOST.fullmatch(values["MEDIA_NODE_IP"]):
        raise ValueError("MEDIA_NODE_IP must be 'auto' or a hostname/IP without a URL scheme")
    maintenance_interval = _bounded_number(
        values, "HOTATO_MAINTENANCE_INTERVAL_SECONDS", "30", 0.1, 86_400
    )
    quiescence = _bounded_number(
        values, "HOTATO_QUIESCENCE_SECONDS", "30", 0, 86_400
    )
    retention_text = values.get("HOTATO_RETENTION_DAYS", "none").strip().lower()
    retention_seconds = (
        None
        if retention_text in {"none", "disabled"}
        else _bounded_number(
            values, "HOTATO_RETENTION_DAYS", retention_text, 0, 3650
        ) * 86_400
    )
    required_lanes = tuple(
        lane.strip()
        for lane in values.get(
            "HOTATO_REQUIRED_LANES", ",".join(_EVIDENCE_LANES)
        ).split(",")
        if lane.strip()
    )
    if (
        not required_lanes
        or len(set(required_lanes)) != len(required_lanes)
        or any(lane not in _EVIDENCE_LANES for lane in required_lanes)
    ):
        raise ValueError(
            "HOTATO_REQUIRED_LANES must be a unique comma-separated subset of "
            + ",".join(_EVIDENCE_LANES)
        )
    image_names = (
        "LIVEKIT_SERVER_IMAGE",
        "LIVEKIT_SIP_IMAGE",
        "VALKEY_IMAGE",
        "OTEL_COLLECTOR_IMAGE",
    )
    if not allow_tags:
        unpinned = [name for name in image_names if not _DIGEST_IMAGE.fullmatch(values[name])]
        if unpinned:
            raise ValueError(
                "production images require immutable @sha256 references: " + ", ".join(unpinned)
            )

    if values["MEDIA_NODE_IP"] == "auto":
        livekit_address = "  use_external_ip: true\n"
        livekit_address_mode = "stun-discovery"
    else:
        # LiveKit documents node_ip as the explicit-address alternative to
        # use_external_ip. Setting both is misleading because discovery wins.
        livekit_address = (
            f"  use_external_ip: false\n  node_ip: {_quote(values['MEDIA_NODE_IP'])}\n"
        )
        livekit_address_mode = "explicit-node-ip"

    livekit = f"""port: 7880
bind_addresses: [\"0.0.0.0\"]
redis:
  address: \"127.0.0.1:6379\"
  password: {_quote(values["VALKEY_PASSWORD"])}
rtc:
  tcp_port: 7881
  port_range_start: 50000
  port_range_end: 60000
{livekit_address.rstrip()}
keys:
  {_quote(values["LIVEKIT_API_KEY"])}: {_quote(values["LIVEKIT_API_SECRET"])}
logging:
  level: info
""".encode("utf-8")
    sip = f"""api_key: {_quote(values["LIVEKIT_API_KEY"])}
api_secret: {_quote(values["LIVEKIT_API_SECRET"])}
ws_url: \"ws://127.0.0.1:7880\"
redis:
  address: \"127.0.0.1:6379\"
  password: {_quote(values["VALKEY_PASSWORD"])}
sip_port: 5060
rtp_port: 10000-20000
use_external_ip: true
health_port: 8090
prometheus_port: 8091
log_level: info
""".encode("utf-8")
    valkey = f"""bind 0.0.0.0
protected-mode yes
port 6379
requirepass {_quote(values["VALKEY_PASSWORD"])}
appendonly yes
appendfsync everysec
dir /data
""".encode("utf-8")
    maintenance = (
        json.dumps(
            {
                "schema": "hotato.production-maintenance.v1",
                "interval_seconds": maintenance_interval,
                "quiescence_seconds": quiescence,
                "required_lanes": list(required_lanes),
                "alert_rules": [
                    {"id": "degraded-session", "condition": "degraded"},
                    {"id": "event-conflict", "condition": "conflict"},
                    {"id": "out-of-order", "condition": "out_of_order"},
                    {"id": "missing-audio", "condition": "missing_audio"},
                    {
                        "id": "missing-tool-evidence",
                        "condition": "missing_tool_evidence",
                    },
                ],
                "retention_seconds": retention_seconds,
            },
            indent=2,
            sort_keys=True,
        )
        + "\n"
    ).encode("utf-8")
    otelcol = f"""extensions:
  file_storage/hotato:
    directory: /var/lib/otelcol/hotato-wal
    timeout: 10s
    fsync: true
    create_directory: true
    directory_permissions: "0700"
    compaction:
      on_start: true
      on_rebound: true
      directory: /var/lib/otelcol/hotato-compaction
      max_transaction_size: 65536
      cleanup_on_start: true
      rebound_needed_threshold_mib: 100
      rebound_trigger_threshold_mib: 10
      check_interval: 5s
receivers:
  otlp/hotato:
    protocols:
      grpc:
        endpoint: 127.0.0.1:4317
      http:
        endpoint: 127.0.0.1:4318
processors:
  memory_limiter:
    check_interval: 1s
    limit_mib: 256
    spike_limit_mib: 64
  batch:
    timeout: 1s
    send_batch_size: 512
    send_batch_max_size: 1024
exporters:
  otlp_http/hotato:
    traces_endpoint: http://127.0.0.1:8432/v1/traces
    encoding: json
    compression: none
    headers:
      Authorization: {_quote("Bearer " + values["HOTATO_PRODUCTION_TOKEN"])}
      X-Hotato-Source: otel-collector
    timeout: 10s
    sending_queue:
      enabled: true
      storage: file_storage/hotato
      sizer: requests
      queue_size: 10000
      num_consumers: 4
      wait_for_result: false
      block_on_overflow: false
    retry_on_failure:
      enabled: true
      initial_interval: 1s
      max_interval: 30s
      max_elapsed_time: 0s
service:
  extensions: [file_storage/hotato]
  telemetry:
    metrics:
      level: normal
      readers:
        - pull:
            exporter:
              prometheus:
                host: 127.0.0.1
                port: 8888
                without_type_suffix: true
                without_units: true
  pipelines:
    traces/hotato:
      receivers: [otlp/hotato]
      processors: [memory_limiter, batch]
      exporters: [otlp_http/hotato]
""".encode("utf-8")
    _atomic_private(runtime / "livekit.yaml", livekit)
    _atomic_private(runtime / "sip.yaml", sip)
    _atomic_private(runtime / "valkey.conf", valkey)
    _atomic_private(runtime / "valkey-password", (values["VALKEY_PASSWORD"] + "\n").encode())
    _atomic_private(
        runtime / "hotato-production-token", (values["HOTATO_PRODUCTION_TOKEN"] + "\n").encode()
    )
    _atomic_private(runtime / "hotato-production-maintenance.json", maintenance)
    _atomic_private(runtime / "otelcol.yaml", otelcol)
    manifest = {
        "schema": "hotato.control-plane-bootstrap.v1",
        "config_sha256": {
            "livekit.yaml": "sha256:" + hashlib.sha256(livekit).hexdigest(),
            "sip.yaml": "sha256:" + hashlib.sha256(sip).hexdigest(),
            "valkey.conf": "sha256:" + hashlib.sha256(valkey).hexdigest(),
            "hotato-production-maintenance.json": "sha256:"
            + hashlib.sha256(maintenance).hexdigest(),
            "otelcol.yaml": "sha256:" + hashlib.sha256(otelcol).hexdigest(),
        },
        "image_references": {name: values[name] for name in image_names},
        "image_refs_immutable": all(_DIGEST_IMAGE.fullmatch(values[name]) for name in image_names),
        "runtime_files_mode": "0600",
        "network_mode": "linux-host-for-livekit-and-sip",
        "livekit_media_address_mode": livekit_address_mode,
        "sip_address_mode": "external-ip-discovery",
        "production_maintenance": {
            "interval_seconds": maintenance_interval,
            "quiescence_seconds": quiescence,
            "required_lanes": list(required_lanes),
            "retention_seconds": retention_seconds,
        },
        "otel_buffer": {
            "input_protocols": ["otlp/grpc", "otlp/http"],
            "input_scope": "loopback-only",
            "persistence": "single-host-file-storage-fsync",
            "queue_capacity": 10000,
            "queue_unit": "requests",
            "overflow_behavior": "reject-and-count",
            "retry_max_elapsed_time_seconds": None,
            "internal_metrics": "127.0.0.1:8888/metrics",
        },
        "local_control_routes": [
            "127.0.0.1:4317",
            "127.0.0.1:4318",
            "127.0.0.1:7880",
            "127.0.0.1:8432",
            "127.0.0.1:8888",
        ],
        "host_bindings": [
            "0.0.0.0:7880 (LiveKit; firewall-restricted)",
            "127.0.0.1:8432 (Hotato production gateway)",
            "127.0.0.1:6379 (Valkey Docker publication)",
            "127.0.0.1:4317/4318 (OTLP receiver)",
            "127.0.0.1:8888 (Collector internal metrics)",
        ],
        "must_remain_firewalled": [
            "4317/tcp",
            "4318/tcp",
            "6379/tcp",
            "7880/tcp",
            "8090/tcp",
            "8091/tcp",
            "8432/tcp",
            "8888/tcp",
        ],
        "public_firewall_when_enabled": [
            "443/tcp (operator TLS reverse proxy)",
            "5060/tcp",
            "5060/udp",
            "10000-20000/udp",
            "7881/tcp",
            "50000-60000/udp",
        ],
    }
    raw = (json.dumps(manifest, indent=2, sort_keys=True) + "\n").encode()
    _atomic_private(runtime / "bootstrap-manifest.json", raw)
    return manifest


def main(argv=None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--env", default=str(Path(__file__).with_name(".env")))
    parser.add_argument("--runtime", default=str(Path(__file__).with_name("runtime")))
    parser.add_argument(
        "--allow-tags",
        action="store_true",
        help="local evaluation only; production requires image digests",
    )
    args = parser.parse_args(argv)
    try:
        manifest = render(
            Path(os.path.abspath(args.env)),
            Path(os.path.abspath(args.runtime)),
            allow_tags=args.allow_tags,
        )
    except (OSError, ValueError) as exc:
        print(json.dumps({"ok": False, "error": str(exc)}, sort_keys=True))
        return 2
    print(json.dumps({"ok": True, **manifest}, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
