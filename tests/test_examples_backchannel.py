"""M4: backchannel-discrimination example fixtures.

Three reference fixtures where the caller only gives listener feedback and the
correct agent HOLDS the floor (did_yield stays false, the event passes): repeated
backchannels, a single mid-utterance backchannel, and a deliberate near-miss (a
long backchannel that briefly resembles a floor-take). The near-miss's bad twin
lives in funnel-demo (fd-02): an agent that yields to almost exactly that kind of
backchannel, which must FAIL. Holding through all three AND catching the real
interruption in fd-01 is the case one sensitivity dial cannot serve.
"""

import json
import os

import pytest

from hotato.core import run_single, run_suite

EXAMPLES = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "examples")
SCEN = os.path.join(EXAMPLES, "scenarios")
AUD = os.path.join(EXAMPLES, "audio")
FSCEN = os.path.join(EXAMPLES, "funnel-demo", "scenarios")
FAUD = os.path.join(EXAMPLES, "funnel-demo", "audio")

BACKCHANNEL = [
    "bc-01-repeated-backchannels",
    "bc-02-midutterance-backchannel",
    "bc-03-near-miss-floor-take",
]


def _onset(sid):
    with open(os.path.join(SCEN, sid + ".json"), encoding="utf-8") as fh:
        return json.load(fh)["caller_onset_sec"]


@pytest.mark.parametrize("sid", BACKCHANNEL)
def test_backchannel_reference_holds_and_passes(sid):
    env = run_single(
        stereo=os.path.join(AUD, sid + ".example.wav"),
        onset_sec=_onset(sid),
        expect="hold",
    )
    e = env["events"][0]
    assert e["verdict"]["did_yield"] is False, sid    # the agent HELD the floor
    assert e["verdict"]["passed"] is True, sid
    assert e["fix"] is None, sid                        # a pass carries no fix


def test_backchannel_references_all_pass_in_suite():
    env = run_suite(suite="barge-in", scenarios_dir=SCEN, audio_dir=AUD)
    by = {e["scenario_id"]: e for e in env["events"]}
    for sid in BACKCHANNEL:
        assert by[sid]["expected_yield"] is False, sid
        assert by[sid]["verdict"]["passed"] is True, sid


def test_bad_agent_variant_yields_and_fails():
    """The paired bad-agent variant (fd-02) yields to a bare backchannel and must
    FAIL, routing to engagement-control -- the discrimination fix, not a dial."""
    env = run_suite(suite="barge-in", scenarios_dir=FSCEN, audio_dir=FAUD, stack="livekit")
    e = {ev["scenario_id"]: ev for ev in env["events"]}["fd-02-backchannel-yielded"]
    assert e["expected_yield"] is False
    assert e["verdict"]["did_yield"] is True            # YIELDED (wrong)
    assert e["verdict"]["passed"] is False              # -> FAIL
    assert e["fix"]["fix_class"] == "engagement-control"
