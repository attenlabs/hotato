from __future__ import annotations

import hashlib
import os

import pytest

from hotato import call_runtime
from hotato.call_runtime import (
    AppendOnlyCallLog,
    CallController,
    CapabilityState,
    CapabilityUnavailable,
    ConversationSession,
    RuntimeContractError,
    SidecarError,
    SippSubprocessAdapter,
    canonical_event_hash,
    capability,
    livekit_sip_contract,
    normalize_call_event,
    pipecat_media_contract,
    require_capability,
    validate_sipp_spec,
)


def _event(sequence=0, previous=None, monotonic=100):
    raw = f"raw-{sequence}".encode()
    return {
        "schema": "hotato.call-event.v1",
        "event_id": f"event-{sequence}",
        "run_id": "run-1",
        "call_id": "call-1",
        "leg_id": "caller",
        "sequence": sequence,
        "source": "fake-session",
        "kind": "audio.delivered",
        "observed_monotonic_ns": monotonic,
        "source_timestamp": None,
        "trace_id": "trace-1",
        "payload": {"bytes": len(raw)},
        "raw_sha256": "sha256:" + hashlib.sha256(raw).hexdigest(),
        "trust": "measured",
        "previous_event_hash": previous,
    }


def test_call_event_chain_is_canonical_append_only_and_verifiable():
    log = AppendOnlyCallLog()
    first = log.append(_event())
    second = log.append(_event(1, first["event_hash"], 101))

    assert first["event_hash"] == canonical_event_hash(first)
    assert log.verify() == second["event_hash"]
    assert isinstance(log.snapshot(), tuple)

    tampered = dict(second)
    tampered["payload"] = {"bytes": 999}
    with pytest.raises(RuntimeContractError, match="event_hash"):
        normalize_call_event(tampered, first)


@pytest.mark.parametrize(
    "mutator, message",
    [
        (lambda event: event.update(sequence=2), "sequence"),
        (lambda event: event.update(previous_event_hash="sha256:" + "0" * 64), "previous_event_hash"),
        (lambda event: event.update(observed_monotonic_ns=99), "backwards"),
        (lambda event: event.update(call_id="other-call"), "cannot change"),
        (lambda event: event.update(extra=True), "unknown"),
    ],
)
def test_call_event_refuses_broken_chain(mutator, message):
    first = normalize_call_event(_event())
    second = _event(1, first["event_hash"], 101)
    mutator(second)
    with pytest.raises(RuntimeContractError, match=message):
        normalize_call_event(second, first)


def test_duplicate_event_id_is_refused():
    first = normalize_call_event(_event())
    second = _event(1, first["event_hash"], 101)
    second["event_id"] = first["event_id"]
    log = AppendOnlyCallLog([first])
    with pytest.raises(RuntimeContractError, match="already present"):
        log.append(second)


def test_explicit_capability_states_gate_dtmf_hold_and_transfer():
    supported = capability(CapabilityState.SUPPORTED, "fake session records it")
    unobservable = capability(CapabilityState.UNOBSERVABLE, "fake provider returns no evidence")
    unsupported = capability(CapabilityState.UNSUPPORTED, "fake session has no second leg")
    matrix = {"dtmf": supported, "hold": unobservable, "warm_transfer": unsupported}

    require_capability(matrix, "dtmf")
    with pytest.raises(CapabilityUnavailable, match="UNOBSERVABLE"):
        require_capability(matrix, "hold")
    with pytest.raises(CapabilityUnavailable, match="UNSUPPORTED"):
        require_capability(matrix, "warm_transfer")


