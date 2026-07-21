"""``formula`` composite assertions and ``when:`` preconditions (assert.v1).

Pins the honesty properties of the two cross-assertion features:

* ``formula`` combines OTHER named assertions' results with and/or/not,
  parentheses, and a weighted-sum >= threshold form -- parsed by a
  recursive-descent parser (never eval()), evaluated over the SAME run's
  results, deterministic and byte-stable;
* unknown reference names, self-references, and reference cycles are REFUSED
  up front (ValueError, the exit-2 usage-error path), before anything runs;
* a referenced INCONCLUSIVE makes the formula INCONCLUSIVE -- absent input
  propagates, a composite never guesses;
* ``when:`` skips an assertion (INCONCLUSIVE with ``skipped: true`` and a
  reason) unless every referenced assertion PASSed -- the check never ran,
  so no verdict is fabricated;
* references evaluate before their dependents whatever the document order,
  and results are always emitted in document order.
"""

from __future__ import annotations

import json
from importlib import resources

import pytest

from hotato import assert_ as A

# --- fixtures / helpers -----------------------------------------------------


def _ctx():
    """A transcript-only context: phrase assertions on it are determinate,
    tool_call assertions (no trace supplied) are INCONCLUSIVE."""
    return A.build_context(transcript=[{"role": "agent", "text": "hello world"}])


def _doc(*assertions):
    return {"version": 1, "assertions": list(assertions)}


def _passing(aid):
    return {"id": aid, "kind": "phrase", "regex": "hello"}


def _failing(aid):
    return {"id": aid, "kind": "phrase", "regex": "never-said"}


def _inconclusive(aid):
    # tool_call with no trace in the context -> INCONCLUSIVE
    return {"id": aid, "kind": "tool_call", "name": "lookup"}


def _formula(aid, expr, **extra):
    return {"id": aid, "kind": "formula", "expr": expr, **extra}


def _by_id(env):
    return {r["id"]: r for r in env["results"]}


def _schema():
    return json.loads(
        resources.files("hotato").joinpath("schema", "assert.v1.json")
        .read_text(encoding="utf-8")
    )


# =========================================================================
# registration: the kind is part of the deterministic wall
# =========================================================================

def test_formula_is_a_registered_deterministic_kind():
    assert "formula" in A.KINDS
    assert "formula" in A.ALL_KINDS
    assert "formula" in A._EVALUATORS
    enum = set(_schema()["definitions"]["result"]["properties"]["kind"]["enum"])
    assert "formula" in enum


# =========================================================================
# formula truth table: and / or / not / parentheses over other results
# =========================================================================

@pytest.mark.parametrize("expr,expected", [
    ("p1 and p2", "PASS"),
    ("p1 and f1", "FAIL"),
    ("f1 and f2", "FAIL"),
    ("p1 or f1", "PASS"),
    ("f1 or p1", "PASS"),
    ("f1 or f2", "FAIL"),
    ("not f1", "PASS"),
    ("not p1", "FAIL"),
    ("not (p1 and f1)", "PASS"),
    ("not (p1 or f1)", "FAIL"),
    ("p1 and (f1 or p2)", "PASS"),
    ("p1 and not (f1 or f2)", "PASS"),
    ("not p1 or p2", "PASS"),          # precedence: (not p1) or p2
    ("not f1 and p1", "PASS"),         # precedence: (not f1) and p1
    ("p1 or f1 and f2", "PASS"),       # precedence: p1 or (f1 and f2)
])
def test_formula_truth_table(expr, expected):
    env = A.run_assertions(
        _doc(
            _passing("p1"), _passing("p2"), _failing("f1"), _failing("f2"),
            _formula("combo", expr),
        ),
        _ctx(),
    )
    r = _by_id(env)["combo"]
    assert r["status"] == expected
    assert r["deterministic"] is True


def test_formula_fail_carries_refs_met_of_and_reasons():
    env = A.run_assertions(
        _doc(_passing("p1"), _failing("f1"), _formula("combo", "p1 and f1")),
        _ctx(),
    )
    r = _by_id(env)["combo"]
    assert r["status"] == "FAIL"
    assert r["refs"] == ["p1", "f1"]          # first-reference order, deduped
    assert r["met"] == 1
    assert r["of"] == 2
    assert "f1" in r["reason"]
    assert r["public_reason"] == (
        "The declared formula was not satisfied; 1 of 2 referenced checks "
        "passed."
    )
    assert env["exit_code"] == 1


