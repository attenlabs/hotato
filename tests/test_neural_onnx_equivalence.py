"""Direct-ONNX Silero VAD equivalence + torch-free guarantees.

The neural VAD backend runs the bundled Silero VAD ONNX weights DIRECTLY on
onnxruntime with a numpy re-implementation of silero-vad's segmentation. This
suite proves that refactor is EQUIVALENCE-PRESERVING and torch-free:

  1. FIXTURE EQUIVALENCE -- on bundled audio, the direct-onnxruntime path
     reproduces, byte-for-byte, the speech segments and the projected per-hop
     `active` track that the ORIGINAL silero-vad `get_speech_timestamps` path
     produced. The golden (tests/golden/neural_onnx_equivalence.json) was
     captured by running the REAL silero-vad library once; it is the reference.
  2. POST-PROCESSING EQUIVALENCE -- the numpy port of `get_speech_timestamps`
     reproduces the REAL library's segments on scripted probability sequences
     that exercise start/stop hysteresis, min-speech rejection, min-silence
     bridging, trailing speech, two-segment padding, and the 8 kHz window path.
     Golden captured from the real library via a scripted fake model; this test
     needs numpy only (no onnxruntime).
  3. TORCH-FREE -- a neural run completes with `torch` and `silero_vad` import
     BLOCKED, and neither is present in sys.modules afterwards. The [neural]
     path depends on onnxruntime + numpy alone.
  4. EXTRA LOCKSTEP -- the [neural] extra declares onnxruntime + numpy and does
     NOT pull in silero-vad or torch.

No accuracy number is asserted anywhere: this pins EQUIVALENCE to the prior
behavior and the absence of a heavy dependency, nothing about correctness of the
model's speech decisions.
"""

import importlib.util
import json
import os
import sys

import pytest

HERE = os.path.dirname(__file__)
GOLDEN_DIR = os.path.join(HERE, "golden")


def _load_golden(name):
    with open(os.path.join(GOLDEN_DIR, name)) as f:
        return json.load(f)


def _has(mod):
    return importlib.util.find_spec(mod) is not None


requires_onnxruntime = pytest.mark.skipif(
    not (_has("onnxruntime") and _has("numpy")),
    reason="requires the optional [neural] extra (pip install 'hotato[neural]': onnxruntime + numpy)",
)
requires_numpy = pytest.mark.skipif(not _has("numpy"), reason="requires numpy")


def _bundled_path(rel):
    from importlib import resources

    return str(resources.files("hotato").joinpath(*rel.split("/")))


def _project(segments, sample_rate, hop_sec, n_frames):
    """The exact hop-grid projection the neural backend applies to segments."""
    active = [False] * n_frames
    for seg in segments:
        lo = int(seg["start"] / sample_rate / hop_sec)
        hi = int(seg["end"] / sample_rate / hop_sec)
        for k in range(max(0, lo), min(n_frames, hi + 1)):
            active[k] = True
    return active


# --- 1. FIXTURE EQUIVALENCE: direct-onnx == silero-vad, segments + active ----


@requires_onnxruntime
def test_direct_onnx_matches_silero_golden_on_fixtures():
    """For every bundled fixture, the direct-onnxruntime segments AND per-hop
    active track equal the golden captured from the real silero-vad path."""
    import numpy as np

    from hotato import neural
    from hotato._engine.audio import frame_rms, read_wav

    golden = _load_golden("neural_onnx_equivalence.json")
    session = neural._load_session()
    assert golden["cases"], "golden has no cases"

    nontrivial = 0
    for case in golden["cases"]:
        sig = read_wav(_bundled_path(case["wav"]))
        samples = sig.get(case["channel"])
        sr = sig.sample_rate
        assert sr == case["sample_rate"], case["id"]

        wav = np.asarray(samples, dtype=np.float32)
        segs = neural._get_speech_timestamps(wav, sr, session)
        segs = [{"start": int(s["start"]), "end": int(s["end"])} for s in segs]
        assert segs == case["segments"], (
            f"{case['id']}: direct-onnx segments diverged from the silero-vad golden"
        )

        rms, hop = frame_rms(samples, sr, case["frame_ms"], case["hop_ms"])
        assert len(rms) == case["n_frames"], case["id"]
        assert abs(hop - case["hop_sec"]) < 1e-12, case["id"]
        active = _project(segs, sr, hop, len(rms))
        golden_active = [bool(a) for a in case["active"]]
        assert active == golden_active, (
            f"{case['id']}: projected per-hop active diverged from the golden"
        )
        if sum(golden_active):
            nontrivial += 1

    # Guard the guard: at least one fixture must have a NON-empty speech track,
    # otherwise "equivalence" would be the trivial empty==empty and prove nothing.
    assert nontrivial >= 1, "no fixture exercised a non-empty neural track"


