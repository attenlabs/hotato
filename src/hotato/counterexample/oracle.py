"""Exact deterministic failure identity and three-way preservation oracle."""

from __future__ import annotations

import re
from typing import Any, Dict, List, Optional, Set, Tuple

from .. import assert_ as A
from .. import conversation_test as CT
from .. import scenario as SC
from .. import simulate as SIM
from ..state_adapter import MockStateAdapter
from .model import (
    ABSENT,
    DRIFTED,
    PRESERVED,
    UNRESOLVED,
    CounterexampleRefusal,
    prefixed_digest,
)

_REFUSED_KINDS = frozenset({"timing_contract"})
_RUBRIC_KINDS = frozenset(A.RUBRIC_KINDS)


def target_assertion(test_doc: Dict[str, Any], target_id: str) -> Dict[str, Any]:
    doc = CT.validate_conversation_test_doc(test_doc)
    deterministic = list((doc.get("assertions") or {}).get("deterministic") or [])
    rubric = list((doc.get("assertions") or {}).get("rubric") or [])
    if any(item.get("id") == target_id for item in rubric):
        raise CounterexampleRefusal(
            "advisory_target_refused",
            f"target {target_id!r} is in the model-judged rubric lane; only deterministic failures can gate a capsule",
        )
    matches = [item for item in deterministic if item.get("id") == target_id]
    if len(matches) != 1:
        raise CounterexampleRefusal(
            "target_not_unique",
            f"target {target_id!r} must identify exactly one deterministic assertion",
        )
    assertion = dict(matches[0])
    A.validate_assertions_doc({"version": 1, "assertions": [assertion]})
    # The project validators are additive, while the counterexample proof
    # schemas are deliberately closed. Refuse edge values that the assertion
    # evaluator can consume but that cannot be represented by the v1 typed
    # witness contract.
    if assertion["kind"] == "count":
        bound = assertion.get("count")
        values = (
            [bound.get("min"), bound.get("max")]
            if isinstance(bound, dict) else [bound]
        )
        if any(value is not None and (
            isinstance(value, bool) or not isinstance(value, int) or value < 0
        ) for value in values):
            raise CounterexampleRefusal(
                "unsupported_target", "count bounds must be non-negative integers"
            )
    if assertion["kind"] == "entity_accuracy" and any(
        not isinstance(key, str) or not key for key in assertion.get("reference", {})
    ):
        raise CounterexampleRefusal(
            "unsupported_target", "entity_accuracy reference keys must be non-empty strings"
        )
    if assertion["kind"] == "state" and any(
        not isinstance(path, str) or not path for path in assertion.get("expect", {})
    ):
        raise CounterexampleRefusal(
            "unsupported_target", "state expectation paths must be non-empty strings"
        )
    if assertion["kind"] in _REFUSED_KINDS:
        raise CounterexampleRefusal(
            "unsupported_target",
            f"assertion kind {assertion['kind']!r} depends on an external bundle and is outside reducers v1",
        )
    if assertion["kind"] == "latency" and "field" in assertion:
        raise CounterexampleRefusal(
            "unsupported_target",
            "latency assertions over an external timing field are outside the scripted-scenario oracle",
        )
    if assertion["kind"] == "policy" and assertion.get("pack_path"):
        raise CounterexampleRefusal(
            "external_policy_refused",
            "policy pack_path is external to the capsule; use the bundled default pack for reducers v1",
        )
    return assertion


def projected_test(test_doc: Dict[str, Any], assertion: Dict[str, Any]) -> Dict[str, Any]:
    """One target, same assertion bytes, no rubric lane and no blended result."""
    source = CT.validate_conversation_test_doc(test_doc)
    return {
        "kind": CT.KIND,
        "version": CT.VERSION,
        "id": source["id"],
        "agent": source["agent"],
        "assertions": {"deterministic": [dict(assertion)], "rubric": []},
        "repetitions": 1,
        "inconclusive_policy": "refuse",
        "success": {
            "required": ["all_deterministic_assertions_pass", "no_inconclusive"],
            "report_dimensions": list(CT.REPORT_DIMENSIONS),
        },
    }


def _span_field(span: Dict[str, Any], *keys: str) -> Any:
    attrs = span.get("attributes") or {}
    for key in keys:
        if span.get(key) is not None:
            return span[key]
        if attrs.get(key) is not None:
            return attrs[key]
    return None