def test_formula_refs_are_deduplicated():
    env = A.run_assertions(
        _doc(_passing("p1"), _failing("f1"),
             _formula("combo", "(p1 or f1) and (p1 or not f1)")),
        _ctx(),
    )
    r = _by_id(env)["combo"]
    assert r["refs"] == ["p1", "f1"]
    assert r["of"] == 2


# =========================================================================
# the weighted-sum >= threshold form
# =========================================================================

def test_weighted_sum_passes_at_and_above_threshold():
    env = A.run_assertions(
        _doc(_passing("p1"), _failing("f1"),
             _formula("at", "0.6*p1 + 0.4*f1 >= 0.6"),
             _formula("above", "0.6*p1 + 0.4*f1 >= 0.5")),
        _ctx(),
    )
    by_id = _by_id(env)
    assert by_id["at"]["status"] == "PASS"       # 0.6 >= 0.6
    assert by_id["above"]["status"] == "PASS"    # 0.6 >= 0.5


def test_weighted_sum_fails_below_threshold():
    env = A.run_assertions(
        _doc(_passing("p1"), _failing("f1"),
             _formula("combo", "0.6*p1 + 0.4*f1 >= 0.7")),
        _ctx(),
    )
    assert _by_id(env)["combo"]["status"] == "FAIL"


def test_weighted_sum_bare_names_weigh_one():
    env = A.run_assertions(
        _doc(_passing("p1"), _passing("p2"), _failing("f1"),
             _formula("two-of-three", "p1 + p2 + f1 >= 2")),
        _ctx(),
    )
    assert _by_id(env)["two-of-three"]["status"] == "PASS"


def test_weighted_sum_composes_inside_a_boolean_expression():
    env = A.run_assertions(
        _doc(_passing("p1"), _failing("f1"), _passing("p2"),
             _formula("combo", "(0.5*p1 + 0.5*f1 >= 0.5) and p2")),
        _ctx(),
    )
    assert _by_id(env)["combo"]["status"] == "PASS"


# =========================================================================
# refusal: unknown names, self-references, cycles -- all before anything runs
# =========================================================================

def test_formula_unknown_reference_is_refused():
    with pytest.raises(ValueError, match="unknown"):
        A.validate_assertions_doc(
            _doc(_passing("p1"), _formula("combo", "p1 and ghost"))
        )


def test_formula_self_reference_is_refused():
    with pytest.raises(ValueError, match="itself"):
        A.validate_assertions_doc(_doc(_formula("combo", "combo")))


def test_formula_cycle_is_refused():
    with pytest.raises(ValueError, match="cycle"):
        A.validate_assertions_doc(
            _doc(_formula("a", "b"), _formula("b", "a"))
        )


def test_when_unknown_reference_is_refused():
    with pytest.raises(ValueError, match="unknown"):
        A.validate_assertions_doc(
            _doc(dict(_passing("p1"), when="ghost"))
        )


def test_when_self_reference_is_refused():
    with pytest.raises(ValueError, match="itself"):
        A.validate_assertions_doc(_doc(dict(_passing("p1"), when="p1")))


def test_cycle_through_when_and_formula_is_refused():
    with pytest.raises(ValueError, match="cycle"):
        A.validate_assertions_doc(
            _doc(dict(_passing("p1"), when="combo"), _formula("combo", "p1"))
        )


def test_run_assertions_refuses_before_evaluating_anything():
    # A cyclic document never yields a partial envelope.
    with pytest.raises(ValueError, match="cycle"):
        A.run_assertions(
            _doc(_formula("a", "b"), _formula("b", "a")), _ctx()
        )


# =========================================================================
# refusal: malformed expressions and malformed when fields
# =========================================================================

@pytest.mark.parametrize("bad", [
    {"id": "x", "kind": "formula"},                      # expr missing
    {"id": "x", "kind": "formula", "expr": ""},          # expr empty
    {"id": "x", "kind": "formula", "expr": "   "},       # expr blank
    {"id": "x", "kind": "formula", "expr": 7},           # expr not a string
])
def test_formula_requires_a_non_empty_expr(bad):
    with pytest.raises(ValueError, match="expr"):
        A.validate_assertions_doc(_doc(bad))


