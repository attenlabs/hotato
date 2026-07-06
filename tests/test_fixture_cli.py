"""`hotato fixture create`: one bad call moment -> a permanent regression
fixture that round-trips through `hotato run --scenarios DIR --audio DIR`.

Pinned here: the two output files and their schema shape, the clip onset
re-basing (the measured timing must match the uncut original), --no-clip,
overwrite refusal without --force, the not-scorable refusal (exit 2, partial
outputs removed), and the usage-error exit codes.
"""

import json
import shlex
import struct
import wave
from importlib import resources

import pytest

from hotato import cli
from hotato._engine.audio import read_wav, write_wav
from hotato.core import run_single, run_suite


def _bundled(sid):
    return str(
        resources.files("hotato").joinpath("data", "audio", sid + ".example.wav")
    )


HARD = _bundled("01-hard-interruption")          # caller onset 2.40, yields
BACKCHANNEL = _bundled("02-backchannel-mhm")     # caller onset 2.10, holds


def _create(tmp_path, *extra, src=HARD, onset="2.40", expect="yield",
            fid="fx-created-001"):
    return cli.main([
        "fixture", "create", "--stereo", src, "--id", fid,
        "--onset", onset, "--expect", expect, "--out", str(tmp_path),
        *extra,
    ])


def _scenario(tmp_path, fid="fx-created-001"):
    with open(tmp_path / "scenarios" / (fid + ".json"), encoding="utf-8") as fh:
        return json.load(fh)


def _write_silence(path, sample_rate, n_channels, n_frames):
    with wave.open(str(path), "wb") as w:
        w.setnchannels(n_channels)
        w.setsampwidth(2)
        w.setframerate(sample_rate)
        w.writeframes(struct.pack(
            "<" + "h" * (n_frames * n_channels),
            *([0] * (n_frames * n_channels))))


# --- creation, shape, and the round trip -----------------------------------

def test_create_from_stereo_writes_both_files(tmp_path):
    assert _create(tmp_path) == 0
    assert (tmp_path / "scenarios" / "fx-created-001.json").exists()
    assert (tmp_path / "audio" / "fx-created-001.example.wav").exists()
    wav = read_wav(str(tmp_path / "audio" / "fx-created-001.example.wav"))
    assert wav.num_channels == 2


def test_scenario_shape_matches_the_bundled_schema(tmp_path):
    assert _create(tmp_path, "--tags", "core, refund") == 0
    sc = _scenario(tmp_path)
    assert sc["id"] == "fx-created-001"
    assert sc["title"] == "fx created 001"        # auto from the id
    assert sc["category"] == "should_yield"
    assert sc["tags"] == ["core", "refund"]
    assert sc["sample_rate"] == 16000
    assert sc["expected"] == {"yield": True, "max_time_to_yield_sec": None,
                              "max_talk_over_sec": None}
    assert sc["provenance"]["source"] == "01-hard-interruption.example.wav"
    assert sc["provenance"]["source_onset_sec"] == 2.40
    assert sc["provenance"]["created_by"] == "hotato fixture create"
    assert "should yield" in sc["why_it_matters"]


def test_split_channel_input_writes_one_two_channel_wav(tmp_path):
    src = read_wav(HARD)
    caller = tmp_path / "c.wav"
    agent = tmp_path / "a.wav"
    write_wav(str(caller), src.sample_rate, [src.get(0)])
    write_wav(str(agent), src.sample_rate, [src.get(1)])
    rc = cli.main([
        "fixture", "create", "--caller", str(caller), "--agent", str(agent),
        "--id", "fx-split-001", "--onset", "2.40", "--expect", "yield",
        "--out", str(tmp_path),
    ])
    assert rc == 0
    wav = read_wav(str(tmp_path / "audio" / "fx-split-001.example.wav"))
    assert wav.num_channels == 2


def test_created_fixture_round_trips_through_run(tmp_path):
    assert _create(tmp_path, "--max-talk-over", "0.8",
                   "--max-time-to-yield", "0.7") == 0
    # The exact command the tool suggests, through the CLI entry point.
    assert cli.main(["run", "--suite", "barge-in",
                     "--scenarios", str(tmp_path / "scenarios"),
                     "--audio", str(tmp_path / "audio")]) == 0
    env = run_suite(scenarios_dir=str(tmp_path / "scenarios"),
                    audio_dir=str(tmp_path / "audio"))
    (event,) = env["events"]
    assert event["event_id"] == "fx-created-001"
    assert event["category"] == "should_yield"
    assert event["verdict"]["passed"] is True


