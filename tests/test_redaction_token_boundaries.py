"""Redaction-derivation and query-secret stripping boundaries.

Split out of the round-6 security sweep so it never collides with the CAS /
workspace-authz coverage: this file pins only (1) audio redaction as a safe
DERIVED artifact (no source aliasing, no invalid/no-op spans, no truncated PCM,
atomic publish) and (2) that the token query-stripper decodes keys the same way
authentication does, so a percent-encoded ``token`` spelling is still stripped.
"""
from __future__ import annotations

import os
import wave

import pytest

from hotato.fleet import privacy
from hotato.serve.app import _strip_token_qs
from tests import _trial_audio as ta


def test_redaction_refuses_source_and_filesystem_aliases_without_mutation(tmp_path):
    src = tmp_path / "source.wav"
    ta.yielding_call(src)
    original = src.read_bytes()

    with pytest.raises(ValueError, match="new file"):
        privacy.redact_audio(str(src), [(2.0, 3.0)], str(src))
    assert src.read_bytes() == original

    hard = tmp_path / "hard.wav"
    try:
        os.link(src, hard)
    except OSError:
        pytest.skip("hardlinks unavailable")
    with pytest.raises(ValueError, match="hardlink"):
        privacy.redact_audio(str(src), [(2.0, 3.0)], str(hard))
    assert src.read_bytes() == original

    link = tmp_path / "link.wav"
    try:
        link.symlink_to(src)
    except (OSError, NotImplementedError):
        pytest.skip("symlinks unavailable")
    with pytest.raises(ValueError, match="symlink"):
        privacy.redact_audio(str(src), [(2.0, 3.0)], str(link))
    assert src.read_bytes() == original


@pytest.mark.parametrize(
    "spans",
    [
        [],
        [(float("nan"), 1.0)],
        [(0.0, float("inf"))],
        [(2.0, 2.0)],
        [(3.0, 2.0)],
        [(-0.1, 1.0)],
        [(0.0, 6.1)],
        [(2.0, 3.0), (1.0, 1.5)],
        [(1.0, 2.0), (1.5, 2.5)],
        [(True, 1.0)],
        [(0.0,)],
    ],
)
def test_redaction_rejects_invalid_spans_and_preserves_destination(tmp_path, spans):
    src = tmp_path / "source.wav"
    out = tmp_path / "existing.wav"
    ta.yielding_call(src)
    out.write_bytes(b"existing destination sentinel")
    with pytest.raises(ValueError):
        privacy.redact_audio(str(src), spans, str(out))
    assert out.read_bytes() == b"existing destination sentinel"


def test_redaction_refuses_noop_silence_and_writes_valid_output_atomically(
    tmp_path, monkeypatch
):
    src = tmp_path / "source.wav"
    out = tmp_path / "out.wav"
    ta.yielding_call(src)
    out.write_bytes(b"old")

    # The opening 100 ms are silent in this fixture.  A derived-redaction claim
    # with unchanged PCM identity is refused and the old destination survives.
    with pytest.raises(ValueError, match="already-silent"):
        privacy.redact_audio(str(src), [(0.0, 0.1)], str(out))
    assert out.read_bytes() == b"old"

    real_replace = privacy.os.replace

    def fail_replace(_src, _dst):
        raise OSError("simulated publish interruption")

    monkeypatch.setattr(privacy.os, "replace", fail_replace)
    with pytest.raises(OSError, match="publish interruption"):
        privacy.redact_audio(str(src), [(2.0, 3.0)], str(out))
    assert out.read_bytes() == b"old"
    assert list(tmp_path.glob(".hotato-redact-*.wav")) == []

    monkeypatch.setattr(privacy.os, "replace", real_replace)
    result = privacy.redact_audio(str(src), [(2.0, 3.0)], str(out))
    assert result["parent_pcm_sha256"] == privacy._pcm_sha256(str(src))
    assert result["pcm_sha256"] == privacy._pcm_sha256(str(out))
    assert result["pcm_sha256"] != result["parent_pcm_sha256"]


def test_redaction_fractional_adjacent_spans_cover_every_touched_44100hz_frame(
    tmp_path,
):
    src = tmp_path / "fractional-44100.wav"
    out = tmp_path / "redacted.wav"
    rate = 44_100
    frame = b"\x01\x00\x02\x00"  # non-silent 16-bit stereo frame
    with wave.open(str(src), "wb") as wf:
        wf.setnchannels(2)
        wf.setsampwidth(2)
        wf.setframerate(rate)
        wf.writeframes(frame * 8)

    shared_boundary = 2.25 / rate
    spans = [(0.5 / rate, shared_boundary), (shared_boundary, 3.5 / rate)]
    result = privacy.redact_audio(str(src), spans, str(out))

    assert result["redacted_spans_sec"] == [list(span) for span in spans]
    assert result["requested_spans_sec"] == [list(span) for span in spans]
    # Adjacent fractional requests overlap one realized frame by design: every
    # frame touched by either interval is silenced rather than leaking an edge.
    assert result["realized_frame_ranges"] == [[0, 3], [2, 4]]
    assert result["realized_interleaved_sample_ranges"] == [[0, 6], [4, 8]]
    with wave.open(str(out), "rb") as wf:
        assert wf.getframerate() == rate
        pcm = wf.readframes(wf.getnframes())
    assert pcm == (b"\x00" * (4 * len(frame))) + (frame * 4)


def test_redaction_rejects_truncated_pcm_before_touching_destination(tmp_path):
    src = tmp_path / "truncated.wav"
    out = tmp_path / "existing.wav"
    frame = b"\x01\x00\x02\x00"
    with wave.open(str(src), "wb") as wf:
        wf.setnchannels(2)
        wf.setsampwidth(2)
        wf.setframerate(44_100)
        wf.writeframes(frame * 10)
    src.write_bytes(src.read_bytes()[:-2])
    out.write_bytes(b"existing destination sentinel")

    with pytest.raises(ValueError, match="truncated PCM data"):
        privacy.redact_audio(str(src), [(0.0, 1.0 / 44_100)], str(out))
    assert out.read_bytes() == b"existing destination sentinel"


@pytest.mark.parametrize("encoded_key", ["token", "%74oken", "t%6fken", "to%6ben"])
def test_token_query_stripping_uses_the_same_decoding_as_auth(encoded_key):
    cleaned = _strip_token_qs(f"x=1&{encoded_key}=SECRET&keep=a%20b")
    assert "SECRET" not in cleaned
    assert "token" not in cleaned
    assert cleaned == "x=1&keep=a+b"
