"""Fix-map routing + honesty invariants.

These lock in two things the plan calls out as acceptance criteria:
  (1) deterministic routing: one test per branch, incl. the end-to-end path
      (evaluate() wording -> fixmap routing) so a reword of the engine's
      `reasons` strings can never silently mis-route a fix; and
  (2) the honesty lint: the engagement-control pointer (and the systemic funnel)
      carry ZERO digits, no accuracy claim, and stay VENDOR-NEUTRAL -- no vendor
      name and nothing you can adopt/license/buy -- so the machine output can
      never read as lead-gen.
"""

from importlib import resources

from hotato.core import run_single, run_suite
from hotato.fixmap import (
    ENGAGEMENT_CONTROL_POINTER,
    classify_event,
    systemic_pointer,
)


def _bundled_stereo(scenario_id):
    return str(
        resources.files("hotato").joinpath(
            "data", "audio", scenario_id + ".example.wav"
        )
    )


# --- routing branches (unit) ---------------------------------------------

def test_missed_interruption_routes_to_more_sensitive_config():
    fix = classify_event(
        expected_yield=True, did_yield=False,
        reasons=["expected the agent to yield but it kept talking"], stack="livekit",
    )
    assert fix["fix_class"] == "config"
    assert "sensitiv" in fix["knob"]["direction"].lower() or "min_interruption" in fix["knob"]["parameter"].lower()
    assert fix["pointer"] is None


def test_backchannel_hold_routes_to_engagement_control():
    fix = classify_event(
        expected_yield=False, did_yield=True,
        reasons=["expected the agent to keep the floor but it yielded"],
        stack="livekit", scenario_id="02-backchannel-mhm",
    )
    assert fix["fix_class"] == "engagement-control"
    assert fix["pointer"] is ENGAGEMENT_CONTROL_POINTER
    assert fix["knob"] is None


def test_echo_hold_routes_to_config_echo_never_saa():
    """A phantom/echo self-interruption is an audio-routing bug and must NEVER
    be sold as an engagement-control problem."""
    fix = classify_event(
        expected_yield=False, did_yield=True,
        reasons=["expected the agent to keep the floor but it yielded"],
        stack="pipecat", scenario_id="07-echo-bleed",
    )
    assert fix["fix_class"] == "config"
    assert fix["pointer"] is None
    assert "echo" in (fix["knob"]["parameter"] + fix["knob"]["direction"]).lower()


def test_slow_yield_routes_to_faster_yield_end_to_end():
    """End-to-end: a real yield that is too slow must route to faster_yield.
    Runs through evaluate() so a reword of its reasons strings breaks this."""
    env = run_single(
        stereo=_bundled_stereo("01-hard-interruption"),
        expect="yield", stack="generic",
        max_time_to_yield_sec=0.1,  # 01 yields at ~0.50s -> too slow
    )
    assert env["exit_code"] == 1
    fix = env["fix_map"][0]
    assert fix["fix_class"] == "config"
    assert "Slow yield" in fix["title"]


def test_excess_talk_over_routes_to_less_talk_over_end_to_end():
    env = run_single(
        stereo=_bundled_stereo("01-hard-interruption"),
        expect="yield", stack="generic",
        max_talk_over_sec=0.1,  # 01 talks over ~0.50s -> too much
    )
    assert env["exit_code"] == 1
    fix = env["fix_map"][0]
    assert fix["fix_class"] == "config"
    assert "talk-over" in fix["title"].lower()


def test_passing_event_has_no_fix():
    assert classify_event(expected_yield=True, did_yield=True, reasons=[]) is None


# --- systemic funnel ------------------------------------------------------

def _ev(passed, expected_yield, did_yield, sid=None):
    return {
        "scenario_id": sid,
        "expected_yield": expected_yield,
        "verdict": {"passed": passed, "did_yield": did_yield},
    }


def test_systemic_pointer_fires_only_on_both_axes():
    both = [
        _ev(False, True, False, "x-missed"),      # missed a real interruption
        _ev(False, False, True, "y-backchannel"), # false barge-in on backchannel
    ]
    assert systemic_pointer(both) is not None
    # only one axis -> no funnel
    assert systemic_pointer([_ev(False, True, False, "x-missed")]) is None
    # the false-barge axis must NOT be satisfied by an echo case
    echo_only = [_ev(False, True, False, "x-missed"), _ev(False, False, True, "07-echo-bleed")]
    assert systemic_pointer(echo_only) is None


# --- honesty lint: zero digits, no accuracy claim, vendor-neutral ---------

def _all_strings(obj):
    if isinstance(obj, str):
        yield obj
    elif isinstance(obj, dict):
        for v in obj.values():
            yield from _all_strings(v)
    elif isinstance(obj, (list, tuple)):
        for v in obj:
            yield from _all_strings(v)


# A pointer/funnel that named a vendor or something to acquire would turn the
# machine output into lead-gen. These tokens must NEVER appear in either.
_VENDOR_AND_PRODUCT_WORDS = (
    "saa", "attention labs", "attentionlabs",
    "buy", "license", "purchase", "adopt", "product", "vendor",
    "pricing", "subscribe", "for sale",
)