def _typed_spans(ctx: A.Context, span_type: str, name: Optional[str] = None) -> List[Dict[str, Any]]:
    return [
        span for span in (ctx.spans or [])
        if span.get("type") == span_type and (name is None or span.get("name") == name)
    ]


def _count_bounds(n: int, spec: Any) -> Dict[str, Any]:
    if isinstance(spec, int):
        return {"observed": n, "expected": spec, "relation": "equal"}
    return {"observed": n, "min": spec.get("min"), "max": spec.get("max")}


def _tool_entries(ctx: A.Context) -> List[Dict[str, Any]]:
    out: List[Dict[str, Any]] = []
    for index, span in enumerate(ctx.spans or []):
        if span.get("type") != "tool_call":
            continue
        args = _span_field(span, "arguments")
        out.append({
            "index": index,
            "name": span.get("name"),
            "arguments": args if isinstance(args, dict) else {},
            "result": _span_field(span, "result"),
            "error": _span_field(span, "error", "error_message", "message"),
            "status": _span_field(span, "status"),
            "ok": _span_field(span, "ok"),
        })
    return out


def _is_subset(small: Dict[str, Any], big: Dict[str, Any]) -> bool:
    return all(key in big and big[key] == value for key, value in small.items())


def _value_digest(value: Any) -> str:
    return prefixed_digest(value)


def _query_state(ctx: A.Context, assertion: Dict[str, Any], when: Optional[str] = None) -> Any:
    if ctx.state_adapter is None:
        return None
    filters = dict(assertion.get("filters") or {})
    if when is not None:
        filters["when"] = when
    return ctx.state_adapter.query(assertion["resource"], **filters)


def _get_path(value: Any, path: str) -> Tuple[Any, bool]:
    current = value
    for part in path.split("."):
        if not isinstance(current, dict) or part not in current:
            return None, False
        current = current[part]
    return current, True


def _sequence_prefix(assertion: Dict[str, Any], ctx: A.Context) -> int:
    last = -1
    matched = 0
    for step in assertion["steps"]:
        found = None
        for index, span in enumerate(ctx.spans or []):
            if index <= last:
                continue
            ok = (
                span.get("type") == "tool_call" and span.get("name") == step["tool"]
                if "tool" in step else span.get("type") == step["span_type"]
            )
            if ok:
                found = index
                break
        if found is None:
            break
        matched += 1
        last = found
    return matched


