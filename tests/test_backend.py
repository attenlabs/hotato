"""S3: the optional model-backed VAD backend seam.

Proves, without any network or the real model (which is gated in this build):

  1. CONTRACT -- the neural backend returns the IDENTICAL VADResult shape as the
     energy backend (same fields, same types, same length, same hop grid), so
     one shared VADResult contract covers both. A dependency-free STUB stands in
     for the real Silero model here; it is used ONLY in tests.
  2. CLEAN ERROR -- requesting backend="neural" when the optional [neural] extra
     is NOT installed raises a clean, explicit BackendUnavailable (never a silent
     fallback to energy that would change a published number's identity).
  3. REFERENCE UNAFFECTED -- the neural code path existing / a neural backend
     being registered does not move a single golden/bundled number; the bundled
     suite always scores with the energy reference.

The energy backend remains the deterministic reference; neural is a FLAGGED,
non-reference cross-check. No accuracy number is asserted or implied anywhere.
"""

import math

import pytest

import hotato  # noqa: F401  -- importing registers the real (Silero) neural factory
import hotato._engine.vad as _vad
from hotato import cli
from hotato._engine.audio import frame_rms
from hotato._engine.score import ScoreConfig, ScoreResult, score_channels
from hotato._engine.vad import (
    BackendUnavailable,
    VADParams,
    VADResult,
    energy_vad,
    neural_vad,
    register_neural_backend,
)
from hotato.core import run_single, run_suite

SR = 16000

# The frozen bundled 8 (energy reference). Must never move, including when a
# neural backend is registered. Mirrors tests/test_frozen_regression.py.
FROZEN_8 = {
    "01-hard-interruption": (True, 0.5, 0.5),
    "02-backchannel-mhm": (False, None, 1.57),
    "03-filler-start": (True, 0.65, 0.56),
    "04-correction": (True, 0.5, 0.5),
    "05-telephony-8khz": (True, 0.5, 0.5),
    "06-double-talk": (True, 1.05, 1.05),
    "07-echo-bleed": (False, None, 3.0),
    "08-rapid-turn-taking": (True, 0.5, 0.5),
}


def _silero_installed() -> bool:
    try:
        import silero_vad  # noqa: F401
        return True
    except Exception:
        return False


def _bundled(sid):
    from importlib import resources
    return str(
        resources.files("hotato").joinpath("data", "audio", sid + ".example.wav")
    )


# --- a dependency-free STUB neural backend (tests only) --------------------
#
# A crude per-frame peak gate. It is NOT the real Silero model and claims nothing
# about accuracy -- it exists only to satisfy the activity-function contract
# (samples, sample_rate, hop_sec, n_frames) -> List[bool] with a deterministic,
# distinct track, so the seam and the VADResult shape can be exercised offline.

def _stub_activity(samples, sample_rate, hop_sec, n_frames):
    hop = max(1, int(round(hop_sec * sample_rate)))
    out = []
    for k in range(n_frames):
        seg = samples[k * hop : (k + 1) * hop]
        peak = max((abs(x) for x in seg), default=0.0)
        out.append(peak >= 0.02)
    return out


def _stub_factory():
    return _stub_activity


@pytest.fixture
def stub_neural():
    """Register the stub neural backend for the duration of a test, then restore
    whatever was registered before (the real Silero factory, from importing
    hotato) so tests stay isolated."""
    saved = _vad._NEURAL_FACTORY
    register_neural_backend(_stub_factory)
    try:
        yield
    finally:
        register_neural_backend(saved) if saved is not None else _vad.clear_neural_backend()


def _make_samples():
    """A tone burst in the middle over a near-silent floor -- enough for both the
    energy VAD and the stub to produce a non-trivial, deterministic track."""
    n = SR * 3
    out = []
    for i in range(n):
        if SR // 2 <= i < 2 * SR:  # 0.5s .. 2.0s active
            out.append(0.3 * math.sin(2 * math.pi * 220 * i / SR))
        else:
            out.append(0.0004 * ((i % 5) - 2))
    return out


