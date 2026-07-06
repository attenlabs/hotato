"""P7: the stack benchmark harness (`hotato benchmark` / `benchmark compare`).

The harness scores the USER'S captured recordings against a named scenario set
and never fabricates: no recording, no number. Pinned here:

  - a benchmark over the bundled audio dir produces a valid result JSON whose
    events and summary match run_suite on the same input (scoring is reused,
    never duplicated);
  - missing recordings are listed plainly as not captured, never scored as
    failures;
  - the result timestamp derives from input file mtimes, not wall clock;
  - compare renders the md table with correct signed deltas, compares only the
    intersection when scenario sets differ, and says what was skipped;
  - usage errors exit 2; --fail-on-regression gates exit 1 on scored failures
    and only then.
"""

import json
import os
import shutil

from hotato import cli, stackbench
from hotato.core import run_suite

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
AUDIO_DIR = os.path.join(REPO, "src", "hotato", "data", "audio")
SCEN_DIR = os.path.join(REPO, "src", "hotato", "data", "scenarios")

ALL_IDS = sorted(
    fn[:-len(".json")] for fn in os.listdir(SCEN_DIR)
    if fn.endswith(".json") and fn != "manifest.json"
)


def _load_json(path):
    with open(path, encoding="utf-8") as fh:
        return json.load(fh)


# --- the benchmark run ------------------------------------------------------

def test_benchmark_bundled_matches_run_suite(tmp_path):
    out = tmp_path / "result.json"
    rc = cli.main(["benchmark", "--stack", "generic",
                   "--recordings", AUDIO_DIR, "--out", str(out)])
    assert rc == 0
    result = _load_json(out)

    assert result["tool"] == "hotato"
    assert result["kind"] == "stack-benchmark"
    assert result["schema_version"] == "1"
    assert result["stack"] == "generic"
    assert result["suite"] == "barge-in"
    assert result["offline"] is True
    assert result["scenarios"] == {
        "total": len(ALL_IDS), "captured": len(ALL_IDS), "not_captured": [],
    }

    # Scoring is core.run_suite unchanged: events and summary are identical to
    # a plain suite run over the same audio.
    ref = run_suite()
    assert result["events"] == ref["events"]
    assert result["summary"] == ref["summary"]
    assert result["fix_map"] == ref["fix_map"]

    # The exposed thresholds ship in the result.
    assert result["config"]["hop_ms"] > 0
    assert "caller_vad" in result["config"]

    # Provenance names who ran it and exactly which input files.
    prov = result["provenance"]
    assert prov["recordings_dir"] == os.path.abspath(AUDIO_DIR)
    assert prov["scenario_source"] == "bundled"
    assert prov["suffix"] == ".example.wav"
    assert [r["scenario_id"] for r in prov["recordings"]] == ALL_IDS
    assert all(r["bytes"] > 0 for r in prov["recordings"])

    # Timestamp is derived from input mtimes (deterministic), not wall clock.
    latest = max(
        os.path.getmtime(os.path.join(AUDIO_DIR, sid + ".example.wav"))
        for sid in ALL_IDS
    )
    assert result["generated_at_utc"] == stackbench._iso_utc(latest)
    assert "not wall clock" in result["timestamp_source"]


def test_benchmark_stdout_json_without_out(capsys):
    rc = cli.main(["benchmark", "--stack", "pipecat",
                   "--recordings", AUDIO_DIR])
    assert rc == 0
    data = json.loads(capsys.readouterr().out)
    assert data["kind"] == "stack-benchmark"
    assert data["stack"] == "pipecat"


def test_missing_recordings_listed_not_failed(tmp_path, capsys):
    rec = tmp_path / "rec"
    rec.mkdir()
    keep = ALL_IDS[:3]
    for sid in keep:
        shutil.copy(os.path.join(AUDIO_DIR, sid + ".example.wav"),
                    rec / (sid + ".wav"))
    out = tmp_path / "r.json"
    rc = cli.main(["benchmark", "--stack", "livekit",
                   "--recordings", str(rec), "--out", str(out)])
    assert rc == 0
    result = _load_json(out)

    assert result["scenarios"]["captured"] == 3
    assert result["scenarios"]["not_captured"] == ALL_IDS[3:]
    # Only captured scenarios are scored; missing ones never become events,
    # never fail, and never touch the summary.
    assert [e["scenario_id"] for e in result["events"]] == keep
    assert result["summary"]["events"] == 3
    assert result["summary"]["failed"] == 0
    assert result["summary"]["regression"] is False
    for e in result["events"]:
        assert not any("missing audio" in r for r in e["verdict"]["reasons"])
    # And they are stated plainly on stderr.
    err = capsys.readouterr().err
    assert "not captured" in err
    assert ALL_IDS[3] in err