def typed_witness(
    assertion: Dict[str, Any], result: Dict[str, Any], ctx: A.Context
) -> Dict[str, Any]:
    """A content-minimizing, kind-specific observation; never the reason text."""
    kind = assertion["kind"]
    witness: Dict[str, Any] = {"type": f"{kind}-failure"}

    if kind == "phrase":
        role = assertion.get("role")
        flags = 0 if assertion.get("case_sensitive") else re.IGNORECASE
        rx = re.compile(assertion["regex"], flags)
        turns = [t for t in (ctx.transcript or []) if role is None or t.get("role") == role]
        matches = sum(1 for turn in turns if rx.search(turn.get("text") or ""))
        witness.update({
            "mode": "forbidden-present" if assertion.get("absent") else "required-missing",
            "matches": matches,
        })
    elif kind == "pii":
        hits = result.get("hits") or []
        anchors = []
        for hit in hits:
            role = hit.get("role")
            turns = [turn for turn in (ctx.transcript or []) if turn.get("role") == role]
            index = hit.get("turn")
            text_digest = None
            if isinstance(index, int) and not isinstance(index, bool) and 0 <= index < len(turns):
                text_digest = _value_digest(turns[index].get("text") or "")
            anchors.append({
                "detector": hit.get("detector"),
                "role": role,
                "turn_text_digest": text_digest,
            })
        witness.update({
            "detector_roles": sorted([[h.get("detector"), h.get("role")] for h in hits]),
            "hits": len(hits),
            "hit_anchors": sorted(
                anchors,
                key=lambda row: (
                    str(row["detector"]), str(row["role"]), str(row["turn_text_digest"])
                ),
            ),
        })
    elif kind == "policy":
        witness["violations"] = sorted([
            {"rule": row.get("rule"), "type": row.get("type")}
            for row in (result.get("matched_rules") or [])
        ], key=lambda row: (str(row["rule"]), str(row["type"])))
        witness["pack"] = result.get("pack")
    elif kind == "tool_call":
        entries = _tool_entries(ctx)
        name = assertion.get("name")
        named = [row for row in entries if row["name"] == name] if name is not None else []
        args = assertion.get("args_subset") or {}
        qualifying = [row for row in named if _is_subset(args, row["arguments"])]
        witness["named_matches"] = len(qualifying)
        if assertion.get("count") is not None:
            witness["count"] = _count_bounds(len(qualifying), assertion["count"])
        order = assertion.get("require_order") or []
        prefix = 0
        pos = -1
        for tool in order:
            found = next((r["index"] for r in entries if r["index"] > pos and r["name"] == tool), None)
            if found is None:
                break
            prefix += 1
            pos = found
        if order:
            witness["order_prefix"] = prefix
            witness["order_steps"] = len(order)
        if assertion.get("never_before"):
            pair = assertion["never_before"]
            y = next((r["index"] for r in entries if r["name"] == pair["until"]), None)
            offenders = [r for r in entries if r["name"] == pair["tool"] and (y is None or r["index"] < y)]
            witness["never_before"] = {"offenders": len(offenders), "until_present": y is not None}
    elif kind == "outcome":
        mode = "all_of" if assertion.get("all_of") is not None else "any_of"
        predicates = []
        for index, predicate in enumerate(assertion[mode]):
            if "tool_called" in predicate:
                name = predicate["tool_called"]
                count = sum(
                    1 for span in (ctx.spans or [])
                    if span.get("type") == "tool_call" and span.get("name") == name
                )
                predicates.append({"index": index, "kind": "tool_called", "matches": count})
            elif "phrase" in predicate:
                rx = re.compile(predicate["phrase"], re.IGNORECASE)
                role = predicate.get("role")
                matched = [
                    turn for turn in (ctx.transcript or [])
                    if (role is None or turn.get("role") == role)
                    and rx.search(turn.get("text") or "")
                ]
                predicates.append({
                    "index": index,
                    "kind": "phrase",
                    "matches": len(matched),
                    "turn_text_digests": sorted(_value_digest(turn.get("text") or "") for turn in matched),
                })
            else:
                predicates.append({"index": index, "kind": "field_present", "present": False})
        witness.update({
            "mode": mode,
            "met": result.get("met"),
            "of": result.get("of"),
            "predicates": predicates,
        })
    elif kind == "tool_result":
        rows = [row for row in _tool_entries(ctx) if row["name"] == assertion["name"]]
        subset = assertion.get("result_subset") or {}
        matching = [row for row in rows if isinstance(row["result"], dict) and _is_subset(subset, row["result"])]
        witness.update({"calls": len(rows), "matching_results": len(matching)})
    elif kind == "tool_error":
        rows = [row for row in _tool_entries(ctx) if row["name"] == assertion["name"]]
        rx = re.compile(assertion["error_matches"], re.IGNORECASE) if assertion.get("error_matches") else None
        matched = 0
        for row in rows:
            errored = row["error"] not in (None, "", False) or row["status"] == "error" or row["ok"] is False
            if not errored:
                continue
            if rx is not None and not (isinstance(row["error"], str) and rx.search(row["error"])):
                continue
            matched += 1
        witness.update({"mode": "error-forbidden" if assertion.get("absent") else "error-required",
                        "matching_errors": matched})
    elif kind == "state":
        record = _query_state(ctx, assertion)
        if record is None:
            witness["record"] = "absent"
        else:
            mismatched = []
            for path, expected in assertion["expect"].items():
                value, found = _get_path(record, path)
                if not found or value != expected:
                    mismatched.append({"path": path, "observed": _value_digest(value) if found else None})
            witness.update({"record": "present", "mismatched": sorted(mismatched, key=lambda x: x["path"])})
    elif kind == "state_change":
        before = _query_state(ctx, assertion, "before")
        after = _query_state(ctx, assertion, "after")
        field = assertion["field"]
        bval, bfound = _get_path(before, field) if before is not None else (None, False)
        aval, afound = _get_path(after, field) if after is not None else (None, False)
        checks: List[str] = []
        if "from" in assertion and (not bfound or bval != assertion["from"]):
            checks.append("from-mismatch")
        if "to" in assertion and (not afound or aval != assertion["to"]):
            checks.append("to-mismatch")
        if assertion.get("changed") and bfound and afound and bval == aval:
            checks.append("unchanged")
        witness.update({
            "before_present": before is not None,
            "after_present": after is not None,
            "checks": sorted(checks),
            "before_value": _value_digest(bval) if bfound else None,
            "after_value": _value_digest(aval) if afound else None,
        })
    elif kind == "handoff":
        rows = _typed_spans(ctx, "handoff")
        if assertion.get("to") is not None:
            rows = [row for row in rows if _span_field(row, "to", "target", "name") == assertion["to"]]
        witness.update({"mode": "forbidden" if assertion.get("absent") else "required", "matches": len(rows)})
    elif kind == "dtmf":
        seen = "".join(str(_span_field(s, "digits", "digit")) for s in _typed_spans(ctx, "dtmf")
                       if _span_field(s, "digits", "digit") is not None)
        witness.update({"mode": "forbidden" if assertion.get("absent") else "required",
                        "contains": assertion["digits"] in seen, "stream_digest": _value_digest(seen)})
    elif kind == "termination":
        rows = [s for s in (ctx.spans or []) if s.get("type") in ("termination", "call_ended", "call_terminated", "hangup")]
        if assertion.get("reason") is not None:
            rows = [s for s in rows if _span_field(s, "reason") == assertion["reason"]]
        if assertion.get("by") is not None:
            rows = [s for s in rows if _span_field(s, "by", "terminated_by") == assertion["by"]]
        witness.update({"mode": "forbidden" if assertion.get("absent") else "required", "matches": len(rows)})
    elif kind == "latency":
        witness.update({"measured": result.get("measured"), "measured_ms": result.get("measured_ms")})
    elif kind == "entity_accuracy":
        observed: Dict[str, Any] = {}
        for row in _tool_entries(ctx):
            observed.update(row["arguments"])
        reference = assertion["reference"]
        case_sensitive = bool(assertion.get("case_sensitive", False))
        mismatched = []
        observed_digests = {}
        for key, expected in reference.items():
            got = observed.get(key)
            if got is not None:
                observed_digests[key] = _value_digest(got)
            left = str(got) if case_sensitive else str(got).lower()
            right = str(expected) if case_sensitive else str(expected).lower()
            if got is None or left != right:
                mismatched.append(key)
        witness.update({
            "met": result.get("met"),
            "of": result.get("of"),
            "require": assertion.get("require", "all"),
            "mismatched_keys": sorted(mismatched),
            "observed_value_digests": observed_digests,
        })
    elif kind == "sequence":
        witness.update({"matched_prefix": _sequence_prefix(assertion, ctx), "steps": len(assertion["steps"])})
    elif kind == "count":
        witness["count"] = _count_bounds(int(result.get("observed", 0)), assertion["count"])
    else:
        raise CounterexampleRefusal("unsupported_target", f"no typed oracle for assertion kind {kind!r}")
    return witness


