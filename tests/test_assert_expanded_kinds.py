"""Phase-1 expanded deterministic assertion kinds (assert.v1, Slice 2).

Pins the honesty properties that are the whole point of the expanded kinds:

* each new kind (tool_result, tool_error, state, state_change, handoff, dtmf,
  termination, latency, timing_contract, entity_accuracy, sequence, count)
  reports PASS / FAIL / INCONCLUSIVE deterministically, and reports
  INCONCLUSIVE -- never a guessed PASS/FAIL -- when its required input is
  absent;
* the Authority 1 & 2 kinds (tool_result, tool_error, state, state_change)
  read ONLY the authenticated trace spans / the state adapter, never the
  transcript, and have no model code path -- so an agent's spoken claim can
  never satisfy one (proven structurally below);
* human_rubric / judge_rubric are NAMED but quarantined -- INCONCLUSIVE with
  "rubric engine not built (Phase 3)", no LLM path;
* the new kinds validate against schema/assert.v1.json;
* the original five kinds are byte-unchanged.
"""

from __future__ import annotations

import inspect
import json
from importlib import resources

import pytest

from hotato import assert_ as A
from hotato.state_adapter import MockStateAdapter


# --- fixtures / helpers -----------------------------------------------------

def _turn(role, text, start=0.0, end=1.0):
    return {"role": role, "text": text, "start": start, "end": end}


def _tool_span(idx, name, *, result=None, error=None, latency_ms=None,
               args=None, status=None, ok=None):
    s = {"type": "tool_call", "start_sec": float(idx), "end_sec": float(idx) + 0.5,
         "name": name}
    if result is not None:
        s["result"] = result
    if error is not None:
        s["error"] = error
    if latency_ms is not None:
        s["latency_ms"] = latency_ms
    if args is not None:
        s["arguments"] = args
    if status is not None:
        s["status"] = status
    if ok is not None:
        s["ok"] = ok
    return s


def _point(idx, typ, **fields):
    s = {"type": typ, "time_sec": float(idx)}
    s.update(fields)
    return s


def _schema():
    return json.loads(
        resources.files("hotato").joinpath("schema", "assert.v1.json")
        .read_text(encoding="utf-8")
    )


def _ev(a, ctx):
    return A.evaluate_assertion(a, ctx)


# =========================================================================
# kind registration: the schema enum, KINDS, and the validator all agree
# =========================================================================

def test_all_expanded_kinds_are_registered_and_evaluable():
    expanded = (
        "tool_result", "tool_error", "state", "state_change", "handoff",
        "dtmf", "termination", "latency", "timing_contract", "entity_accuracy",
        "sequence", "count",
    )
    for k in expanded:
        assert k in A.KINDS
        assert k in A._EVALUATORS
    for k in A.RUBRIC_KINDS:
        assert k in A.ALL_KINDS
        assert k not in A.KINDS               # not in the deterministic wall
        assert k in A._EVALUATORS


def test_schema_enum_lists_every_kind():
    enum = set(_schema()["definitions"]["result"]["properties"]["kind"]["enum"])
    assert enum == set(A.ALL_KINDS)


# =========================================================================
# tool_result -- Authority 1 (reads tool spans' result, never the transcript)
# =========================================================================

def test_tool_result_pass_when_result_subset_matches():
    a = {"id": "a", "kind": "tool_result", "name": "issue_refund",
         "result_subset": {"status": "ok", "amount": 50}}
    ctx = A.build_context(spans=[
        _tool_span(0, "issue_refund", result={"status": "ok", "amount": 50, "ref": "R1"}),
    ])
    r = _ev(a, ctx)
    assert r["status"] == "PASS"
    assert r["deterministic"] is True
    assert r["span_ids"] == ["s_0"]


def test_tool_result_fail_when_called_but_result_mismatches():
    a = {"id": "a", "kind": "tool_result", "name": "issue_refund",
         "result_subset": {"status": "ok"}}
    ctx = A.build_context(spans=[
        _tool_span(0, "issue_refund", result={"status": "declined"}),
    ])
    r = _ev(a, ctx)
    assert r["status"] == "FAIL"
    assert "no result matched" in r["reason"]


