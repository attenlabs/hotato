"""Credential-origin and secret-bearing URL regression tests.

The authenticated capture primitives may retain credentials only across a
redirect with the same scheme, canonical host, and effective port.  Every URL
that reaches a capture error or notify warning must remove userinfo and redact
query/fragment data before it is displayed.
"""

from __future__ import annotations

import io
import urllib.error
import urllib.request

import pytest

from hotato import capture, errors, notify

_SECRET_URL = (
    "https://alice:hunter2@hooks.example.test/deliver"
    "?token=notify-token-123&X-Amz-Signature=signed-value-456"
    "#private-fragment-789"
)
_SECRETS = (
    "alice",
    "hunter2",
    "notify-token-123",
    "signed-value-456",
    "private-fragment-789",
    "X-Amz-Signature",
)


@pytest.fixture(autouse=True)
def _avoid_dns_in_redirect_unit_tests(monkeypatch):
    # These tests call the redirect handler directly.  The target-origin SSRF
    # behavior has separate coverage; suppress DNS so this suite stays offline.
    monkeypatch.setenv("HOTATO_ALLOW_PRIVATE_URLS", "1")


def _assert_secrets_absent(text: str) -> None:
    for secret in _SECRETS:
        assert secret not in text


@pytest.mark.parametrize(
    "left,right,expected",
    [
        ("https://EXAMPLE.test/a", "https://example.test:443/b", True),
        ("http://example.test/a", "http://example.test:80/b", True),
        ("https://example.test:443/a", "https://example.test:444/b", False),
        ("https://example.test/a", "http://example.test/b", False),
        ("http://[0:0:0:0:0:0:0:1]/a", "http://[::1]:80/b", True),
    ],
)
def test_normalized_origin_includes_scheme_canonical_host_and_effective_port(
    left, right, expected
):
    assert capture._same_origin(left, right) is expected


@pytest.mark.parametrize(
    "target",
    [
        "https://api.example.test:444/final",  # same host, different port
        "http://api.example.test/final",       # HTTPS -> HTTP downgrade
    ],
)
def test_redirect_strips_credentials_when_normalized_origin_changes(target):
    req = urllib.request.Request(
        "https://api.example.test/start",
        headers={
            "Authorization": "Bearer capture-secret",
            "Proxy-Authorization": "Basic proxy-secret",
            "Cookie": "session=cookie-secret",
        },
    )
    redirected = capture._CredentialSafeRedirectHandler().redirect_request(
        req, None, 302, "Found", {}, target
    )
    assert redirected is not None
    lowered = {k.lower(): v for k, v in redirected.header_items()}
    assert "authorization" not in lowered
    assert "proxy-authorization" not in lowered
    assert "cookie" not in lowered


def test_redirect_retains_credentials_for_same_origin_with_explicit_default_port():
    req = urllib.request.Request(
        "https://API.EXAMPLE.test/start",
        headers={"Authorization": "Bearer keep-on-origin"},
    )
    redirected = capture._CredentialSafeRedirectHandler().redirect_request(
        req, None, 302, "Found", {}, "https://api.example.test:443/final"
    )
    assert redirected.get_header("Authorization") == "Bearer keep-on-origin"


def test_vendor_recording_auth_uses_the_same_origin_rule():
    headers = {"Authorization": "Bearer vendor-secret"}
    base = "https://api.example.test"
    assert capture._auth_headers_for(
        "https://api.example.test:443/recording", base, headers
    ) is headers
    assert capture._auth_headers_for(
        "https://api.example.test:444/recording", base, headers
    ) is None
    assert capture._auth_headers_for(
        "http://api.example.test/recording", base, headers
    ) is None


def test_shared_url_sanitizer_removes_userinfo_query_and_fragment():
    safe = errors.sanitize_url(_SECRET_URL)
    assert safe == "https://hooks.example.test/deliver?redacted"
    _assert_secrets_absent(safe)


def test_capture_network_error_redacts_request_and_exception_urls(monkeypatch):
    def fail(req, timeout=None):
        raise urllib.error.URLError(f"upstream repeated {_SECRET_URL}")

    monkeypatch.setattr(urllib.request, "urlopen", fail)
    with pytest.raises(ValueError) as exc_info:
        capture._http_get(_SECRET_URL)
    message = str(exc_info.value)
    assert "https://hooks.example.test/deliver?redacted" in message
    _assert_secrets_absent(message)


def test_capture_http_error_redacts_url_repeated_by_response_body(monkeypatch):
    def fail(req, timeout=None):
        body = io.BytesIO(f"request denied for {_SECRET_URL}".encode())
        raise urllib.error.HTTPError(req.full_url, 403, "Forbidden", {}, body)

    monkeypatch.setattr(urllib.request, "urlopen", fail)
    with pytest.raises(ValueError) as exc_info:
        capture._http_get(_SECRET_URL)
    message = str(exc_info.value)
    assert "HTTP 403" in message
    _assert_secrets_absent(message)


def test_capture_non_json_preview_redacts_embedded_presigned_url(monkeypatch):
    class Response(io.BytesIO):
        def __enter__(self):
            return self

        def __exit__(self, *args):
            self.close()

    def respond(req, timeout=None):
        return Response(f"proxy says retry at {_SECRET_URL}".encode())

    monkeypatch.setattr(urllib.request, "urlopen", respond)
    with pytest.raises(ValueError) as exc_info:
        capture._http_get_json(_SECRET_URL)
    message = str(exc_info.value)
    assert "non-JSON body" in message
    _assert_secrets_absent(message)


def test_pull_skip_reason_sanitizes_adapter_exception_url(tmp_path, monkeypatch):
    def fail(*args, **kwargs):
        raise ValueError(f"download failed at {_SECRET_URL}")

    monkeypatch.setattr(capture, "fetch_one", fail)
    result = capture.pull(
        "vapi", {"api_key": "unused"}, out_dir=str(tmp_path), ids=["call-1"]
    )
    reason = result["skipped"][0]["reason"]
    assert "https://hooks.example.test/deliver?redacted" in reason
    _assert_secrets_absent(reason)


def test_notify_failure_redacts_target_and_exception_urls(monkeypatch, capsys):
    def fail(req, timeout=None):
        raise urllib.error.URLError(f"delivery refused for {_SECRET_URL}")

    monkeypatch.setattr(urllib.request, "urlopen", fail)
    assert notify.post_notification(_SECRET_URL, {"kind": "test"}) is False
    warning = capsys.readouterr().err
    assert warning.count("https://hooks.example.test/deliver?redacted") == 2
    _assert_secrets_absent(warning)


def test_notify_validation_error_does_not_echo_ftp_userinfo_or_query():
    unsafe = (
        "ftp://alice:hunter2@hooks.example.test/deliver"
        "?token=notify-token-123&X-Amz-Signature=signed-value-456"
    )
    with pytest.raises(ValueError) as exc_info:
        notify.validate_notify_url(unsafe)
    message = str(exc_info.value)
    assert "ftp://hooks.example.test/deliver?redacted" in message
    _assert_secrets_absent(message)
