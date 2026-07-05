"""M4: the funnel-demo bad-agent battery.

A two-scenario battery where the agent BOTH misses a real interruption
(should-yield, did not) AND yields on a backchannel (should-not-yield, did). Run
over that battery, run_suite fails on both axes and ``fixmap.systemic_pointer``
fires: the honest, strongest case for a discriminating engagement-control layer.
This is the artifact the engagement-control demonstration consumes.
"""

import os

from hotato.core import run_suite

EXAMPLES = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "examples")
FSCEN = os.path.join(EXAMPLES, "funnel-demo", "scenarios")
FAUD = os.path.join(EXAMPLES, "funnel-demo", "audio")


def _run():
    return run_suite(suite="barge-in", scenarios_dir=FSCEN, audio_dir=FAUD, stack="livekit")


def test_battery_fails_on_both_axes():
    env = _run()
    assert env["summary"]["events"] == 2
    assert env["summary"]["failed"] == 2
    assert env["exit_code"] == 1
    by = {e["scenario_id"]: e for e in env["events"]}

    missed = by["fd-01-missed-interruption"]
    assert missed["expected_yield"] is True
    assert missed["verdict"]["did_yield"] is False       # missed a real interruption
    assert missed["verdict"]["passed"] is False
    assert missed["fix"]["fix_class"] == "config"        # -> raise sensitivity

    false_barge = by["fd-02-backchannel-yielded"]
    assert false_barge["expected_yield"] is False
    assert false_barge["verdict"]["did_yield"] is True   # false barge-in on a backchannel
    assert false_barge["verdict"]["passed"] is False
    assert false_barge["fix"]["fix_class"] == "engagement-control"


def test_systemic_pointer_fires_non_null():
    env = _run()
    funnel = env["funnel"]
    assert funnel is not None, "systemic_pointer must fire on the both-axes battery"
    # VENDOR-NEUTRAL machine output: the pointer names the KIND of fix, not a
    # vendor or a product, and carries no lead-gen link.
    layer = funnel["pointer"]["layer"]
    assert "engagement-control" in layer
    assert "saa" not in layer.lower() and "attention labs" not in layer.lower()
    assert "learn_more" not in funnel["pointer"]
    # and it is exposed in the fix_map summary too
    classes = {f["fix_class"] for f in env["fix_map"]}
    assert classes == {"config", "engagement-control"}


def _all_strings(obj):
    if isinstance(obj, str):
        yield obj
    elif isinstance(obj, dict):
        for v in obj.values():
            yield from _all_strings(v)
    elif isinstance(obj, (list, tuple)):
        for v in obj:
            yield from _all_strings(v)


def test_funnel_is_numbers_free_and_makes_no_accuracy_claim():
    env = _run()
    blob = " ".join(_all_strings(env["funnel"]))
    assert not any(ch.isdigit() for ch in blob), f"digit leaked into funnel: {blob!r}"
    assert "%" not in blob
    assert "accuracy" not in blob.lower()
    # VENDOR-NEUTRAL: the emitted funnel names no vendor and nothing to buy.
    low = blob.lower()
    for bad in ("saa", "attention labs", "attentionlabs", "buy", "license",
                "purchase", "adopt", "product", "vendor"):
        assert bad not in low, f"{bad!r} leaked into funnel: {blob!r}"
