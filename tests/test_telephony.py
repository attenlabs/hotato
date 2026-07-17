from __future__ import annotations

import json
import os

import pytest

from hotato.call_runtime import CapabilityState, CapabilityUnavailable
from hotato.telephony import CallHandle, TelephonyClient, TelephonyError, validate_spec


class FakeTransport:
    def __init__(self, responses):
        self.responses = list(responses)
        self.requests = []

    def request(self, method, url, *, headers, body, timeout):
        self.requests.append({"method": method, "url": url, "headers": dict(headers), "body": body, "timeout": timeout})
        return self.responses.pop(0)


def _local_spec():
    return {
        "schema": "hotato.telephony-call.v1",
        "id": "local-test",
        "provider": "local",
        "to": "fixture",
        "record": False,
    }


def test_local_lifecycle_export_and_bounded_cleanup(tmp_path):
    client = TelephonyClient(clock=lambda: 1_700_000_000)
    handle = client.create(_local_spec())
    completed = client.wait(handle, timeout_seconds=1, poll_seconds=0.01)
    canceled = client.cancel(handle)

    assert handle.normalized_status == "queued"
    assert completed.normalized_status == "completed"
    assert canceled.normalized_status == "canceled"

    path = client.export(canceled, str(tmp_path / "exports"))
    doc = json.loads(open(path, encoding="utf-8").read())
    assert doc["schema"] == "hotato.telephony-export.v1"
    assert doc["limitations"]
    cleanup = client.cleanup(canceled, path)
    assert cleanup["local_export_deleted"] is True
    assert cleanup["provider_record_deleted"] is False


def test_lifecycle_capabilities_separate_media_and_provider_limits():
    client = TelephonyClient()
    twilio = client.capabilities("twilio")
    vapi = client.capabilities("vapi")
    local = client.capabilities("local")

    assert twilio["cancel"].state is CapabilityState.SUPPORTED
    assert twilio["media"].state is CapabilityState.UNOBSERVABLE
    assert vapi["cancel"].state is CapabilityState.UNSUPPORTED
    assert local["status"].state is CapabilityState.UNSUPPORTED
    assert all(entry.state is not CapabilityState.SUPPORTED for name, entry in twilio.items() if name in {"media", "dtmf", "hold", "cold_transfer", "warm_transfer"})


def test_twilio_create_get_cancel_and_receipts_never_contain_credentials(monkeypatch):
    monkeypatch.setenv("TWILIO_ACCOUNT_SID", "AC-public-id")
    monkeypatch.setenv("TWILIO_AUTH_TOKEN", "super-secret-token")
    transport = FakeTransport([
        (201, {}, json.dumps({"sid": "CA-1", "status": "queued", "token": "echoed-secret", "to": "+15550001", "recordingUrl": "https://signed.example/audio?token=x"}).encode()),
        (200, {}, json.dumps({"sid": "CA-1", "status": "in-progress", "transcript": "private words"}).encode()),
        (200, {}, json.dumps({"sid": "CA-1", "status": "canceled", "authorization": "echoed-auth"}).encode()),
    ])
    client = TelephonyClient(transport, clock=lambda: 1_700_000_000)
    spec = {
        "schema": "hotato.telephony-call.v1",
        "id": "twilio-test",
        "provider": "twilio",
        "to": "+15550001",
        "from": "+15550002",
        "twiml_url": "https://example.test/twiml",
    }

    created = client.create(spec)
    observed = client.get("twilio", created.call_id)
    canceled = client.cancel(observed)

    receipts = json.dumps([created.receipt, observed.receipt, canceled.receipt], sort_keys=True)
    assert "super-secret-token" not in receipts
    assert "echoed-secret" not in receipts
    assert "private words" not in receipts
    assert "echoed-auth" not in receipts
    assert "signed.example" not in receipts
    assert transport.requests[0]["url"].startswith("https://api.twilio.com/")
    assert transport.requests[2]["body"] == b"Status=completed"


