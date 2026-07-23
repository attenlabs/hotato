"""``hotato autopsy <recording>``: the zero-config forensics front door.

Pinned here, over the three deterministic rendered example calls in
``examples/autopsy/`` (rendered by conftest when absent, seed = sha256(id)):

  * stereo end to end: the existing deterministic scan path finds the
    rendered incident in each example (barge-in, dead air, talk-over), and
    two runs on the same file produce byte-identical CLI text AND a
    byte-identical HTML report at a content-addressed path;
  * mono end to end: best-effort silence-timing findings, each with a
    measured confidence and its derivation, the one-line functional scope
    stated, and no talk-over/barge-in attribution anywhere;
  * an unreadable input is refused with the reason (exit 2), never scored;
  * no arguments prints the usage + quick start (exit 0);
  * est. cost renders ONLY with --cost-config: with no config there is no
    cost line and no dollar figure on any surface; a malformed config is a
    clean usage error;
  * the HTML report is self-contained: no external src/href anywhere;
  * the autopsy id is content-derived (apx- + 12 hex of sha256(file bytes)):
    the same bytes get the same id whatever the file is named -- the id shape
    a later ``hotato pin <id>`` resolves;
  * an mp3 with no ffmpeg on PATH is refused with the one-line actionable
    message, never a traceback.
"""

import json
import math
import os
import random
import re
import struct
import wave

import pytest

from hotato import autopsy as autopsy_mod
from hotato import cli

EXAMPLES = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "examples", "autopsy", "audio",
)


def _example(name: str) -> str:
    return os.path.join(EXAMPLES, name + ".example.wav")