def test_benchmark_custom_scenarios_dir(tmp_path):
    # A scenarios dir is a first-class input (corpus/suites/* shape): point the
    # benchmark at a subset dir and the suite label records the source.
    scen = tmp_path / "scenarios"
    scen.mkdir()
    for sid in ALL_IDS[:2]:
        shutil.copy(os.path.join(SCEN_DIR, sid + ".json"),
                    scen / (sid + ".json"))
    out = tmp_path / "r.json"
    rc = cli.main(["benchmark", "--stack", "twilio",
                   "--recordings", AUDIO_DIR,
                   "--scenarios", str(scen), "--out", str(out)])
    assert rc == 0
    result = _load_json(out)
    assert result["scenarios"]["total"] == 2
    assert result["suite"] == os.path.abspath(str(scen))
    assert result["provenance"]["scenario_source"] == os.path.abspath(str(scen))


def test_fail_on_regression_gates_exit_1(tmp_path):
    rec = tmp_path / "rec"
    rec.mkdir()
    # A hold render scored against the hard-interruption labels fails its
    # should-yield thresholds: a real scored failure, from real audio.
    shutil.copy(os.path.join(AUDIO_DIR, "02-backchannel-mhm.example.wav"),
                rec / "01-hard-interruption.wav")
    out = tmp_path / "r.json"
    base_args = ["benchmark", "--stack", "generic",
                 "--recordings", str(rec), "--out", str(out)]

    # Default: the benchmark measures, it does not gate.
    assert cli.main(base_args) == 0
    result = _load_json(out)
    assert result["summary"]["regression"] is True

    # Opt-in gate: exit 1 only with the flag AND a scored failure.
    assert cli.main(base_args + ["--fail-on-regression"]) == 1


def test_fail_on_regression_passing_run_exits_0(tmp_path):
    out = tmp_path / "r.json"
    rc = cli.main(["benchmark", "--stack", "generic",
                   "--recordings", AUDIO_DIR, "--out", str(out),
                   "--fail-on-regression"])
    assert rc == 0


# --- compare ----------------------------------------------------------------

def _mk_result(path, stack, rows):
    """Write a minimal, honest stack-benchmark result file.

    rows: list of (scenario_id, did_yield, seconds_to_yield, talk_over_sec).
    """
    events = [
        {
            "event_id": sid,
            "scenario_id": sid,
            "verdict": {
                "passed": True,
                "did_yield": dy,
                "seconds_to_yield": tty,
                "talk_over_sec": to,
                "reasons": [],
            },
        }
        for sid, dy, tty, to in rows
    ]
    data = {
        "tool": "hotato",
        "kind": "stack-benchmark",
        "schema_version": "1",
        "stack": stack,
        "suite": "barge-in",
        "summary": {"events": len(rows), "passed": len(rows), "failed": 0,
                    "regression": False},
        "scenarios": {"total": len(rows), "captured": len(rows),
                      "not_captured": []},
        "events": events,
    }
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(data, fh)
    return data


def test_compare_md_deltas_and_intersection(tmp_path):
    a = tmp_path / "a.json"
    b = tmp_path / "b.json"
    # Shared: s1 (full deltas) and s4 (B has no yield, so no time to yield and
    # no fabricated delta). Unshared: s2 only in A, s3 only in B.
    _mk_result(a, "vapi", [
        ("s1", True, 1.0, 0.5),
        ("s2", True, 0.4, 0.2),
        ("s4", True, 0.6, 0.3),
    ])
    _mk_result(b, "livekit", [
        ("s1", True, 0.25, 0.3),
        ("s3", True, 0.5, 0.1),
        ("s4", False, None, 0.2),
    ])
    out = tmp_path / "cmp.md"
    rc = cli.main(["benchmark", "compare", str(a), str(b),
                   "--out", str(out)])
    assert rc == 0
    md = out.read_text(encoding="utf-8")

    # Correct signed deltas vs the first input.
    assert "| `s1` | 0.500 | 0.300 | -0.200 |" in md       # talk-over
    assert "| `s1` | 1.000 | 0.250 | -0.750 |" in md       # time to yield
    # None is shown as '-', never invented.
    assert "| `s4` | 0.600 | - | - |" in md                # time to yield
    # Intersection only, and the rest is stated as skipped.
    assert "Compared: **2** scenario(s)" in md
    assert "`s2` (missing from b.json)" in md
    assert "`s3` (missing from a.json)" in md
    # Medians over the compared scenarios (A: [0.5, 0.3] -> 0.400; B: 0.250).
    assert "| talk-over median (s) | 0.400 (n=2) | 0.250 (n=2) | -0.150 |" in md
    # Measurements only: the header says so.
    assert "Nothing here ranks a vendor" in md


