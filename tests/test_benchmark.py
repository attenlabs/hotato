"""Tests for the reproducible measurement-error harness (``hotato.benchmark``).

The harness turns the synthetic fixtures into an honest report: per-signal
measurement error in milliseconds and a ``did_yield`` confusion matrix, with NO
accuracy percentage. These tests assert:

  1. it runs on every bundled + example synthetic fixture and returns the report
     structure it promises;
  2. the reported ms-errors are within KNOWN, config-derived tolerances -- reusing
     the same "within one frame hop of what was rendered" logic the existing signal
     and latency tests use. Under the default shipped config the yield/gap error is
     the exposed VAD hangover; with the hangover neutralised (exactly as
     ``tests/test_signals.py`` / ``tests/test_examples_latency.py`` do) every signal
     collapses to within one hop of the rendered ground truth -- which proves the
     error is framing/hangover, not an accuracy ceiling;
  3. the confusion matrix matches the known rendered behaviour of each set (the
     all-pass bundled + example sets sit on the diagonal; the deliberately-bad
     ``funnel-demo`` set is the only off-diagonal, and it is intended);
  4. nothing is fabricated (no reference => no error) and no accuracy percentage
     ever appears.
"""

import json
import os

import pytest

from hotato._engine.score import ScoreConfig
from hotato._engine.vad import VADParams
from hotato.benchmark import (
    ERROR_SIGNALS,
    default_fixture_sets,
    main,
    measure_fixture,
    render_markdown,
    rendered_references,
    run_benchmark,
    write_artifacts,
)


def _no_hangover_cfg():
    # Neutralise the VAD hangover so active-track edges equal rendered edges to
    # within one hop -- identical to the isolation the existing tests use.
    return ScoreConfig(
        caller_vad=VADParams(hangover_sec=0.0),
        agent_vad=VADParams(hangover_sec=0.0),
    )


def _sets_by_name(report):
    return {s["name"]: s for s in report["sets"]}


def _rows_by_id(report):
    out = {}
    for s in report["sets"]:
        for r in s["rows"]:
            out[r["id"]] = r
    return out


# --- structure + it actually runs -----------------------------------------

def test_harness_runs_on_synthetic_fixtures_and_reports_structure():
    report = run_benchmark(default_fixture_sets())
    assert report["kind"] == "measurement-error-report"
    # bundled (8) + examples (6) + funnel-demo (2) = 16 synthetic fixtures
    assert report["fixtures_total"] == 16
    names = {s["name"] for s in report["sets"]}
    assert {"bundled", "examples", "funnel-demo"} <= names

    agg = report["aggregate"]["error_stats_ms"]
    assert set(agg.keys()) == set(ERROR_SIGNALS)
    # onset derivable for all but the echo-bleed fixture (no independent caller
    # speech); yield only for should-yield fixtures that actually stop; response
    # gap only for the two fixtures that record it.
    assert agg["onset_sec"]["n"] == 15
    assert agg["time_to_yield_sec"]["n"] == 9
    assert agg["response_gap_sec"]["n"] == 2


# --- reference derivation never fabricates ---------------------------------

def test_rendered_references_are_exact_and_honest():
    by_id = {sc["id"]: sc for s in default_fixture_sets() for sc, _ in s.fixtures}

    # a hard interruption: onset = first caller segment; yield = agent turn end - onset
    refs01 = rendered_references(by_id["01-hard-interruption"])
    assert refs01["onset_sec"] == 2.40
    assert abs(refs01["time_to_yield_sec"] - (2.75 - 2.40)) < 1e-9

    # a should-NOT-yield backchannel: onset present, NO yield reference invented
    refs02 = rendered_references(by_id["02-backchannel-mhm"])
    assert refs02["onset_sec"] == 2.10
    assert "time_to_yield_sec" not in refs02

    # echo-of-agent: the caller channel carries no real speech -> no onset ref
    refs07 = rendered_references(by_id["07-echo-bleed"])
    assert "onset_sec" not in refs07
    assert "time_to_yield_sec" not in refs07

    # a latency fixture records its rendered response gap
    refs_lat = rendered_references(by_id["lat-01-prompt-response-prompt"])
    assert refs_lat["response_gap_sec"] == 0.50


