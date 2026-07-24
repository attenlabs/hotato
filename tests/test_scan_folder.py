"""``hotato scan <directory>``: the folder health report.

Pinned here:

  * BACK-COMPAT: every existing ``hotato scan --stereo`` invocation form is
    byte-identical to the untouched library path (``scan.scan_recording`` +
    ``scan.render_text``), text and json, and the --out side file still
    lands; a directory positional plus --stereo is a usage error, and bare
    ``hotato scan`` stays a usage error (exit 2);
  * the aggregate mode runs the autopsy engine over every recording in the
    folder: the HEALTH headline is the measured share ("N of M calls had no
    critical incidents (X%)" -- the arithmetic is checked against the
    per-call criticals, never a blended 0-100 score), refused files are
    listed with their reason (never skipped silently), the worst-calls
    ranking links to per-call autopsy reports generated alongside, and the
    per-call envelopes land so ``hotato pin`` resolves right away;
  * byte-determinism x2: the same directory + the same flags produce
    byte-identical CLI text, a byte-identical HTML report, and a
    byte-identical summary envelope (content-addressed naming, and the
    stored envelope -- including its recorded_at provenance -- is left
    untouched on a re-run of unchanged content);
  * TREND: a stored prior envelope for the same directory renders the
    run-over-run strip (its recorded_at provenance on the page), and the
    current run's own envelope never lists itself as a prior run;
  * est. cost totals render ONLY under --cost-config;
  * a directory with no recordings is a usage error (exit 2).
"""

import json
import os
import re
import shutil
from importlib import resources

from hotato import cli
from hotato import scan as scan_mod
from hotato import scanfolder as scanfolder_mod

EXAMPLES = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "examples", "autopsy", "audio",
)


def _example(name: str) -> str:
    return os.path.join(EXAMPLES, name + ".example.wav")


def _bundled(sid: str) -> str:
    return str(
        resources.files("hotato").joinpath("data", "audio", sid + ".example.wav")
    )


def _make_calls_dir(tmp_path):
    """Four analyzable calls (one clean, three with a critical incident each)
    plus one unreadable file: the health share is 1 of 4 (25%)."""
    calls = tmp_path / "calls"
    calls.mkdir()
    for name in ("autopsy-01-barge-in-say-do", "autopsy-02-latency-dead-air",
                 "autopsy-03-talk-over"):
        shutil.copy(_example(name), calls / (name + ".example.wav"))
    shutil.copy(_bundled("01-hard-interruption"),
                calls / "01-hard-interruption.example.wav")
    (calls / "broken.wav").write_text("this is not a wav file")
    return calls


# --- back-compat: the existing --stereo forms are byte-identical -------------

def test_stereo_text_output_is_byte_identical_to_the_library_path(capsys):
    src = _example("autopsy-01-barge-in-say-do")
    assert cli.main(["scan", "--stereo", src]) == 0
    out = capsys.readouterr().out
    expected = scan_mod.render_text(
        scan_mod.scan_recording(src, min_gap_sec=2.0), top=20) + "\n"
    assert out == expected


def test_stereo_json_and_out_forms_keep_their_shape(tmp_path, capsys):
    src = _example("autopsy-03-talk-over")
    out_file = tmp_path / "candidates.json"
    assert cli.main(["scan", "--stereo", src, "--format", "json",
                     "--out", str(out_file), "--top", "1"]) == 0
    captured = capsys.readouterr()
    doc = json.loads(captured.out)
    assert doc["kind"] == "scan"
    assert doc["shown"] == len(doc["candidates"]) <= 1
    side = json.loads(out_file.read_text(encoding="utf-8"))
    # the file gets EVERY candidate; --top caps only the stdout listing
    assert side["total_candidates"] == len(side["candidates"])


def test_no_arguments_is_still_a_usage_error(capsys):
    assert cli.main(["scan"]) == 2
    err = capsys.readouterr().err
    assert "error:" in err
    assert "--stereo" in err