def test_protocols_accept_a_hermetic_controller_and_duplex_session():
    supported = capability(CapabilityState.SUPPORTED, "recorded by fake")

    class FakeController:
        def capabilities(self, provider):
            return {"create": supported}

        def create(self, spec):
            return spec

        def get(self, provider, call_id):
            return call_id

        def wait(self, handle, **kwargs):
            return handle

        def cancel(self, handle):
            return handle

        def export(self, handle, output_dir):
            return output_dir

        def cleanup(self, handle, export_path=None):
            return {"deleted": False}

    class FakeSession:
        def __init__(self):
            self.operations = []

        def capabilities(self):
            return {name: supported for name in ("media", "dtmf", "hold", "cold_transfer", "warm_transfer")}

        def connect(self):
            self.operations.append(("connect",))

        def events(self):
            return ()

        def send_audio(self, pcm_s16le, *, sample_rate_hz, channels=1):
            self.operations.append(("audio", len(pcm_s16le), sample_rate_hz, channels))

        def send_dtmf(self, digits):
            self.operations.append(("dtmf", digits))

        def hold(self, enabled):
            self.operations.append(("hold", enabled))

        def transfer(self, destination, *, warm=False):
            self.operations.append(("transfer", destination, warm))

        def hangup(self):
            self.operations.append(("hangup",))

        def close(self):
            self.operations.append(("close",))

    controller = FakeController()
    session = FakeSession()
    assert isinstance(controller, CallController)
    assert isinstance(session, ConversationSession)
    session.send_dtmf("12#")
    session.hold(True)
    session.transfer("sip:queue@example.test", warm=True)
    assert session.operations == [
        ("dtmf", "12#"),
        ("hold", True),
        ("transfer", "sip:queue@example.test", True),
    ]


def test_sidecar_contracts_do_not_claim_unobserved_media():
    livekit = livekit_sip_contract("http://127.0.0.1:7880")
    pipecat = pipecat_media_contract("ws://127.0.0.1:9000/media")

    assert livekit.kind == "livekit-sip"
    assert livekit.capabilities["dtmf"].state is CapabilityState.UNOBSERVABLE
    assert "delivered_audio_sha256" in livekit.evidence_required
    assert pipecat.capabilities["hold"].state is CapabilityState.UNSUPPORTED


def test_sipp_adapter_uses_fixed_argv_minimal_environment_and_bounded_artifacts(tmp_path, monkeypatch):
    scenario = tmp_path / "scenario.xml"
    scenario.write_text("<scenario></scenario>", encoding="utf-8")
    monkeypatch.setenv("SECRET_THAT_MUST_NOT_CROSS", "do-not-send")
    observed = {}

    def runner(argv, cwd, timeout, env):
        observed.update(argv=tuple(argv), cwd=cwd, timeout=timeout, env=dict(env))
        return 0, b"one call passed\n", b""

    out = tmp_path / "evidence"
    receipt = SippSubprocessAdapter(runner).run(
        {
            "target": "127.0.0.1:5060",
            "scenario_path": str(scenario),
            "calls": 3,
            "rate_per_second": 2,
            "timeout_seconds": 9,
        },
        str(out),
    )

    assert observed["argv"] == (
        "sipp", "127.0.0.1:5060", "-sf", str((out / "scenario.xml").resolve()),
        "-m", "3", "-r", "2", "-nostdin",
    )
    assert "SECRET_THAT_MUST_NOT_CROSS" not in observed["env"]
    assert observed["timeout"] == 9
    assert receipt["status"] == "PASS"
    assert receipt["scenario_policy"] == {
        "schema": "hotato.sipp-scenario-policy.v1",
        "profile": "safe_default",
        "authority": "hotato_static_validation",
        "command_and_host_file_features": "DENIED_BY_STATIC_POLICY",
        "destination_redirection": "DENIED_BY_STATIC_POLICY",
        "dtd_and_entity_resolution": "DENIED",
        "os_process_sandbox": "ABSENT",
    }
    assert receipt["target_sha256"].startswith("sha256:")
    assert (out / "scenario.xml").read_bytes() == scenario.read_bytes()
    assert (out / "sipp.stdout").read_bytes() == b"one call passed\n"
    assert (out / "sipp.receipt.json").is_file()


