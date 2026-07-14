"""Not-scorable rendering in the HTML and Markdown REPORTS, from an external
release review.

The envelope semantics (scorable: false, not_scorable_reason, summary counts,
process exit mapping) are pinned in test_not_scorable.py and the CLI text
paths in test_not_scorable_rendering.py. This file pins the report renderers:

  * a not-scorable event gets a NOT SCORABLE chip / verdict cell with its
    reason, never PASS or FAIL;
  * the overall verdict is REGRESSION when any scorable event failed, else
    NOT SCORABLE when any input could not be judged, else ALL PASS;
  * not-scorable events are excluded from the failure clusters and from the
    time-to-yield / talk-over distributions, and get their own
    "Not scorable inputs" section (id + reason), never "Failures and fixes";
  * the summary counts line gains not_scorable=N when N > 0, mirroring the
    CLI text;
  * fully-scorable reports render exactly as before: no NOT SCORABLE strings
    anywhere and the ALL PASS verdict unchanged.
"""

import math
import struct
import wave

import pytest

from hotato import cli, core, report
from hotato._engine.score import ScoreConfig

# --- deterministic synthetic fixtures (same shape as test_not_scorable.py) ---

def _write_stereo(path, caller_segments, agent_segments, duration_sec=3.0, sr=16000):
    """Two-channel PCM WAV: caller on channel 0, agent on channel 1."""
    n = int(duration_sec * sr)

    def _on(segments, t):
        return any(start <= t < end for start, end in segments)

    frames = bytearray()
    for i in range(n):
        t = i / sr
        c = int(0.35 * 32767 * math.sin(2 * math.pi * 220.0 * i / sr)) if _on(caller_segments, t) else 0
        a = int(0.35 * 32767 * math.sin(2 * math.pi * 330.0 * i / sr)) if _on(agent_segments, t) else 0
        frames += struct.pack("<hh", c, a)
    with wave.open(str(path), "wb") as w:
        w.setnchannels(2)
        w.setsampwidth(2)
        w.setframerate(sr)
        w.writeframes(bytes(frames))
    return str(path)


@pytest.fixture()
def silent_caller_wav(tmp_path):
    # The agent talks; the caller channel never does -> not scorable.
    return _write_stereo(tmp_path / "silent-caller.wav", [], [(0.2, 2.8)])


# --- single not-scorable recording: HTML -------------------------------------

def test_report_html_not_scorable(silent_caller_wav, tmp_path):
    out = tmp_path / "ns.html"
    rc = cli.main(["report", "--stereo", silent_caller_wav, "--out", str(out)])
    assert rc == 2  # unusable input, same mapping as run/doctor/capture
    html = out.read_text(encoding="utf-8")
    assert "NOT SCORABLE" in html
    assert "ALL PASS" not in html
    assert "REGRESSION" not in html
    # never a normal verdict chip for an unjudgeable input
    assert ">PASS<" not in html
    assert ">FAIL<" not in html
    # the reason is on the card and the dedicated section lists id + reason
    assert "caller speech" in html
    assert "Not scorable inputs" in html
    assert "silent-caller.wav" in html
    # summary counts line mirrors the CLI text
    assert "(failed=0, not_scorable=1)" in html


def test_report_html_not_scorable_excluded_from_analytics(silent_caller_wav):
    # The analytics rollup only renders once a page has >= 3 events, so pair
    # the not-scorable input with two real, passing, yield-measured events
    # (from the bundled suite) to exercise the exclusion.
    ns_event = core.run_single(stereo=silent_caller_wav)["events"][0]
    assert ns_event["scorable"] is False

    suite_env = core.run_suite(suite="barge-in")
    passing = [e for e in suite_env["events"] if e["verdict"]["passed"]
               and e["verdict"]["seconds_to_yield"] is not None][:2]
    assert len(passing) == 2

    env = core._envelope(mode="suite", stack=None, events=[ns_event] + passing)
    cfg = ScoreConfig()
    models = [report._event_model(e, [], cfg.hop_ms / 1000.0, cfg)
              for e in env["events"]]
    html = report._render_page(env, models, cfg)

    assert "Analytics" in html  # 3 events: the rollup renders
    # failure clusters must not count it (it used to land in "unclassified")
    assert "unclassified" not in html
    assert "No failures to cluster" in html
    # its null measurements stay out of the latency distribution; both real
    # events yielded, so nothing is missing either
    assert "no yield measured:" not in html


