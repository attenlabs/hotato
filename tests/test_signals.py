"""M1 signal-bus tests: the namespaced ``signals`` dict, the pure-timing latency
dimension, the frame-dump round-trip, and back-compat of the three originals.

Zero third-party deps; runs exactly as a fresh install would. The latency tests
reuse the ``make_channel`` pattern from ``openrepo/tests/test_scoring.py`` on
synthetic channels with KNOWN segment timings, and neutralise the VAD hangover
so the active-track boundaries equal the rendered boundaries -- that isolates the
timing math (turn-end detection + onset detection) from VAD smoothing, so the
measurement can be checked to within one frame hop of what was rendered.
"""

import json
import random
import tempfile
import os

import pytest

from hotato.core import dump_frames_for_input, run_suite
from hotato import cli
from hotato._engine.score import ScoreConfig, score_channels, score_stereo
from hotato._engine.vad import VADParams


SR = 16000

# The three originals for the 8 bundled scenarios, captured BEFORE the signal bus
# was added. The suite must still reproduce these byte-for-byte (back-compat).
GOLDEN_BARGE_IN = {
    "01-hard-interruption": (True, 0.5, 0.5),
    "02-backchannel-mhm": (False, None, 1.57),
    "03-filler-start": (True, 0.65, 0.56),
    "04-correction": (True, 0.5, 0.5),
    "05-telephony-8khz": (True, 0.5, 0.5),
    "06-double-talk": (True, 1.05, 1.05),
    "07-echo-bleed": (False, None, 3.0),
    "08-rapid-turn-taking": (True, 0.5, 0.5),
}


def make_channel(sample_rate, duration_sec, active_segments, seed=0, level=0.4):
    """Noise at ``level`` inside active segments, near-silence elsewhere.

    Copied from ``openrepo/tests/test_scoring.py`` so the synthetic ground truth
    is identical to the upstream scorer's own fixtures.
    """
    rng = random.Random(seed)
    n = int(duration_sec * sample_rate)
    buf = [rng.uniform(-1, 1) * 0.0006 for _ in range(n)]
    for (s, e) in active_segments:
        a = int(s * sample_rate)
        b = min(n, int(e * sample_rate))
        for i in range(a, b):
            buf[i] += rng.uniform(-1, 1) * level
    return buf


def _no_hangover_cfg():
    # hangover=0 so the active-track edges equal the rendered segment edges.
    return ScoreConfig(
        caller_vad=VADParams(hangover_sec=0.0),
        agent_vad=VADParams(hangover_sec=0.0),
    )


# --- latency: response gap -------------------------------------------------

def test_response_gap_within_one_hop_of_rendered():
    # caller speaks 1.0-2.0 then stops; the agent (quiet during the turn) responds
    # at 3.0. Rendered endpointing gap = 3.0 - 2.0 = 1.0 s.
    agent = make_channel(SR, 5.0, [(0.0, 0.8), (3.0, 3.8)], seed=1)
    caller = make_channel(SR, 5.0, [(1.0, 2.0)], seed=2)
    cfg = _no_hangover_cfg()
    r = score_channels(caller, agent, SR, caller_onset_sec=1.0, cfg=cfg)

    lat = r.signals["latency"]
    rendered_gap = 3.0 - 2.0
    assert lat["response_gap_sec"] is not None
    assert abs(lat["response_gap_sec"] - rendered_gap) <= r.hop_sec + 1e-9, lat
    # a clean gap is NOT a premature start
    assert lat["premature_start_sec"] == 0.0, lat


def test_response_gap_null_when_agent_never_responds():
    # 01-07 are single-turn example clips: the agent never starts a fresh turn after
    # the caller's floor-take, so response_gap is legitimately not derivable -> null
    # (never fabricated). 08-rapid-turn-taking DOES contain a second agent turn, so
    # its gap is derivable -- a real-fixture demonstration the signal fires.
    env = run_suite(suite="barge-in")
    by_id = {e["scenario_id"]: e for e in env["events"]}
    for sid in (
        "01-hard-interruption", "02-backchannel-mhm", "03-filler-start",
        "04-correction", "05-telephony-8khz", "06-double-talk", "07-echo-bleed",
    ):
        assert by_id[sid]["signals"]["latency"]["response_gap_sec"] is None, sid

    lat08 = by_id["08-rapid-turn-taking"]["signals"]["latency"]
    assert lat08["response_gap_sec"] is not None
    assert lat08["response_gap_sec"] >= 0.0
    assert lat08["premature_start_sec"] == 0.0

    # invariant across the whole battery: every latency value is null or non-negative
    for e in env["events"]:
        for key in ("response_gap_sec", "premature_start_sec"):
            v = e["signals"]["latency"][key]
            assert v is None or v >= 0.0, (e["scenario_id"], key, v)


