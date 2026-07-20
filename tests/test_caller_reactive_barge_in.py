"""Hermetic proof of the reactive barge-in caller's REACTIVE property.

A fixed-timeline caller speaks at wall-clock offsets from call start; a reactive
caller barges in WHEN the agent starts talking.  These tests exercise
``caller.reactive_barge_in_plan`` through the deterministic ``FakeSession``
harness (reused from ``tests.test_caller``) and prove that the caller's
interrupting utterance is keyed to the agent-speech ONSET event, not to a clock:

* order is receive -> wait -> say -> hangup and the wait carries the configured delay,
* the say cannot fire before the onset event is received, and shifting the onset
  moves the interrupt with it (whereas a fixed-timeline plan does not move),
* with no onset event the listen times out to ``giveup`` and NO say is emitted.

No network, no audio, no STT: the onset event is emitted deterministically and
the run uses an injected constant clock plus a fixed ``created_at`` so results
are byte-stable.
"""

from __future__ import annotations

from hotato import caller
from tests.test_caller import FakeSession, node, plan

# A constant clock makes the run's elapsed timing deterministic (the reactive
# delay itself is carried by the plan's ``wait`` node, not by wall time).
FROZEN_CLOCK = lambda: 0.0  # noqa: E731
CREATED_AT = "2026-07-20T00:00:00Z"

# A lifecycle event that is NOT the onset (different status), used to push the
# agent-speech onset later in the event stream without matching the trigger.
NON_ONSET_EVENT = {"kind": "lifecycle", "status": "connected"}


def _op_names(session):
    return [op[0] for op in session.operations]


def test_onset_event_drives_receive_wait_say_hangup_order_with_delay(tmp_path):
    """(a) With the onset event present: receive -> wait -> say -> hangup, and the
    wait carries the configured delay."""
    graph = caller.reactive_barge_in_plan(
        text="Sorry to cut in -- I need the refund total.",
        delay_ms=180,
        listen_timeout_ms=5_000,
    )
    session = FakeSession([caller.agent_speech_started_event()])

    run = caller.run_caller(
        graph, session, str(tmp_path / "reactive"),
        clock=FROZEN_CLOCK, created_at=CREATED_AT,
    )

    assert run.exit_code == 0
    assert run.result["status"] == "HUNG_UP"
    assert _op_names(session) == ["receive", "wait", "send_text", "hangup"]
    # the wait between the onset and the say carried exactly the configured delay
    assert session.operations[1] == ("wait", 180)
    assert session.operations[-1] == ("hangup", "reactive_barge_in_complete")
    # the onset event was consumed by the listen, and the caller spoke once
    assert [e["kind"] for e in run.result["events"]] == ["lifecycle"]
    assert run.result["events"][0]["status"] == "agent_speech_started"
    say_actions = [a for a in run.result["actions"] if a["action"] == "say"]
    assert len(say_actions) == 1
    assert run.verification["ok"]


def test_interrupt_is_measured_from_the_event_not_call_start(tmp_path):
    """(b) The say cannot fire before the onset receive, and the delay between the
    onset receive and the say equals the configured N (structural + injected clock)."""
    delay_ms = 250
    graph = caller.reactive_barge_in_plan(
        text="One moment -- can you repeat the amount?",
        delay_ms=delay_ms,
        listen_timeout_ms=4_000,
    )
    session = FakeSession([caller.agent_speech_started_event()])

    run = caller.run_caller(
        graph, session, str(tmp_path / "measured"),
        clock=FROZEN_CLOCK, created_at=CREATED_AT,
    )

    names = _op_names(session)
    onset_receive = names.index("receive")
    say = names.index("send_text")
    # structural: the say is strictly AFTER the onset receive; nothing is spoken
    # before the onset is observed.
    assert say > onset_receive
    assert "send_text" not in names[:onset_receive + 1]
    # the only thing between the onset receive and the say is the configured delay
    assert names[onset_receive:say + 1] == ["receive", "wait", "send_text"]
    wait_op = session.operations[names.index("wait")]
    assert wait_op == ("wait", delay_ms)
    assert run.exit_code == 0


