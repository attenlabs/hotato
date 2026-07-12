"""``hotato assert``: the deterministic assertion engine (assert.v1).

Pins the honesty properties that are the entire point of this module:
every result carries ``deterministic: true`` (including INCONCLUSIVE, which
reflects absent input, never a guess); the envelope's ``summary`` splits
deterministic from judge counts and never emits a merged score; ``pii``
never echoes raw matched text; ``tool_call`` reads spans, never transcript
text; ``phrase`` absent-mode is a compliance check (PASS when never
present); Luhn validation is exact; and the whole pipeline is byte-stable
across repeated runs on identical input.
"""

import json
import os
from importlib import resources

import pytest

from hotato import assert_ as A

jsonschema = pytest.importorskip("jsonschema")


def _schema():
    return json.loads(
        resources.files("hotato").joinpath("schema", "assert.v1.json").read_text(encoding="utf-8")
    )


def _validate(env):
    jsonschema.validate(instance=env, schema=_schema())


# --- YAML-subset parsing ---------------------------------------------------

PLAN_EXAMPLE = """\
version: 1
assertions:
  - id: refund-confirmed
    kind: outcome
    all_of: [{tool_called: issue_refund}, {phrase: "confirmation number", role: agent}]
  - id: tool-order
    kind: tool_call
    require_order: [verify_identity, lookup_account, issue_refund]
    never_before: {tool: issue_refund, until: verify_identity}
  - id: disclosure
    kind: phrase
    regex: "recorded for quality"
    role: agent
    position: first
  - id: no-ssn-leak
    kind: pii
    detectors: [ssn, card_luhn]
    mode: must_not_leak
"""


def test_parse_assertions_yaml_matches_plan_example():
    doc = A.parse_assertions_yaml(PLAN_EXAMPLE)
    assert doc["version"] == 1
    items = doc["assertions"]
    assert [i["id"] for i in items] == [
        "refund-confirmed", "tool-order", "disclosure", "no-ssn-leak",
    ]
    outcome = items[0]
    assert outcome["all_of"] == [
        {"tool_called": "issue_refund"},
        {"phrase": "confirmation number", "role": "agent"},
    ]
    tool_order = items[1]
    assert tool_order["require_order"] == ["verify_identity", "lookup_account", "issue_refund"]
    assert tool_order["never_before"] == {"tool": "issue_refund", "until": "verify_identity"}
    disclosure = items[2]
    assert disclosure["regex"] == "recorded for quality"
    assert disclosure["role"] == "agent"
    assert disclosure["position"] == "first"
    pii = items[3]
    assert pii["detectors"] == ["ssn", "card_luhn"]
    assert pii["mode"] == "must_not_leak"


def test_parse_assertions_yaml_accepts_equivalent_json():
    text = json.dumps({
        "version": 1,
        "assertions": [
            {"id": "a", "kind": "phrase", "regex": "hello"},
        ],
    })
    doc = A.parse_assertions_yaml(text)
    assert doc["assertions"][0]["regex"] == "hello"


def test_parse_assertions_yaml_rejects_tabs():
    with pytest.raises(ValueError, match="tab"):
        A.parse_assertions_yaml("version: 1\n\tassertions: []\n")


def test_parse_assertions_yaml_rejects_empty():
    with pytest.raises(ValueError):
        A.parse_assertions_yaml("   \n # just a comment\n")


# --- document validation ----------------------------------------------------

def _doc(*assertions, version=1):
    return {"version": version, "assertions": list(assertions)}


def test_validate_missing_version():
    with pytest.raises(ValueError, match="version"):
        A.validate_assertions_doc({"assertions": []})


def test_validate_unsupported_version():
    with pytest.raises(ValueError, match="unsupported"):
        A.validate_assertions_doc(_doc({"id": "a", "kind": "phrase", "regex": "x"}, version=2))


def test_validate_missing_assertions_key():
    with pytest.raises(ValueError, match="assertions"):
        A.validate_assertions_doc({"version": 1})


def test_validate_empty_assertions_list():
    with pytest.raises(ValueError):
        A.validate_assertions_doc(_doc())


def test_validate_duplicate_id():
    with pytest.raises(ValueError, match="duplicate"):
        A.validate_assertions_doc(_doc(
            {"id": "dup", "kind": "phrase", "regex": "x"},
            {"id": "dup", "kind": "phrase", "regex": "y"},
        ))


