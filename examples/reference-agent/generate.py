#!/usr/bin/env python3
"""Generate the Conversation-QA Foundation 1.3 REFERENCE AGENT: >=25 scenario.v1
files across realistic voice-agent jobs, a paired conversation-test.v1 per job,
and one suite.v1 binding them (1.3 item 11).

Each scenario declares a scripted caller PLUS a deterministic ``agent_mock`` (the
Phase-2 tool mocks + state sandbox): the mock agent's tool calls become
Authority-1 ``tool_call`` spans and its post-call ``state`` becomes an
Authority-2 sandbox, so the conversation-tests exercise the OUTCOME and POLICY
authorities offline -- never from the agent's spoken claim. The variation matrix
gives each scenario 5 caller behaviours (speaking paces 0.7x-1.4x) x 3 audio
environments = 15 runs; 25 scenarios => 375 runs, executed OFFLINE through the
deterministic simulator (``hotato.simulate.run_matrix``) by ``run_reference.py``.

The reference agent is a realistic agent under test: MOST jobs are handled
correctly, and a HANDFUL carry genuine DEFECTS (a skipped tool, a wrong post-call
state, a missing escalation, a slow tool) so the suite surfaces real failures --
the raw material for failure clustering and the production-to-regression flow.

This writes JSON files (the zero-install parser reads JSON or the small YAML
subset). Re-runnable + deterministic: same inputs -> byte-identical files.
"""

from __future__ import annotations

import json
import os
import re

HERE = os.path.dirname(os.path.abspath(__file__))
SCN_DIR = os.path.join(HERE, "scenarios")
TEST_DIR = os.path.join(HERE, "tests")

# 5 caller behaviours = 5 speaking paces (a deterministic, genuinely-distinct
# render axis: pace scales every turn's timing, so each pace is a different
# content hash). 3 audio environments = 3 noise labels (attribution axis).
BEHAVIOURS = [0.7, 0.85, 1.0, 1.2, 1.4]
ENVIRONMENTS = ["clean", "cafe", "street"]


def _tool(name, arguments, result=None, error=None, latency_ms=None):
    t = {"name": name, "arguments": arguments}
    if error is not None:
        t["error"] = error
    else:
        t["result"] = result if result is not None else {}
    if latency_ms is not None:
        t["latency_ms"] = latency_ms
    return t


# =========================================================================
# The 25 jobs. Each: (id, goal, target, facts, caller lines, tools, state,
# assertions, note). A `defect` marks a genuine agent bug the suite must catch.
# =========================================================================