def test_no_reference_means_no_error_never_a_fabricated_number():
    report = run_benchmark(default_fixture_sets())
    rows = _rows_by_id(report)
    # echo-bleed: no onset/yield/gap ground truth -> every error is None, not 0
    for sig in ERROR_SIGNALS:
        assert rows["07-echo-bleed"]["error_ms"][sig] is None
    # every reported error is a non-negative magnitude in ms
    for r in rows.values():
        for sig in ERROR_SIGNALS:
            v = r["error_ms"][sig]
            assert v is None or v >= 0.0


# --- default shipped config: within KNOWN config-derived tolerances --------

def test_default_config_errors_within_known_tolerances():
    report = run_benchmark(default_fixture_sets())
    cfg = report["config"]
    hop_ms = cfg["hop_ms"]
    vad_hangover_ms = cfg["agent_vad_hangover_sec"] * 1000.0
    turn_end_ms = cfg["turn_end_silence_sec"] * 1000.0

    onset_tol = hop_ms + 1e-6                                  # within one hop
    yield_tol = vad_hangover_ms + hop_ms + 1e-6               # hangover + one hop
    gap_tol = turn_end_ms + vad_hangover_ms + 2 * hop_ms + 1e-6

    for r in _rows_by_id(report).values():
        e = r["error_ms"]
        if e["onset_sec"] is not None:
            assert e["onset_sec"] <= onset_tol, r["id"]
        if e["time_to_yield_sec"] is not None:
            assert e["time_to_yield_sec"] <= yield_tol, r["id"]
        if e["response_gap_sec"] is not None:
            assert e["response_gap_sec"] <= gap_tol, r["id"]


# --- neutralised hangover: everything collapses to within one hop ----------

def test_neutralised_hangover_collapses_every_error_to_within_one_hop():
    """With the VAD hangover removed, the measurement error is just the frame hop
    -- the canonical +/-1 hop bound from the existing signal/latency tests. This
    demonstrates the default-config error is the exposed hangover, not an accuracy
    ceiling."""
    report = run_benchmark(default_fixture_sets(), cfg=_no_hangover_cfg())
    hop_ms = report["config"]["hop_ms"]
    tol = hop_ms + 1e-6
    for r in _rows_by_id(report).values():
        for sig in ERROR_SIGNALS:
            v = r["error_ms"][sig]
            if v is not None:
                assert v <= tol, (r["id"], sig, v)


# --- confusion matrix matches the known rendered behaviour -----------------

def test_confusion_matrix_matches_rendered_agent_behaviour():
    report = run_benchmark(default_fixture_sets())
    conf = report["aggregate"]["confusion"]
    # 9 real yields correctly caught, 5 holds correctly kept; the only two
    # off-diagonal cells are the deliberately-bad funnel-demo agent.
    assert conf == {
        "correct_yield": 9,
        "missed_yield": 1,
        "false_yield": 1,
        "correct_hold": 5,
    }
    assert report["aggregate"]["confusion_off_diagonal"] == 2

    by_set = _sets_by_name(report)
    # the all-pass sets have ZERO off-diagonal (scorer agrees with a good agent)
    assert by_set["bundled"]["confusion"] == {
        "correct_yield": 6, "missed_yield": 0, "false_yield": 0, "correct_hold": 2,
    }
    assert by_set["examples"]["confusion"] == {
        "correct_yield": 3, "missed_yield": 0, "false_yield": 0, "correct_hold": 3,
    }
    # the bad-agent set is the ONLY source of off-diagonal cells, on purpose
    assert by_set["funnel-demo"]["confusion"] == {
        "correct_yield": 0, "missed_yield": 1, "false_yield": 1, "correct_hold": 0,
    }


# --- honesty: no accuracy percentage anywhere ------------------------------

def test_report_carries_no_accuracy_percentage():
    report = run_benchmark(default_fixture_sets())
    assert "no_accuracy_percent" in report["honesty"]
    # there is no aggregated accuracy figure in the report at all
    assert "accuracy" not in report["aggregate"]
    md = render_markdown(report)
    blob = json.dumps(report)
    # a percent sign would signal an accuracy/rate claim slipped in
    assert "%" not in md
    assert "%" not in blob


# --- artifacts round-trip --------------------------------------------------