def test_validate_unknown_kind():
    with pytest.raises(ValueError, match="kind"):
        A.validate_assertions_doc(_doc({"id": "a", "kind": "vibes", "regex": "x"}))


def test_validate_phrase_bad_regex():
    with pytest.raises(ValueError, match="invalid regex"):
        A.validate_assertions_doc(_doc({"id": "a", "kind": "phrase", "regex": "("}))


def test_validate_phrase_bad_position():
    with pytest.raises(ValueError, match="position"):
        A.validate_assertions_doc(_doc(
            {"id": "a", "kind": "phrase", "regex": "x", "position": "middle"}
        ))


def test_validate_pii_bad_detector():
    with pytest.raises(ValueError, match="detector"):
        A.validate_assertions_doc(_doc(
            {"id": "a", "kind": "pii", "detectors": ["bogus"], "mode": "must_not_leak"}
        ))


def test_validate_pii_bad_mode():
    with pytest.raises(ValueError, match="mode"):
        A.validate_assertions_doc(_doc(
            {"id": "a", "kind": "pii", "detectors": ["ssn"], "mode": "warn_only"}
        ))


def test_validate_tool_call_needs_a_check():
    with pytest.raises(ValueError, match="at least one"):
        A.validate_assertions_doc(_doc({"id": "a", "kind": "tool_call"}))


def test_validate_outcome_needs_all_of_or_any_of():
    with pytest.raises(ValueError, match="all_of"):
        A.validate_assertions_doc(_doc({"id": "a", "kind": "outcome"}))


def test_validate_outcome_predicate_needs_exactly_one_key():
    with pytest.raises(ValueError, match="exactly one"):
        A.validate_assertions_doc(_doc({
            "id": "a", "kind": "outcome",
            "all_of": [{"tool_called": "x", "phrase": "y"}],
        }))


# --- Context / build_context ------------------------------------------------

def test_build_context_defaults_to_none_when_nothing_supplied():
    ctx = A.build_context()
    assert ctx.transcript is None
    assert ctx.spans is None
    assert ctx.timing is None


def test_build_context_empty_list_is_distinct_from_absent():
    ctx = A.build_context(transcript=[], spans=[])
    assert ctx.transcript == []
    assert ctx.spans == []


def test_build_context_rejects_both_transcript_forms():
    with pytest.raises(ValueError, match="not both"):
        A.build_context(transcript=[], transcript_path="x.json")


def test_build_context_rejects_both_span_forms():
    with pytest.raises(ValueError, match="not both"):
        A.build_context(spans=[], trace_path="x.jsonl")


def test_build_context_normalizes_turns():
    ctx = A.build_context(transcript=[{"role": "agent", "text": "hi"}])
    assert ctx.transcript == [{"role": "agent", "text": "hi", "start": None, "end": None}]


def test_load_transcript_file_plain_list(tmp_path):
    p = tmp_path / "t.json"
    p.write_text(json.dumps([{"role": "caller", "text": "hi", "start": 0.0, "end": 1.0}]))
    ctx = A.build_context(transcript_path=str(p))
    assert ctx.transcript == [{"role": "caller", "text": "hi", "start": 0.0, "end": 1.0}]


def test_load_transcript_file_segments_shape(tmp_path):
    p = tmp_path / "t.json"
    p.write_text(json.dumps({"segments": [{"start": 0, "end": 1, "text": "hi", "role": "agent"}]}))
    ctx = A.build_context(transcript_path=str(p))
    assert ctx.transcript[0]["text"] == "hi"
    assert ctx.transcript[0]["role"] == "agent"


def test_load_transcript_file_nested_transcript_key(tmp_path):
    p = tmp_path / "t.json"
    p.write_text(json.dumps({"transcript": {"segments": [{"start": 0, "end": 1, "text": "hi", "role": None}]}}))
    ctx = A.build_context(transcript_path=str(p))
    assert ctx.transcript[0]["text"] == "hi"


def test_load_transcript_file_bad_shape_raises(tmp_path):
    p = tmp_path / "t.json"
    p.write_text(json.dumps({"nope": 1}))
    with pytest.raises(ValueError):
        A.build_context(transcript_path=str(p))