def test_directory_plus_stereo_is_a_usage_error(tmp_path, capsys):
    calls = tmp_path / "calls"
    calls.mkdir()
    assert cli.main(["scan", str(calls), "--stereo",
                     _example("autopsy-03-talk-over")]) == 2
    assert "not both" in capsys.readouterr().err


def test_folder_mode_refuses_single_mode_only_flags(tmp_path, capsys):
    calls = tmp_path / "calls"
    calls.mkdir()
    shutil.copy(_example("autopsy-03-talk-over"), calls / "a.wav")
    assert cli.main(["scan", str(calls), "--out", "x.json"]) == 2
    assert "--out" in capsys.readouterr().err
    assert cli.main(["scan", str(calls), "--caller-channel", "1",
                     "--agent-channel", "0"]) == 2
    assert "--caller-channel" in capsys.readouterr().err
    # a bad --min-gap is one usage mistake, never a per-file refusal row
    assert cli.main(["scan", str(calls), "--min-gap", "0"]) == 2
    assert "--min-gap must be > 0" in capsys.readouterr().err


def test_cost_config_on_stereo_mode_is_a_usage_error(tmp_path, capsys):
    cfg = tmp_path / "costs.json"
    cfg.write_text(json.dumps({"per_incident": {"dead-air": 1.0}}))
    assert cli.main(["scan", "--stereo", _example("autopsy-03-talk-over"),
                     "--cost-config", str(cfg)]) == 2
    assert "hotato autopsy" in capsys.readouterr().err


# --- the aggregate mode -------------------------------------------------------

def test_folder_health_share_arithmetic_and_refused_visibility(
        tmp_path, monkeypatch, capsys):
    calls = _make_calls_dir(tmp_path)
    monkeypatch.chdir(tmp_path)
    assert cli.main(["scan", str(calls)]) == 0
    out = capsys.readouterr().out
    assert "5 recordings: 4 analyzed, 1 refused" in out
    # The measured share: exactly one of the four analyzed calls (the
    # yielding call) has zero critical incidents.
    assert "health: 1 of 4 calls had no critical incidents (25%)" in out
    # never a blended 0-100 quality score
    assert re.search(r"\b\d+\s*/\s*100\b", out) is None
    assert "never a blended quality score" in out
    # the refused file is visible with its reason, not silently skipped
    assert "broken.wav" in out
    assert "not a readable PCM WAV" in out
    # per-category breakdown with worst measured magnitudes
    assert "BARGE-IN" in out and "TALK-OVER" in out and "DEAD AIR" in out
    assert re.search(r"worst \d+\.\d\ds (overlap|gap)", out)
    # the artifacts landed where the output says
    m = re.search(r"report:\s+(\S+\.html)", out)
    assert m and os.path.isfile(m.group(1))
    m = re.search(r"envelope:\s+(\S+\.json)", out)
    assert m and os.path.isfile(m.group(1))


def test_folder_generates_per_call_reports_and_envelopes(
        tmp_path, monkeypatch, capsys):
    calls = _make_calls_dir(tmp_path)
    monkeypatch.chdir(tmp_path)
    assert cli.main(["scan", str(calls), "--format", "json"]) == 0
    result = json.loads(capsys.readouterr().out)
    assert result["kind"] == "scan-folder"
    assert result["counts"] == {"scanned": 5, "analyzed": 4, "refused": 1}
    assert result["health"]["calls_no_critical"] == 1
    assert result["health"]["calls_analyzed"] == 4
    # worst-first: critical count, then worst measured magnitude
    crits = [c["critical"] for c in result["calls"]]
    assert crits == sorted(crits, reverse=True)
    for c in result["calls"]:
        # every ranked call links to its own generated autopsy report, and
        # its envelope is in place for `hotato pin`
        assert os.path.isfile(c["report_path"])
        env_path = os.path.join("hotato-output",
                                f"autopsy-{c['autopsy_id']}.json")
        assert os.path.isfile(env_path)
        env = json.loads(open(env_path, encoding="utf-8").read())
        assert env["kind"] == "autopsy"
        assert env["id"] == c["autopsy_id"]
    # the scan report links each worst call by relative href
    html = open(result["report_path"], encoding="utf-8").read()
    for c in result["calls"]:
        assert f'href="{os.path.basename(c["report_path"])}"' in html
    assert "broken.wav" in html


