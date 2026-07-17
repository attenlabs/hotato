"""Low-conflict CLI registration for Hotato's operational control plane.

The main CLI needs one integration line::

    from . import ops_cli
    ops_cli.register(subparsers)

Imports of optional transports and operational modules remain lazy.  Secrets
are accepted only by environment-variable or bounded regular-file reference;
their values are never copied into argv, JSON output, or evidence packages.

The load grammar is intentionally explicit: ``load telephony run|verify``
controls provider lifecycle workloads and ``load caller run|verify`` executes
full caller programs.  Caller load accepts the WebSocket sidecar boundary.  A
single direct LiveKit run is supported by ``caller run``; concurrent LiveKit
loads require an external sidecar or a future official-SDK token minter that
issues a unique short-lived token and participant identity per child process.
"""

from __future__ import annotations

import argparse
import contextlib
import json
import os
import stat
import tempfile
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, Mapping, Optional, Sequence, Tuple

_COMMANDS = ("telephony", "caller", "load", "production")
_FORMATS = ("json", "text")


def _emit(payload: Mapping[str, Any], output_format: str, lines: Sequence[str]) -> None:
    if output_format == "json":
        print(
            json.dumps(
                dict(payload),
                sort_keys=True,
                indent=2,
                ensure_ascii=False,
                allow_nan=False,
            ),
            flush=True,
        )
        return
    for line in lines:
        print(line, flush=True)


def _add_format(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--format",
        choices=_FORMATS,
        default="json",
        help="stdout representation (default: json)",
    )


def _read_regular_bytes(
    path: str, maximum: int, label: str, *, require_private: bool = False
) -> bytes:
    before = os.lstat(path)
    if stat.S_ISLNK(before.st_mode) or not stat.S_ISREG(before.st_mode):
        raise ValueError(f"{label} must be a regular non-symlink file")
    if before.st_size > maximum:
        raise ValueError(f"{label} exceeds the {maximum}-byte safety limit")
    flags = os.O_RDONLY | getattr(os, "O_BINARY", 0)
    flags |= getattr(os, "O_NONBLOCK", 0) | getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(path, flags)
    try:
        opened = os.fstat(descriptor)
        if (
            not stat.S_ISREG(opened.st_mode)
            or (opened.st_dev, opened.st_ino) != (before.st_dev, before.st_ino)
            or opened.st_size > maximum
        ):
            raise ValueError(f"{label} changed while it was opened")
        if require_private and os.name == "posix" and opened.st_mode & 0o077:
            raise ValueError(
                f"{label} must not grant group or other permissions"
            )
        chunks = []
        remaining = maximum + 1
        while remaining:
            chunk = os.read(descriptor, min(1024 * 1024, remaining))
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
        value = b"".join(chunks)
        if len(value) > maximum:
            raise ValueError(f"{label} exceeds the {maximum}-byte safety limit")
        after = os.fstat(descriptor)
        if (
            after.st_size != opened.st_size
            or after.st_mtime_ns != opened.st_mtime_ns
            or len(value) != opened.st_size
        ):
            raise ValueError(f"{label} changed while it was read")
        return value
    finally:
        os.close(descriptor)


def _load_json(path: str, label: str, maximum: int = 64 * 1024 * 1024) -> Any:
    try:
        raw = _read_regular_bytes(path, maximum, label)
        return json.loads(
            raw.decode("utf-8"),
            parse_constant=lambda value: (_ for _ in ()).throw(ValueError(value)),
        )
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError(f"{label} is not bounded UTF-8 JSON: {exc}") from exc


def _atomic_private_json(path: str, value: Mapping[str, Any]) -> None:
    data = (
        json.dumps(
            dict(value),
            sort_keys=True,
            indent=2,
            ensure_ascii=False,
            allow_nan=False,
        )
        + "\n"
    ).encode("utf-8")
    parent = os.path.dirname(os.path.abspath(path)) or "."
    descriptor, temporary = tempfile.mkstemp(dir=parent, prefix=".hotato-ops-", suffix=".part")
    try:
        if os.name == "posix":
            os.fchmod(descriptor, 0o600)
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    except BaseException:
        try:
            os.unlink(temporary)
        except OSError:
            pass
        raise


def _secret_from_file(path: str, label: str) -> str:
    try:
        raw = _read_regular_bytes(path, 65_536, label, require_private=True)
        value = raw.decode("utf-8").rstrip("\r\n")
    except (OSError, UnicodeDecodeError) as exc:
        raise ValueError(f"{label} must contain bounded UTF-8 text") from exc
    if not value or any(character in value for character in ("\x00", "\r", "\n")):
        raise ValueError(f"{label} must contain exactly one secret value")
    return value


