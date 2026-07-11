"""Optional ASR transcript CONTEXT rendered by the report + folded into the
machine envelope.

Pins the honesty invariants from the transcript integration:

  * absent by default -- a report built with no ``transcript`` (or
    ``transcript=None``) is byte-identical to one built before this feature
    existed: no "Transcript" panel, no ``transcript_context`` key anywhere.
  * report.py never imports ``hotato.transcribe`` -- it renders a
    Transcript-like object (or an equivalent dict) purely as data, so the
    strictly opt-in ``[transcribe]`` extra is never a hard dependency of the
    report path.
  * when present, it renders a collapsed, clearly-labelled "Transcript
    (context, not a score)" panel per matching event, and is folded into the
    returned envelope as an ADDITIVE ``transcript_context`` key.
  * it NEVER changes any timing/verdict field: did_yield, talk_over_sec,
    seconds_to_yield, and the PASS/FAIL/NOT SCORABLE chip are identical with
    or without a transcript attached.
  * suite mode supports per-event transcripts keyed by scenario_id; an event
    with no matching transcript stays untouched (no panel, no envelope key).
"""

import subprocess
import sys

from hotato import report
from hotato.transcribe import Transcript, TranscriptSegment


def _bundled_wav() -> str:
    from importlib import resources

    return str(resources.files("hotato").joinpath(
        "data", "audio", "01-hard-interruption.example.wav"))


def _transcript() -> Transcript:
    return Transcript(
        text="hello there how can I help",
        segments=[
            TranscriptSegment(start=0.0, end=1.0, text="hello there"),
            TranscriptSegment(start=1.2, end=2.5, text="how can I help"),
        ],
        language="en",
        model="base.en",
        device="cpu",
        compute_type="int8",
    )


# --- byte-identical default -------------------------------------------------

def test_report_py_never_imports_hotato_transcribe():
    """Importing hotato.report (and building a plain report) must never import
    hotato.transcribe as a side effect: the [transcribe] extra stays strictly
    opt-in and report.py stays dependency-free of it."""
    code = (
        "import sys\n"
        "from hotato import report\n"
        "report.build_report_html(suite='barge-in')\n"
        "assert 'hotato.transcribe' not in sys.modules, "
        "'hotato.report imported hotato.transcribe without a transcript'\n"
    )
    result = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True)
    assert result.returncode == 0, result.stderr


def test_default_html_has_no_transcript_panel_or_key():
    html, env = report.build_report_html(suite="barge-in")
    assert "Transcript (context, not a score)" not in html
    assert "transcript" not in html.lower()
    for e in env["events"]:
        assert "transcript_context" not in e


def test_explicit_none_is_byte_identical_to_omitted():
    a, _ = report.build_report_html(stereo=_bundled_wav())
    b, _ = report.build_report_html(stereo=_bundled_wav(), transcript=None)
    assert a == b


def test_default_md_has_no_transcript_section():
    md, env = report.build_report_md(suite="barge-in")
    assert "Transcripts (context, not a score)" not in md
    for e in env["events"]:
        assert "transcript_context" not in e


# --- single-recording report ------------------------------------------------

def test_single_recording_renders_transcript_panel():
    html, env = report.build_report_html(stereo=_bundled_wav(), transcript=_transcript())
    assert "Transcript (context, not a score)" in html
    assert "hello there" in html
    assert "how can I help" in html
    assert '<details class="transcript">' in html
    ctx = env["events"][0]["transcript_context"]
    assert ctx["text"] == "hello there how can I help"
    assert ctx["segments"] == [
        {"start": 0.0, "end": 1.0, "text": "hello there"},
        {"start": 1.2, "end": 2.5, "text": "how can I help"},
    ]
    assert ctx["model"] == "base.en"
    assert ctx["language"] == "en"