def test_provider_receipt_allowlist_refuses_unknown_nested_payloads(monkeypatch):
    monkeypatch.setenv("VAPI_API_KEY", "provider-secret-that-must-not-escape")
    transport = FakeTransport([
        (201, {}, json.dumps({
            "id": "call-1",
            "status": "queued",
            "customer": {"number": "+15550001111"},
            "artifact": {
                "messages": [{"content": "private medical statement"}],
                "recordingUrl": "https://signed.example/private.wav?token=x",
            },
            "metadata": {"patient_name": "Sensitive Name"},
            "subscriptionLimits": {
                "concurrencyBlocked": False,
                "concurrencyLimit": 10,
                "remainingConcurrentCalls": 9,
                "internalCustomerLabel": "secret-label",
            },
        }).encode()),
    ])
    client = TelephonyClient(transport, clock=lambda: 1_700_000_000)
    handle = client.create({
        "schema": "hotato.telephony-call.v1",
        "id": "vapi-test",
        "provider": "vapi",
        "to": "+15550001111",
        "agent_id": "agent-1",
        "phone_number_id": "phone-1",
    })
    serialized = json.dumps(handle.receipt, sort_keys=True)
    assert "private medical statement" not in serialized
    assert "Sensitive Name" not in serialized
    assert "signed.example" not in serialized
    assert "secret-label" not in serialized
    summary = handle.receipt["provider_response"]
    assert summary["selected"]["status"] == "queued"
    assert summary["selected"]["subscriptionLimits"] == {
        "concurrencyBlocked": False,
        "concurrencyLimit": 10,
        "remainingConcurrentCalls": 9,
    }
    assert summary["payload_policy"] == "fixed_lifecycle_allowlist"
    assert transport.requests[0]["url"] == "https://api.vapi.ai/call"


@pytest.mark.parametrize(
    "provider,env_name,response,spec_fields",
    [
        (
            "vapi",
            "VAPI_API_KEY",
            {"id": "call-vapi", "status": "queued"},
            {"agent_id": "agent-1", "phone_number_id": "phone-1"},
        ),
        (
            "retell",
            "RETELL_API_KEY",
            {"call_id": "call-retell", "call_status": "registered"},
            {"agent_id": "agent-1", "from": "+15550002222"},
        ),
    ],
)
def test_provider_payload_preserves_reserved_run_correlation(
    monkeypatch, provider, env_name, response, spec_fields
):
    monkeypatch.setenv(env_name, "provider-secret")
    transport = FakeTransport([(201, {}, json.dumps(response).encode())])
    spec = {
        "schema": "hotato.telephony-call.v1",
        "id": "run-correlation-1",
        "provider": provider,
        "to": "+15550001111",
        "metadata": {"campaign": "regression"},
        **spec_fields,
    }
    TelephonyClient(transport).create(spec)
    body = json.loads(transport.requests[0]["body"])
    assert body["metadata"] == {
        "campaign": "regression",
        "hotato_run_id": "run-correlation-1",
    }
    spec["metadata"]["hotato_run_id"] = "attacker-controlled"
    with pytest.raises(ValueError, match="reserved"):
        validate_spec(spec)


def test_lifecycle_allowlist_omits_freeform_reason_and_identifier_fields(monkeypatch):
    monkeypatch.setenv("VAPI_API_KEY", "provider-secret")
    transport = FakeTransport([(
        201,
        {},
        json.dumps({
            "id": "phone-number-disguised-as-id-+15550001111",
            "status": "queued",
            "endedReason": "caller said SSN 123-45-6789",
            "disconnection_reason": "card 4111111111111111",
            "duration": 12,
            "createdAt": "2026-07-17T12:00:00Z",
        }).encode(),
    )])
    handle = TelephonyClient(transport).create({
        "schema": "hotato.telephony-call.v1",
        "id": "vapi-private-fields",
        "provider": "vapi",
        "to": "+15550001111",
        "agent_id": "agent-1",
        "phone_number_id": "phone-1",
    })
    receipt = json.dumps(handle.receipt, sort_keys=True)
    assert "123-45-6789" not in receipt
    assert "4111111111111111" not in receipt
    assert "phone-number-disguised" not in receipt
    assert handle.receipt["provider_response"]["selected"] == {
        "createdAt": "2026-07-17T12:00:00Z",
        "duration": 12,
        "status": "queued",
    }