def _secret(env_name: Optional[str], file_path: Optional[str], label: str) -> str:
    if bool(env_name) == bool(file_path):
        raise ValueError(f"exactly one {label} environment or file reference is required")
    if env_name:
        value = os.environ.get(env_name)
        if value is None:
            raise ValueError(f"{label} names unset environment variable {env_name!r}")
        if not value or any(character in value for character in ("\x00", "\r", "\n")):
            raise ValueError(f"{label} environment variable must contain one value")
        if len(value.encode("utf-8")) > 65_536:
            raise ValueError(f"{label} environment variable exceeds 65536 bytes")
        return value
    assert file_path is not None
    return _secret_from_file(file_path, label)


def _headers(items: Sequence[str]) -> Dict[str, str]:
    if len(items) > 64:
        raise ValueError("--header-env exceeds the 64-header limit")
    output: Dict[str, str] = {}
    normalized_names = set()
    total_bytes = 0
    for raw in items:
        if "=" not in raw:
            raise ValueError(f"--header-env {raw!r} must be HEADER=ENV_VAR")
        name, env_name = raw.split("=", 1)
        if (
            not name
            or not env_name
            or any(character in name for character in "\r\n:")
            or any(character in env_name for character in "\r\n=")
        ):
            raise ValueError(f"--header-env {raw!r} is not a safe reference")
        normalized_name = name.lower()
        if normalized_name in normalized_names:
            raise ValueError(f"duplicate request header {name!r}")
        normalized_names.add(normalized_name)
        value = os.environ.get(env_name)
        if value is None:
            raise ValueError(f"--header-env {raw!r} names unset environment variable {env_name!r}")
        if any(character in value for character in ("\r", "\n")):
            raise ValueError(f"environment variable {env_name!r} contains a newline")
        total_bytes += len(name.encode("utf-8")) + len(value.encode("utf-8"))
        if total_bytes > 256 * 1024:
            raise ValueError("--header-env values exceed the 262144-byte limit")
        output[name] = value
    return output


def _handle_payload(handle: Any) -> Dict[str, Any]:
    return {
        "schema": "hotato.telephony-handle.v1",
        "provider": handle.provider,
        "call_id": handle.call_id,
        "normalized_status": handle.normalized_status,
        "provider_status": handle.provider_status,
        "observed_at": handle.created_at,
        "lifecycle_receipt": handle.receipt,
    }


def _telephony(args: argparse.Namespace) -> int:
    from . import telephony
    from .call_runtime import CapabilityUnavailable

    client = telephony.TelephonyClient()
    try:
        if args.telephony_command == "capabilities":
            capabilities = {
                name: {
                    "state": capability.state.value,
                    "reason": capability.reason,
                    "authority": capability.authority,
                }
                for name, capability in client.capabilities(args.provider).items()
            }
            payload = {
                "schema": "hotato.telephony-capabilities.v1",
                "provider": args.provider,
                "capabilities": capabilities,
            }
            _emit(
                payload,
                args.format,
                [
                    f"{name}: {item['state']} -- {item['reason']}"
                    for name, item in sorted(capabilities.items())
                ],
            )
            return 0
        if args.telephony_command == "create":
            specification = _load_json(args.spec, "telephony call specification")
            handle = client.create(specification)
            if args.wait:
                handle = client.wait(
                    handle,
                    timeout_seconds=args.timeout,
                    poll_seconds=args.poll_seconds,
                )
            exported = client.export(handle, args.export_dir) if args.export_dir else None
            payload = _handle_payload(handle)
            payload["export_path"] = exported
            if args.save_handle:
                _atomic_private_json(args.save_handle, payload)
            _emit(
                payload,
                args.format,
                [
                    f"provider: {handle.provider}",
                    f"call id: {handle.call_id}",
                    f"status: {handle.normalized_status}",
                ]
                + ([f"export: {exported}"] if exported else []),
            )
            failed = telephony.TERMINAL_STATUSES - telephony.SUCCESS_STATUSES
            return 1 if args.wait and handle.normalized_status in failed else 0
        if args.telephony_command == "status":
            handle = client.get(args.provider, args.call_id)
            payload = _handle_payload(handle)
            _emit(
                payload,
                args.format,
                [f"{handle.provider} {handle.call_id}: {handle.normalized_status}"],
            )
            failed = telephony.TERMINAL_STATUSES - telephony.SUCCESS_STATUSES
            return 1 if handle.normalized_status in failed else 0
        if args.telephony_command == "cancel":
            supplied = {
                "schema": "hotato.telephony-receipt.v1",
                "operation": "operator_reference",
                "provider": args.provider,
                "call_id": args.call_id,
                "authority": "operator_supplied",
            }
            handle = telephony.CallHandle(
                args.provider,
                args.call_id,
                args.status,
                args.status,
                time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                supplied,
            )
            canceled = client.cancel(handle)
            payload = _handle_payload(canceled)
            _emit(
                payload,
                args.format,
                [f"{canceled.provider} {canceled.call_id}: {canceled.normalized_status}"],
            )
            return 0
        handle = client.get(args.provider, args.call_id)
        export_path = client.export(handle, args.out)
        payload = {**_handle_payload(handle), "export_path": export_path}
        _emit(
            payload,
            args.format,
            [
                f"exported lifecycle receipt: {export_path}",
                "delivered media and task outcome: not established",
            ],
        )
        return 0
    except (telephony.TelephonyError, CapabilityUnavailable) as exc:
        raise ValueError(f"telephony operation failed: {exc}") from exc


