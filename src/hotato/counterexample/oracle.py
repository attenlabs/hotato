"""Exact deterministic failure identity and three-way preservation oracle."""

from __future__ import annotations

import json
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

_REFUSED_KINDS = frozenset({"timing_contract", "dtmf"})
_RUBRIC_KINDS = frozenset(A.RUBRIC_KINDS)
_MAX_PROOF_REGEX_BYTES = 1_024
_MAX_TARGET_ASSERTION_BYTES = 256 * 1024
_MAX_ORACLE_SCENARIO_BYTES = 2 * 1024 * 1024
_MAX_ORACLE_TRANSCRIPT_BYTES = 256 * 1024
_MAX_ORACLE_RESULT_BYTES = 2 * 1024 * 1024
_MAX_ORACLE_EVIDENCE_ROWS = 10_000


def _json_bytes(value: Any) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=True,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def _validate_proof_regex(pattern: str, field: str) -> None:
    """Accept a deliberately linear, portable regex subset for proof replay.

    The main assertion engine supports Python regexes. A counterexample may be
    replayed many times against untrusted capsules, so its proof lane refuses
    constructs with backtracking-dependent complexity: groups, alternation,
    backreferences, and every variable quantifier. Python's backtracking
    engine can make even one unanchored repetition quadratic on a no-match
    search, so the proof lane accepts only fixed-width patterns.
    """
    if len(pattern.encode("utf-8")) > _MAX_PROOF_REGEX_BYTES:
        raise CounterexampleRefusal(
            "unsupported_target_regex",
            f"{field} exceeds the {_MAX_PROOF_REGEX_BYTES}-byte proof-regex limit",
        )
    escaped = False
    in_class = False
    for char in pattern:
        if escaped:
            if char.isdigit() or char == "g":
                raise CounterexampleRefusal(
                    "unsupported_target_regex",
                    f"{field} uses a backreference outside the proof-regex profile",
                )
            escaped = False
            continue
        if char == "\\":
            escaped = True
            continue
        if in_class:
            if char == "]":
                in_class = False
            continue
        if char == "[":
            in_class = True
            continue
        if char in "()|":
            raise CounterexampleRefusal(
                "unsupported_target_regex",
                f"{field} uses grouping or alternation outside the proof-regex profile",
            )
        if char in "*+?{":
            raise CounterexampleRefusal(
                "unsupported_target_regex",
                f"{field} uses a variable quantifier outside the proof-regex profile",
            )