@pytest.mark.skipif(not hasattr(os, "mkfifo"), reason="FIFOs are POSIX-only")
def test_load_transcript_file_refuses_fifo_not_hang(tmp_path):
    fifo = tmp_path / "t.json"
    os.mkfifo(str(fifo))
    with pytest.raises(ValueError, match="not a regular file"):
        A.load_transcript_file(str(fifo))


@pytest.mark.skipif(not hasattr(os, "mkfifo"), reason="FIFOs are POSIX-only")
def test_load_spans_file_refuses_fifo_not_hang(tmp_path):
    fifo = tmp_path / "t.jsonl"
    os.mkfifo(str(fifo))
    with pytest.raises(ValueError, match="not a regular file"):
        A.load_spans_file(str(fifo))


def test_load_spans_file_reads_voice_trace_jsonl(tmp_path):
    from hotato import trace as T

    otel_path = tmp_path / "in.jsonl"
    otel_path.write_text(
        '{"type": "tool_call", "start_sec": 1.0, "end_sec": 1.2, "name": "lookup_order"}\n'
    )
    out_path = tmp_path / "vt.jsonl"
    T.ingest_otel(str(otel_path), out_path=str(out_path))
    spans = A.load_spans_file(str(out_path))
    assert spans[0]["type"] == "tool_call"
    assert spans[0]["name"] == "lookup_order"


# --- phrase kind -------------------------------------------------------------

def _turn(role, text, start=0.0, end=1.0):
    return {"role": role, "text": text, "start": start, "end": end}


def test_phrase_pass_any_position():
    a = {"id": "a", "kind": "phrase", "regex": "recorded for quality"}
    ctx = A.build_context(transcript=[_turn("agent", "hi, recorded for quality assurance")])
    r = A.evaluate_assertion(a, ctx)
    assert r == {"id": "a", "kind": "phrase", "deterministic": True, "status": "PASS"}


def test_phrase_fail_when_never_matches():
    a = {"id": "a", "kind": "phrase", "regex": "recorded for quality"}
    ctx = A.build_context(transcript=[_turn("agent", "hello there")])
    r = A.evaluate_assertion(a, ctx)
    assert r["status"] == "FAIL"
    assert r["deterministic"] is True
    assert "reason" in r


def test_phrase_role_filter():
    a = {"id": "a", "kind": "phrase", "regex": "refund", "role": "agent"}
    ctx = A.build_context(transcript=[
        _turn("caller", "I want a refund"),
        _turn("agent", "sure, one moment"),
    ])
    r = A.evaluate_assertion(a, ctx)
    assert r["status"] == "FAIL"  # only caller said "refund"


def test_phrase_position_first_pass():
    a = {"id": "a", "kind": "phrase", "regex": "recorded for quality", "role": "agent", "position": "first"}
    ctx = A.build_context(transcript=[
        _turn("agent", "hi, this call is recorded for quality"),
        _turn("agent", "how can I help"),
    ])
    assert A.evaluate_assertion(a, ctx)["status"] == "PASS"


def test_phrase_position_first_fail_when_disclosure_is_later():
    a = {"id": "a", "kind": "phrase", "regex": "recorded for quality", "role": "agent", "position": "first"}
    ctx = A.build_context(transcript=[
        _turn("agent", "hello"),
        _turn("agent", "by the way this call is recorded for quality"),
    ])
    assert A.evaluate_assertion(a, ctx)["status"] == "FAIL"


def test_phrase_position_last():
    a = {"id": "a", "kind": "phrase", "regex": "bye", "role": "agent", "position": "last"}
    ctx = A.build_context(transcript=[_turn("agent", "hello"), _turn("agent", "bye now")])
    assert A.evaluate_assertion(a, ctx)["status"] == "PASS"


def test_phrase_absent_mode_pass_when_never_present():
    a = {"id": "a", "kind": "phrase", "regex": "\\bssn\\b", "absent": True}
    ctx = A.build_context(transcript=[_turn(None, "have a nice day")])
    r = A.evaluate_assertion(a, ctx)
    assert r["status"] == "PASS"


def test_phrase_absent_mode_fail_when_present():
    a = {"id": "a", "kind": "phrase", "regex": "credit card number"}
    a["absent"] = True
    ctx = A.build_context(transcript=[_turn("caller", "here is my credit card number")])
    r = A.evaluate_assertion(a, ctx)
    assert r["status"] == "FAIL"


