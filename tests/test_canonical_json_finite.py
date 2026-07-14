"""Canonical digest boundaries accept RFC 8259 numbers only."""

import pytest

from hotato import attest, manifest


@pytest.mark.parametrize("value", [float("nan"), float("inf"), float("-inf")])
@pytest.mark.parametrize("encoder", [manifest.canonical_json, attest.canonical_json])
def test_canonical_json_refuses_non_finite_numbers(encoder, value):
    with pytest.raises(ValueError):
        encoder({"value": value})


@pytest.mark.parametrize("encoder", [manifest.canonical_json, attest.canonical_json])
def test_canonical_json_finite_bytes_remain_stable(encoder):
    assert encoder({"z": 1.25, "a": [0, -2.5]}) == '{"a":[0,-2.5],"z":1.25}'