def _jobs():
    jobs = []

    # ---- appointment booking / cancel / reschedule ----------------------
    jobs.append(dict(
        id="appointment-book-basic",
        goal=("book_appointment", "cardiology follow-up"),
        facts={"patient_id": "patient_104", "clinic": "cardiology"},
        caller=["hi, this is for patient_104",
                "i would like to book a cardiology follow-up next week"],
        tools=[_tool("verify_identity", {"patient_id": "patient_104"}, {"verified": True}, latency_ms=420),
               _tool("book_appointment", {"patient_id": "patient_104", "clinic": "cardiology"},
                     {"status": "confirmed", "appointment_id": "appt_900"}, latency_ms=780)],
        state={"appointments": [{"appointment_id": "appt_900", "status": "confirmed"}]},
        outcome_tool=("book_appointment", {"status": "confirmed"}),
        state_check=("appointments", {"appointment_id": "appt_900"}, {"status": "confirmed"}),
        identity_first="book_appointment",
        speech_latency=("book_appointment", 1500),
    ))
    jobs.append(dict(
        id="appointment-cancel-after-cutoff",
        goal=("cancel_appointment", "appt_772"),
        facts={"patient_id": "patient_104", "appointment_id": "appt_772"},
        caller=["hello, patient_104 here",
                "i need to cancel appointment appt_772 please"],
        tools=[_tool("verify_identity", {"patient_id": "patient_104"}, {"verified": True}, latency_ms=400),
               _tool("read_disclosure", {"policy": "cancellation_policy_v2"}, {"read": True}, latency_ms=210),
               _tool("cancel_appointment", {"appointment_id": "appt_772"},
                     {"status": "cancelled"}, latency_ms=650)],
        state={"appointments": [{"appointment_id": "appt_772", "status": "cancelled"}]},
        outcome_tool=("cancel_appointment", {"status": "cancelled"}),
        state_check=("appointments", {"appointment_id": "appt_772"}, {"status": "cancelled"}),
        identity_first="cancel_appointment",
        disclosure="read_disclosure",
        speech_latency=("cancel_appointment", 1500),
    ))
    jobs.append(dict(
        id="appointment-reschedule",
        goal=("reschedule_appointment", "appt_512"),
        facts={"patient_id": "patient_221", "appointment_id": "appt_512"},
        caller=["this is patient_221",
                "can we move appointment appt_512 to thursday instead of tuesday"],
        tools=[_tool("verify_identity", {"patient_id": "patient_221"}, {"verified": True}, latency_ms=390),
               _tool("reschedule_appointment", {"appointment_id": "appt_512", "to": "thursday"},
                     {"status": "rescheduled", "new_day": "thursday"}, latency_ms=910)],
        state={"appointments": [{"appointment_id": "appt_512", "status": "rescheduled",
                                 "day": "thursday"}]},
        outcome_tool=("reschedule_appointment", {"status": "rescheduled"}),
        state_check=("appointments", {"appointment_id": "appt_512"}, {"day": "thursday"}),
        identity_first="reschedule_appointment",
        speech_latency=("reschedule_appointment", 1500),
    ))

    # ---- refunds --------------------------------------------------------
    jobs.append(dict(
        id="refund-damaged-order",
        goal=("get_refund", "order A-1001"),
        facts={"order_id": "A-1001"},
        caller=["hi, my order is A-1001 and it arrived damaged",
                "i would like a refund please"],
        tools=[_tool("lookup_order", {"order_id": "A-1001"}, {"found": True, "total": 42}, latency_ms=300),
               _tool("issue_refund", {"order_id": "A-1001"}, {"status": "refunded", "amount": 42}, latency_ms=560)],
        state={"orders": [{"order_id": "A-1001", "refund_status": "refunded"}]},
        outcome_tool=("issue_refund", {"status": "refunded"}),
        state_check=("orders", {"order_id": "A-1001"}, {"refund_status": "refunded"}),
        sequence=[("lookup_order",), ("issue_refund",)],
        speech_latency=("issue_refund", 1500),
    ))
    jobs.append(dict(
        id="refund-partial",
        goal=("get_partial_refund", "order A-2044"),
        facts={"order_id": "A-2044"},
        caller=["order A-2044 was missing one item",
                "can i get a partial refund for the missing item"],
        tools=[_tool("lookup_order", {"order_id": "A-2044"}, {"found": True}, latency_ms=280),
               _tool("issue_refund", {"order_id": "A-2044", "kind": "partial"},
                     {"status": "refunded", "amount": 12}, latency_ms=540)],
        state={"orders": [{"order_id": "A-2044", "refund_status": "partial"}]},
        outcome_tool=("issue_refund", {"status": "refunded"}),
        state_check=("orders", {"order_id": "A-2044"}, {"refund_status": "partial"}),
        sequence=[("lookup_order",), ("issue_refund",)],
        speech_latency=("issue_refund", 1500),
    ))
    # DEFECT: the agent claims a refund but never calls issue_refund (outcome FAILs).
    jobs.append(dict(
        id="refund-claimed-not-issued",
        goal=("get_refund", "order A-3090"),
        facts={"order_id": "A-3090"},
        caller=["order A-3090 never arrived",
                "i want a refund for order A-3090"],
        tools=[_tool("lookup_order", {"order_id": "A-3090"}, {"found": True}, latency_ms=300)],
        state={"orders": [{"order_id": "A-3090", "refund_status": "none"}]},
        outcome_tool=("issue_refund", {"status": "refunded"}),   # asserted but never called -> FAIL
        state_check=("orders", {"order_id": "A-3090"}, {"refund_status": "refunded"}),  # -> FAIL
        defect="the mock agent said the refund was done but never called issue_refund",
    ))

    # ---- identity verification -----------------------------------------
    jobs.append(dict(
        id="identity-verify-success",
        goal=("verify_identity", "patient_500"),
        facts={"patient_id": "patient_500", "dob": "1980-02-11"},
        caller=["i'm patient_500, date of birth 1980-02-11",
                "i need to access my records"],
        tools=[_tool("verify_identity", {"patient_id": "patient_500", "dob": "1980-02-11"},
                     {"verified": True}, latency_ms=460)],
        state={"sessions": [{"patient_id": "patient_500", "authenticated": True}]},
        outcome_tool=("verify_identity", {"verified": True}),
        state_check=("sessions", {"patient_id": "patient_500"}, {"authenticated": True}),
        speech_latency=("verify_identity", 1500),
    ))
    # DEFECT: the agent looks up records BEFORE verifying identity (policy FAILs).
    jobs.append(dict(
        id="identity-skipped-before-lookup",
        goal=("access_records", "patient_808"),
        facts={"patient_id": "patient_808"},
        caller=["hi it's patient_808",
                "pull up my lab results please"],
        tools=[_tool("lookup_records", {"patient_id": "patient_808"}, {"records": 3}, latency_ms=350),
               _tool("verify_identity", {"patient_id": "patient_808"}, {"verified": True}, latency_ms=420)],
        state={"sessions": [{"patient_id": "patient_808", "authenticated": True}]},
        identity_first="lookup_records",   # verify_identity AFTER lookup -> sequence FAIL
        defect="the mock agent looked up records before verifying identity",
    ))

    # ---- escalation / handoff ------------------------------------------
    jobs.append(dict(
        id="escalate-to-human-supervisor",
        goal=("escalate", "billing dispute"),
        facts={"account_id": "acct_77"},
        caller=["account acct_77, i have a billing dispute",
                "i want to speak to a human supervisor now"],
        tools=[_tool("verify_identity", {"account_id": "acct_77"}, {"verified": True}, latency_ms=400)],
        handoff={"to": "human_supervisor"},
        state={"tickets": [{"account_id": "acct_77", "escalated": True}]},
        handoff_check="human_supervisor",
        state_check=("tickets", {"account_id": "acct_77"}, {"escalated": True}),
    ))
    # DEFECT: escalation requested but the agent never hands off (policy FAILs).
    jobs.append(dict(
        id="escalate-not-handed-off",
        goal=("escalate", "angry caller"),
        facts={"account_id": "acct_91"},
        caller=["account acct_91, this is unacceptable",
                "get me a manager immediately"],
        tools=[_tool("verify_identity", {"account_id": "acct_91"}, {"verified": True}, latency_ms=410)],
        state={"tickets": [{"account_id": "acct_91", "escalated": False}]},
        handoff_check="human_supervisor",   # no handoff span rendered -> FAIL
        defect="the mock agent never handed off despite an explicit manager request",
    ))

    # ---- FAQ / info-only ------------------------------------------------
    jobs.append(dict(
        id="faq-store-hours",
        goal=("answer_faq", "store hours"),
        facts={},
        caller=["what are your store hours on saturday"],
        tools=[_tool("lookup_faq", {"topic": "hours"}, {"answer": "9 to 5"}, latency_ms=230)],
        outcome_tool=("lookup_faq", {"answer": "9 to 5"}),
        speech_latency=("lookup_faq", 1000),
    ))

    # ---- wrong number / out of scope -----------------------------------
    jobs.append(dict(
        id="wrong-number-polite-exit",
        goal=("handle_wrong_number", "misdial"),
        facts={},
        caller=["oh sorry, i think i dialed the wrong number",
                "i was trying to reach the pharmacy"],
        tools=[],
        termination={"reason": "wrong_number", "by": "agent"},
        termination_check={"reason": "wrong_number"},
        no_error=True,
    ))

    # ---- angry caller / de-escalation ----------------------------------
    jobs.append(dict(
        id="angry-caller-deescalate-refund",
        goal=("get_refund", "order A-5000"),
        facts={"order_id": "A-5000"},
        caller=["this is the third time i've called about order A-5000",
                "i am furious, i just want my refund"],
        tools=[_tool("lookup_order", {"order_id": "A-5000"}, {"found": True}, latency_ms=310),
               _tool("issue_refund", {"order_id": "A-5000"}, {"status": "refunded"}, latency_ms=580)],
        state={"orders": [{"order_id": "A-5000", "refund_status": "refunded"}]},
        outcome_tool=("issue_refund", {"status": "refunded"}),
        state_check=("orders", {"order_id": "A-5000"}, {"refund_status": "refunded"}),
        sequence=[("lookup_order",), ("issue_refund",)],
    ))

    # ---- backchannel-heavy caller --------------------------------------
    jobs.append(dict(
        id="backchannel-heavy-address-change",
        goal=("update_address", "acct_320"),
        facts={"account_id": "acct_320"},
        caller=["hi, account acct_320",
                "i want to update my address to 12 oak street"],
        behavior={"backchannels": {"probability": 0.8}},
        tools=[_tool("verify_identity", {"account_id": "acct_320"}, {"verified": True}, latency_ms=400),
               _tool("update_address", {"account_id": "acct_320", "address": "12 oak street"},
                     {"status": "updated"}, latency_ms=520)],
        state={"accounts": [{"account_id": "acct_320", "address": "12 oak street"}]},
        outcome_tool=("update_address", {"status": "updated"}),
        state_check=("accounts", {"account_id": "acct_320"}, {"address": "12 oak street"}),
        identity_first="update_address",
    ))

    # ---- interruption / barge-in ---------------------------------------
    jobs.append(dict(
        id="interruption-correct-date",
        goal=("book_appointment", "dermatology"),
        facts={"patient_id": "patient_640"},
        caller=["patient_640, i want a dermatology appointment on tuesday",
                "actually make it thursday not tuesday"],
        behavior={"interruptions": [{"trigger": "agent_confirms_date", "offset_ms": 2200}]},
        tools=[_tool("verify_identity", {"patient_id": "patient_640"}, {"verified": True}, latency_ms=400),
               _tool("book_appointment", {"patient_id": "patient_640", "day": "thursday"},
                     {"status": "confirmed", "day": "thursday"}, latency_ms=760)],
        state={"appointments": [{"patient_id": "patient_640", "day": "thursday"}]},
        outcome_tool=("book_appointment", {"day": "thursday"}),
        state_check=("appointments", {"patient_id": "patient_640"}, {"day": "thursday"}),
        identity_first="book_appointment",
    ))

    # ---- payment / billing ---------------------------------------------
    jobs.append(dict(
        id="make-payment",
        goal=("make_payment", "invoice inv_88"),
        facts={"account_id": "acct_450", "invoice_id": "inv_88"},
        caller=["account acct_450, i'd like to pay invoice inv_88",
                "please charge my card on file"],
        tools=[_tool("verify_identity", {"account_id": "acct_450"}, {"verified": True}, latency_ms=430),
               _tool("charge_card", {"account_id": "acct_450", "invoice_id": "inv_88"},
                     {"status": "paid", "amount": 120}, latency_ms=900)],
        state={"invoices": [{"invoice_id": "inv_88", "status": "paid"}]},
        outcome_tool=("charge_card", {"status": "paid"}),
        state_check=("invoices", {"invoice_id": "inv_88"}, {"status": "paid"}),
        identity_first="charge_card",
        speech_latency=("charge_card", 1500),
    ))
    # DEFECT: the payment tool ERRORS but the agent proceeds (tool_error surfaces).
    jobs.append(dict(
        id="payment-declined-handled-wrong",
        goal=("make_payment", "invoice inv_99"),
        facts={"account_id": "acct_451", "invoice_id": "inv_99"},
        caller=["account acct_451, pay invoice inv_99 please"],
        tools=[_tool("verify_identity", {"account_id": "acct_451"}, {"verified": True}, latency_ms=420),
               _tool("charge_card", {"account_id": "acct_451", "invoice_id": "inv_99"},
                     error="card_declined", latency_ms=880)],
        state={"invoices": [{"invoice_id": "inv_99", "status": "unpaid"}]},
        tool_error_absent="charge_card",   # asserts NO error -> FAIL (it errored)
        defect="the mock agent's charge_card errored (card_declined) but the flow asserted no error",
    ))

    # ---- subscription cancel -------------------------------------------
    jobs.append(dict(
        id="subscription-cancel",
        goal=("cancel_subscription", "sub_12"),
        facts={"account_id": "acct_600", "subscription_id": "sub_12"},
        caller=["account acct_600, i want to cancel subscription sub_12",
                "please stop the renewal"],
        tools=[_tool("verify_identity", {"account_id": "acct_600"}, {"verified": True}, latency_ms=410),
               _tool("read_disclosure", {"policy": "cancellation_terms"}, {"read": True}, latency_ms=220),
               _tool("cancel_subscription", {"subscription_id": "sub_12"},
                     {"status": "cancelled"}, latency_ms=640)],
        state={"subscriptions": [{"subscription_id": "sub_12", "status": "cancelled"}]},
        outcome_tool=("cancel_subscription", {"status": "cancelled"}),
        state_check=("subscriptions", {"subscription_id": "sub_12"}, {"status": "cancelled"}),
        identity_first="cancel_subscription",
        disclosure="read_disclosure",
    ))

    # ---- password reset / DTMF -----------------------------------------
    jobs.append(dict(
        id="password-reset",
        goal=("reset_password", "acct_700"),
        facts={"account_id": "acct_700"},
        caller=["account acct_700, i'm locked out",
                "i need to reset my password"],
        tools=[_tool("verify_identity", {"account_id": "acct_700"}, {"verified": True}, latency_ms=450),
               _tool("send_reset_link", {"account_id": "acct_700"},
                     {"status": "sent"}, latency_ms=380)],
        state={"accounts": [{"account_id": "acct_700", "reset_pending": True}]},
        outcome_tool=("send_reset_link", {"status": "sent"}),
        state_check=("accounts", {"account_id": "acct_700"}, {"reset_pending": True}),
        identity_first="send_reset_link",
    ))

    # ---- complaint logging ---------------------------------------------
    jobs.append(dict(
        id="log-complaint",
        goal=("log_complaint", "acct_810"),
        facts={"account_id": "acct_810"},
        caller=["account acct_810, i want to file a complaint",
                "the delivery driver was rude"],
        tools=[_tool("verify_identity", {"account_id": "acct_810"}, {"verified": True}, latency_ms=400),
               _tool("log_complaint", {"account_id": "acct_810", "category": "delivery"},
                     {"status": "logged", "ticket_id": "t_55"}, latency_ms=470)],
        state={"tickets": [{"ticket_id": "t_55", "status": "logged"}]},
        outcome_tool=("log_complaint", {"status": "logged"}),
        state_check=("tickets", {"ticket_id": "t_55"}, {"status": "logged"}),
    ))

    # ---- prescription refill -------------------------------------------
    jobs.append(dict(
        id="prescription-refill",
        goal=("refill_prescription", "rx_44"),
        facts={"patient_id": "patient_900", "prescription_id": "rx_44"},
        caller=["patient_900, i need to refill prescription rx_44",
                "the same pharmacy as last time please"],
        tools=[_tool("verify_identity", {"patient_id": "patient_900"}, {"verified": True}, latency_ms=440),
               _tool("refill_prescription", {"prescription_id": "rx_44"},
                     {"status": "queued"}, latency_ms=560)],
        state={"prescriptions": [{"prescription_id": "rx_44", "status": "queued"}]},
        outcome_tool=("refill_prescription", {"status": "queued"}),
        state_check=("prescriptions", {"prescription_id": "rx_44"}, {"status": "queued"}),
        identity_first="refill_prescription",
    ))

    # ---- reservation ----------------------------------------------------
    jobs.append(dict(
        id="restaurant-reservation",
        goal=("book_reservation", "friday 7pm"),
        facts={"party_size": "4"},
        caller=["i'd like a table for 4",
                "friday at 7pm if you have it"],
        tools=[_tool("check_availability", {"day": "friday", "time": "7pm", "party": 4},
                     {"available": True}, latency_ms=350),
               _tool("book_reservation", {"day": "friday", "time": "7pm", "party": 4},
                     {"status": "confirmed", "reservation_id": "r_77"}, latency_ms=610)],
        state={"reservations": [{"reservation_id": "r_77", "status": "confirmed"}]},
        outcome_tool=("book_reservation", {"status": "confirmed"}),
        state_check=("reservations", {"reservation_id": "r_77"}, {"status": "confirmed"}),
        sequence=[("check_availability",), ("book_reservation",)],
    ))

    # ---- warranty claim -------------------------------------------------
    jobs.append(dict(
        id="warranty-claim",
        goal=("file_warranty_claim", "device dev_31"),
        facts={"device_id": "dev_31"},
        caller=["my device dev_31 stopped working",
                "i want to file a warranty claim"],
        tools=[_tool("lookup_device", {"device_id": "dev_31"},
                     {"under_warranty": True}, latency_ms=330),
               _tool("file_claim", {"device_id": "dev_31"},
                     {"status": "filed", "claim_id": "c_12"}, latency_ms=700)],
        state={"claims": [{"claim_id": "c_12", "status": "filed"}]},
        outcome_tool=("file_claim", {"status": "filed"}),
        state_check=("claims", {"claim_id": "c_12"}, {"status": "filed"}),
        sequence=[("lookup_device",), ("file_claim",)],
    ))

    # ---- callback scheduling -------------------------------------------
    jobs.append(dict(
        id="schedule-callback",
        goal=("schedule_callback", "tomorrow morning"),
        facts={"account_id": "acct_940"},
        caller=["account acct_940, i can't talk now",
                "can someone call me back tomorrow morning"],
        tools=[_tool("verify_identity", {"account_id": "acct_940"}, {"verified": True}, latency_ms=410),
               _tool("schedule_callback", {"account_id": "acct_940", "when": "tomorrow_am"},
                     {"status": "scheduled"}, latency_ms=430)],
        state={"callbacks": [{"account_id": "acct_940", "status": "scheduled"}]},
        outcome_tool=("schedule_callback", {"status": "scheduled"}),
        state_check=("callbacks", {"account_id": "acct_940"}, {"status": "scheduled"}),
        identity_first="schedule_callback",
    ))

    # ---- shipping address correction -----------------------------------
    jobs.append(dict(
        id="shipping-address-correction",
        goal=("correct_shipping", "order A-7010"),
        facts={"order_id": "A-7010"},
        caller=["order A-7010 has the wrong shipping address",
                "please change it to 9 pine road before it ships"],
        tools=[_tool("lookup_order", {"order_id": "A-7010"}, {"status": "processing"}, latency_ms=300),
               _tool("update_shipping", {"order_id": "A-7010", "address": "9 pine road"},
                     {"status": "updated"}, latency_ms=520)],
        state={"orders": [{"order_id": "A-7010", "ship_to": "9 pine road"}]},
        outcome_tool=("update_shipping", {"status": "updated"}),
        state_check=("orders", {"order_id": "A-7010"}, {"ship_to": "9 pine road"}),
        sequence=[("lookup_order",), ("update_shipping",)],
    ))

    return jobs


