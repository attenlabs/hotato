"""Optional, NON-REFERENCE neural VAD backend (Silero VAD, MIT).

FLAGGED, by construction, as a cross-check -- never the reference. The
deterministic energy backend in the vendored engine (``_engine.vad.energy_vad``)
produces every published, golden, and bundled number Hotato reports,
byte-for-byte. This module exists to answer one reviewer objection -- "this is
just energy VAD, rebuildable in a weekend" -- by letting the SAME turn-taking
timing math run over a learned, model-backed speech track that you can compare
against the energy track. It is opt-in (``hotato run ... --backend neural`` / the
``[neural]`` extra) and is wired through the one shared ``VADResult`` contract,
so its output is shape-identical to the energy track.

What it changes, honestly:
  * It TIGHTENS onset precision on clean speech: a learned model can place the
    speech / no-speech boundary better than a fixed dB threshold on some audio.
  * It does NOT close the energy-vs-intent gap. A cough, a laugh, a door slam,
    or crosstalk still carries speech-band energy, and a VAD -- energy OR neural
    -- can mark it active. Whether a sound is a real bid for the conversational
    turn is not decidable from one channel's activity alone. No accuracy number
    is claimed for either backend, here or anywhere.

Primary target: Silero VAD (MIT), run locally / fully offline. Inference
executes through onnxruntime on CPU with the ONNX weights that ship inside the
silero-vad pip package (no download at run time); note the silero-vad package
itself depends on torch for its segmentation utilities, so installing the extra
installs torch. The interface is a plain per-frame activity function, so an
open-weight turn-detection model (e.g. LiveKit's Smart-Turn) can be dropped in
exactly the same way later.

VERIFIED against the real model (silero-vad 6.x, ONNX, CPU). Properties that
hold, measured in this repo (see METHODOLOGY.md, "Optional neural cross-check
(non-reference)"):
  * Contract: the seam returns the identical ``VADResult`` shape as energy, on
    the same hop grid.
  * Determinism: repeated runs on the same audio produce byte-identical output.
  * No fallback: with the extra absent, requesting the neural backend raises a
    clean ``BackendUnavailable``; it never silently substitutes energy.
  * On the SYNTHETIC fixtures (bundled battery and corpus suites), a
    speech-trained model assigns the shaped-noise renders near-zero speech
    probability, so the neural track is empty there at Silero's default
    threshold. Those fixtures are rendered for the energy reference; the
    neural cross-check is informative on real recordings.
  * Sample rates: Silero accepts 8000 Hz, 16000 Hz, and integer multiples of
    16000 Hz (decimated by silero-vad itself). Anything else is rejected with
    an actionable error below; the energy backend measures at any rate.
"""

from __future__ import annotations

from typing import Callable, List

from ._engine.vad import BackendUnavailable


def build_silero_backend() -> Callable[[List[float], int, float, int], List[bool]]:
    """Factory for the Silero-VAD per-frame activity function.

    Returns ``activity(samples, sample_rate, hop_sec, n_frames) -> List[bool]``
    of length ``n_frames`` aligned to the energy VAD's hop grid. Raises
    ``BackendUnavailable`` (never a bare ImportError, never a silent energy
    fallback) if the optional ``[neural]`` extra is not installed, or if the
    packaged model weights cannot be loaded (a broken or partial install).

    This factory is registered lazily (see ``hotato.__init__``): it is only
    *called* -- and thus only imports/loads the model -- the first time
    ``--backend neural`` is actually requested. The zero-dependency energy path
    never reaches this code.
    """
    try:
        import numpy as np  # noqa: F401  # silero operates on float32 waveforms
        from silero_vad import get_speech_timestamps, load_silero_vad
    except Exception as exc:  # ImportError, or a partial/broken install
        raise BackendUnavailable(
            "the 'neural' VAD backend requires the optional extra: "
            "pip install 'hotato[neural]'  (missing dependency: "
            f"{exc}). The energy backend stays the reproducible reference; "
            "nothing falls back to it silently."
        ) from exc

    try:
        model = load_silero_vad(onnx=True)
    except Exception as exc:  # broken install / unreadable packaged weights
        raise BackendUnavailable(
            "the 'neural' extra is installed but the Silero VAD model could not "
            "be loaded. The ONNX weights ship inside the silero-vad package, so "
            "this usually means a broken or partial install; reinstall with "
            "pip install --force-reinstall 'hotato[neural]'. "
            f"(underlying: {exc})"
        ) from exc

    def _activity(samples, sample_rate, hop_sec, n_frames):
        import numpy as np

        sr = int(sample_rate)
        # Mirror Silero's supported-rate contract up front so an unsupported
        # recording fails with an actionable message instead of a model-internal
        # error. silero-vad decimates 16 kHz multiples itself and returns
        # timestamps in the ORIGINAL sample coordinates (verified), so those
        # pass through untouched.
        if sr not in (8000, 16000) and not (sr > 16000 and sr % 16000 == 0):
            raise ValueError(
                f"the neural (Silero) backend supports 8000 Hz, 16000 Hz, and "
                f"integer multiples of 16000 Hz; this recording is {sr} Hz. "
                "Resample it first, e.g. "
                "ffmpeg -i in.wav -ar 16000 out.wav, or score it with the "
                "energy backend (the reference), which measures at any rate."
            )
        wav = np.asarray(samples, dtype=np.float32)
        segments = get_speech_timestamps(wav, model, sampling_rate=sr)
        active = [False] * n_frames
        # Project the model's [start, end) SAMPLE segments onto the same hop grid
        # the energy track uses, so the two `active` lists are directly comparable.
        for seg in segments:
            lo = int(seg["start"] / sample_rate / hop_sec)
            hi = int(seg["end"] / sample_rate / hop_sec)
            for k in range(max(0, lo), min(n_frames, hi + 1)):
                active[k] = True
        return active

    return _activity


# Stable identity of the OPTIONAL, NON-REFERENCE neural VAD backend. A result
# produced with ``--backend neural`` carries this descriptor so the
# reference-vs-non-reference backend is IDENTIFIED in provenance -- never left
# indistinguishable from the deterministic energy reference. ``reference`` is
# False by construction: the energy backend yields every published/golden/bundled
# number and is the default, so an energy run carries NO such block and its
# output stays byte-identical.
NEURAL_BACKEND = "neural"
NEURAL_MODEL = "silero-vad"


def neural_backend_provenance() -> dict:
    """Provenance descriptor naming the neural VAD backend (a fresh dict per call).

    Present on a result ONLY when the neural backend actually produced a track;
    the energy reference (the default) attaches nothing, so its bytes are
    unchanged. ``reference: False`` states plainly that this is the non-reference
    cross-check, not the number-of-record backend.
    """
    return {
        "backend": NEURAL_BACKEND,
        "model": NEURAL_MODEL,
        "runtime": "onnxruntime-cpu",
        "reference": False,
    }


__all__ = [
    "build_silero_backend",
    "neural_backend_provenance",
    "NEURAL_BACKEND",
    "NEURAL_MODEL",
]