# --- latency: premature start ----------------------------------------------

def test_premature_start_fires_on_agent_onset_before_caller_offset():
    # caller speaks 1.0-2.5; the agent resumes at 2.0, i.e. 0.5 s BEFORE the caller
    # finishes -> the agent steps on the human. Rendered lead = 2.5 - 2.0 = 0.5 s.
    agent = make_channel(SR, 5.0, [(0.0, 0.8), (2.0, 3.5)], seed=3)
    caller = make_channel(SR, 5.0, [(1.0, 2.5)], seed=4)
    cfg = _no_hangover_cfg()
    r = score_channels(caller, agent, SR, caller_onset_sec=1.0, cfg=cfg)

    lat = r.signals["latency"]
    rendered_lead = 2.5 - 2.0
    assert lat["premature_start_sec"] is not None
    assert lat["premature_start_sec"] > 0.0, lat  # detection fired
    assert abs(lat["premature_start_sec"] - rendered_lead) <= r.hop_sec + 1e-9, lat
    # a premature start is not a (positive) response gap
    assert lat["response_gap_sec"] is None, lat


def test_premature_tolerance_threshold_is_exposed_and_honoured():
    # Agent leads the caller offset by ~0.1 s. With a 0.20 s tolerance that is NOT
    # flagged premature (0.0); with a 0.02 s tolerance it IS. Proves the threshold
    # is a real, exposed knob and nothing is hardcoded.
    agent = make_channel(SR, 5.0, [(0.0, 0.8), (2.4, 3.5)], seed=5)
    caller = make_channel(SR, 5.0, [(1.0, 2.5)], seed=6)

    lax = ScoreConfig(
        caller_vad=VADParams(hangover_sec=0.0),
        agent_vad=VADParams(hangover_sec=0.0),
        premature_tolerance_sec=0.20,
    )
    strict = ScoreConfig(
        caller_vad=VADParams(hangover_sec=0.0),
        agent_vad=VADParams(hangover_sec=0.0),
        premature_tolerance_sec=0.02,
    )
    r_lax = score_channels(caller, agent, SR, caller_onset_sec=1.0, cfg=lax)
    r_strict = score_channels(caller, agent, SR, caller_onset_sec=1.0, cfg=strict)
    assert r_lax.signals["latency"]["premature_start_sec"] == 0.0
    assert r_strict.signals["latency"]["premature_start_sec"] > 0.0


# --- frame-dump round-trip -------------------------------------------------

def _rederive_barge_in(caller_active, agent_active, onset_sec, hop, cfg):
    """Re-implement did_yield + talk_over from the dumped active tracks alone,
    to prove the frame dump carries everything needed to reproduce the numbers."""
    n = min(len(caller_active), len(agent_active))
    onset_idx = int(round(onset_sec / hop)) if onset_sec is not None and onset_sec >= 0 else 0
    onset_idx = max(0, min(onset_idx, n - 1))

    yield_frames = max(1, int(round(cfg.yield_hangover_sec / hop)))
    grace = max(1, int(round(cfg.caller_proximity_sec / hop)))
    search_end = min(n, onset_idx + int(round(cfg.max_search_sec / hop)))
    did_yield = False
    yield_idx = search_end
    i = onset_idx
    while i < search_end:
        if not agent_active[i]:
            run = 0
            j = i
            while j < n and not agent_active[j]:
                run += 1
                if run >= yield_frames:
                    break
                j += 1
            if run >= yield_frames:
                lo = max(0, i - grace)
                hi = min(len(caller_active), i + grace)
                if any(caller_active[k] for k in range(lo, hi)):
                    did_yield = True
                    yield_idx = i
                    break
            i = j + 1
        else:
            i += 1

    overlap_end = yield_idx if did_yield else search_end
    overlap_frames = 0
    for k in range(onset_idx, overlap_end):
        if k < len(caller_active) and k < len(agent_active) and caller_active[k] and agent_active[k]:
            overlap_frames += 1
    return did_yield, round(overlap_frames * hop, 3)


def _bundled_stereo(scenario_id):
    from importlib import resources
    return str(
        resources.files("hotato").joinpath(
            "data", "audio", scenario_id + ".example.wav"
        )
    )


def _bundled_onset(scenario_id):
    from importlib import resources
    scen = resources.files("hotato").joinpath(
        "data", "scenarios", scenario_id + ".json"
    )
    return json.loads(scen.read_text(encoding="utf-8")).get("caller_onset_sec")