def test_next_command_from_fixture_create_runs_verbatim(tmp_path, capsys):
    """Regression: the exact `next` command `fixture create` emits (no bare
    --suite) must run and PASS on its own fixture -- this is the command a
    user actually copies and pastes."""
    assert _create(tmp_path, "--format", "json") == 0
    out = json.loads(capsys.readouterr().out)
    next_cmd = out["next"]
    assert next_cmd.startswith("hotato ")
    argv = shlex.split(next_cmd)[1:]  # drop the leading "hotato"
    assert "--suite" not in argv
    assert cli.main(argv) == 0
    assert "PASS" in capsys.readouterr().out


def test_scenarios_and_audio_without_suite_flag_scores_directly(tmp_path):
    """`hotato run --scenarios DIR --audio DIR` (no bare --suite) must enter
    suite mode on its own: this is the exact form documented in the fixture
    create epilog, docs/BAD-CALL-TO-CI.md, and the CI YAML snippet."""
    assert _create(tmp_path) == 0
    assert cli.main([
        "run", "--scenarios", str(tmp_path / "scenarios"),
        "--audio", str(tmp_path / "audio"),
    ]) == 0


def test_scenarios_and_audio_with_stereo_is_still_a_usage_error(tmp_path,
                                                                 capsys):
    """The implicit suite mode (--scenarios + --audio, no bare --suite) keeps
    the same conflict rule as the explicit --suite case: it cannot be mixed
    with a single-recording input."""
    assert _create(tmp_path) == 0
    rc = cli.main([
        "run", "--scenarios", str(tmp_path / "scenarios"),
        "--audio", str(tmp_path / "audio"), "--stereo", HARD,
    ])
    assert rc == 2
    assert "cannot be combined" in capsys.readouterr().err


def test_hold_label_gets_null_bounds_and_hold_category(tmp_path):
    rc = _create(tmp_path, src=BACKCHANNEL, onset="2.10", expect="hold",
                 fid="fx-hold-001")
    assert rc == 0
    sc = _scenario(tmp_path, "fx-hold-001")
    assert sc["category"] == "should_not_yield"
    assert sc["expected"] == {"yield": False, "max_time_to_yield_sec": None,
                              "max_talk_over_sec": None}
    assert sc["related_signals"] == ["did_yield"]
    assert "should hold" in sc["why_it_matters"]


# --- the clip: onset re-based, measurements preserved -----------------------

def test_clip_rebases_onset_and_preserves_the_measured_timing(tmp_path):
    assert _create(tmp_path) == 0
    sc = _scenario(tmp_path)
    # source onset 2.40, default --pre 2.0: the clip starts at 0.40 and the
    # fixture onset is re-based to 2.00.
    assert sc["caller_onset_sec"] == pytest.approx(2.0, abs=0.001)
    assert sc["provenance"]["clip_start_sec"] == pytest.approx(0.4, abs=0.001)

    uncut = run_single(stereo=HARD, onset_sec=2.40, expect="yield")
    cut = run_suite(scenarios_dir=str(tmp_path / "scenarios"),
                    audio_dir=str(tmp_path / "audio"))
    v_uncut = uncut["events"][0]["verdict"]
    v_cut = cut["events"][0]["verdict"]
    assert v_cut["did_yield"] is True
    assert v_cut["seconds_to_yield"] == pytest.approx(
        v_uncut["seconds_to_yield"], abs=0.05)
    assert v_cut["talk_over_sec"] == pytest.approx(
        v_uncut["talk_over_sec"], abs=0.05)


def test_onset_near_start_clips_from_zero(tmp_path):
    rc = _create(tmp_path, "--pre", "5.0", fid="fx-early-001")
    assert rc == 0
    sc = _scenario(tmp_path, "fx-early-001")
    assert sc["provenance"]["clip_start_sec"] == 0.0
    assert sc["caller_onset_sec"] == pytest.approx(2.40, abs=0.001)


