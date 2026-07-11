"""The interactive HTML report and the one-command ``doctor`` flow.

The report is the shareable artifact, so these tests pin the properties that
make it trustworthy and portable: it is a SINGLE self-contained file (no
external requests of any kind), it draws a real per-event timeline, every value
on it is a real measurement the scorer produced, and no accuracy percentage or
competitor vendor name ever leaks onto the page.
"""

import json
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


def test_doctor_format_json_emits_only_the_envelope_on_stdout(tmp_path, capsys):
    # Mirrors `demo --format json`: stdout is the pure machine envelope, the
    # report path and every human-readable line land on stderr instead.
    out = tmp_path / "doctor3.html"
    code = cli.main(["doctor", "--demo", "--no-open", "--format", "json",
                      "--out", str(out)])
    assert code == 0
    cap = capsys.readouterr()
    env = json.loads(cap.out)  # raises if stdout carries anything but JSON
    assert env["tool"] == "hotato"
    assert "ok" not in env
    assert f"report: {out}" in cap.err
    assert cap.out.strip().endswith("}")  # nothing appended after the envelope
    assert out.exists()


def test_doctor_format_json_scores_a_real_recording(tmp_path, capsys):
    from importlib import resources

    wav = str(resources.files("hotato").joinpath(
        "data", "audio", "01-hard-interruption.example.wav"))
    out = tmp_path / "doctor4.html"
    code = cli.main(["doctor", "--stereo", wav, "--no-open", "--format", "json",
                      "--out", str(out)])
    env = json.loads(capsys.readouterr().out)
    assert env["mode"] == "single"
    assert code == env["exit_code"]


# --- audio embedding (--embed-audio) ---------------------------------------

def _bundled_wav() -> str:
    from importlib import resources

    return str(resources.files("hotato").joinpath(
        "data", "audio", "01-hard-interruption.example.wav"))


def test_default_report_has_no_audio_tags():
    # Embedding is strictly opt-in: the plain report stays small.
    html, _ = report.build_report_html(suite="barge-in")
    assert "<audio" not in html
    assert "data:audio" not in html


def test_embed_audio_one_player_per_event_still_zero_external_requests():
    html, env = report.build_report_html(suite="barge-in", embed_audio=True)
    # One native player per event, fed by an inline data URI.
    assert html.count("<audio") == env["summary"]["events"]
    assert html.count("data:audio/wav;base64,") == env["summary"]["events"]
    assert "controls" in html
    # Still one self-contained file: nothing fetched from anywhere.
    assert "http://" not in html
    assert "https://" not in html
    assert "<link" not in html
    assert "<script" not in html
    # The bundled fixtures are synthetic and the page says so.
    assert "synthetic fixture" in html


def test_embed_audio_single_recording_via_cli(tmp_path):
    out = tmp_path / "single-embedded.html"
    code = cli.main(["report", "--stereo", _bundled_wav(), "--embed-audio",
                     "--out", str(out)])
    assert code == 0
    html = out.read_text(encoding="utf-8")
    assert html.count("<audio") == 1
    assert "data:audio/wav;base64," in html
    assert "http://" not in html and "https://" not in html


def test_embed_audio_oversize_file_is_noted_and_skipped(monkeypatch):
    # Shrink the ceiling instead of shipping a >8 MB fixture: same code path.
    monkeypatch.setattr(report, "_EMBED_MAX_BYTES", 1024)
    html, _ = report.build_report_html(stereo=_bundled_wav(), embed_audio=True)
    assert "<audio" not in html
    assert "data:audio" not in html
    assert "audio not embedded" in html
    assert "embed limit" in html


def test_embed_audio_with_md_format_is_a_clean_usage_error(tmp_path):
    out = tmp_path / "r.md"
    code = cli.main(["report", "--suite", "barge-in", "--format", "md",
                     "--embed-audio", "--out", str(out)])
    assert code == 2
    assert not out.exists()


def test_report_cli_prints_total_size_when_embedding(tmp_path, capsys):
    out = tmp_path / "suite-embedded.html"
    code = cli.main(["report", "--suite", "barge-in", "--embed-audio",
                     "--out", str(out)])
    assert code == 0
    err = capsys.readouterr().err
    assert "report size:" in err
    assert f"{out.stat().st_size} bytes" in err
    assert out.read_text(encoding="utf-8").count("<audio") == 8


def test_doctor_embeds_audio_for_a_recording_but_not_for_self_test(tmp_path):
    rec = tmp_path / "doctor-rec.html"
    code = cli.main(["doctor", "--stereo", _bundled_wav(), "--no-open",
                     "--out", str(rec)])
    assert code == 0
    html = rec.read_text(encoding="utf-8")
    assert "<audio" in html and "data:audio/wav;base64," in html

    demo = tmp_path / "doctor-demo.html"
    code = cli.main(["doctor", "--demo", "--no-open", "--out", str(demo)])
    assert code == 0
    assert "<audio" not in demo.read_text(encoding="utf-8")


# --- audio_reference (fleet) vs self_contained (local) export split ---------

def test_audio_reference_references_audio_and_never_inlines_pcm():
    # The fleet-safe mode: name the content-addressed audio, inline none of it.
    html, _ = report.build_report_html(stereo=_bundled_wav(),
                                       audio_mode="audio_reference")
    # No PCM anywhere: no data: URI, no native player.
    assert "data:audio" not in html
    assert "<audio" not in html
    # A stable content-addressed reference IS present: pcm_sha256 + a locator.
    assert "pcm_sha256" in html
    assert "locator" in html
    # The page says playback needs the fleet store, so a shared copy leaks no PII.
    assert "fleet store" in html.lower()
    # Still one self-contained page: nothing is fetched.
    assert "http://" not in html and "https://" not in html


def test_self_contained_mode_equals_embed_audio_and_keeps_data_uri():
    # Both spellings of "inline the audio" still produce a base64 data URI.
    a, _ = report.build_report_html(stereo=_bundled_wav(), embed_audio=True)
    b, _ = report.build_report_html(stereo=_bundled_wav(),
                                    audio_mode="self_contained")
    assert "data:audio/wav;base64," in a
    assert "data:audio/wav;base64," in b


def test_audio_mode_none_is_the_unchanged_small_default():
    default, _ = report.build_report_html(suite="barge-in")
    explicit, _ = report.build_report_html(suite="barge-in", audio_mode="none")
    for html in (default, explicit):
        assert "<audio" not in html and "data:audio" not in html


def test_audio_reference_over_the_bundled_suite_leaks_no_pcm():
    html, env = report.build_report_html(suite="barge-in",
                                         audio_mode="audio_reference")
    assert "data:audio" not in html
    assert "<audio" not in html
    # every scored source is referenced by its content-address hash
    assert html.count("pcm_sha256") == env["summary"]["events"]


def test_audio_mode_wins_over_embed_audio_when_both_given():
    # audio_mode is explicit intent; it overrides the legacy bool.
    html, _ = report.build_report_html(stereo=_bundled_wav(), embed_audio=True,
                                       audio_mode="audio_reference")
    assert "data:audio" not in html
    assert "pcm_sha256" in html


def test_unknown_audio_mode_is_a_clean_value_error():
    with pytest.raises(ValueError):
        report.build_report_html(suite="barge-in", audio_mode="bogus")
