"""Interrupt-mid-tool-call fixture bundle (`examples/interrupted-tool-call`).

A caller interrupts while a tool call is in flight. The in-flight result is
cancelled, discarded, or orphaned, and the side effect either double-fires (two
reservations, two deposits) or is discarded while the backend committed anyway.
Public evidence the class exists on two independent stacks: livekit/agents#3702
(interrupted tool result -> duplicate reservations / DB rows) and
pipecat-ai/pipecat#4936 (timed-out tool discarded while the side effect still
lands, so a retry can double-fire).

The class lands on hotato's say-do wedge: what the agent said happened versus
what the trace and the system of record hold. This module drives all three
variants through the SAME `hotato test run` the README documents, over committed
files only (no network), and pins the scored verdict:

  * clean-cancel  -> exit 0, every assertion PASS;
  * double-fire   -> exit 1, `booked-exactly-once` (tool_call count bound) AND
    `reservation-committed-once` (state) both FAIL, each with its own grounded
    evidence;
  * zombie        -> exit 1, `said-failed-but-record-landed` (state) FAIL: the
    agent reported failure while the record shows the booking landed.

Committed files, no random, no clock -> the replay is byte-identical.
"""

import json
import os

import pytest

from hotato import cli

BUNDLE = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "examples", "interrupted-tool-call",
)
AGENT = "reservation-agent-v1"


def _run_variant(variant, capsys):
    """Run one variant the way the README says a user runs it, and return
    ``(exit_code, result_dict)``."""
    d = os.path.join(BUNDLE, variant)
    code = cli.main([
        "test", "run", os.path.join(d, "test.json"),
        "--agent", AGENT,
        "--trace", os.path.join(d, "voice_trace.jsonl"),
        "--transcript", os.path.join(d, "transcript.json"),
        "--state", os.path.join(d, "sandbox.json"),
        "--format", "json",
    ])
    result = json.loads(capsys.readouterr().out)
    return code, result


def _status(result, assertion_id):
    for r in result["assertions"]["results"]:
        if r["id"] == assertion_id:
            return r
    raise AssertionError(f"assertion {assertion_id!r} not in results")


def test_clean_cancel_passes(capsys):
    # Barge-in lands while the tool_call span is open (tts_cancel_requested
    # fires), the operation completes exactly once, the sandbox holds one row.
    code, result = _run_variant("clean-cancel", capsys)
    assert code == 0
    assert result["exit_code"] == 0
    assert result["success"]["passed"] is True
    assert all(r["status"] == "PASS" for r in result["assertions"]["results"])


def test_double_fire_fails_on_count_and_state(capsys):
    # The retry double-fires: two book_table tool_call spans, two committed
    # holds. The tool_call count bound AND the state check both FAIL, each
    # grounded in its own authority (trace spans / system of record).
    code, result = _run_variant("double-fire", capsys)
    assert code == 1
    assert result["exit_code"] == 1
    assert result["success"]["passed"] is False

    count = _status(result, "booked-exactly-once")
    assert count["status"] == "FAIL"
    assert "2 time(s)" in count["reason"]

    state = _status(result, "reservation-committed-once")
    assert state["status"] == "FAIL"
    # grounded in the record, not the agent's words: the doubled fields.
    assert "deposit" in state["reason"] and "holds" in state["reason"]


def test_zombie_fails_on_state_say_do_mismatch(capsys):
    # The tool span times out unresolved and the agent reports failure, but the
    # backend committed. The state assertion catches the say-do mismatch: said
    # it failed, the record shows it landed.
    code, result = _run_variant("zombie", capsys)
    assert code == 1
    assert result["exit_code"] == 1
    assert result["success"]["passed"] is False

    # the agent DID say it failed -- that half of the say-do pair is true.
    assert _status(result, "agent-reported-failure")["status"] == "PASS"

    state = _status(result, "said-failed-but-record-landed")
    assert state["status"] == "FAIL"
    assert "active" in state["reason"] and "holds" in state["reason"]


@pytest.mark.parametrize("variant", ["clean-cancel", "double-fire", "zombie"])
def test_variant_replay_is_byte_identical(variant, capsys):
    # Two runs over the same committed files produce the same assertion verdicts
    # -- the determinism the fixture claims (no random, no wall clock).
    _, first = _run_variant(variant, capsys)
    _, second = _run_variant(variant, capsys)
    a = [(r["id"], r["status"]) for r in first["assertions"]["results"]]
    b = [(r["id"], r["status"]) for r in second["assertions"]["results"]]
    assert a == b
