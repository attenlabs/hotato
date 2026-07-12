"""Optional voice-trace CONTEXT rendered by the report + folded into the
machine envelope.

Pins the honesty invariants from the trace integration, mirroring the
transcript and assertions integrations exactly:

  * absent by default -- a report built with no ``trace`` (or ``trace=None``)
    is byte-identical to one built before this feature existed: no "Trace"
    section, no new CSS classes, no ``trace_context`` key anywhere.
  * report.py never imports ``hotato.trace`` -- it renders a voice_trace.v1
    object (or an equivalent dict/list) purely as data, so the circular import
    ``hotato.trace`` -> ``hotato.contract`` -> ``hotato.report`` never forms.
  * when present, it renders ONE collapsed, clearly-labelled call-level
    "Trace (context, not a score)" section and is folded into the returned
    envelope as an ADDITIVE top-level ``trace_context`` key.
  * it NEVER changes any timing/verdict field: did_yield, talk_over_sec,
    seconds_to_yield, and the PASS/FAIL/NOT SCORABLE chip are identical with
    or without a trace attached.
  * redaction is respected: a span carrying ``text_redacted: true`` shows a
    ``[redacted]`` placeholder and NEVER its text (not even text lurking in
    the span's own ``attributes``).
  * the Markdown renderer mirrors the same section as a table.
"""

import subprocess
import sys
from importlib import resources

from hotato import report


def _bundled_wav() -> str:
    return str(resources.files("hotato").joinpath(
        "data", "audio", "01-hard-interruption.example.wav"))


def _trace() -> dict:
    """A realistic voice_trace.v1 object (the shape load_voice_trace_jsonl
    returns): meta keys plus a list of spans -- one interval span, one point
    span, and a tool_call carrying a name + latency."""
    return {
        "schema": "hotato.voice_trace.v1",
        "call_id": None,
        "deployment": {"stack": "vapi"},
        "spans": [
            {"type": "caller_audio_active", "start_sec": 2.40, "end_sec": 4.10},
            {"type": "tts_cancel_requested", "time_sec": 2.60},
            {"type": "tool_call", "start_sec": 1.10, "end_sec": 1.42,
             "name": "lookup_order", "latency_ms": 320},
        ],
    }


def _redacted_trace() -> dict:
    """An asr_partial ingested WITHOUT --include-text: text dropped,
    text_redacted:true. The raw text can still lurk in ``attributes`` (that is
    how ingest leaves it), so the render must honor the flag BEFORE reading
    anything else -- the placeholder is shown and the text never appears."""
    return {
        "schema": "hotato.voice_trace.v1",
        "spans": [
            {"type": "asr_partial", "start_sec": 2.40, "end_sec": 2.95,
             "text_redacted": True,
             "attributes": {"text": "SECRET-REFUND-REQUEST"}},
        ],
    }


def _transcript_dict() -> dict:
    return {
        "text": "hello there how can I help",
        "segments": [{"start": 0.0, "end": 1.0, "text": "hello there"}],
        "model": "base.en",
        "language": "en",
    }


def _assert_env() -> dict:
    """A minimal, already-evaluated assert.v1 envelope (hand-built so this test
    needs neither hotato.assert_ nor jsonschema): one PASS deterministic
    phrase result, a two-count summary, no overall_score."""
    return {
        "schema": "assert.v1", "exit_code": 0,
        "results": [{"id": "disclosure", "kind": "phrase", "status": "PASS",
                     "deterministic": True, "reason": ""}],
        "summary": {
            "deterministic": {"pass": 1, "fail": 0, "inconclusive": 0},
            "judge": {"pass": 0, "fail": 0},
        },
    }


# --- byte-identical default -------------------------------------------------

def test_report_py_never_imports_hotato_trace():
    """Importing hotato.report and building a report (even WITH a trace) must
    never import hotato.trace as a side effect: report.py renders the trace as
    data, so the hotato.trace -> hotato.contract -> hotato.report cycle never
    forms."""
    code = (
        "import sys\n"
        "from hotato import report\n"
        "report.build_report_html(suite='barge-in', trace={'spans': "
        "[{'type': 'tts_cancel_requested', 'time_sec': 1.0}]})\n"
        "assert 'hotato.trace' not in sys.modules, "
        "'hotato.report imported hotato.trace'\n"
    )
    result = subprocess.run([sys.executable, "-c", code],
                            capture_output=True, text=True)
    assert result.returncode == 0, result.stderr