def test_folder_is_byte_deterministic_across_two_runs(
        tmp_path, monkeypatch, capsys):
    calls = _make_calls_dir(tmp_path)
    monkeypatch.chdir(tmp_path)
    assert cli.main(["scan", str(calls)]) == 0
    out1 = capsys.readouterr().out
    report = re.search(r"report:\s+(\S+\.html)", out1).group(1)
    envelope = re.search(r"envelope:\s+(\S+\.json)", out1).group(1)
    html1 = open(report, "rb").read()
    env1 = open(envelope, "rb").read()
    assert cli.main(["scan", str(calls)]) == 0
    out2 = capsys.readouterr().out
    assert out1 == out2
    assert open(report, "rb").read() == html1
    # the content-addressed envelope (recorded_at provenance included) is
    # left untouched on a re-run of unchanged content
    assert open(envelope, "rb").read() == env1


def test_scan_report_html_is_self_contained_and_dollar_free(
        tmp_path, monkeypatch, capsys):
    calls = _make_calls_dir(tmp_path)
    monkeypatch.chdir(tmp_path)
    assert cli.main(["scan", str(calls)]) == 0
    out = capsys.readouterr().out
    report = re.search(r"report:\s+(\S+\.html)", out).group(1)
    html = open(report, encoding="utf-8").read()
    assert re.search(r'(src|href)\s*=\s*["\']https?://', html) is None
    assert "<script" not in html
    # no cost config: no dollar figure anywhere
    assert "est. cost" not in html
    assert "$" not in html
    assert "$" not in out


def test_cost_totals_render_only_under_cost_config(
        tmp_path, monkeypatch, capsys):
    calls = _make_calls_dir(tmp_path)
    cfg = tmp_path / "costs.json"
    cfg.write_text(json.dumps(
        {"currency": "USD", "per_incident": {"barge-in": 2.0,
                                             "talk-over": 2.0}}))
    monkeypatch.chdir(tmp_path)
    assert cli.main(["scan", str(calls), "--cost-config", str(cfg)]) == 0
    out = capsys.readouterr().out
    # 2 barge-ins (incl. the yielding call's warning) + 1 talk-over at $2
    assert "est. cost total: $6.00 (3 priced incidents" in out
    report = re.search(r"report:\s+(\S+\.html)", out).group(1)
    assert "est. cost total: $6.00" in open(report, encoding="utf-8").read()
    # the stored envelopes stay cost-free: measured facts only
    envelope = re.search(r"envelope:\s+(\S+\.json)", out).group(1)
    env_text = open(envelope, encoding="utf-8").read()
    assert '"est_cost"' not in env_text and "$" not in env_text


def test_trend_strip_renders_from_a_stored_prior_envelope(
        tmp_path, monkeypatch, capsys):
    calls = _make_calls_dir(tmp_path)
    monkeypatch.chdir(tmp_path)
    assert cli.main(["scan", str(calls)]) == 0
    out1 = capsys.readouterr().out
    assert "trend:" not in out1  # first run: no prior envelopes
    envelope = re.search(r"envelope:\s+(\S+\.json)", out1).group(1)
    report = re.search(r"report:\s+(\S+\.html)", out1).group(1)
    current = json.loads(open(envelope, encoding="utf-8").read())

    # a prior run of the SAME directory (same dir_key, different content id),
    # stored with its own recorded_at provenance
    prior = dict(current)
    prior["id"] = "scn-aaaaaaaaaaaa"
    prior["recorded_at"] = "2026-07-20T09:00:00Z"
    prior["counts"] = {"scanned": 5, "analyzed": 5, "refused": 0}
    prior["health"] = {"calls_no_critical": 1, "calls_analyzed": 5,
                       "share": 0.2,
                       "headline": "1 of 5 calls had no critical incidents (20%)"}
    prior["incidents"] = {"critical": 6, "warning": 2}
    with open(os.path.join("hotato-output", "scan-scn-aaaaaaaaaaaa.json"),
              "w", encoding="utf-8") as fh:
        json.dump(prior, fh, indent=2)

    assert cli.main(["scan", str(calls)]) == 0
    out2 = capsys.readouterr().out
    assert "trend: 1 prior run of this directory" in out2
    html = open(report, encoding="utf-8").read()
    # the prior run's provenance timestamp and share, plus the current row
    assert "2026-07-20T09:00:00Z" in html
    assert "1 of 5 calls with no critical incidents" in html
    assert "this run" in html
    # deterministic given the same directory + the same prior-run store
    assert cli.main(["scan", str(calls)]) == 0
    assert capsys.readouterr().out == out2
    assert open(report, encoding="utf-8").read() == html