def test_write_artifacts_produces_readable_json_and_markdown(tmp_path):
    report = run_benchmark(default_fixture_sets())
    json_path, md_path = write_artifacts(report, str(tmp_path))
    assert os.path.exists(json_path) and os.path.exists(md_path)
    with open(json_path, encoding="utf-8") as fh:
        loaded = json.load(fh)
    assert loaded["fixtures_total"] == 16
    with open(md_path, encoding="utf-8") as fh:
        md = fh.read()
    assert "measurement error" in md.lower()
    assert "confusion matrix" in md.lower()


# --- single-fixture entry point -------------------------------------------

def test_measure_fixture_shape():
    sets = default_fixture_sets()
    sc, wav = sets[0].fixtures[0]
    row = measure_fixture(sc, wav)
    assert set(row["error_ms"].keys()) == set(ERROR_SIGNALS)
    assert row["confusion_cell"] in (
        "correct_yield", "missed_yield", "false_yield", "correct_hold",
    )


# --- hostile input: a malformed BYO WAV must be a clean refusal, never a raw
# --- wave.Error/struct.error traceback or an uncaught exit-1 crash ---------

def test_measure_fixture_raises_clean_valueerror_on_malformed_wav(tmp_path):
    """Before the fix, measure_fixture() called the vendored engine's raw
    ``_engine.read_wav`` directly, so a non-WAV file escaped as an uncaught
    ``wave.Error`` traceback instead of the same actionable ``ValueError``
    every other entry point produces."""
    bad_wav = tmp_path / "not-a-wav.example.wav"
    bad_wav.write_text("this is plainly not a WAV file\n" * 4)
    sc = {"id": "not-a-wav"}
    with pytest.raises(ValueError, match="not a readable PCM WAV"):
        measure_fixture(sc, str(bad_wav))


def _write_byo_set(tmp_path, *, wav_bytes: bytes):
    scen_dir = tmp_path / "scenarios"
    audio_dir = tmp_path / "audio"
    scen_dir.mkdir()
    audio_dir.mkdir()
    (scen_dir / "bad-1.json").write_text(json.dumps({"id": "bad-1"}))
    (audio_dir / "bad-1.example.wav").write_bytes(wav_bytes)
    return scen_dir, audio_dir


def test_main_exits_2_on_malformed_byo_wav_never_a_traceback(tmp_path, capsys):
    """Reproduces the original defect at the ``python -m hotato.benchmark
    --scenarios ... --audio ...`` call site: a malformed WAV in a BYO audio dir
    used to propagate an uncaught wave.Error out of main() (default Python exit
    code 1, a raw traceback on stderr). main() must instead return the same
    exit-2 usage-error contract the real hotato CLI guarantees, with a clean
    one-line ``error: ...`` message and no traceback."""
    scen_dir, audio_dir = _write_byo_set(tmp_path, wav_bytes=b"garbage, not a RIFF/WAVE file")
    out_dir = tmp_path / "out"

    rc = main([
        "--scenarios", str(scen_dir),
        "--audio", str(audio_dir),
        "--out", str(out_dir),
        "--quiet",
    ])

    assert rc == 2
    captured = capsys.readouterr()
    assert captured.err.startswith("error: ")
    assert "Traceback" not in captured.err
    assert "Traceback" not in captured.out
    # no partial/bogus report should have been written on a failed run
    assert not os.path.exists(os.path.join(str(out_dir), "measurement-error.json"))


def test_main_exits_2_on_truncated_byo_wav(tmp_path, capsys):
    """A header that declares more frames than the data chunk actually holds
    (a truncated recording) must also be a clean exit 2, not a raw
    struct.error/EOFError traceback."""
    import struct
    import wave

    full = tmp_path / "full.wav"
    with wave.open(str(full), "wb") as w:
        w.setnchannels(2)
        w.setsampwidth(2)
        w.setframerate(16000)
        n_frames = 1600
        w.writeframes(struct.pack("<" + "h" * (n_frames * 2), *([0] * (n_frames * 2))))
    truncated = full.read_bytes()[:100]  # header intact, data chunk cut off

    scen_dir, audio_dir = _write_byo_set(tmp_path, wav_bytes=truncated)

    rc = main([
        "--scenarios", str(scen_dir),
        "--audio", str(audio_dir),
        "--out", str(tmp_path / "out"),
        "--quiet",
    ])

    assert rc == 2
    captured = capsys.readouterr()
    assert captured.err.startswith("error: ")
    assert "Traceback" not in captured.err
