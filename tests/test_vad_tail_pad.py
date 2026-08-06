"""The VAD tail pad fabricated findings. These pin that it cannot come back.

The energy VAD keeps a channel marked active for ``hangover_sec`` after its
energy drops. That pad does two jobs and only one of them was wanted: BETWEEN
two frames of energy it bridges the gap between words, but AFTER the last frame
of energy it reports silence as speech. Downstream, that invented tail is read
as the channel still holding the floor, so:

  * it overlaps the other channel's real onset and reports talk-over that did
    not happen -- exactly ``hangover_sec - gap`` seconds of it;
  * it moves every run's END late by ``hangover_sec``, which pushes a caller's
    turn end late (understating ``response_gap`` by that same amount, and
    fabricating ``premature_start`` when turn-taking is fast).

``VADParams.trim_tail_to_raw`` pulls each run back to its last frame of measured
energy, leaving the bridging intact -- a bridged gap has energy on both sides of
it by definition, so trimming a run's tail cannot re-fragment an utterance.

The recalibration that ships with it is the other half. ``yield_hangover_sec``
and ``turn_end_silence_sec`` count QUIET frames on this track. With the pad on,
the first ``hangover_sec`` of every silence was eaten, so a threshold written as
0.20 was really a demand for 0.35s of quiet in the audio. Trimming without
raising them would silently loosen that to a literal 0.20s, and a mid-phrase
breath would start scoring as a yield the agent never made. The last test here
is the one that fails if anyone ever "simplifies" 0.35 back to 0.20.
"""

import math

import pytest

from hotato._engine.score import ScoreConfig, score_channels
from hotato._engine.vad import VADParams

SR = 16000
HOP = 0.01
HANGOVER = 0.15


def _tone(n, seg, freq=220.0, amp=0.3, sr=SR):
    out = [0.0] * n
    a, b = int(seg[0] * sr), int(seg[1] * sr)
    for i in range(a, min(b, n)):
        out[i] = amp * math.sin(2 * math.pi * freq * i / sr)
    return out


def _legacy_cfg():
    """Scoring exactly as it behaved before the trim: pad on, thresholds literal."""
    return ScoreConfig(
        yield_hangover_sec=0.20,
        turn_end_silence_sec=0.20,
        caller_vad=VADParams(trim_tail_to_raw=False),
        agent_vad=VADParams(trim_tail_to_raw=False),
    )


# --------------------------------------------------------------------------- #
# 1. The phantom: no overlap in the audio, overlap in the report.
# --------------------------------------------------------------------------- #

@pytest.mark.parametrize("gap", [0.05, 0.10, 0.20, 0.30])
def test_sequential_speech_reports_no_overlap(gap):
    """Agent stops, caller starts ``gap`` later. They never speak at once."""
    dur = 4.0
    n = int(dur * SR)
    agent = _tone(n, (0.0, 1.0), 330.0)
    caller = _tone(n, (1.0 + gap, 2.5), 220.0)

    result = score_channels(caller, agent, SR)

    assert result.talk_over_sec == 0.0, (
        f"reported {result.talk_over_sec}s of talk-over on audio with none; "
        f"the VAD tail pad is leaking into the overlap count again"
    )


@pytest.mark.parametrize("gap", [0.05, 0.10])
def test_the_phantom_overlap_was_the_pad_minus_the_gap(gap):
    """Pin the OLD behaviour too, so the mechanism stays legible.

    The fabricated overlap was never noise -- it was exactly the part of the pad
    the caller's onset ran into. Keeping this here means a future reader can see
    what was wrong without having to reconstruct it.
    """
    dur = 4.0
    n = int(dur * SR)
    agent = _tone(n, (0.0, 1.0), 330.0)
    caller = _tone(n, (1.0 + gap, 2.5), 220.0)

    legacy = score_channels(caller, agent, SR, cfg=_legacy_cfg())

    assert legacy.talk_over_sec == pytest.approx(HANGOVER - gap, abs=0.02)