# =========================================================================
# emit scenario.v1 + conversation-test.v1 per job
# =========================================================================

def _behavior_block(job):
    b = dict(job.get("behavior") or {})
    b.setdefault("backchannels", {"probability": 0.0})
    return b


def _scenario(job):
    scn = {
        "kind": "hotato.scenario", "version": 1, "id": job["id"],
        "goal": {"type": job["goal"][0], "target": job["goal"][1]},
        "facts": job["facts"],
        "caller": {
            "script": [{"say": line} for line in job["caller"]],
            "behavior": _behavior_block(job),
        },
        "environment": {"locale": "en-US", "route": "phone"},
        "variation_matrix": {
            "speaking_rate": BEHAVIOURS,      # 5 caller behaviours (pace)
            "noise": ENVIRONMENTS,            # 3 audio environments
            "repetitions": 1,
        },
        "seed": 0,
    }
    agent_mock = {"tools": job.get("tools") or []}
    if job.get("handoff"):
        agent_mock["handoff"] = job["handoff"]
    if job.get("termination"):
        agent_mock["termination"] = job["termination"]
    if job.get("state"):
        agent_mock["state"] = job["state"]
    scn["agent_mock"] = agent_mock
    return scn


def _test(job):
    det = []
    # conversation: the caller actually stated their need (a caller-side phrase;
    # the first word of the opening line, escaped so any punctuation is literal).
    first_word = job["caller"][0].split()[0]
    det.append({"id": "caller-stated-need", "kind": "phrase",
                "regex": re.escape(first_word), "role": "caller",
                "dimension": "conversation"})
    det.append({"id": "caller-turns", "kind": "count", "phrase": r"\S",
                "role": "caller", "count": {"min": 1}, "dimension": "conversation"})
    # outcome: the agent completed the task (Authority 1 tool_result).
    if job.get("outcome_tool"):
        name, subset = job["outcome_tool"]
        det.append({"id": "outcome-tool", "kind": "tool_result", "name": name,
                    "result_subset": subset, "dimension": "outcome"})
    # outcome: the post-call state confirms it (Authority 2 state).
    if job.get("state_check"):
        res, filt, exp = job["state_check"]
        det.append({"id": "outcome-state", "kind": "state", "resource": res,
                    "filters": filt, "expect": exp, "dimension": "outcome"})
    # policy: identity verified BEFORE the sensitive action (sequence).
    if job.get("identity_first"):
        det.append({"id": "identity-before-action", "kind": "sequence",
                    "steps": [{"tool": "verify_identity"},
                              {"tool": job["identity_first"]}],
                    "dimension": "policy"})
    # policy: required disclosure was read (a tool_call the agent performed).
    if job.get("disclosure"):
        det.append({"id": "required-disclosure", "kind": "tool_call",
                    "name": job["disclosure"], "dimension": "policy"})
    # policy: the required escalation handoff occurred.
    if job.get("handoff_check"):
        det.append({"id": "escalation-handoff", "kind": "handoff",
                    "to": job["handoff_check"], "dimension": "policy"})
    # policy: the call terminated for the stated reason.
    if job.get("termination_check"):
        det.append({"id": "call-terminated", "kind": "termination",
                    **job["termination_check"], "dimension": "policy"})
    # policy: no tool errored when the flow requires none.
    if job.get("tool_error_absent"):
        det.append({"id": "no-tool-error", "kind": "tool_error",
                    "name": job["tool_error_absent"], "absent": True,
                    "dimension": "policy"})
    if job.get("no_error"):
        for t in job.get("tools") or []:
            det.append({"id": f"no-error-{t['name']}", "kind": "tool_error",
                        "name": t["name"], "absent": True, "dimension": "policy"})
    # conversation: an ordered multi-step flow.
    if job.get("sequence"):
        det.append({"id": "ordered-flow", "kind": "sequence",
                    "steps": [{"tool": s[0]} for s in job["sequence"]],
                    "dimension": "conversation"})
    # speech: the agent's tool responded within a latency budget.
    if job.get("speech_latency"):
        tool, max_ms = job["speech_latency"]
        det.append({"id": "tool-latency", "kind": "latency", "tool": tool,
                    "max_ms": max_ms, "dimension": "speech"})
    return {
        "kind": "hotato.conversation-test", "version": 1,
        "id": f"{job['id']}-test", "agent": "reference-agent-v1",
        "scenario": f"../scenarios/{job['id']}.scenario.json",
        "assertions": {"deterministic": det},
        "repetitions": 1,
        "success": {
            "required": ["all_deterministic_assertions_pass"],
            "report_dimensions": ["outcome", "policy", "conversation", "speech"],
        },
    }