@requires_onnxruntime
def test_direct_onnx_active_matches_golden_through_neural_vad_seam():
    """End-to-end through the public VAD seam: neural_vad(...).active equals the
    golden active on the non-trivial fixtures (the real registered backend)."""
    import numpy as np  # noqa: F401

    import hotato._engine.vad as _vad
    from hotato._engine.audio import frame_rms, read_wav
    from hotato._engine.vad import VADParams, neural_vad
    from hotato.neural import build_silero_backend

    golden = _load_golden("neural_onnx_equivalence.json")
    saved = _vad._NEURAL_FACTORY
    _vad.register_neural_backend(build_silero_backend)
    try:
        checked = 0
        for case in golden["cases"]:
            if not sum(case["active"]):
                continue  # focus the seam assertion on non-trivial tracks
            sig = read_wav(_bundled_path(case["wav"]))
            samples = sig.get(case["channel"])
            sr = sig.sample_rate
            rms, hop = frame_rms(samples, sr, case["frame_ms"], case["hop_ms"])
            r = neural_vad(samples, sr, rms, hop, VADParams(backend="neural"))
            assert r.active == [bool(a) for a in case["active"]], case["id"]
            assert len(r.active) == case["n_frames"], case["id"]
            checked += 1
        assert checked >= 1
    finally:
        if saved is not None:
            _vad.register_neural_backend(saved)
        else:
            _vad.clear_neural_backend()


@requires_onnxruntime
def test_direct_onnx_is_deterministic_on_a_fixture():
    """Two runs of the direct-onnx path over the same audio are byte-identical."""
    import numpy as np

    from hotato import neural
    from hotato._engine.audio import read_wav

    # fd-02 is a recorded call whose agent channel carries a real speech track
    # (a non-empty determinism check). fd-01 is a synthesized speech-envelope
    # render (band-limited noise), which the speech-trained model scores as no
    # speech, so it would exercise only the trivial empty==empty path here.
    sig = read_wav(_bundled_path("data/demo/failing/audio/fd-02-backchannel-yielded.example.wav"))
    wav = np.asarray(sig.get(1), dtype=np.float32)
    session = neural._load_session()
    a = neural._get_speech_timestamps(wav, sig.sample_rate, session)
    b = neural._get_speech_timestamps(wav, sig.sample_rate, session)
    assert a == b and len(a) >= 1


# --- 2. POST-PROCESSING EQUIVALENCE on scripted probabilities (numpy-only) ---


@requires_numpy
def test_speech_timestamps_port_matches_silero_on_scripted_probs():
    """The numpy port of get_speech_timestamps reproduces the REAL library's
    segments on crafted probability sequences (hysteresis, min-speech rejection,
    min-silence bridging, trailing speech, two segments, 8 kHz)."""
    from hotato.neural import _speech_timestamps_from_probs

    golden = _load_golden("neural_postproc_stress.json")
    assert golden["cases"]
    branch_ids = set()
    for case in golden["cases"]:
        segs = _speech_timestamps_from_probs(
            case["probs"],
            case["audio_length_samples"],
            case["window_size"],
            case["step"],
            case["sample_rate"],
        )
        segs = [{"start": int(s["start"]), "end": int(s["end"])} for s in segs]
        assert segs == case["segments"], (
            f"{case['id']}: ported get_speech_timestamps diverged from silero golden"
        )
        branch_ids.add(case["id"])
    # sanity: the stress golden covers the empty, single, two-segment, rejected,
    # hysteresis, trailing, and 8 kHz branches
    for needle in ("all_silence", "single_block", "two_blocks", "rejected", "hysteresis", "trailing", "8k"):
        assert any(needle in cid for cid in branch_ids), f"missing stress branch: {needle}"