def test_compare_json_format(tmp_path):
    a = tmp_path / "a.json"
    b = tmp_path / "b.json"
    _mk_result(a, "vapi", [("s1", True, 1.0, 0.5)])
    _mk_result(b, "livekit", [("s1", True, 0.25, 0.3)])
    out = tmp_path / "cmp.json"
    rc = cli.main(["benchmark", "compare", str(a), str(b),
                   "--format", "json", "--out", str(out)])
    assert rc == 0
    cmp_env = _load_json(out)
    assert cmp_env["kind"] == "stack-benchmark-comparison"
    assert cmp_env["compared"] == ["s1"]
    assert cmp_env["skipped"] == []
    b_meas = cmp_env["per_scenario"][0]["measurements"][1]
    assert b_meas["delta_vs_first"]["talk_over_sec"] == -0.2
    assert b_meas["delta_vs_first"]["seconds_to_yield"] == -0.75


def test_compare_no_scenarios_in_common(tmp_path):
    a = tmp_path / "a.json"
    b = tmp_path / "b.json"
    _mk_result(a, "vapi", [("s1", True, 1.0, 0.5)])
    _mk_result(b, "livekit", [("s2", True, 0.25, 0.3)])
    out = tmp_path / "cmp.md"
    # Disjoint sets are stated plainly (exit 0), never padded into a table.
    assert cli.main(["benchmark", "compare", str(a), str(b),
                     "--out", str(out)]) == 0
    md = out.read_text(encoding="utf-8")
    assert "No scenarios in common" in md


def test_compare_three_files_real_run(tmp_path):
    # End to end over real scored results: three benchmark runs, compared.
    outs = []
    for i, stack in enumerate(("generic", "livekit", "vapi")):
        out = tmp_path / f"r{i}.json"
        assert cli.main(["benchmark", "--stack", stack,
                         "--recordings", AUDIO_DIR, "--out", str(out)]) == 0
        outs.append(str(out))
    cmp_out = tmp_path / "cmp.md"
    assert cli.main(["benchmark", "compare", *outs,
                     "--out", str(cmp_out)]) == 0
    md = cmp_out.read_text(encoding="utf-8")
    assert f"Compared: **{len(ALL_IDS)}** scenario(s)" in md
    # Identical recordings measure identically: every delta is +0.000 or '-'.
    assert "delta B-A" in md and "delta C-A" in md
    assert "-0." not in md.replace("| -0.000 |", "")


# --- usage errors -----------------------------------------------------------

def test_benchmark_without_inputs_exits_2():
    assert cli.main(["benchmark"]) == 2


def test_benchmark_missing_recordings_dir_exits_2(tmp_path):
    assert cli.main(["benchmark", "--stack", "vapi",
                     "--recordings", str(tmp_path / "nope")]) == 2


def test_benchmark_no_matching_recordings_exits_2(tmp_path):
    empty = tmp_path / "empty"
    empty.mkdir()
    assert cli.main(["benchmark", "--stack", "vapi",
                     "--recordings", str(empty)]) == 2


def test_compare_single_file_exits_2(tmp_path):
    a = tmp_path / "a.json"
    _mk_result(a, "vapi", [("s1", True, 1.0, 0.5)])
    assert cli.main(["benchmark", "compare", str(a)]) == 2


def test_compare_rejects_non_benchmark_json(tmp_path):
    a = tmp_path / "a.json"
    _mk_result(a, "vapi", [("s1", True, 1.0, 0.5)])
    bad = tmp_path / "bad.json"
    bad.write_text(json.dumps({"tool": "hotato", "kind": "frame-dump"}),
                   encoding="utf-8")
    assert cli.main(["benchmark", "compare", str(a), str(bad)]) == 2


def test_compare_missing_file_exits_2(tmp_path):
    a = tmp_path / "a.json"
    _mk_result(a, "vapi", [("s1", True, 1.0, 0.5)])
    assert cli.main(["benchmark", "compare", str(a),
                     str(tmp_path / "gone.json")]) == 2
