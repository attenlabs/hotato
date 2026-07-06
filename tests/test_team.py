"""P8: team mode. Aggregation math is pinned on synthetic envelopes with known
values (mean/median/p90 are hand-checkable), ordering follows mtime or filename,
fewer than 2 runs is stated plainly with exit 0, and the HTML page stays
self-contained and honest.
"""

import json
import os

import pytest

from hotato import aggregate, cli


def _env(passed, tovs, ttys, fix_class=None):
    events = []
    for i, p in enumerate(passed):
        e = {
            "event_id": f"e{i}",
            "scenario_id": f"e{i}",
            "expected_yield": True,
            "verdict": {
                "passed": p,
                "did_yield": ttys[i] is not None,
                "seconds_to_yield": ttys[i],
                "talk_over_sec": tovs[i],
                "reasons": [] if p else ["expected the agent to yield"],
            },
            "measurements": {},
            "signals": {},
            "fix": None,
        }
        if not p:
            e["fix"] = {"fix_class": fix_class or "engagement-control",
                        "title": "t", "detail": "", "knob": None, "pointer": None}
        events.append(e)
    n_pass = sum(1 for p in passed if p)
    return {
        "tool": "hotato",
        "schema_version": "1",
        "mode": "suite",
        "stack": "generic",
        "offline": True,
        "events": events,
        "summary": {"events": len(events), "passed": n_pass,
                    "failed": len(events) - n_pass,
                    "regression": n_pass < len(events)},
        "exit_code": 0 if n_pass == len(events) else 1,
    }


def _write_runs(dirpath):
    """Three synthetic runs. mtime order: 001 (oldest, all pass), 003, 002."""
    runs = {
        # file name -> (env, mtime)
        "001.json": (_env([True, True], [0.3, 0.4], [0.6, 0.7]), 1000),
        "003.json": (_env([True, False], [0.1, 0.2], [0.5, None]), 2000),
        "002.json": (_env([False, True], [0.5, 0.6], [None, 0.8]), 3000),
    }
    for name, (env, mtime) in runs.items():
        p = os.path.join(str(dirpath), name)
        with open(p, "w", encoding="utf-8") as fh:
            json.dump(env, fh)
        os.utime(p, (mtime, mtime))


def test_aggregate_math_on_synthetic_envelopes(tmp_path):
    _write_runs(tmp_path)
    loaded = aggregate.load_run_dir(str(tmp_path), order="mtime")
    agg = aggregate.aggregate_runs(loaded["runs"], order="mtime")

    assert agg["runs"] == 3
    assert agg["events_total"] == 6

    # pooled talk-over: [0.1..0.6] -> mean 0.35, median 0.35,
    # p90 (linear interpolation, pos = 0.9*5 = 4.5) = 0.5 + 0.5*0.1 = 0.55
    d = agg["talk_over_sec"]
    assert d["n"] == 6
    assert d["mean"] == 0.35
    assert d["median"] == 0.35
    assert d["p90"] == 0.55
    assert d["min"] == 0.1 and d["max"] == 0.6

    # pooled time-to-yield over measured yields only: [0.5,0.6,0.7,0.8]
    # p90 pos = 0.9*3 = 2.7 -> 0.7 + 0.7*0.1 = 0.77
    d = agg["seconds_to_yield"]
    assert d["n"] == 4
    assert d["mean"] == 0.65
    assert d["median"] == 0.65
    assert d["p90"] == 0.77

    # pass rate over time, mtime order: 1.0, 0.5, 0.5 -> trend down
    rates = [p["pass_rate"] for p in agg["pass_rate_over_time"]]
    assert rates == [1.0, 0.5, 0.5]
    assert agg["pass_rate"]["first"] == 1.0
    assert agg["pass_rate"]["latest"] == 0.5
    assert agg["pass_rate"]["direction"] == "down"

    # both failures carry the same class -> it is the most common one
    assert agg["most_common_failure_class"] == {
        "fix_class": "engagement-control", "count": 2, "of_failures": 2}