def test_phrase_case_sensitivity():
    ctx = A.build_context(transcript=[_turn("agent", "REFUND issued")])
    insensitive = {"id": "a", "kind": "phrase", "regex": "refund"}
    sensitive = {"id": "b", "kind": "phrase", "regex": "refund", "case_sensitive": True}
    assert A.evaluate_assertion(insensitive, ctx)["status"] == "PASS"
    assert A.evaluate_assertion(sensitive, ctx)["status"] == "FAIL"


def test_phrase_inconclusive_when_no_transcript():
    a = {"id": "a", "kind": "phrase", "regex": "x"}
    ctx = A.build_context()  # no transcript at all
    r = A.evaluate_assertion(a, ctx)
    assert r["status"] == "INCONCLUSIVE"
    assert r["deterministic"] is True
    assert "reason" in r


# --- pii kind ----------------------------------------------------------------

def test_pii_pass_when_clean():
    a = {"id": "a", "kind": "pii", "detectors": ["ssn", "card_luhn", "email", "phone"], "mode": "must_not_leak"}
    ctx = A.build_context(transcript=[_turn("caller", "I'd like to check my order status please")])
    r = A.evaluate_assertion(a, ctx)
    assert r["status"] == "PASS"
    assert "hits" not in r


def test_pii_detects_ssn_and_redacts():
    a = {"id": "a", "kind": "pii", "detectors": ["ssn"], "mode": "must_not_leak"}
    ctx = A.build_context(transcript=[_turn("caller", "my ssn is 219-09-9999")])
    r = A.evaluate_assertion(a, ctx)
    assert r["status"] == "FAIL"
    assert r["hits"] == [{"detector": "ssn", "turn": 0, "role": "caller"}]
    # the raw SSN digits must never appear anywhere in the result
    dumped = json.dumps(r)
    assert "219-09-9999" not in dumped
    assert "219099999" not in dumped
    assert r["redacted_transcript"][0]["text"] == "my ssn is [REDACTED]"


def test_pii_detects_valid_luhn_card():
    a = {"id": "a", "kind": "pii", "detectors": ["card_luhn"], "mode": "must_not_leak"}
    ctx = A.build_context(transcript=[_turn("caller", "card number 4111 1111 1111 1111 please")])
    r = A.evaluate_assertion(a, ctx)
    assert r["status"] == "FAIL"
    assert r["hits"][0]["detector"] == "card_luhn"


def test_pii_ignores_invalid_luhn_card():
    a = {"id": "a", "kind": "pii", "detectors": ["card_luhn"], "mode": "must_not_leak"}
    # last digit flipped -> fails the Luhn checksum -> not a hit
    ctx = A.build_context(transcript=[_turn("caller", "card number 4111 1111 1111 1112")])
    r = A.evaluate_assertion(a, ctx)
    assert r["status"] == "PASS"


def test_pii_detects_email():
    a = {"id": "a", "kind": "pii", "detectors": ["email"], "mode": "must_not_leak"}
    ctx = A.build_context(transcript=[_turn("caller", "reach me at jane.doe@example.com")])
    r = A.evaluate_assertion(a, ctx)
    assert r["status"] == "FAIL"
    assert "jane.doe@example.com" not in json.dumps(r)


def test_pii_detects_phone():
    a = {"id": "a", "kind": "pii", "detectors": ["phone"], "mode": "must_not_leak"}
    ctx = A.build_context(transcript=[_turn("caller", "call me back at 415-555-0100")])
    r = A.evaluate_assertion(a, ctx)
    assert r["status"] == "FAIL"


def test_pii_inconclusive_when_no_transcript():
    a = {"id": "a", "kind": "pii", "detectors": ["ssn"], "mode": "must_not_leak"}
    ctx = A.build_context()
    r = A.evaluate_assertion(a, ctx)
    assert r["status"] == "INCONCLUSIVE"
    assert r["deterministic"] is True


def test_luhn_valid_known_test_numbers():
    assert A._luhn_valid("4111111111111111")
    assert not A._luhn_valid("4111111111111112")
    assert A._luhn_valid("79927398713")  # canonical Luhn example


# --- policy kind -------------------------------------------------------------

def test_policy_pass_on_clean_call():
    a = {"id": "a", "kind": "policy"}
    ctx = A.build_context(transcript=[
        _turn("agent", "hi, this call is recorded for quality and training purposes"),
        _turn("caller", "ok great"),
    ])
    r = A.evaluate_assertion(a, ctx)
    assert r["status"] == "PASS"
    assert r["pack"] == {"name": "default", "version": 1}


