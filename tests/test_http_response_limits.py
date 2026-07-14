"""Remote response bodies have explicit, path-appropriate byte ceilings.

These are transport-boundary tests.  A declared oversize body is refused
without a read; a chunked/no-length body is sampled only to one byte beyond
the ceiling.  The integration cases pin each non-fleet outbound reader while
keeping the established caller-facing exception contract.
"""

from __future__ import annotations

import urllib.error
import urllib.request

import pytest

from hotato import apply, capture, errors, inspectcfg, notify, rubric, state_adapter


class _Response:
    def __init__(self, body=b"", *, headers=None, fail_if_read=False):
        self.body = body
        self.headers = dict(headers or {})
        self.fail_if_read = fail_if_read
        self.read_sizes = []

    def read(self, size=-1):
        if self.fail_if_read:
            raise AssertionError("declared oversize response body must not be read")
        self.read_sizes.append(size)
        return self.body if size is None or size < 0 else self.body[:size]

    def __enter__(self):
        return self

    def __exit__(self, *exc_info):
        return False

    def close(self):
        return None


def _install_response(monkeypatch, response):
    monkeypatch.setattr(urllib.request, "urlopen", lambda req, timeout=None: response)


def test_bounded_reader_refuses_declared_oversize_before_reading():
    response = _Response(
        headers={"Content-Length": "9"},
        fail_if_read=True,
    )
    with pytest.raises(errors.HttpResponseTooLarge) as exc_info:
        errors.read_bounded_http_body(response, max_bytes=8, subject="test response")

    assert response.read_sizes == []
    assert str(exc_info.value) == (
        "test response exceeds the 8-byte response limit; refusing the body "
        "before reading it"
    )
    # The existing structured-error contract remains unchanged: this is the
    # ordinary ValueError/usage-error lane, not a new schema slug.
    assert errors.classify(exc_info.value)[0] == "usage_error"


def test_bounded_reader_refuses_chunked_body_at_one_byte_over_limit():
    response = _Response(
        b"123456789",
        headers={"Transfer-Encoding": "chunked"},
    )
    with pytest.raises(errors.HttpResponseTooLarge, match="8-byte response limit"):
        errors.read_bounded_http_body(response, max_bytes=8)

    assert response.read_sizes == [9]


def test_bounded_reader_accepts_body_exactly_at_limit():
    response = _Response(b"12345678")
    assert errors.read_bounded_http_body(response, max_bytes=8) == b"12345678"
    assert response.read_sizes == [9]


def test_capture_get_declared_limit_sanitizes_url(monkeypatch):
    response = _Response(headers={"Content-Length": "9"}, fail_if_read=True)
    _install_response(monkeypatch, response)

    with pytest.raises(errors.HttpResponseTooLarge) as exc_info:
        capture._http_get(
            "https://api.example.test/calls?token=super-secret",
            max_bytes=8,
        )

    message = str(exc_info.value)
    assert "api.example.test/calls?redacted" in message
    assert "super-secret" not in message
    assert response.read_sizes == []


def test_capture_post_chunked_limit(monkeypatch):
    response = _Response(b"123456789", headers={"Transfer-Encoding": "chunked"})
    _install_response(monkeypatch, response)

    with pytest.raises(errors.HttpResponseTooLarge, match="8-byte response limit"):
        capture._http_post(
            "https://api.example.test/calls",
            b"{}",
            max_bytes=8,
        )
    assert response.read_sizes == [9]


def test_capture_download_uses_separate_recording_limit(monkeypatch, tmp_path):
    response = _Response(b"123456789")
    _install_response(monkeypatch, response)
    monkeypatch.setattr(capture, "_HTTP_RECORDING_RESPONSE_MAX_BYTES", 8)
    monkeypatch.setattr(capture, "_validate_download_url", lambda url: url)
    destination = tmp_path / "recording.wav"

    with pytest.raises(errors.HttpResponseTooLarge, match="8-byte response limit"):
        capture._download(
            "https://media.example.test/recording.wav?signature=super-secret",
            str(destination),
        )

    assert response.read_sizes == [9]
    assert not destination.exists()