def test_default_html_has_no_trace_section_or_key():
    html, env = report.build_report_html(suite="barge-in")
    assert "Trace (context, not a score)" not in html
    assert "tracetab" not in html
    assert "trace_context" not in env


def test_explicit_none_is_byte_identical_to_omitted_html():
    a, _ = report.build_report_html(stereo=_bundled_wav())
    b, _ = report.build_report_html(stereo=_bundled_wav(), trace=None)
    assert a == b


def test_default_md_has_no_trace_section():
    md, env = report.build_report_md(suite="barge-in")
    assert "## Trace (context, not a score)" not in md
    assert "trace_context" not in env


def test_explicit_none_is_byte_identical_to_omitted_md():
    a, _ = report.build_report_md(stereo=_bundled_wav())
    b, _ = report.build_report_md(stereo=_bundled_wav(), trace=None)
    assert a == b


def test_no_trace_css_added_when_absent():
    html, _ = report.build_report_html(suite="barge-in")
    assert "table.tracetab{" not in html
    assert "details.trace summary{" not in html


# --- normal span rendering --------------------------------------------------

def test_trace_section_renders_spans():
    html, _ = report.build_report_html(stereo=_bundled_wav(), trace=_trace())
    assert "Trace (context, not a score)" in html
    assert '<details class="card trace">' in html
    assert "table.tracetab{" in html  # CSS appended only when present
    # span types, the tool name, and the point/interval times all render
    assert "caller_audio_active" in html
    assert "tts_cancel_requested" in html
    assert "lookup_order" in html
    assert "2.40s" in html and "4.10s" in html   # interval span
    assert "2.60s" in html                        # point span (time_sec)
    assert "latency_ms=320" in html


def test_trace_section_states_context_not_a_score():
    html, _ = report.build_report_html(stereo=_bundled_wav(), trace=_trace())
    assert "never fed back into any measurement" in html
    assert "did_yield" in html and "PASS/FAIL verdict are unaffected" in html


def test_trace_as_bare_span_list_also_works():
    """Duck-typed: a bare list of span dicts (no meta wrapper) renders too."""
    spans = [{"type": "tts_audio_stopped", "time_sec": 2.9}]
    html, env = report.build_report_html(stereo=_bundled_wav(), trace=spans)
    assert "tts_audio_stopped" in html
    assert env["trace_context"]["spans"] == [
        {"type": "tts_audio_stopped", "time_sec": 2.9}
    ]
    assert env["trace_context"]["meta"] == {}


def test_empty_trace_renders_empty_state_note():
    html, env = report.build_report_html(stereo=_bundled_wav(),
                                         trace={"spans": []})
    assert "Trace (context, not a score)" in html
    assert "No spans in this trace." in html
    # additive, never dropped
    assert env["trace_context"] == {"meta": {}, "spans": []}


def test_bad_trace_type_raises_value_error():
    import pytest
    with pytest.raises(ValueError, match="voice_trace"):
        report.build_report_html(stereo=_bundled_wav(), trace="nope")


# --- redaction wall ---------------------------------------------------------

def test_redacted_span_shows_placeholder_never_the_text():
    html, _ = report.build_report_html(stereo=_bundled_wav(),
                                       trace=_redacted_trace())
    # the placeholder renders in the span's OWN detail CELL (not merely in the
    # explanatory note, which also mentions "[redacted]")
    assert "<td>[redacted]</td>" in html
    # the raw text never leaks -- not from the dropped ``text`` and not from
    # the ``attributes`` copy the flag must shield
    assert "SECRET-REFUND-REQUEST" not in html
    # the redacted span still appears (its type is shown), just not its text
    assert "asr_partial" in html


def test_redacted_span_redacted_in_markdown_too():
    md, _ = report.build_report_md(stereo=_bundled_wav(),
                                   trace=_redacted_trace())
    assert "| [redacted] |" in md
    assert "SECRET-REFUND-REQUEST" not in md