def _raw_proof_regex_fields(assertion: Dict[str, Any]) -> List[Tuple[str, str]]:
    """Extract string regexes without invoking the general regex validator."""
    kind = assertion.get("kind")
    fields: List[Tuple[str, str]] = []
    if kind == "phrase" and isinstance(assertion.get("regex"), str):
        fields.append((assertion["regex"], "phrase.regex"))
    elif kind == "count" and isinstance(assertion.get("phrase"), str):
        fields.append((assertion["phrase"], "count.phrase"))
    elif kind == "tool_error" and isinstance(assertion.get("error_matches"), str):
        fields.append((assertion["error_matches"], "tool_error.error_matches"))
    elif kind == "outcome":
        # Preflight both lanes independently. The exactly-one rule is enforced
        # after generic shape validation, so using ``all_of or any_of`` here
        # would let an invalid second lane reach ``re.compile`` first.
        for lane in ("all_of", "any_of"):
            predicates = assertion.get(lane) or []
            if isinstance(predicates, list):
                fields.extend(
                    (predicate["phrase"], f"outcome.{lane}[{index}].phrase")
                    for index, predicate in enumerate(predicates)
                    if isinstance(predicate, dict)
                    and isinstance(predicate.get("phrase"), str)
                )
    return fields


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
    # These bounds run before the general assertion validator calls
    # ``re.compile``. A proof target is untrusted input, so an oversized or
    # backtracking-dependent regex must never reach Python's regex parser.
    if len(_json_bytes(assertion)) > _MAX_TARGET_ASSERTION_BYTES:
        raise CounterexampleRefusal(
            "unsupported_target",
            f"target assertion exceeds {_MAX_TARGET_ASSERTION_BYTES} bytes",
        )
    for pattern, field in _raw_proof_regex_fields(assertion):
        _validate_proof_regex(pattern, field)
    A.validate_assertions_doc({"version": 1, "assertions": [assertion]})
    if assertion["kind"] == "outcome":
        all_of = assertion.get("all_of")
        any_of = assertion.get("any_of")
        if (all_of is None) == (any_of is None):
            raise CounterexampleRefusal(
                "unsupported_target",
                "outcome proof targets require exactly one of all_of or any_of",
            )
        for predicate in all_of or any_of or []:
            discriminator = next(
                key for key in ("tool_called", "phrase", "field_present")
                if key in predicate
            )
            allowed = {discriminator, "role"} if discriminator == "phrase" else {discriminator}
            if set(predicate) != allowed and not (
                discriminator == "phrase" and set(predicate) == {"phrase"}
            ):
                raise CounterexampleRefusal(
                    "unsupported_target",
                    "outcome proof predicates use a closed field set",
                )
    if assertion["kind"] == "outcome":
        predicates = assertion.get("all_of") or assertion.get("any_of") or []
        if any("field_present" in predicate for predicate in predicates):
            raise CounterexampleRefusal(
                "unsupported_target",
                "outcome field_present predicates require external timing context "
                "and are outside the scripted-scenario oracle",
            )
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
    if assertion["kind"] == "entity_accuracy" and any(
        value is None for value in assertion.get("reference", {}).values()
    ):
        raise CounterexampleRefusal(
            "unsupported_target",
            "entity_accuracy null reference values cannot produce a passing "
            "comparison in the deterministic evaluator",
        )
    if assertion["kind"] in {"tool_call", "tool_result"}:
        subset_name = (
            "args_subset" if assertion["kind"] == "tool_call" else "result_subset"
        )
        if any(
            not isinstance(key, str) or not key
            for key in assertion.get(subset_name, {})
        ):
            raise CounterexampleRefusal(
                "unsupported_target",
                f"{assertion['kind']} {subset_name} keys must be non-empty strings",
            )
    if assertion["kind"] == "state" and any(
        not isinstance(path, str) or not path for path in assertion.get("expect", {})
    ):
        raise CounterexampleRefusal(
            "unsupported_target", "state expectation paths must be non-empty strings"
        )
    if assertion["kind"] in _REFUSED_KINDS:
        if assertion["kind"] == "dtmf":
            raise CounterexampleRefusal(
                "unsupported_target",
                "DTMF assertions require trace evidence the scripted simulator "
                "does not emit",
            )
        raise CounterexampleRefusal(
            "unsupported_target",
            f"assertion kind {assertion['kind']!r} depends on an external bundle and is outside reducers v1",
        )
    if assertion["kind"] == "latency" and "field" in assertion:
        raise CounterexampleRefusal(
            "unsupported_target",
            "latency assertions over an external timing field are outside the scripted-scenario oracle",
        )
    if (
        assertion["kind"] == "latency"
        and assertion.get("span_type") not in (None, "tool_call")
    ):
        raise CounterexampleRefusal(
            "unsupported_target",
            "the scripted-scenario oracle exposes latency_ms only on tool_call spans",
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


def _span_field_with_presence(
    span: Dict[str, Any], *keys: str
) -> Tuple[Any, bool]:
    """Return the assertion-visible value and whether any source field exists.

    The main evaluator treats top-level and ``attributes`` payloads as one
    observation surface. Proof branches additionally need to distinguish a
    deleted field from a present field whose value is ``null`` or otherwise
    wrong; collapsing those cases would let reduction erase the evidence named
    by a value-mismatch branch.
    """
    value = _span_field(span, *keys)
    if value is not None:
        return value, True
    attrs = span.get("attributes") or {}
    return None, any(key in span or key in attrs for key in keys)


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
        args, arguments_present = _span_field_with_presence(span, "arguments")
        out.append({
            "index": index,
            "name": span.get("name"),
            "arguments": args if isinstance(args, dict) else {},
            "arguments_present": arguments_present,
            "result": _span_field(span, "result"),
            # Mirror assert_._span_errored exactly: an error_message alone is
            # diagnostic text, not an error indicator.
            "error": _span_field(span, "error"),
            "error_message": _span_field(
                span, "error", "error_message", "message"
            ),
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


def _sequence_failure_atom(
    assertion: Dict[str, Any], ctx: A.Context
) -> Dict[str, Any]:
    last = -1
    for index, step in enumerate(assertion["steps"]):
        candidates = [
            span_index
            for span_index, span in enumerate(ctx.spans or [])
            if (
                span.get("type") == "tool_call" and span.get("name") == step["tool"]
                if "tool" in step
                else span.get("type") == step["span_type"]
            )
        ]
        found = None
        for span_index in candidates:
            if span_index > last:
                found = span_index
                break
        if found is None:
            return {
                "code": (
                    "sequence-step-out-of-order"
                    if candidates
                    else "sequence-step-absent"
                ),
                "index": index,
            }
        last = found
    raise CounterexampleRefusal(
        "failure_atom_unavailable",
        "failed sequence assertion has no typed missing or out-of-order step",
    )


FAILURE_ATOM_FIELDS: Dict[str, Dict[str, Tuple[str, ...]]] = {
    "phrase": {
        "forbidden-match": (),
        "no-qualifying-turns": (),
        "required-match-missing": (),
    },
    "pii": {"pii-detected": ("detector",)},
    "policy": {"policy-violation": ("rule", "type")},
    "tool_call": {
        "tool-missing": (),
        "tool-arguments-missing": (),
        "tool-argument-field-missing": ("key",),
        "tool-argument-value-mismatch": ("key",),
        "tool-count-below": (),
        "tool-count-above": (),
        "order-step-absent": ("index",),
        "order-step-out-of-order": ("index",),
        "never-before-boundary-missing": (),
        "never-before-order-violation": (),
    },
    "outcome": {
        "predicate-unmet": ("index",),
        "no-predicate-met": (),
    },
    "tool_result": {
        "tool-missing": (),
        "result-missing": (),
        "result-field-missing": ("key",),
        "result-field-value-mismatch": ("key",),
    },
    "tool_error": {
        "tool-missing": (),
        "tool-error-missing": (),
        "tool-error-pattern-mismatch": (),
        "unexpected-tool-error": (),
    },
    "state": {
        "state-record-missing": (),
        "state-field-missing": ("field",),
        "state-field-value-mismatch": ("field",),
    },
    "state_change": {
        "before-field-missing": ("field",),
        "before-value-mismatch": ("field",),
        "after-field-missing": ("field",),
        "after-value-mismatch": ("field",),
        "state-unchanged": ("field",),
    },
    "handoff": {
        "handoff-missing": (),
        "handoff-target-mismatch": (),
        "unexpected-handoff": (),
    },
    "termination": {
        "termination-missing": (),
        "termination-attribute-missing": ("field",),
        "termination-attribute-value-mismatch": ("field",),
        "unexpected-termination": (),
    },
    "latency": {
        "latency-declared-threshold-exceeded": (),
        "latency-default-threshold-exceeded": (),
    },
    "entity_accuracy": {
        "entity-missing": ("key",),
        "entity-value-mismatch": ("key",),
    },
    "sequence": {
        "sequence-step-absent": ("index",),
        "sequence-step-out-of-order": ("index",),
    },
    "count": {
        "count-below": (),
        "count-above": (),
    },
}


def failure_atom_sort_key(atom: Dict[str, Any]) -> bytes:
    """Canonical UTF-8 ordering for deterministic multi-failure selection."""
    return json.dumps(
        atom,
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def _sorted_atoms(atoms: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    unique = {failure_atom_sort_key(atom): atom for atom in atoms}
    return [unique[key] for key in sorted(unique)]


def _count_failure_code(observed: int, spec: Any) -> str:
    if isinstance(spec, int):
        if observed < spec:
            return "count-below"
        if observed > spec:
            return "count-above"
        raise CounterexampleRefusal(
            "failure_atom_unavailable",
            "failed count assertion equals its required count",
        )
    minimum = spec.get("min")
    maximum = spec.get("max")
    if minimum is not None and observed < minimum:
        return "count-below"
    if maximum is not None and observed > maximum:
        return "count-above"
    raise CounterexampleRefusal(
        "failure_atom_unavailable", "failed count assertion has no typed count branch"
    )


def _outcome_predicate_met(predicate: Dict[str, Any], ctx: A.Context) -> bool:
    if "tool_called" in predicate:
        return any(
            span.get("type") == "tool_call"
            and span.get("name") == predicate["tool_called"]
            for span in (ctx.spans or [])
        )
    if "phrase" in predicate:
        rx = re.compile(predicate["phrase"], re.IGNORECASE)
        role = predicate.get("role")
        return any(
            (role is None or turn.get("role") == role)
            and bool(rx.search(turn.get("text") or ""))
            for turn in (ctx.transcript or [])
        )
    value: Any = ctx.timing
    for part in predicate["field_present"].split("."):
        if not isinstance(value, dict) or part not in value:
            return False
        value = value[part]
    return value is not None


def failure_atoms(
    assertion: Dict[str, Any],
    result: Dict[str, Any],
    ctx: A.Context,
    scenario: Optional[Dict[str, Any]] = None,
) -> List[Dict[str, Any]]:
    """Return the closed, payload-free failure branches for one deterministic FAIL."""
    kind = assertion["kind"]
    atoms: List[Dict[str, Any]] = []

    if kind == "phrase":
        if assertion.get("absent"):
            atoms.append({"code": "forbidden-match"})
        else:
            role = assertion.get("role")
            turns = [
                turn
                for turn in (ctx.transcript or [])
                if role is None or turn.get("role") == role
            ]
            atoms.append({
                "code": (
                    "required-match-missing" if turns else "no-qualifying-turns"
                )
            })
    elif kind == "pii":
        atoms.extend({"code": "pii-detected", "detector": hit.get("detector")} for hit in (result.get("hits") or []))
    elif kind == "policy":
        atoms.extend({
            "code": "policy-violation",
            "rule": row.get("rule"),
            "type": row.get("type"),
        } for row in (result.get("matched_rules") or []))
    elif kind == "tool_call":
        entries = _tool_entries(ctx)
        name = assertion.get("name")
        if name is not None:
            named = [row for row in entries if row["name"] == name]
            args = assertion.get("args_subset") or {}
            qualifying = [row for row in named if _is_subset(args, row["arguments"])]
            spec = assertion.get("count")
            if spec is None and not qualifying:
                if not named:
                    atoms.append({"code": "tool-missing"})
                elif args:
                    for row in named:
                        if not row["arguments_present"]:
                            atoms.append({"code": "tool-arguments-missing"})
                            continue
                        for key, expected in args.items():
                            if key not in row["arguments"]:
                                atoms.append({
                                    "code": "tool-argument-field-missing",
                                    "key": key,
                                })
                            elif row["arguments"][key] != expected:
                                atoms.append({
                                    "code": "tool-argument-value-mismatch",
                                    "key": key,
                                })
            elif spec is not None:
                observed = len(qualifying)
                if isinstance(spec, int):
                    if observed != spec:
                        atoms.append({
                            "code": f"tool-{_count_failure_code(observed, spec)}"
                        })
                elif (
                    (spec.get("min") is not None and observed < spec["min"])
                    or (spec.get("max") is not None and observed > spec["max"])
                ):
                    code = _count_failure_code(observed, spec)
                    atoms.append({"code": f"tool-{code}"})
        order = assertion.get("require_order") or []
        last = -1
        for index, tool in enumerate(order):
            found = next(
                (
                    row["index"]
                    for row in entries
                    if row["index"] > last and row["name"] == tool
                ),
                None,
            )
            if found is None:
                atoms.append({
                    "code": (
                        "order-step-out-of-order"
                        if any(row["name"] == tool for row in entries)
                        else "order-step-absent"
                    ),
                    "index": index,
                })
                break
            last = found
        boundary = assertion.get("never_before")
        if boundary:
            until = next(
                (row["index"] for row in entries if row["name"] == boundary["until"]),
                None,
            )
            if any(
                row["name"] == boundary["tool"]
                and (until is None or row["index"] < until)
                for row in entries
            ):
                atoms.append({
                    "code": (
                        "never-before-boundary-missing"
                        if until is None
                        else "never-before-order-violation"
                    )
                })
    elif kind == "outcome":
        mode = "all_of" if assertion.get("all_of") is not None else "any_of"
        if mode == "all_of":
            atoms.extend(
                {"code": "predicate-unmet", "index": index}
                for index, predicate in enumerate(assertion[mode])
                if not _outcome_predicate_met(predicate, ctx)
            )
        else:
            atoms.append({"code": "no-predicate-met"})
    elif kind == "tool_result":
        rows = [row for row in _tool_entries(ctx) if row["name"] == assertion["name"]]
        if not rows:
            atoms.append({"code": "tool-missing"})
        else:
            subset = assertion.get("result_subset") or {}
            for row in rows:
                observed = row["result"]
                if not isinstance(observed, dict):
                    atoms.append({"code": "result-missing"})
                    continue
                for key, expected in subset.items():
                    if key not in observed:
                        atoms.append({
                            "code": "result-field-missing",
                            "key": key,
                        })
                    elif observed[key] != expected:
                        atoms.append({
                            "code": "result-field-value-mismatch",
                            "key": key,
                        })
    elif kind == "tool_error":
        if assertion.get("absent"):
            atoms.append({"code": "unexpected-tool-error"})
        else:
            rows = [
                row for row in _tool_entries(ctx)
                if row["name"] == assertion["name"]
            ]
            if not rows:
                atoms.append({"code": "tool-missing"})
            else:
                errored = [
                    row for row in rows
                    if row["error"] not in (None, "", False)
                    or row["status"] == "error"
                    or row["ok"] is False
                ]
                atoms.append({
                    "code": (
                        "tool-error-pattern-mismatch"
                        if errored and assertion.get("error_matches")
                        else "tool-error-missing"
                    )
                })
    elif kind == "state":
        record = _query_state(ctx, assertion)
        if record is None:
            atoms.append({"code": "state-record-missing"})
        else:
            for field, expected in assertion["expect"].items():
                observed, found = _get_path(record, field)
                if not found:
                    atoms.append({"code": "state-field-missing", "field": field})
                elif observed != expected:
                    atoms.append({
                        "code": "state-field-value-mismatch",
                        "field": field,
                    })
    elif kind == "state_change":
        before = _query_state(ctx, assertion, "before")
        after = _query_state(ctx, assertion, "after")
        field = assertion["field"]
        before_value, before_found = _get_path(before, field) if before is not None else (None, False)
        after_value, after_found = _get_path(after, field) if after is not None else (None, False)
        if "from" in assertion and before_value != assertion["from"]:
            atoms.append({
                "code": (
                    "before-value-mismatch" if before_found else "before-field-missing"
                ),
                "field": field,
            })
        if "to" in assertion and after_value != assertion["to"]:
            atoms.append({
                "code": (
                    "after-value-mismatch" if after_found else "after-field-missing"
                ),
                "field": field,
            })
        if assertion.get("changed") and before_value == after_value:
            atoms.append({"code": "state-unchanged", "field": field})
    elif kind == "handoff":
        if assertion.get("absent"):
            atoms.append({"code": "unexpected-handoff"})
        else:
            rows = [
                span for span in (ctx.spans or [])
                if span.get("type") == "handoff"
            ]
            atoms.append({
                "code": "handoff-target-mismatch" if rows else "handoff-missing"
            })
    elif kind == "termination":
        if assertion.get("absent"):
            atoms.append({"code": "unexpected-termination"})
        else:
            rows = [
                span for span in (ctx.spans or [])
                if span.get("type") in {
                    "termination", "call_ended", "call_terminated", "hangup",
                }
            ]
            if not rows:
                atoms.append({"code": "termination-missing"})
            else:
                for row in rows:
                    for field, keys in (
                        ("reason", ("reason",)),
                        ("by", ("by", "terminated_by")),
                    ):
                        if field not in assertion:
                            continue
                        observed, found = _span_field_with_presence(row, *keys)
                        if not found:
                            atoms.append({
                                "code": "termination-attribute-missing",
                                "field": field,
                            })
                        elif observed != assertion[field]:
                            atoms.append({
                                "code": "termination-attribute-value-mismatch",
                                "field": field,
                            })
    elif kind == "latency":
        matching = [
            span
            for span in (ctx.spans or [])
            if (
                span.get("type") == "tool_call"
                and span.get("name") == assertion["tool"]
                if "tool" in assertion
                else span.get("type") == assertion["span_type"]
            )
            and isinstance(_span_field(span, "latency_ms"), (int, float))
            and not isinstance(_span_field(span, "latency_ms"), bool)
            and _span_field(span, "latency_ms") > assertion["max_ms"]
        ]
        if scenario is None:
            atoms.append({"code": "latency-declared-threshold-exceeded"})
        else:
            specs = list((scenario.get("agent_mock") or {}).get("tools") or [])
            rendered_tools = _typed_spans(ctx, "tool_call")
            declared_by_identity = {
                id(span): "latency_ms" in spec
                for span, spec in zip(rendered_tools, specs)
            }
            for span in matching:
                atoms.append({
                    "code": (
                        "latency-declared-threshold-exceeded"
                        if declared_by_identity.get(id(span), False)
                        else "latency-default-threshold-exceeded"
                    )
                })
    elif kind == "entity_accuracy":
        observed: Dict[str, Any] = {}
        for row in _tool_entries(ctx):
            observed.update(row["arguments"])
        case_sensitive = bool(assertion.get("case_sensitive", False))
        for key, expected in assertion["reference"].items():
            found = key in observed
            got = observed.get(key)
            left = str(got) if case_sensitive else str(got).lower()
            right = str(expected) if case_sensitive else str(expected).lower()
            if not found:
                atoms.append({"code": "entity-missing", "key": key})
            elif left != right:
                atoms.append({"code": "entity-value-mismatch", "key": key})
    elif kind == "sequence":
        atoms.append(_sequence_failure_atom(assertion, ctx))
    elif kind == "count":
        atoms.append({
            "code": _count_failure_code(int(result.get("observed", 0)), assertion["count"])
        })
    else:
        raise CounterexampleRefusal(
            "unsupported_target", f"no typed oracle for assertion kind {kind!r}"
        )
    return _sorted_atoms(atoms)


def failure_identity_digest(identity: Dict[str, Any]) -> str:
    return prefixed_digest({
        "test_id": identity["test_id"],
        "assertion_digest": identity["assertion_digest"],
        "assertion_id": identity["assertion_id"],
        "kind": identity["kind"],
        "dimension": identity.get("dimension"),
        "authority": identity["authority"],
        "required_status": identity["required_status"],
        "failure_atom": identity["failure_atom"],
    })


def failure_fingerprint(
    test_id: str,
    assertion: Dict[str, Any],
    failure_atom: Dict[str, Any],
    source_failure_atoms: Optional[List[Dict[str, Any]]] = None,
) -> Dict[str, Any]:
    identity = {
        "test_id": test_id,
        "assertion_digest": prefixed_digest(assertion),
        "assertion_id": assertion["id"],
        "kind": assertion["kind"],
        "dimension": assertion.get("dimension"),
        "authority": "deterministic",
        "required_status": "FAIL",
        "failure_atom": failure_atom,
        "source_failure_atoms": list(source_failure_atoms or [failure_atom]),
    }
    identity["fingerprint"] = failure_identity_digest(identity)
    return identity


def _scope_freezes(assertion: Dict[str, Any], failure_atom: Dict[str, Any]) -> Set[str]:
    """Return explicit immutable components for reducers v1.

    Scripted candidates remain complete scenario documents and keep at least
    one caller turn. The selected structured failure branch carries the
    observation domain, so
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
            if len(_json_bytes(doc)) > _MAX_ORACLE_SCENARIO_BYTES:
                return {
                    "status": UNRESOLVED,
                    "code": "resource_limit_exceeded",
                    "detail": "scenario exceeds the deterministic oracle byte limit",
                }
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
            transcript_bytes = sum(
                len(str(turn.get("text") or "").encode("utf-8"))
                for turn in produced["transcript"]["segments"]
            )
            if transcript_bytes > _MAX_ORACLE_TRANSCRIPT_BYTES:
                return {
                    "status": UNRESOLVED,
                    "code": "resource_limit_exceeded",
                    "detail": "rendered transcript exceeds the deterministic oracle byte limit",
                }
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
            if (
                len(result.get("hits") or []) > _MAX_ORACLE_EVIDENCE_ROWS
                or len(result.get("matched_rules") or []) > _MAX_ORACLE_EVIDENCE_ROWS
                or len(_json_bytes(result)) > _MAX_ORACLE_RESULT_BYTES
            ):
                return {
                    "status": UNRESOLVED,
                    "code": "resource_limit_exceeded",
                    "detail": "assertion evidence exceeds the deterministic oracle limit",
                }
            atoms = (
                failure_atoms(self.assertion, result, ctx, doc)
                if result.get("status") == "FAIL"
                else []
            )
            if result.get("status") == "FAIL" and not atoms:
                return {
                    "status": UNRESOLVED,
                    "code": "failure_atom_unavailable",
                    "result": result,
                }
            atom = atoms[0] if atoms else None
            identity = (
                failure_fingerprint(
                    self.test_doc["id"], self.assertion, atom, atoms
                )
                if atom is not None
                else None
            )
            return {
                "status": PRESERVED if result.get("status") == "FAIL" else ABSENT,
                "code": "target_failed" if result.get("status") == "FAIL" else "target_absent",
                "result": result,
                "result_digest": prefixed_digest(result),
                "failure_atom": atom,
                "failure_atom_digest": prefixed_digest(atom) if atom else None,
                "failure_atoms": atoms,
                "identity": identity,
                "produced": produced,
            }
        except CounterexampleRefusal:
            raise
        except (
            ValueError,
            TypeError,
            AttributeError,
            OverflowError,
            OSError,
            RecursionError,
            MemoryError,
        ) as exc:
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
        self.frozen = _scope_freezes(self.assertion, result["failure_atom"])
        return result

    def evaluate(self, scenario: Dict[str, Any]) -> Dict[str, Any]:
        if self.source_identity is None:
            raise RuntimeError("freeze_source must run before candidate evaluation")
        result = self._evaluate_once(scenario)
        if result.get("status") != PRESERVED:
            return result
        source_atom = self.source_identity["failure_atom"]
        if source_atom not in result["failure_atoms"]:
            return {
                "status": DRIFTED,
                "code": "failure_identity_drift",
                "result_digest": result.get("result_digest"),
                "failure_atom_digest": result.get("failure_atom_digest"),
            }
        result["failure_atom"] = source_atom
        result["failure_atom_digest"] = prefixed_digest(source_atom)
        result["identity"] = dict(self.source_identity)
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
                "rule": (
                    "candidate is a complete schema-valid scripted session and must "
                    "preserve the source-selected structured failure branch"
                ),
                "minimum_caller_turns": 1,
                "transforms": "deletion-only",
            },
        }