# --- 1. CONTRACT: identical VADResult shape, energy vs (stub) neural --------

def test_neural_and_energy_share_vadresult_contract(stub_neural):
    samples = _make_samples()
    rms, hop = frame_rms(samples, SR, 20.0, 10.0)

    e = energy_vad(rms, hop, VADParams())
    nresult = neural_vad(samples, SR, rms, hop, VADParams(backend="neural"))

    # same dataclass, same field set
    assert type(e) is VADResult and type(nresult) is VADResult
    import dataclasses
    e_fields = {f.name for f in dataclasses.fields(e)}
    n_fields = {f.name for f in dataclasses.fields(nresult)}
    assert e_fields == n_fields == {"active", "hop_sec", "threshold_db", "noise_floor_db"}

    # identical shape / grid
    assert len(nresult.active) == len(e.active) == len(rms)
    assert nresult.hop_sec == e.hop_sec == hop

    # identical field TYPES (the synthesized dB descriptors are real, finite floats)
    assert isinstance(nresult.active, list) and all(isinstance(a, bool) for a in nresult.active)
    assert isinstance(nresult.threshold_db, float) and math.isfinite(nresult.threshold_db)
    assert isinstance(nresult.noise_floor_db, float) and math.isfinite(nresult.noise_floor_db)

    # the neural track really came from the registered backend (not the energy path)
    assert nresult.active == _stub_activity(samples, SR, hop, len(rms))


def test_neural_backend_scores_end_to_end_same_result_shape(stub_neural):
    """The seam works through the public scorer: backend='neural' returns a normal
    ScoreResult with the same fields as the energy path."""
    samples = _make_samples()
    other = [0.0004 * ((i % 3) - 1) for i in range(len(samples))]
    cfg = ScoreConfig(
        caller_vad=VADParams(backend="neural"),
        agent_vad=VADParams(backend="neural"),
    )
    r = score_channels(samples, other, SR, caller_onset_sec=0.5, cfg=cfg)
    assert isinstance(r, ScoreResult)
    # same envelope of fields the energy path produces
    assert set(r.signals.keys()) == {"barge_in", "latency"}
    assert isinstance(r.did_yield, bool)
    assert isinstance(r.hop_sec, float)


# --- 2. CLEAN ERROR when the [neural] extra is not installed ----------------

def test_missing_extra_raises_clean_backend_unavailable():
    """With the real Silero factory registered (from importing hotato) and the
    [neural] extra absent, a neural request raises BackendUnavailable -- never a
    silent fallback to energy."""
    if _silero_installed():
        pytest.skip("silero-vad is installed here; the missing-extra path is not exercisable")
    samples = _make_samples()
    rms, hop = frame_rms(samples, SR, 20.0, 10.0)
    with pytest.raises(BackendUnavailable) as ei:
        neural_vad(samples, SR, rms, hop, VADParams(backend="neural"))
    msg = str(ei.value).lower()
    assert "neural" in msg and ("extra" in msg or "install" in msg)


def test_missing_extra_error_through_score_channels():
    if _silero_installed():
        pytest.skip("silero-vad is installed here; the missing-extra path is not exercisable")
    samples = _make_samples()
    cfg = ScoreConfig(
        caller_vad=VADParams(backend="neural"),
        agent_vad=VADParams(backend="neural"),
    )
    with pytest.raises(BackendUnavailable):
        score_channels(samples, samples, SR, caller_onset_sec=0.5, cfg=cfg)


def test_unknown_backend_is_a_hard_error(stub_neural):
    samples = _make_samples()
    cfg = ScoreConfig(
        caller_vad=VADParams(backend="wishful"),
        agent_vad=VADParams(backend="wishful"),
    )
    with pytest.raises(BackendUnavailable):
        score_channels(samples, samples, SR, caller_onset_sec=0.5, cfg=cfg)


# --- 3. REFERENCE UNAFFECTED by the neural code path existing ---------------

def test_default_backend_is_energy():
    assert VADParams().backend == "energy"