def test_policy_fails_on_banned_language():
    a = {"id": "a", "kind": "policy"}
    ctx = A.build_context(transcript=[
        _turn("agent", "this is recorded for quality"),
        _turn("agent", "well hell, that's odd"),
    ])
    r = A.evaluate_assertion(a, ctx)
    assert r["status"] == "FAIL"
    rule_ids = [m["rule"] for m in r["matched_rules"]]
    assert "no-profanity" in rule_ids


def test_policy_fails_on_missing_required_disclosure():
    a = {"id": "a", "kind": "policy"}
    ctx = A.build_context(transcript=[_turn("agent", "hello, how can I help?")])
    r = A.evaluate_assertion(a, ctx)
    assert r["status"] == "FAIL"
    rule_ids = [m["rule"] for m in r["matched_rules"]]
    assert "recording-disclosure" in rule_ids


def test_policy_custom_pack_path(tmp_path):
    pack = {
        "name": "acme-v1", "version": 3,
        "rules": [{"id": "no-brand-x", "type": "banned", "regex": r"\bcompetitor\b"}],
    }
    p = tmp_path / "pack.json"
    p.write_text(json.dumps(pack))
    a = {"id": "a", "kind": "policy", "pack_path": str(p)}
    ctx = A.build_context(transcript=[_turn("agent", "we beat our competitor on price")])
    r = A.evaluate_assertion(a, ctx)
    assert r["status"] == "FAIL"
    assert r["pack"] == {"name": "acme-v1", "version": 3}


def test_policy_unknown_builtin_pack_raises():
    with pytest.raises(ValueError, match="unknown"):
        A.load_policy_pack("nonexistent")


# --- tool_call kind (spans only, never transcript) ---------------------------

def _tc_span(idx, name, args=None):
    s = {"type": "tool_call", "start_sec": float(idx), "end_sec": float(idx) + 0.5, "name": name}
    if args is not None:
        s["arguments"] = args
    return s


def test_tool_call_reads_spans_not_transcript():
    a = {"id": "a", "kind": "tool_call", "name": "issue_refund"}
    # transcript LIES and claims the tool ran; spans say it never did.
    ctx = A.build_context(
        transcript=[_turn("agent", "I called issue_refund and it succeeded")],
        spans=[_tc_span(0, "lookup_account")],
    )
    r = A.evaluate_assertion(a, ctx)
    assert r["status"] == "FAIL"
    assert "never called" in r["reason"]


def test_tool_call_name_present_pass():
    a = {"id": "a", "kind": "tool_call", "name": "issue_refund"}
    ctx = A.build_context(spans=[_tc_span(0, "issue_refund")])
    r = A.evaluate_assertion(a, ctx)
    assert r["status"] == "PASS"
    assert r["span_ids"] == ["s_0"]


def test_tool_call_args_subset_match():
    a = {"id": "a", "kind": "tool_call", "name": "issue_refund", "args_subset": {"amount": 50}}
    ctx = A.build_context(spans=[_tc_span(0, "issue_refund", args={"amount": 50, "currency": "usd"})])
    r = A.evaluate_assertion(a, ctx)
    assert r["status"] == "PASS"


def test_tool_call_args_subset_mismatch():
    a = {"id": "a", "kind": "tool_call", "name": "issue_refund", "args_subset": {"amount": 999}}
    ctx = A.build_context(spans=[_tc_span(0, "issue_refund", args={"amount": 50})])
    r = A.evaluate_assertion(a, ctx)
    assert r["status"] == "FAIL"
    assert "arguments matching" in r["reason"]


def test_tool_call_count_bounds_exact():
    a = {"id": "a", "kind": "tool_call", "name": "lookup_account", "count": 2}
    ctx = A.build_context(spans=[_tc_span(0, "lookup_account"), _tc_span(1, "lookup_account")])
    assert A.evaluate_assertion(a, ctx)["status"] == "PASS"


def test_tool_call_count_bounds_min_max():
    a = {"id": "a", "kind": "tool_call", "name": "lookup_account", "count": {"max": 1}}
    ctx = A.build_context(spans=[_tc_span(0, "lookup_account"), _tc_span(1, "lookup_account")])
    r = A.evaluate_assertion(a, ctx)
    assert r["status"] == "FAIL"


