#!/usr/bin/env python3
"""Build the four corpus scenario CLASSES under corpus/classes/.

Every scenario here is SYNTHETIC and says so, exactly like corpus/suites/:
deterministic shaped noise rendered from the exact segment timings in its own
JSON (seed = sha256(id), byte-identical on every machine). The segment
timings ARE the ground truth. No recorded speech, no accuracy claim.

This module deliberately REUSES the existing corpus generator rather than
re-inventing it: the scenario-builder helpers (``_sc``, ``_seg``, ``yield_case``,
``hold_case``, ``latency_case``, ...) are loaded straight from
``corpus/suites/build_suites.py``, and the audio itself is rendered by the
same ``examples/render_examples.py`` used by every other fixture in this
repository. The only NEW code here is (a) the four scenario shapes below and
(b) the deterministic telephony degradation used by ``telephony-degraded``
(``telephony_codec.py``), applied as an explicit, labeled POST-processing
step so the shared render path stays untouched.

Classes:
  mid-utterance-pause     the caller speaks, pauses mid-turn for a multi-second
                           gap (a thinking pause), then resumes. Labeled at the
                           pause; a well-behaved agent must not grab the floor
                           during it. Scored on the latency axis
                           (premature_start_sec), the same signal that already
                           powers the bundled prompt-response fixtures, but
                           with ``turn_end_silence_sec`` widened past the pause
                           length so the pause itself is not mistaken for the
                           end of the caller's turn (default hangover is
                           0.20s; the pause is multi-second). This is stated
                           explicitly in every scenario's ``why_it_matters``
                           and pinned by ``tests/test_corpus_classes.py``,
                           which is the only place that config is applied.
  backchannel-multilingual short non-English acknowledgement tokens (romanized
                           labels only: Hindi/Telugu 'hmm', Spanish 'si',
                           Japanese 'hai') over agent speech, should NOT
                           yield. Hotato's VAD is energy-based, not lexical:
                           it does not detect language or words. Every clip in
                           this corpus is rendered shaped noise, never real
                           phonetic content, so "multilingual" here documents
                           that the funnel and its labels are not built
                           English-only, not that the tool performs language
                           identification.
  noise-hold               the caller channel carries sustained non-speech
                           energy for most of the call (a cafe/TV-like
                           background presence, not a brief backchannel),
                           should NOT yield. Hotato measures whether the AGENT
                           held the floor through that energy; it does not
                           classify the energy as noise versus speech, and
                           this class says so.
  telephony-degraded       an existing gold scenario's exact timings
                           (gl-8k-hard-interrupt), re-rendered through a
                           degraded 8 kHz telephony line: mu-law companding
                           plus mild, fixed-schedule packet loss
                           (``telephony_codec.py``). Proves the scorer's
                           verdict is stable across codec degradation in BOTH
                           directions: the reference agent still passes, and
                           the same missed-interrupt defect still fails.

Usage:
  python3 corpus/classes/build_classes.py           # write JSONs + render audio
  python3 corpus/classes/build_classes.py --check   # regenerate to a temp dir and
                                                      # byte-compare against disk
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import os
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))            # corpus/classes
REPO = os.path.dirname(os.path.dirname(HERE))                 # repo root
SUITES_DIR = os.path.join(REPO, "corpus", "suites")
BUILD_SUITES_PATH = os.path.join(SUITES_DIR, "build_suites.py")

CLASS_NAMES = [
    "mid-utterance-pause",
    "backchannel-multilingual",
    "noise-hold",
    "telephony-degraded",
    "leading-edge-onset",
    "structured-utterance",
    "browser-telephony-parity",
]


def load_build_suites():
    """Load corpus/suites/build_suites.py so its scenario-builder helpers
    (``_sc``, ``_seg``, ``yield_case``, ``hold_case``, ...) can be reused
    verbatim instead of re-implemented."""
    spec = importlib.util.spec_from_file_location("hotato_build_suites", BUILD_SUITES_PATH)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod


def load_telephony_codec():
    spec = importlib.util.spec_from_file_location(
        "hotato_telephony_codec", os.path.join(HERE, "telephony_codec.py"))
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod


# ---------------------------------------------------------------------------
# a scenario shape NOT already covered by build_suites.py: a caller utterance
# with an internal multi-second thinking pause, scored on the latency axis.
# ---------------------------------------------------------------------------

def pause_case(bs, sid, title, tags, family, why, *, pre_len, pause_gap, resume_len,
               resp_gap=None, resp_lead_into_pause=None, sr=16000, verdict="pass",
               axis=None, turn_end_silence_sec):
    """A caller utterance with an internal thinking pause: speaks for
    ``pre_len``, falls silent for ``pause_gap`` seconds mid-turn, resumes for
    ``resume_len``, then truly stops. Exactly one of ``resp_gap`` (the agent
    answers this many seconds after the TRUE end, i.e. after the resume) or
    ``resp_lead_into_pause`` (the agent starts this many seconds INTO the
    pause window, before the caller resumes: a premature grab) must be given.

    ``turn_end_silence_sec`` is carried in the label so the harness (and
    ``tests/test_corpus_classes.py``) can score it with a ``ScoreConfig`` wide
    enough that the pause itself is not mistaken for the end of the caller's
    turn; Hotato's DEFAULT turn-end detector fires after only 0.20s of
    silence, which the multi-second pause would trip immediately. This is the
    honest reason this class is not scored by the generic suite tests with
    the library default config, and is stated in ``why_it_matters``."""
    assert (resp_gap is None) != (resp_lead_into_pause is None)
    assert turn_end_silence_sec > pause_gap, "the pause must not look like a turn end"
    onset = 1.6
    pre_end = round(onset + pre_len, 2)
    resume_start = round(pre_end + pause_gap, 2)
    resume_end = round(resume_start + resume_len, 2)
    if resp_gap is not None:
        resp_on = round(resume_end + resp_gap, 2)
    else:
        resp_on = round(pre_end + resp_lead_into_pause, 2)
        assert resp_on < resume_start, "the premature grab must land inside the pause"
    resp_end = round(resp_on + 1.5, 2)
    # the caller track needs turn_end_silence_sec of UNBROKEN trailing silence
    # after resume_end SOMEWHERE in the file for _caller_turn_end_idx to
    # resolve at all (it scans the whole track, not just up to the agent's
    # response), so the recording must outlast that regardless of resp_end.
    dur = round(max(resp_end, resume_end + turn_end_silence_sec) + 0.3, 2)
    rr = {
        "continuous": True,
        "agent_segments_sec": bs._seg([(0.2, 1.4), (resp_on, resp_end)]),
        "caller_segments_sec": bs._seg([(onset, pre_end), (resume_start, resume_end)]),
        "caller_offset_sec": resume_end,
        "agent_response_onset_sec": resp_on,
        "pause_gap_sec": round(pause_gap, 2),
    }
    if resp_gap is not None:
        rr["rendered_response_gap_sec"] = round(resp_gap, 2)
        signals = ["response_gap_sec", "did_yield"]
    else:
        lead = round(resume_end - resp_on, 2)
        rr["rendered_premature_lead_sec"] = lead
        signals = ["premature_start_sec", "did_yield"]
    expected = {"yield": True, "max_time_to_yield_sec": 0.70, "max_talk_over_sec": 0.80}
    latency_bounds = {
        "max_response_gap_sec": 1.00,
        "premature_is_failure": True,
        "boundary_tolerance_hops": 1,
        "turn_end_silence_sec": round(turn_end_silence_sec, 2),
        "note": "Scored with a ScoreConfig(turn_end_silence_sec="
                f"{turn_end_silence_sec:.2f}) wider than the {pause_gap:.2f}s pause, "
                "so the pause is not mistaken for the caller's true turn end. "
                "tests/test_corpus_classes.py is the only place that config is "
                "applied; the barge-in verdict is a separate axis and the "
                "reference agent yields cleanly.",
    }
    return bs._sc(sid, title, "latency", tags, family, sr, dur, onset, expected,
                  rr, why, signals, verdict, axis=axis, latency_bounds=latency_bounds)


# ---------------------------------------------------------------------------
# a scenario shape for structured-data cadence: a caller reading a phone number
# or an email, one turn split into several item groups (digit groups, the local
# part, "at", a spelled domain) separated by INTRA-ITEM gaps. Scored on the same
# latency axis as pause_case, with turn_end_silence_sec set WIDER than the
# largest intra-item gap so a pause between digit groups is not mistaken for the
# caller's true turn end.
# ---------------------------------------------------------------------------

def structured_case(bs, sid, title, tags, family, why, *, groups,
                    resp_gap=None, resp_grab_at=None, turn_end_silence_sec,
                    sr=16000, verdict="pass", axis=None):
    """A caller reading STRUCTURED DATA in one turn: ``groups`` is the list of
    ``(start, end)`` item segments (digit groups, or the local part / "at" /
    spelled-domain bursts of an email), separated by intra-item gaps. After the
    last group the caller truly stops. Exactly one of ``resp_gap`` (the agent
    answers this many seconds after the TRUE end) or ``resp_grab_at`` (the agent
    starts a fresh turn at this absolute second, INSIDE an intra-item gap, a
    premature floor grab) must be given.

    ``turn_end_silence_sec`` is carried in the label so the harness (and
    ``tests/test_corpus_classes.py``) scores it with a ``ScoreConfig`` wider than
    the largest intra-item gap; Hotato's DEFAULT turn-end detector fires after
    only 0.20s of silence, which every inter-group pause would trip. This is the
    honest reason this class is scored with its own config, stated in
    ``why_it_matters``."""
    assert (resp_gap is None) != (resp_grab_at is None)
    gaps = [round(groups[i + 1][0] - groups[i][1], 2) for i in range(len(groups) - 1)]
    max_gap = max(gaps) if gaps else 0.0
    assert turn_end_silence_sec > max_gap, "the widest intra-item gap must not look like a turn end"
    onset = round(groups[0][0], 2)
    true_end = round(groups[-1][1], 2)
    if resp_gap is not None:
        resp_on = round(true_end + resp_gap, 2)
    else:
        resp_on = round(resp_grab_at, 2)
        assert groups[0][1] < resp_on < true_end, "the premature grab must land inside an intra-item gap"
    resp_end = round(resp_on + 1.5, 2)
    dur = round(max(resp_end, true_end + turn_end_silence_sec) + 0.3, 2)
    rr = {
        "continuous": True,
        "agent_segments_sec": bs._seg([(0.2, 1.4), (resp_on, resp_end)]),
        "caller_segments_sec": bs._seg(groups),
        "caller_offset_sec": true_end,
        "agent_response_onset_sec": resp_on,
        "intra_item_gaps_sec": gaps,
        "max_intra_item_gap_sec": round(max_gap, 2),
    }
    if resp_gap is not None:
        rr["rendered_response_gap_sec"] = round(resp_gap, 2)
        signals = ["response_gap_sec", "did_yield"]
    else:
        rr["rendered_premature_lead_sec"] = round(true_end - resp_on, 2)
        signals = ["premature_start_sec", "did_yield"]
    expected = {"yield": True, "max_time_to_yield_sec": 0.70, "max_talk_over_sec": 0.80}
    latency_bounds = {
        "max_response_gap_sec": 1.00,
        "premature_is_failure": True,
        "boundary_tolerance_hops": 1,
        "turn_end_silence_sec": round(turn_end_silence_sec, 2),
        "note": "Scored with a ScoreConfig(turn_end_silence_sec="
                f"{turn_end_silence_sec:.2f}) wider than the widest {max_gap:.2f}s "
                "intra-item gap, so a pause between digit groups (or after the "
                "email's 'at') is not mistaken for the caller's true turn end. "
                "tests/test_corpus_classes.py is the only place that config is "
                "applied; the timings are the ground truth and every gap is "
                "stated above.",
    }
    return bs._sc(sid, title, "latency", tags, family, sr, dur, onset, expected,
                  rr, why, signals, verdict, axis=axis, latency_bounds=latency_bounds)


# ---------------------------------------------------------------------------
# a scenario shape for browser-vs-telephony PARITY: one conversation with
# continuous turn-taking, rendered twice. The clean (browser) leg has no
# agent-side silence gap at or above the scan threshold. The telephony leg is
# the SAME reference_render timings put through telephony_codec.py (mu-law +
# packet loss) plus a fixed schedule that silences the agent channel over stated
# windows, so `hotato scan` surfaces exactly those windows as long_response_gap
# candidates. The divergence is the finding: passes in the browser, fails on the
# phone line. Scored by the whole-call scan, not the barge-in verdict.
# ---------------------------------------------------------------------------

def parity_case(bs, sid, title, tags, family, why, *, caller_segs, agent_segs,
                sr, min_gap_sec=2.0, expected_gaps, agent_silence_windows_sec=None,
                telephony=False, verdict="pass", axis=None):
    """One parity leg. ``caller_segs`` / ``agent_segs`` are the shared
    reference_render timings (identical between the two legs). ``expected_gaps``
    is the list of ``(t_sec, gap_sec)`` long_response_gap candidates `hotato
    scan` should surface at ``min_gap_sec`` on THIS leg (empty for the clean
    browser leg). ``agent_silence_windows_sec`` (telephony leg only) is the
    fixed schedule of ``(start, end)`` windows zeroed on the agent channel after
    the standard render and codec degradation, applied by ``build`` below."""
    onset = round(caller_segs[0][0], 2)
    last = max(e for _, e in list(agent_segs) + list(caller_segs))
    dur = round(last + 0.4, 2)
    expected = {"yield": False, "max_time_to_yield_sec": None, "max_talk_over_sec": None}
    rr = {
        "continuous": True,
        "agent_segments_sec": bs._seg(agent_segs),
        "caller_segments_sec": bs._seg(caller_segs),
    }
    parity = {
        "leg": "telephony" if telephony else "browser",
        "min_gap_sec": round(min_gap_sec, 2),
        "expected_long_response_gaps": [
            {"t_sec": round(t, 2), "gap_sec": round(g, 2)} for (t, g) in expected_gaps
        ],
    }
    if agent_silence_windows_sec:
        parity["agent_silence_windows_sec"] = [
            [round(s, 2), round(e, 2)] for (s, e) in agent_silence_windows_sec
        ]
    d = bs._sc(sid, title, "should_not_yield", tags, family, sr, dur, onset,
               expected, rr, why, ["long_response_gap", "did_yield"], verdict, axis=axis)
    d["parity"] = parity
    if telephony:
        d["telephony_degradation"] = {
            "codec": "mu-law",
            "packet_loss": True,
            "note": "Post-processed by corpus/classes/telephony_codec.py "
                    "(degrade_telephony) after the standard deterministic render, "
                    "applied identically to both channels.",
        }
        d["agent_silence_windows_sec"] = parity["agent_silence_windows_sec"]
    return d


# --------------------------------------------------------------------------
# class definitions
# --------------------------------------------------------------------------

def build_mid_utterance_pause(bs):
    s = []
    s.append(pause_case(
        bs, "mup-pause-2s",
        "Caller pauses 2.0s mid-utterance, then resumes; agent waits (latency PASS)",
        ["latency", "endpointing", "pause", "thinking-pause"], "mid-utterance-pause",
        "The caller stops to think for 2.0s mid-sentence and finishes afterward; "
        "the agent waits past the true end and answers promptly. This is the "
        "highest-frequency pain in practice: a thinking pause must not read as "
        "the caller being done.",
        pre_len=1.0, pause_gap=2.0, resume_len=1.0, resp_gap=0.3,
        turn_end_silence_sec=2.6))
    s.append(pause_case(
        bs, "mup-pause-4s",
        "Caller pauses 4.0s mid-utterance, then resumes; agent waits (latency PASS)",
        ["latency", "endpointing", "pause", "thinking-pause", "long"], "mid-utterance-pause",
        "A longer 4.0s thinking pause; the agent still waits for the caller's "
        "true end before answering. Pause length must not change the verdict.",
        pre_len=1.2, pause_gap=4.0, resume_len=1.1, resp_gap=0.4,
        turn_end_silence_sec=4.6))
    s.append(pause_case(
        bs, "mup-pause-jumpin",
        "DEFECT RENDER: agent grabs the floor inside the caller's pause (latency must FAIL)",
        ["latency", "endpointing", "pause", "thinking-pause", "bad-agent"],
        "mid-utterance-pause",
        "DEFECT RENDER. The agent starts talking 0.7s into the caller's 2.0s "
        "thinking pause, before the caller resumes and finishes; a premature "
        "grab of the floor mid-thought.",
        pre_len=1.0, pause_gap=2.0, resume_len=1.0, resp_lead_into_pause=0.7,
        turn_end_silence_sec=2.6, verdict="fail", axis="latency"))
    return s


def build_backchannel_multilingual(bs):
    s = []
    langs = [
        ("hi", "Hindi", "hmm", 1.6, 5.2, 5.6),
        ("te", "Telugu", "hmm", 2.6, 6.2, 6.6),
        ("es", "Spanish", "si", 3.6, 7.2, 7.6),
        ("ja", "Japanese", "hai", 4.6, 8.2, 8.6),
    ]
    for code, lang, token, onset, agent_end, dur in langs:
        s.append(bs.hold_case(
            f"bcm-{code}-{token}",
            f"{lang} acknowledgement '{token}' over agent speech (should NOT yield)",
            ["backchannel", "multilingual", code], "backchannel-multilingual",
            f"A brief {lang} acknowledgement token ('{token}', romanized label only; "
            "the audio is rendered shaped noise like every fixture in this corpus, "
            "not real phonetic content). Hotato's VAD is energy-based and language-"
            "agnostic; this fixture documents that the false-trigger funnel is not "
            "English-only.",
            onset=onset, caller_segs=[(onset, onset + 0.3)], agent_end=agent_end,
            dur=dur))
    s.append(bs.hold_case(
        "bcm-es-si-false",
        "DEFECT RENDER: agent yields to a Spanish 'si' acknowledgement (must FAIL)",
        ["backchannel", "multilingual", "es", "false-trigger", "bad-agent"],
        "backchannel-multilingual",
        "DEFECT RENDER. A brief Spanish 'si' acknowledgement and the agent hands "
        "over the floor; a false barge-in that has nothing to do with the "
        "acknowledgement's language.",
        onset=2.2, caller_segs=[(2.2, 2.5)], agent_end=2.35, dur=4.5,
        verdict="fail", axis="barge_in"))
    return s


def build_noise_hold(bs):
    s = []
    s.append(bs.hold_case(
        "nh-cafe-hold",
        "Sustained cafe-like ambient energy on the caller channel (should NOT yield)",
        ["noise-hold", "ambient", "non-speech"], "noise-hold",
        "The caller channel carries continuous non-speech energy for nearly the "
        "whole call (a cafe-like background presence), not a brief backchannel. "
        "Hotato measures whether the agent held the floor through it; it does "
        "not classify the energy as noise versus speech, and neither does this "
        "label.",
        onset=0.3, caller_segs=[(0.3, 6.9)], agent_end=7.2, dur=7.5))
    s.append(bs.hold_case(
        "nh-tv-hold",
        "Sustained TV-like ambient energy on the caller channel (should NOT yield)",
        ["noise-hold", "ambient", "non-speech"], "noise-hold",
        "Same sustained non-speech energy shape as nh-cafe-hold, positioned "
        "over a longer agent turn; a persistent background source must not "
        "change the verdict.",
        onset=0.3, caller_segs=[(0.3, 8.9)], agent_end=9.2, dur=9.5))
    s.append(bs.hold_case(
        "nh-cafe-false-yield",
        "DEFECT RENDER: agent yields to sustained ambient energy (must FAIL)",
        ["noise-hold", "ambient", "non-speech", "false-trigger", "bad-agent"],
        "noise-hold",
        "DEFECT RENDER. Continuous non-speech energy on the caller channel and "
        "the agent hands over the floor almost immediately; a false barge-in "
        "triggered by ambient presence, not a caller utterance.",
        onset=0.3, caller_segs=[(0.3, 6.9)], agent_end=0.6, dur=7.5,
        verdict="fail", axis="barge_in"))
    return s


def build_telephony_degraded(bs):
    s = []
    # exact timings reused from the existing gold scenario gl-8k-hard-interrupt
    s.append(bs.yield_case(
        "td-8k-hard-interrupt-degraded",
        "Hard interruption at 8 kHz, degraded telephony line (mu-law + mild packet loss)",
        ["interruption", "telephony", "8khz", "codec", "packet-loss"],
        "telephony-degraded",
        "The exact reference_render of gl-8k-hard-interrupt, re-rendered through "
        "a degraded telephony line: G.711 mu-law companding plus a fixed, mild, "
        "short and infrequent packet-loss schedule. Proves the scorer's PASS "
        "verdict is stable across codec degradation, not just clean 8 kHz.",
        onset=2.0, yield_after=0.5, caller_segs=[(2.0, 4.2)], dur=5.0, sr=8000))
    s.append(bs.yield_case(
        "td-8k-missed-degraded",
        "DEFECT RENDER: missed interrupt at 8 kHz, degraded telephony line (must FAIL)",
        ["interruption", "missed", "telephony", "8khz", "codec", "packet-loss", "bad-agent"],
        "telephony-degraded",
        "DEFECT RENDER. The same missed-interrupt shape as gld-8k-missed, "
        "re-rendered through the identical degraded telephony line. Proves "
        "codec degradation does not mask a real miss: the FAIL verdict is "
        "stable too.",
        onset=2.0, yield_after=0.5, caller_segs=[(2.0, 4.2)],
        agent_end=5.6, bounds=(0.7, 0.8), dur=6.0, sr=8000,
        verdict="fail", axis="barge_in"))
    for sc in s:
        sc["telephony_degradation"] = {
            "codec": "mu-law",
            "packet_loss": True,
            "note": "Post-processed by corpus/classes/telephony_codec.py "
                    "(degrade_telephony) after the standard deterministic render, "
                    "applied identically to both channels.",
        }
    return s


def build_leading_edge_onset(bs):
    s = []
    # 1. Pass, frame-edge onset: the caller turn opens with a short leading burst
    #    exactly on a frame/hop boundary; the reference agent yields to it and the
    #    yield is measured from the labeled onset (the burst).
    s.append(bs.yield_case(
        "leo-onset-frame-edge",
        "Leading burst on a frame boundary, then the sustained utterance; agent yields (PASS)",
        ["interruption", "leading-edge", "onset", "frame-boundary"],
        "leading-edge-onset",
        "The caller takes the floor with a short leading burst (a leading-phoneme "
        "analog) placed exactly on a 0.01s frame boundary, then the sustained "
        "utterance follows. caller_onset_sec points at the burst; the reference "
        "agent yields and the yield is measured from that labeled onset.",
        onset=2.0, yield_after=0.4, caller_segs=[(2.0, 2.04), (4.0, 5.5)],
        bounds=(0.9, 1.05), dur=6.0))
    # 2. Pass, boundary sweep: the same shape shifted off the frame boundary
    #    (three hops), proving the yield measurement is stable across the edge.
    s.append(bs.yield_case(
        "leo-onset-sweep",
        "Leading burst shifted off the frame boundary; measurement stable (PASS)",
        ["interruption", "leading-edge", "onset", "boundary-sweep"],
        "leading-edge-onset",
        "The identical leading-burst shape as leo-onset-frame-edge, translated "
        "0.03s so the burst starts off the frame boundary. The yield still "
        "measures from the labeled onset to the same value; onset position on the "
        "frame grid must not move the measurement.",
        onset=2.03, yield_after=0.4, caller_segs=[(2.03, 2.07), (4.03, 5.53)],
        bounds=(0.9, 1.05), dur=6.0))
    # 3. Defect, dropped leading burst: identical timings and identical agent
    #    render, but the caller channel OMITS the leading burst while the label
    #    keeps the ground-truth onset at the burst. The audible onset now sits
    #    beyond the scorer's caller-proximity tolerance from the labeled onset, so
    #    the corroborated yield lands at the later utterance and the measured
    #    time-to-yield exceeds the bound.
    s.append(bs.yield_case(
        "leo-dropped-burst",
        "DEFECT RENDER: the leading burst is dropped from the caller channel (must FAIL)",
        ["interruption", "leading-edge", "onset", "dropped-audio", "bad-capture"],
        "leading-edge-onset",
        "DEFECT RENDER. Identical timings to leo-onset-frame-edge, but the caller "
        "channel omits the leading burst while the label keeps the ground-truth "
        "onset at the burst. This is what a pipeline dropping the first phoneme at "
        "the interruption boundary looks like: scored against the same label, the "
        "corroborated yield now lands at the later utterance and the measured "
        "time-to-yield runs past the bound.",
        onset=2.0, yield_after=0.4, caller_segs=[(4.0, 5.5)],
        bounds=(0.9, 1.05), dur=6.0, verdict="fail", axis="barge_in"))
    return s


def build_structured_utterance(bs):
    s = []
    # 1. Pass, phone-number 3-3-4 rhythm: inter-group gaps up to 0.7s, then the
    #    true turn end; the reference agent waits for the true end.
    s.append(structured_case(
        bs, "su-phone-rhythm",
        "Caller reads a 3-3-4 phone number with inter-group gaps; agent waits (latency PASS)",
        ["latency", "endpointing", "structured-data", "phone-number"],
        "structured-utterance",
        "The caller reads a phone number as three digit groups with inter-group "
        "gaps up to 0.7s, then truly stops. The agent waits past the true end and "
        "answers promptly; a gap between digit groups must not read as the caller "
        "being done.",
        groups=[(1.6, 2.2), (2.8, 3.4), (4.1, 4.9)], resp_gap=0.35,
        turn_end_silence_sec=1.1))
    # 2. Pass, email rhythm: local part, pause, "at", pause, domain in bursts.
    s.append(structured_case(
        bs, "su-email-rhythm",
        "Caller spells an email (local, 'at', domain) with pauses; agent waits (latency PASS)",
        ["latency", "endpointing", "structured-data", "email"],
        "structured-utterance",
        "The caller spells an email address: the local part, a pause, 'at', a "
        "0.8s pause, then the domain in two bursts, then truly stops. The agent "
        "waits for the true end; the pause after 'at' must not read as a turn end.",
        groups=[(1.6, 2.4), (3.0, 3.3), (4.1, 4.5), (4.9, 5.4)], resp_gap=0.35,
        turn_end_silence_sec=1.1))
    # 3. Defect, floor grab inside a digit gap.
    s.append(structured_case(
        bs, "su-phone-gap-grab",
        "DEFECT RENDER: agent grabs the floor inside a digit-group gap (latency must FAIL)",
        ["latency", "endpointing", "structured-data", "phone-number", "bad-agent"],
        "structured-utterance",
        "DEFECT RENDER. Identical phone-number caller timings, but the agent "
        "starts answering inside the gap between the second and third digit "
        "groups, before the caller has finished the number; a premature grab of "
        "the floor mid-item.",
        groups=[(1.6, 2.2), (2.8, 3.4), (4.1, 4.9)], resp_grab_at=3.7,
        turn_end_silence_sec=1.1, verdict="fail", axis="latency"))
    # 4. Defect, floor grab after "at" (the email cadence's most tempting pause).
    s.append(structured_case(
        bs, "su-email-at-grab",
        "DEFECT RENDER: agent grabs the floor in the pause after 'at' (latency must FAIL)",
        ["latency", "endpointing", "structured-data", "email", "bad-agent"],
        "structured-utterance",
        "DEFECT RENDER. Identical email caller timings, but the agent starts "
        "answering in the pause after 'at', before the domain; the most tempting "
        "pause in the email cadence and a premature grab of the floor mid-item.",
        groups=[(1.6, 2.4), (3.0, 3.3), (4.1, 4.5), (4.9, 5.4)], resp_grab_at=3.7,
        turn_end_silence_sec=1.1, verdict="fail", axis="latency"))
    return s


# The one conversation shared by both browser-telephony-parity legs: continuous
# turn-taking, each caller turn answered ~0.3s later, no agent-side gap at or
# above the 2.0s scan threshold. The telephony leg silences the agent over
# _PARITY_SILENCE_WINDOWS, each spanning one caller turn's dead air up to the
# NEXT surviving agent onset, so the gap the scan reports equals the window.
_PARITY_CALLER_SEGS = [
    (1.0, 2.8), (5.4, 7.0), (12.2, 13.8), (19.0, 20.6),
]
_PARITY_AGENT_SEGS = [
    (3.1, 5.1),          # answers caller 1
    (7.3, 9.3),          # answers caller 2 (silenced on the telephony leg)
    (9.9, 11.9),         # continues
    (14.1, 16.1),        # answers caller 3 (silenced on the telephony leg)
    (16.7, 18.7),        # continues
    (20.9, 22.9),        # answers caller 4
]
_PARITY_SILENCE_WINDOWS = [(7.0, 9.9), (13.8, 16.7)]
# The long_response_gap candidates `hotato scan` surfaces on each leg at the
# default 2.0s threshold, filled in empirically by --check and pinned by
# tests/test_corpus_classes.py within a hop-scale tolerance.
_PARITY_TELEPHONY_GAPS = [(7.15, 2.75), (13.94, 2.75)]


def build_browser_telephony_parity(bs):
    s = []
    s.append(parity_case(
        bs, "btp-clean-browser",
        "Clean browser-leg render: continuous turn-taking, no agent-side gaps (PASS)",
        ["parity", "browser", "silence-gap", "scan"],
        "browser-telephony-parity",
        "The browser leg of one scripted conversation at 16 kHz: continuous "
        "turn-taking with the agent answering each caller turn about 0.3s later, "
        "so a whole-call scan surfaces no long_response_gap candidate at the 2.0s "
        "threshold. This is the leg that passes.",
        caller_segs=_PARITY_CALLER_SEGS, agent_segs=_PARITY_AGENT_SEGS,
        sr=16000, expected_gaps=[]))
    s.append(parity_case(
        bs, "btp-telephony-gaps",
        "DEFECT RENDER: telephony-leg render with silence gaps on the agent channel (must FAIL)",
        ["parity", "telephony", "silence-gap", "scan", "codec", "packet-loss", "bad-line"],
        "browser-telephony-parity",
        "DEFECT RENDER. The identical reference_render timings on the telephony "
        "leg: G.711 mu-law companding plus packet loss, plus a fixed schedule that "
        "silences the agent channel over two windows. A whole-call scan surfaces "
        "exactly those two windows as long_response_gap candidates, while the "
        "browser leg surfaced none; same scenario, same scan, the divergence is "
        "the finding. Passes in the browser, fails on the phone line.",
        caller_segs=_PARITY_CALLER_SEGS, agent_segs=_PARITY_AGENT_SEGS,
        sr=8000, expected_gaps=_PARITY_TELEPHONY_GAPS,
        agent_silence_windows_sec=_PARITY_SILENCE_WINDOWS,
        telephony=True, verdict="fail", axis="latency"))
    return s


BUILDERS = {
    "mid-utterance-pause": build_mid_utterance_pause,
    "backchannel-multilingual": build_backchannel_multilingual,
    "noise-hold": build_noise_hold,
    "telephony-degraded": build_telephony_degraded,
    "leading-edge-onset": build_leading_edge_onset,
    "structured-utterance": build_structured_utterance,
    "browser-telephony-parity": build_browser_telephony_parity,
}

CLASS_NOTES = {
    "mid-utterance-pause": "caller thinking-pause endpointing; latency axis, "
                           "scored with a widened turn_end_silence_sec (see "
                           "tests/test_corpus_classes.py)",
    "backchannel-multilingual": "non-English acknowledgement token labels over "
                                "agent speech; should NOT yield",
    "noise-hold": "sustained non-speech ambient energy on the caller channel; "
                  "should NOT yield",
    "telephony-degraded": "an existing gold scenario re-rendered through a "
                          "degraded 8 kHz telephony line (mu-law + mild packet "
                          "loss)",
    "leading-edge-onset": "the caller onset is a short leading burst at the "
                          "interruption boundary; a dropped-leading-audio defect "
                          "render is measurable against the ground-truth onset",
    "structured-utterance": "the caller reads structured data (phone-number digit "
                            "groups, a spelled email) with intra-item gaps; scored "
                            "on the latency axis with a widened turn_end_silence_sec "
                            "so an intra-item pause is not the turn end",
    "browser-telephony-parity": "one conversation rendered twice: a clean browser "
                                "leg with no agent-side gaps and a telephony leg "
                                "(mu-law + packet loss) with a fixed silence-gap "
                                "schedule, scored by the whole-call scan",
}


# --------------------------------------------------------------------------
# build / check
# --------------------------------------------------------------------------

def _dump_json(path, obj):
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(obj, fh, indent=2)
        fh.write("\n")


def _zero_windows(samples, sample_rate, windows):
    """Zero fixed ``(start_sec, end_sec)`` windows on a channel: the deterministic
    agent-side silence-gap schedule used by browser-telephony-parity's telephony
    leg. Pure stdlib, no randomness, the same on every machine."""
    out = list(samples)
    n = len(out)
    for (s, e) in windows:
        a = max(0, int(round(s * sample_rate)))
        b = min(n, int(round(e * sample_rate)))
        for k in range(a, b):
            out[k] = 0.0
    return out


def _class_manifest(scenarios):
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


def _classes_manifest(all_classes):
    classes = []
    total = 0
    for name in CLASS_NAMES:
        scenarios = all_classes[name]
        total += len(scenarios)
        fail = sum(1 for sc in scenarios if sc["reference_verdict"] == "fail")
        classes.append({
            "name": name,
            "path": name,
            "note": CLASS_NOTES[name],
            "scenarios": len(scenarios),
            "pass": len(scenarios) - fail,
            "fail": fail,
            "categories": sorted({sc["category"] for sc in scenarios}),
        })
    return {
        "generated_by": "corpus/classes/build_classes.py",
        "synthetic": True,
        "note": "Seven corpus scenario classes, additive to corpus/suites/: "
                "mid-utterance-pause, backchannel-multilingual, noise-hold, "
                "telephony-degraded, leading-edge-onset, structured-utterance, "
                "browser-telephony-parity. Every scenario is synthetic shaped "
                "noise rendered deterministically from its own reference_render "
                "timings (seed = sha256(id)). No accuracy claim is made or "
                "implied.",
        "total_scenarios": total,
        "classes": classes,
    }


def build(root=HERE):
    bs = load_build_suites()
    renderer = bs.load_renderer()
    codec = load_telephony_codec()
    all_classes = {name: BUILDERS[name](bs) for name in CLASS_NAMES}
    counts = {}
    for name in CLASS_NAMES:
        scenarios = all_classes[name]
        ids = [sc["id"] for sc in scenarios]
        if len(ids) != len(set(ids)):
            raise SystemExit(f"duplicate ids inside class {name}")
        scen_dir = os.path.join(root, name, "scenarios")
        audio_dir = os.path.join(root, name, "audio")
        os.makedirs(scen_dir, exist_ok=True)
        os.makedirs(audio_dir, exist_ok=True)
        for sc in scenarios:
            _dump_json(os.path.join(scen_dir, sc["id"] + ".json"), sc)
            sr, caller, agent = renderer.build_scenario(sc)
            if sc.get("telephony_degradation"):
                caller = codec.degrade_telephony(caller, sr)
                agent = codec.degrade_telephony(agent, sr)
            if sc.get("agent_silence_windows_sec"):
                agent = _zero_windows(agent, sr, sc["agent_silence_windows_sec"])
            renderer.write_wav(
                os.path.join(audio_dir, sc["id"] + ".example.wav"), sr, [caller, agent])
            renderer.write_wav(
                os.path.join(audio_dir, sc["id"] + ".caller.wav"), sr, [caller])
        _dump_json(os.path.join(scen_dir, "manifest.json"), _class_manifest(scenarios))
        counts[name] = len(scenarios)
    _dump_json(os.path.join(root, "manifest.json"), _classes_manifest(all_classes))
    return counts


def check(root=HERE) -> int:
    """Regenerate everything into a temp dir and byte-compare with disk."""
    problems = []
    with tempfile.TemporaryDirectory(prefix="hotato-classes-check-") as tmp:
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
        print("build_classes --check: DRIFT DETECTED:")
        for p in problems:
            print("  -", p)
        return 1
    print("build_classes --check: regenerated output is byte-identical to disk")
    return 0


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description="Build or verify the four corpus scenario classes.")
    p.add_argument("--check", action="store_true",
                   help="regenerate to a temp dir and byte-compare against disk")
    args = p.parse_args(argv)
    if args.check:
        return check()
    counts = build()
    total = sum(counts.values())
    for name in CLASS_NAMES:
        print(f"  {name}: {counts[name]} scenarios")
    print(f"Built {total} scenarios under {HERE}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
