"""``hotato.transcribe``'s content-addressed transcript cache
(:class:`~hotato.transcribe.TranscriptCache`) + verify-by-diff
(:func:`~hotato.transcribe.transcribe_cached`), mirroring
``hotato.rubric.VerdictCache`` / ``tests/test_rubric.py`` exactly:

  * a cache hit is byte-identical and SKIPS the model entirely (proven by
    injecting a fake ``transcribe`` that would answer differently on a
    second call);
  * ``no_cache=True`` re-transcribes fresh and DIFFS the fresh Transcript
    against the cached one, surfacing ``drift`` -- never silently
    overwriting, never hiding a mismatch -- and ``drift is None`` when the
    fresh result matches the cached baseline;
  * the DEFAULT cache location gracefully degrades to no caching (with a
    warning, never a crash) when unwritable; an EXPLICIT cache dir stays a
    strict error (a persistence request is never silently discarded).

Uses a deterministic FAKE ``transcribe()`` double (canned Transcripts,
injected via monkeypatch), so this never touches faster-whisper or a real
model -- exactly like ``test_rubric.py``'s ``FakeJudge``.
"""
from __future__ import annotations

import json
import struct
import wave
from importlib import resources

import pytest

from hotato import transcribe as T

# =========================================================================
# helpers
# =========================================================================

def _write_tiny_wav(path, n_samples=1600, sr=16000):
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(sr)
        w.writeframes(struct.pack("<%dh" % n_samples, *([0] * n_samples)))


def _bundled(sid):
    return str(resources.files("hotato").joinpath("data", "audio", sid + ".example.wav"))


def _transcript(text, *, model="base.en", device="cpu", compute_type="int8"):
    return T.Transcript(
        text=text,
        segments=[T.TranscriptSegment(start=0.0, end=1.0, text=text)],
        language="en", model=model, device=device, compute_type=compute_type,
    )


class _FakeTranscribe:
    """Returns canned Transcripts in sequence (repeating the last once
    exhausted), records call count. Never touches faster-whisper -- the
    SHIPPED ``transcribe()`` calls the real faster-whisper model. Mirrors
    ``tests/test_rubric.py``'s ``FakeJudge`` test double."""

    def __init__(self, transcripts):
        self._transcripts = list(transcripts)
        self.calls = 0

    def __call__(self, path, model="base.en", device="auto", *,
                 compute_type=None, word_timestamps=False, vad_filter=False,
                 language=None):
        t = self._transcripts[min(self.calls, len(self._transcripts) - 1)]
        self.calls += 1
        return t


# =========================================================================
# cache: byte-identical hit + skip the model
# =========================================================================

def test_cache_hit_is_byte_identical_and_skips_the_model(tmp_path, monkeypatch):
    wav = tmp_path / "call.wav"
    _write_tiny_wav(wav)
    cache = T.TranscriptCache(str(tmp_path / "c"))

    fake1 = _FakeTranscribe([_transcript("first pass")])
    monkeypatch.setattr(T, "transcribe", fake1)
    r1 = T.transcribe_cached(str(wav), cache=cache)
    assert r1.cached is False and fake1.calls == 1
    assert r1.transcript.text == "first pass"

    # a different backend would answer differently, but the cache hit must
    # NOT call it at all.
    fake2 = _FakeTranscribe([_transcript("second pass, totally different")])
    monkeypatch.setattr(T, "transcribe", fake2)
    r2 = T.transcribe_cached(str(wav), cache=cache)
    assert fake2.calls == 0
    assert r2.cached is True
    assert r2.cache_key == r1.cache_key
    assert r2.drift is None
    # byte-identical replay of the cached (first) transcript
    assert r2.transcript.text == "first pass"
    assert r2.transcript.segments[0].text == "first pass"
    assert T._transcript_to_dict(r2.transcript) == T._transcript_to_dict(r1.transcript)