def test_sipp_adapter_refuses_injection_symlinks_and_oversized_runner_output(tmp_path):
    scenario = tmp_path / "scenario.xml"
    scenario.write_text("<scenario />", encoding="utf-8")
    with pytest.raises(RuntimeContractError, match="target"):
        validate_sipp_spec({"target": "-trace_msg", "scenario_path": str(scenario)})

    link = tmp_path / "scenario-link.xml"
    try:
        link.symlink_to(scenario)
    except (OSError, NotImplementedError):
        pytest.skip("symlink creation unavailable")
    with pytest.raises(RuntimeContractError, match="non-symlink"):
        validate_sipp_spec({"target": "example.test", "scenario_path": str(link)})

    adapter = SippSubprocessAdapter(lambda argv, cwd, timeout, env: (0, b"x" * (1024 * 1024 + 1), b""))
    with pytest.raises(SidecarError, match="exceeded"):
        adapter.run({"target": "127.0.0.1", "scenario_path": str(scenario)}, str(tmp_path / "out"))


def test_sipp_artifacts_are_exclusive(tmp_path):
    scenario = tmp_path / "scenario.xml"
    scenario.write_text("<scenario />", encoding="utf-8")
    adapter = SippSubprocessAdapter(lambda argv, cwd, timeout, env: (0, b"", b""))
    out = tmp_path / "out"
    adapter.run({"target": "127.0.0.1", "scenario_path": str(scenario)}, str(out))
    with pytest.raises(FileExistsError):
        adapter.run({"target": "127.0.0.1", "scenario_path": str(scenario)}, str(out))


def test_sipp_remote_execution_is_allowlisted_acknowledged_and_ip_bound(
    tmp_path, monkeypatch
):
    scenario = tmp_path / "scenario.xml"
    scenario.write_text("<scenario />", encoding="utf-8")
    remote_ip = "203.0.113.10"
    monkeypatch.setattr(
        call_runtime.socket,
        "getaddrinfo",
        lambda *_args, **_kwargs: [
            (call_runtime.socket.AF_INET, call_runtime.socket.SOCK_DGRAM, 17, "", (remote_ip, 5060))
        ],
    )
    observed = {}

    def runner(argv, cwd, timeout, env):
        observed["argv"] = tuple(argv)
        return 0, b"", b""

    adapter = SippSubprocessAdapter(runner)
    base = {
        "target": "voice.example.test:5060",
        "scenario_path": str(scenario),
        "calls": 2,
    }
    with pytest.raises(RuntimeContractError, match="default-deny"):
        adapter.run(base, str(tmp_path / "denied"))
    with pytest.raises(RuntimeContractError, match="max_remote_calls"):
        validate_sipp_spec({
            **base,
            "allow_remote": True,
            "remote_ip_allowlist": [remote_ip],
            "remote_acknowledgement": call_runtime.SIPP_REMOTE_ACKNOWLEDGEMENT,
            "max_remote_calls": 1,
        })

    receipt = adapter.run(
        {
            **base,
            "allow_remote": True,
            "remote_ip_allowlist": [remote_ip],
            "remote_acknowledgement": call_runtime.SIPP_REMOTE_ACKNOWLEDGEMENT,
            "max_remote_calls": 2,
        },
        str(tmp_path / "allowed"),
    )
    assert observed["argv"][1] == f"{remote_ip}:5060"
    assert receipt["resolved_destination"] == f"{remote_ip}:5060"
    assert receipt["network_scope"] == "remote"
    assert receipt["remote_authorization"] == {
        "allowed": True,
        "max_remote_calls": 2,
        "external_cost_state": "UNOBSERVABLE",
        "external_cost_microusd": None,
    }


@pytest.mark.skipif(not hasattr(os, "mkfifo"), reason="FIFOs require POSIX")
def test_sipp_scenario_refuses_fifo_without_waiting_for_a_writer(tmp_path):
    scenario = tmp_path / "scenario.xml"
    os.mkfifo(scenario)
    with pytest.raises(RuntimeContractError, match="regular, non-symlink"):
        validate_sipp_spec(
            {"target": "example.test", "scenario_path": str(scenario)}
        )


