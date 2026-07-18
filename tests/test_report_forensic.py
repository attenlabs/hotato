"""Derived forensic-analysis block: per-hop latency waterfall, earliest
violated invariant (cross-lane, by wall-clock), and preserved interrupted
speech.

All three are additive, purely-derived enrichments to the forensic report,
composed from evidence the report ALREADY carries (timing models + the
attached voice trace's span timestamps + evaluated assertions). Pins:

  * absent by default -- no trace means no ``forensic`` envelope key and no
    "Forensic analysis" section, byte-identical to before;
  * the waterfall labels each hop with the exact span delta it came from and
    fail-closes a hop with no spans to "not captured", never a fabricated ms;
  * the earliest violated invariant is chosen across turn-taking + tool +
    state + trace by TIME-IN-CALL, not lane order, and a failure that cites no
    timed span is listed under ``untimed`` (never ordered by guess);
  * interrupted speech preserves WHAT the agent was saying when cut off, and
    honors the trace redaction wall (a redacted span never leaks its text).
"""

from __future__ import annotations

from importlib import resources

import pytest

from hotato import report as R


def _wav() -> str:
    return str(resources.files("hotato").joinpath(
        "data", "audio", "01-hard-interruption.example.wav"))


def _full_trace() -> dict:
    return {
        "schema": "hotato.voice_trace.v1",
        "deployment": {"stack": "vapi"},
        "spans": [
            {"type": "caller_audio_active", "start_sec": 2.40, "end_sec": 4.10},
            {"type": "agent_audio_active", "start_sec": 0.00, "end_sec": 2.90},
            {"type": "asr_partial", "start_sec": 2.40, "end_sec": 2.95,
             "text_redacted": True},
            {"type": "llm_first_token", "time_sec": 3.10},
            {"type": "tool_call", "start_sec": 3.20, "end_sec": 3.50,
             "name": "issue_refund", "latency_ms": 300},
            {"type": "tts_cancel_requested", "time_sec": 2.60,
             "text": "let me look that up for you"},
            {"type": "tts_audio_stopped", "time_sec": 2.90},
            {"type": "http_exchange", "time_sec": 3.30, "latency_ms": 220},
        ],
    }


# --- Feature 2: per-hop latency waterfall ----------------------------------

def test_latency_waterfall_measures_each_hop_from_span_deltas():
    wf = R._latency_waterfall(_full_trace())
    by = {h["hop"]: h for h in wf["hops"]}
    assert [h["hop"] for h in wf["hops"]] == [
        "stt", "llm", "tool", "tts", "transport"]
    assert by["stt"]["status"] == "measured"
    assert by["stt"]["ms"] == pytest.approx(550.0)       # 2.95 - 2.40
    assert by["llm"]["ms"] == pytest.approx(150.0)       # 3.10 - 2.95
    assert by["tool"]["ms"] == pytest.approx(300.0)      # tool_call latency_ms
    assert by["tts"]["ms"] == pytest.approx(300.0)       # 2.90 - 2.60
    assert by["transport"]["ms"] == pytest.approx(220.0)  # http latency_ms
    for h in wf["hops"]:
        assert h["basis"]                                 # every hop names its source


def test_latency_waterfall_marks_absent_hops_not_captured():
    # A trace with only a tool_call: every other hop must fail-closed.
    trace = {"spans": [{"type": "tool_call", "start_sec": 1.0, "end_sec": 1.3,
                        "name": "lookup", "latency_ms": 300}]}
    by = {h["hop"]: h for h in R._latency_waterfall(trace)["hops"]}
    assert by["tool"]["status"] == "measured"
    for hop in ("stt", "llm", "tts", "transport"):
        assert by[hop]["status"] == "not captured"
        assert by[hop]["ms"] is None


def test_latency_waterfall_tool_without_latency_is_not_captured():
    trace = {"spans": [{"type": "tool_call", "name": "lookup"}]}
    by = {h["hop"]: h for h in R._latency_waterfall(trace)["hops"]}
    assert by["tool"]["status"] == "not captured"


# --- Feature 5: preserve interrupted-speech text ---------------------------

def test_interrupted_speech_preserves_text():
    ents = R._interrupted_speech(_full_trace())
    cancel = next(e for e in ents if e["event"] == "tts_cancel_requested")
    assert cancel["text_state"] == "preserved"
    assert cancel["text"] == "let me look that up for you"
    assert cancel["at_sec"] == pytest.approx(2.60)


def test_interrupted_speech_honors_redaction_wall():
    trace = {"spans": [
        {"type": "tts_cancel_requested", "time_sec": 2.6, "text_redacted": True,
         "attributes": {"text": "SECRET"}},
    ]}
    ents = R._interrupted_speech(trace)
    assert ents[0]["text_state"] == "redacted"
    assert ents[0]["text"] is None
    # the raw text never rides along, not even from attributes
    assert "SECRET" not in str(ents)


def test_interrupted_speech_empty_when_no_text_carried():
    # A cut-off pair with no speech text at all: skip, never fabricate a line.
    trace = {"spans": [
        {"type": "tts_cancel_requested", "time_sec": 2.6},
        {"type": "tts_audio_stopped", "time_sec": 2.9},
    ]}
    assert R._interrupted_speech(trace) == []


# --- Feature 3: earliest violated invariant (cross-lane, by wall-clock) -----

def _fail_model(onset, *, did_yield=False, talk_over=None):
    return {"event": {"scorable": True}, "passed": False,
            "expected_yield": True, "did_yield": did_yield, "onset": onset,
            "talk_over_sec": talk_over}