def test_tool_result_fail_when_tool_never_produced_a_result():
    a = {"id": "a", "kind": "tool_result", "name": "issue_refund"}
    ctx = A.build_context(spans=[_tool_span(0, "lookup_order", result={"x": 1})])
    r = _ev(a, ctx)
    assert r["status"] == "FAIL"
    assert "no result span" in r["reason"]


def test_tool_result_inconclusive_without_a_trace():
    a = {"id": "a", "kind": "tool_result", "name": "issue_refund"}
    r = _ev(a, A.build_context())            # no spans supplied
    assert r["status"] == "INCONCLUSIVE"
    assert r["deterministic"] is True


# =========================================================================
# tool_error -- Authority 1
# =========================================================================

def test_tool_error_pass_when_tool_errored():
    a = {"id": "a", "kind": "tool_error", "name": "charge_card"}
    ctx = A.build_context(spans=[_tool_span(0, "charge_card", error="gateway timeout")])
    assert _ev(a, ctx)["status"] == "PASS"


def test_tool_error_pass_via_status_error_and_ok_false():
    ctx = A.build_context(spans=[
        _tool_span(0, "a", status="error"), _tool_span(1, "b", ok=False),
    ])
    assert _ev({"id": "x", "kind": "tool_error", "name": "a"}, ctx)["status"] == "PASS"
    assert _ev({"id": "y", "kind": "tool_error", "name": "b"}, ctx)["status"] == "PASS"


def test_tool_error_fail_when_tool_did_not_error():
    a = {"id": "a", "kind": "tool_error", "name": "charge_card"}
    ctx = A.build_context(spans=[_tool_span(0, "charge_card", result={"ok": 1})])
    assert _ev(a, ctx)["status"] == "FAIL"


def test_tool_error_absent_mode_passes_when_no_error():
    a = {"id": "a", "kind": "tool_error", "name": "charge_card", "absent": True}
    ctx = A.build_context(spans=[_tool_span(0, "charge_card", result={"ok": 1})])
    assert _ev(a, ctx)["status"] == "PASS"


def test_tool_error_inconclusive_without_a_trace():
    a = {"id": "a", "kind": "tool_error", "name": "charge_card"}
    assert _ev(a, A.build_context())["status"] == "INCONCLUSIVE"


# =========================================================================
# state / state_change -- Authority 2 (post-call state adapter)
# =========================================================================

def _adapter():
    return MockStateAdapter({
        "orders": [{"id": "A1", "status": "refunded", "amount": 50}],
        "account": {
            "before": [{"id": "u1", "balance": 100}],
            "after": [{"id": "u1", "balance": 0}],
        },
    })


def test_state_pass_when_record_matches_expected():
    a = {"id": "a", "kind": "state", "resource": "orders",
         "filters": {"id": "A1"}, "expect": {"status": "refunded"}}
    r = _ev(a, A.build_context(state_adapter=_adapter()))
    assert r["status"] == "PASS"
    assert r["deterministic"] is True


def test_state_fail_when_field_mismatches():
    a = {"id": "a", "kind": "state", "resource": "orders",
         "filters": {"id": "A1"}, "expect": {"status": "open"}}
    r = _ev(a, A.build_context(state_adapter=_adapter()))
    assert r["status"] == "FAIL"
    assert "status" in r["reason"]


def test_state_fail_when_no_record_but_adapter_present():
    a = {"id": "a", "kind": "state", "resource": "orders",
         "filters": {"id": "NOPE"}, "expect": {"status": "refunded"}}
    r = _ev(a, A.build_context(state_adapter=_adapter()))
    assert r["status"] == "FAIL"          # queryable, but genuinely absent record


def test_state_inconclusive_without_an_adapter():
    a = {"id": "a", "kind": "state", "resource": "orders", "expect": {"status": "x"}}
    r = _ev(a, A.build_context())         # no adapter supplied
    assert r["status"] == "INCONCLUSIVE"