def test_transcript_never_changes_the_verdict_or_measurements():
    plain_html, plain_env = report.build_report_html(stereo=_bundled_wav())
    tr_html, tr_env = report.build_report_html(stereo=_bundled_wav(),
                                               transcript=_transcript())
    assert plain_env["summary"] == tr_env["summary"]
    pe, te = plain_env["events"][0], tr_env["events"][0]
    assert pe["verdict"] == te["verdict"]
    assert pe["measurements"] == te["measurements"]
    assert pe["signals"] == te["signals"]
    # The chip / PASS-FAIL text is identical between the two renders.
    import re

    def _chip(html):
        m = re.search(r'class="chip"[^>]*>([A-Z ]+)<', html)
        return m.group(1)

    assert _chip(plain_html) == _chip(tr_html)


def test_transcript_panel_states_context_not_a_score():
    html, _ = report.build_report_html(stereo=_bundled_wav(), transcript=_transcript())
    assert "NEVER fed back into the measurement" in html
    assert "did_yield" in html and "PASS/FAIL verdict are unaffected" in html


def test_transcript_as_plain_dict_also_works():
    plain = {
        "text": "hi",
        "segments": [{"start": 0.0, "end": 0.5, "text": "hi"}],
        "model": "tiny.en",
    }
    html, env = report.build_report_html(stereo=_bundled_wav(), transcript=plain)
    assert "Transcript (context, not a score)" in html
    assert ">hi<" in html
    assert env["events"][0]["transcript_context"]["text"] == "hi"
    assert env["events"][0]["transcript_context"]["model"] == "tiny.en"


def test_transcript_with_no_segments_falls_back_to_plain_text():
    t = Transcript(text="just some text", segments=[])
    html, env = report.build_report_html(stereo=_bundled_wav(), transcript=t)
    assert "just some text" in html
    assert env["events"][0]["transcript_context"]["segments"] == []


def test_empty_transcript_renders_no_panel():
    t = Transcript(text="", segments=[])
    html, env = report.build_report_html(stereo=_bundled_wav(), transcript=t)
    assert "Transcript (context, not a score)" not in html
    # The envelope still carries the (empty) context: additive, never dropped.
    assert env["events"][0]["transcript_context"] == {
        "text": "", "segments": [], "model": "unknown",
        "device": "unknown", "compute_type": "unknown", "language": None,
    }


# --- suite mode: per-event transcripts keyed by scenario_id -----------------

def test_suite_transcript_dict_attaches_only_to_matching_events():
    per_event = {"02-backchannel-mhm": _transcript()}
    html, env = report.build_report_html(suite="barge-in", transcript=per_event)
    assert html.count("Transcript (context, not a score)") == 1
    matched = next(e for e in env["events"] if e["scenario_id"] == "02-backchannel-mhm")
    assert matched["transcript_context"]["text"] == "hello there how can I help"
    others = [e for e in env["events"] if e["scenario_id"] != "02-backchannel-mhm"]
    assert all("transcript_context" not in e for e in others)


def test_suite_transcript_dict_with_no_matches_is_unchanged():
    html_plain, env_plain = report.build_report_html(suite="barge-in")
    html_tr, env_tr = report.build_report_html(
        suite="barge-in", transcript={"nonexistent-scenario": _transcript()}
    )
    assert html_plain == html_tr
    assert env_plain["events"] == env_tr["events"]


def test_suite_single_transcript_object_applies_to_every_event():
    html, env = report.build_report_html(suite="barge-in", transcript=_transcript())
    assert html.count("Transcript (context, not a score)") == env["summary"]["events"]
    assert all(e.get("transcript_context") for e in env["events"])


# --- markdown rendering ------------------------------------------------------

def test_markdown_renders_transcript_section_only_when_present():
    md_plain, _ = report.build_report_md(stereo=_bundled_wav())
    md_tr, _ = report.build_report_md(stereo=_bundled_wav(), transcript=_transcript())
    assert "Transcripts (context, not a score)" not in md_plain
    assert "Transcripts (context, not a score)" in md_tr
    assert "hello there" in md_tr
    assert "how can I help" in md_tr


def test_markdown_transcript_never_changes_the_verdict_table():
    md_plain, _ = report.build_report_md(suite="barge-in")
    md_tr, _ = report.build_report_md(suite="barge-in", transcript=_transcript())
    # Strip the appended transcript section and compare the rest verbatim.
    head_plain = md_plain.split("## Analytics")[0]
    head_tr = md_tr.split("## Transcripts (context, not a score)")[0]
    assert head_plain == head_tr
