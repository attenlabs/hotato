"""P12: research exports. The CSVs must carry exactly the documented columns,
one row per event / per frame (counts verifiable against the envelope and the
frame dump), header comments documenting every column, and the JSON envelope
byte-equal to a plain run. Stdlib csv only; empty cell = not derivable.
"""

import csv
import json

from importlib import resources

from hotato import cli
from hotato.core import dump_frames_for_input, run_suite
from hotato.export import EVENT_COLUMNS, FRAME_COLUMNS


def _bundled(sid):
    return str(
        resources.files("hotato").joinpath("data", "audio", sid + ".example.wav")
    )


def _read_csv(path):
    """Split a CSV into (comment_lines, header, rows)."""
    with open(path, encoding="utf-8", newline="") as fh:
        lines = fh.read().splitlines()
    comments = [ln for ln in lines if ln.startswith("#")]
    data = [ln for ln in lines if not ln.startswith("#")]
    parsed = list(csv.reader(data))
    return comments, parsed[0], parsed[1:]


def test_export_suite_writes_all_three_files(tmp_path):
    out = tmp_path / "research"
    code = cli.main(["export", "--suite", "barge-in", "--out", str(out)])
    assert code == 0
    assert (out / "events.csv").exists()
    assert (out / "frames.csv").exists()
    assert (out / "envelope.json").exists()


def test_events_csv_columns_and_row_count(tmp_path):
    out = tmp_path / "research"
    assert cli.main(["export", "--suite", "barge-in", "--out", str(out)]) == 0
    comments, header, rows = _read_csv(out / "events.csv")
    assert header == EVENT_COLUMNS
    env = run_suite(suite="barge-in")
    assert len(rows) == env["summary"]["events"] == 8
    # every column is documented in the header comments
    assert comments, "events.csv must document its columns in # comments"
    doc = "\n".join(comments)
    for col in EVENT_COLUMNS:
        assert col in doc, f"column {col!r} undocumented in events.csv header"
    # spot-check real measurements land in the right columns
    by_id = {r[header.index("event_id")]: r for r in rows}
    e01 = by_id["01-hard-interruption"]
    ref = next(e for e in env["events"] if e["event_id"] == "01-hard-interruption")
    assert e01[header.index("passed")] == "true"
    assert float(e01[header.index("seconds_to_yield")]) == ref["verdict"]["seconds_to_yield"]
    assert float(e01[header.index("talk_over_sec")]) == ref["verdict"]["talk_over_sec"]
    # a hold scenario has no yield: empty cell, never a fabricated number
    e02 = by_id["02-backchannel-mhm"]
    assert e02[header.index("seconds_to_yield")] == ""
    assert e02[header.index("expected_yield")] == "false"


def test_frames_csv_columns_and_row_count_single(tmp_path):
    wav = _bundled("01-hard-interruption")
    out = tmp_path / "research"
    assert cli.main(["export", "--stereo", wav, "--out", str(out)]) == 0
    comments, header, rows = _read_csv(out / "frames.csv")
    assert header == FRAME_COLUMNS
    dump = dump_frames_for_input(stereo=wav)
    assert len(rows) == len(dump["frames"])
    doc = "\n".join(comments)
    for col in FRAME_COLUMNS:
        assert col in doc, f"column {col!r} undocumented in frames.csv header"
    # frame values match the dump exactly (same measurement, flat format)
    f0 = dump["frames"][0]
    assert float(rows[0][header.index("t_sec")]) == f0["t_sec"]
    assert float(rows[0][header.index("caller_dbfs")]) == f0["caller_dbfs"]
    assert rows[0][header.index("caller_active")] in ("true", "false")

    # events.csv for a single recording: exactly one row
    _, eheader, erows = _read_csv(out / "events.csv")
    assert len(erows) == 1


def test_frames_csv_suite_covers_every_event(tmp_path):
    out = tmp_path / "research"
    assert cli.main(["export", "--suite", "barge-in", "--out", str(out)]) == 0
    _, header, rows = _read_csv(out / "frames.csv")
    event_ids = {r[header.index("event_id")] for r in rows}
    env = run_suite(suite="barge-in")
    assert event_ids == {e["event_id"] for e in env["events"]}
    # row count equals the sum of the per-scenario frame dumps
    expected = 0
    for e in env["events"]:
        wav = _bundled(e["scenario_id"])
        expected += len(dump_frames_for_input(stereo=wav)["frames"])
    assert len(rows) == expected


def test_envelope_json_matches_plain_run(tmp_path):
    out = tmp_path / "research"
    assert cli.main(["export", "--suite", "barge-in", "--out", str(out)]) == 0
    written = json.loads((out / "envelope.json").read_text(encoding="utf-8"))
    assert json.dumps(written, sort_keys=True) == json.dumps(
        run_suite(suite="barge-in"), sort_keys=True)


def test_export_exit_codes(tmp_path):
    wav = _bundled("01-hard-interruption")
    # regression (impossible bound) -> 1; --no-fail -> 0; usage errors -> 2
    assert cli.main(["export", "--stereo", wav, "--max-time-to-yield", "0.0",
                     "--out", str(tmp_path / "a")]) == 1
    assert cli.main(["export", "--stereo", wav, "--max-time-to-yield", "0.0",
                     "--no-fail", "--out", str(tmp_path / "b")]) == 0
    assert cli.main(["export", "--out", str(tmp_path / "c")]) == 2
    assert cli.main(["export", "--suite", "barge-in", "--stereo", wav,
                     "--out", str(tmp_path / "d")]) == 2
    assert cli.main(["export", "--stereo", "/nonexistent/nope.wav",
                     "--out", str(tmp_path / "e")]) == 2


def test_export_csv_no_percent_or_dashes(tmp_path):
    out = tmp_path / "research"
    assert cli.main(["export", "--suite", "barge-in", "--out", str(out)]) == 0
    for name in ("events.csv", "frames.csv"):
        text = (out / name).read_text(encoding="utf-8")
        assert "–" not in text and "—" not in text


def test_export_not_scorable_single_exits_2(tmp_path):
    """export follows the same not-scorable exit mapping as run."""
    import struct
    import wave

    p = tmp_path / "silent.wav"
    w = wave.open(str(p), "wb")
    w.setnchannels(2)
    w.setsampwidth(2)
    w.setframerate(16000)
    w.writeframes(struct.pack("<" + "h" * 6400, *([0] * 6400)))
    w.close()
    rc = cli.main(["export", "--stereo", str(p), "--out", str(tmp_path / "out")])
    assert rc == 2
