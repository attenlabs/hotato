"""Low-SNR scorability gate (core.run_single ``snr_gate_db`` + ``hotato run
--snr-gate-db``).

Fail-closed contract under test: a two-channel recording whose noise floor
sits inside the energy VAD's dynamic margin flips a correct yield into a
false talk-over (the agent's stop is not observable). With the gate on, that
recording is NOT SCORABLE with a low-snr reason (exit 2), never silently
mis-scored. With the gate off (the default), behavior is byte-identical to
before the gate existed.

Zero third-party deps; the synthetic fixtures are deterministic (seeded
random.Random, the same make_channel pattern as test_signals.py) and the
shipped fixtures are the bundled example WAVs plus one rendered gold-tier
noise-family scenario (the quietest scoring channel any shipped tier reaches).
"""

import importlib.util
import json
import math
import os
import random
from importlib import resources

import pytest

from hotato import cli
from hotato._engine.audio import read_wav, write_wav
from hotato.core import (
    SNR_GATE_DEFAULT_DB,
    estimate_channel_snr_db,
    process_exit_code,
    run_single,
)

SR = 16000
DUR = 5.0
AGENT_SEG = [(0.2, 2.5)]
CALLER_SEG = [(2.0, 4.2)]
ONSET = 2.0

_REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_RENDERER = os.path.join(_REPO, "examples", "render_examples.py")
_GOLD_QUIETEST = os.path.join(
    _REPO, "corpus", "suites", "gold", "scenarios", "gl-hi-noise-020.json"
)


def _bundled(sid):
    return str(
        resources.files("hotato").joinpath("data", "audio", sid + ".example.wav")
    )


def _channel(segments, seed, level=0.4):
    rng = random.Random(seed)
    n = int(DUR * SR)
    buf = [rng.uniform(-1, 1) * 0.0006 for _ in range(n)]
    for (s, e) in segments:
        a = int(s * SR)
        b = min(n, int(e * SR))
        for i in range(a, b):
            buf[i] += rng.uniform(-1, 1) * level
    return buf


def _seg_rms(x, segments):
    acc, cnt = 0.0, 0
    for (s, e) in segments:
        a, b = int(s * SR), min(len(x), int(e * SR))
        for i in range(a, b):
            acc += x[i] * x[i]
            cnt += 1
    return (acc / cnt) ** 0.5


def _with_noise(x, segments, snr_db, seed):
    """Add seeded uniform noise calibrated against the channel's speech RMS:
    noise_rms = speech_rms * 10**(-snr_db/20); uniform amp = noise_rms*sqrt(3)."""
    amp = _seg_rms(x, segments) * (10 ** (-snr_db / 20.0)) * math.sqrt(3.0)
    rng = random.Random(seed)
    return [max(-1.0, min(1.0, s + rng.uniform(-1, 1) * amp)) for s in x]


def _write_pair(tmp_path, caller, agent):
    cp = tmp_path / "caller.wav"
    ap = tmp_path / "agent.wav"
    write_wav(str(cp), SR, [caller])
    write_wav(str(ap), SR, [agent])
    return str(cp), str(ap)


def _noisy_pair(tmp_path, snr_db=15.0):
    """A yielding pair with seeded uniform noise on both channels, inside the
    measured failure band (the uniform-noise verdict cliff is between 19 and
    18 dB injection SNR)."""
    caller = _with_noise(_channel(CALLER_SEG, 2), CALLER_SEG, snr_db, 101)
    agent = _with_noise(_channel(AGENT_SEG, 1), AGENT_SEG, snr_db, 202)
    return _write_pair(tmp_path, caller, agent)


# --- default off: byte-identity -------------------------------------------

def test_gate_off_is_default_and_adds_nothing(tmp_path):
    cp, ap = _write_pair(tmp_path, _channel(CALLER_SEG, 2), _channel(AGENT_SEG, 1))
    env = run_single(caller=cp, agent=ap, onset_sec=ONSET, expect="yield")
    ev = env["events"][0]
    assert "snr_estimate" not in ev
    assert "scorable" not in ev
    assert ev["verdict"]["did_yield"] is True


def test_default_off_byte_identity_on_shipped_fixture():
    # The kwarg left unset and snr_gate_db=None produce byte-identical
    # canonical output on a shipped fixture: the default path never changes.
    wav = _bundled("01-hard-interruption")
    unset = run_single(stereo=wav, onset_sec=2.4, expect="yield")
    off = run_single(stereo=wav, onset_sec=2.4, expect="yield", snr_gate_db=None)
    assert json.dumps(unset, sort_keys=True) == json.dumps(off, sort_keys=True)


