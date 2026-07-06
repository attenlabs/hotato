#!/usr/bin/env python3
"""Build the tiered synthetic scenario suites under corpus/suites/.

Every scenario here is SYNTHETIC and says so: deterministic shaped noise
rendered from the exact segment timings in its own JSON (seed = sha256(id),
byte-identical on every machine). The segment timings ARE the ground truth.
No recorded speech, no accuracy claim, no simulated venue. Where a scenario
raises the noise floor or scales a channel, the JSON states the exact physical
parameter used, nothing more.

Tiers:
  silver           clean conditions, 16 kHz, default noise floor. Every
                   reference render PASSES the barge-in verdict.
  silver-defects   clean conditions, deliberately bad agent renders. Every
                   scenario FAILS on its labeled axis (barge_in or latency).
  gold             hard conditions: raised noise floors, 8 kHz telephony,
                   channel gain extremes, echo bleed, edge timings, heavy
                   overlap, one minute endurance. Reference renders PASS.
  gold-defects     hard-condition defect renders, including two labeled
                   capture-defect cases where the measurement itself is the
                   failure. Every scenario FAILS on its labeled axis.

Usage:
  python3 corpus/suites/build_suites.py           # write JSONs + render audio
  python3 corpus/suites/build_suites.py --check   # regenerate to a temp dir and
                                                  # byte-compare against disk

The render algorithm is loaded from examples/render_examples.py, the project
mirror of the canonical upstream generator, so one render code path produces
every fixture in this repository.
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import math
import os
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))          # corpus/suites
REPO = os.path.dirname(os.path.dirname(HERE))               # repo root
RENDERER_PATH = os.path.join(REPO, "examples", "render_examples.py")

SUITE_NAMES = ["silver", "silver-defects", "gold", "gold-defects"]


def load_renderer():
    """Load build_scenario/write_wav from the project render mirror."""
    spec = importlib.util.spec_from_file_location("hotato_render_examples", RENDERER_PATH)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod


def _noise_db(amp: float) -> float:
    """Approximate RMS dBFS of a uniform noise floor with peak amplitude amp."""
    return round(20.0 * math.log10(amp / math.sqrt(3.0)), 1)


# --------------------------------------------------------------------------
# scenario constructors
# --------------------------------------------------------------------------

def _seg(pairs):
    return [[round(s, 2), round(e, 2)] for (s, e) in pairs]


def _sc(sid, title, category, tags, family, sr, dur, onset, expected, rr, why,
        signals, verdict, axis=None, latency_bounds=None):
    d = {
        "id": sid,
        "title": title,
        "category": category,
        "tags": tags,
        "family": family,
        "source_type": "synthetic",
        "sample_rate": sr,
        "duration_sec": round(dur, 2),
        "caller_onset_sec": round(onset, 2),
        "expected": expected,
    }
    if latency_bounds is not None:
        d["latency_bounds"] = latency_bounds
    d["reference_render"] = rr
    d["reference_verdict"] = verdict
    if axis is not None:
        d["failure_axis"] = axis
    d["why_it_matters"] = why
    d["related_signals"] = signals
    return d


def yield_case(sid, title, tags, family, why, *, onset, yield_after, caller_segs,
               agent_extra=(), sr=16000, dur, bounds=None, verdict="pass",
               axis=None, agent_start=0.2, agent_end=None, noise=None,
               caller_noise=None, agent_noise=None, caller_gain=None,
               agent_gain=None,
               signals=("did_yield", "time_to_yield", "talk_over")):
    """A should_yield scenario. By default the render is a correct agent that
    stops ``yield_after`` seconds after the labeled onset; pass ``agent_end``
    to render a defect (an agent that keeps talking, or stops late)."""
    a_end = round(agent_end if agent_end is not None else onset + yield_after, 2)
    agent = _seg([(agent_start, a_end)] + list(agent_extra))
    if bounds is None:
        bounds = (round(yield_after + 0.5, 2), round(yield_after + 0.65, 2))
    expected = {
        "yield": True,
        "max_time_to_yield_sec": bounds[0],
        "max_talk_over_sec": bounds[1],
    }
    rr = {"agent_segments_sec": agent, "caller_segments_sec": _seg(caller_segs)}
    _apply_knobs(rr, noise, caller_noise, agent_noise, caller_gain, agent_gain)
    return _sc(sid, title, "should_yield", tags, family, sr, dur, onset,
               expected, rr, why, list(signals), verdict, axis=axis)


def hold_case(sid, title, tags, family, why, *, onset, caller_segs, agent_end,
              sr=16000, dur, verdict="pass", axis=None, noise=None,
              caller_noise=None, agent_noise=None, echo=None):
    """A should_not_yield scenario. The reference agent holds the floor through
    the search window; a defect render stops right after the backchannel."""
    expected = {
        "yield": False,
        "max_time_to_yield_sec": None,
        "max_talk_over_sec": None,
    }
    rr = {
        "agent_segments_sec": _seg([(0.2, agent_end)]),
        "caller_segments_sec": _seg(caller_segs),
    }
    if echo is not None:
        delay, gain = echo
        rr["caller_segments_sec"] = []
        rr["caller_is_echo_of_agent"] = True
        rr["echo_delay_sec"] = delay
        rr["echo_gain"] = gain
    _apply_knobs(rr, noise, caller_noise, agent_noise, None, None)
    return _sc(sid, title, "should_not_yield", tags, family, sr, dur, onset,
               expected, rr, why, ["did_yield"], verdict, axis=axis)


def latency_case(sid, title, tags, family, why, *, gap=None, lead=None,
                 sr=16000, noise=None, tolerance_hops=1, verdict="pass",
                 axis=None):
    """A prompt-response endpointing scenario (continuous render, exact
    boundaries). ``gap`` renders a response that starts ``gap`` seconds after
    the caller stops; ``lead`` renders one that starts ``lead`` seconds BEFORE
    the caller stops (a premature start). Exactly one must be given."""
    assert (gap is None) != (lead is None)
    onset = 1.6
    if gap is not None:
        caller_end = 3.0
        resp_on = round(caller_end + gap, 2)
    else:
        caller_end = 3.2
        resp_on = round(caller_end - lead, 2)
    resp_end = round(resp_on + 1.5, 2)
    dur = round(max(resp_end, caller_end) + 0.8, 2)
    rr = {
        "continuous": True,
        "agent_segments_sec": _seg([(0.2, 1.7), (resp_on, resp_end)]),
        "caller_segments_sec": _seg([(onset, caller_end)]),
        "caller_offset_sec": caller_end,
        "agent_response_onset_sec": resp_on,
    }
    if gap is not None:
        rr["rendered_response_gap_sec"] = round(gap, 2)
        signals = ["response_gap_sec", "did_yield"]
    else:
        rr["rendered_premature_lead_sec"] = round(lead, 2)
        signals = ["premature_start_sec", "did_yield"]
    _apply_knobs(rr, noise, None, None, None, None)
    expected = {
        "yield": True,
        "max_time_to_yield_sec": 0.70,
        "max_talk_over_sec": 0.80,
    }
    latency_bounds = {
        "max_response_gap_sec": 1.00,
        "premature_is_failure": True,
        "boundary_tolerance_hops": tolerance_hops,
        "note": "Exposed timing thresholds applied to signals.latency by the "
                "suite tests; the barge-in verdict is a separate axis and the "
                "reference agent yields cleanly in every latency fixture.",
    }
    return _sc(sid, title, "latency", tags, family, sr, dur, onset, expected,
               rr, why, signals, verdict, axis=axis,
               latency_bounds=latency_bounds)


def _apply_knobs(rr, noise, caller_noise, agent_noise, caller_gain, agent_gain):
    if noise is not None:
        rr["noise_floor_amp"] = noise
    if caller_noise is not None:
        rr["caller_noise_floor_amp"] = caller_noise
    if agent_noise is not None:
        rr["agent_noise_floor_amp"] = agent_noise
    if caller_gain is not None:
        rr["caller_gain"] = caller_gain
    if agent_gain is not None:
        rr["agent_gain"] = agent_gain


def _bursts(start, count, on=0.12, off=0.11, long_len=1.3):
    """A stutter shaped onset: ``count`` short bursts then one long utterance.
    Burst gaps sit below the VAD hangover so the train reads as one take."""
    segs = []
    t = start
    for _ in range(count):
        segs.append((t, t + on))
        t = t + on + off
    segs.append((t, t + long_len))
    return segs


# --------------------------------------------------------------------------
# suite definitions
# --------------------------------------------------------------------------

def build_silver():
    s = []
    # hard interruptions at varied onsets
    for code, onset in [("06", 0.6), ("12", 1.2), ("20", 2.0), ("30", 3.0), ("45", 4.5)]:
        s.append(yield_case(
            f"sv-hi-onset-{code}",
            f"Hard interruption at a {onset}s onset",
            ["interruption", "floor-taking", "onset-position"],
            "hard-interrupt-onset",
            f"The same hard floor take placed at {onset}s; onset position must not change the verdict.",
            onset=onset, yield_after=0.5, caller_segs=[(onset, onset + 2.0)],
            dur=onset + 2.9))
    # hard interruptions at varied caller durations
    for code, span in [("06", 0.6), ("10", 1.0), ("18", 1.8), ("30", 3.0)]:
        s.append(yield_case(
            f"sv-hi-dur-{code}",
            f"Hard interruption, caller holds the floor for {span}s",
            ["interruption", "floor-taking", "duration"],
            "hard-interrupt-duration",
            f"A real interrupt lasting {span}s; a short hold of the floor still counts as taking it.",
            onset=2.0, yield_after=0.45, caller_segs=[(2.0, 2.0 + span)],
            dur=2.0 + max(span, 0.45) + 1.0))
    # yield speed sweep
    for code, y in [("025", 0.25), ("050", 0.5), ("080", 0.8), ("120", 1.2)]:
        s.append(yield_case(
            f"sv-hi-speed-{code}",
            f"Hard interruption, reference agent stops after {y}s",
            ["interruption", "yield-speed"],
            "hard-interrupt-speed",
            f"The reference agent stops {y}s after onset; the bound checks yield speed, not just that it happened.",
            onset=2.0, yield_after=y, caller_segs=[(2.0, 4.2)], dur=5.0))
    # one-word interrupts
    for code, span in [("030", 0.30), ("038", 0.38), ("045", 0.45)]:
        s.append(yield_case(
            f"sv-ow-{code}",
            f"One-word interrupt, {span}s of caller speech",
            ["interruption", "one-word", "short"],
            "one-word-interrupt",
            "A single short word takes the floor and the agent stops; brevity alone must not demote a real interrupt.",
            onset=1.8, yield_after=0.25, caller_segs=[(1.8, 1.8 + span)],
            agent_extra=[(3.0, 4.2)], dur=5.0))
    # single backchannel at early / mid / late positions
    for code, onset, a_end, dur in [("early", 1.0, 4.6, 5.0), ("mid", 3.0, 6.6, 7.0), ("late", 4.4, 8.0, 8.4)]:
        s.append(hold_case(
            f"sv-bc-{code}",
            f"Single backchannel at {onset}s (should NOT yield)",
            ["backchannel", "false-trigger", code],
            "backchannel-position",
            "One brief acknowledgement while the agent talks; listener feedback, not a bid for the floor.",
            onset=onset, caller_segs=[(onset, onset + 0.3)], agent_end=a_end, dur=dur))
    # repeated backchannels
    s.append(hold_case(
        "sv-bc-rep-2", "Two backchannels across one agent turn (should NOT yield)",
        ["backchannel", "repeated"], "backchannel-repeated",
        "Two acknowledgements in a row; the correct agent holds through both.",
        onset=2.0, caller_segs=[(2.0, 2.28), (3.2, 3.48)], agent_end=5.4, dur=5.8))
    s.append(hold_case(
        "sv-bc-rep-4", "Four backchannels across one agent turn (should NOT yield)",
        ["backchannel", "repeated"], "backchannel-repeated",
        "Four acknowledgements in a row; every one is feedback and the agent holds.",
        onset=2.0, caller_segs=[(t, t + 0.28) for t in (2.0, 3.0, 4.0, 5.0)],
        agent_end=5.9, dur=6.3))
    s.append(hold_case(
        "sv-bc-rep-6", "Six backchannels across one agent turn (should NOT yield)",
        ["backchannel", "repeated"], "backchannel-repeated",
        "Six acknowledgements in a row; sustained feedback is still not a floor take.",
        onset=1.5, caller_segs=[(t, t + 0.28) for t in (1.5, 2.5, 3.5, 4.5, 5.5, 6.5)],
        agent_end=7.3, dur=7.7))
    s.append(hold_case(
        "sv-bc-pair-late", "Two late backchannels near the end of the turn (should NOT yield)",
        ["backchannel", "repeated", "late"], "backchannel-repeated",
        "Feedback landing late in the agent turn; position within the turn must not change the hold.",
        onset=4.0, caller_segs=[(4.0, 4.28), (5.0, 5.28)], agent_end=7.5, dur=7.9))
    # dense backchannels
    s.append(hold_case(
        "sv-bc-dense-8", "Eight backchannels in eight seconds (should NOT yield)",
        ["backchannel", "dense"], "backchannel-dense",
        "Eight acknowledgements in eight seconds; every one is feedback, none is a bid for the floor.",
        onset=1.2, caller_segs=[(1.2 + 0.9 * i, 1.2 + 0.9 * i + 0.28) for i in range(8)],
        agent_end=8.2, dur=8.6))
    # long near-miss acknowledgement
    s.append(hold_case(
        "sv-bc-longack", "A 0.8s acknowledgement that flirts with a floor take (should NOT yield)",
        ["backchannel", "near-miss"], "backchannel-longack",
        "A long acknowledgement that briefly resembles a floor take; still feedback, the agent holds.",
        onset=3.0, caller_segs=[(3.0, 3.8)], agent_end=6.8, dur=7.2))
    # graceful double-talk
    for code, y in [("04", 0.4), ("08", 0.8), ("15", 1.5)]:
        s.append(yield_case(
            f"sv-dt-graceful-{code}",
            f"Double-talk resolved gracefully in {y}s",
            ["double-talk", "overlap"], "double-talk-graceful",
            "Both channels live at once; the agent may overlap briefly but hands over the floor within the bound.",
            onset=2.2, yield_after=y, caller_segs=[(2.2, 5.4)], dur=6.1))
    # rapid multi-turn
    s.append(yield_case(
        "sv-mt-3x", "Three-turn exchange, scored at the interrupt",
        ["turn-taking", "multi-turn", "resume"], "multi-turn",
        "Interrupt, answer, hand back; scoring is anchored on the labeled onset.",
        onset=2.1, yield_after=0.35, caller_segs=[(2.1, 3.6)],
        agent_extra=[(4.1, 6.3)], dur=7.0))
    s.append(yield_case(
        "sv-mt-5x", "Five-turn rapid exchange, scored at the first interrupt",
        ["turn-taking", "multi-turn", "rapid"], "multi-turn",
        "Four caller turns and four agent turns in twelve seconds; a longer back and forth must score the same.",
        onset=1.7, yield_after=0.2,
        caller_segs=[(1.7, 2.9), (5.2, 6.0), (8.1, 8.9), (10.9, 11.6)],
        agent_extra=[(3.5, 4.8), (6.4, 7.7), (9.3, 10.4)], dur=12.2))
    # resume / re-interrupt
    s.append(yield_case(
        "sv-ri-first", "Yield, resume, re-interrupt: scored at the FIRST interrupt",
        ["turn-taking", "resume", "re-interrupt"], "resume-reinterrupt",
        "The agent yields, resumes, and is interrupted again; this fixture scores the first take.",
        onset=2.2, yield_after=0.2, caller_segs=[(2.2, 3.5), (4.9, 6.4)],
        agent_extra=[(4.2, 5.1)], dur=7.0))
    s.append(yield_case(
        "sv-ri-second", "Yield, resume, re-interrupt: scored at the SECOND interrupt",
        ["turn-taking", "resume", "re-interrupt"], "resume-reinterrupt",
        "Same shape as sv-ri-first with the label on the re-interrupt; the second yield must be as clean as the first.",
        onset=4.9, yield_after=0.2, caller_segs=[(2.2, 3.5), (4.9, 6.4)],
        agent_extra=[(4.2, 5.1)], agent_end=2.4, dur=7.0))
    # prompt-response latency, inside the bound
    for code, g in [("03", 0.3), ("06", 0.6), ("09", 0.9)]:
        s.append(latency_case(
            f"sv-lat-gap-{code}",
            f"Prompt response {g}s after the caller stops (latency PASS)",
            ["latency", "endpointing", "response-gap"], "latency-prompt",
            f"The caller stops and the agent answers {g}s later, inside the 1.0s bound.",
            gap=g))
    # stutter shaped onsets
    for n in (2, 3, 4):
        s.append(yield_case(
            f"sv-st-burst-{n}",
            f"Interrupt onset arriving as {n} short bursts then the full utterance",
            ["interruption", "stutter", "burst-onset"], "stutter-onset",
            "The onset arrives as short bursts before the full utterance; burst shaped starts must still read as one floor take.",
            onset=1.6, yield_after=0.8, caller_segs=_bursts(1.6, n),
            bounds=(1.3, 1.45), dur=4.6))
    # clean resume after the caller finishes
    s.append(yield_case(
        "sv-hi-resume-quick", "Yield then a clean resume 0.9s after the caller finishes",
        ["interruption", "resume"], "hard-interrupt-resume",
        "A correct yield followed by a prompt resume once the caller is done.",
        onset=2.0, yield_after=0.5, caller_segs=[(2.0, 3.5)],
        agent_extra=[(4.4, 5.6)], dur=6.2))
    s.append(yield_case(
        "sv-hi-resume-slow", "Yield then a resume 1.8s after the caller finishes",
        ["interruption", "resume"], "hard-interrupt-resume",
        "A correct yield with a slower resume; the barge-in verdict is unaffected by resume timing.",
        onset=2.0, yield_after=0.5, caller_segs=[(2.0, 3.5)],
        agent_extra=[(5.3, 6.5)], dur=7.1))
    return s


def build_silver_defects():
    s = []
    for code, onset in [("10", 1.0), ("25", 2.5)]:
        s.append(yield_case(
            f"svd-hi-missed-{code}",
            f"DEFECT RENDER: agent talks through a real interrupt at {onset}s (must FAIL)",
            ["interruption", "missed", "bad-agent"], "missed-interrupt",
            "DEFECT RENDER. The agent never stops for a clear floor take; this fixture must fail.",
            onset=onset, yield_after=0.5, caller_segs=[(onset, onset + 2.2)],
            agent_end=onset + 3.6, bounds=(0.7, 0.8), dur=onset + 4.0,
            verdict="fail", axis="barge_in"))
    s.append(yield_case(
        "svd-hi-slow-16", "DEFECT RENDER: yield after 1.6s against a 0.8s bound (must FAIL)",
        ["interruption", "slow-yield", "bad-agent"], "slow-yield",
        "DEFECT RENDER. The agent does stop, but 1.6s is twice the labeled bound; slow is a failure too.",
        onset=2.0, yield_after=1.6, caller_segs=[(2.0, 4.4)],
        bounds=(0.8, 2.5), dur=5.2, verdict="fail", axis="barge_in"))
    s.append(yield_case(
        "svd-hi-slow-22", "DEFECT RENDER: yield after 2.2s against a 1.0s bound (must FAIL)",
        ["interruption", "slow-yield", "bad-agent"], "slow-yield",
        "DEFECT RENDER. A very late yield; the caller repeated themselves long before the agent stopped.",
        onset=2.0, yield_after=2.2, caller_segs=[(2.0, 4.9)],
        bounds=(1.0, 3.2), dur=5.8, verdict="fail", axis="barge_in"))
    s.append(yield_case(
        "svd-hi-talkover", "DEFECT RENDER: talk-over exceeds its bound while the yield bound is met (must FAIL)",
        ["interruption", "talk-over", "bad-agent"], "talk-over-bound",
        "DEFECT RENDER. The yield lands inside its bound but the agent talked over the caller past the talk-over bound.",
        onset=2.0, yield_after=1.3, caller_segs=[(2.0, 4.2)],
        bounds=(2.2, 0.6), dur=5.0, verdict="fail", axis="barge_in"))
    s.append(hold_case(
        "svd-bc-false-early", "DEFECT RENDER: agent yields to an early backchannel (must FAIL)",
        ["backchannel", "false-trigger", "bad-agent"], "false-barge",
        "DEFECT RENDER. One early acknowledgement and the agent hands over the floor; a false barge-in.",
        onset=1.2, caller_segs=[(1.2, 1.48)], agent_end=1.35, dur=3.5,
        verdict="fail", axis="barge_in"))
    s.append(hold_case(
        "svd-bc-false-late", "DEFECT RENDER: agent yields to a late backchannel (must FAIL)",
        ["backchannel", "false-trigger", "bad-agent"], "false-barge",
        "DEFECT RENDER. The same false trigger late in the turn; position must not excuse it.",
        onset=4.8, caller_segs=[(4.8, 5.08)], agent_end=4.95, dur=7.0,
        verdict="fail", axis="barge_in"))
    s.append(hold_case(
        "svd-bc-false-third", "DEFECT RENDER: agent holds two backchannels then yields on the third (must FAIL)",
        ["backchannel", "false-trigger", "repeated", "bad-agent"], "false-barge",
        "DEFECT RENDER. Holding twice does not help if the third acknowledgement flips the agent; still a false barge-in.",
        onset=2.0, caller_segs=[(2.0, 2.28), (3.2, 3.48), (4.4, 4.68)],
        agent_end=4.55, dur=6.0, verdict="fail", axis="barge_in"))
    s.append(yield_case(
        "svd-dt-stubborn", "DEFECT RENDER: stubborn double-talk, agent never hands over (must FAIL)",
        ["double-talk", "overlap", "bad-agent"], "double-talk-stubborn",
        "DEFECT RENDER. Both channels stay live for the whole search window; the agent finishes its sentence instead of yielding.",
        onset=2.2, yield_after=0.8, caller_segs=[(2.2, 5.6)],
        agent_end=6.0, bounds=(1.2, 1.5), dur=6.4, verdict="fail", axis="barge_in"))
    s.append(yield_case(
        "svd-ow-missed", "DEFECT RENDER: one-word interrupt ignored (must FAIL)",
        ["interruption", "one-word", "missed", "bad-agent"], "missed-interrupt",
        "DEFECT RENDER. A single word takes the floor and the agent talks through it.",
        onset=1.8, yield_after=0.25, caller_segs=[(1.8, 2.18)],
        agent_end=5.2, bounds=(0.75, 0.9), dur=5.6, verdict="fail", axis="barge_in"))
    s.append(yield_case(
        "svd-st-missed", "DEFECT RENDER: stutter shaped interrupt ignored (must FAIL)",
        ["interruption", "stutter", "missed", "bad-agent"], "missed-interrupt",
        "DEFECT RENDER. A burst shaped onset followed by a full utterance, and the agent never stops.",
        onset=1.6, yield_after=0.8, caller_segs=_bursts(1.6, 3),
        agent_end=5.1, bounds=(1.3, 1.45), dur=5.5, verdict="fail", axis="barge_in"))
    s.append(latency_case(
        "svd-lat-sluggish-18", "DEFECT RENDER: 1.8s of dead air before the answer (latency must FAIL)",
        ["latency", "endpointing", "dead-air", "bad-agent"], "latency-sluggish",
        "DEFECT RENDER. The agent yields cleanly, then leaves 1.8s of dead air against a 1.0s bound.",
        gap=1.8, verdict="fail", axis="latency"))
    s.append(latency_case(
        "svd-lat-sluggish-26", "DEFECT RENDER: 2.6s of dead air before the answer (latency must FAIL)",
        ["latency", "endpointing", "dead-air", "bad-agent"], "latency-sluggish",
        "DEFECT RENDER. Two and a half seconds of silence reads as a dropped line on a phone call.",
        gap=2.6, verdict="fail", axis="latency"))
    s.append(latency_case(
        "svd-lat-overeager-04", "DEFECT RENDER: agent starts 0.4s before the caller finishes (latency must FAIL)",
        ["latency", "endpointing", "premature-start", "bad-agent"], "latency-overeager",
        "DEFECT RENDER. The agent steps on the tail of the caller's question by 0.4s.",
        lead=0.4, verdict="fail", axis="latency"))
    s.append(latency_case(
        "svd-lat-overeager-08", "DEFECT RENDER: agent starts 0.8s before the caller finishes (latency must FAIL)",
        ["latency", "endpointing", "premature-start", "bad-agent"], "latency-overeager",
        "DEFECT RENDER. Nearly a second of step-on; the opposite failure to dead air.",
        lead=0.8, verdict="fail", axis="latency"))
    s.append(yield_case(
        "svd-mt-second-missed", "DEFECT RENDER: clean first yield, re-interrupt ignored (must FAIL)",
        ["turn-taking", "re-interrupt", "missed", "bad-agent"], "missed-reinterrupt",
        "DEFECT RENDER. The agent yields once, resumes, then talks straight through the second interrupt; scored at the re-interrupt.",
        onset=4.9, yield_after=0.2, caller_segs=[(2.2, 3.5), (4.9, 6.6)],
        agent_extra=[(4.2, 8.4)], agent_end=2.4, bounds=(0.8, 1.0), dur=8.8,
        verdict="fail", axis="barge_in"))
    return s


def build_gold():
    s = []
    # noise floor sweep over the hard interruption
    for code, amp in [("002", 0.002), ("006", 0.006), ("020", 0.02)]:
        s.append(yield_case(
            f"gl-hi-noise-{code}",
            f"Hard interruption with the noise floor raised to {amp} amplitude",
            ["interruption", "noise-floor", "snr"], "noise-hard-interrupt",
            f"Same hard interrupt with the noise floor at {amp} peak amplitude (about {_noise_db(amp)} dBFS RMS); speech stays at 0.6 peak.",
            onset=2.0, yield_after=0.5, caller_segs=[(2.0, 4.2)], dur=5.0,
            noise=amp))
    # noise floor sweep over a backchannel hold
    for code, amp in [("002", 0.002), ("006", 0.006), ("020", 0.02)]:
        s.append(hold_case(
            f"gl-bc-noise-{code}",
            f"Backchannel hold with the noise floor raised to {amp} amplitude (should NOT yield)",
            ["backchannel", "noise-floor", "snr"], "noise-backchannel",
            f"A single acknowledgement over a noise floor of {amp} peak amplitude (about {_noise_db(amp)} dBFS RMS); the hold must survive the noise.",
            onset=3.0, caller_segs=[(3.0, 3.3)], agent_end=6.6, dur=7.0,
            noise=amp))
    # noise over double-talk
    for code, amp in [("006", 0.006), ("020", 0.02)]:
        s.append(yield_case(
            f"gl-dt-noise-{code}",
            f"Graceful double-talk with the noise floor raised to {amp} amplitude",
            ["double-talk", "overlap", "noise-floor"], "noise-double-talk",
            f"Overlapping speech over a {amp} amplitude noise floor (about {_noise_db(amp)} dBFS RMS); overlap plus noise is the hard measurement case.",
            onset=2.2, yield_after=0.8, caller_segs=[(2.2, 5.4)], dur=6.1,
            noise=amp))
    # noise over a one-word interrupt
    s.append(yield_case(
        "gl-ow-noise-006", "One-word interrupt over a raised noise floor",
        ["interruption", "one-word", "noise-floor"], "noise-one-word",
        f"A 0.38s interrupt over a 0.006 amplitude noise floor (about {_noise_db(0.006)} dBFS RMS); short speech must clear the raised floor.",
        onset=1.8, yield_after=0.25, caller_segs=[(1.8, 2.18)],
        agent_extra=[(3.0, 4.2)], dur=5.0, noise=0.006))
    # 8 kHz telephony replicas
    s.append(yield_case(
        "gl-8k-hard-interrupt", "Hard interruption at 8 kHz",
        ["interruption", "telephony", "8khz"], "telephony-8k",
        "Telephony rate replica of the hard interruption; 8 kHz must not change the verdict.",
        onset=2.0, yield_after=0.5, caller_segs=[(2.0, 4.2)], dur=5.0, sr=8000))
    s.append(yield_case(
        "gl-8k-one-word", "One-word interrupt at 8 kHz",
        ["interruption", "one-word", "telephony", "8khz"], "telephony-8k",
        "Telephony rate replica of the one-word interrupt.",
        onset=1.8, yield_after=0.25, caller_segs=[(1.8, 2.18)],
        agent_extra=[(3.0, 4.2)], dur=5.0, sr=8000))
    s.append(hold_case(
        "gl-8k-bc-single", "Single backchannel at 8 kHz (should NOT yield)",
        ["backchannel", "telephony", "8khz"], "telephony-8k",
        "Telephony rate replica of the mid-turn backchannel hold.",
        onset=3.0, caller_segs=[(3.0, 3.3)], agent_end=6.6, dur=7.0, sr=8000))
    s.append(hold_case(
        "gl-8k-bc-repeated", "Four backchannels at 8 kHz (should NOT yield)",
        ["backchannel", "repeated", "telephony", "8khz"], "telephony-8k",
        "Telephony rate replica of the repeated backchannel hold.",
        onset=2.0, caller_segs=[(t, t + 0.28) for t in (2.0, 3.0, 4.0, 5.0)],
        agent_end=5.9, dur=6.3, sr=8000))
    s.append(yield_case(
        "gl-8k-dt-graceful", "Graceful double-talk at 8 kHz",
        ["double-talk", "telephony", "8khz"], "telephony-8k",
        "Telephony rate replica of the graceful double-talk case.",
        onset=2.2, yield_after=0.8, caller_segs=[(2.2, 5.4)], dur=6.1, sr=8000))
    s.append(yield_case(
        "gl-8k-multiturn", "Three-turn exchange at 8 kHz",
        ["turn-taking", "multi-turn", "telephony", "8khz"], "telephony-8k",
        "Telephony rate replica of the three-turn exchange.",
        onset=2.1, yield_after=0.35, caller_segs=[(2.1, 3.6)],
        agent_extra=[(4.1, 6.3)], dur=7.0, sr=8000))
    s.append(yield_case(
        "gl-8k-stutter", "Stutter shaped interrupt at 8 kHz",
        ["interruption", "stutter", "telephony", "8khz"], "telephony-8k",
        "Telephony rate replica of the burst shaped onset.",
        onset=1.6, yield_after=0.8, caller_segs=_bursts(1.6, 3),
        bounds=(1.3, 1.45), dur=4.6, sr=8000))
    s.append(latency_case(
        "gl-8k-latency-prompt", "Prompt response at 8 kHz (latency PASS)",
        ["latency", "endpointing", "telephony", "8khz"], "telephony-8k",
        "Telephony rate replica of the prompt-response case; 0.5s of gap inside the 1.0s bound.",
        gap=0.5, sr=8000))
    s.append(hold_case(
        "gl-8k-echo-hold", "Echo bleed at 8 kHz (should NOT yield)",
        ["echo", "self-interrupt", "telephony", "8khz"], "telephony-8k",
        "Telephony rate replica of the echo bleed hold; the input carries only the agent's own delayed output.",
        onset=2.0, caller_segs=[], agent_end=5.7, dur=6.0, sr=8000,
        echo=(0.12, 0.35)))
    s.append(yield_case(
        "gl-8k-noise-006", "Hard interruption at 8 kHz over a raised noise floor",
        ["interruption", "telephony", "8khz", "noise-floor"], "telephony-8k",
        f"8 kHz and a 0.006 amplitude noise floor (about {_noise_db(0.006)} dBFS RMS) at once; conditions compound.",
        onset=2.0, yield_after=0.5, caller_segs=[(2.0, 4.2)], dur=5.0, sr=8000,
        noise=0.006))
    # quiet and loud channels
    for code, gain in [("30", 0.3), ("10", 0.1), ("05", 0.05)]:
        s.append(yield_case(
            f"gl-quiet-caller-{code}",
            f"Quiet caller, channel scaled to {gain}x",
            ["interruption", "gain", "quiet-caller"], "channel-gain",
            f"Caller channel scaled to {gain}x; a low capture level must still read as a floor take.",
            onset=2.0, yield_after=0.5, caller_segs=[(2.0, 4.2)], dur=5.0,
            caller_gain=gain))
    s.append(yield_case(
        "gl-loud-caller-150", "Hot caller channel, scaled to 1.5x",
        ["interruption", "gain", "loud-caller"], "channel-gain",
        "Caller channel scaled to 1.5x of the reference level; a hot capture must not change the verdict.",
        onset=2.0, yield_after=0.5, caller_segs=[(2.0, 4.2)], dur=5.0,
        caller_gain=1.5))
    s.append(yield_case(
        "gl-loud-agent-150", "Hot agent channel, scaled to 1.5x",
        ["interruption", "gain", "loud-agent"], "channel-gain",
        "Agent channel scaled to 1.5x; the yield must be measured the same on a hot channel.",
        onset=2.0, yield_after=0.5, caller_segs=[(2.0, 4.2)], dur=5.0,
        agent_gain=1.5))
    s.append(yield_case(
        "gl-quiet-agent-30", "Quiet agent channel, scaled to 0.3x",
        ["interruption", "gain", "quiet-agent"], "channel-gain",
        "Agent channel scaled to 0.3x; a quiet agent still has to stop, and the stop must still be visible.",
        onset=2.0, yield_after=0.5, caller_segs=[(2.0, 4.2)], dur=5.0,
        agent_gain=0.3))
    # echo / self-interrupt holds at varied delay and gain
    for code, delay, gain in [("fast", 0.08, 0.5), ("mid", 0.25, 0.35), ("long", 0.40, 0.20)]:
        s.append(hold_case(
            f"gl-echo-hold-{code}",
            f"Echo bleed, {delay}s delay at {gain}x gain (should NOT yield)",
            ["echo", "self-interrupt", "aec"], "echo-hold",
            f"The input channel carries only the agent's own output delayed {delay}s at {gain}x gain; the correct agent keeps talking.",
            onset=2.0, caller_segs=[], agent_end=5.7, dur=6.0,
            echo=(delay, gain)))
    # edge timings
    s.append(yield_case(
        "gl-edge-immediate", "Interrupt 0.2s after the agent starts talking",
        ["interruption", "edge-timing"], "edge-timing",
        "The caller barges in almost as soon as the agent opens its mouth; early edges must be measurable.",
        onset=0.4, yield_after=0.4, caller_segs=[(0.4, 2.2)], dur=3.0))
    s.append(yield_case(
        "gl-edge-tail", "Interrupt near the end of the recording",
        ["interruption", "edge-timing"], "edge-timing",
        "The floor take lands 1.2s before the file ends; the yield window barely fits and must still resolve.",
        onset=4.8, yield_after=0.4, caller_segs=[(4.8, 5.9)], dur=6.0))
    # heavy overlap
    s.append(yield_case(
        "gl-ov-two-bouts", "A backchannel bout then a real floor take",
        ["overlap", "double-talk", "discrimination"], "overlap-heavy",
        "A short caller bout, a pause, then the real take; scored at the labeled second onset.",
        onset=3.0, yield_after=0.8, caller_segs=[(2.0, 2.6), (3.0, 5.2)],
        bounds=(1.3, 1.6), dur=5.8))
    s.append(yield_case(
        "gl-ov-long-lead", "A slow but bounded yield under full overlap",
        ["overlap", "double-talk", "talk-over"], "overlap-heavy",
        "The agent takes 1.6s to hand over while both channels run; inside the labeled tolerant bound.",
        onset=1.4, yield_after=1.6, caller_segs=[(1.4, 4.6)],
        bounds=(2.2, 2.4), dur=5.2))
    # endurance
    s.append(yield_case(
        "gl-endurance-60s", "One minute recording, scored interrupt at 45s",
        ["interruption", "endurance", "long-recording"], "endurance",
        "One minute of audio with backchannels along the way and the scored interrupt at 45s; long recordings must not degrade the measurement.",
        onset=45.0, yield_after=0.5,
        caller_segs=[(5.0, 5.3), (15.0, 15.3), (28.0, 28.3), (40.0, 40.3), (45.0, 47.5)],
        agent_extra=[(0.2, 10.0), (10.8, 22.0), (22.7, 34.0)],
        agent_start=34.6, agent_end=45.5, dur=60.0))
    # combined conditions
    s.append(yield_case(
        "gl-mt-noise-006", "Three-turn exchange over a raised noise floor",
        ["turn-taking", "multi-turn", "noise-floor"], "noise-multi-turn",
        f"The three-turn exchange over a 0.006 amplitude noise floor (about {_noise_db(0.006)} dBFS RMS).",
        onset=2.1, yield_after=0.35, caller_segs=[(2.1, 3.6)],
        agent_extra=[(4.1, 6.3)], dur=7.0, noise=0.006))
    s.append(yield_case(
        "gl-ri-noise-006", "Re-interrupt scored under a raised noise floor",
        ["turn-taking", "re-interrupt", "noise-floor"], "noise-multi-turn",
        f"The re-interrupt case over a 0.006 amplitude noise floor (about {_noise_db(0.006)} dBFS RMS); scored at the second take.",
        onset=4.9, yield_after=0.2, caller_segs=[(2.2, 3.5), (4.9, 6.4)],
        agent_extra=[(4.2, 5.1)], agent_end=2.4, dur=7.0, noise=0.006))
    s.append(latency_case(
        "gl-lat-noise-006", "Prompt response over a raised noise floor (latency PASS)",
        ["latency", "endpointing", "noise-floor"], "noise-latency",
        f"A 0.5s response gap measured over a 0.006 amplitude noise floor (about {_noise_db(0.006)} dBFS RMS).",
        gap=0.5, noise=0.006))
    s.append(latency_case(
        "gl-lat-noise-020", "Prompt response over a loud noise floor (latency PASS)",
        ["latency", "endpointing", "noise-floor"], "noise-latency",
        f"A 0.5s response gap over a 0.02 amplitude noise floor (about {_noise_db(0.02)} dBFS RMS); boundary tolerance widened to two hops.",
        gap=0.5, noise=0.02, tolerance_hops=2))
    s.append(yield_case(
        "gl-st-noise-006", "Stutter shaped interrupt over a raised noise floor",
        ["interruption", "stutter", "noise-floor"], "noise-stutter",
        f"Burst shaped onset over a 0.006 amplitude noise floor (about {_noise_db(0.006)} dBFS RMS); short bursts must clear the floor.",
        onset=1.6, yield_after=0.8, caller_segs=_bursts(1.6, 3),
        bounds=(1.3, 1.45), dur=4.6, noise=0.006))
    s.append(yield_case(
        "gl-quiet-noise-combo", "Quiet caller over a raised noise floor",
        ["interruption", "gain", "noise-floor", "quiet-caller"], "channel-gain",
        f"Caller at 0.3x gain over a 0.002 amplitude noise floor (about {_noise_db(0.002)} dBFS RMS); low level and noise at once.",
        onset=2.0, yield_after=0.5, caller_segs=[(2.0, 4.2)], dur=5.0,
        caller_gain=0.3, noise=0.002))
    s.append(yield_case(
        "gl-8k-quiet-caller", "Quiet caller at 8 kHz, channel scaled to 0.2x",
        ["interruption", "gain", "quiet-caller", "telephony", "8khz"], "channel-gain",
        "8 kHz and a 0.2x caller level at once; compound capture conditions.",
        onset=2.0, yield_after=0.5, caller_segs=[(2.0, 4.2)], dur=5.0, sr=8000,
        caller_gain=0.2))
    return s


def build_gold_defects():
    s = []
    for code, delay, gain in [("fast", 0.12, 0.5), ("slow", 0.30, 0.45)]:
        s.append(hold_case(
            f"gld-echo-phantom-{code}",
            f"DEFECT RENDER: agent yields to its own {delay}s echo (must FAIL)",
            ["echo", "self-interrupt", "phantom", "bad-agent"], "echo-phantom",
            "DEFECT RENDER. The only input energy is the agent's own delayed output, and the agent stops for it; a phantom self-interruption.",
            onset=2.0, caller_segs=[], agent_end=2.6, dur=6.0,
            echo=(delay, gain), verdict="fail", axis="barge_in"))
    for code, amp in [("006", 0.006), ("020", 0.02)]:
        s.append(yield_case(
            f"gld-hi-missed-noise-{code}",
            f"DEFECT RENDER: missed interrupt under a {amp} amplitude noise floor (must FAIL)",
            ["interruption", "missed", "noise-floor", "bad-agent"], "noise-missed",
            f"DEFECT RENDER. The agent talks through a floor take over a {amp} amplitude noise floor (about {_noise_db(amp)} dBFS RMS); noise must not excuse the miss.",
            onset=2.0, yield_after=0.5, caller_segs=[(2.0, 4.2)],
            agent_end=5.6, bounds=(0.7, 0.8), dur=6.0, noise=amp,
            verdict="fail", axis="barge_in"))
    s.append(hold_case(
        "gld-bc-false-noise-006", "DEFECT RENDER: false barge on a backchannel under noise (must FAIL)",
        ["backchannel", "false-trigger", "noise-floor", "bad-agent"], "noise-false-barge",
        f"DEFECT RENDER. One acknowledgement over a 0.006 amplitude noise floor (about {_noise_db(0.006)} dBFS RMS) and the agent hands over the floor.",
        onset=2.2, caller_segs=[(2.2, 2.48)], agent_end=2.35, dur=4.5,
        noise=0.006, verdict="fail", axis="barge_in"))
    s.append(yield_case(
        "gld-8k-missed", "DEFECT RENDER: missed interrupt at 8 kHz (must FAIL)",
        ["interruption", "missed", "telephony", "8khz", "bad-agent"], "telephony-8k-defect",
        "DEFECT RENDER. The 8 kHz replica of a missed floor take.",
        onset=2.0, yield_after=0.5, caller_segs=[(2.0, 4.2)],
        agent_end=5.6, bounds=(0.7, 0.8), dur=6.0, sr=8000,
        verdict="fail", axis="barge_in"))
    s.append(hold_case(
        "gld-8k-false-barge", "DEFECT RENDER: false barge on a backchannel at 8 kHz (must FAIL)",
        ["backchannel", "false-trigger", "telephony", "8khz", "bad-agent"], "telephony-8k-defect",
        "DEFECT RENDER. The 8 kHz replica of a false barge-in on a bare acknowledgement.",
        onset=2.2, caller_segs=[(2.2, 2.48)], agent_end=2.35, dur=4.5, sr=8000,
        verdict="fail", axis="barge_in"))
    s.append(yield_case(
        "gld-quiet-caller-invisible", "CAPTURE DEFECT RENDER: caller below the absolute gate (must FAIL)",
        ["interruption", "gain", "capture-defect"], "capture-defect",
        "CAPTURE DEFECT RENDER. The caller channel is scaled to 0.003x, below the scorer's -60 dBFS absolute gate; the rendered yield cannot be attributed to the caller and the fixture must fail. Fix the capture level, not the agent.",
        onset=2.0, yield_after=0.5, caller_segs=[(2.0, 4.2)], dur=5.0,
        caller_gain=0.003, verdict="fail", axis="barge_in"))
    s.append(yield_case(
        "gld-noise-saturated", "CAPTURE DEFECT RENDER: noise floor saturates the energy VAD (must FAIL)",
        ["interruption", "noise-floor", "capture-defect"], "capture-defect",
        f"CAPTURE DEFECT RENDER. A 0.1 amplitude noise floor (about {_noise_db(0.1)} dBFS RMS) sits within the VAD's 22 dB dynamic margin of the speech peaks, so every frame reads active and the agent's stop is not observable; the fixture must fail. This is the honest measurement ceiling.",
        onset=2.0, yield_after=0.5, caller_segs=[(2.0, 4.2)], dur=5.0,
        noise=0.1, verdict="fail", axis="barge_in"))
    s.append(yield_case(
        "gld-hi-slow-noise", "DEFECT RENDER: slow yield under a raised noise floor (must FAIL)",
        ["interruption", "slow-yield", "noise-floor", "bad-agent"], "noise-slow-yield",
        f"DEFECT RENDER. A 1.8s yield against a 0.8s bound, over a 0.006 amplitude noise floor (about {_noise_db(0.006)} dBFS RMS).",
        onset=2.0, yield_after=1.8, caller_segs=[(2.0, 4.6)],
        bounds=(0.8, 2.6), dur=5.4, noise=0.006, verdict="fail", axis="barge_in"))
    s.append(yield_case(
        "gld-dt-stubborn-8k", "DEFECT RENDER: stubborn double-talk at 8 kHz (must FAIL)",
        ["double-talk", "overlap", "telephony", "8khz", "bad-agent"], "telephony-8k-defect",
        "DEFECT RENDER. The 8 kHz replica of an agent that never hands over under sustained overlap.",
        onset=2.2, yield_after=0.8, caller_segs=[(2.2, 5.6)],
        agent_end=6.0, bounds=(1.2, 1.5), dur=6.4, sr=8000,
        verdict="fail", axis="barge_in"))
    s.append(latency_case(
        "gld-lat-sluggish-8k", "DEFECT RENDER: 2.0s of dead air at 8 kHz (latency must FAIL)",
        ["latency", "endpointing", "telephony", "8khz", "bad-agent"], "telephony-8k-defect",
        "DEFECT RENDER. Two seconds of dead air against a 1.0s bound, at telephony rate.",
        gap=2.0, sr=8000, verdict="fail", axis="latency"))
    s.append(latency_case(
        "gld-lat-overeager-noise", "DEFECT RENDER: 0.5s premature start under noise (latency must FAIL)",
        ["latency", "endpointing", "premature-start", "noise-floor", "bad-agent"], "noise-latency-defect",
        f"DEFECT RENDER. The agent steps on the caller by 0.5s over a 0.006 amplitude noise floor (about {_noise_db(0.006)} dBFS RMS).",
        lead=0.5, noise=0.006, verdict="fail", axis="latency"))
    s.append(yield_case(
        "gld-endurance-missed", "DEFECT RENDER: interrupt at 45s of a 50s call ignored (must FAIL)",
        ["interruption", "endurance", "missed", "bad-agent"], "endurance-defect",
        "DEFECT RENDER. Fifty seconds in, the agent talks through the scored interrupt; endurance must not blunt the failure.",
        onset=45.0, yield_after=0.5,
        caller_segs=[(7.0, 7.3), (20.0, 20.3), (33.0, 33.3), (45.0, 47.6)],
        agent_extra=[(0.2, 10.0), (10.8, 24.0), (24.7, 38.0)],
        agent_start=38.6, agent_end=49.4, bounds=(1.0, 1.15), dur=50.0,
        verdict="fail", axis="barge_in"))
    s.append(yield_case(
        "gld-ri-missed-8k", "DEFECT RENDER: re-interrupt ignored at 8 kHz (must FAIL)",
        ["turn-taking", "re-interrupt", "missed", "telephony", "8khz", "bad-agent"], "telephony-8k-defect",
        "DEFECT RENDER. The 8 kHz replica of a clean first yield followed by a missed re-interrupt; scored at the second take.",
        onset=4.9, yield_after=0.2, caller_segs=[(2.2, 3.5), (4.9, 6.6)],
        agent_extra=[(4.2, 8.4)], agent_end=2.4, bounds=(0.8, 1.0), dur=8.8,
        sr=8000, verdict="fail", axis="barge_in"))
    s.append(yield_case(
        "gld-dt-overlong-noise", "DEFECT RENDER: talk-over bound exceeded under noise (must FAIL)",
        ["double-talk", "talk-over", "noise-floor", "bad-agent"], "noise-talk-over",
        f"DEFECT RENDER. The yield lands inside its time bound but the overlap runs past the talk-over bound, over a 0.006 amplitude noise floor (about {_noise_db(0.006)} dBFS RMS).",
        onset=2.2, yield_after=1.4, caller_segs=[(2.2, 5.0)],
        bounds=(2.4, 0.7), dur=5.8, noise=0.006, verdict="fail", axis="barge_in"))
    return s


BUILDERS = {
    "silver": build_silver,
    "silver-defects": build_silver_defects,
    "gold": build_gold,
    "gold-defects": build_gold_defects,
}

TIER_NOTES = {
    "silver": "clean conditions, 16 kHz, default noise floor; every reference render passes",
    "silver-defects": "clean conditions, deliberate defect renders; every scenario fails on its labeled axis",
    "gold": "hard conditions: noise floors, 8 kHz, gain extremes, echo, edge timings, endurance; reference renders pass",
    "gold-defects": "hard-condition defect renders plus two labeled capture-defect cases; every scenario fails on its labeled axis",
}


# --------------------------------------------------------------------------
# build / check
# --------------------------------------------------------------------------

def _dump_json(path, obj):
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(obj, fh, indent=2)
        fh.write("\n")


def _suite_manifest(scenarios):
    entries = []
    for sc in scenarios:
        entries.append({
            "id": sc["id"],
            "title": sc["title"],
            "category": sc["category"],
            "family": sc["family"],
            "sample_rate": sc["sample_rate"],
            "expected_yield": sc["expected"].get("yield"),
            "reference_verdict": sc["reference_verdict"],
            "failure_axis": sc.get("failure_axis"),
            "example_wav": f"audio/{sc['id']}.example.wav",
            "caller_wav": f"audio/{sc['id']}.caller.wav",
        })
    return {"scenarios": entries}


def _suites_manifest(all_suites):
    suites = []
    total = 0
    for name in SUITE_NAMES:
        scenarios = all_suites[name]
        total += len(scenarios)
        barge_fail = sum(
            1 for sc in scenarios
            if sc["reference_verdict"] == "fail" and sc.get("failure_axis") == "barge_in"
        )
        latency_fail = sum(
            1 for sc in scenarios
            if sc["reference_verdict"] == "fail" and sc.get("failure_axis") == "latency"
        )
        families = {}
        categories = {}
        for sc in scenarios:
            families[sc["family"]] = families.get(sc["family"], 0) + 1
            categories[sc["category"]] = categories.get(sc["category"], 0) + 1
        suites.append({
            "name": name,
            "path": name,
            "tier": "silver" if name.startswith("silver") else "gold",
            "note": TIER_NOTES[name],
            "scenarios": len(scenarios),
            "barge_in_pass": len(scenarios) - barge_fail,
            "barge_in_fail": barge_fail,
            "latency_axis_fail": latency_fail,
            "expected_exit_code": 1 if barge_fail else 0,
            "sample_rates": sorted({sc["sample_rate"] for sc in scenarios}),
            "families": dict(sorted(families.items())),
            "categories": dict(sorted(categories.items())),
        })
    dims = {
        "sample_rates": sorted({sc["sample_rate"] for ss in all_suites.values() for sc in ss}),
        "noise_floor_amps": sorted({
            sc["reference_render"].get("noise_floor_amp", 0.0006)
            for ss in all_suites.values() for sc in ss
        }),
        "caller_gains": sorted({
            sc["reference_render"].get("caller_gain", 1.0)
            for ss in all_suites.values() for sc in ss
        }),
        "agent_gains": sorted({
            sc["reference_render"].get("agent_gain", 1.0)
            for ss in all_suites.values() for sc in ss
        }),
        "echo_variants": sorted(
            "delay {:.2f}s gain {:.2f}x".format(
                sc["reference_render"]["echo_delay_sec"],
                sc["reference_render"]["echo_gain"])
            for ss in all_suites.values() for sc in ss
            if sc["reference_render"].get("caller_is_echo_of_agent")
        ),
        "max_duration_sec": max(
            sc["duration_sec"] for ss in all_suites.values() for sc in ss
        ),
        "families": sorted({sc["family"] for ss in all_suites.values() for sc in ss}),
    }
    return {
        "generated_by": "corpus/suites/build_suites.py",
        "synthetic": True,
        "note": "Every scenario is synthetic shaped noise rendered deterministically "
                "from its own reference_render timings (seed = sha256(id)). The "
                "timings are the ground truth. No accuracy claim is made or implied.",
        "total_scenarios": total,
        "suites": suites,
        "dimensions": dims,
    }


def build(root=HERE):
    renderer = load_renderer()
    all_suites = {name: BUILDERS[name]() for name in SUITE_NAMES}
    counts = {}
    for name in SUITE_NAMES:
        scenarios = all_suites[name]
        ids = [sc["id"] for sc in scenarios]
        if len(ids) != len(set(ids)):
            raise SystemExit(f"duplicate ids inside suite {name}")
        scen_dir = os.path.join(root, name, "scenarios")
        audio_dir = os.path.join(root, name, "audio")
        os.makedirs(scen_dir, exist_ok=True)
        os.makedirs(audio_dir, exist_ok=True)
        for sc in scenarios:
            _dump_json(os.path.join(scen_dir, sc["id"] + ".json"), sc)
            sr, caller, agent = renderer.build_scenario(sc)
            renderer.write_wav(
                os.path.join(audio_dir, sc["id"] + ".example.wav"), sr, [caller, agent])
            renderer.write_wav(
                os.path.join(audio_dir, sc["id"] + ".caller.wav"), sr, [caller])
        _dump_json(os.path.join(scen_dir, "manifest.json"), _suite_manifest(scenarios))
        counts[name] = len(scenarios)
    _dump_json(os.path.join(root, "manifest.json"), _suites_manifest(all_suites))
    return counts


def check(root=HERE) -> int:
    """Regenerate everything into a temp dir and byte-compare with disk."""
    problems = []
    with tempfile.TemporaryDirectory(prefix="hotato-suites-check-") as tmp:
        build(root=tmp)
        for dirpath, _, filenames in os.walk(tmp):
            rel = os.path.relpath(dirpath, tmp)
            for fn in sorted(filenames):
                fresh = os.path.join(dirpath, fn)
                committed = os.path.join(root, rel, fn)
                if not os.path.exists(committed):
                    problems.append(f"missing on disk: {os.path.join(rel, fn)}")
                    continue
                with open(fresh, "rb") as fa, open(committed, "rb") as fb:
                    if fa.read() != fb.read():
                        problems.append(f"differs: {os.path.join(rel, fn)}")
    if problems:
        print("build_suites --check: DRIFT DETECTED:")
        for p in problems:
            print("  -", p)
        return 1
    print("build_suites --check: regenerated output is byte-identical to disk")
    return 0


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description="Build or verify the tiered synthetic suites.")
    p.add_argument("--check", action="store_true",
                   help="regenerate to a temp dir and byte-compare against disk")
    args = p.parse_args(argv)
    if args.check:
        return check()
    counts = build()
    total = sum(counts.values())
    for name in SUITE_NAMES:
        print(f"  {name}: {counts[name]} scenarios")
    print(f"Built {total} scenarios under {HERE}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