def test_current_run_never_lists_itself_as_a_prior_run(
        tmp_path, monkeypatch, capsys):
    calls = _make_calls_dir(tmp_path)
    monkeypatch.chdir(tmp_path)
    assert cli.main(["scan", str(calls), "--format", "json"]) == 0
    first = json.loads(capsys.readouterr().out)
    assert first["prior_runs"] == []
    # the first run stored its envelope; the second run of the same content
    # resolves to the same id and must not read it back as a prior run
    assert cli.main(["scan", str(calls), "--format", "json"]) == 0
    second = json.loads(capsys.readouterr().out)
    assert second["prior_runs"] == []


def test_a_directory_with_no_recordings_is_refused(tmp_path, capsys):
    empty = tmp_path / "empty"
    empty.mkdir()
    assert cli.main(["scan", str(empty)]) == 2
    assert "no call recordings" in capsys.readouterr().err


def test_mono_calls_are_analyzed_best_effort_not_refused(
        tmp_path, monkeypatch, capsys):
    # a mono recording participates in the aggregate per autopsy's rules
    # (best-effort silence timing), with its mode stated per call
    import math
    import random
    import struct
    import wave

    calls = tmp_path / "calls"
    calls.mkdir()
    rng = random.Random(7)
    sr = 16000
    n = int(14.0 * sr)
    active = [False] * n
    for a, b in ((0.5, 2.0), (8.5, 10.0), (12.0, 13.5)):
        for i in range(int(a * sr), min(n, int(b * sr))):
            active[i] = True
    frames = []
    for i in range(n):
        if active[i]:
            v = 0.4 * math.sin(2 * math.pi * 220 * i / sr) + 0.1 * rng.uniform(-1, 1)
        else:
            v = 0.0005 * rng.uniform(-1, 1)
        frames.append(int(max(-1.0, min(1.0, v)) * 32767))
    with wave.open(str(calls / "mono-call.wav"), "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(sr)
        wf.writeframes(struct.pack("<%dh" % len(frames), *frames))
    shutil.copy(_bundled("01-hard-interruption"), calls / "clean.wav")
    monkeypatch.chdir(tmp_path)
    assert cli.main(["scan", str(calls), "--format", "json"]) == 0
    result = json.loads(capsys.readouterr().out)
    assert result["counts"] == {"scanned": 2, "analyzed": 2, "refused": 0}
    modes = {c["source"]: c["mode"] for c in result["calls"]}
    assert modes == {"mono-call.wav": "mono", "clean.wav": "stereo"}


def test_scan_id_is_content_derived(tmp_path):
    calls = tmp_path / "calls"
    calls.mkdir()
    shutil.copy(_example("autopsy-03-talk-over"), calls / "a.wav")
    result1, _ = scanfolder_mod.run_scan_folder(str(calls))
    # same content, same flags -> same id; changed content -> a new id
    result2, _ = scanfolder_mod.run_scan_folder(str(calls))
    assert result1["id"] == result2["id"]
    shutil.copy(_example("autopsy-02-latency-dead-air"), calls / "b.wav")
    result3, _ = scanfolder_mod.run_scan_folder(str(calls))
    assert result3["id"] != result1["id"]
    assert result3["dir_key"] == result1["dir_key"]