# --------------------------------------------------------------------------- #
# 2. The other side of it: a real overlap must survive.
#    A fix that "works" by reporting less is worse than the bug.
# --------------------------------------------------------------------------- #

def test_a_genuine_overlap_is_still_measured():
    """The agent keeps talking 0.4s into the caller's turn. That is real."""
    dur = 4.0
    n = int(dur * SR)
    caller = _tone(n, (1.0, 2.5), 220.0)
    agent = _tone(n, (0.0, 1.4), 330.0)

    result = score_channels(caller, agent, SR)

    assert result.talk_over_sec == pytest.approx(0.4, abs=0.05), (
        f"a real 0.4s overlap measured as {result.talk_over_sec}s; the trim is "
        f"suppressing true positives, not just phantoms"
    )


def test_a_genuine_barge_in_still_scores_a_yield():
    """Agent talking, caller cuts in, agent stops and stays stopped."""
    dur = 4.0
    n = int(dur * SR)
    caller = _tone(n, (1.0, 2.5), 220.0)
    agent = _tone(n, (0.0, 1.3), 330.0)

    result = score_channels(caller, agent, SR)

    assert result.agent_talking_at_onset is True
    assert result.did_yield is True


# --------------------------------------------------------------------------- #
# 3. The turn-end timing correction.
# --------------------------------------------------------------------------- #

def test_response_gap_is_not_understated_by_one_hangover():
    """Caller stops at 2.0, agent answers at 2.6. The gap is 0.6s.

    With the pad on, the caller's turn end was detected at 2.15 and the gap came
    back 0.15s short.
    """
    dur = 5.0
    n = int(dur * SR)
    caller = _tone(n, (1.0, 2.0), 220.0)
    agent = _tone(n, (2.6, 3.6), 330.0)

    result = score_channels(caller, agent, SR, caller_onset_sec=1.0)
    gap = result.signals["latency"]["response_gap_sec"]

    assert gap is not None
    assert gap == pytest.approx(0.6, abs=0.05), (
        f"response_gap came back {gap}s for a 0.6s gap in the audio"
    )

    legacy = score_channels(caller, agent, SR, caller_onset_sec=1.0, cfg=_legacy_cfg())
    legacy_gap = legacy.signals["latency"]["response_gap_sec"]
    assert legacy_gap == pytest.approx(0.6 - HANGOVER, abs=0.05), (
        "the pre-trim understatement is the thing being fixed; if this stops "
        "holding, the mechanism above is no longer what is happening"
    )


# --------------------------------------------------------------------------- #
# 4. The recalibration. This is the test that fails if 0.35 is "simplified".
# --------------------------------------------------------------------------- #

@pytest.mark.parametrize("breath", [0.25, 0.31, 0.34])
def test_a_mid_phrase_breath_is_not_a_yield(breath):
    """The agent pauses for breath and carries straight on. It never yielded.

    These three lengths are the trap: each is longer than a literal 0.20s
    threshold and shorter than the 0.35s of real quiet that was always being
    required. Scoring any of them as a yield means the agent gets credit for
    stopping when it did not stop -- on a clip whose whole point is that a
    backchannel is not a barge-in.
    """
    dur = 4.0
    n = int(dur * SR)
    agent = _tone(n, (0.0, 1.0), 330.0) + [0.0] * 0
    tail = _tone(n, (1.0 + breath, 2.0 + breath), 330.0)
    agent = [a + b for a, b in zip(agent, tail)]
    caller = _tone(n, (1.02, 1.14), 220.0)

    result = score_channels(caller, agent, SR, caller_onset_sec=1.02)

    assert result.did_yield is False, (
        f"a {breath}s breath scored as a yield; yield_hangover_sec has been "
        f"lowered without accounting for the tail pad it was calibrated against"
    )


