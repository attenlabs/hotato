"""Privacy sentinel -- the G1 merge blocker for the share-safe Failure Record.

The assertion engine's ``public_reason`` and the Failure Record projection may
quote ONLY allowlisted STRUCTURED fields (assertion kind/id, rule/tool/resource
ids, policy rule id+type, detector names, counts, span ids, numeric timing).
They must NEVER consume transcript text, a regex body, tool arguments/results,
state filters/values, DTMF digits, the raw evaluator ``reason`` (which can carry
an exception message), or an absolute path.

This plants one unique value -- ``SENTINEL-SECRET-9f3a77`` -- in each of those
UNSAFE source fields, drives the real evaluator (so the sentinel genuinely flows
into the place ``public_reason`` is built), projects a Failure Record, and
renders all four formats. It then asserts the sentinel is ABSENT from:

  * every evaluated result's ``public_reason``;
  * the record headline;
  * the whole projected record (JSON);
  * all four rendered formats (json/md/html/svg);
  * the contract-verify projection.

It fails GENUINELY if ``public_reason`` (or the projection) ever starts
consuming an unsafe field: point any template at ``a['regex']`` / ``args_subset``
/ ``digits`` / ``reason`` and a scenario below turns red.
"""

import json

import pytest

from hotato import assert_ as A
from hotato import failure_record as FR
from hotato import failure_render as FRR
from tests._failure_sources import (
    make_contract_result,
    make_contract_verify,
    make_test_run,
)

SENTINEL = "SENTINEL-SECRET-9f3a77"


class _FakeStateAdapter:
    """A post-call system of record whose rows never match the (sentinel-laden)
    filters/expectations, forcing a deterministic FAIL without ever echoing an
    input value back to the caller."""

    def query(self, resource, when=None, **filters):
        return {"status": "settled", "balance": 0}


def _eval(assertion, ctx):
    """Drive the real deterministic evaluator and return the result row (with
    its ``public_reason``); the row must be non-passing so a record projects."""
    row = A.evaluate_assertion(assertion, ctx)
    assert row["status"] in ("FAIL", "INCONCLUSIVE"), (assertion, row)
    return row


# --- structured-field plants: each runs through evaluate_assertion so the
# sentinel actually reaches the code path that builds public_reason -----------

def _scn_transcript_text():
    a = {"id": "greeting", "kind": "phrase", "regex": "welcome aboard",
         "dimension": "policy"}
    ctx = A.build_context(transcript=[{"role": "agent", "text": SENTINEL}])
    return {"rows": [_eval(a, ctx)]}


def _scn_phrase_regex():
    a = {"id": "greeting", "kind": "phrase", "regex": f"{SENTINEL}-never",
         "dimension": "policy"}
    ctx = A.build_context(transcript=[{"role": "agent", "text": "hello there"}])
    return {"rows": [_eval(a, ctx)]}


def _scn_tool_argument_subset():
    a = {"id": "charge-once", "kind": "tool_call", "name": "charge",
         "args_subset": {"api_key": SENTINEL}, "dimension": "outcome"}
    ctx = A.build_context(spans=[
        {"type": "tool_call", "name": "charge", "arguments": {"api_key": "real"}},
    ])
    return {"rows": [_eval(a, ctx)]}


def _scn_tool_result_subset():
    a = {"id": "fetch-token", "kind": "tool_result", "name": "fetch",
         "result_subset": {"token": SENTINEL}, "dimension": "outcome"}
    ctx = A.build_context(spans=[
        {"type": "tool_call", "name": "fetch", "result": {"token": "real"}},
    ])
    return {"rows": [_eval(a, ctx)]}


def _scn_state_filters_and_values():
    a = {"id": "acct-settled", "kind": "state", "resource": "account",
         "filters": {"id": SENTINEL}, "expect": {"status": SENTINEL},
         "dimension": "outcome"}
    ctx = A.build_context(state_adapter=_FakeStateAdapter())
    return {"rows": [_eval(a, ctx)]}


def _scn_state_change_before_after_values():
    a = {"id": "bal-zeroed", "kind": "state_change", "resource": "account",
         "field": "balance", "from": SENTINEL, "to": SENTINEL,
         "filters": {"id": SENTINEL}, "dimension": "outcome"}
    ctx = A.build_context(state_adapter=_FakeStateAdapter())
    return {"rows": [_eval(a, ctx)]}