def test_tool_call_require_order_pass():
    a = {"id": "a", "kind": "tool_call", "require_order": ["verify_identity", "issue_refund"]}
    ctx = A.build_context(spans=[
        _tc_span(0, "verify_identity"), _tc_span(1, "issue_refund"),
    ])
    assert A.evaluate_assertion(a, ctx)["status"] == "PASS"


def test_tool_call_require_order_fail_reports_reason():
    a = {"id": "a", "kind": "tool_call", "require_order": ["verify_identity", "issue_refund"]}
    ctx = A.build_context(spans=[
        _tc_span(0, "issue_refund"), _tc_span(1, "verify_identity"),
    ])
    r = A.evaluate_assertion(a, ctx)
    assert r["status"] == "FAIL"
    assert "verify_identity" in r["reason"] or "issue_refund" in r["reason"]


def test_tool_call_never_before_violation():
    a = {"id": "a", "kind": "tool_call", "never_before": {"tool": "issue_refund", "until": "verify_identity"}}
    ctx = A.build_context(spans=[
        _tc_span(0, "issue_refund"),  # too early: verify_identity hasn't happened
        _tc_span(1, "verify_identity"),
    ])
    r = A.evaluate_assertion(a, ctx)
    assert r["status"] == "FAIL"
    assert r["span_ids"] == ["s_0"]


def test_tool_call_never_before_holds():
    a = {"id": "a", "kind": "tool_call", "never_before": {"tool": "issue_refund", "until": "verify_identity"}}
    ctx = A.build_context(spans=[
        _tc_span(0, "verify_identity"), _tc_span(1, "issue_refund"),
    ])
    assert A.evaluate_assertion(a, ctx)["status"] == "PASS"


def test_tool_call_inconclusive_when_no_trace():
    a = {"id": "a", "kind": "tool_call", "name": "issue_refund"}
    ctx = A.build_context()  # no spans at all -- not even an empty list
    r = A.evaluate_assertion(a, ctx)
    assert r["status"] == "INCONCLUSIVE"
    assert r["deterministic"] is True
    assert "trace" in r["reason"]


def test_tool_call_fails_not_inconclusive_when_trace_present_but_empty():
    a = {"id": "a", "kind": "tool_call", "name": "issue_refund"}
    ctx = A.build_context(spans=[])  # a trace WAS ingested, it just has no spans
    r = A.evaluate_assertion(a, ctx)
    assert r["status"] == "FAIL"


# --- outcome kind ------------------------------------------------------------

def test_outcome_all_of_full_pass():
    a = {
        "id": "a", "kind": "outcome",
        "all_of": [{"tool_called": "issue_refund"}, {"phrase": "confirmation number", "role": "agent"}],
    }
    ctx = A.build_context(
        transcript=[_turn("agent", "here is your confirmation number 12345")],
        spans=[_tc_span(0, "issue_refund")],
    )
    r = A.evaluate_assertion(a, ctx)
    assert r["status"] == "PASS"
    assert r["met"] == 2 and r["of"] == 2


def test_outcome_all_of_partial_fail_reports_fraction():
    a = {
        "id": "a", "kind": "outcome",
        "all_of": [{"tool_called": "issue_refund"}, {"phrase": "confirmation number"}],
    }
    ctx = A.build_context(transcript=[_turn("agent", "sorry, no can do")], spans=[])
    r = A.evaluate_assertion(a, ctx)
    assert r["status"] == "FAIL"
    assert r["met"] == 0 and r["of"] == 2


def test_outcome_any_of_pass_with_one_true():
    a = {"id": "a", "kind": "outcome", "any_of": [{"tool_called": "issue_refund"}, {"tool_called": "escalate"}]}
    ctx = A.build_context(spans=[_tc_span(0, "escalate")])
    r = A.evaluate_assertion(a, ctx)
    assert r["status"] == "PASS"
    assert r["met"] == 1 and r["of"] == 2


def test_outcome_field_present_true():
    a = {"id": "a", "kind": "outcome", "all_of": [{"field_present": "measurements.caller_onset_sec"}]}
    ctx = A.build_context(timing={"measurements": {"caller_onset_sec": 1.2}})
    r = A.evaluate_assertion(a, ctx)
    assert r["status"] == "PASS"