@dataclass(frozen=True)
class _WebSocketFactory:
    endpoint: str
    header_references: Tuple[str, ...]
    allow_remote: bool
    connect_timeout: float
    command_timeout: float

    def __call__(self, context: Mapping[str, Any]) -> Any:
        del context
        from .caller_session import WebSocketCallerSession

        return WebSocketCallerSession(
            self.endpoint,
            # Resolve values inside the consumer process.  The pickleable
            # factory contains only HEADER=ENV_VAR references, never secrets.
            headers=_headers(self.header_references),
            allow_remote=self.allow_remote,
            connect_timeout=self.connect_timeout,
            command_timeout=self.command_timeout,
        )


@dataclass(frozen=True)
class _LiveKitFactory:
    url: str
    token: str = field(repr=False)
    target_identity: str
    allow_remote: bool
    sample_rate_hz: int
    connect_timeout: float
    evidence_topic: Optional[str]

    def __call__(self, context: Mapping[str, Any]) -> Any:
        del context
        from .livekit_session import LiveKitCallerSession

        return LiveKitCallerSession(
            self.url,
            self.token,
            target_identity=self.target_identity,
            allow_remote=self.allow_remote,
            sample_rate_hz=self.sample_rate_hz,
            receive_sample_rate_hz=self.sample_rate_hz,
            connect_timeout_seconds=self.connect_timeout,
            evidence_topic=self.evidence_topic,
        )


@dataclass(frozen=True)
class _OllamaFactory:
    model: str
    endpoint: str
    temperature: float
    seed: int
    timeout_seconds: float

    def __call__(self, context: Mapping[str, Any]) -> Any:
        del context
        from .caller import OllamaCallerModel

        return OllamaCallerModel(
            self.model,
            endpoint=self.endpoint,
            temperature=self.temperature,
            seed=self.seed,
            timeout_seconds=self.timeout_seconds,
        )


@dataclass(frozen=True)
class _PiperFactory:
    model_path: str
    config_path: str
    command: str
    voice: str
    timeout_seconds: float
    max_output_bytes: int

    def __call__(self, context: Mapping[str, Any]) -> Any:
        del context
        from .piper_tts import PiperCallerTTS

        return PiperCallerTTS(
            self.model_path,
            self.config_path,
            command=self.command,
            voice=self.voice,
            timeout_seconds=self.timeout_seconds,
            max_output_bytes=self.max_output_bytes,
        )


def _validate_caller_options(args: argparse.Namespace, plan: Mapping[str, Any]) -> None:
    piper_requested = bool(args.piper_model or args.piper_config)
    if piper_requested and not (args.piper_model and args.piper_config):
        raise ValueError("--piper-model and --piper-config must be supplied together")
    if args.livekit_url:
        if args.header_env:
            raise ValueError("--header-env applies only to --target-ws")
        if not args.livekit_target_identity:
            raise ValueError("--livekit-target-identity is required with --livekit-url")
        if not (args.livekit_token_env or args.livekit_token_file):
            raise ValueError(
                "--livekit-token-env or --livekit-token-file is required with --livekit-url"
            )
        caller_plan = plan.get("caller_plan", plan)
        spoken = any(
            node.get("type") == "say"
            or (node.get("type") == "generate" and "say" in node.get("allowed_actions", []))
            for node in caller_plan.get("nodes", [])
        )
        if spoken and not piper_requested and caller_plan.get("mode") != "frozen_replay":
            raise ValueError("direct LiveKit caller speech requires local Piper")
    else:
        livekit_values = (
            args.livekit_target_identity,
            args.livekit_token_env,
            args.livekit_token_file,
            args.livekit_evidence_topic,
            args.livekit_sample_rate,
        )
        if any(value is not None for value in livekit_values):
            raise ValueError("LiveKit-specific options require --livekit-url")