def test_no_onset_times_out_to_giveup_and_never_speaks(tmp_path):
    """(c) With no agent-speech event the listen times out, routes to ``giveup``,
    and NO say is emitted -- it reacts to the signal, not to a clock."""
    graph = caller.reactive_barge_in_plan(
        text="This line should never be spoken.",
        delay_ms=200,
        listen_timeout_ms=3_000,
    )
    session = FakeSession([])  # the agent never starts talking

    run = caller.run_caller(
        graph, session, str(tmp_path / "giveup"),
        clock=FROZEN_CLOCK, created_at=CREATED_AT,
    )

    assert run.exit_code == 0
    assert run.result["status"] == "HUNG_UP"
    # listened once (got a timeout), then hung up WITHOUT ever speaking
    assert _op_names(session) == ["receive", "hangup"]
    assert "send_text" not in _op_names(session)
    assert session.operations[-1] == ("hangup", "agent_speech_onset_not_detected")
    assert [a["action"] for a in run.result["actions"]] == ["hangup"]
    assert not any(a["action"] == "say" for a in run.result["actions"])
    # the listen recorded a local-timer timeout, not an agent-speech event
    assert [e["kind"] for e in run.result["events"]] == ["timeout"]
    assert run.verification["ok"]


def test_reactive_interrupt_tracks_onset_while_fixed_timeline_does_not(tmp_path):
    """The reactive-vs-fixed distinction, directly: shifting the onset later moves
    the reactive interrupt later, but a fixed-timeline plan's interrupt does not
    move and never consumes the onset event."""
    reactive = caller.reactive_barge_in_plan(
        text="Cutting in now.", delay_ms=120, listen_timeout_ms=5_000,
    )

    # Onset at the very start of the stream.
    early = FakeSession([caller.agent_speech_started_event()])
    early_run = caller.run_caller(
        reactive, early, str(tmp_path / "early"),
        clock=FROZEN_CLOCK, created_at=CREATED_AT,
    )
    early_say = _op_names(early).index("send_text")

    # Same plan, onset pushed two non-onset lifecycle events later.
    late = FakeSession([
        NON_ONSET_EVENT, NON_ONSET_EVENT, caller.agent_speech_started_event(),
    ])
    late_run = caller.run_caller(
        reactive, late, str(tmp_path / "late"),
        clock=FROZEN_CLOCK, created_at=CREATED_AT,
    )
    late_say = _op_names(late).index("send_text")

    # REACTIVE: the barge-in tracks the onset -- a later onset means more receives
    # before the say, so the interrupt lands later in the operation stream.
    assert late_say > early_say
    assert late_say - early_say == 2  # exactly the two extra pre-onset receives
    # and the reactive caller consumed the onset events (three vs one).
    assert len(late_run.result["events"]) == 3
    assert len(early_run.result["events"]) == 1

    # FIXED-TIMELINE control: say -> wait -> say -> hangup, no listen. Its first
    # interrupt is at a constant offset regardless of the event stream, and it
    # never consumes the agent-speech onset event.
    fixed = plan([
        node("open", "say", "gap", text="Opening line."),
        node("gap", "wait", "cut", duration_ms=120),
        node("cut", "say", "done", text="Fixed interrupt."),
        node("done", "hangup", reason="fixed_complete"),
    ])
    fixed_no_onset = FakeSession([])
    fixed_with_onset = FakeSession([
        NON_ONSET_EVENT, NON_ONSET_EVENT, caller.agent_speech_started_event(),
    ])
    a = caller.run_caller(
        fixed, fixed_no_onset, str(tmp_path / "fixed-a"),
        clock=FROZEN_CLOCK, created_at=CREATED_AT,
    )
    b = caller.run_caller(
        fixed, fixed_with_onset, str(tmp_path / "fixed-b"),
        clock=FROZEN_CLOCK, created_at=CREATED_AT,
    )
    # the fixed interrupt (second say) sits at the same op index either way ...
    assert _op_names(fixed_no_onset) == _op_names(fixed_with_onset)
    assert _op_names(fixed_no_onset) == ["send_text", "wait", "send_text", "hangup"]
    # ... and the fixed plan never listened, so the onset event was left unread.
    assert a.result["events"] == []
    assert b.result["events"] == []


def test_reactive_run_is_byte_stable_across_identical_seeds(tmp_path):
    """Deterministic-lane guarantee: same plan + same onset stream + same clock and
    ``created_at`` produce byte-identical evidence run to run."""
    graph = caller.reactive_barge_in_plan(
        text="Byte-stable barge-in.", delay_ms=150, listen_timeout_ms=6_000,
    )

    def _run(name):
        session = FakeSession([caller.agent_speech_started_event()])
        return caller.run_caller(
            graph, session, str(tmp_path / name),
            clock=FROZEN_CLOCK, created_at=CREATED_AT,
        )

    first = _run("first")
    second = _run("second")

    assert first.result == second.result
    assert first.result["result_id"] == second.result["result_id"]
    assert first.verification["package_id"] == second.verification["package_id"]
    assert first.verification["ok"] and second.verification["ok"]