def test_state_change_pass_on_before_after_delta():
    a = {"id": "a", "kind": "state_change", "resource": "account",
         "filters": {"id": "u1"}, "field": "balance", "from": 100, "to": 0}
    r = _ev(a, A.build_context(state_adapter=_adapter()))
    assert r["status"] == "PASS"
    assert r["delta"] == {"field": "balance", "before": 100, "after": 0}


def test_state_change_fail_when_after_value_wrong():
    a = {"id": "a", "kind": "state_change", "resource": "account",
         "filters": {"id": "u1"}, "field": "balance", "to": 999}
    assert _ev(a, A.build_context(state_adapter=_adapter()))["status"] == "FAIL"


def test_state_change_changed_flag():
    unchanged = MockStateAdapter({"account": {
        "before": [{"id": "u1", "balance": 5}], "after": [{"id": "u1", "balance": 5}]}})
    a = {"id": "a", "kind": "state_change", "resource": "account",
         "filters": {"id": "u1"}, "field": "balance", "changed": True}
    assert _ev(a, A.build_context(state_adapter=unchanged))["status"] == "FAIL"


def test_state_change_inconclusive_when_snapshot_missing():
    only_after = MockStateAdapter({"account": {"after": [{"id": "u1", "balance": 0}]}})
    a = {"id": "a", "kind": "state_change", "resource": "account",
         "filters": {"id": "u1"}, "field": "balance", "to": 0}
    r = _ev(a, A.build_context(state_adapter=only_after))
    assert r["status"] == "INCONCLUSIVE"
    assert "before" in r["reason"]


# =========================================================================
# HARD INVARIANT: Authority 1 & 2 kinds cannot be satisfied by a spoken claim
# =========================================================================

_LYING_TRANSCRIPT = [
    _turn("agent", "I issued the refund, cancelled the order, and updated your "
                   "balance to zero -- all done successfully."),
]


@pytest.mark.parametrize("a", [
    {"id": "a", "kind": "tool_result", "name": "issue_refund"},
    {"id": "a", "kind": "tool_error", "name": "issue_refund", "absent": True},
    {"id": "a", "kind": "state", "resource": "orders", "expect": {"status": "refunded"}},
    {"id": "a", "kind": "state_change", "resource": "account", "field": "balance", "to": 0},
])
def test_spoken_claim_alone_never_satisfies_authority_kinds(a):
    # A transcript full of confident success claims, but NO authenticated
    # evidence (no spans, no state adapter). None of these may PASS.
    ctx = A.build_context(transcript=_LYING_TRANSCRIPT)
    r = _ev(a, ctx)
    assert r["status"] == "INCONCLUSIVE"     # absent evidence, never a guessed PASS
    assert r["status"] != "PASS"


def test_authority_kinds_evaluators_never_read_the_transcript_source():
    # Structural enforcement: the Authority 1 & 2 evaluators have no
    # ``ctx.transcript`` read and no model call in their source at all, so no
    # LLM verdict / spoken claim path can exist to satisfy them.
    fns = {
        "tool_result": A._eval_tool_result,
        "tool_error": A._eval_tool_error,
        "state": A._eval_state,
        "state_change": A._eval_state_change,
    }
    assert set(fns) == set(A._AUTHORITY_1_2_KINDS)
    for kind, fn in fns.items():
        src = inspect.getsource(fn)
        assert "transcript" not in src, f"{kind} must not read the transcript"
        for banned in ("neural", "model", "llm", "judge", "openai", "ollama"):
            assert banned not in src.lower(), f"{kind} must have no model path ({banned})"


def test_tool_result_ignores_a_lying_transcript_even_with_a_contradicting_trace():
    # The agent SAYS it refunded; the trace shows the refund tool erroring.
    a = {"id": "a", "kind": "tool_result", "name": "issue_refund",
         "result_subset": {"status": "ok"}}
    ctx = A.build_context(
        transcript=_LYING_TRANSCRIPT,
        spans=[_tool_span(0, "issue_refund", error="declined")],
    )
    assert _ev(a, ctx)["status"] == "FAIL"   # the trace wins, never the claim


# =========================================================================
# handoff / dtmf / termination -- read the authenticated trace
# =========================================================================