# --- envelope: additive trace_context ---------------------------------------

def test_envelope_carries_trace_context_additively():
    plain_html, plain_env = report.build_report_html(stereo=_bundled_wav())
    tr_html, tr_env = report.build_report_html(stereo=_bundled_wav(),
                                               trace=_trace())
    assert "trace_context" not in plain_env
    ctx = tr_env["trace_context"]
    assert ctx["meta"]["schema"] == "hotato.voice_trace.v1"
    assert ctx["meta"]["deployment"] == {"stack": "vapi"}
    assert [s["type"] for s in ctx["spans"]] == [
        "caller_audio_active", "tts_cancel_requested", "tool_call",
    ]


def test_trace_never_changes_the_verdict_or_measurements():
    plain_html, plain_env = report.build_report_html(stereo=_bundled_wav())
    tr_html, tr_env = report.build_report_html(stereo=_bundled_wav(),
                                               trace=_trace())
    assert plain_env["summary"] == tr_env["summary"]
    pe, te = plain_env["events"][0], tr_env["events"][0]
    assert pe["verdict"] == te["verdict"]
    assert pe["measurements"] == te["measurements"]
    assert pe["signals"] == te["signals"]

    import re

    def _chip(html):
        return re.search(r'class="chip"[^>]*>([A-Z ]+)<', html).group(1)

    assert _chip(plain_html) == _chip(tr_html)


def test_byte_stable_across_repeated_renders():
    a, _ = report.build_report_html(stereo=_bundled_wav(), trace=_trace())
    b, _ = report.build_report_html(stereo=_bundled_wav(), trace=_trace())
    assert a == b
    ma, _ = report.build_report_md(stereo=_bundled_wav(), trace=_trace())
    mb, _ = report.build_report_md(stereo=_bundled_wav(), trace=_trace())
    assert ma == mb


# --- markdown rendering -----------------------------------------------------

def test_markdown_renders_trace_section_only_when_present():
    md_plain, _ = report.build_report_md(stereo=_bundled_wav())
    md_tr, _ = report.build_report_md(stereo=_bundled_wav(), trace=_trace())
    assert "## Trace (context, not a score)" not in md_plain
    assert "## Trace (context, not a score)" in md_tr
    assert "lookup_order" in md_tr
    assert "latency_ms=320" in md_tr


# --- write_report passthrough -----------------------------------------------

def test_write_report_carries_trace_through(tmp_path):
    out = tmp_path / "r.html"
    report.write_report(str(out), fmt="html", stereo=_bundled_wav(),
                        trace=_trace())
    text = out.read_text(encoding="utf-8")
    assert "Trace (context, not a score)" in text
    assert "lookup_order" in text


# --- unified report: timing + transcript + trace + assertions all present ---

def test_unified_report_shows_all_four_sections():
    """A single report with the base timing report PLUS transcript PLUS trace
    PLUS assertions renders all four as distinct, clearly-labelled sections --
    the Phase-0 consolidation goal (one report unifies them)."""
    html, env = report.build_report_html(
        stereo=_bundled_wav(),
        transcript=_transcript_dict(),
        trace=_trace(),
        assertions=_assert_env(),
    )
    # 1) base timing report (the always-present per-event timing card + stats)
    assert "time to yield" in html and "Thresholds used" in html
    # 2) transcript context
    assert "Transcript (context, not a score)" in html
    # 3) trace context
    assert "Trace (context, not a score)" in html
    # 4) assertions
    assert "1 deterministic pass / 0 fail  0 judge-scored (advisory)" in html

    # the machine envelope carries the additive context keys too
    assert env["events"][0]["transcript_context"]["text"] == \
        "hello there how can I help"
    assert env["trace_context"]["meta"]["schema"] == "hotato.voice_trace.v1"

    md, _ = report.build_report_md(
        stereo=_bundled_wav(),
        transcript=_transcript_dict(),
        trace=_trace(),
        assertions=_assert_env(),
    )
    assert "## Transcripts (context, not a score)" in md
    assert "## Trace (context, not a score)" in md
    assert "## Assertions" in md