# --- 3. TORCH-FREE: the neural path never imports torch/silero_vad ------------


@requires_onnxruntime
def test_neural_run_needs_no_torch_or_silero():
    """A neural run completes with `torch` and `silero_vad` import BLOCKED, and
    neither ends up in sys.modules -- the [neural] path is onnxruntime + numpy."""
    import numpy as np  # noqa: F401

    import hotato._engine.vad as _vad
    from hotato._engine.audio import frame_rms, read_wav
    from hotato._engine.vad import VADParams, neural_vad
    from hotato.neural import build_silero_backend

    # Block imports of torch and silero_vad: a None entry in sys.modules makes
    # any `import X` raise ImportError, so if the neural path tried to import
    # either, this test would fail loudly instead of passing by accident.
    saved_mods = {name: sys.modules.get(name, "__ABSENT__") for name in ("torch", "silero_vad")}
    for name in ("torch", "silero_vad"):
        sys.modules[name] = None

    saved_factory = _vad._NEURAL_FACTORY
    _vad.register_neural_backend(build_silero_backend)
    try:
        # fd-02's agent channel is a recorded speech track (non-empty neural
        # result); fd-01 is a synthesized speech-envelope render the speech
        # model scores as no speech, so it cannot prove the path ran.
        sig = read_wav(_bundled_path("data/demo/failing/audio/fd-02-backchannel-yielded.example.wav"))
        samples = sig.get(1)
        rms, hop = frame_rms(samples, sig.sample_rate, 20.0, 10.0)
        r = neural_vad(samples, sig.sample_rate, rms, hop, VADParams(backend="neural"))
        assert any(r.active), "expected a non-empty neural track on this fixture"
        # our import-blocking sentinels are untouched: nothing swapped in a real
        # torch/silero_vad module mid-run
        assert sys.modules.get("torch") is None
        assert sys.modules.get("silero_vad") is None
    finally:
        if saved_factory is not None:
            _vad.register_neural_backend(saved_factory)
        else:
            _vad.clear_neural_backend()
        for name, val in saved_mods.items():
            if val == "__ABSENT__":
                sys.modules.pop(name, None)
            else:
                sys.modules[name] = val


# --- 4. EXTRA LOCKSTEP: [neural] declares onnxruntime + numpy, not torch ------


def test_neural_extra_declares_onnxruntime_numpy_not_torch_or_silero():
    """pyproject's [neural] extra must stay onnxruntime + numpy only -- a guard
    so nobody silently re-introduces the torch/silero-vad transitive stack."""
    try:
        import tomllib
    except ModuleNotFoundError:
        pytest.skip("tomllib requires Python 3.11+; the extra is checked there")

    root = os.path.dirname(HERE)
    with open(os.path.join(root, "pyproject.toml"), "rb") as f:
        data = tomllib.load(f)
    extras = data["project"]["optional-dependencies"]
    neural = extras["neural"]
    joined = " ".join(neural).lower()
    assert any(d.startswith("onnxruntime") for d in neural), neural
    assert any(d.startswith("numpy") for d in neural), neural
    assert "silero-vad" not in joined and "silero_vad" not in joined, neural
    assert "torch" not in joined, neural
    # the `all` extra must not drag silero-vad back in either
    all_joined = " ".join(extras.get("all", [])).lower()
    assert "silero-vad" not in all_joined and "silero_vad" not in all_joined, extras.get("all")