def _piper_factory(args: argparse.Namespace) -> Optional[_PiperFactory]:
    if not args.piper_model:
        return None
    return _PiperFactory(
        args.piper_model,
        args.piper_config,
        args.piper_command,
        args.piper_voice,
        args.piper_timeout,
        args.piper_max_output_bytes,
    )


def _piper_rate(factory: Optional[_PiperFactory]) -> Optional[int]:
    if factory is None:
        return None
    adapter = factory({})
    try:
        return int(adapter.sample_rate_hz)
    finally:
        adapter.close()


def _session_factory(
    args: argparse.Namespace,
    plan: Mapping[str, Any],
    piper_rate: Optional[int],
) -> Any:
    _validate_caller_options(args, plan)
    if args.livekit_url:
        token = _secret(
            args.livekit_token_env,
            args.livekit_token_file,
            "LiveKit token source",
        )
        sample_rate = args.livekit_sample_rate or piper_rate or 48_000
        if piper_rate is not None and sample_rate != piper_rate:
            raise ValueError("--livekit-sample-rate must match Piper audio.sample_rate")
        return _LiveKitFactory(
            args.livekit_url,
            token,
            args.livekit_target_identity,
            args.allow_remote,
            sample_rate,
            args.connect_timeout,
            args.livekit_evidence_topic,
        )
    # Validate references now and resolve them again in each child.  This
    # refuses missing/newline values before scheduling without serializing the
    # values into multiprocessing state.
    _headers(args.header_env)
    return _WebSocketFactory(
        args.target_ws,
        tuple(args.header_env),
        args.allow_remote,
        args.connect_timeout,
        args.command_timeout,
    )


def _model_factory(args: argparse.Namespace) -> Optional[_OllamaFactory]:
    if not args.ollama_model:
        return None
    return _OllamaFactory(
        args.ollama_model,
        args.ollama_endpoint,
        args.temperature,
        args.seed,
        args.model_timeout,
    )


def _caller(args: argparse.Namespace) -> int:
    from . import caller

    if args.caller_command == "verify":
        verification = caller.verify_package(args.directory)
        _emit(
            verification,
            args.format,
            [
                ("VERIFIED" if verification.get("ok") else "REFUSED")
                + f": {verification.get('package_id', 'unknown package')}"
            ],
        )
        return 0 if verification.get("ok") else 2
    plan = caller.load_plan(args.plan)
    _validate_caller_options(args, plan)
    piper_factory = _piper_factory(args)
    try:
        with contextlib.ExitStack() as stack:
            tts = None
            piper_rate = None
            if piper_factory is not None:
                tts = stack.enter_context(piper_factory({}))
                piper_rate = int(tts.sample_rate_hz)
            session_factory = _session_factory(args, plan, piper_rate)
            session = stack.enter_context(session_factory({}))
            model_factory = _model_factory(args)
            model = model_factory({}) if model_factory is not None else None
            run = caller.run_caller(plan, session, args.out, model=model, tts=tts)
    except (OSError, RuntimeError) as exc:
        raise ValueError(
            f"caller session failed ({type(exc).__name__}); adapter text was suppressed"
        ) from None
    payload = {
        "schema": "hotato.caller-cli-result.v1",
        "output_dir": run.output_dir,
        "result": run.result,
        "verification": run.verification,
    }
    _emit(
        payload,
        args.format,
        [
            f"caller: {run.result.get('status')}",
            f"package: {run.output_dir}",
            f"verified: {str(bool(run.verification.get('ok'))).lower()}",
        ],
    )
    return run.exit_code if run.verification.get("ok") else 2