def test_cache_key_is_content_addressed_by_audio_bytes_and_settings(tmp_path, monkeypatch):
    """Two different audio files (different bytes) never collide in the
    cache; the same file with the same settings always does."""
    wav_a = tmp_path / "a.wav"
    wav_b = tmp_path / "b.wav"
    _write_tiny_wav(wav_a, n_samples=1600)
    _write_tiny_wav(wav_b, n_samples=3200)  # different byte length -> different sha256
    cache = T.TranscriptCache(str(tmp_path / "c"))
    fake = _FakeTranscribe([_transcript("a"), _transcript("b")])
    monkeypatch.setattr(T, "transcribe", fake)

    ra = T.transcribe_cached(str(wav_a), cache=cache)
    rb = T.transcribe_cached(str(wav_b), cache=cache)
    assert ra.cache_key != rb.cache_key
    assert fake.calls == 2  # neither was a cache hit against the other


# =========================================================================
# --no-transcribe-cache: re-query fresh + DIFF, surfacing drift
# =========================================================================

def test_no_cache_requeries_and_surfaces_drift(tmp_path, monkeypatch):
    wav = tmp_path / "call.wav"
    _write_tiny_wav(wav)
    cache = T.TranscriptCache(str(tmp_path / "c"))

    monkeypatch.setattr(T, "transcribe", _FakeTranscribe([_transcript("hello there")]))
    baseline = T.transcribe_cached(str(wav), cache=cache)
    assert baseline.cached is False

    fresh_fake = _FakeTranscribe([_transcript("goodbye now, changed my mind")])
    monkeypatch.setattr(T, "transcribe", fresh_fake)
    result = T.transcribe_cached(str(wav), cache=cache, no_cache=True)

    assert fresh_fake.calls == 1  # no_cache=True ALWAYS re-transcribes
    assert result.cached is False
    assert result.transcript.text == "goodbye now, changed my mind"
    drift = result.drift
    assert drift is not None
    assert drift["changed"] is True
    assert drift["cached_sha256"] != drift["fresh_sha256"]
    assert "hello there" in drift["diff_summary"]
    assert "goodbye now" in drift["diff_summary"]

    # the stored baseline is left untouched -- a plain cache-hitting replay
    # right after --no-transcribe-cache still returns the ORIGINAL cached
    # transcript, so drift stays visible on the next default run rather than
    # being silently overwritten.
    monkeypatch.setattr(T, "transcribe", _FakeTranscribe([_transcript("should not be called")]))
    replay = T.transcribe_cached(str(wav), cache=cache)
    assert replay.cached is True
    assert replay.transcript.text == "hello there"


def test_no_cache_no_drift_when_transcript_matches(tmp_path, monkeypatch):
    wav = tmp_path / "call.wav"
    _write_tiny_wav(wav)
    cache = T.TranscriptCache(str(tmp_path / "c"))

    monkeypatch.setattr(T, "transcribe", _FakeTranscribe([_transcript("same text")]))
    T.transcribe_cached(str(wav), cache=cache)

    monkeypatch.setattr(T, "transcribe", _FakeTranscribe([_transcript("same text")]))
    result = T.transcribe_cached(str(wav), cache=cache, no_cache=True)
    assert result.drift is None  # identical transcript -> no drift


def test_no_cache_without_a_cached_baseline_has_no_drift(tmp_path, monkeypatch):
    """no_cache=True with nothing cached yet is just a fresh transcription --
    there is no baseline to diff against, so drift is honestly None, not a
    fabricated comparison."""
    wav = tmp_path / "call.wav"
    _write_tiny_wav(wav)
    cache = T.TranscriptCache(str(tmp_path / "c"))
    fake = _FakeTranscribe([_transcript("first ever")])
    monkeypatch.setattr(T, "transcribe", fake)
    result = T.transcribe_cached(str(wav), cache=cache, no_cache=True)
    assert fake.calls == 1
    assert result.cached is False
    assert result.drift is None


# =========================================================================
# cache=None: caching off entirely, byte-identical to calling transcribe()
# =========================================================================

