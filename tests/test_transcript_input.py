"""``hotato investigate --transcript FILE.json``: score a timestamped,
speaker-labeled transcript through the EXISTING turn-taking scorer, no audio,
so a text/chat agent is scorable.

Pins the four properties that make this path shippable:

  (a) PARITY -- a transcript whose turns reproduce a known audio case's active
      runs yields the same did_yield / seconds_to_yield (within +/-1 hop) and
      the same response_gap_sec as scoring that audio directly; did_yield is
      cross-checked against diarize._timeline_yield over the same timelines.
  (b) HONESTY GATE -- a sequential transcript cannot represent acoustic
      overlap, so talk_over_sec / premature_start_sec are null with a reason and
      the overlap/crosstalk candidate kinds never appear.
  (c) TEXT-AGENT E2E -- a {"segments": [...]} chat transcript with a >2s agent
      reply gap scores (exit 0), surfaces a long_response_gap candidate, and
      leaves behind a valid FILE#N investigate-state ref.
  (d) ROLE MAPPING / VALIDATION -- an unmapped role, a missing start/end, and a
      --transcript + SOURCE/--demo/--stack combination each fail loudly.
"""

import io
import json
import math
from contextlib import redirect_stdout

import pytest

from hotato import cli, fixture
from hotato import diarize as D
from hotato import investigate as I
from hotato import transcript_input as tx
from hotato._engine.score import ScoreConfig, score_channels

SR = 16000

# A single scenario, expressed once as segments and reused three ways: as a
# transcript, as the two boolean timelines, and as a synthetic two-channel audio
# whose active runs reproduce those exact segments.
#   agent holds the floor [0.0, 1.5); caller barges in [1.0, 2.5); the agent
#   yields (quiet 1.5-3.5) then answers [3.5, 4.5).
_PARITY_SEGMENTS = [
    {"role": "agent",  "start": 0.0, "end": 1.5},
    {"role": "caller", "start": 1.0, "end": 2.5},
    {"role": "agent",  "start": 3.5, "end": 4.5},
]


def _hop_sec(cfg):
    return D._hop_samples(SR, cfg) / SR


def _sine_channel(segments, freq, *, total_sec, amp=0.35):
    """A float channel: a `freq` sine inside its active segments, exact silence
    outside -- the same construction test_diarize uses. Its energy-VAD active
    runs reproduce the segment spans."""
    n = int(round(total_sec * SR))

    def on(t):
        return any(s <= t < e for s, e in segments)

    return [amp * math.sin(2 * math.pi * freq * i / SR) if on(i / SR) else 0.0
            for i in range(n)]


# --- (a) parity with a known audio case -------------------------------------

def test_transcript_parity_with_equivalent_audio():
    cfg = ScoreConfig()
    hop = _hop_sec(cfg)

    timelines = tx.transcript_to_timelines(_PARITY_SEGMENTS, hop, cfg)
    caller_tl = timelines[D.SPEAKER_A]
    agent_tl = timelines[D.SPEAKER_B]

    transcript_score = tx.score_transcript_timelines(timelines, sample_rate=SR, cfg=cfg)

    # The "known audio case": the identical active runs as real two-channel
    # audio (caller 220 Hz, agent 330 Hz), scored by the SAME engine.
    caller_ch = _sine_channel([(1.0, 2.5)], 220.0, total_sec=4.6)
    agent_ch = _sine_channel([(0.0, 1.5), (3.5, 4.5)], 330.0, total_sec=4.6)
    audio_score = score_channels(caller_ch, agent_ch, SR, caller_onset_sec=None, cfg=cfg)

    # did_yield is identical, and cross-checked against the diarizer's own
    # timeline-yield decision (no re-VAD) over the same two timelines.
    assert transcript_score.did_yield == audio_score.did_yield
    timeline_yield = D._timeline_yield(caller_tl, agent_tl, hop, cfg)
    assert timeline_yield["did_yield"] == transcript_score.did_yield
    assert transcript_score.did_yield is True

    # seconds_to_yield within +/-1 hop of the audio case (the re-VAD of the
    # synthesized carrier can land a frame off at a run boundary; no more).
    assert transcript_score.time_to_yield_sec is not None
    assert audio_score.time_to_yield_sec is not None
    assert abs(transcript_score.time_to_yield_sec
               - audio_score.time_to_yield_sec) <= hop + 1e-9

    # response_gap_sec is the same pure-timing measurement on both paths.
    assert (transcript_score.signals["latency"]["response_gap_sec"]
            == audio_score.signals["latency"]["response_gap_sec"])
    assert transcript_score.signals["latency"]["response_gap_sec"] is not None


# --- (b) the mandatory honesty gate -----------------------------------------

def test_honesty_gate_nulls_overlap_signals_with_reasons(tmp_path):
    doc = {"segments": _PARITY_SEGMENTS}
    p = tmp_path / "call.json"
    p.write_text(json.dumps(doc))
    result, code = I.run_investigate_transcript(
        str(p), state_path=str(tmp_path / "state.json"))
    assert code == 0

    event = result["event"]
    # talk_over is null everywhere it is surfaced, with the stated reason.
    assert event["verdict"]["talk_over_sec"] is None
    assert event["signals"]["barge_in"]["talk_over_sec"] is None
    assert (event["signals"]["barge_in"]["talk_over_reason"]
            == tx.TRANSCRIPT_OVERLAP_REASON)
    # premature_start is null, with the stated reason.
    assert event["signals"]["latency"]["premature_start_sec"] is None
    assert (event["signals"]["latency"]["premature_start_reason"]
            == tx.TRANSCRIPT_OVERLAP_REASON)
    # cross-channel echo is definitionally N/A on this path.
    assert event["signals"]["echo"]["applicable"] is False
    assert event["signals"]["echo"]["echo_suspected"] is False

    # The timestamp-derivable signals are left intact (not nulled).
    assert "did_yield" in event["verdict"]
    assert "response_gap_sec" in event["signals"]["latency"]

    # The overlap/crosstalk candidate kinds are never emitted.
    suppressed = {"overlap_while_agent_talking", "agent_start_during_caller",
                  "echo_correlated_activity"}
    kinds = {c["kind"] for c in result["candidates"]}
    assert not (kinds & suppressed)