def test_a_real_stop_still_scores_a_yield():
    """The counterweight to the test above: 0.5s of quiet IS a yield."""
    dur = 4.0
    n = int(dur * SR)
    agent = _tone(n, (0.0, 1.0), 330.0)
    tail = _tone(n, (1.5, 2.5), 330.0)
    agent = [a + b for a, b in zip(agent, tail)]
    caller = _tone(n, (1.02, 1.14), 220.0)

    result = score_channels(caller, agent, SR, caller_onset_sec=1.02)

    assert result.did_yield is True


# --------------------------------------------------------------------------- #
# 5. The escape hatch has to actually work.
# --------------------------------------------------------------------------- #

def test_trim_can_be_turned_off_to_reproduce_older_numbers():
    """Someone with a pinned pre-1.20.0 manifest needs the old track back."""
    dur = 4.0
    n = int(dur * SR)
    agent = _tone(n, (0.0, 1.0), 330.0)
    caller = _tone(n, (1.05, 2.5), 220.0)

    trimmed = score_channels(caller, agent, SR)
    legacy = score_channels(caller, agent, SR, cfg=_legacy_cfg())

    assert trimmed.talk_over_sec == 0.0
    assert legacy.talk_over_sec > 0.0
    assert legacy.talk_over_sec != trimmed.talk_over_sec


def test_bridging_survives_the_trim():
    """Two words 0.05s apart are still ONE run, not two.

    The pad's wanted job is bridging. If the trim broke it, a single utterance
    would fragment and every count downstream would change.
    """
    dur = 3.0
    n = int(dur * SR)
    word_one = _tone(n, (0.5, 0.9), 220.0)
    word_two = _tone(n, (0.95, 1.4), 220.0)
    caller = [a + b for a, b in zip(word_one, word_two)]

    from hotato._engine.audio import frame_rms
    from hotato._engine.vad import energy_vad

    cfg = ScoreConfig()
    rms, hop = frame_rms(caller, SR, cfg.frame_ms, cfg.hop_ms)
    res = energy_vad(rms, hop)

    runs = 0
    prev = False
    for a in res.active:
        if a and not prev:
            runs += 1
        prev = a

    assert runs == 1, f"the utterance fragmented into {runs} runs; bridging broke"


# --------------------------------------------------------------------------- #
# 6. The yield threshold is a property of the audio, not of the onset label.
# --------------------------------------------------------------------------- #

@pytest.mark.parametrize("onset_offset", [-0.10, -0.05, 0.0, 0.05, 0.10])
def test_yield_decision_does_not_depend_on_where_the_onset_lands(onset_offset):
    """The same recording, labelled a few frames either side, scores the same.

    The quiet run is measured from where it actually began, not from wherever
    the search happened to enter it. Without that, an agent that went quiet just
    before the caller's onset would have to stay quiet longer than one that went
    quiet just after -- the same audio, decided by the label. The tail pad hid
    this by keeping the agent "active" past the onset, so the search rarely
    entered a run part-way through.
    """
    dur = 5.0
    n = int(dur * SR)
    # Agent stops at 2.00 and stays down 0.45s -- comfortably a yield either way.
    agent = [a + b for a, b in zip(_tone(n, (0.0, 2.0), 330.0),
                                   _tone(n, (2.45, 4.0), 330.0))]
    caller = _tone(n, (1.95, 3.0), 220.0)

    result = score_channels(caller, agent, SR, caller_onset_sec=2.0 + onset_offset)

    assert result.did_yield is True, (
        f"labelling the onset {onset_offset:+.2f}s away changed the answer; the "
        f"quiet run is being measured from the label instead of from its start"
    )


def test_the_quiet_run_is_measured_from_its_own_start():
    """Directly: a run entered part-way through still counts its whole length."""
    from hotato._engine.score import _quiet_run_start

    #        0  1  2  3  4  5  6  7
    active = [True, False, False, False, False, True, True, False]
    # entering the quiet stretch at 3 still reports it starting at 1
    assert _quiet_run_start(active, 3) == 1
    assert _quiet_run_start(active, 1) == 1
    assert _quiet_run_start(active, 7) == 7