def test_no_cache_object_means_no_caching_at_all(tmp_path, monkeypatch):
    wav = tmp_path / "call.wav"
    _write_tiny_wav(wav)
    fake = _FakeTranscribe([_transcript("one"), _transcript("two")])
    monkeypatch.setattr(T, "transcribe", fake)

    r1 = T.transcribe_cached(str(wav), cache=None)
    r2 = T.transcribe_cached(str(wav), cache=None)
    assert fake.calls == 2  # every call is fresh; nothing was ever stored
    assert r1.cached is False and r2.cached is False
    assert r1.drift is None and r2.drift is None
    assert r1.transcript.text == "one"
    assert r2.transcript.text == "two"
    # a cache_key is still computed for free (pure content address), even
    # with no cache backend to store it in.
    assert r1.cache_key == r2.cache_key


# =========================================================================
# graceful degrade: an unwritable DEFAULT cache location never blocks a run;
# an EXPLICIT cache_dir stays a strict persistence request.
# =========================================================================

def test_default_cache_gracefully_degrades_when_home_is_unwritable(tmp_path, monkeypatch):
    blocked_home = tmp_path / "home-is-a-file"
    blocked_home.write_text("no directory can be created below this path")
    monkeypatch.setenv("HOME", str(blocked_home))
    monkeypatch.setenv("USERPROFILE", str(blocked_home))

    cache, warning = T.build_transcript_cache()
    assert cache is None
    assert warning is not None
    assert "transcript cache is unavailable" in warning
    assert "continuing without transcript caching" in warning


def test_explicit_cache_dir_unwritable_stays_strict(tmp_path):
    blocked_parent = tmp_path / "cache-parent-is-a-file"
    blocked_parent.write_text("not a directory")
    with pytest.raises(OSError):
        T.build_transcript_cache(str(blocked_parent / "cache"))


def test_transcribe_cached_works_with_no_cache_backend_when_default_degrades(
    tmp_path, monkeypatch,
):
    """The end-to-end degrade path: the default cache is unavailable, the
    caller (mirroring the CLI/MCP builders) gets (None, warning) and passes
    cache=None through -- transcribe_cached still runs and returns a real
    transcript, it just never persists or replays one."""
    blocked_home = tmp_path / "home-is-a-file"
    blocked_home.write_text("blocked")
    monkeypatch.setenv("HOME", str(blocked_home))
    monkeypatch.setenv("USERPROFILE", str(blocked_home))

    cache, warning = T.build_transcript_cache()
    assert cache is None and warning is not None

    wav = tmp_path / "call.wav"
    _write_tiny_wav(wav)
    monkeypatch.setattr(T, "transcribe", _FakeTranscribe([_transcript("degraded but working")]))
    result = T.transcribe_cached(str(wav), cache=cache)
    assert result.transcript.text == "degraded but working"
    assert result.cached is False


# =========================================================================
# CLI end-to-end: --transcribe-cache-dir / --no-transcribe-cache /
# --no-transcribe-store, and the default-cache graceful degrade at the CLI.
# =========================================================================

def test_cli_run_transcribe_cache_hit_skips_the_model(tmp_path, monkeypatch, capsys):
    from hotato import cli

    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("USERPROFILE", str(tmp_path))
    wav = _bundled("01-hard-interruption")
    cache_dir = str(tmp_path / "tcache")

    fake1 = _FakeTranscribe([_transcript("first cli pass")])
    monkeypatch.setattr(T, "transcribe", fake1)
    cli.main([
        "run", "--stereo", wav, "--transcribe",
        "--transcribe-cache-dir", cache_dir, "--format", "json",
    ])
    env1 = json.loads(capsys.readouterr().out)
    assert env1["transcript"]["cache"]["cached"] is False
    assert fake1.calls == 1

    fake2 = _FakeTranscribe([_transcript("would answer differently")])
    monkeypatch.setattr(T, "transcribe", fake2)
    cli.main([
        "run", "--stereo", wav, "--transcribe",
        "--transcribe-cache-dir", cache_dir, "--format", "json",
    ])
    env2 = json.loads(capsys.readouterr().out)
    assert fake2.calls == 0  # cache hit -- the model was never called again
    assert env2["transcript"]["cache"]["cached"] is True
    assert env2["transcript"]["text"] == "first cli pass"