def test_sipp_scenario_read_binds_precheck_to_opened_inode(tmp_path, monkeypatch):
    scenario = tmp_path / "scenario.xml"
    replacement = tmp_path / "replacement.xml"
    scenario.write_text("<scenario name='first'/>", encoding="utf-8")
    replacement.write_text("<scenario name='second'/>", encoding="utf-8")
    original_open = call_runtime.os.open

    def swapped(path, flags, *args):
        if os.fspath(path) == os.fspath(scenario):
            return original_open(replacement, flags, *args)
        return original_open(path, flags, *args)

    monkeypatch.setattr(call_runtime.os, "open", swapped)
    adapter = SippSubprocessAdapter(
        lambda argv, cwd, timeout, env: (0, b"", b"")
    )
    with pytest.raises(RuntimeContractError, match="changed while it was being opened"):
        adapter.run(
            {"target": "127.0.0.1", "scenario_path": str(scenario)},
            str(tmp_path / "out"),
        )


@pytest.mark.parametrize(
    "xml, message",
    [
        (
            b"<scenario><nop><action><exec command='touch /tmp/hotato-pwned'/>"
            b"</action></nop></scenario>",
            "exec actions",
        ),
        (
            b"<scenario xmlns:x='urn:adversarial'><nop><action>"
            b"<x:exec command='touch /tmp/hotato-pwned'/></action></nop></scenario>",
            "exec actions",
        ),
        (
            b"<scenario><nop><action><setdest host='192.0.2.1' port='5060' "
            b"protocol='udp'/></action></nop></scenario>",
            "setdest actions",
        ),
        (
            b"<!DOCTYPE scenario [<!ENTITY xxe SYSTEM 'file:///etc/passwd'>]>"
            b"<scenario>&xxe;</scenario>",
            "DTD",
        ),
        (
            b"<?host-command run='id'?><scenario/>",
            "processing instructions",
        ),
        (
            b"<scenario><nop file='/etc/passwd'/></scenario>",
            "command/file attributes",
        ),
        (
            b"<scenario><nop source='../private.key'/></scenario>",
            "file/path references",
        ),
        (
            b"<scenario><nop source='&#46;&#46;/private.key'/></scenario>",
            "file/path references",
        ),
        (
            b"<scenario><send><![CDATA[INVITE sip:x SIP/2.0\r\n"
            b"X-Leak: [file name=/etc/passwd]\r\n\r\n]]></send></scenario>",
            "host-file keywords",
        ),
    ],
)
def test_sipp_safe_profile_rejects_executable_xml_and_host_file_access(
    tmp_path, xml, message
):
    scenario = tmp_path / "scenario.xml"
    scenario.write_bytes(xml)
    called = []
    adapter = SippSubprocessAdapter(
        lambda argv, cwd, timeout, env: (called.append(tuple(argv)) or (0, b"", b""))
    )

    with pytest.raises(RuntimeContractError, match=message):
        adapter.run(
            {"target": "127.0.0.1", "scenario_path": str(scenario)},
            str(tmp_path / "out"),
        )
    assert called == []


def test_sipp_safe_profile_accepts_bounded_benign_scenario(tmp_path):
    scenario = tmp_path / "scenario.xml"
    scenario.write_bytes(
        b"<?xml version='1.0'?>\n"
        b"<scenario name='basic'>"
        b"<send><![CDATA[INVITE sip:[service]@[remote_ip]:[remote_port] SIP/2.0\r\n"
        b"Content-Length: [len]\r\n\r\n]]></send>"
        b"<recv response='200'/><pause milliseconds='10'/></scenario>"
    )
    receipt = SippSubprocessAdapter(
        lambda argv, cwd, timeout, env: (0, b"", b"")
    ).run(
        {"target": "127.0.0.1", "scenario_path": str(scenario)},
        str(tmp_path / "out"),
    )

    assert receipt["status"] == "PASS"
    assert receipt["scenario_policy"]["profile"] == "safe_default"
    assert receipt["scenario_policy"]["os_process_sandbox"] == "ABSENT"