def _load(args: argparse.Namespace) -> int:
    if args.load_family == "telephony":
        from . import loadtest

        try:
            if args.load_command == "run":
                run = loadtest.run(args.plan, args.out)
                payload = {
                    "schema": "hotato.telephony-load-cli-result.v1",
                    "output_dir": run.output_dir,
                    "summary": run.summary,
                    "verification": run.verification,
                }
                metrics = run.summary["metrics"]
                _emit(
                    payload,
                    args.format,
                    [
                        f"telephony load: {run.summary['status']}",
                        f"scheduled: {metrics['scheduled']}",
                        f"started: {metrics['started']}",
                        f"verified: {str(bool(run.verification.get('ok'))).lower()}",
                    ],
                )
                return run.exit_code if run.verification.get("ok") else 2
            verification = loadtest.verify(args.directory)
            _emit(
                verification,
                args.format,
                [
                    ("VERIFIED" if verification.get("ok") else "REFUSED")
                    + f": {verification.get('result_id', 'unknown result')}"
                ],
            )
            return 0 if verification.get("ok") else 2
        except (loadtest.LoadError, KeyError, TypeError, RuntimeError) as exc:
            raise ValueError(f"telephony load execution failed: {exc}") from exc

    from . import caller_load

    if args.load_command == "verify":
        verification = caller_load.verify_caller_load(args.directory)
        _emit(
            verification,
            args.format,
            [
                ("VERIFIED" if verification.get("ok") else "REFUSED")
                + f": {verification.get('package_id', 'unknown package')}"
            ],
        )
        return 0 if verification.get("ok") else 2
    try:
        plan = caller_load.load_plan(args.plan)
        _validate_caller_options(args, plan)
        piper_factory = _piper_factory(args)
        rate = _piper_rate(piper_factory)
        session_factory = _session_factory(args, plan, rate)
        run = caller_load.run_caller_load(
            plan,
            args.out,
            session_factory,
            model_factory=_model_factory(args),
            tts_factory=piper_factory,
            base_dir=os.path.dirname(os.path.abspath(args.plan)),
            created_at=args.created_at,
            remote_endpoint=args.target_ws if args.allow_remote else None,
            execution_scope="remote" if args.allow_remote else "local",
        )
    except (caller_load.CallerLoadError, OSError, RuntimeError) as exc:
        raise ValueError(
            f"caller load execution failed ({type(exc).__name__}); adapter text was suppressed"
        ) from None
    payload = {
        "schema": "hotato.caller-load-cli-result.v1",
        "output_dir": run.output_dir,
        "result": run.result,
        "verification": run.verification,
    }
    metrics = run.result["metrics"]
    _emit(
        payload,
        args.format,
        [
            f"caller load: {run.result['status']}",
            f"scheduled: {metrics['scheduled']}",
            f"started: {metrics['started']}",
            f"verified: {str(bool(run.verification.get('ok'))).lower()}",
        ],
    )
    return run.exit_code if run.verification.get("ok") else 2


def _production(args: argparse.Namespace) -> int:
    from . import production

    store = None
    try:
        if args.production_command == "verify-regression":
            verification = production.verify_regression_candidate(args.directory)
            _emit(
                verification,
                args.format,
                [
                    ("VERIFIED" if verification.get("valid") else "REFUSED")
                    + f": {verification.get('candidate_id', 'unknown candidate')}"
                ],
            )
            return 0 if verification.get("valid") else 2
        store = production.ProductionStore(args.db)
        if args.production_command == "serve":
            from . import production_supervisor

            token = None
            if args.token_env or args.token_file:
                token = _secret(args.token_env, args.token_file, "gateway bearer token")
            hmac_secret = None
            if args.hmac_secret_env or args.hmac_secret_file:
                hmac_secret = _secret(
                    args.hmac_secret_env,
                    args.hmac_secret_file,
                    "gateway HMAC secret",
                )
            gateway = production.ProductionGateway(
                store,
                token,
                hmac_secret=hmac_secret,
                host=args.host,
                port=args.port,
                max_workers=args.max_workers,
                max_signature_skew_seconds=args.max_signature_skew,
                request_timeout_seconds=args.request_timeout,
            )
            supervisor = None
            try:
                policy = (
                    production_supervisor.load_policy(args.maintenance_policy)
                    if args.maintenance_policy
                    else None
                )
                if policy is not None:
                    supervisor = production_supervisor.ProductionSupervisor(store, policy)
                host, port = gateway.address
                _emit(
                    {
                        "schema": "hotato.production-gateway.v1",
                        "host": host,
                        "port": port,
                        "database": os.path.abspath(args.db),
                        "authentication": {
                            "bearer": bool(token),
                            "hmac": bool(hmac_secret),
                        },
                        "maintenance": {
                            "enabled": policy is not None,
                            "policy": policy.public() if policy else None,
                        },
                    },
                    args.format,
                    [f"production evidence gateway: http://{host}:{port}"],
                )
                gateway.thread.join()
            except KeyboardInterrupt:
                pass
            finally:
                try:
                    if supervisor is not None:
                        supervisor.close()
                finally:
                    gateway.close()
            return 0
        if args.production_command == "ingest":
            value = _load_json(args.input, "production input")
            if args.otlp:
                results = store.ingest_otlp(
                    value,
                    source=args.source,
                    authority_kind=args.authority,
                    redact_payloads=not args.include_payloads,
                )
                payload = {
                    "schema": "hotato.production-ingest-result.v1",
                    "kind": "otlp",
                    "events": results,
                    "event_count": len(results),
                    "durability": "committed",
                }
            else:
                result = store.ingest(value, redact_payloads=not args.include_payloads)
                payload = {
                    "schema": "hotato.production-ingest-result.v1",
                    "kind": "event",
                    **result,
                }
            _emit(
                payload,
                args.format,
                [
                    f"production ingest: {payload.get('status', 'committed')}",
                    f"events: {payload.get('event_count', 1)}",
                ],
            )
            return 0
        if args.production_command == "status":
            manifest = store.manifest(args.session)
            _emit(
                manifest,
                args.format,
                [
                    f"session: {manifest['session_id']}",
                    f"status: {manifest['status']}",
                    f"events: {manifest['event_count']}",
                ],
            )
            return 0
        if args.production_command == "finalize":
            manifests = store.finalize(
                quiescence_seconds=args.quiescence,
                required_lanes=args.require_lane or production.EVIDENCE_LANES,
            )
            payload = {
                "schema": "hotato.production-finalization.v1",
                "finalized": manifests,
                "count": len(manifests),
            }
            _emit(payload, args.format, [f"finalized sessions: {len(manifests)}"])
            return 0
        if args.production_command == "maintain":
            from . import production_supervisor

            policy = production_supervisor.load_policy(args.policy)
            supervisor = production_supervisor.ProductionSupervisor(store, policy, autostart=False)
            try:
                payload = supervisor.run_once()
            finally:
                supervisor.close()
            _emit(
                payload,
                args.format,
                [
                    f"finalized: {payload['finalized_count']}",
                    f"alert transitions: {payload['alert_transition_count']}",
                    f"retention deletions: {payload['retention_deletion_count']}",
                ],
            )
            return 0
        if args.production_command == "alerts":
            rules = _load_json(args.rules, "production alert rules")
            if not isinstance(rules, list):
                raise ValueError("production alert rules must be a JSON list")
            changes = store.evaluate_alerts(rules)
            payload = {
                "schema": "hotato.production-alert-evaluation.v1",
                "transitions": changes,
                "transition_count": len(changes),
            }
            _emit(payload, args.format, [f"alert transitions: {len(changes)}"])
            return 0
        if args.production_command == "export-regression":
            result = store.export_regression_candidate(args.session, args.out)
            _emit(
                result,
                args.format,
                [f"regression candidate: {result['candidate_id']}", f"output: {result['path']}"],
            )
            return 0
        if args.production_command == "audit":
            verification = store.verify_audit_chain()
            _emit(
                verification,
                args.format,
                [
                    ("VERIFIED" if verification.get("valid") else "REFUSED")
                    + f": {verification.get('entries', 0)} audit entries"
                ],
            )
            return 0 if verification.get("valid") else 2
        receipt = store.delete_session(args.session, reason=args.reason)
        _emit(
            receipt,
            args.format,
            [
                f"deleted session payloads: {receipt['subject_sha256']}",
                f"receipt: {receipt['receipt_id']}",
            ],
        )
        return 0
    except KeyError as exc:
        raise ValueError(f"production session is unknown: {exc.args[0]}") from exc
    except (production.ProductionError, production.EventConflict) as exc:
        raise ValueError(f"production operation failed: {exc}") from exc
    finally:
        if store is not None:
            store.close()