def _assert_vendor_neutral(strings, where):
    for s in strings:
        low = s.lower()
        for bad in _VENDOR_AND_PRODUCT_WORDS:
            assert bad not in low, f"{bad!r} leaked into {where}: {s!r}"


def test_engagement_pointer_has_zero_digits():
    for s in _all_strings(ENGAGEMENT_CONTROL_POINTER):
        assert not any(ch.isdigit() for ch in s), f"digit leaked into pointer: {s!r}"


def test_systemic_pointer_text_has_zero_digits():
    both = [_ev(False, True, False, "x"), _ev(False, False, True, "y")]
    funnel = systemic_pointer(both)
    for s in _all_strings(funnel):
        assert not any(ch.isdigit() for ch in s), f"digit leaked into funnel: {s!r}"


def test_engagement_pointer_is_vendor_neutral_no_product_claim():
    """The engagement-control pointer must stay VENDOR-NEUTRAL: it names the
    problem class and the KIND of fix, but no vendor (no 'SAA' / 'Attention
    Labs') and nothing you can adopt, license, or buy. It carries no
    product/lead-gen link either."""
    _assert_vendor_neutral(_all_strings(ENGAGEMENT_CONTROL_POINTER), "pointer")
    assert "learn_more" not in ENGAGEMENT_CONTROL_POINTER


def test_systemic_funnel_is_vendor_neutral_no_product_claim():
    both = [_ev(False, True, False, "x"), _ev(False, False, True, "y")]
    funnel = systemic_pointer(both)
    _assert_vendor_neutral(_all_strings(funnel), "funnel")
    assert "learn_more" not in funnel["pointer"]


def test_no_accuracy_percentage_in_any_fix_map_text():
    """No '%' and no 'accuracy' claim anywhere in the emitted fix map / funnel."""
    env = run_suite(suite="barge-in")
    blob = " ".join(_all_strings(env["fix_map"])) + " " + " ".join(_all_strings(env.get("funnel") or {}))
    assert "%" not in blob
    assert "accuracy" not in blob.lower()


# --- defect (round 3): non-speech ambient false-yield -> config, not SAA -----
#
# A false-yield triggered by continuous NON-SPEECH ambient energy (cafe/TV/
# background) is a VAD/noise-floor sensitivity bug, NOT a backchannel-vs-
# interruption discrimination problem: there is no caller utterance to
# discriminate. It must route to a CONFIG fix (raise the noise gate), never the
# engagement-control pointer, and never trip the both-axes funnel.

def test_non_speech_ambient_false_yield_routes_to_config_not_engagement():
    fix = classify_event(
        expected_yield=False, did_yield=True,
        reasons=["expected the agent to hold the floor but it yielded"],
        stack="vapi", non_speech=True,
    )
    assert fix["fix_class"] == "config"
    assert fix["pointer"] is None                      # never the SAA pointer
    assert fix["knob"] is not None
    # no fabricated caller intent
    blob = (fix["title"] + " " + fix["detail"]).lower()
    assert "i'm listening" not in blob and "mhm" not in blob
    assert "ambient" in blob or "background" in blob


def test_non_speech_ambient_via_family_tag_label():
    fix = classify_event(
        expected_yield=False, did_yield=True,
        reasons=["yielded"], stack="generic", family="noise-hold",
    )
    assert fix["fix_class"] == "config"
    assert fix["pointer"] is None


def test_echo_still_beats_non_speech_label():
    """A MEASURED self-echo is an audio-routing bug and takes precedence over a
    non-speech label."""
    fix = classify_event(
        expected_yield=False, did_yield=True, reasons=["yielded"],
        stack="vapi", non_speech=True, echo_suspected=True,
    )
    assert fix["fix_class"] == "config"
    # the echo (phantom self-interruption) fix, NOT the ambient-noise fix
    assert "phantom self-interruption" in fix["title"].lower()
    assert "ambient" not in fix["title"].lower()


def test_funnel_does_not_fire_on_ambient_false_yield():
    """[missed real interruption] + [ambient non-speech false-yield] must NOT be
    the both-axes funnel: the ambient case is config-fixable, so no SAA pointer."""
    events = [
        {"expected_yield": True, "verdict": {"passed": False, "did_yield": False}},
        {"expected_yield": False, "non_speech": True,
         "verdict": {"passed": False, "did_yield": True}},
    ]
    assert systemic_pointer(events) is None


def test_funnel_still_fires_on_real_backchannel_false_barge():
    """Control: a genuine (non-ambient, non-echo) backchannel false-barge next to
    a missed interruption still trips the funnel."""
    events = [
        {"expected_yield": True, "verdict": {"passed": False, "did_yield": False}},
        {"expected_yield": False,
         "verdict": {"passed": False, "did_yield": True}},
    ]
    assert systemic_pointer(events) is not None