def test_handoff_pass_fail_absent_inconclusive():
    spans = [_point(0, "handoff", to="billing")]
    assert _ev({"id": "a", "kind": "handoff"}, A.build_context(spans=spans))["status"] == "PASS"
    assert _ev({"id": "a", "kind": "handoff", "to": "billing"},
               A.build_context(spans=spans))["status"] == "PASS"
    assert _ev({"id": "a", "kind": "handoff", "to": "sales"},
               A.build_context(spans=spans))["status"] == "FAIL"
    assert _ev({"id": "a", "kind": "handoff", "absent": True},
               A.build_context(spans=[]))["status"] == "PASS"
    assert _ev({"id": "a", "kind": "handoff"}, A.build_context())["status"] == "INCONCLUSIVE"


def test_dtmf_pass_fail_inconclusive():
    spans = [_point(0, "dtmf", digits="12"), _point(1, "dtmf", digits="34")]
    assert _ev({"id": "a", "kind": "dtmf", "digits": "1234"},
               A.build_context(spans=spans))["status"] == "PASS"
    assert _ev({"id": "a", "kind": "dtmf", "digits": "9"},
               A.build_context(spans=spans))["status"] == "FAIL"
    assert _ev({"id": "a", "kind": "dtmf", "digits": "1"},
               A.build_context())["status"] == "INCONCLUSIVE"


def test_termination_pass_fail_inconclusive():
    spans = [_point(0, "call_ended", reason="completed", by="agent")]
    assert _ev({"id": "a", "kind": "termination"},
               A.build_context(spans=spans))["status"] == "PASS"
    assert _ev({"id": "a", "kind": "termination", "reason": "completed"},
               A.build_context(spans=spans))["status"] == "PASS"
    assert _ev({"id": "a", "kind": "termination", "reason": "dropped"},
               A.build_context(spans=spans))["status"] == "FAIL"
    assert _ev({"id": "a", "kind": "termination"},
               A.build_context(spans=[]))["status"] == "FAIL"
    assert _ev({"id": "a", "kind": "termination"},
               A.build_context())["status"] == "INCONCLUSIVE"


# =========================================================================
# latency -- numeric, from trace latency_ms or a timing field
# =========================================================================

def test_latency_trace_pass_and_fail():
    ctx = A.build_context(spans=[_tool_span(0, "lookup", latency_ms=120)])
    ok = _ev({"id": "a", "kind": "latency", "tool": "lookup", "max_ms": 200}, ctx)
    assert ok["status"] == "PASS" and ok["measured_ms"] == 120
    bad = _ev({"id": "a", "kind": "latency", "tool": "lookup", "max_ms": 100}, ctx)
    assert bad["status"] == "FAIL"


def test_latency_inconclusive_without_measurement_or_trace():
    # tool present but no latency_ms -> cannot measure -> INCONCLUSIVE.
    ctx = A.build_context(spans=[_tool_span(0, "lookup")])
    assert _ev({"id": "a", "kind": "latency", "tool": "lookup", "max_ms": 100},
               ctx)["status"] == "INCONCLUSIVE"
    assert _ev({"id": "a", "kind": "latency", "tool": "lookup", "max_ms": 100},
               A.build_context())["status"] == "INCONCLUSIVE"


def test_latency_timing_field_source():
    timing = {"verdict": {"response_gap_sec": 0.8}}
    ok = _ev({"id": "a", "kind": "latency", "field": "verdict.response_gap_sec",
              "max": 1.0}, A.build_context(timing=timing))
    assert ok["status"] == "PASS" and ok["measured"] == 0.8
    bad = _ev({"id": "a", "kind": "latency", "field": "verdict.response_gap_sec",
               "max": 0.5}, A.build_context(timing=timing))
    assert bad["status"] == "FAIL"
    assert _ev({"id": "a", "kind": "latency", "field": "verdict.response_gap_sec",
                "max": 1.0}, A.build_context())["status"] == "INCONCLUSIVE"


# =========================================================================
# entity_accuracy -- deterministic string match vs a reference (NOT WER)
# =========================================================================