def _add_transport_arguments(
    parser: argparse.ArgumentParser, *, allow_direct_livekit: bool = True
) -> None:
    if allow_direct_livekit:
        target = parser.add_mutually_exclusive_group(required=True)
        target.add_argument("--target-ws", metavar="WS_URL")
        target.add_argument("--livekit-url", metavar="WS_URL")
    else:
        parser.add_argument("--target-ws", required=True, metavar="WS_URL")
        # One static room token and participant identity cannot safely be used
        # by concurrent processes.  Load uses a sidecar until an official
        # LiveKit token minter supplies a unique short-lived token per child.
        parser.set_defaults(
            livekit_url=None,
            livekit_target_identity=None,
            livekit_token_env=None,
            livekit_token_file=None,
            livekit_evidence_topic=None,
            livekit_sample_rate=None,
        )
    parser.add_argument("--header-env", action="append", default=[], metavar="HEADER=ENV_VAR")
    parser.add_argument("--allow-remote", action="store_true")
    parser.add_argument("--connect-timeout", type=float, default=10.0, metavar="SEC")
    parser.add_argument("--command-timeout", type=float, default=60.0, metavar="SEC")
    if allow_direct_livekit:
        parser.add_argument("--livekit-target-identity", metavar="IDENTITY")
        token = parser.add_mutually_exclusive_group()
        token.add_argument("--livekit-token-env", metavar="ENV_VAR")
        token.add_argument("--livekit-token-file", metavar="PATH")
        parser.add_argument("--livekit-evidence-topic", metavar="TOPIC")
        parser.add_argument("--livekit-sample-rate", type=int, metavar="HZ")
    parser.add_argument("--piper-model", metavar="MODEL.onnx")
    parser.add_argument("--piper-config", metavar="MODEL.onnx.json")
    parser.add_argument("--piper-command", default="piper", metavar="PATH")
    parser.add_argument("--piper-voice", default="default", metavar="LABEL")
    parser.add_argument("--piper-timeout", type=float, default=60.0, metavar="SEC")
    parser.add_argument(
        "--piper-max-output-bytes",
        type=int,
        default=32 * 1024 * 1024,
        metavar="BYTES",
    )
    parser.add_argument("--ollama-model", metavar="MODEL")
    parser.add_argument("--ollama-endpoint", default="http://127.0.0.1:11434", metavar="URL")
    parser.add_argument("--temperature", type=float, default=0.0)
    parser.add_argument("--seed", type=int, default=0)
    parser.add_argument("--model-timeout", type=float, default=60.0, metavar="SEC")