def test_sipp_trusted_scenario_escape_hatch_is_fixed_and_receipted(tmp_path):
    scenario = tmp_path / "scenario.xml"
    scenario.write_text(
        "<scenario><nop><action><exec command='printf trusted'/>"
        "</action></nop></scenario>",
        encoding="utf-8",
    )
    base = {"target": "127.0.0.1", "scenario_path": str(scenario)}
    with pytest.raises(RuntimeContractError, match="fixed phrase"):
        validate_sipp_spec(
            {**base, "trusted_scenario_acknowledgement": "I trust this file"}
        )
    with pytest.raises(RuntimeContractError, match="exec actions"):
        validate_sipp_spec(base)

    receipt = SippSubprocessAdapter(
        lambda argv, cwd, timeout, env: (0, b"", b"")
    ).run(
        {
            **base,
            "trusted_scenario_acknowledgement": (
                call_runtime.SIPP_TRUSTED_SCENARIO_ACKNOWLEDGEMENT
            ),
        },
        str(tmp_path / "trusted-out"),
    )
    assert receipt["scenario_policy"] == {
        "schema": "hotato.sipp-scenario-policy.v1",
        "profile": "trusted_host_access",
        "authority": "operator_attested",
        "command_and_host_file_features": "UNRESTRICTED",
        "destination_redirection": "DENIED_BY_STATIC_POLICY",
        "dtd_and_entity_resolution": "DENIED",
        "os_process_sandbox": "ABSENT",
    }


def test_sipp_trusted_scenario_cannot_redirect_destination(tmp_path):
    scenario = tmp_path / "scenario.xml"
    scenario.write_text(
        "<scenario><nop><action><setdest host='192.0.2.1' port='5060' "
        "protocol='udp'/></action></nop></scenario>",
        encoding="utf-8",
    )
    with pytest.raises(RuntimeContractError, match="static destination policy"):
        validate_sipp_spec(
            {
                "target": "127.0.0.1",
                "scenario_path": str(scenario),
                "trusted_scenario_acknowledgement": (
                    call_runtime.SIPP_TRUSTED_SCENARIO_ACKNOWLEDGEMENT
                ),
            }
        )


def test_sipp_trusted_scenario_never_enables_dtd_or_entity_resolution(tmp_path):
    scenario = tmp_path / "scenario.xml"
    scenario.write_text(
        "<!DOCTYPE scenario [<!ENTITY host SYSTEM 'file:///etc/passwd'>]>"
        "<scenario>&host;</scenario>",
        encoding="utf-8",
    )
    with pytest.raises(RuntimeContractError, match="DTD"):
        validate_sipp_spec(
            {
                "target": "127.0.0.1",
                "scenario_path": str(scenario),
                "trusted_scenario_acknowledgement": (
                    call_runtime.SIPP_TRUSTED_SCENARIO_ACKNOWLEDGEMENT
                ),
            }
        )


def test_sipp_remote_and_trusted_scenario_acknowledgements_are_independent(
    tmp_path, monkeypatch
):
    scenario = tmp_path / "scenario.xml"
    scenario.write_text(
        "<scenario><nop><action><exec int_cmd='stop_now'/></action></nop></scenario>",
        encoding="utf-8",
    )
    remote_ip = "203.0.113.20"
    monkeypatch.setattr(
        call_runtime.socket,
        "getaddrinfo",
        lambda *_args, **_kwargs: [
            (
                call_runtime.socket.AF_INET,
                call_runtime.socket.SOCK_DGRAM,
                17,
                "",
                (remote_ip, 5060),
            )
        ],
    )
    base = {
        "target": "voice.example.test:5060",
        "scenario_path": str(scenario),
        "allow_remote": True,
        "remote_ip_allowlist": [remote_ip],
        "max_remote_calls": 1,
    }
    with pytest.raises(RuntimeContractError, match="remote acknowledgement"):
        validate_sipp_spec(
            {
                **base,
                "trusted_scenario_acknowledgement": (
                    call_runtime.SIPP_TRUSTED_SCENARIO_ACKNOWLEDGEMENT
                ),
            }
        )
    with pytest.raises(RuntimeContractError, match="exec actions"):
        validate_sipp_spec(
            {
                **base,
                "remote_acknowledgement": call_runtime.SIPP_REMOTE_ACKNOWLEDGEMENT,
            }
        )
