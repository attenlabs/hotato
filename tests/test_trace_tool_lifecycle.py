"""First-class tool-lifecycle span types: ``tool_timeout`` and ``tool_retry``.

A timed-out-then-retried tool is the zombie-tool / double-fire failure class
(pipecat#4936, livekit#3702): a tool passes its deadline, a retry re-invokes
it, and BOTH the original and the retry execute on the backend, so one spoken
"refund issued" hides two real refunds. This pins that these two events are
now first-class, queryable spans -- carrying the tool ``name`` and the retry
``attempt`` number out on the span itself, not smuggled into the generic
``attributes`` bag -- and that the double-fire pattern is therefore assertable
end to end (ingest -> ``sequence`` / ``count``) with no new assertion kind.
"""

from __future__ import annotations

import json
import os

from hotato import assert_ as A
from hotato import trace as _trace

ZOMBIE_OTEL = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                           "data", "otel", "zombie-tool.otel.jsonl")


def test_canonical_span_types_lists_the_tool_lifecycle_spans():
    assert "tool_timeout" in _trace.CANONICAL_SPAN_TYPES
    assert "tool_retry" in _trace.CANONICAL_SPAN_TYPES
    assert _trace.TOOL_LIFECYCLE_SPAN_TYPES == (
        "tool_call", "tool_timeout", "tool_retry")


def _ingest(tmp_path, src=ZOMBIE_OTEL, **kw):
    out = tmp_path / "voice_trace.jsonl"
    _trace.ingest_otel(src, out_path=str(out), **kw)
    return _trace.load_voice_trace_jsonl(str(out))


def test_ingest_keeps_timeout_and_retry_first_class(tmp_path):
    vt = _ingest(tmp_path)
    types = [s["type"] for s in vt["spans"]]
    assert types.count("tool_call") == 2
    assert "tool_timeout" in types
    assert "tool_retry" in types

    timeout = next(s for s in vt["spans"] if s["type"] == "tool_timeout")
    # name + latency ride first-class on the span, never in attributes.
    assert timeout["name"] == "issue_refund"
    assert timeout["latency_ms"] == 5000
    assert "attributes" not in timeout

    retry = next(s for s in vt["spans"] if s["type"] == "tool_retry")
    assert retry["name"] == "issue_refund"
    assert retry["attempt"] == 2
    assert "attributes" not in retry


def test_export_round_trips_the_retry_attempt(tmp_path):
    vt = _ingest(tmp_path)
    # Round-trip: re-ingest the JSONL we wrote and confirm attempt survives.
    src2 = tmp_path / "roundtrip.jsonl"
    _trace._atomic_write(str(src2), _trace._dump_voice_trace_jsonl(vt))
    vt2 = _trace.load_voice_trace_jsonl(str(src2))
    retry = next(s for s in vt2["spans"] if s["type"] == "tool_retry")
    assert retry["attempt"] == 2


def test_standard_otel_json_maps_dotted_names_and_lifts_attempt(tmp_path):
    doc = {
        "resourceSpans": [{
            "resource": {"attributes": [
                {"key": "stack", "value": {"stringValue": "livekit"}}]},
            "scopeSpans": [{"spans": [
                {"name": "tool.timeout", "startTimeUnixNano": "1000000000",
                 "attributes": [
                     {"key": "tool.name", "value": {"stringValue": "issue_refund"}},
                     {"key": "latency_ms", "value": {"intValue": 5000}}]},
                {"name": "tool.retry", "startTimeUnixNano": "1100000000",
                 "attributes": [
                     {"key": "tool.name", "value": {"stringValue": "issue_refund"}},
                     {"key": "attempt", "value": {"intValue": 2}}]},
            ]}],
        }],
    }
    src = tmp_path / "std.otel.json"
    src.write_text(json.dumps(doc), encoding="utf-8")
    vt = _ingest(tmp_path, src=str(src))
    assert vt["source"]["format"] == "otel-json"
    timeout = next(s for s in vt["spans"] if s["type"] == "tool_timeout")
    assert timeout["name"] == "issue_refund"
    retry = next(s for s in vt["spans"] if s["type"] == "tool_retry")
    assert retry["name"] == "issue_refund"
    assert retry["attempt"] == 2


# --- the double-fire failure class is now assertable end to end ------------

def _ctx_from_zombie(tmp_path):
    vt = _ingest(tmp_path)
    return A.build_context(spans=vt["spans"])


def test_count_of_tool_retry_span_type_is_assertable(tmp_path):
    ctx = _ctx_from_zombie(tmp_path)
    a = {"id": "no-retries", "kind": "count", "span_type": "tool_retry",
         "count": {"max": 0}}
    A.validate_assertions_doc({"version": 1, "assertions": [a]})
    r = A.evaluate_assertion(a, ctx)
    assert r["status"] == "FAIL"          # one retry happened; max was 0
    assert r["observed"] == 1


def test_double_fire_count_of_tool_call_fails(tmp_path):
    ctx = _ctx_from_zombie(tmp_path)
    a = {"id": "refund-once", "kind": "count", "tool": "issue_refund",
         "count": {"max": 1}}
    r = A.evaluate_assertion(a, ctx)
    assert r["status"] == "FAIL"          # issue_refund fired twice
    assert r["observed"] == 2


def test_zombie_tool_sequence_matches(tmp_path):
    ctx = _ctx_from_zombie(tmp_path)
    a = {
        "id": "zombie-pattern", "kind": "sequence",
        "steps": [
            {"tool": "issue_refund"},
            {"span_type": "tool_timeout"},
            {"span_type": "tool_retry"},
            {"tool": "issue_refund"},
        ],
    }
    A.validate_assertions_doc({"version": 1, "assertions": [a]})
    r = A.evaluate_assertion(a, ctx)
    assert r["status"] == "PASS"
    # Four spans bound the pattern, by their stable synthetic ids.
    assert len(r["span_ids"]) == 4
