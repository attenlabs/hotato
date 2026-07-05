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

Primary target: Silero VAD (MIT), run locally / fully offline via onnxruntime
(no torch needed at inference). The interface is a plain per-frame activity
function, so an open-weight turn-detection model (e.g. LiveKit's Smart-Turn) can
be dropped in exactly the same way later.

VERIFICATION IS GATED HERE: this build environment has no network and cannot
download the Silero weights, so the real model is WIRED but has NOT been executed
or validated in this repo. Running it against real weights is a documented,
gated step (see METHODOLOGY.md, "Optional neural cross-check (non-reference)").
Nothing below fabricates a result: with the extra (or the weights) absent,
requesting the neural backend raises a clean ``BackendUnavailable`` and never
falls back to energy.
"""

from __future__ import annotations

from typing import Callable, List

from ._engine.vad import BackendUnavailable

# Peak level (linear, samples in [-1, 1]) below which the model is treated as
# not having fired for a frame when mapping segment timestamps to the frame grid.
# Used only inside the timestamp->frame projection, not as a decision threshold.


def build_silero_backend() -> Callable[[List[float], int, float, int], List[bool]]:
    """Factory for the Silero-VAD per-frame activity function.

    Returns ``activity(samples, sample_rate, hop_sec, n_frames) -> List[bool]``
    of length ``n_frames`` aligned to the energy VAD's hop grid. Raises
    ``BackendUnavailable`` (never a bare ImportError, never a silent energy
    fallback) if the optional ``[neural]`` extra is not installed, or if the
    model weights cannot be loaded offline (the gated verification step).

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
    except Exception as exc:  # weights not cached / no network on first run
        raise BackendUnavailable(
            "the 'neural' extra is installed but the Silero VAD model could not "
            "be loaded (offline / first run with no cached weights). Loading and "
            "running the real model against your audio is the documented, gated "
            "verification step -- see METHODOLOGY.md, 'Optional neural cross-check'. "
            f"(underlying: {exc})"
        ) from exc

    def _activity(samples, sample_rate, hop_sec, n_frames):
        import numpy as np

        wav = np.asarray(samples, dtype=np.float32)
        segments = get_speech_timestamps(wav, model, sampling_rate=int(sample_rate))
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


__all__ = ["build_silero_backend"]