def _register_telephony(subparsers: Any) -> None:
    parser = subparsers.add_parser("telephony", help="provider call lifecycle evidence")
    commands = parser.add_subparsers(dest="telephony_command", required=True)
    capabilities = commands.add_parser("capabilities")
    capabilities.add_argument(
        "--provider", required=True, choices=("twilio", "vapi", "retell", "local")
    )
    _add_format(capabilities)
    capabilities.set_defaults(func=_telephony)
    create = commands.add_parser("create")
    create.add_argument("spec", metavar="CALL.json")
    create.add_argument("--wait", action="store_true")
    create.add_argument("--timeout", type=float, default=600.0)
    create.add_argument("--poll-seconds", type=float, default=2.0)
    create.add_argument("--export-dir", metavar="DIR")
    create.add_argument("--save-handle", metavar="FILE.json")
    _add_format(create)
    create.set_defaults(func=_telephony)
    status = commands.add_parser("status")
    status.add_argument("--provider", required=True, choices=("twilio", "vapi", "retell"))
    status.add_argument("--call-id", required=True)
    _add_format(status)
    status.set_defaults(func=_telephony)
    cancel = commands.add_parser("cancel")
    cancel.add_argument("--provider", required=True, choices=("twilio", "vapi", "retell", "local"))
    cancel.add_argument("--call-id", required=True)
    cancel.add_argument(
        "--status", default="queued", choices=("queued", "ringing", "answered", "in-progress")
    )
    _add_format(cancel)
    cancel.set_defaults(func=_telephony)
    export = commands.add_parser("export")
    export.add_argument("--provider", required=True, choices=("twilio", "vapi", "retell"))
    export.add_argument("--call-id", required=True)
    export.add_argument("--out", required=True, metavar="DIR")
    _add_format(export)
    export.set_defaults(func=_telephony)


def _register_caller(subparsers: Any) -> None:
    parser = subparsers.add_parser("caller", help="bounded scripted or generative caller")
    commands = parser.add_subparsers(dest="caller_command", required=True)
    run = commands.add_parser("run")
    run.add_argument("plan", metavar="PLAN.json")
    run.add_argument("--out", required=True, metavar="DIR")
    _add_transport_arguments(run)
    _add_format(run)
    run.set_defaults(func=_caller)
    verify = commands.add_parser("verify")
    verify.add_argument("directory", metavar="DIR")
    _add_format(verify)
    verify.set_defaults(func=_caller)


def _register_load(subparsers: Any) -> None:
    parser = subparsers.add_parser("load", help="telephony or caller-program load")
    families = parser.add_subparsers(dest="load_family", required=True)
    telephony = families.add_parser("telephony", help="provider lifecycle workload")
    telephony_commands = telephony.add_subparsers(dest="load_command", required=True)
    telephony_run = telephony_commands.add_parser("run")
    telephony_run.add_argument("plan", metavar="PLAN.json")
    telephony_run.add_argument("--out", required=True, metavar="DIR")
    _add_format(telephony_run)
    telephony_run.set_defaults(func=_load)
    telephony_verify = telephony_commands.add_parser("verify")
    telephony_verify.add_argument("directory", metavar="DIR")
    _add_format(telephony_verify)
    telephony_verify.set_defaults(func=_load)

    caller = families.add_parser("caller", help="full caller-program workload")
    caller_commands = caller.add_subparsers(dest="load_command", required=True)
    caller_run = caller_commands.add_parser("run")
    caller_run.add_argument("plan", metavar="PLAN.json")
    caller_run.add_argument("--out", required=True, metavar="DIR")
    caller_run.add_argument("--created-at", metavar="RFC3339")
    _add_transport_arguments(caller_run, allow_direct_livekit=False)
    _add_format(caller_run)
    caller_run.set_defaults(func=_load)
    caller_verify = caller_commands.add_parser("verify")
    caller_verify.add_argument("directory", metavar="DIR")
    _add_format(caller_verify)
    caller_verify.set_defaults(func=_load)