def test_order_by_name_uses_filename_as_explicit_index(tmp_path):
    _write_runs(tmp_path)
    loaded = aggregate.load_run_dir(str(tmp_path), order="name")
    files = [r["file"] for r in loaded["runs"]]
    assert files == ["001.json", "002.json", "003.json"]
    loaded_m = aggregate.load_run_dir(str(tmp_path), order="mtime")
    assert [r["file"] for r in loaded_m["runs"]] == ["001.json", "003.json", "002.json"]


def test_non_envelope_jsons_are_skipped_not_guessed(tmp_path):
    _write_runs(tmp_path)
    (tmp_path / "notes.json").write_text('{"hello": 1}', encoding="utf-8")
    (tmp_path / "dump.json").write_text(
        '{"tool": "hotato", "kind": "frame-dump", "frames": []}', encoding="utf-8")
    (tmp_path / "broken.json").write_text("{nope", encoding="utf-8")
    loaded = aggregate.load_run_dir(str(tmp_path))
    assert len(loaded["runs"]) == 3
    assert sorted(s["file"] for s in loaded["skipped"]) == [
        "broken.json", "dump.json", "notes.json"]


def test_team_cli_prints_aggregates(tmp_path, capsys):
    _write_runs(tmp_path)
    code = cli.main(["team", str(tmp_path)])
    assert code == 0
    out = capsys.readouterr().out
    assert "3 runs" in out
    assert "talk-over: mean 0.35s median 0.35s p90 0.55s (n=6)" in out
    assert "time to yield: mean 0.65s median 0.65s p90 0.77s (n=4)" in out
    assert "most common failure class: engagement-control (2 of 2 failures)" in out
    assert "trend: 1.00 to 0.50 (down)" in out


def test_team_cli_fewer_than_two_runs_says_so_and_exits_zero(tmp_path, capsys):
    code = cli.main(["team", str(tmp_path)])
    assert code == 0
    assert "needs at least 2 run envelopes" in capsys.readouterr().out

    # one run: still stated plainly, still exit 0
    env = _env([True], [0.1], [0.5])
    (tmp_path / "only.json").write_text(json.dumps(env), encoding="utf-8")
    code = cli.main(["team", str(tmp_path)])
    assert code == 0
    assert "found 1" in capsys.readouterr().out


def test_team_cli_missing_dir_is_usage_error(tmp_path):
    assert cli.main(["team", str(tmp_path / "nope")]) == 2


def test_team_cli_json_and_out_file(tmp_path, capsys):
    _write_runs(tmp_path)
    out = tmp_path / "agg.json"
    code = cli.main(["team", str(tmp_path), "--format", "json",
                     "--out", str(out)])
    assert code == 0
    agg_stdout = json.loads(capsys.readouterr().out)
    agg_file = json.loads(out.read_text(encoding="utf-8"))
    assert agg_stdout == agg_file
    assert agg_file["kind"] == "team-aggregate"
    assert agg_file["exit_code"] == 0


def test_team_html_page_self_contained_with_trend(tmp_path):
    _write_runs(tmp_path)
    html_path = tmp_path / "team.html"
    code = cli.main(["team", str(tmp_path), "--html", str(html_path)])
    assert code == 0
    html = html_path.read_text(encoding="utf-8")
    assert html.startswith("<!doctype html>")
    assert '<svg class="trend-svg"' in html
    assert "<polyline" in html
    assert "Team aggregate" in html
    # honesty + self-containment rules, same as the report
    assert "%" not in html
    assert "http://" not in html and "https://" not in html
    assert "<script" not in html and "xmlns" not in html
    assert "–" not in html and "—" not in html
    assert "No accuracy score" in html


def test_aggregate_refuses_fewer_than_two_runs():
    with pytest.raises(ValueError):
        aggregate.aggregate_runs([], order="mtime")