def test_golden_suite_unaffected_while_neural_registered(stub_neural):
    """Even with a neural backend registered, the bundled suite scores with the
    energy reference and every frozen number is unchanged."""
    env = run_suite(suite="barge-in")
    assert env["summary"]["events"] == 8
    assert env["summary"]["passed"] == 8
    by = {e["scenario_id"]: e["verdict"] for e in env["events"]}
    assert set(by) == set(FROZEN_8)
    for sid, (did_yield, ttoy, talk_over) in FROZEN_8.items():
        assert by[sid]["did_yield"] == did_yield, sid
        assert by[sid]["seconds_to_yield"] == ttoy, sid
        assert by[sid]["talk_over_sec"] == talk_over, sid


# --- backend PROVENANCE in the scored envelope (invariant 4) ----------------
#
# The reference-vs-non-reference backend must be IDENTIFIED in the result: a run
# that actually used the neural backend has to say so, and an energy (reference)
# run must stay byte-identical (no such block appears).

def test_neural_run_labels_backend_in_event_provenance(stub_neural):
    """A neural-backed single run identifies 'neural' in the event's backend
    provenance (naming the model), so it can never be mistaken for the energy
    reference."""
    stereo = _bundled("01-hard-interruption")
    cfg = ScoreConfig(
        caller_vad=VADParams(backend="neural"),
        agent_vad=VADParams(backend="neural"),
    )
    env = run_single(stereo=stereo, onset_sec=0.5, cfg=cfg)
    ev = env["events"][0]
    assert "vad_backend" in ev, "neural run must carry backend provenance"
    prov = ev["vad_backend"]
    assert prov["backend"] == "neural"
    assert prov["caller_backend"] == "neural" and prov["agent_backend"] == "neural"
    assert prov["reference"] is False
    # the model behind the neural track is named, not just the word "neural"
    assert prov["neural"]["backend"] == "neural"
    assert prov["neural"]["model"] == "silero-vad"
    assert prov["neural"]["reference"] is False


def test_energy_run_omits_backend_provenance_and_stays_byte_identical(stub_neural):
    """The default energy (reference) run attaches NO backend-provenance block, so
    the energy envelope is byte-identical to before the field existed -- proving
    the neural label is strictly additive and never touches the reference bytes.
    Registering a neural backend must not change this."""
    stereo = _bundled("01-hard-interruption")
    env = run_single(stereo=stereo, onset_sec=0.5)  # cfg=None -> energy default
    ev = env["events"][0]
    assert "vad_backend" not in ev
    # the score-bearing bytes are the deterministic energy reference for this
    # exact input, unchanged by the additive provenance field existing
    assert ev["verdict"]["did_yield"] is True
    assert ev["verdict"]["seconds_to_yield"] == 2.4
    assert ev["verdict"]["talk_over_sec"] == 0.51


# --- CLI surface -----------------------------------------------------------

def test_cli_default_backend_energy_suite_passes():
    assert cli.main(["run", "--suite", "barge-in", "--format", "json"]) == 0


def test_cli_suite_ignores_neural_and_stays_energy(capsys):
    """--backend neural + --suite: the suite is the energy reference; it still
    passes (energy), and a stderr note explains the neural request was ignored."""
    code = cli.main(["run", "--suite", "barge-in", "--backend", "neural", "--format", "json"])
    assert code == 0
    err = capsys.readouterr().err.lower()
    assert "energy reference" in err or "ignored for --suite" in err


def test_cli_backend_neural_missing_extra_is_clean_exit_2(capsys):
    if _silero_installed():
        pytest.skip("silero-vad is installed here; the missing-extra path is not exercisable")
    code = cli.main([
        "run", "--stereo", _bundled("01-hard-interruption"),
        "--backend", "neural", "--format", "json",
    ])
    assert code == 2  # clean config error, not a crash and not a silent energy score
    # --format json emits the structured error contract (schema/error.v1.json)
    # to stdout, not a plain "error:" line.
    import json
    err_obj = json.loads(capsys.readouterr().out)
    assert err_obj["ok"] is False
    assert err_obj["error_code"] == "backend_unavailable"
    assert err_obj["exit_code"] == 2
    assert "neural" in err_obj["message"].lower()