def _write(path, obj):
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(obj, fh, indent=2, sort_keys=True)
        fh.write("\n")


def main():
    os.makedirs(SCN_DIR, exist_ok=True)
    os.makedirs(TEST_DIR, exist_ok=True)
    # Clear any stale generated files so a removed job never leaves an orphan.
    for d in (SCN_DIR, TEST_DIR):
        for f in os.listdir(d):
            if f.endswith(".json"):
                os.remove(os.path.join(d, f))
    jobs = _jobs()
    ids = [j["id"] for j in jobs]
    assert len(ids) == len(set(ids)), "duplicate job id"
    assert len(jobs) >= 25, f"need >=25 jobs, have {len(jobs)}"
    test_refs = []
    for job in jobs:
        _write(os.path.join(SCN_DIR, f"{job['id']}.scenario.json"), _scenario(job))
        _write(os.path.join(TEST_DIR, f"{job['id']}.test.json"), _test(job))
        test_refs.append(f"tests/{job['id']}.test.json")
    suite = {
        "kind": "hotato.suite", "version": 1,
        "suite_id": "reference-agent-suite",
        "name": "Reference agent -- full conversation-QA suite",
        "purpose": ("25 realistic voice-agent jobs x 5 caller behaviours x 3 audio "
                    "environments = 375 offline simulated runs, scored across the "
                    "outcome / policy / conversation / speech dimensions."),
        "required_for_release": True,
        "inconclusive_policy": "fail",
        "tests": sorted(test_refs),
    }
    _write(os.path.join(HERE, "suite.json"), suite)
    runs = len(jobs) * len(BEHAVIOURS) * len(ENVIRONMENTS)
    print(f"wrote {len(jobs)} scenarios + {len(jobs)} tests + suite.json "
          f"({runs} total runs = {len(jobs)} x {len(BEHAVIOURS)} behaviours x "
          f"{len(ENVIRONMENTS)} environments)")


if __name__ == "__main__":
    main()