def _scn_dtmf_digits():
    a = {"id": "keypad", "kind": "dtmf", "digits": SENTINEL,
         "dimension": "conversation"}
    ctx = A.build_context(spans=[{"type": "dtmf", "digits": "123"}])
    return {"rows": [_eval(a, ctx)]}


# --- projection-only plants: the sentinel is in the RAW reason (an exception
# message, an absolute path); the projection must never render it -------------

def _reason_row(reason):
    return [{"id": "probe", "kind": "tool_call", "deterministic": True,
             "status": "FAIL", "dimension": "conversation", "reason": reason}]


def _scn_raw_reason():
    return {"rows": _reason_row(f"raw evaluator reason carrying {SENTINEL}")}


def _scn_posix_path():
    return {"rows": _reason_row(f"query failed at /var/secrets/{SENTINEL}/db.json")}


def _scn_windows_path():
    return {"rows": _reason_row(rf"open C:\Users\{SENTINEL}\secret\db.json failed")}


def _scn_unc_path():
    return {"rows": _reason_row(rf"read \\fileserver\{SENTINEL}\creds.json failed")}


_SCENARIOS = {
    "transcript-text": _scn_transcript_text,
    "phrase-regex": _scn_phrase_regex,
    "tool-argument-subset": _scn_tool_argument_subset,
    "tool-result-subset": _scn_tool_result_subset,
    "state-filters-and-values": _scn_state_filters_and_values,
    "state-change-before-after": _scn_state_change_before_after_values,
    "dtmf-digits": _scn_dtmf_digits,
    "raw-reason": _scn_raw_reason,
    "posix-path": _scn_posix_path,
    "windows-path": _scn_windows_path,
    "unc-path": _scn_unc_path,
}


@pytest.mark.parametrize("name", sorted(_SCENARIOS))
def test_sentinel_never_reaches_a_share_safe_surface(name):
    scenario = _SCENARIOS[name]()
    rows = scenario["rows"]

    # 1. no evaluated result's public_reason may contain the sentinel -- this is
    #    the line that turns red if _public_reason consumes an unsafe field.
    for row in rows:
        assert SENTINEL not in (row.get("public_reason") or ""), (
            f"sentinel leaked into public_reason for {name}")

    record = FR.project(make_test_run(rows))

    # 2. the headline and 3. the whole projected record are sentinel-free.
    assert SENTINEL not in record["headline"], f"sentinel in headline ({name})"
    assert SENTINEL not in json.dumps(record), f"sentinel in record ({name})"

    # 4. every rendered format is sentinel-free.
    for fmt, content in FRR.render_all(record).items():
        assert SENTINEL not in content, f"sentinel leaked into {fmt} ({name})"


def test_sentinel_never_reaches_the_contract_projection():
    # A failing contract whose non-rendered fields (a raw provider blob, a side
    # channel) carry the sentinel: the numeric observed sentence and the record
    # must never surface it.
    env = make_contract_verify([
        make_contract_result("greeting-yield", passed=False, expect="yield",
                             did_yield=False, talk_over_sec=0.25),
    ])
    unit = env["results"][0]
    unit["measurement"]["raw_provider_blob"] = SENTINEL
    unit["secret_side_channel"] = SENTINEL

    record = FR.project(env)
    observed = record["dimensions"]["conversation"]["assertions"][0]["observed"]
    assert observed == "Agent did not yield; measured talk-over was 0.25 s."
    assert SENTINEL not in json.dumps(record)
    for fmt, content in FRR.render_all(record).items():
        assert SENTINEL not in content, f"sentinel leaked into {fmt}"


def test_the_sentinel_scenarios_actually_exercise_public_reason():
    # Guard the guard: every structured-field scenario must produce a row that
    # actually carries a public_reason (else the absence check above would pass
    # vacuously). The four projection-only reason/path scenarios intentionally
    # carry none (a legacy row), so they are excluded here.
    structured = [n for n in _SCENARIOS
                  if n not in ("raw-reason", "posix-path", "windows-path",
                               "unc-path")]
    for name in structured:
        rows = _SCENARIOS[name]()["rows"]
        assert any(r.get("public_reason") for r in rows), name