def test_apply_transcript_honesty_gate_is_in_place():
    cfg = ScoreConfig()
    hop = _hop_sec(cfg)
    timelines = tx.transcript_to_timelines(_PARITY_SEGMENTS, hop, cfg)
    score = tx.score_transcript_timelines(timelines, sample_rate=SR, cfg=cfg)
    from hotato import core
    event = core._event_from_result(
        event_id="t", result=score, expected={}, stack=None, onset_provided=False)
    # Before the gate the engine reports a concrete overlap number.
    assert event["signals"]["barge_in"]["talk_over_sec"] is not None
    returned = tx.apply_transcript_honesty_gate(event)
    assert returned is event  # mutates in place, returns the same object
    assert event["signals"]["barge_in"]["talk_over_sec"] is None


# --- (c) text-agent end to end through the CLI ------------------------------

def test_text_agent_e2e_long_response_gap(tmp_path):
    # A chat transcript in the {"segments": [...]} shape with a >2s reply gap.
    doc = {"segments": [
        {"role": "user",      "start": 0.0, "end": 1.0},
        {"role": "assistant", "start": 4.0, "end": 5.0},
    ]}
    p = tmp_path / "chat.json"
    p.write_text(json.dumps(doc))
    state = tmp_path / ".hotato" / "state.json"

    buf = io.StringIO()
    with redirect_stdout(buf):
        code = cli.main(["investigate", "--transcript", str(p),
                         "--state", str(state), "--format", "json"])
    assert code == 0

    result = json.loads(buf.getvalue())
    kinds = [c["kind"] for c in result["candidates"]]
    assert "long_response_gap" in kinds
    gap = next(c for c in result["candidates"] if c["kind"] == "long_response_gap")
    assert gap["durations"]["gap_sec"] == pytest.approx(3.0, abs=0.05)

    # The state file is a valid FILE#N candidate ref: it parses, and it is the
    # kind "analyze" envelope with a candidates list that the ref resolver reads.
    assert state.exists()
    st = json.loads(state.read_text())
    assert st["schema"] == I.STATE_SCHEMA_ID
    assert st["kind"] == "analyze"
    assert isinstance(st["candidates"], list) and st["candidates"]
    ref = f"{state}#1"
    path, call, num = fixture.parse_candidate_ref(ref)
    assert call is None and num == 1
    # fixture._load_result accepts the state file as an analyze result envelope.
    assert fixture._load_result(str(state))["kind"] == "analyze"


# --- (d) role mapping and input validation ----------------------------------

def test_unmapped_role_raises_naming_it(tmp_path):
    p = tmp_path / "t.json"
    p.write_text(json.dumps([{"role": "narrator", "start": 0.0, "end": 1.0}]))
    with pytest.raises(ValueError, match="narrator"):
        I.run_investigate_transcript(str(p), state_path=str(tmp_path / "s.json"))


def test_custom_role_override_maps_the_channel(tmp_path):
    p = tmp_path / "t.json"
    p.write_text(json.dumps([
        {"role": "shopper", "start": 0.0, "end": 1.0},
        {"role": "rep",     "start": 4.0, "end": 5.0},
    ]))
    result, code = I.run_investigate_transcript(
        str(p), state_path=str(tmp_path / "s.json"),
        caller_role="shopper", agent_role="rep")
    assert code == 0
    # shopper->caller finishes, rep->agent replies late: a long_response_gap.
    assert "long_response_gap" in {c["kind"] for c in result["candidates"]}


def test_missing_start_or_end_raises(tmp_path):
    p = tmp_path / "t.json"
    p.write_text(json.dumps([{"role": "user", "start": 0.0}]))
    with pytest.raises(ValueError, match="start.*end|numeric"):
        I.run_investigate_transcript(str(p), state_path=str(tmp_path / "s.json"))


def test_end_not_after_start_raises(tmp_path):
    p = tmp_path / "t.json"
    p.write_text(json.dumps([{"role": "user", "start": 2.0, "end": 2.0}]))
    with pytest.raises(ValueError, match="end > start"):
        I.run_investigate_transcript(str(p), state_path=str(tmp_path / "s.json"))


@pytest.mark.parametrize("extra", [
    ["source.wav"],
    ["--demo"],
    ["--stack", "vapi", "--call-id", "abc"],
])
def test_transcript_is_mutually_exclusive_with_audio_inputs(tmp_path, extra):
    p = tmp_path / "chat.json"
    p.write_text(json.dumps({"segments": [
        {"role": "user", "start": 0.0, "end": 1.0}]}))
    buf = io.StringIO()
    with redirect_stdout(buf):
        code = cli.main(["investigate", "--transcript", str(p),
                         "--state", str(tmp_path / "s.json")] + extra)
    assert code == 2