def _write_mono_wav(path: str, active_spans, duration_sec=14.0, sr=16000):
    """A deterministic one-channel WAV: a 220 Hz tone plus seeded noise inside
    each active span, near-silence elsewhere."""
    rng = random.Random(7)
    n = int(duration_sec * sr)
    active = [False] * n
    for a, b in active_spans:
        for i in range(int(a * sr), min(n, int(b * sr))):
            active[i] = True
    frames = []
    for i in range(n):
        if active[i]:
            v = 0.4 * math.sin(2 * math.pi * 220 * i / sr) + 0.1 * rng.uniform(-1, 1)
        else:
            v = 0.0005 * rng.uniform(-1, 1)
        frames.append(int(max(-1.0, min(1.0, v)) * 32767))
    with wave.open(path, "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(sr)
        wf.writeframes(struct.pack("<%dh" % len(frames), *frames))


# --- stereo end to end: incidents found, deterministic byte for byte --------

def test_stereo_barge_in_example_end_to_end(tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    assert cli.main(["autopsy", _example("autopsy-01-barge-in-say-do")]) == 0
    out = capsys.readouterr().out
    assert "2 channels, stereo" in out
    assert "[CRITICAL] BARGE-IN" in out
    assert "overlap=" in out
    # the scanner's plain-English sentence rides along
    assert "the caller took the floor" in out
    # the report landed at the content-addressed path the output names
    m = re.search(r"report: (\S+\.html)", out)
    assert m and os.path.isfile(m.group(1))
    # the pin hint prints the content-derived autopsy id
    assert re.search(r"pin: apx-[0-9a-f]{12}", out)


def test_stereo_dead_air_and_talk_over_examples(tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    assert cli.main(["autopsy", _example("autopsy-02-latency-dead-air")]) == 0
    out = capsys.readouterr().out
    assert "[CRITICAL] DEAD AIR" in out and "gap=" in out
    assert cli.main(["autopsy", _example("autopsy-03-talk-over")]) == 0
    out = capsys.readouterr().out
    assert "[CRITICAL] TALK-OVER" in out and "overlap=" in out


def test_stereo_is_deterministic_byte_for_byte(tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    src = _example("autopsy-01-barge-in-say-do")
    assert cli.main(["autopsy", src]) == 0
    out1 = capsys.readouterr().out
    report = re.search(r"report: (\S+\.html)", out1).group(1)
    html1 = open(report, "rb").read()
    os.remove(report)
    assert cli.main(["autopsy", src]) == 0
    out2 = capsys.readouterr().out
    html2 = open(report, "rb").read()
    assert out1 == out2
    assert html1 == html2


def test_autopsy_id_is_content_derived(tmp_path):
    src = _example("autopsy-03-talk-over")
    copy = tmp_path / "renamed-call.wav"
    copy.write_bytes(open(src, "rb").read())
    a = autopsy_mod.autopsy_id(src)
    b = autopsy_mod.autopsy_id(str(copy))
    assert a == b
    assert re.fullmatch(r"apx-[0-9a-f]{12}", a)


# --- mono end to end: measured confidence, stated scope, no attribution -----

def test_mono_best_effort_confidence_and_scope(tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    mono = tmp_path / "mono.wav"
    _write_mono_wav(str(mono), [(0.5, 2.0), (8.5, 10.0), (12.0, 13.5)])
    assert cli.main(["autopsy", str(mono)]) == 0
    out = capsys.readouterr().out
    assert "1 channel, mono" in out
    # the one-line functional scope is stated
    assert autopsy_mod.MONO_SCOPE_NOTE in out
    # the dead-air finding carries a measured confidence and its derivation
    assert "DEAD AIR" in out
    assert re.search(r"confidence 0\.\d\d \(measured:", out)
    assert "dB below the speech-activity threshold" in out
    # no fabricated attribution: mono never claims talk-over or barge-in
    assert "BARGE-IN" not in out
    assert "TALK-OVER" not in out


def test_mono_json_findings_carry_confidence(tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    mono = tmp_path / "mono.wav"
    _write_mono_wav(str(mono), [(0.5, 2.0), (8.5, 10.0)])
    assert cli.main(["autopsy", str(mono), "--format", "json"]) == 0
    result = json.loads(capsys.readouterr().out)
    assert result["kind"] == "autopsy"
    assert result["mode"] == "mono"
    assert result["note"] == autopsy_mod.MONO_SCOPE_NOTE
    assert result["total_incidents"] >= 1
    for inc in result["incidents"]:
        assert 0.0 <= inc["confidence"] <= 1.0
        assert inc["confidence_basis"].startswith("measured:")
        assert inc["kind_key"] in ("dead-air", "latency-spike")


# --- refusal: garbage in, reason out, exit 2 --------------------------------

def test_garbage_input_is_refused_with_reason(tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    bad = tmp_path / "not-audio.wav"
    bad.write_text("this is not a wav file")
    assert cli.main(["autopsy", str(bad)]) == 2
    err = capsys.readouterr().err
    assert "error:" in err
    assert "not a readable PCM WAV" in err


def test_missing_file_is_refused(tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    assert cli.main(["autopsy", str(tmp_path / "absent.wav")]) == 2
    assert "no such file" in capsys.readouterr().err


def test_mp3_without_ffmpeg_gets_the_actionable_message(
        tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    fake = tmp_path / "call.mp3"
    fake.write_bytes(b"\xff\xfb\x90\x00" + b"\x00" * 64)
    monkeypatch.setattr(autopsy_mod.shutil, "which", lambda name: None)
    assert cli.main(["autopsy", str(fake)]) == 2
    err = capsys.readouterr().err
    assert "ffmpeg" in err
    assert "install ffmpeg" in err


# --- no args: usage + quick start -------------------------------------------

def test_no_args_prints_usage_and_quick_start(capsys):
    assert cli.main(["autopsy"]) == 0
    out = capsys.readouterr().out
    assert out.startswith("usage: hotato autopsy")
    assert "Quick start: hotato autopsy examples/" in out


# --- est. cost: only with a config, never a default -------------------------

def test_no_cost_config_means_no_dollar_figure_anywhere(
        tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    assert cli.main(["autopsy", _example("autopsy-02-latency-dead-air")]) == 0
    out = capsys.readouterr().out
    assert "est. cost" not in out
    assert "$" not in out
    report = re.search(r"report: (\S+\.html)", out).group(1)
    html = open(report, encoding="utf-8").read()
    assert "est. cost" not in html
    assert "$" not in html


def test_cost_config_renders_est_cost_lines(tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    cfg = tmp_path / "costs.json"
    cfg.write_text(json.dumps(
        {"currency": "USD", "per_incident": {"dead-air": 3.5}}))
    assert cli.main(["autopsy", _example("autopsy-02-latency-dead-air"),
                     "--cost-config", str(cfg)]) == 0
    out = capsys.readouterr().out
    assert "est. cost: $3.50 (your figure for dead-air)" in out
    assert "est. cost total: $3.50" in out
    report = re.search(r"report: (\S+\.html)", out).group(1)
    html = open(report, encoding="utf-8").read()
    assert "est. cost" in html and "$3.50" in html


def test_malformed_cost_config_is_a_usage_error(tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    cfg = tmp_path / "costs.json"
    cfg.write_text(json.dumps({"per_incident": {"no-such-kind": 1.0}}))
    assert cli.main(["autopsy", _example("autopsy-03-talk-over"),
                     "--cost-config", str(cfg)]) == 2
    assert "unknown incident kind" in capsys.readouterr().err
    cfg.write_text("not json")
    assert cli.main(["autopsy", _example("autopsy-03-talk-over"),
                     "--cost-config", str(cfg)]) == 2
    assert "not readable JSON" in capsys.readouterr().err


# --- the HTML report is self-contained --------------------------------------

@pytest.mark.parametrize("example", [
    "autopsy-01-barge-in-say-do",
    "autopsy-02-latency-dead-air",
    "autopsy-03-talk-over",
])
def test_report_html_is_self_contained(example, tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    assert cli.main(["autopsy", _example(example)]) == 0
    out = capsys.readouterr().out
    report = re.search(r"report: (\S+\.html)", out).group(1)
    html = open(report, encoding="utf-8").read()
    # zero external requests: no src/href pointing off this file
    assert re.search(r'(src|href)\s*=\s*["\']https?://', html) is None
    assert "<script" not in html
    # the same measured numbers the CLI printed are on the page
    assert "hotato autopsy" in html
    assert re.search(r"apx-[0-9a-f]{12}#1", html)


def test_report_json_shape_and_incident_ids(tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    assert cli.main(["autopsy", _example("autopsy-01-barge-in-say-do"),
                     "--format", "json"]) == 0
    result = json.loads(capsys.readouterr().out)
    assert result["tool"] == "hotato"
    assert result["kind"] == "autopsy"
    assert result["schema_version"] == "1"
    assert re.fullmatch(r"apx-[0-9a-f]{12}", result["id"])
    assert result["mode"] == "stereo"
    assert result["cost"] is None
    for i, inc in enumerate(result["incidents"], 1):
        assert inc["rank"] == i
        assert inc["id"] == f"{result['id']}#{i}"
        assert inc["severity"] in ("CRITICAL", "WARNING")
        assert inc["kind_key"] in autopsy_mod.COST_KIND_KEYS
    assert result["report_path"] == os.path.join(
        "hotato-output", f"autopsy-{result['id']}.html")