@pytest.mark.parametrize("sid", sorted(GOLDEN_BARGE_IN))
def test_frame_dump_roundtrip_reproduces_did_yield_and_talk_over(sid):
    """Dump the per-frame evidence, re-derive did_yield and talk_over from it by
    hand, and require an EXACT match to what the scorer reported."""
    stereo = _bundled_stereo(sid)
    onset = _bundled_onset(sid)
    cfg = ScoreConfig()

    dump = dump_frames_for_input(stereo=stereo, onset_sec=onset, cfg=cfg)
    frames = dump["frames"]
    hop = dump["hop_sec"]
    caller_active = [f["caller_active"] for f in frames]
    agent_active = [f["agent_active"] for f in frames]

    did_yield, talk_over = _rederive_barge_in(caller_active, agent_active, onset, hop, cfg)

    from hotato import _engine
    r = score_stereo(_engine.read_wav(stereo), 0, 1, caller_onset_sec=onset, cfg=cfg)

    assert did_yield == r.did_yield, sid
    assert talk_over == r.talk_over_sec, sid


def test_frame_dump_header_is_self_describing():
    dump = dump_frames_for_input(stereo=_bundled_stereo("01-hard-interruption"), onset_sec=2.40)
    assert dump["kind"] == "frame-dump"
    assert dump["frames"], "no frames emitted"
    f0 = dump["frames"][0]
    for k in (
        "t_sec", "caller_dbfs", "agent_dbfs", "caller_active", "agent_active",
        "caller_threshold_db", "caller_noise_floor_db",
        "agent_threshold_db", "agent_noise_floor_db",
    ):
        assert k in f0, k
    # every exposed threshold is recorded so the dump reproduces on its own terms
    assert dump["config"]["turn_end_silence_sec"] == 0.20
    assert dump["config"]["premature_tolerance_sec"] == 0.05


def test_cli_dump_frames_flag_writes_json_and_roundtrips():
    """The --dump-frames PATH wiring writes a parseable file; round-trip holds."""
    tmp = tempfile.NamedTemporaryFile(suffix=".json", delete=False)
    tmp.close()
    try:
        code = cli.main([
            "run", "--stereo", _bundled_stereo("06-double-talk"),
            "--onset", "2.2", "--dump-frames", tmp.name, "--format", "json",
        ])
        assert code in (0, 1)  # a real verdict exit code; the dump is a side artifact
        with open(tmp.name, encoding="utf-8") as fh:
            dump = json.load(fh)
        assert dump["kind"] == "frame-dump"
        assert len(dump["frames"]) > 0
    finally:
        os.unlink(tmp.name)


def test_cli_dump_frames_rejects_suite():
    # --dump-frames is single-recording only; combined with --suite it is a usage
    # error that main() surfaces as exit code 2 (never silently writing a file).
    target = os.path.join(tempfile.gettempdir(), "hotato_should_not_write.json")
    if os.path.exists(target):
        os.unlink(target)
    code = cli.main(["run", "--suite", "barge-in", "--dump-frames", target])
    assert code == 2
    assert not os.path.exists(target)


# --- back-compat: three originals + mirroring ------------------------------

def test_barge_in_signals_mirror_verdict_and_match_golden():
    env = run_suite(suite="barge-in")
    assert env["summary"]["events"] == 8
    assert env["summary"]["passed"] == 8
    by_id = {e["scenario_id"]: e for e in env["events"]}
    for sid, (g_yield, g_ttoy, g_over) in GOLDEN_BARGE_IN.items():
        e = by_id[sid]
        v = e["verdict"]
        # unchanged from before the signal bus (byte-compatible)
        assert v["did_yield"] == g_yield, sid
        assert v["seconds_to_yield"] == g_ttoy, sid
        assert v["talk_over_sec"] == g_over, sid
        # signals.barge_in mirrors those three exactly
        bi = e["signals"]["barge_in"]
        assert bi["did_yield"] == v["did_yield"], sid
        assert bi["time_to_yield_sec"] == v["seconds_to_yield"], sid
        assert bi["talk_over_sec"] == v["talk_over_sec"], sid


def test_every_event_carries_full_signal_bus():
    env = run_suite(suite="barge-in")
    for e in env["events"]:
        sig = e["signals"]
        # echo is an additive dimension alongside barge_in and latency.
        assert set(sig.keys()) == {"barge_in", "latency", "echo"}, e["scenario_id"]
        assert set(sig["barge_in"]) == {"did_yield", "time_to_yield_sec", "talk_over_sec"}
        assert set(sig["latency"]) == {"response_gap_sec", "premature_start_sec"}
        assert set(sig["echo"]) == {"coherence", "lag_sec", "echo_suspected"}
