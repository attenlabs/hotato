"""The interactive HTML report and the one-command ``doctor`` flow.

The report is the shareable artifact, so these tests pin the properties that
make it trustworthy and portable: it is a SINGLE self-contained file (no
external requests of any kind), it draws a real per-event timeline, every value
on it is a real measurement the scorer produced, and no accuracy percentage or
competitor vendor name ever leaks onto the page.
"""

import re

import pytest

from hotato import cli, report
from hotato._engine.score import ScoreConfig

# Vendor / product names the honest, vendor-neutral output must never surface.
_VENDORS = [
    "krisp", "deepgram", "assemblyai", "cartesia", "elevenlabs",
    "vapi", "retell", "twilio", "livekit", "pipecat",
]


def _fmt(x):
    return None if x is None else f"{x:.2f}s"


def test_report_generates_for_bundled_suite():
    html, env = report.build_report_html(suite="barge-in")
    assert env["summary"]["events"] == 8
    assert html.startswith("<!doctype html>")
    assert "hotato" in html
    assert "</html>" in html


def test_report_is_single_self_contained_file():
    html, _ = report.build_report_html(suite="barge-in")
    # No external requests of ANY kind: no absolute URLs, no linked/loaded assets.
    assert "http://" not in html
    assert "https://" not in html
    assert "<link" not in html
    assert "<script" not in html
    assert "src=" not in html
    assert "@import" not in html
    assert "url(" not in html
    # Inline SVG must not reintroduce a namespace URL.
    assert "xmlns" not in html


def test_report_has_a_timeline_per_event():
    html, env = report.build_report_html(suite="barge-in")
    # One inline SVG timeline per event, each with a caller and an agent track.
    # (The analytics block adds its own charts; timelines carry the tl-svg class.)
    assert html.count('<svg class="tl-svg"') == env["summary"]["events"]
    assert ">Caller<" in html
    assert ">Agent<" in html
    # The overlap span is shaded and the markers are drawn/labelled.
    assert "talk-over" in html
    assert ">onset<" in html
    assert ">yield<" in html


def test_every_number_on_the_page_is_a_real_measurement():
    html, env = report.build_report_html(suite="barge-in")
    # For every event, its real measured talk-over and onset seconds appear on
    # the page (nothing invented, nothing rounded differently).
    for e in env["events"]:
        tov = _fmt(e["verdict"]["talk_over_sec"])
        if tov is not None:
            assert tov in html, f"missing measured talk_over {tov}"
        onset = e["measurements"].get("caller_onset_sec")
        if onset is not None and onset >= 0:
            assert _fmt(onset) in html, f"missing measured onset {_fmt(onset)}"


def test_thresholds_are_shown_for_reproducibility():
    html, _ = report.build_report_html(suite="barge-in")
    cfg = ScoreConfig()
    assert "Thresholds used" in html
    assert "yield_hangover_sec" in html
    assert "max_search_sec" in html
    assert str(cfg.yield_hangover_sec) in html
    # VAD parameters for both channels are exposed.
    assert "noise_percentile" in html


def test_no_accuracy_percentage_anywhere():
    html, _ = report.build_report_html(suite="barge-in")
    # The strongest form of "no accuracy score": there is no percent sign at all.
    assert "%" not in html
    # And no digit-then-percent pattern (belt and suspenders).
    assert re.search(r"\d\s*%", html) is None
    # The honest disclaimer is stated positively.
    assert "No accuracy score" in html


def test_no_vendor_name_leaks_in_generic_report():
    html, _ = report.build_report_html(suite="barge-in")
    low = html.lower()
    for name in _VENDORS:
        assert name not in low, f"vendor name leaked into the report: {name}"


def test_no_em_or_en_dashes_in_report():
    html, _ = report.build_report_html(suite="barge-in")
    assert "\u2013" not in html  # en dash
    assert "\u2014" not in html  # em dash


def test_report_single_recording_fail_path(tmp_path):
    from importlib import resources

    wav = str(resources.files("hotato").joinpath(
        "data", "audio", "01-hard-interruption.example.wav"))
    out = tmp_path / "single.html"
    # An impossible bound forces a FAIL so the chip / reasons / fix all render.
    code = cli.main(["report", "--stereo", wav, "--max-time-to-yield", "0.0",
                     "--out", str(out)])
    assert code == 1
    html = out.read_text(encoding="utf-8")
    assert ">FAIL<" in html
    assert "http://" not in html and "https://" not in html
    assert "%" not in html
    assert html.count('<svg class="tl-svg"') == 1


def test_report_cli_writes_self_contained_file(tmp_path):
    out = tmp_path / "suite.html"
    code = cli.main(["report", "--suite", "barge-in", "--out", str(out)])
    assert code == 0
    assert out.exists()
    html = out.read_text(encoding="utf-8")
    assert "http" not in html and "%" not in html
    assert html.count('<svg class="tl-svg"') == 8


def test_doctor_demo_runs_end_to_end_and_exits_zero(tmp_path):
    out = tmp_path / "doctor.html"
    # --demo self-test, offline, no browser launch: writes the report, exits 0.
    code = cli.main(["doctor", "--demo", "--no-open", "--out", str(out)])
    assert code == 0
    assert out.exists()
    html = out.read_text(encoding="utf-8")
    assert html.count('<svg class="tl-svg"') == 8
    assert "http://" not in html and "https://" not in html
    assert "%" not in html


def test_doctor_defaults_to_self_test_without_a_recording(tmp_path):
    out = tmp_path / "doctor2.html"
    # No recording and no --demo: still falls back to the bundled self-test.
    code = cli.main(["doctor", "--no-open", "--out", str(out)])
    assert code == 0
    assert out.exists()