# --- 4. REAL MODEL (runs only with the [neural] extra installed) -------------
#
# Everything above exercises the seam with a dependency-free stub so it is
# testable offline. The tests below run the REAL Silero model (ONNX weights
# bundled in the silero-vad package, onnxruntime CPU) and are skipped cleanly
# when the optional extra is absent, so the zero-dependency test run stays
# green. They pin the seam's verified PROPERTIES (contract, determinism, the
# actionable unsupported-rate error); they assert nothing about what the model
# marks active, because that is a model behavior, not a seam contract.

requires_silero = pytest.mark.skipif(
    not _silero_installed(),
    reason="requires the optional [neural] extra (pip install 'hotato[neural]')",
)


@pytest.fixture
def real_neural():
    """Force the REAL Silero factory for the test (another test's stub may have
    been registered), then restore whatever was there before."""
    from hotato.neural import build_silero_backend

    saved = _vad._NEURAL_FACTORY
    register_neural_backend(build_silero_backend)
    try:
        yield
    finally:
        register_neural_backend(saved) if saved is not None else _vad.clear_neural_backend()


@requires_silero
def test_real_silero_vadresult_contract_and_determinism(real_neural):
    """The real model honors the shared VADResult contract on a bundled fixture
    and is deterministic: two runs over the same audio give identical tracks."""
    import dataclasses

    from hotato import _engine

    sig = _engine.read_wav(_bundled("01-hard-interruption"))
    samples = sig.get(0)
    rms, hop = frame_rms(samples, sig.sample_rate, 20.0, 10.0)

    r1 = neural_vad(samples, sig.sample_rate, rms, hop, VADParams(backend="neural"))
    r2 = neural_vad(samples, sig.sample_rate, rms, hop, VADParams(backend="neural"))

    assert type(r1) is VADResult
    fields = {f.name for f in dataclasses.fields(r1)}
    assert fields == {"active", "hop_sec", "threshold_db", "noise_floor_db"}
    assert len(r1.active) == len(rms)
    assert r1.hop_sec == hop
    assert all(isinstance(a, bool) for a in r1.active)
    assert math.isfinite(r1.threshold_db) and math.isfinite(r1.noise_floor_db)
    # determinism: identical output, run to run
    assert r1 == r2


@requires_silero
def test_real_silero_end_to_end_score_is_deterministic(real_neural):
    """backend='neural' with the real model produces a normal ScoreResult through
    the public scorer, identical across two runs on the same recording."""
    from hotato import _engine

    sig = _engine.read_wav(_bundled("01-hard-interruption"))
    cfg = ScoreConfig(
        caller_vad=VADParams(backend="neural"),
        agent_vad=VADParams(backend="neural"),
    )
    r1 = score_channels(sig.get(0), sig.get(1), sig.sample_rate, caller_onset_sec=0.5, cfg=cfg)
    r2 = score_channels(sig.get(0), sig.get(1), sig.sample_rate, caller_onset_sec=0.5, cfg=cfg)
    assert isinstance(r1, ScoreResult)
    assert set(r1.signals.keys()) == {"barge_in", "latency"}
    assert r1.as_dict() == r2.as_dict()


@requires_silero
def test_real_silero_unsupported_rate_is_an_actionable_error(real_neural):
    """REGRESSION: Silero supports 8 kHz, 16 kHz, and 16 kHz multiples. A 44.1 kHz
    recording must fail with the seam's actionable resample message (never a
    model-internal error, never a silent energy fallback)."""
    sr = 44100
    samples = [0.3 * math.sin(2 * math.pi * 220 * i / sr) for i in range(sr)]
    rms, hop = frame_rms(samples, sr, 20.0, 10.0)
    with pytest.raises(ValueError, match="[Rr]esample") as ei:
        neural_vad(samples, sr, rms, hop, VADParams(backend="neural"))
    msg = str(ei.value)
    assert "16000" in msg and "44100" in msg and "energy" in msg