def _register_production(subparsers: Any) -> None:
    parser = subparsers.add_parser("production", help="durable production evidence plane")
    commands = parser.add_subparsers(dest="production_command", required=True)

    def operation(name: str) -> argparse.ArgumentParser:
        child = commands.add_parser(name)
        if name != "verify-regression":
            child.add_argument("--db", default=".hotato/production.sqlite3", metavar="FILE")
        return child

    serve = operation("serve")
    serve.add_argument("--host", default="127.0.0.1")
    serve.add_argument("--port", type=int, default=8322)
    bearer = serve.add_mutually_exclusive_group()
    bearer.add_argument("--token-env", metavar="ENV_VAR")
    bearer.add_argument("--token-file", metavar="PATH")
    hmac = serve.add_mutually_exclusive_group()
    hmac.add_argument("--hmac-secret-env", metavar="ENV_VAR")
    hmac.add_argument("--hmac-secret-file", metavar="PATH")
    serve.add_argument("--max-workers", type=int, default=16)
    serve.add_argument("--max-signature-skew", type=int, default=300)
    serve.add_argument("--request-timeout", type=float, default=30.0)
    serve.add_argument("--maintenance-policy", metavar="POLICY.json")
    _add_format(serve)
    serve.set_defaults(func=_production)

    ingest = operation("ingest")
    ingest.add_argument("input", metavar="INPUT.json")
    ingest.add_argument("--otlp", action="store_true")
    ingest.add_argument("--source", default="otlp")
    ingest.add_argument(
        "--authority",
        default="adapter_reported",
        choices=(
            "submitted",
            "adapter_reported",
            "provider_export",
            "signed_attestation",
            "measured",
        ),
    )
    ingest.add_argument("--include-payloads", action="store_true")
    _add_format(ingest)
    ingest.set_defaults(func=_production)

    status = operation("status")
    status.add_argument("session", metavar="SESSION_ID")
    _add_format(status)
    status.set_defaults(func=_production)
    finalize = operation("finalize")
    finalize.add_argument("--quiescence", type=float, default=30.0)
    finalize.add_argument(
        "--require-lane",
        action="append",
        default=None,
        choices=(
            "participant_audio",
            "transcript",
            "model_trace",
            "tool_calls",
            "backend_state",
        ),
    )
    _add_format(finalize)
    finalize.set_defaults(func=_production)
    maintain = operation("maintain")
    maintain.add_argument("policy", metavar="POLICY.json")
    _add_format(maintain)
    maintain.set_defaults(func=_production)
    alerts = operation("alerts")
    alerts.add_argument("rules", metavar="RULES.json")
    _add_format(alerts)
    alerts.set_defaults(func=_production)
    export = operation("export-regression")
    export.add_argument("session", metavar="SESSION_ID")
    export.add_argument("--out", required=True, metavar="DIR")
    _add_format(export)
    export.set_defaults(func=_production)
    verify = operation("verify-regression")
    verify.add_argument("directory", metavar="DIR")
    _add_format(verify)
    verify.set_defaults(func=_production)
    audit = operation("audit")
    _add_format(audit)
    audit.set_defaults(func=_production)
    delete = operation("delete")
    delete.add_argument("session", metavar="SESSION_ID")
    delete.add_argument("--reason", default="operator_request")
    _add_format(delete)
    delete.set_defaults(func=_production)


def _assign_exit_epilogs(
    parser: argparse.ArgumentParser,
    prefix: str,
    factory: Callable[[str], str],
) -> None:
    """Attach the parent CLI's exit contract to this whole parser tree."""

    parser.epilog = factory(prefix)
    for action in parser._actions:
        if isinstance(action, argparse._SubParsersAction):
            for name, child in action.choices.items():
                _assign_exit_epilogs(child, f"{prefix} {name}", factory)


def register(
    subparsers: Any,
    *,
    epilog_factory: Optional[Callable[[str], str]] = None,
) -> None:
    """Register all operational command families exactly once.

    A semantic integration against a newer CLI should call this immediately
    after creating its top-level ``argparse`` subparser action.  Refusing any
    partial collision prevents a mixed old/new command grammar.
    """

    choices = getattr(subparsers, "choices", None)
    if not isinstance(choices, dict) or not hasattr(subparsers, "add_parser"):
        raise TypeError("register() requires an argparse subparsers action")
    duplicates = sorted(set(_COMMANDS).intersection(choices))
    if duplicates:
        raise ValueError("operational CLI commands already registered: " + ", ".join(duplicates))
    _register_telephony(subparsers)
    _register_caller(subparsers)
    _register_load(subparsers)
    _register_production(subparsers)
    if epilog_factory is not None:
        for name in _COMMANDS:
            _assign_exit_epilogs(choices[name], name, epilog_factory)


__all__ = ["register"]