def failure_fingerprint(
    test_id: str, assertion: Dict[str, Any], witness: Dict[str, Any]
) -> Dict[str, Any]:
    identity = {
        "test_id": test_id,
        "assertion_digest": prefixed_digest(assertion),
        "assertion_id": assertion["id"],
        "kind": assertion["kind"],
        "dimension": assertion.get("dimension"),
        "authority": "deterministic",
        "required_status": "FAIL",
        "witness": witness,
    }
    identity["fingerprint"] = prefixed_digest(identity)
    return identity


def _scope_freezes(assertion: Dict[str, Any], witness: Dict[str, Any]) -> Set[str]:
    """Return explicit immutable components for reducers v1.

    Scripted candidates remain complete scenario documents and keep at least
    one caller turn.  Exact typed witnesses carry the observation domain, so
    v1 does not freeze whole script/tool/state collections: irrelevant members
    are precisely what a counterexample compiler must remove.
    """
    return set()


class FailureOracle:
    def __init__(self, test_doc: Dict[str, Any], assertion: Dict[str, Any], seed: int):
        self.test_doc = projected_test(test_doc, assertion)
        self.assertion = dict(assertion)
        self.seed = int(seed)
        self.source_identity: Optional[Dict[str, Any]] = None
        self.frozen: Set[str] = set()

    def _evaluate_once(self, scenario: Dict[str, Any]) -> Dict[str, Any]:
        try:
            doc = SC.validate_scenario_doc(scenario)
            if len(doc["caller"]["script"]) > 10_000:
                raise CounterexampleRefusal("too_many_turns", "scenario exceeds 10,000 turns")
            if len((doc.get("agent_mock") or {}).get("tools") or []) > 10_000:
                raise CounterexampleRefusal("too_many_tools", "scenario exceeds 10,000 mock tools")
            produced = SIM.render(doc, self.seed)
            sim_verdict = SIM.validate_simulation(doc, produced)
            if not sim_verdict.get("ok"):
                return {"status": UNRESOLVED, "code": "simulator_invalid"}
            state = (doc.get("agent_mock") or {}).get("state")
            adapter = MockStateAdapter(state) if isinstance(state, dict) else None
            ctx = A.build_context(
                transcript=produced["transcript"]["segments"],
                spans=produced["trace"]["spans"],
                state_adapter=adapter,
            )
            result = A.evaluate_assertion(self.assertion, ctx)
            if (
                not isinstance(result, dict)
                or result.get("id") != self.assertion.get("id")
                or result.get("kind") != self.assertion.get("kind")
                or result.get("deterministic") is not True
                or result.get("status") not in {"PASS", "FAIL", "INCONCLUSIVE"}
            ):
                return {
                    "status": UNRESOLVED,
                    "code": "assertion_contract_invalid",
                }
            if result.get("status") == "INCONCLUSIVE":
                return {"status": UNRESOLVED, "code": "assertion_inconclusive", "result": result}
            witness = typed_witness(self.assertion, result, ctx) if result.get("status") == "FAIL" else None
            identity = failure_fingerprint(self.test_doc["id"], self.assertion, witness) if witness else None
            return {
                "status": PRESERVED if result.get("status") == "FAIL" else ABSENT,
                "code": "target_failed" if result.get("status") == "FAIL" else "target_absent",
                "result": result,
                "result_digest": prefixed_digest(result),
                "witness": witness,
                "witness_digest": prefixed_digest(witness) if witness else None,
                "identity": identity,
                "produced": produced,
            }
        except CounterexampleRefusal:
            raise
        except (ValueError, OSError, RecursionError, MemoryError) as exc:
            return {"status": UNRESOLVED, "code": "candidate_invalid", "detail": str(exc)}

    def freeze_source(self, scenario: Dict[str, Any]) -> Dict[str, Any]:
        result = self._evaluate_once(scenario)
        if result.get("status") == UNRESOLVED:
            raise CounterexampleRefusal(
                "source_unresolved", f"source cannot produce a deterministic verdict ({result.get('code')})"
            )
        if result.get("status") != PRESERVED:
            raise CounterexampleRefusal(
                "source_not_failing", f"target assertion {self.assertion['id']!r} does not FAIL on the source"
            )
        self.source_identity = dict(result["identity"])
        self.frozen = _scope_freezes(self.assertion, result["witness"])
        return result

    def evaluate(self, scenario: Dict[str, Any]) -> Dict[str, Any]:
        if self.source_identity is None:
            raise RuntimeError("freeze_source must run before candidate evaluation")
        result = self._evaluate_once(scenario)
        if result.get("status") != PRESERVED:
            return result
        if result["identity"]["fingerprint"] != self.source_identity["fingerprint"]:
            return {
                "status": DRIFTED,
                "code": "failure_identity_drift",
                "result_digest": result.get("result_digest"),
                "witness_digest": result.get("witness_digest"),
            }
        return result

    def oracle_document(self) -> Dict[str, Any]:
        if self.source_identity is None:
            raise RuntimeError("source is not frozen")
        return {
            "kind": "hotato.counterexample-oracle.v1",
            "version": 1,
            "authority": "deterministic",
            "ci_gate_eligible": True,
            "target": self.source_identity,
            "observation_scope": {
                "frozen_components": sorted(self.frozen),
                "rule": "candidate is a complete schema-valid scripted session and must reproduce the exact typed failure fingerprint",
                "minimum_caller_turns": 1,
                "transforms": "deletion-only",
            },
        }
