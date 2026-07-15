"""R-01 regression: ``hotato run``'s stereo verdict routes through trust's K6
channel gate, and a suspected channel SWAP is a NON-FATAL caveat -- not a refusal.

hotato does ADDRESSEE detection, not speaker-ID, so a channel SWAP cannot be
reliably told from timing: a caller-dominant recording is an ordinary caller-led
yield far more often than a swapped caller/agent mapping. So the ``run`` gate does
NOT refuse a swap-suspect recording; it still SCORES it (normal pass/fail exit)
and attaches a structured, non-fatal ``channel_mapping_caveat`` so the operator
can confirm the mapping. ``--confirm-channels`` suppresses the caveat. (Genuine
cross-channel LEAKAGE -- the correlation-based signal -- still refuses; that is
pinned in tests/test_run_gate_crosstalk_r01b.py.)

Every render is deterministic synthetic PCM (same construction as
tests/test_trust.py), so it needs no audio corpus and no optional extra.
"""

import json
import math
import struct
import wave

from hotato import cli, core
from hotato import trust as trust_mod


def _write_stereo(path, caller_segments, agent_segments, *, duration_sec=6.0,
                  sr=16000, caller_amp=0.35, agent_amp=0.35):
    """Two-channel PCM WAV: caller on channel 0, agent on channel 1. Each channel
    is a pure sine inside its active segments and exact digital silence outside,
    so every render is byte-identical everywhere (mirrors test_trust.py)."""
    n = int(duration_sec * sr)

    def _on(segments, t):
        return any(start <= t < end for start, end in segments)

    frames = bytearray()
    for i in range(n):
        t = i / sr
        c = int(caller_amp * 32767 * math.sin(2 * math.pi * 220.0 * i / sr)) if _on(caller_segments, t) else 0
        a = int(agent_amp * 32767 * math.sin(2 * math.pi * 330.0 * i / sr)) if _on(agent_segments, t) else 0
        frames += struct.pack("<hh", c, a)
    with wave.open(str(path), "wb") as w:
        w.setnchannels(2)
        w.setsampwidth(2)
        w.setframerate(sr)
        w.writeframes(bytes(frames))
    return str(path)


# A legitimate CALLER-DOMINANT recording: the channel mapped as caller holds the
# floor far longer than the agent (a normal caller-led yield where the caller
# simply talks more). This trips the possible-swap HEURISTIC (caller-active >
# 1.5x agent-active, margin >= 1.0s), but two DISTINCT tones do not correlate at
# the sample level, so it is NOT leakage -- exactly the case the old hard-refuse
# regressed on.
def _caller_dominant_fixture(tmp_path):
    return _write_stereo(tmp_path / "caller-dominant.wav",
                         caller_segments=[(0.2, 5.8)],
                         agent_segments=[(1.0, 2.0)])


# A clean, normally-mapped recording: agent dominant, caller a brief interjection.
def _clean_fixture(tmp_path):
    return _write_stereo(tmp_path / "clean.wav",
                        caller_segments=[(3.0, 3.7)],
                        agent_segments=[(0.2, 5.8)])


def test_trust_exposes_separate_swap_caveat_and_leakage_signals(tmp_path):
    """The gate's two inputs are exposed SEPARATELY on the trust report: a
    caller-dominant recording carries a ``channel_mapping_caveat`` (the swap
    heuristic) but ``crosstalk_verdict_refused`` is False (no genuine leakage)."""
    p = _caller_dominant_fixture(tmp_path)
    tr = trust_mod.trust_report(p)
    assert tr["channels"]["possible_swap"] is True
    # A suspected swap is NOT a leakage refusal.
    assert tr["crosstalk_verdict_refused"] is False
    # The non-fatal caveat is present and structured.
    cav = tr["channel_mapping_caveat"]
    assert cav is not None
    assert cav["reason"] == trust_mod.CHANNEL_MAPPING_CAVEAT_REASON
    assert cav["hint"] == trust_mod.CHANNEL_MAPPING_CAVEAT_HINT
    assert cav["detail"] == tr["channels"]["swap_reason"]
    # verdict_eligible stays False for the CONTRACT/CI gate (unchanged): the
    # decoupling lives in the run-gate consumer, not in trust's own field.
    assert tr["verdict_eligible"] is False


def test_confirm_channels_clears_the_trust_caveat(tmp_path):
    """A confirmed mapping suppresses the swap caveat on the trust report."""
    p = _caller_dominant_fixture(tmp_path)
    tr = trust_mod.trust_report(p, channel_map_confirmed=True)
    assert tr["channels"]["possible_swap"] is True
    assert tr["channel_mapping_caveat"] is None
    assert tr["crosstalk_verdict_refused"] is False