def test_no_clip_keeps_full_audio_and_original_onset(tmp_path):
    assert _create(tmp_path, "--no-clip", fid="fx-full-001") == 0
    sc = _scenario(tmp_path, "fx-full-001")
    assert sc["caller_onset_sec"] == pytest.approx(2.40, abs=0.001)
    src = read_wav(HARD)
    out = read_wav(str(tmp_path / "audio" / "fx-full-001.example.wav"))
    assert out.num_samples == src.num_samples
    assert sc["duration_sec"] == pytest.approx(src.duration_sec, abs=0.001)


# --- refusals: overwrite, unusable input, usage errors ----------------------

def test_overwrite_refused_without_force(tmp_path, capsys):
    assert _create(tmp_path) == 0
    assert _create(tmp_path) == 2
    assert "--force" in capsys.readouterr().err
    assert _create(tmp_path, "--force") == 0


def test_not_scorable_moment_refused_with_reason_and_cleaned_up(tmp_path,
                                                                capsys):
    # At 5.5 s the agent is long silent in 01-hard-interruption: a
    # should-yield label there has no meaning, so creation is refused.
    rc = _create(tmp_path, onset="5.5", fid="fx-bad-001")
    assert rc == 2
    err = capsys.readouterr().err
    assert "not scorable" in err
    assert "agent was not talking" in err
    assert not (tmp_path / "scenarios" / "fx-bad-001.json").exists()
    assert not (tmp_path / "audio" / "fx-bad-001.example.wav").exists()


def test_mono_input_is_refused(tmp_path):
    mono = tmp_path / "mono.wav"
    _write_silence(mono, 16000, 1, 16000)
    assert _create(tmp_path, src=str(mono)) == 2


def test_sample_rate_mismatch_on_split_input_is_refused(tmp_path):
    a = tmp_path / "c16.wav"
    b = tmp_path / "a8.wav"
    _write_silence(a, 16000, 1, 16000)
    _write_silence(b, 8000, 1, 8000)
    assert cli.main(["fixture", "create", "--caller", str(a),
                     "--agent", str(b), "--id", "fx-sr-001", "--onset", "1.0",
                     "--expect", "yield", "--out", str(tmp_path)]) == 2


def test_missing_onset_is_a_usage_error():
    with pytest.raises(SystemExit) as exc:
        cli.main(["fixture", "create", "--stereo", HARD, "--id", "fx-x",
                  "--expect", "yield", "--out", "/tmp/nowhere"])
    assert exc.value.code == 2


def test_invalid_slug_is_refused(tmp_path):
    assert _create(tmp_path, fid="Not A Slug!") == 2
    assert _create(tmp_path, fid="trailing-hyphen-") == 2


def test_bounds_do_not_apply_to_hold(tmp_path):
    rc = cli.main(["fixture", "create", "--stereo", BACKCHANNEL,
                   "--id", "fx-hold-b", "--onset", "2.10", "--expect", "hold",
                   "--max-talk-over", "0.5", "--out", str(tmp_path)])
    assert rc == 2


# --- machine output ----------------------------------------------------------

def test_json_output_carries_paths_validation_and_next(tmp_path, capsys):
    rc = _create(tmp_path, "--format", "json")
    assert rc == 0
    out = json.loads(capsys.readouterr().out)
    assert out["tool"] == "hotato"
    assert out["kind"] == "fixture"
    assert out["paths"]["scenario"].endswith("fx-created-001.json")
    assert out["paths"]["audio"].endswith("fx-created-001.example.wav")
    assert out["onset"] == {"source_sec": 2.4, "fixture_sec": 2.0}
    assert out["validation"]["tool"] == "hotato"
    assert out["validation"]["events"][0]["event_id"] == "fx-created-001"
    assert out["next"].startswith("hotato run --scenarios ")


def test_text_output_states_the_rebased_onset_and_next_command(tmp_path,
                                                               capsys):
    assert _create(tmp_path) == 0
    out = capsys.readouterr().out
    assert "created Hotato fixture: fx-created-001" in out
    assert "2.40s source -> 2.00s fixture" in out
    assert "check:    scorable" in out
    assert "hotato run --scenarios" in out