def test_entity_accuracy_pass_against_tool_arguments():
    ctx = A.build_context(spans=[
        _tool_span(0, "book", args={"name": "Ada Lovelace", "date": "2026-07-20"}),
    ])
    a = {"id": "a", "kind": "entity_accuracy",
         "reference": {"name": "ada lovelace", "date": "2026-07-20"}}
    r = _ev(a, ctx)
    assert r["status"] == "PASS"           # case-insensitive by default
    assert r["met"] == 2 and r["of"] == 2


def test_entity_accuracy_fail_and_require_any():
    ctx = A.build_context(spans=[
        _tool_span(0, "book", args={"name": "Ada", "date": "2026-01-01"}),
    ])
    a_all = {"id": "a", "kind": "entity_accuracy",
             "reference": {"name": "Ada", "date": "2026-07-20"}}
    r = _ev(a_all, ctx)
    assert r["status"] == "FAIL"
    assert "date" in r["reason"] and "Ada" not in r["reason"]   # keys, not values
    a_any = dict(a_all, id="b", require="any")
    assert _ev(a_any, ctx)["status"] == "PASS"


def test_entity_accuracy_inconclusive_without_a_trace():
    a = {"id": "a", "kind": "entity_accuracy", "reference": {"name": "x"}}
    assert _ev(a, A.build_context())["status"] == "INCONCLUSIVE"


# =========================================================================
# sequence / count
# =========================================================================

def test_sequence_pass_and_fail():
    spans = [_tool_span(0, "verify"), _point(1, "handoff"), _tool_span(2, "refund")]
    steps = [{"tool": "verify"}, {"span_type": "handoff"}, {"tool": "refund"}]
    r = _ev({"id": "a", "kind": "sequence", "steps": steps}, A.build_context(spans=spans))
    assert r["status"] == "PASS" and r["span_ids"] == ["s_0", "s_1", "s_2"]
    bad = _ev({"id": "a", "kind": "sequence",
               "steps": [{"tool": "refund"}, {"tool": "verify"}]},
              A.build_context(spans=spans))
    assert bad["status"] == "FAIL"


def test_sequence_inconclusive_without_a_trace():
    assert _ev({"id": "a", "kind": "sequence", "steps": [{"tool": "x"}]},
               A.build_context())["status"] == "INCONCLUSIVE"


def test_count_over_spans_and_over_phrases():
    spans = [_tool_span(0, "retry"), _tool_span(1, "retry"), _tool_span(2, "done")]
    ok = _ev({"id": "a", "kind": "count", "tool": "retry", "count": 2},
             A.build_context(spans=spans))
    assert ok["status"] == "PASS" and ok["observed"] == 2
    rng = _ev({"id": "a", "kind": "count", "span_type": "tool_call",
               "count": {"min": 3}}, A.build_context(spans=spans))
    assert rng["status"] == "PASS" and rng["observed"] == 3
    bad = _ev({"id": "a", "kind": "count", "tool": "retry", "count": 5},
              A.build_context(spans=spans))
    assert bad["status"] == "FAIL"
    # phrase-count reads the transcript (like the existing phrase kind).
    tx = [_turn("agent", "recorded for quality"), _turn("agent", "recorded for quality")]
    ph = _ev({"id": "a", "kind": "count", "phrase": "recorded for quality", "count": 2},
             A.build_context(transcript=tx))
    assert ph["status"] == "PASS"


def test_count_inconclusive_on_missing_input():
    assert _ev({"id": "a", "kind": "count", "tool": "x", "count": 1},
               A.build_context())["status"] == "INCONCLUSIVE"
    assert _ev({"id": "a", "kind": "count", "phrase": "x", "count": 1},
               A.build_context())["status"] == "INCONCLUSIVE"


# =========================================================================
# rubric kinds: NAMED but quarantined -> INCONCLUSIVE, no LLM path
# =========================================================================