@pytest.mark.parametrize("expr", [
    "p1 and",                 # ends unexpectedly
    "and p1",                 # operator with no left operand
    "p1 && p2",               # unknown character
    "(p1",                    # missing closing paren
    "p1)",                    # trailing token
    "0.5*p1",                 # weighted sum without '>= threshold'
    "0.5*p1 >= ",             # threshold missing
    "0.5 + p1 >= 1",          # a weight must be followed by '*name'
    "p1 * 0.5 >= 0.4",        # weights are written weight*name
    "2fa and p1",             # a referenced id must not start with a digit
])
def test_malformed_formula_expressions_are_refused(expr):
    with pytest.raises(ValueError, match="formula"):
        A.validate_assertions_doc(_doc(_passing("p1"), _formula("combo", expr)))


@pytest.mark.parametrize("when", [5, "", [], [1], ["ok", ""], {"id": "x"}])
def test_malformed_when_fields_are_refused(when):
    with pytest.raises(ValueError, match="'when'"):
        A.validate_assertions_doc(
            _doc(_passing("gate"), dict(_passing("p1"), when=when))
        )


# =========================================================================
# when: skip semantics
# =========================================================================

def test_when_evaluates_normally_after_a_passing_precondition():
    env = A.run_assertions(
        _doc(_passing("gate"), dict(_failing("check"), when="gate")),
        _ctx(),
    )
    r = _by_id(env)["check"]
    assert r["status"] == "FAIL"           # ran, and failed on its own merits
    assert "skipped" not in r


def test_when_skips_on_a_failed_precondition():
    env = A.run_assertions(
        _doc(_failing("gate"), dict(_passing("check"), when="gate")),
        _ctx(),
    )
    r = _by_id(env)["check"]
    assert r["status"] == "INCONCLUSIVE"
    assert r["skipped"] is True
    assert "skipped" in r["reason"] and "gate" in r["reason"]
    assert r["public_reason"] == (
        "A declared precondition did not pass; the check was skipped."
    )


def test_when_skips_on_an_inconclusive_precondition():
    env = A.run_assertions(
        _doc(_inconclusive("gate"), dict(_passing("check"), when="gate")),
        _ctx(),
    )
    r = _by_id(env)["check"]
    assert r["status"] == "INCONCLUSIVE"
    assert r["skipped"] is True


def test_when_list_requires_every_reference_to_pass():
    env = A.run_assertions(
        _doc(_passing("g1"), _failing("g2"),
             dict(_passing("check"), when=["g1", "g2"])),
        _ctx(),
    )
    r = _by_id(env)["check"]
    assert r["status"] == "INCONCLUSIVE"
    assert r["skipped"] is True
    assert "g2" in r["reason"] and "g1" not in r["reason"]


def test_when_list_evaluates_when_all_pass():
    env = A.run_assertions(
        _doc(_passing("g1"), _passing("g2"),
             dict(_passing("check"), when=["g1", "g2"])),
        _ctx(),
    )
    assert _by_id(env)["check"]["status"] == "PASS"


def test_when_skip_gates_under_inconclusive_policy_fail():
    env = A.run_assertions(
        _doc(_failing("gate"), dict(_passing("check"), when="gate")),
        _ctx(),
        inconclusive_policy="fail",
    )
    assert env["exit_code"] == 1


# =========================================================================
# evaluation order: references first, document-order output always
# =========================================================================

def test_formula_may_reference_later_assertions():
    env = A.run_assertions(
        _doc(_formula("combo", "p1 and p2"), _passing("p1"), _passing("p2")),
        _ctx(),
    )
    assert [r["id"] for r in env["results"]] == ["combo", "p1", "p2"]
    assert env["results"][0]["status"] == "PASS"


def test_formula_may_reference_another_formula():
    env = A.run_assertions(
        _doc(_formula("outer", "inner and p1"),
             _formula("inner", "p1"),
             _passing("p1")),
        _ctx(),
    )
    by_id = _by_id(env)
    assert by_id["inner"]["status"] == "PASS"
    assert by_id["outer"]["status"] == "PASS"


def test_when_may_reference_a_later_assertion():
    env = A.run_assertions(
        _doc(dict(_passing("check"), when="gate"), _failing("gate")),
        _ctx(),
    )
    assert [r["id"] for r in env["results"]] == ["check", "gate"]
    assert env["results"][0]["skipped"] is True