def test_cli_run_no_transcribe_cache_surfaces_drift(tmp_path, monkeypatch, capsys):
    from hotato import cli

    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("USERPROFILE", str(tmp_path))
    wav = _bundled("01-hard-interruption")
    cache_dir = str(tmp_path / "tcache")

    monkeypatch.setattr(T, "transcribe", _FakeTranscribe([_transcript("baseline text")]))
    cli.main([
        "run", "--stereo", wav, "--transcribe",
        "--transcribe-cache-dir", cache_dir, "--format", "json",
    ])
    capsys.readouterr()

    monkeypatch.setattr(T, "transcribe", _FakeTranscribe([_transcript("drifted text")]))
    cli.main([
        "run", "--stereo", wav, "--transcribe", "--no-transcribe-cache",
        "--transcribe-cache-dir", cache_dir, "--format", "json",
    ])
    env = json.loads(capsys.readouterr().out)
    drift = env["transcript"]["cache"]["drift"]
    assert drift is not None and drift["changed"] is True
    assert env["transcript"]["text"] == "drifted text"


def test_cli_run_no_transcribe_store_never_persists(tmp_path, monkeypatch, capsys):
    from hotato import cli

    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("USERPROFILE", str(tmp_path))
    wav = _bundled("01-hard-interruption")
    cache_dir = str(tmp_path / "tcache")

    fake1 = _FakeTranscribe([_transcript("never stored")])
    monkeypatch.setattr(T, "transcribe", fake1)
    cli.main([
        "run", "--stereo", wav, "--transcribe", "--no-transcribe-store",
        "--transcribe-cache-dir", cache_dir, "--format", "json",
    ])
    capsys.readouterr()

    fake2 = _FakeTranscribe([_transcript("re-transcribed, --no-transcribe-store means no cache read either")])
    monkeypatch.setattr(T, "transcribe", fake2)
    cli.main([
        "run", "--stereo", wav, "--transcribe", "--no-transcribe-store",
        "--transcribe-cache-dir", cache_dir, "--format", "json",
    ])
    env = json.loads(capsys.readouterr().out)
    assert fake2.calls == 1  # never a cache hit -- --no-transcribe-store bypasses the cache entirely
    assert env["transcript"]["cache"]["cached"] is False


def test_cli_run_transcribe_default_cache_degrades_gracefully(tmp_path, monkeypatch, capsys):
    """Mirrors test_conversation_verify_cli.py's rubric-cache degrade test:
    an uncreatable default HOME must not block a --transcribe run; the
    result is still real (a cache_key is still computed), just unpersisted."""
    from hotato import cli

    blocked_home = tmp_path / "home-is-a-file"
    blocked_home.write_text("no directory can be created below this path")
    monkeypatch.setenv("HOME", str(blocked_home))
    monkeypatch.setenv("USERPROFILE", str(blocked_home))

    wav = _bundled("01-hard-interruption")
    monkeypatch.setattr(T, "transcribe", _FakeTranscribe([_transcript("still works")]))
    cli.main(["run", "--stereo", wav, "--transcribe", "--format", "json"])
    captured = capsys.readouterr()
    assert "transcript cache is unavailable" in captured.err
    env = json.loads(captured.out)
    assert env["transcript"]["text"] == "still works"
    assert env["transcript"]["cache"]["cached"] is False


def test_cli_run_explicit_unwritable_transcribe_cache_dir_stays_strict(tmp_path, monkeypatch):
    """An EXPLICIT --transcribe-cache-dir is a persistence request: it must
    fail loudly (exit 2) rather than silently discard the replay/drift
    baseline, mirroring the rubric cache's own explicit-cache-dir contract."""
    from hotato import cli

    monkeypatch.setenv("HOME", str(tmp_path / "otherwise-writable-home"))
    monkeypatch.setenv("USERPROFILE", str(tmp_path / "otherwise-writable-home"))
    blocked_cache_parent = tmp_path / "cache-parent-is-a-file"
    blocked_cache_parent.write_text("not a directory")

    wav = _bundled("01-hard-interruption")
    monkeypatch.setattr(T, "transcribe", _FakeTranscribe([_transcript("unused")]))
    code = cli.main([
        "run", "--stereo", wav, "--transcribe",
        "--transcribe-cache-dir", str(blocked_cache_parent / "cache"),
        "--format", "json",
    ])
    assert code == 2