@pytest.mark.parametrize("kind", ["human_rubric", "judge_rubric"])
def test_rubric_kinds_are_quarantined_inconclusive(kind):
    r = _ev({"id": "a", "kind": kind, "rubric": "was the agent polite?"},
            A.build_context(transcript=[_turn("agent", "hello")]))
    # assert.v1 is the deterministic wall: a rubric kind is routed OUT of it as
    # a deterministic INCONCLUSIVE pointing to the SEPARATE model-judged rubric
    # lane (hotato.rubric) -- never a model call here, so summary.judge stays the
    # {0,0} quarantine and deterministic:true is untouched.
    assert r["status"] == "INCONCLUSIVE"
    assert r["deterministic"] is True
    assert "rubric lane" in r["reason"]


def test_rubric_stub_has_no_model_path():
    # The rubric ENGINE (a real local model) lives in hotato.rubric; the
    # assert.v1 evaluator that routes a rubric kind out must itself never call a
    # model -- the deterministic wall stays model-free.
    src = inspect.getsource(A._eval_rubric_stub)
    for banned in ("neural", "openai", "ollama", "requests", "urllib", "http"):
        assert banned not in src.lower()


def test_envelope_with_rubric_keeps_judge_lane_zero():
    # A rubric kind returns a deterministic INCONCLUSIVE stub; the judge lane
    # honestly stays {pass:0, fail:0} because no model ever scored anything.
    ctx = A.build_context(transcript=[_turn("agent", "hi")])
    env = A.run_assertions({"version": 1, "assertions": [
        {"id": "a", "kind": "judge_rubric", "rubric": "polite?"}]}, ctx)
    assert env["summary"]["judge"] == {"pass": 0, "fail": 0}
    assert "overall_score" not in json.dumps(env)


# =========================================================================
# validation: malformed expanded-kind fields are a usage error (exit-2 path)
# =========================================================================

@pytest.mark.parametrize("bad", [
    {"id": "a", "kind": "tool_result"},                       # missing name
    {"id": "a", "kind": "tool_error"},                        # missing name
    {"id": "a", "kind": "state", "resource": "o"},            # missing expect
    {"id": "a", "kind": "state", "expect": {"x": 1}},         # missing resource
    {"id": "a", "kind": "state_change", "resource": "o", "field": "f"},  # no from/to/changed
    {"id": "a", "kind": "dtmf"},                              # missing digits
    {"id": "a", "kind": "latency", "tool": "t"},              # missing max_ms
    {"id": "a", "kind": "latency", "max_ms": 1},              # no source
    {"id": "a", "kind": "timing_contract"},                   # missing bundle
    {"id": "a", "kind": "entity_accuracy"},                   # missing reference
    {"id": "a", "kind": "sequence", "steps": []},             # empty steps
    {"id": "a", "kind": "count", "tool": "t"},                # missing count spec
    {"id": "a", "kind": "count", "count": 1},                 # no matcher
])
def test_malformed_expanded_kind_raises_before_evaluation(bad):
    with pytest.raises(ValueError):
        A.validate_assertions_doc({"version": 1, "assertions": [bad]})


def test_rubric_kind_accepted_by_validator():
    v, items = A.validate_assertions_doc({"version": 1, "assertions": [
        {"id": "a", "kind": "human_rubric", "rubric": "anything"}]})
    assert items[0]["kind"] == "human_rubric"


# =========================================================================
# schema: an envelope carrying every expanded kind validates
# =========================================================================