def test_formula_with_when_skips_before_combining():
    env = A.run_assertions(
        _doc(_failing("gate"), _passing("p1"),
             _formula("combo", "p1", when="gate")),
        _ctx(),
    )
    r = _by_id(env)["combo"]
    assert r["status"] == "INCONCLUSIVE"
    assert r["skipped"] is True


# =========================================================================
# INCONCLUSIVE propagation: a composite never guesses over absent input
# =========================================================================

def test_formula_is_inconclusive_when_a_reference_is_inconclusive():
    env = A.run_assertions(
        _doc(_inconclusive("t"), _passing("p1"), _formula("combo", "p1 and t")),
        _ctx(),
    )
    r = _by_id(env)["combo"]
    assert r["status"] == "INCONCLUSIVE"
    assert "t" in r["reason"]
    assert r["deterministic"] is True
    assert env["exit_code"] == 0            # default policy: never gates


def test_formula_referencing_a_skipped_assertion_is_inconclusive():
    env = A.run_assertions(
        _doc(_failing("gate"), dict(_passing("check"), when="gate"),
             _formula("combo", "check")),
        _ctx(),
    )
    assert _by_id(env)["combo"]["status"] == "INCONCLUSIVE"


# =========================================================================
# standalone evaluation: no run results is absent input, not a guess
# =========================================================================

def test_formula_standalone_evaluation_is_inconclusive():
    r = A.evaluate_assertion(_formula("combo", "a and b"), A.build_context())
    assert r["status"] == "INCONCLUSIVE"
    assert r["deterministic"] is True


def test_when_standalone_evaluation_is_an_inconclusive_skip():
    r = A.evaluate_assertion(dict(_passing("check"), when="gate"), _ctx())
    assert r["status"] == "INCONCLUSIVE"
    assert r["skipped"] is True


# =========================================================================
# determinism, YAML round-trip, and the envelope schema
# =========================================================================

YAML_EXAMPLE = """\
version: 1
assertions:
  - id: said-hello
    kind: phrase
    regex: "hello"
  - id: said-goodbye
    kind: phrase
    regex: "goodbye"
  - id: either-greeting
    kind: formula
    expr: "said-hello or said-goodbye"
  - id: weighted-health
    kind: formula
    expr: "0.7*said-hello + 0.3*said-goodbye >= 0.6"
  - id: goodbye-followup
    kind: phrase
    regex: "see you"
    when: said-goodbye
"""


def test_yaml_document_with_formula_and_when_end_to_end():
    env = A.run_assertions_from_yaml(YAML_EXAMPLE, _ctx())
    by_id = _by_id(env)
    assert by_id["said-hello"]["status"] == "PASS"
    assert by_id["said-goodbye"]["status"] == "FAIL"
    assert by_id["either-greeting"]["status"] == "PASS"
    assert by_id["weighted-health"]["status"] == "PASS"     # 0.7 >= 0.6
    assert by_id["goodbye-followup"]["status"] == "INCONCLUSIVE"
    assert by_id["goodbye-followup"]["skipped"] is True
    assert [r["id"] for r in env["results"]] == [
        "said-hello", "said-goodbye", "either-greeting", "weighted-health",
        "goodbye-followup",
    ]
    assert env["exit_code"] == 1            # said-goodbye FAILed


def test_formula_run_is_byte_stable():
    first = A.run_assertions_from_yaml(YAML_EXAMPLE, _ctx())
    second = A.run_assertions_from_yaml(YAML_EXAMPLE, _ctx())
    assert json.dumps(first, sort_keys=True) == json.dumps(second, sort_keys=True)


def test_formula_and_skip_results_validate_against_the_schema():
    jsonschema = pytest.importorskip("jsonschema")
    env = A.run_assertions_from_yaml(YAML_EXAMPLE, _ctx())
    jsonschema.validate(instance=env, schema=_schema())


def test_summary_counts_include_formula_and_skip_results():
    env = A.run_assertions_from_yaml(YAML_EXAMPLE, _ctx())
    det = env["summary"]["deterministic"]
    assert det == {"pass": 3, "fail": 1, "inconclusive": 1}
    assert env["summary"]["judge"] == {"pass": 0, "fail": 0}