# --- gate on: clean audio still scores -------------------------------------

def test_gate_on_clean_recording_still_scores(tmp_path):
    cp, ap = _write_pair(tmp_path, _channel(CALLER_SEG, 2), _channel(AGENT_SEG, 1))
    env = run_single(
        caller=cp, agent=ap, onset_sec=ONSET, expect="yield",
        snr_gate_db=SNR_GATE_DEFAULT_DB,
    )
    ev = env["events"][0]
    assert "scorable" not in ev
    assert ev["verdict"]["did_yield"] is True
    est = ev["snr_estimate"]
    assert est["gate_db"] == SNR_GATE_DEFAULT_DB
    assert est["caller_snr_db"] > SNR_GATE_DEFAULT_DB
    assert est["agent_snr_db"] > SNR_GATE_DEFAULT_DB


def test_gate_passes_every_bundled_fixture_and_scores_the_quietest():
    # Every shipped example WAV clears the floor on both channels; the
    # quietest channel of the set, gated at the default floor, still scores.
    sids = [
        "01-hard-interruption", "02-backchannel-mhm", "03-filler-start",
        "04-correction", "05-telephony-8khz", "06-double-talk",
        "07-echo-bleed", "08-rapid-turn-taking",
    ]
    quietest = (float("inf"), None)
    for sid in sids:
        sig = read_wav(_bundled(sid))
        for ch in range(sig.num_channels):
            est = estimate_channel_snr_db(sig.get(ch), sig.sample_rate)
            assert est > SNR_GATE_DEFAULT_DB, f"{sid} ch{ch} estimates {est}"
            if est < quietest[0]:
                quietest = (est, sid)
    _, sid = quietest
    with open(os.path.join(
            _REPO, "src", "hotato", "data", "scenarios", sid + ".json"),
            encoding="utf-8") as fh:
        sc = json.load(fh)
    env = run_single(
        stereo=_bundled(sid),
        onset_sec=sc["caller_onset_sec"],
        expect="yield" if sc["expected"]["yield"] else "hold",
        snr_gate_db=SNR_GATE_DEFAULT_DB,
    )
    ev = env["events"][0]
    assert "scorable" not in ev
    assert "snr_estimate" in ev


@pytest.mark.skipif(
    not (os.path.exists(_RENDERER) and os.path.exists(_GOLD_QUIETEST)),
    reason="repo checkout only (renderer or gold suite scenario absent)",
)
def test_gate_passes_quietest_gold_tier_render(tmp_path):
    # The gold noise family is the quietest scoring tier any shipped suite
    # reaches (noise floor about 23.8 dB below speech by the sweep's SNR
    # formula; the estimator reads it about 27.3 dB). Gated at the default
    # floor it still scores: the shipped corpus sits clear of the cliff.
    spec = importlib.util.spec_from_file_location("rx", _RENDERER)
    rx = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(rx)
    with open(_GOLD_QUIETEST, encoding="utf-8") as fh:
        sc = json.load(fh)
    sr, caller, agent = rx.build_scenario(sc)
    cp, ap = str(tmp_path / "caller.wav"), str(tmp_path / "agent.wav")
    write_wav(cp, sr, [caller])
    write_wav(ap, sr, [agent])
    env = run_single(
        caller=cp, agent=ap,
        onset_sec=sc["caller_onset_sec"],
        expect="yield" if sc["expected"]["yield"] else "hold",
        snr_gate_db=SNR_GATE_DEFAULT_DB,
    )
    ev = env["events"][0]
    assert "scorable" not in ev
    est = ev["snr_estimate"]
    assert min(est["caller_snr_db"], est["agent_snr_db"]) > SNR_GATE_DEFAULT_DB
    assert ev["verdict"]["did_yield"] is True


# --- gate on: below-floor audio refuses ------------------------------------

def test_gate_off_low_snr_is_misscored_gate_on_refuses(tmp_path):
    cp, ap = _noisy_pair(tmp_path)

    # Without the gate: the false verdict this gate exists to refuse.
    env_off = run_single(caller=cp, agent=ap, onset_sec=ONSET, expect="yield")
    ev_off = env_off["events"][0]
    assert "scorable" not in ev_off
    assert ev_off["verdict"]["did_yield"] is False  # the silent mis-score

    # With the gate: NOT SCORABLE, low-snr reason, fail-closed exit 2.
    env_on = run_single(
        caller=cp, agent=ap, onset_sec=ONSET, expect="yield",
        snr_gate_db=SNR_GATE_DEFAULT_DB,
    )
    ev_on = env_on["events"][0]
    assert ev_on["scorable"] is False
    assert ev_on["not_scorable_reason"].startswith("low-snr:")
    assert ev_on["verdict"]["passed"] is False
    assert ev_on["snr_estimate"]["agent_snr_db"] < SNR_GATE_DEFAULT_DB
    assert process_exit_code(env_on) == 2


