"""The optional, NON-REFERENCE transcript CONTEXT layer (faster-whisper).

Proves the honesty invariants that make this seam shippable, mirroring
tests/test_backend.py (neural) and tests/test_diarize.py (diarize):

  1. LAZY + STRICTLY OPT-IN -- importing hotato.transcribe never imports
     faster_whisper; that only happens inside transcribe(), on first real use.
  2. CLEAN ERROR, NO FALLBACK -- with the [transcribe] extra absent,
     transcribe() raises a clean BackendUnavailable naming
     `pip install hotato[transcribe]`, never silently skipping or scoring
     anything else instead.
  3. FIFO-SAFE -- transcribe() guards the input path with
     errors.require_regular_file BEFORE any read/model call, so a FIFO raises
     immediately (ValueError) instead of blocking forever, even before the
     BackendUnavailable check would otherwise fire.
  4. align_transcript_to_events is PURE and READ-ONLY on the events: it never
     mutates the input, never changes any existing key (timing or verdict),
     and returns a new list of new dicts with only one key added.
"""

from __future__ import annotations

import os
import sys

import pytest

from hotato._engine.vad import BackendUnavailable
from hotato.transcribe import (
    Transcript,
    TranscriptSegment,
    align_transcript_to_events,
    transcribe,
)


@pytest.fixture(autouse=True)
def _home(monkeypatch, tmp_path):
    # A CLI `--transcribe` run builds the default transcript cache under
    # HOME; keep it under a per-test tmp dir, never the real ~/.hotato.
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("USERPROFILE", str(tmp_path))


def _faster_whisper_installed() -> bool:
    try:
        import faster_whisper  # noqa: F401

        return True
    except Exception:
        return False


# --------------------------------------------------------------------------- #
# 1. Lazy import.
# --------------------------------------------------------------------------- #

def test_importing_module_never_imports_faster_whisper():
    """Importing hotato.transcribe must not import faster_whisper as a side
    effect -- the extra is strictly opt-in, exactly like neural/diarize."""
    assert "faster_whisper" not in sys.modules or _faster_whisper_installed()
    # A stronger check independent of whatever else happened to import it in
    # this process: reload in a subprocess with the module fresh.
    import subprocess

    code = (
        "import sys\n"
        "import hotato.transcribe\n"
        "assert 'faster_whisper' not in sys.modules, "
        "'hotato.transcribe imported faster_whisper at import time'\n"
    )
    result = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True)
    assert result.returncode == 0, result.stderr


# --------------------------------------------------------------------------- #
# 2. Clean error, no fallback, when the extra is absent.
# --------------------------------------------------------------------------- #

def test_missing_extra_raises_clean_backend_unavailable(tmp_path):
    if _faster_whisper_installed():
        pytest.skip("faster-whisper is installed here; the missing-extra path is not exercisable")
    wav = tmp_path / "call.wav"
    _write_tiny_wav(wav)
    with pytest.raises(BackendUnavailable) as ei:
        transcribe(str(wav))
    msg = str(ei.value).lower()
    assert "pip install" in msg and "hotato[transcribe]" in msg


def test_missing_extra_error_is_a_handled_error_class():
    """BackendUnavailable is in errors.HANDLED, so a missing extra becomes a
    clean exit-2 / structured error on both the CLI and MCP surfaces, never a
    raw traceback -- exactly like neural/diarize."""
    from hotato.errors import HANDLED

    assert BackendUnavailable in HANDLED


# --------------------------------------------------------------------------- #
# 3. require_regular_file guard runs before anything else.
# --------------------------------------------------------------------------- #

@pytest.mark.skipif(not hasattr(os, "mkfifo"), reason="FIFOs require a POSIX OS")
def test_fifo_input_is_rejected_before_any_model_work(tmp_path):
    fifo = tmp_path / "live.fifo"
    os.mkfifo(str(fifo))
    try:
        with pytest.raises(ValueError) as ei:
            transcribe(str(fifo))
        msg = str(ei.value).lower()
        assert "fifo" in msg or "named pipe" in msg or "not a regular file" in msg
    finally:
        os.remove(str(fifo))


def test_missing_file_raises_oserror_not_a_hang(tmp_path):
    missing = tmp_path / "does-not-exist.wav"
    with pytest.raises(OSError):
        transcribe(str(missing))