def test_full_envelope_of_expanded_kinds_validates_against_schema():
    jsonschema = pytest.importorskip("jsonschema")
    ctx = A.build_context(
        transcript=[_turn("agent", "recorded for quality")],
        spans=[
            _tool_span(0, "issue_refund", result={"status": "ok"}, latency_ms=90),
            _point(1, "handoff", to="billing"),
            _point(2, "dtmf", digits="42"),
            _point(3, "call_ended", reason="completed"),
        ],
        timing={"verdict": {"response_gap_sec": 0.5}},
        state_adapter=_adapter(),
    )
    doc = {"version": 1, "assertions": [
        {"id": "k1", "kind": "tool_result", "name": "issue_refund"},
        {"id": "k2", "kind": "tool_error", "name": "issue_refund", "absent": True},
        {"id": "k3", "kind": "state", "resource": "orders", "expect": {"status": "refunded"}},
        {"id": "k4", "kind": "state_change", "resource": "account",
         "filters": {"id": "u1"}, "field": "balance", "to": 0},
        {"id": "k5", "kind": "handoff"},
        {"id": "k6", "kind": "dtmf", "digits": "42"},
        {"id": "k7", "kind": "termination"},
        {"id": "k8", "kind": "latency", "tool": "issue_refund", "max_ms": 500},
        {"id": "k9", "kind": "latency", "field": "verdict.response_gap_sec", "max": 1.0},
        {"id": "k10", "kind": "entity_accuracy", "reference": {"status": "ok"}},
        {"id": "k11", "kind": "sequence", "steps": [{"tool": "issue_refund"}, {"span_type": "handoff"}]},
        {"id": "k12", "kind": "count", "span_type": "tool_call", "count": 1},
        {"id": "k13", "kind": "judge_rubric", "rubric": "polite?"},
    ]}
    env = A.run_assertions(doc, ctx)
    jsonschema.validate(instance=env, schema=_schema())
    assert all(r["deterministic"] is True for r in env["results"])
    assert env["summary"]["judge"] == {"pass": 0, "fail": 0}


# =========================================================================
# the original five kinds are byte-unchanged (a regression pin for this slice)
# =========================================================================

def test_founding_five_kinds_unchanged():
    ctx = A.build_context(transcript=[_turn("agent", "recorded for quality")])
    r = _ev({"id": "a", "kind": "phrase", "regex": "recorded for quality"}, ctx)
    assert r == {"id": "a", "kind": "phrase", "deterministic": True, "status": "PASS"}
    # the schema description still names all five founding kinds first
    for k in ("phrase", "pii", "policy", "tool_call", "outcome"):
        assert k in A.KINDS


def test_byte_stable_across_repeated_runs_expanded():
    ctx = A.build_context(
        spans=[_tool_span(0, "issue_refund", result={"status": "ok"}, latency_ms=90)],
        state_adapter=_adapter(),
    )
    doc = {"version": 1, "assertions": [
        {"id": "a", "kind": "tool_result", "name": "issue_refund", "result_subset": {"status": "ok"}},
        {"id": "b", "kind": "state", "resource": "orders", "expect": {"status": "refunded"}},
        {"id": "c", "kind": "latency", "tool": "issue_refund", "max_ms": 200},
    ]}
    e1 = A.run_assertions(doc, ctx)
    e2 = A.run_assertions(doc, ctx)
    assert json.dumps(e1, sort_keys=True) == json.dumps(e2, sort_keys=True)


# =========================================================================
# timing_contract -- REUSE `contract verify` on a real .hotato bundle
# =========================================================================

HARD = str(resources.files("hotato").joinpath(
    "data", "audio", "01-hard-interruption.example.wav"))


def _make_bundle(tmp_path, cid, expect):
    from hotato import cli
    rc = cli.main([
        "contract", "create", "--stereo", HARD, "--id", cid,
        "--onset", "2.40", "--expect", expect, "--out", str(tmp_path),
    ])
    assert rc == 0
    return str(tmp_path / (cid + ".hotato"))


def test_timing_contract_pass_on_a_passing_bundle(tmp_path):
    bundle = _make_bundle(tmp_path, "tc-pass", "yield")   # HARD yields at 2.40
    r = _ev({"id": "a", "kind": "timing_contract", "bundle": bundle}, A.build_context())
    assert r["status"] == "PASS"
    assert r["deterministic"] is True
    assert r["contracts"]["passed"] == 1


def test_timing_contract_fail_on_a_failing_bundle(tmp_path):
    bundle = _make_bundle(tmp_path, "tc-fail", "hold")    # yielded when it should hold
    r = _ev({"id": "a", "kind": "timing_contract", "bundle": bundle}, A.build_context())
    assert r["status"] == "FAIL"
    assert "tc-fail" in r["reason"]


def test_timing_contract_inconclusive_when_bundle_missing(tmp_path):
    r = _ev({"id": "a", "kind": "timing_contract",
             "bundle": str(tmp_path / "nope.hotato")}, A.build_context())
    assert r["status"] == "INCONCLUSIVE"