def test_outcome_field_present_false_when_null():
    a = {"id": "a", "kind": "outcome", "all_of": [{"field_present": "measurements.caller_onset_sec"}]}
    ctx = A.build_context(timing={"measurements": {"caller_onset_sec": None}})
    r = A.evaluate_assertion(a, ctx)
    assert r["status"] == "FAIL"


def test_outcome_field_present_over_event_list():
    a = {"id": "a", "kind": "outcome", "all_of": [{"field_present": "verdict.did_yield"}]}
    ctx = A.build_context(timing=[{"verdict": {}}, {"verdict": {"did_yield": True}}])
    r = A.evaluate_assertion(a, ctx)
    assert r["status"] == "PASS"


def test_outcome_inconclusive_when_a_predicate_context_is_missing():
    a = {
        "id": "a", "kind": "outcome",
        "all_of": [{"tool_called": "issue_refund"}, {"field_present": "measurements.x"}],
    }
    # spans present (tool_called resolvable), but timing was never supplied.
    ctx = A.build_context(spans=[_tc_span(0, "issue_refund")])
    r = A.evaluate_assertion(a, ctx)
    assert r["status"] == "INCONCLUSIVE"
    assert r["met"] == 1  # the one resolvable predicate DID pass
    assert r["of"] == 2


# --- envelope / run_assertions -----------------------------------------------

def test_run_assertions_all_pass_exit_0_and_validates():
    ctx = A.build_context(
        transcript=[_turn("agent", "hi, recorded for quality, here is your confirmation number 42")],
        spans=[_tc_span(0, "issue_refund")],
    )
    env = A.run_assertions(_doc(
        {"id": "disclosure", "kind": "phrase", "regex": "recorded for quality", "role": "agent"},
        {"id": "refunded", "kind": "tool_call", "name": "issue_refund"},
    ), ctx)
    assert env["schema"] == "assert.v1"
    assert env["exit_code"] == 0
    assert env["summary"]["deterministic"] == {"pass": 2, "fail": 0, "inconclusive": 0}
    assert env["summary"]["judge"] == {"pass": 0, "fail": 0}
    assert all(r["deterministic"] is True for r in env["results"])
    _validate(env)


def test_run_assertions_any_fail_forces_exit_1():
    ctx = A.build_context(transcript=[_turn("agent", "nope")], spans=[])
    env = A.run_assertions(_doc(
        {"id": "a", "kind": "phrase", "regex": "recorded for quality"},
    ), ctx)
    assert env["exit_code"] == 1
    _validate(env)


def test_run_assertions_inconclusive_only_does_not_force_exit_1():
    ctx = A.build_context()  # nothing supplied
    env = A.run_assertions(_doc(
        {"id": "a", "kind": "phrase", "regex": "x"},
    ), ctx)
    assert env["results"][0]["status"] == "INCONCLUSIVE"
    assert env["exit_code"] == 0
    _validate(env)


def test_run_assertions_malformed_doc_raises_before_any_evaluation():
    ctx = A.build_context(transcript=[_turn("agent", "hi")])
    with pytest.raises(ValueError):
        A.run_assertions({"version": 1, "assertions": [{"id": "a", "kind": "nope"}]}, ctx)


def test_summary_never_carries_a_merged_score_field():
    ctx = A.build_context(transcript=[_turn("agent", "hello")])
    env = A.run_assertions(_doc({"id": "a", "kind": "phrase", "regex": "hello"}), ctx)
    dumped = json.dumps(env)
    assert "overall_score" not in dumped
    assert "\"score\"" not in dumped


def test_run_assertions_from_yaml_end_to_end():
    ctx = A.build_context(transcript=[
        _turn("agent", "this call is recorded for quality assurance"),
    ])
    env = A.run_assertions_from_yaml(
        "version: 1\nassertions:\n  - id: disclosure\n    kind: phrase\n"
        "    regex: \"recorded for quality\"\n    role: agent\n",
        ctx,
    )
    assert env["results"][0]["status"] == "PASS"


def test_run_assertions_from_file(tmp_path):
    p = tmp_path / "assertions.yaml"
    p.write_text("version: 1\nassertions:\n  - id: a\n    kind: phrase\n    regex: hi\n")
    ctx = A.build_context(transcript=[_turn(None, "hi there")])
    env = A.run_assertions_from_file(str(p), ctx)
    assert env["results"][0]["status"] == "PASS"