# --------------------------------------------------------------------------- #
# 4. align_transcript_to_events: pure, read-only, additive.
# --------------------------------------------------------------------------- #

def _transcript():
    return Transcript(
        text="hello there how can I help",
        segments=[
            TranscriptSegment(start=0.0, end=1.0, text="hello there"),
            TranscriptSegment(start=1.2, end=2.5, text="how can I help"),
        ],
        language="en",
        model="base.en",
        device="cpu",
        compute_type="int8",
    )


def test_attaches_overlapping_span_as_context():
    events = [{"start_sec": 0.2, "end_sec": 0.8, "did_yield": True, "talk_over_sec": 0.1}]
    out = align_transcript_to_events(_transcript(), events)
    assert out[0]["transcript_context"]["text"] == "hello there"
    assert out[0]["transcript_context"]["segments"] == [
        {"start": 0.0, "end": 1.0, "text": "hello there"}
    ]


def test_point_event_time_sec_is_supported():
    events = [{"time_sec": 1.3}]
    out = align_transcript_to_events(_transcript(), events)
    assert out[0]["transcript_context"]["text"] == "how can I help"


def test_event_spanning_both_segments_gets_both_in_order():
    events = [{"start_sec": 0.5, "end_sec": 2.0}]
    out = align_transcript_to_events(_transcript(), events)
    assert out[0]["transcript_context"]["text"] == "hello there how can I help"


def test_event_with_no_timing_gets_empty_context_not_dropped():
    events = [{"note": "no timing here"}]
    out = align_transcript_to_events(_transcript(), events)
    assert len(out) == 1
    assert out[0]["transcript_context"] == {"text": "", "segments": []}
    assert out[0]["note"] == "no timing here"


def test_never_mutates_input_events_or_list():
    events = [{"start_sec": 0.0, "end_sec": 1.0, "did_yield": True, "talk_over_sec": 0.42}]
    import copy

    before = copy.deepcopy(events)
    out = align_transcript_to_events(_transcript(), events)
    assert events == before
    assert "transcript_context" not in events[0]
    assert out[0] is not events[0]


def test_verdict_and_timing_fields_pass_through_unchanged():
    """The whole point: attaching a transcript never alters did_yield,
    talk_over_sec, or any other verdict/timing field -- byte-identical in,
    byte-identical (plus one new key) out."""
    event = {
        "start_sec": 0.5,
        "end_sec": 1.5,
        "did_yield": False,
        "talk_over_sec": 1.57,
        "time_to_yield": None,
    }
    out = align_transcript_to_events(_transcript(), [event])[0]
    for key, value in event.items():
        assert out[key] == value


def test_output_length_and_order_match_input():
    events = [
        {"start_sec": 0.0, "end_sec": 0.5},
        {"note": "untimed"},
        {"time_sec": 1.3},
    ]
    out = align_transcript_to_events(_transcript(), events)
    assert len(out) == len(events)
    assert out[1]["note"] == "untimed"
    assert out[2]["transcript_context"]["text"] == "how can I help"


def test_returns_new_dict_instances():
    events = [{"start_sec": 0.0, "end_sec": 0.1}]
    out = align_transcript_to_events(_transcript(), events)
    assert isinstance(out, list)
    assert out is not events
    assert all(isinstance(e, dict) for e in out)


# --------------------------------------------------------------------------- #
# 5. Wiring into core.run_single / the CLI: opt-in, additive, byte-identical.
# --------------------------------------------------------------------------- #

def _bundled(sid):
    from importlib import resources

    return str(resources.files("hotato").joinpath("data", "audio", sid + ".example.wav"))


def test_run_single_transcribe_missing_extra_raises_backend_unavailable():
    """``core.run_single(..., transcribe=True)`` mirrors the module-level
    contract: with the extra absent, a clean BackendUnavailable, never a
    fallback that silently skips the transcript."""
    if _faster_whisper_installed():
        pytest.skip("faster-whisper is installed here; the missing-extra path is not exercisable")
    from hotato import core

    with pytest.raises(BackendUnavailable) as ei:
        core.run_single(stereo=_bundled("01-hard-interruption"), transcribe=True)
    msg = str(ei.value).lower()
    assert "pip install" in msg and "hotato[transcribe]" in msg