# --- single not-scorable recording: Markdown ---------------------------------

def test_report_md_not_scorable(silent_caller_wav, tmp_path):
    out = tmp_path / "ns.md"
    rc = cli.main(["report", "--stereo", silent_caller_wav,
                   "--format", "md", "--out", str(out)])
    assert rc == 2
    md = out.read_text(encoding="utf-8")
    assert "Verdict: NOT SCORABLE." in md
    assert "| NOT SCORABLE |" in md
    assert ": FAIL" not in md
    assert "| FAIL |" not in md
    assert "ALL PASS" not in md
    # id + reason live in their own section, never under Failures and fixes
    assert "## Not scorable inputs" in md
    assert "silent-caller.wav" in md
    assert "caller speech" in md
    assert "## Failures and fixes" not in md
    # summary counts line mirrors the CLI text
    assert "(failed=0, not_scorable=1)" in md


# --- mixed suite: a real failure always dominates the overall verdict --------

def _failing_scorable_event(tmp_path):
    # agent yields at ~1.5s; an impossible bound forces a real (scorable) FAIL
    wav = _write_stereo(tmp_path / "late-yield.wav", [(1.0, 2.0)], [(0.0, 1.5)])
    env = core.run_single(stereo=wav, max_time_to_yield_sec=0.0)
    e = env["events"][0]
    assert e.get("scorable") is not False
    assert not e["verdict"]["passed"]
    return e


def test_mixed_suite_regression_beats_not_scorable(silent_caller_wav, tmp_path):
    fail_event = _failing_scorable_event(tmp_path)
    ns_event = core.run_single(stereo=silent_caller_wav)["events"][0]
    assert ns_event["scorable"] is False

    env = core._envelope(mode="suite", stack=None, events=[fail_event, ns_event])
    assert env["summary"]["failed"] == 1
    assert env["summary"]["not_scorable"] == 1

    cfg = ScoreConfig()
    models = [report._event_model(e, [], cfg.hop_ms / 1000.0, cfg)
              for e in env["events"]]

    html = report._render_page(env, models, cfg)
    # REGRESSION wins the overall chip; the event still says NOT SCORABLE
    assert ">REGRESSION<" in html
    assert ">NOT SCORABLE<" in html
    assert "Not scorable inputs" in html
    assert "(failed=1, not_scorable=1)" in html

    md = report._render_md(env, models, cfg)
    assert "Verdict: REGRESSION." in md
    assert "| NOT SCORABLE |" in md
    assert "| FAIL |" in md
    assert "## Failures and fixes" in md
    assert "### late-yield.wav: FAIL" in md
    # the not-scorable input never appears as a failure
    assert "### silent-caller.wav" not in md
    assert "## Not scorable inputs" in md
    assert "- silent-caller.wav:" in md


# --- base comparison: no PASS/FAIL label for a not-scorable event ------------

def test_base_comparison_never_shows_pass_fail_for_not_scorable(silent_caller_wav):
    base_env = core.run_single(stereo=silent_caller_wav)

    md, _ = report.build_report_md(stereo=silent_caller_wav, base=base_env,
                                   base_label="base.json")
    assert "NOT SCORABLE to NOT SCORABLE" in md
    assert "| N/A |" in md
    assert "FAIL to" not in md
    assert "to FAIL" not in md

    html, _ = report.build_report_html(stereo=silent_caller_wav, base=base_env)
    assert "NOT SCORABLE to" in html
    assert ">N/A</span>" in html
    assert "FAIL to" not in html


# --- fully-scorable reports render exactly as before -------------------------

def test_all_scorable_suite_report_renders_as_before():
    html, env = report.build_report_html(suite="barge-in")
    assert env["summary"]["failed"] == 0
    assert "not_scorable" not in env["summary"]
    assert "NOT SCORABLE" not in html
    assert "not_scorable" not in html
    assert "Not scorable inputs" not in html
    assert ">ALL PASS<" in html
    assert "No failures to cluster. Every event passed." in html

    md, _ = report.build_report_md(suite="barge-in")
    assert "NOT SCORABLE" not in md
    assert "not_scorable" not in md
    assert "Not scorable inputs" not in md
    assert "Verdict: ALL PASS." in md
    assert "No failures to cluster. Every event passed." in md
