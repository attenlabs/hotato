"""``order`` assertions (assert.v1): a deterministic transcript ordering check.

Pins the honesty properties of the ``order`` kind, which checks that the FIRST
turn matching the ``before`` regex precedes the FIRST turn matching ``after``:

* it precedes -> PASS (carrying the two matched turn indices);
* it does not precede (wrong order, or same turn) -> FAIL;
* either phrase never matches -> a vacuous PASS (nothing to violate), never a
  guess;
* no transcript at all -> INCONCLUSIVE (absent input), never a fabricated
  verdict;
* an optional ``role`` filter and ``case_sensitive`` flag behave as declared;
* a missing/invalid ``before``/``after`` regex (or a non-string ``role`` /
  non-bool ``case_sensitive``) is a usage error caught up front (ValueError),
  before any assertion runs;
* every result is ``deterministic: true`` and a replay is byte-identical.
"""

from __future__ import annotations

import json
from importlib import resources

import pytest

from hotato import assert_ as A


def _turn(role, text):
    return {"role": role, "text": text}


def _ctx(*turns):
    return A.build_context(transcript=list(turns))


def _order(aid="ord", **extra):
    base = {"id": aid, "kind": "order", "before": "verify", "after": "balance"}
    base.update(extra)
    return base


def _run(assertion, ctx):
    env = A.run_assertions({"version": 1, "assertions": [assertion]}, ctx)
    return env["results"][0]


def _schema():
    return json.loads(
        resources.files("hotato").joinpath("schema", "assert.v1.json")
        .read_text(encoding="utf-8")
    )


# --- registration: part of the deterministic wall --------------------------

def test_order_is_a_registered_deterministic_kind():
    assert "order" in A.KINDS
    assert "order" in A.ALL_KINDS
    assert "order" in A._EVALUATORS
    enum = set(_schema()["definitions"]["result"]["properties"]["kind"]["enum"])
    assert "order" in enum
    assert enum == set(A.ALL_KINDS)


# --- precedes / does-not-precede -------------------------------------------

def test_before_precedes_after_passes():
    r = _run(_order(), _ctx(
        _turn("agent", "let me verify you"),
        _turn("caller", "ok"),
        _turn("agent", "your balance is fifty"),
    ))
    assert r["status"] == "PASS"
    assert r["deterministic"] is True
    assert r["before_turn"] == 0
    assert r["after_turn"] == 2
    assert "public_reason" not in r  # a PASS is never annotated


def test_wrong_order_fails():
    r = _run(_order(), _ctx(
        _turn("agent", "your balance is fifty"),
        _turn("agent", "now let me verify you"),
    ))
    assert r["status"] == "FAIL"
    assert r["before_turn"] == 1
    assert r["after_turn"] == 0
    # share-safe public_reason carries only the structured turn indices
    assert r["public_reason"]
    assert "verify" not in r["public_reason"]
    assert "balance" not in r["public_reason"]


def test_same_turn_is_not_preceding_and_fails():
    # both regexes first match on the SAME turn -> not strictly earlier -> FAIL
    r = _run(_order(), _ctx(_turn("agent", "verify then your balance is shown")))
    assert r["status"] == "FAIL"
    assert r["before_turn"] == 0
    assert r["after_turn"] == 0


# --- empty match set = vacuous PASS ----------------------------------------

def test_before_never_matches_is_vacuous_pass():
    r = _run(_order(), _ctx(_turn("agent", "your balance is fifty")))
    assert r["status"] == "PASS"
    assert r.get("vacuous") is True
    assert "before_turn" not in r


def test_after_never_matches_is_vacuous_pass():
    r = _run(_order(), _ctx(_turn("agent", "let me verify you")))
    assert r["status"] == "PASS"
    assert r.get("vacuous") is True


# --- no transcript = INCONCLUSIVE ------------------------------------------

def test_no_transcript_is_inconclusive():
    r = _run(_order(), A.build_context())  # transcript is None
    assert r["status"] == "INCONCLUSIVE"
    assert r["deterministic"] is True
    assert "public_reason" in r


# --- role filter ------------------------------------------------------------

def test_role_filter_only_considers_that_speaker():
    # the caller says "verify" first, but the role filter restricts to agent,
    # whose "verify" turn is after its "balance" turn -> FAIL under role=agent
    ctx = _ctx(
        _turn("caller", "please verify me"),
        _turn("agent", "your balance is fifty"),
        _turn("agent", "let me verify you"),
    )
    assert _run(_order(role="agent"), ctx)["status"] == "FAIL"
    # without the filter, the caller's "verify" (turn 0) precedes "balance" -> PASS
    assert _run(_order(), ctx)["status"] == "PASS"


# --- case sensitivity -------------------------------------------------------

def test_case_insensitive_by_default():
    r = _run(_order(before="VERIFY", after="BALANCE"), _ctx(
        _turn("agent", "verify"), _turn("agent", "balance"),
    ))
    assert r["status"] == "PASS"


def test_case_sensitive_flag_is_honored():
    # case_sensitive: an uppercase pattern does not match lowercase text, so
    # 'before' never matches -> vacuous PASS (nothing to order)
    r = _run(_order(before="VERIFY", after="balance", case_sensitive=True), _ctx(
        _turn("agent", "verify"), _turn("agent", "balance"),
    ))
    assert r["status"] == "PASS"
    assert r.get("vacuous") is True


# --- up-front validation (usage errors, exit 2) ----------------------------

@pytest.mark.parametrize("bad", [
    {"id": "x", "kind": "order", "after": "b"},                       # missing before
    {"id": "x", "kind": "order", "before": "a"},                      # missing after
    {"id": "x", "kind": "order", "before": "", "after": "b"},         # empty before
    {"id": "x", "kind": "order", "before": "a", "after": 5},          # non-string after
    {"id": "x", "kind": "order", "before": "(", "after": "b"},        # invalid regex
    {"id": "x", "kind": "order", "before": "a", "after": "b", "role": 5},
    {"id": "x", "kind": "order", "before": "a", "after": "b", "case_sensitive": "yes"},
])
def test_malformed_order_is_a_usage_error(bad):
    with pytest.raises(ValueError):
        A.validate_assertions_doc({"version": 1, "assertions": [bad]})


# --- determinism: byte-stable replay ---------------------------------------

def test_order_result_is_byte_stable():
    ctx = _ctx(_turn("agent", "verify"), _turn("agent", "balance"))
    doc = {"version": 1, "assertions": [_order()]}
    a = A.run_assertions(doc, ctx)
    b = A.run_assertions(doc, A.build_context(transcript=[
        _turn("agent", "verify"), _turn("agent", "balance"),
    ]))
    dumps = lambda e: json.dumps(e, sort_keys=True, separators=(",", ":"))
    assert dumps(a) == dumps(b)


# --- schema: an order envelope validates -----------------------------------

def test_order_envelope_validates_against_schema():
    jsonschema = pytest.importorskip("jsonschema")
    ctx = _ctx(_turn("agent", "verify"), _turn("agent", "balance"))
    env = A.run_assertions({"version": 1, "assertions": [_order()]}, ctx)
    jsonschema.validate(instance=env, schema=_schema())