def test_caller_dominant_run_scores_with_caveat(tmp_path):
    """TEST 1 (the R-01 blocker fix): a caller-dominant recording SCORES on
    `hotato run` (a real pass/fail, never a not-scorable refusal) and carries a
    non-fatal channel_mapping_caveat -- it is NOT held out as not-scorable."""
    p = _caller_dominant_fixture(tmp_path)

    env = core.run_single(stereo=p, expect="yield", onset_sec=1.2,
                          gate_verdict_eligibility=True)
    event = env["events"][0]
    # Scores: no not-scorable refusal keys, and the process does not exit 2.
    assert event.get("scorable") is not False
    assert "not_scorable_reason" not in event
    assert core.process_exit_code(env) != 2
    # ...but the non-fatal caveat rides along.
    cav = event["channel_mapping_caveat"]
    assert cav["reason"] == trust_mod.CHANNEL_MAPPING_CAVEAT_REASON
    assert cav["hint"] == trust_mod.CHANNEL_MAPPING_CAVEAT_HINT


def test_confirm_channels_scores_without_caveat(tmp_path):
    """TEST 2 (escape hatch): the SAME recording with channel_map_confirmed scores
    with NO caveat -- the operator explicitly vouched for the mapping."""
    p = _caller_dominant_fixture(tmp_path)
    env = core.run_single(stereo=p, expect="yield", onset_sec=1.2,
                          gate_verdict_eligibility=True,
                          channel_map_confirmed=True)
    event = env["events"][0]
    assert event.get("scorable") is not False
    assert "not_scorable_reason" not in event
    assert "channel_mapping_caveat" not in event


def test_clean_stereo_run_is_byte_identical_even_with_gate(tmp_path):
    """TEST 3 (no regression): a clean, normally-mapped recording still returns a
    real scored verdict EVEN WITH the gate enabled -- neither the not-scorable
    refusal keys NOR a caveat are injected on a clean recording (byte-identical)."""
    p = _clean_fixture(tmp_path)
    tr = trust_mod.trust_report(p)
    assert tr["channels"]["possible_swap"] is False
    assert tr["verdict_eligible"] is True
    assert tr["channel_mapping_caveat"] is None

    gated = core.run_single(stereo=p, expect="yield", onset_sec=3.0,
                            gate_verdict_eligibility=True)
    ungated = core.run_single(stereo=p, expect="yield", onset_sec=3.0)
    event = gated["events"][0]
    assert "scorable" not in event
    assert "not_scorable_reason" not in event
    assert "channel_mapping_caveat" not in event
    assert core.process_exit_code(gated) != 2
    # Byte-identical to the pre-gate engine output.
    assert json.dumps(gated, sort_keys=True) == json.dumps(ungated, sort_keys=True)


def test_default_off_preserves_raw_measurement(tmp_path):
    """The gate is OPT-IN: a raw-measurement caller (e.g. `contract` re-scoring,
    which applies its OWN stricter contract-mode gate) calls run_single WITHOUT
    gate_verdict_eligibility, so the caller-dominant fixture returns a raw scored
    verdict with NO caveat -- those callers are unaffected (merge-safe default)."""
    p = _caller_dominant_fixture(tmp_path)
    env = core.run_single(stereo=p, expect="yield", onset_sec=1.2)
    event = env["events"][0]
    assert event.get("scorable") is not False
    assert "not_scorable_reason" not in event
    assert "channel_mapping_caveat" not in event


def test_cli_run_caller_dominant_scores_and_notes_caveat(tmp_path, capsys):
    """TEST 4 (CLI end-to-end): `hotato run --stereo caller-dominant.wav ...` scores
    (exit != 2, no [NOT SCORABLE]) and prints the channel-mapping caveat note."""
    p = _caller_dominant_fixture(tmp_path)
    rc = cli.main(["run", "--stereo", p, "--expect", "yield",
                   "--onset", "1.2", "--format", "text"])
    assert rc != 2
    out = capsys.readouterr().out
    assert "[NOT SCORABLE]" not in out
    assert "process_exit_code=2" not in out
    assert "caveat:" in out
    assert "channel mapping unconfirmed" in out


def test_cli_run_caller_dominant_json_carries_caveat(tmp_path, capsys):
    """TEST 4b (CLI JSON): the scored event carries the structured caveat field,
    and the envelope is NOT a not-scorable refusal."""
    p = _caller_dominant_fixture(tmp_path)
    rc = cli.main(["run", "--stereo", p, "--expect", "yield", "--onset", "1.2",
                   "--format", "json"])
    assert rc != 2
    event = json.loads(capsys.readouterr().out)["events"][0]
    assert event.get("scorable") is not False
    assert event["channel_mapping_caveat"]["reason"] == trust_mod.CHANNEL_MAPPING_CAVEAT_REASON


def test_cli_run_confirm_channels_suppresses_caveat(tmp_path, capsys):
    """TEST 4c (CLI escape hatch): --confirm-channels scores the same recording
    with no caveat note and no caveat field."""
    p = _caller_dominant_fixture(tmp_path)
    rc = cli.main(["run", "--stereo", p, "--expect", "yield", "--onset", "1.2",
                   "--confirm-channels", "--format", "json"])
    assert rc != 2
    event = json.loads(capsys.readouterr().out)["events"][0]
    assert event.get("scorable") is not False
    assert "channel_mapping_caveat" not in event
