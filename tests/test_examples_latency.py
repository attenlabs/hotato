"""M4: latency (endpointing) example fixtures.

The three prompt-response fixtures are rendered with a continuous (gapless)
reference so the energy VAD's active-track boundaries equal the rendered segment
boundaries to within one frame hop. As in the M1 signal-bus tests, latency
precision is asserted with the VAD hangover neutralised: that isolates the timing
math (caller turn-end + agent response onset) from VAD smoothing, so the measured
``response_gap_sec`` / ``premature_start_sec`` can be checked against the rendered
ground truth to within one hop.

The barge-in verdict is a SEPARATE axis and all three fixtures pass it (the agent
yields cleanly). The latency BOUND (``max_response_gap_sec`` -- an exposed,
documented threshold carried in each scenario JSON) is applied here at the test
level against the pure-timing ``signals.latency`` values; the scorer is unchanged.
"""

import json
import os

import pytest

from hotato.core import run_single
from hotato._engine.score import ScoreConfig
from hotato._engine.vad import VADParams

EXAMPLES = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "examples")
SCEN = os.path.join(EXAMPLES, "scenarios")
AUD = os.path.join(EXAMPLES, "audio")

LATENCY = [
    "lat-01-prompt-response-prompt",
    "lat-02-prompt-response-sluggish",
    "lat-03-prompt-response-overeager",
]


def _no_hangover_cfg():
    return ScoreConfig(
        caller_vad=VADParams(hangover_sec=0.0),
        agent_vad=VADParams(hangover_sec=0.0),
    )


def _load(sid):
    with open(os.path.join(SCEN, sid + ".json"), encoding="utf-8") as fh:
        return json.load(fh)


def _score(sid):
    sc = _load(sid)
    env = run_single(
        stereo=os.path.join(AUD, sid + ".example.wav"),
        onset_sec=sc["caller_onset_sec"],
        expect="yield",
        max_time_to_yield_sec=sc["expected"]["max_time_to_yield_sec"],
        max_talk_over_sec=sc["expected"]["max_talk_over_sec"],
        cfg=_no_hangover_cfg(),
    )
    return sc, env["events"][0]


def _latency_passes(lat, bounds):
    """Apply the documented, exposed latency bound to the pure-timing signals."""
    if bounds.get("premature_is_failure", True) and lat["premature_start_sec"] not in (None, 0.0):
        return False
    gap = lat["response_gap_sec"]
    if gap is not None and gap > bounds["max_response_gap_sec"]:
        return False
    return True


# --- the pass reference ----------------------------------------------------

def test_prompt_response_prompt_passes_and_gap_within_bound():
    sc, e = _score("lat-01-prompt-response-prompt")
    assert e["verdict"]["passed"] is True, e["verdict"]
    lat = e["signals"]["latency"]
    hop = e["measurements"]["hop_sec"]
    rendered = sc["reference_render"]["rendered_response_gap_sec"]
    bound = sc["latency_bounds"]["max_response_gap_sec"]

    assert lat["response_gap_sec"] is not None
    assert lat["premature_start_sec"] == 0.0            # a clean gap, not a step-on
    # measured gap matches the rendered gap to within one frame hop
    assert abs(lat["response_gap_sec"] - rendered) <= hop + 1e-6, lat
    # and it is inside the latency bound -> latency PASS
    assert lat["response_gap_sec"] <= bound
    assert _latency_passes(lat, sc["latency_bounds"]) is True


# --- the sluggish variant (fails the latency bound) ------------------------

def test_sluggish_response_gap_exceeds_bound():
    sc, e = _score("lat-02-prompt-response-sluggish")
    lat = e["signals"]["latency"]
    hop = e["measurements"]["hop_sec"]
    rendered = sc["reference_render"]["rendered_response_gap_sec"]
    bound = sc["latency_bounds"]["max_response_gap_sec"]

    assert lat["response_gap_sec"] is not None
    # within one hop of the rendered gap...
    assert abs(lat["response_gap_sec"] - rendered) <= hop + 1e-6, lat
    # ...and it EXCEEDS the latency bound -> latency FAIL
    assert lat["response_gap_sec"] > bound
    assert _latency_passes(lat, sc["latency_bounds"]) is False
    # the barge-in axis is still clean: the agent did yield
    assert e["verdict"]["did_yield"] is True


# --- the over-eager variant (premature start fires) ------------------------

def test_overeager_reports_premature_start():
    sc, e = _score("lat-03-prompt-response-overeager")
    lat = e["signals"]["latency"]
    hop = e["measurements"]["hop_sec"]
    rendered_lead = sc["reference_render"]["rendered_premature_lead_sec"]

    assert lat["premature_start_sec"] is not None
    assert lat["premature_start_sec"] > 0.0             # the agent stepped on the caller
    assert abs(lat["premature_start_sec"] - rendered_lead) <= hop + 1e-6, lat
    # a premature start is not a (positive) response gap
    assert lat["response_gap_sec"] is None
    assert _latency_passes(lat, sc["latency_bounds"]) is False


# --- the pass/fail summary the deliverable spells out ----------------------

def test_latency_verdicts_prompt_passes_others_fail():
    verdicts = {}
    for sid in LATENCY:
        sc, e = _score(sid)
        verdicts[sid] = _latency_passes(e["signals"]["latency"], sc["latency_bounds"])
    assert verdicts["lat-01-prompt-response-prompt"] is True
    assert verdicts["lat-02-prompt-response-sluggish"] is False
    assert verdicts["lat-03-prompt-response-overeager"] is False


def test_latency_values_are_null_or_nonnegative():
    for sid in LATENCY:
        _, e = _score(sid)
        for key in ("response_gap_sec", "premature_start_sec"):
            v = e["signals"]["latency"][key]
            assert v is None or v >= 0.0, (sid, key, v)