def test_byte_stable_across_repeated_runs():
    ctx = A.build_context(
        transcript=[_turn("agent", "recorded for quality, confirmation number 9")],
        spans=[_tc_span(0, "issue_refund", args={"amount": 10})],
    )
    doc = _doc(
        {"id": "a", "kind": "phrase", "regex": "recorded for quality", "role": "agent"},
        {"id": "b", "kind": "tool_call", "name": "issue_refund", "args_subset": {"amount": 10}},
        {"id": "c", "kind": "outcome", "all_of": [{"tool_called": "issue_refund"}]},
    )
    env1 = A.run_assertions(doc, ctx)
    env2 = A.run_assertions(doc, ctx)
    assert json.dumps(env1, sort_keys=True) == json.dumps(env2, sort_keys=True)


def test_full_result_shape_matches_schema_for_every_kind():
    ctx = A.build_context(
        transcript=[_turn("agent", "recorded for quality. my ssn is 219-09-9999. hell no")],
        spans=[_tc_span(0, "issue_refund")],
        timing={"measurements": {"x": 1}},
    )
    doc = _doc(
        {"id": "p", "kind": "phrase", "regex": "recorded for quality", "role": "agent"},
        {"id": "q", "kind": "pii", "detectors": ["ssn"], "mode": "must_not_leak"},
        {"id": "r", "kind": "policy"},
        {"id": "s", "kind": "tool_call", "name": "issue_refund"},
        {"id": "t", "kind": "outcome", "all_of": [{"field_present": "measurements.x"}]},
    )
    env = A.run_assertions(doc, ctx)
    _validate(env)
    kinds = {r["kind"] for r in env["results"]}
    # The original five deterministic kinds. The Phase-1 expanded kinds are
    # exercised in tests/test_assert_expanded_kinds.py; this test pins that the
    # five founding kinds each still produce a schema-valid result.
    assert kinds == {"phrase", "pii", "policy", "tool_call", "outcome"}


# --- Phase-1: the optional `dimension` TAG propagates onto the result -------

def test_dimension_tag_propagates_onto_result():
    """An assertion's optional ``dimension`` (one of the five report dimensions)
    is copied verbatim onto its assert.v1 result. An assertion with no dimension
    yields a result with NO dimension -- additive, byte-identical to before."""
    ctx = A.build_context(
        transcript=[_turn("agent", "recorded for quality")],
        spans=[_tc_span(0, "issue_refund")],
    )
    doc = _doc(
        {"id": "p", "kind": "phrase", "regex": "recorded for quality",
         "role": "agent", "dimension": "policy"},
        {"id": "s", "kind": "tool_call", "name": "issue_refund",
         "dimension": "outcome"},
        {"id": "u", "kind": "phrase", "regex": "recorded for quality",
         "role": "agent"},  # untagged
    )
    env = A.run_assertions(doc, ctx)
    _validate(env)  # the result-level `dimension` enum is proven schema-valid
    by_id = {r["id"]: r for r in env["results"]}
    assert by_id["p"]["dimension"] == "policy"
    assert by_id["s"]["dimension"] == "outcome"
    assert "dimension" not in by_id["u"]


def test_untagged_result_byte_identical_to_before_dimension_existed():
    """A result for an assertion with no dimension is byte-for-byte what it was
    before this feature -- the dimension key is only ever ADDED when present."""
    ctx = A.build_context(transcript=[_turn("agent", "recorded for quality")])
    tagged = A.evaluate_assertion(
        {"id": "p", "kind": "phrase", "regex": "recorded for quality",
         "role": "agent", "dimension": "policy"}, ctx)
    untagged = A.evaluate_assertion(
        {"id": "p", "kind": "phrase", "regex": "recorded for quality",
         "role": "agent"}, ctx)
    assert "dimension" not in untagged
    assert tagged == dict(untagged, dimension="policy")


def test_all_five_dimensions_accepted():
    for dim in A.RESULT_DIMENSIONS:
        doc = _doc({"id": "a", "kind": "phrase", "regex": "x", "dimension": dim})
        version, items = A.validate_assertions_doc(doc)
        assert items[0]["dimension"] == dim


def test_bad_dimension_value_rejected():
    with pytest.raises(ValueError, match="dimension"):
        A.validate_assertions_doc(
            _doc({"id": "a", "kind": "phrase", "regex": "x",
                  "dimension": "vibes"}))