def test_capture_http_error_detail_read_is_bounded(monkeypatch):
    error_body = _Response(b"x" * (capture._HTTP_ERROR_DETAIL_MAX_BYTES + 1))
    error = urllib.error.HTTPError(
        "https://api.example.test/calls?token=super-secret",
        500,
        "failure",
        {},
        error_body,
    )

    def raise_error(req, timeout=None):
        raise error

    monkeypatch.setattr(urllib.request, "urlopen", raise_error)
    with pytest.raises(capture._HTTPStatusError) as exc_info:
        capture._http_get("https://api.example.test/calls?token=super-secret")

    assert error_body.read_sizes == [capture._HTTP_ERROR_DETAIL_MAX_BYTES + 1]
    assert "super-secret" not in str(exc_info.value)


def test_apply_declared_limit_keeps_value_error_contract(monkeypatch):
    response = _Response(headers={"Content-Length": "9"}, fail_if_read=True)
    _install_response(monkeypatch, response)
    monkeypatch.setattr(apply, "_HTTP_JSON_RESPONSE_MAX_BYTES", 8)

    with pytest.raises(errors.HttpResponseTooLarge) as exc_info:
        apply._http_json(
            "GET",
            "https://api.example.test/assistant/a?token=super-secret",
            headers={},
            body=None,
            timeout=1,
        )

    assert "super-secret" not in str(exc_info.value)
    assert response.read_sizes == []


def test_inspect_chunked_limit_keeps_value_error_contract(monkeypatch):
    response = _Response(b"123456789", headers={"Transfer-Encoding": "chunked"})
    _install_response(monkeypatch, response)
    monkeypatch.setattr(inspectcfg, "_HTTP_JSON_RESPONSE_MAX_BYTES", 8)

    with pytest.raises(errors.HttpResponseTooLarge) as exc_info:
        inspectcfg._http_get_json(
            "https://api.example.test/assistant/a?token=super-secret"
        )

    assert "super-secret" not in str(exc_info.value)
    assert response.read_sizes == [9]


def test_rubric_declared_limit_translates_to_judge_error(monkeypatch):
    response = _Response(headers={"Content-Length": "9"}, fail_if_read=True)
    _install_response(monkeypatch, response)
    monkeypatch.setattr(rubric, "_HTTP_MODEL_RESPONSE_MAX_BYTES", 8)

    with pytest.raises(rubric.JudgeError) as exc_info:
        rubric._urllib_json_call(
            "https://judge.example.test/chat?token=super-secret",
            data=b"{}",
            headers={},
            method="POST",
            timeout=1,
            unreachable_subject="judge endpoint",
            failed_subject="judge",
        )

    message = str(exc_info.value)
    assert "8-byte response limit" in message
    assert "super-secret" not in message
    assert response.read_sizes == []


def test_state_adapter_chunked_limit_is_structured_bad_response(monkeypatch):
    response = _Response(b"123456789", headers={"Transfer-Encoding": "chunked"})
    _install_response(monkeypatch, response)
    monkeypatch.setattr(state_adapter, "_HTTP_STATE_RESPONSE_MAX_BYTES", 8)
    adapter = state_adapter.HttpStateAdapter(
        base_url="https://state.example.test",
        resources={"orders": {"path_template": "/orders"}},
    )

    with pytest.raises(state_adapter.StateAdapterError) as exc_info:
        adapter.query("orders", token="super-secret")

    assert adapter.last_error is not None
    assert adapter.last_error["kind"] == "bad_response"
    assert adapter.last_error["url"] == "https://state.example.test/orders?redacted"
    assert "8-byte response limit" in str(exc_info.value)
    assert "super-secret" not in str(adapter.last_error)
    assert response.read_sizes == [9]


def test_notify_declared_limit_fails_open_and_sanitizes_warning(monkeypatch, capsys):
    response = _Response(headers={"Content-Length": "9"}, fail_if_read=True)
    _install_response(monkeypatch, response)
    monkeypatch.setattr(notify, "_HTTP_NOTIFY_RESPONSE_MAX_BYTES", 8)

    ok = notify.post_notification(
        "https://hooks.example.test/x?token=super-secret",
        {"ok": True},
    )

    assert ok is False
    warning = capsys.readouterr().err
    assert "8-byte response limit" in warning
    assert "hooks.example.test/x?redacted" in warning
    assert "super-secret" not in warning
    assert response.read_sizes == []
