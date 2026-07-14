"""``--transcribe``: the opt-in, CONTEXT-ONLY transcript layer wired into
``hotato run`` (see ``core._attach_transcript_context`` / ``hotato.transcribe``
for the seam itself). Pins the honesty invariants that make this flag
shippable, at both the ``core.run_single`` layer and the CLI layer:

  1. BYTE-IDENTICAL TIMING -- adding ``--transcribe`` to a run never changes
     ``did_yield`` / ``talk_over_sec`` / ``time_to_yield`` / any other verdict
     or measurement field. The only difference is the additive top-level
     ``transcript`` key.
  2. NO SILENT FALLBACK -- with the ``[transcribe]`` extra absent, --transcribe
     errors cleanly (exit 2, a "pip install hotato[transcribe]" message),
     never silently skipping the transcript or scoring differently.
  3. --transcribe requires a single audio file (--stereo, or --mono
     --diarize); the separate --caller/--agent shape and --suite/battery mode
     are both clean usage errors, never a silent partial transcript.

Uses a monkeypatched ``hotato.transcribe.transcribe`` so the byte-identical and
usage-error tests run without the ``[transcribe]`` extra installed, exactly
like ``test_diarize.py``'s stub diarizer backend.
"""

import json
from importlib import resources

import pytest

from hotato import cli, core
from hotato import transcribe as T


def _bundled(sid):
    return str(
        resources.files("hotato").joinpath("data", "audio", sid + ".example.wav")
    )


def _fake_transcribe(path, model="base.en", device="auto", **kw):
    return T.Transcript(
        text="hello there, how can I help you today",
        segments=[
            T.TranscriptSegment(start=0.0, end=1.2, text="hello there,"),
            T.TranscriptSegment(start=1.2, end=2.8, text="how can I help you today"),
        ],
        language="en",
        model=model,
        device="cpu",
        compute_type="int8",
    )


@pytest.fixture
def stub_transcribe(monkeypatch):
    """Replace hotato.transcribe.transcribe with a hermetic, deterministic
    stub for the duration of a test -- no faster-whisper, no model download,
    no network. core._attach_transcript_context does `from .transcribe import
    transcribe as _transcribe` INSIDE its function body on every call, so
    patching the module attribute here is picked up immediately."""
    monkeypatch.setattr(T, "transcribe", _fake_transcribe)


def _faster_whisper_installed():
    try:
        import faster_whisper  # noqa: F401

        return True
    except Exception:
        return False


# --------------------------------------------------------------------------- #
# 1. Byte-identical timing numbers, with and without --transcribe.
# --------------------------------------------------------------------------- #

def test_run_single_transcribe_does_not_change_timing(stub_transcribe):
    wav = _bundled("01-hard-interruption")
    baseline = core.run_single(stereo=wav)
    with_t = core.run_single(stereo=wav, transcribe=True)

    assert with_t["events"][0]["verdict"] == baseline["events"][0]["verdict"]
    assert with_t["events"][0]["measurements"] == baseline["events"][0]["measurements"]
    assert with_t["events"][0]["signals"] == baseline["events"][0]["signals"]
    assert with_t["summary"] == baseline["summary"]
    assert with_t["exit_code"] == baseline["exit_code"]

    # Only the additive top-level `transcript` key differs.
    assert "transcript" not in baseline
    assert with_t["transcript"]["text"] == "hello there, how can I help you today"
    assert with_t["transcript"]["model"] == "base.en"
    for key in baseline["events"][0]:
        assert with_t["events"][0][key] == baseline["events"][0][key]


def test_cli_run_json_envelope_timing_is_byte_identical_with_transcribe(
    stub_transcribe, capsys
):
    wav = _bundled("01-hard-interruption")
    cli.main(["run", "--stereo", wav, "--format", "json"])
    baseline = json.loads(capsys.readouterr().out)

    cli.main(["run", "--stereo", wav, "--transcribe", "--format", "json"])
    with_t = json.loads(capsys.readouterr().out)

    with_t_stripped = dict(with_t)
    with_t_stripped.pop("transcript")
    assert with_t_stripped == baseline


def test_cli_run_text_panel_labeled_context_not_scored(stub_transcribe, capsys):
    wav = _bundled("01-hard-interruption")
    code = cli.main(["run", "--stereo", wav, "--transcribe"])
    out = capsys.readouterr().out
    assert code in (0, 1)
    assert "Transcript (context, not scored):" in out
    assert "hello there" in out
    assert "never affects the verdict" in out


def test_cli_run_text_output_unaffected_without_transcribe(capsys):
    """Without --transcribe: zero behaviour change -- no panel, no mention of
    a transcript anywhere in the default text output."""
    wav = _bundled("01-hard-interruption")
    cli.main(["run", "--stereo", wav])
    out = capsys.readouterr().out
    assert "Transcript" not in out


# --------------------------------------------------------------------------- #
# 2. Missing extra -> clean error, never a silent fallback.
# --------------------------------------------------------------------------- #

def test_run_single_transcribe_without_extra_raises_backend_unavailable():
    if _faster_whisper_installed():
        pytest.skip("faster-whisper is installed here; the missing-extra path is not exercisable")
    from hotato._engine.vad import BackendUnavailable

    wav = _bundled("01-hard-interruption")
    with pytest.raises(BackendUnavailable) as ei:
        core.run_single(stereo=wav, transcribe=True)
    msg = str(ei.value).lower()
    assert "pip install" in msg and "hotato[transcribe]" in msg


def test_cli_run_transcribe_without_extra_errors_cleanly(capsys):
    if _faster_whisper_installed():
        pytest.skip("faster-whisper is installed here; the missing-extra path is not exercisable")
    wav = _bundled("01-hard-interruption")
    code = cli.main(["run", "--stereo", wav, "--transcribe"])
    assert code == 2
    err = capsys.readouterr().err
    assert "pip install" in err.lower()
    assert "hotato[transcribe]" in err.lower()


def test_cli_run_transcribe_without_extra_json_error_is_structured(capsys):
    if _faster_whisper_installed():
        pytest.skip("faster-whisper is installed here; the missing-extra path is not exercisable")
    wav = _bundled("01-hard-interruption")
    code = cli.main(["run", "--stereo", wav, "--transcribe", "--format", "json"])
    assert code == 2
    payload = json.loads(capsys.readouterr().out)
    assert payload["ok"] is False
    assert payload["error_code"] == "backend_unavailable"
    assert "transcribe" in payload["message"].lower()


# --------------------------------------------------------------------------- #
# 3. --transcribe requires a single audio file: --suite and --caller/--agent
#    are clean usage errors, never a silent partial/skipped transcript.
# --------------------------------------------------------------------------- #

def test_transcribe_with_suite_is_a_clean_usage_error(stub_transcribe, capsys):
    code = cli.main(["run", "--suite", "barge-in", "--transcribe"])
    assert code == 2
    err = capsys.readouterr().err
    assert "single recording" in err


def test_transcribe_with_caller_agent_pair_is_a_clean_usage_error(
    stub_transcribe, tmp_path
):
    from hotato._engine.audio import write_wav

    caller = tmp_path / "caller.wav"
    agent = tmp_path / "agent.wav"
    write_wav(str(caller), 16000, [[0.1] * 1600])
    write_wav(str(agent), 16000, [[0.2] * 1600])
    with pytest.raises(ValueError, match="stereo"):
        core.run_single(caller=str(caller), agent=str(agent), transcribe=True)
