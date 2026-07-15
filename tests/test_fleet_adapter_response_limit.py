"""The live fleet delete path never performs an unbounded remote read."""
from __future__ import annotations

import urllib.request

import pytest

from hotato import apply as apply_mod
from hotato import errors
from hotato.fleet import adapters


class _Response:
    def __init__(self, body=b"", *, headers=None, fail_if_read=False):
        self.body = body
        self.headers = dict(headers or {})
        self.fail_if_read = fail_if_read
        self.read_sizes = []

    def read(self, size=-1):
        if self.fail_if_read:
            raise AssertionError("declared oversize response must not be read")
        self.read_sizes.append(size)
        return self.body if size is None or size < 0 else self.body[:size]

    def __enter__(self):
        return self

    def __exit__(self, *exc_info):
        return False


def _adapter_with_staging_clone(monkeypatch):
    monkeypatch.setattr(
        apply_mod,
        "_http_json",
        lambda *args, **kwargs: {"id": "clone-1", "name": "hotato-staging-test"},
    )
    return adapters.get_adapter("vapi", api_key="test-key")


# The durable clone receipt naming clone-1 -- the PRIMARY authorization
# delete_clone now requires before it issues any read or DELETE.
_RECEIPT = {"receipt_id": "clonercpt-t1", "clone_id": "clone-1", "provider": "vapi",
            "nonce": "n1", "trial_id": "t1"}


def test_delete_clone_refuses_declared_oversize_before_reading(monkeypatch):
    response = _Response(
        headers={"Content-Length": "9"},
        fail_if_read=True,
    )
    monkeypatch.setattr(adapters, "_HTTP_DELETE_RESPONSE_MAX_BYTES", 8)
    monkeypatch.setattr(
        urllib.request, "urlopen", lambda request, timeout=None: response
    )

    with pytest.raises(errors.HttpResponseTooLarge, match="8-byte response limit"):
        _adapter_with_staging_clone(monkeypatch).delete_clone("clone-1", receipt=_RECEIPT)

    assert response.read_sizes == []


def test_delete_clone_refuses_undeclared_stream_at_one_byte_over_limit(monkeypatch):
    response = _Response(
        b"123456789",
        headers={"Transfer-Encoding": "chunked"},
    )
    monkeypatch.setattr(adapters, "_HTTP_DELETE_RESPONSE_MAX_BYTES", 8)
    monkeypatch.setattr(
        urllib.request, "urlopen", lambda request, timeout=None: response
    )

    with pytest.raises(errors.HttpResponseTooLarge, match="8-byte response limit"):
        _adapter_with_staging_clone(monkeypatch).delete_clone("clone-1", receipt=_RECEIPT)

    assert response.read_sizes == [9]
