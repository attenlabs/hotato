"""Round-5 hardening regression: confirmed defect #17.

Python's ``urlparse`` lowercases an IPv6 host literal but does NOT canonicalize
it, so ``http://[::1]/x`` and ``http://[0:0:0:0:0:0:0:1]/x`` parse to the
textually-different hostnames ``::1`` and ``0:0:0:0:0:0:0:1`` even though they
are the same address. Both ``capture._validate_download_url`` and
``ingest._validate_recording_url`` compared the incoming hostname against
``HOTATO_INGEST_ALLOWED_HOSTS`` with a raw ``.lower()``, so an operator who
allow-listed one textual form of an IPv6 host would see a host that IS on
their list spuriously refused as "not in HOTATO_INGEST_ALLOWED_HOSTS" when the
vendor (or the operator's own URL) happened to write the address in a
different, equally-valid form (zero-padded, expanded, or shorthand).

This is a fail-closed usability/robustness bug, not an SSRF bypass: the
independent ``_reject_private_host`` guard resolves and checks every hostname
regardless of the allowlist outcome, so the mismatch could only ever make the
allowlist check deny a host the operator meant to allow -- never admit one it
should not. These tests pin the fixed contract: equivalent IPv6 forms now
compare equal for the allowlist, normal denial and non-IP (DNS-name) matching
are unchanged, and the SSRF guard still fires exactly as before.
"""

import pytest

from hotato import capture as cap
from hotato import ingest as ing


# --- unit-level: the canonicalization helper itself ------------------------

@pytest.mark.parametrize("a, b", [
    ("::1", "0:0:0:0:0:0:0:1"),
    ("::1", "0000:0000:0000:0000:0000:0000:0000:0001"),
    ("[::1]", "::1"),
    ("2001:DB8::1", "2001:db8:0:0:0:0:0:1"),
])
def test_canonical_host_normalizes_equivalent_ipv6_forms(a, b):
    assert cap._canonical_host(a) == cap._canonical_host(b)


def test_canonical_host_leaves_dns_names_lowercased_unchanged():
    # Non-IP hostnames keep the pre-fix behavior: a plain lowercase compare.
    assert cap._canonical_host("Storage.Example.com") == "storage.example.com"


def test_canonical_host_distinguishes_different_addresses():
    assert cap._canonical_host("::1") != cap._canonical_host("::2")
    assert cap._canonical_host("10.0.0.1") != cap._canonical_host("10.0.0.2")


# --- capture._validate_download_url: allowlist comparison ------------------

def test_capture_allowlist_matches_equivalent_ipv6_forms(monkeypatch):
    """Reproduces the original defect: before the fix, an allowlist entry
    written as the expanded IPv6 form did not match a download URL using the
    shorthand form (or vice versa), even though they name the same host.
    HOTATO_ALLOW_PRIVATE_URLS=1 isolates the allowlist comparison from the
    separate (and unaffected) SSRF guard, which independently blocks loopback
    regardless of the allowlist outcome."""
    monkeypatch.setenv("HOTATO_ALLOW_PRIVATE_URLS", "1")
    monkeypatch.setenv("HOTATO_INGEST_ALLOWED_HOSTS", "0:0:0:0:0:0:0:1")
    assert cap._validate_download_url("http://[::1]/rec.wav") == "http://[::1]/rec.wav"


def test_capture_allowlist_matches_equivalent_ipv6_forms_reverse(monkeypatch):
    monkeypatch.setenv("HOTATO_ALLOW_PRIVATE_URLS", "1")
    monkeypatch.setenv("HOTATO_INGEST_ALLOWED_HOSTS", "::1")
    assert cap._validate_download_url("http://[0:0:0:0:0:0:0:1]/rec.wav") == \
        "http://[0:0:0:0:0:0:0:1]/rec.wav"


def test_capture_allowlist_still_rejects_a_genuinely_different_host(monkeypatch):
    monkeypatch.setenv("HOTATO_ALLOW_PRIVATE_URLS", "1")
    monkeypatch.setenv("HOTATO_INGEST_ALLOWED_HOSTS", "::2")
    with pytest.raises(ValueError, match="HOTATO_INGEST_ALLOWED_HOSTS"):
        cap._validate_download_url("http://[::1]/rec.wav")


def test_capture_allowlist_dns_name_matching_unchanged(monkeypatch):
    monkeypatch.delenv("HOTATO_ALLOW_PRIVATE_URLS", raising=False)
    monkeypatch.setenv("HOTATO_INGEST_ALLOWED_HOSTS", "Storage.Example.com")
    assert cap._validate_download_url("https://storage.example.com/rec.wav") == \
        "https://storage.example.com/rec.wav"


def test_capture_ssrf_guard_still_fires_despite_matching_allowlist(monkeypatch):
    """The allowlist fix must not weaken the independent SSRF guard: a
    loopback host that IS on the allowlist (in either IPv6 form) is still
    refused unless HOTATO_ALLOW_PRIVATE_URLS=1 is also set."""
    monkeypatch.delenv("HOTATO_ALLOW_PRIVATE_URLS", raising=False)
    monkeypatch.setenv("HOTATO_INGEST_ALLOWED_HOSTS", "0:0:0:0:0:0:0:1")
    with pytest.raises(ValueError, match="non-public|SSRF|private|metadata"):
        cap._validate_download_url("http://[::1]/rec.wav")


# --- ingest._validate_recording_url: same fix, reused via _capture ---------

def test_ingest_allowlist_matches_equivalent_ipv6_forms(monkeypatch):
    monkeypatch.setenv("HOTATO_ALLOW_PRIVATE_URLS", "1")
    monkeypatch.setenv("HOTATO_INGEST_ALLOWED_HOSTS", "0:0:0:0:0:0:0:1")
    assert ing._validate_recording_url("http://[::1]/rec.wav", "pipecat") == \
        "http://[::1]/rec.wav"


def test_ingest_allowlist_still_rejects_a_genuinely_different_host(monkeypatch):
    monkeypatch.setenv("HOTATO_ALLOW_PRIVATE_URLS", "1")
    monkeypatch.setenv("HOTATO_INGEST_ALLOWED_HOSTS", "::2")
    with pytest.raises(ing.IngestError, match="HOTATO_INGEST_ALLOWED_HOSTS"):
        ing._validate_recording_url("http://[::1]/rec.wav", "pipecat")