def test_run_single_transcribe_requires_a_single_audio_file(tmp_path):
    """--caller/--agent (two separate files) is not a single file to run ASR
    over; a clean usage error names --stereo instead of silently picking one
    channel or skipping the transcript."""
    from hotato import core

    caller = tmp_path / "caller.wav"
    agent = tmp_path / "agent.wav"
    _write_tiny_wav(caller)
    _write_tiny_wav(agent)
    with pytest.raises(ValueError, match="--stereo"):
        core.run_single(caller=str(caller), agent=str(agent), transcribe=True)


def test_default_run_has_no_transcript_surface():
    """--transcribe defaults to off: no ``transcript`` key at all, byte-identical
    to a run from before this feature existed."""
    from hotato import core

    env = core.run_single(stereo=_bundled("01-hard-interruption"))
    assert "transcript" not in env


def test_transcribe_attaches_context_without_changing_any_timing_number(monkeypatch):
    """The whole honesty contract, exercised end-to-end through core.run_single:
    attaching a transcript must never alter the timing verdict or measurements.
    Monkeypatches the (optional, absent-here) faster-whisper call with a fake
    Transcript so this is exercisable without the extra installed."""
    import hotato.transcribe as T
    from hotato import core

    path = _bundled("01-hard-interruption")
    baseline = core.run_single(stereo=path)

    fake = T.Transcript(
        text="fake context",
        segments=[T.TranscriptSegment(start=0.0, end=100.0, text="fake context")],
        language="en", model="base.en", device="cpu", compute_type="int8",
    )
    monkeypatch.setattr(
        T, "transcribe", lambda path, model=None, device=None, **kw: fake
    )

    with_transcript = core.run_single(stereo=path, transcribe=True)

    assert with_transcript["events"][0]["verdict"] == baseline["events"][0]["verdict"]
    assert with_transcript["events"][0]["measurements"] == baseline["events"][0]["measurements"]
    assert with_transcript["events"][0]["signals"] == baseline["events"][0]["signals"]
    transcript_block = dict(with_transcript["transcript"])
    # transcribe_cache was not passed to run_single, so caching is off:
    # cached=False, drift=None, but a cache_key is still computed (a pure
    # content address, free provenance even without a cache backend).
    cache = transcript_block.pop("cache")
    assert cache["cached"] is False and cache["drift"] is None
    assert isinstance(cache["cache_key"], str) and len(cache["cache_key"]) == 64
    assert transcript_block == {
        "text": "fake context",
        "segments": [{"start": 0.0, "end": 100.0, "text": "fake context"}],
        "model": "base.en", "device": "cpu", "compute_type": "int8", "language": "en",
    }


def test_cli_transcribe_missing_extra_exits_2():
    """End-to-end through the CLI: `hotato run --stereo ... --transcribe`
    exits 2 (never a fallback, never a silent skip) when the extra is absent."""
    if _faster_whisper_installed():
        pytest.skip("faster-whisper is installed here; the missing-extra path is not exercisable")
    from hotato import cli

    assert cli.main([
        "run", "--stereo", _bundled("01-hard-interruption"),
        "--transcribe", "--format", "json",
    ]) == 2


def test_cli_transcribe_rejects_suite_mode():
    from hotato import cli

    assert cli.main(["run", "--suite", "barge-in", "--transcribe", "--format", "json"]) == 2


def test_cli_transcribe_rejects_caller_agent(tmp_path):
    from hotato import cli

    caller = tmp_path / "caller.wav"
    agent = tmp_path / "agent.wav"
    _write_tiny_wav(caller)
    _write_tiny_wav(agent)
    assert cli.main([
        "run", "--caller", str(caller), "--agent", str(agent),
        "--transcribe", "--format", "json",
    ]) == 2


# --------------------------------------------------------------------------- #
# helpers
# --------------------------------------------------------------------------- #

def _write_tiny_wav(path):
    import struct
    import wave

    sr = 16000
    n = sr // 10  # 0.1s of silence is enough; we never reach decode without the extra
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(sr)
        w.writeframes(struct.pack("<%dh" % n, *([0] * n)))