@pytest.mark.parametrize("provider", ["vapi", "retell"])
def test_unwired_provider_cancel_refuses_without_network(provider):
    transport = FakeTransport([])
    client = TelephonyClient(transport)
    handle = CallHandle(provider, "call-1", "queued", "queued", "2026-01-01T00:00:00Z", {"receipt_id": "sha256:" + "0" * 64})
    with pytest.raises(CapabilityUnavailable, match="UNSUPPORTED"):
        client.cancel(handle)
    assert transport.requests == []


@pytest.mark.parametrize(
    "change, message",
    [
        ({"schema": "wrong"}, "schema"),
        ({"provider": "unknown"}, "provider"),
        ({"id": "../escape"}, "filesystem-safe"),
        ({"extra": True}, "unknown field"),
        ({"timeout_seconds": 0}, "timeout_seconds"),
        ({"record": "false"}, "record"),
    ],
)
def test_call_spec_refuses_invalid_input(change, message):
    doc = _local_spec()
    doc.update(change)
    with pytest.raises(ValueError, match=message):
        validate_spec(doc)


def test_export_is_exclusive_and_cleanup_requires_matching_handle(tmp_path):
    client = TelephonyClient(clock=lambda: 1_700_000_000)
    handle = client.cancel(client.create(_local_spec()))
    path = client.export(handle, str(tmp_path))
    with pytest.raises(TelephonyError, match="overwrite"):
        client.export(handle, str(tmp_path))

    other = CallHandle("local", "local-other", "canceled", "canceled", handle.created_at, handle.receipt)
    with pytest.raises(TelephonyError, match="does not belong"):
        client.cleanup(other, path)


def test_cleanup_refuses_fifo_and_check_open_swap(tmp_path, monkeypatch):
    client = TelephonyClient(clock=lambda: 1_700_000_000)
    handle = client.cancel(client.create(_local_spec()))
    fifo = tmp_path / "fifo"
    os.mkfifo(fifo)
    with pytest.raises(TelephonyError, match="regular"):
        client.cleanup(handle, str(fifo))

    path = client.export(handle, str(tmp_path / "exports"))
    replacement = tmp_path / "replacement.json"
    replacement.write_text("{}", encoding="utf-8")
    original_open = os.open

    def swapped(name, flags, *args, **kwargs):
        if str(name) == str(path):
            return original_open(replacement, flags, *args, **kwargs)
        return original_open(name, flags, *args, **kwargs)

    monkeypatch.setattr("hotato.telephony.os.open", swapped)
    with pytest.raises(TelephonyError, match="changed"):
        client.cleanup(handle, path)


def test_cleanup_refuses_path_replacement_after_verified_read(tmp_path, monkeypatch):
    client = TelephonyClient(clock=lambda: 1_700_000_000)
    handle = client.cancel(client.create(_local_spec()))
    path = client.export(handle, str(tmp_path / "exports"))
    replacement = tmp_path / "replacement.json"
    replacement.write_text('{"owner":"unrelated"}', encoding="utf-8")

    from hotato import telephony as module

    original = module._read_regular_bytes_with_identity

    def swap_after_read(name, maximum):
        value = original(name, maximum)
        os.replace(replacement, name)
        return value

    monkeypatch.setattr(module, "_read_regular_bytes_with_identity", swap_after_read)
    with pytest.raises(TelephonyError, match="changed before cleanup"):
        client.cleanup(handle, path)
    assert json.loads(open(path, encoding="utf-8").read()) == {"owner": "unrelated"}


def test_wait_bounds_and_unknown_provider_refuse():
    client = TelephonyClient(clock=lambda: 1_700_000_000)
    handle = client.create(_local_spec())
    with pytest.raises(ValueError, match="timeout_seconds"):
        client.wait(handle, timeout_seconds=0)
    with pytest.raises(ValueError, match="provider"):
        client.capabilities("other")