def test_earliest_violation_picks_first_by_wall_clock_across_lanes():
    # Turn-taking failure at t=5.0; a tool assertion failure at t=3.2 (its
    # earliest cited span). The tool failure is chronologically first, even
    # though turn-taking is listed first by lane order.
    trace = _full_trace()
    models = [_fail_model(5.0)]
    assertions = {"results": [
        {"id": "refund-once", "kind": "count", "status": "FAIL",
         "span_ids": ["s_4"]},          # tool_call span index 4, start 3.20
    ]}
    ev = R._earliest_violation(models, assertions, trace)
    # a `count` reads trace spans, so its lane is "trace"; it is at t=3.20,
    # chronologically before the turn-taking failure at t=5.0.
    assert ev["earliest"]["lane"] == "trace"
    assert ev["earliest"]["time_sec"] == pytest.approx(3.20)
    # both failures are in the ordered "considered" list, trace before turn-taking
    lanes = [c["lane"] for c in ev["considered"]]
    assert lanes == ["trace", "turn-taking"]


def test_earliest_violation_turn_taking_wins_when_earliest():
    trace = _full_trace()
    models = [_fail_model(1.0)]                     # onset 1.0, before the tool span
    assertions = {"results": [
        {"id": "refund-once", "kind": "count", "status": "FAIL",
         "span_ids": ["s_4"]},
    ]}
    ev = R._earliest_violation(models, assertions, trace)
    assert ev["earliest"]["lane"] == "turn-taking"
    assert ev["earliest"]["time_sec"] == pytest.approx(1.0)


def test_earliest_violation_untimed_failures_listed_not_ordered():
    trace = _full_trace()
    assertions = {"results": [
        {"id": "state-ok", "kind": "state", "status": "FAIL", "span_ids": []},
        {"id": "refund-once", "kind": "count", "status": "FAIL",
         "span_ids": ["s_4"]},
    ]}
    ev = R._earliest_violation([], assertions, trace)
    assert ev["earliest"]["lane"] == "trace"
    untimed_lanes = [u["lane"] for u in ev["untimed"]]
    assert untimed_lanes == ["state"]              # no span -> untimed, not ordered


def test_earliest_violation_none_when_nothing_failed():
    assert R._earliest_violation([], {"results": []}, _full_trace()) is None


def test_assertion_lane_mapping():
    assert R._fx_assertion_lane("tool_call") == "tool"
    assert R._fx_assertion_lane("http_result") == "tool"
    assert R._fx_assertion_lane("state_change") == "state"
    assert R._fx_assertion_lane("sequence") == "trace"
    assert R._fx_assertion_lane("count") == "trace"


# --- integration: envelope + rendered sections -----------------------------

def _assert_env() -> dict:
    return {
        "schema": "assert.v1", "exit_code": 1,
        "results": [
            {"id": "refund-once", "kind": "count", "status": "FAIL",
             "deterministic": True, "span_ids": ["s_4"]},
        ],
        "summary": {"deterministic": {"pass": 0, "fail": 1, "inconclusive": 0},
                    "judge": {"pass": 0, "fail": 0}},
    }


def test_forensic_absent_without_trace_is_byte_identical():
    a, ea = R.build_report_html(stereo=_wav())
    b, eb = R.build_report_html(stereo=_wav(), trace=None)
    assert a == b
    assert "forensic" not in ea
    assert "Forensic analysis" not in a


def test_forensic_present_renders_html_section_and_envelope_key():
    html, env = R.build_report_html(stereo=_wav(), trace=_full_trace(),
                                    assertions=_assert_env())
    assert "Forensic analysis" in html
    assert "Per-hop latency waterfall" in html
    assert "Earliest violated invariant" in html
    assert "Interrupted speech" in html
    assert 'details class="card forensic"' in html
    assert "details.forensic summary" in html          # CSS appended
    # envelope carries the additive forensic block, and trace_context is intact
    assert "latency_waterfall" in env["forensic"]
    assert env["forensic"]["earliest_violation"]["earliest"]["lane"] == "trace"
    assert [s["type"] for s in env["trace_context"]["spans"]][0] == \
        "caller_audio_active"


def test_forensic_markdown_mirror():
    md, _ = R.build_report_md(stereo=_wav(), trace=_full_trace(),
                              assertions=_assert_env())
    assert "## Forensic analysis (derived context, not a score)" in md
    assert "### Per-hop latency waterfall" in md
    assert "### Earliest violated invariant" in md
    plain, _ = R.build_report_md(stereo=_wav())
    assert "## Forensic analysis" not in plain


def test_forensic_is_byte_stable():
    a, _ = R.build_report_html(stereo=_wav(), trace=_full_trace(),
                               assertions=_assert_env())
    b, _ = R.build_report_html(stereo=_wav(), trace=_full_trace(),
                               assertions=_assert_env())
    assert a == b


def test_forensic_never_leaks_redacted_interrupted_text():
    trace = {"spans": [
        {"type": "tts_cancel_requested", "time_sec": 2.6, "text_redacted": True,
         "attributes": {"text": "SECRET-UTTERANCE"}},
        {"type": "tts_audio_stopped", "time_sec": 2.9},
    ]}
    html, _ = R.build_report_html(stereo=_wav(), trace=trace)
    assert "Interrupted speech" in html
    assert "SECRET-UTTERANCE" not in html