def test_low_snr_reason_states_measurement_floor_and_next_step(tmp_path):
    cp, ap = _noisy_pair(tmp_path)
    env = run_single(
        caller=cp, agent=ap, onset_sec=ONSET, expect="yield",
        snr_gate_db=SNR_GATE_DEFAULT_DB,
    )
    ev = env["events"][0]
    reason = ev["not_scorable_reason"]
    worst = min(ev["snr_estimate"]["caller_snr_db"], ev["snr_estimate"]["agent_snr_db"])
    assert reason.startswith("low-snr:")
    assert f"{worst:.1f} dB" in reason
    assert f"{SNR_GATE_DEFAULT_DB:.1f} dB scoring floor" in reason
    assert "dynamic margin" in reason
    assert "input problem, not an agent verdict" in reason
    # The same reason rides the standard verdict envelope, like every other
    # not-scorable input problem.
    assert ev["verdict"]["reasons"] == [reason]


# --- the estimator itself ---------------------------------------------------

def test_estimator_is_deterministic_and_sparse_speech_safe():
    # Determinism: same samples, same number, twice.
    agent = _channel(AGENT_SEG, 7)
    e1 = estimate_channel_snr_db(agent, SR)
    e2 = estimate_channel_snr_db(agent, SR)
    assert e1 == e2
    # Sparse-speech safety: a channel with ONE short backchannel still measures
    # its speech against the floor (not silence vs silence -> near-zero).
    sparse = _channel([(2.0, 2.3)], 9)
    assert estimate_channel_snr_db(sparse, SR) > SNR_GATE_DEFAULT_DB
    # No separable content at all -> 0.0, never a fabricated positive number.
    flat = [0.0] * SR
    assert estimate_channel_snr_db(flat, SR) == 0.0


# --- CLI plumbing -----------------------------------------------------------

def test_cli_snr_gate_refuses_noisy_pair(tmp_path, capsys):
    cp, ap = _noisy_pair(tmp_path)
    code = cli.main([
        "run", "--caller", cp, "--agent", ap, "--onset", str(ONSET),
        "--snr-gate-db", "22.0", "--format", "json",
    ])
    assert code == 2
    env = json.loads(capsys.readouterr().out)
    ev = env["events"][0]
    assert ev["scorable"] is False
    assert ev["not_scorable_reason"].startswith("low-snr:")
    assert ev["snr_estimate"]["gate_db"] == 22.0


def test_cli_bare_flag_uses_the_default_floor(tmp_path, capsys):
    cp, ap = _write_pair(tmp_path, _channel(CALLER_SEG, 2), _channel(AGENT_SEG, 1))
    code = cli.main([
        "run", "--caller", cp, "--agent", ap, "--onset", str(ONSET),
        "--snr-gate-db", "--format", "json",
    ])
    assert code == 0
    env = json.loads(capsys.readouterr().out)
    ev = env["events"][0]
    assert "scorable" not in ev
    assert ev["snr_estimate"]["gate_db"] == SNR_GATE_DEFAULT_DB


def test_cli_default_run_carries_no_estimate(tmp_path, capsys):
    cp, ap = _write_pair(tmp_path, _channel(CALLER_SEG, 2), _channel(AGENT_SEG, 1))
    code = cli.main([
        "run", "--caller", cp, "--agent", ap, "--onset", str(ONSET),
        "--format", "json",
    ])
    assert code == 0
    ev = json.loads(capsys.readouterr().out)["events"][0]
    assert "snr_estimate" not in ev
    assert "scorable" not in ev


def test_cli_snr_gate_conflicts_with_suite():
    assert cli.main(["run", "--suite", "barge-in", "--snr-gate-db", "22"]) == 2


def test_snr_gate_refuses_diarized_mono_cleanly(tmp_path):
    mono = tmp_path / "mono.wav"
    write_wav(str(mono), SR, [_channel(AGENT_SEG, 1)])
    with pytest.raises(ValueError, match="separated channels"):
        run_single(mono=str(mono), diarize=True, snr_gate_db=SNR_GATE_DEFAULT_DB)
