"""Drift guards for the shared provider lifecycle boundary.

``hotato.telephony`` owns signalling endpoints, call-id extraction, status
normalization, and privacy-preserving receipts. ``hotato.drive`` owns scripted
caller composition plus validated recording capture. These tests make an
accidental second provider contract visible before release.
"""

from __future__ import annotations

import json
from pathlib import Path

import jsonschema
import pytest

from hotato import capture, drive, telephony
from hotato._engine.audio import read_wav
from tests import _drive_fakes as fakes


def test_canonical_provider_endpoint_contract_is_exact_and_path_safe():
    assert telephony.provider_lifecycle_url(
        "twilio", "create", account_sid="AC /?"
    ) == (
        "https://api.twilio.com/2010-04-01/Accounts/AC%20%2F%3F/Calls.json"
    )
    assert telephony.provider_lifecycle_url(
        "twilio", "get", account_sid="AC1", call_id="CA /?"
    ) == (
        "https://api.twilio.com/2010-04-01/Accounts/AC1/Calls/CA%20%2F%3F.json"
    )
    assert telephony.provider_lifecycle_url("vapi", "create") == (
        "https://api.vapi.ai/call"
    )
    assert telephony.provider_lifecycle_url(
        "vapi", "get", call_id="call /?"
    ) == "https://api.vapi.ai/call/call%20%2F%3F"
    assert telephony.provider_lifecycle_url("retell", "create") == (
        "https://api.retellai.com/v2/create-phone-call"
    )
    assert telephony.provider_lifecycle_url(
        "retell", "get", call_id="call /?"
    ) == "https://api.retellai.com/v2/get-call/call%20%2F%3F"


def test_drive_status_exports_are_derived_from_shared_lifecycle_contract():
    assert drive.TWILIO_TERMINAL_STATES == (
        telephony.provider_lifecycle_contract("twilio")
        .terminal_provider_statuses
    )
    assert drive.VAPI_TERMINAL_STATES == (
        telephony.provider_lifecycle_contract("vapi")
        .terminal_provider_statuses
    )
    assert telephony.normalize_provider_status("vapi", "ended") == "completed"
    assert telephony.normalize_provider_status("retell", "ongoing") == "in-progress"
    assert telephony.normalize_provider_status("retell", "not_connected") == "no-answer"
    for raw in drive.TWILIO_TERMINAL_STATES:
        assert telephony.provider_status_is_terminal("twilio", raw)
    for raw in drive.VAPI_TERMINAL_STATES:
        assert telephony.provider_status_is_terminal("vapi", raw)
    assert telephony.provider_status_is_success("twilio", "completed")
    assert telephony.provider_status_is_success("vapi", "ended")
    assert not telephony.provider_status_is_success("vapi", "failed")


@pytest.mark.parametrize(
    "provider,capture_call,expected_message",
    [
        (
            "vapi",
            lambda: capture.capture_vapi(
                call_id="call /?", api_key="secret", base_url="https://unit.test"
            ),
            "no stereo recording",
        ),
        (
            "retell",
            lambda: capture.capture_retell(
                call_id="call /?", api_key="secret", base_url="https://unit.test"
            ),
            "no recording on this Retell call",
        ),
    ],
)
def test_capture_metadata_lookup_uses_shared_lifecycle_url(
    provider, capture_call, expected_message, monkeypatch
):
    observed = []

    def fake_get(url, **kwargs):
        observed.append((url, kwargs))
        return {}

    monkeypatch.setattr(capture, "_http_get_json", fake_get)
    with pytest.raises(ValueError, match=expected_message):
        capture_call()
    assert observed[0][0] == telephony.provider_lifecycle_url(
        provider,
        "get",
        base_url="https://unit.test",
        call_id="call /?",
    )


def test_drive_uses_injected_origin_and_shared_redacted_receipt(tmp_path, monkeypatch):
    monkeypatch.setenv("HOTATO_ALLOW_PRIVATE_URLS", "1")
    recorder = fakes.Recorder()
    stereo = fakes.stereo_wav_bytes(tmp_path)
    server, base = fakes.start(fakes.vapi_handler(recorder, stereo))
    try:
        result = drive.place_call_vapi(
            "assistant-private",
            phone_number_id="phone-private",
            customer_number="+15551234567",
            api_key="provider-secret",
            base_url=base,
            poll_interval=0,
            max_wait=5,
            out_path=str(tmp_path / "captured.wav"),
        )
    finally:
        server.shutdown()

    assert read_wav(result["recording"]).num_channels == 2
    assert recorder.requests[0]["path"] == "/call"
    assert any(item["path"] == "/call/vc_1" for item in recorder.requests)
    receipt = result["lifecycle_receipt"]
    assert receipt["provider"] == "vapi"
    assert receipt["provider_response"]["selected"]["status"] == "ended"
    serialized = json.dumps(receipt, sort_keys=True)
    for private in (
        "provider-secret",
        "assistant-private",
        "phone-private",
        "+15551234567",
        "vc_1",
        "/rec/vc_1.stereo.wav",
    ):
        assert private not in serialized
    schema = json.loads(
        (
            Path(__file__).parents[1]
            / "src/hotato/schema/telephony-receipt.v1.json"
        ).read_text(encoding="utf-8")
    )
    jsonschema.Draft7Validator(schema).validate(receipt)


def test_vapi_failed_terminal_state_stops_before_capture(tmp_path, monkeypatch):
    monkeypatch.setenv("HOTATO_ALLOW_PRIVATE_URLS", "1")
    recorder = fakes.Recorder()
    stereo = fakes.stereo_wav_bytes(tmp_path)
    Base = fakes.vapi_handler(recorder, stereo, ends_after=999)

    class Failed(Base):
        def do_GET(self):
            if self.path == "/call/vc_1":
                self._record("GET")
                return self._json({"id": "vc_1", "status": "failed"})
            return super().do_GET()

    server, base = fakes.start(Failed)
    try:
        with pytest.raises(ValueError, match="successful lifecycle completion"):
            drive.place_call_vapi(
                "assistant",
                phone_number_id="phone",
                customer_number="+15550000000",
                api_key="secret",
                base_url=base,
                poll_interval=0,
                max_wait=5,
            )
    finally:
        server.shutdown()
    assert not any("/rec/" in item["path"] for item in recorder.requests)
