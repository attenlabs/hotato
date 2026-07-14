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

Primary target: Silero VAD (MIT), run locally / fully offline. The ONNX weights
(``data/silero_vad.onnx``, ~2.33 MB) ship inside THIS package, and inference
executes directly on onnxruntime (CPU): the forward pass, the streaming LSTM
state carried between windows, and the segmentation post-processing all run on
onnxruntime + numpy alone. The ``[neural]`` extra therefore installs only
``onnxruntime`` and ``numpy`` -- no torch, no torchaudio, no CUDA stack. The
interface is a plain per-frame activity function, so an open-weight
turn-detection model can be dropped in exactly the same way later.

Model attribution: the bundled ``data/silero_vad.onnx`` is the Silero VAD model,
Copyright (c) Silero Team, licensed MIT (https://github.com/snakers4/silero-vad).
See the adjacent ``data/silero_vad.onnx.NOTICE`` and the repository ``NOTICE``.
The segmentation logic below is a numpy re-implementation of silero-vad's
``get_speech_timestamps`` (MIT) that reproduces its exact default parameters and
hysteresis; it is verified byte-for-byte against the upstream library on the
bundled fixtures (see tests/test_neural_onnx_equivalence.py).

VERIFIED against the real model (silero-vad ONNX, CPU). Properties that hold,
measured in this repo (see METHODOLOGY.md, "Optional neural cross-check
(non-reference)"):
  * Equivalence: the direct-onnxruntime path reproduces the upstream silero-vad
    pipeline byte-for-byte -- identical per-window speech probabilities,
    identical speech segments, and the identical projected per-hop ``active``
    track -- on the bundled fixtures (tests/test_neural_onnx_equivalence.py).
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
    16000 Hz (decimated to 16000 Hz here, exactly as silero-vad does). Anything
    else is rejected with an actionable error below; the energy backend measures
    at any rate.
"""

from __future__ import annotations

from typing import Callable, List

from ._engine.vad import BackendUnavailable

# --- direct-onnxruntime Silero VAD ------------------------------------------
#
# These module-level constants and helpers reproduce silero-vad's ONNX inference
# and its ``get_speech_timestamps`` post-processing WITHOUT importing silero-vad
# or torch. The math is identical: the forward pass is the same ONNX graph on the
# same bundled weights, and the segmentation is a faithful numpy port of the
# upstream state machine with its documented defaults. Keeping numpy the only
# runtime dependency of this path is the entire point of the module.

# Silero VAD default get_speech_timestamps parameters (upstream, verified):
_THRESHOLD = 0.5
_MIN_SPEECH_DURATION_MS = 250
_MIN_SILENCE_DURATION_MS = 100
_SPEECH_PAD_MS = 30
_MIN_SILENCE_AT_MAX_SPEECH_MS = 98
_MAX_SPEECH_DURATION_S = float("inf")
_USE_MAX_POSS_SIL_AT_MAX_SPEECH = True


def _model_path() -> str:
    """Absolute path to the bundled Silero VAD ONNX weights (ships in the wheel
    via package-data and in the sdist via MANIFEST.in)."""
    from importlib import resources

    return str(resources.files("hotato").joinpath("data", "silero_vad.onnx"))


# Cache the onnxruntime session across calls within a process: loading the graph
# is the expensive step, and the backend factory may be resolved once and reused.
_SESSION = None


def _load_session():
    global _SESSION
    if _SESSION is None:
        import onnxruntime as ort

        # Match silero-vad's OnnxWrapper session options exactly (single-threaded,
        # CPU) so the forward pass is bit-identical, not merely close.
        opts = ort.SessionOptions()
        opts.inter_op_num_threads = 1
        opts.intra_op_num_threads = 1
        _SESSION = ort.InferenceSession(
            _model_path(), providers=["CPUExecutionProvider"], sess_options=opts
        )
    return _SESSION


def _speech_probs(audio, sampling_rate, session):
    """Per-window Silero speech probabilities, reproducing silero-vad's windowing
    and OnnxWrapper.__call__ (context prepend + carried LSTM state) exactly.

    Returns ``(probs, decimated_length, window_size, step, work_sr)`` where
    ``probs`` is a python ``list[float]`` (one probability per window), and the
    remaining values are what the segmentation state machine needs. The audio is
    decimated to 16000 Hz for integer multiples of 16000, exactly as
    ``get_speech_timestamps`` does (segment coordinates are rescaled by ``step``
    at the end so callers see ORIGINAL sample coordinates).
    """
    import numpy as np

    audio = np.asarray(audio, dtype=np.float32).reshape(-1)
    if sampling_rate > 16000 and sampling_rate % 16000 == 0:
        step = sampling_rate // 16000
        work_sr = 16000
        audio = audio[::step]
    else:
        step = 1
        work_sr = int(sampling_rate)

    window = 512 if work_sr == 16000 else 256
    context_size = 64 if work_sr == 16000 else 32
    n = len(audio)

    state = np.zeros((2, 1, 128), dtype=np.float32)
    context = np.zeros((1, context_size), dtype=np.float32)
    sr_arr = np.array(work_sr, dtype=np.int64)
    probs: List[float] = []
    for start in range(0, n, window):
        chunk = audio[start : start + window]
        if len(chunk) < window:
            chunk = np.pad(chunk, (0, int(window - len(chunk))))
        x = chunk.reshape(1, window).astype(np.float32)
        x_cat = np.concatenate([context, x], axis=1)
        out, state = session.run(None, {"input": x_cat, "state": state, "sr": sr_arr})
        context = x_cat[..., -context_size:]
        probs.append(float(out.reshape(-1)[0]))
    return probs, n, window, step, work_sr


def _speech_timestamps_from_probs(
    speech_probs,
    audio_length_samples,
    window_size_samples,
    step,
    sampling_rate,
    threshold: float = _THRESHOLD,
    min_speech_duration_ms: int = _MIN_SPEECH_DURATION_MS,
    max_speech_duration_s: float = _MAX_SPEECH_DURATION_S,
    min_silence_duration_ms: int = _MIN_SILENCE_DURATION_MS,
    speech_pad_ms: int = _SPEECH_PAD_MS,
    neg_threshold: float = None,
    min_silence_at_max_speech: int = _MIN_SILENCE_AT_MAX_SPEECH_MS,
    use_max_poss_sil_at_max_speech: bool = _USE_MAX_POSS_SIL_AT_MAX_SPEECH,
):
    """Faithful numpy port of silero-vad's ``get_speech_timestamps`` post-processing.

    Consumes the per-window ``speech_probs`` and reproduces silero's hysteresis
    (``threshold`` / ``neg_threshold``), minimum-speech / minimum-silence
    durations, max-speech splitting, and the two-sided ``speech_pad_ms`` padding +
    adjacent-segment stitching -- with the upstream defaults. Returns a list of
    ``{"start": int, "end": int}`` in ORIGINAL sample coordinates (rescaled by
    ``step`` when the input was a 16000 Hz multiple). Byte-for-byte equivalent to
    upstream (tests/test_neural_onnx_equivalence.py).
    """
    min_speech_samples = sampling_rate * min_speech_duration_ms / 1000
    speech_pad_samples = sampling_rate * speech_pad_ms / 1000
    max_speech_samples = (
        sampling_rate * max_speech_duration_s - window_size_samples - 2 * speech_pad_samples
    )
    min_silence_samples = sampling_rate * min_silence_duration_ms / 1000
    min_silence_samples_at_max_speech = sampling_rate * min_silence_at_max_speech / 1000

    if neg_threshold is None:
        neg_threshold = max(threshold - 0.15, 0.01)

    triggered = False
    speeches = []
    current_speech = {}
    temp_end = 0
    prev_end = next_start = 0
    possible_ends = []

    for i, speech_prob in enumerate(speech_probs):
        cur_sample = window_size_samples * i

        # Speech returns after a candidate silence: record the silence if long
        # enough, and clear the pending end.
        if (speech_prob >= threshold) and temp_end:
            sil_dur = cur_sample - temp_end
            if sil_dur > min_silence_samples_at_max_speech:
                possible_ends.append((temp_end, sil_dur))
            temp_end = 0
            if next_start < prev_end:
                next_start = cur_sample

        # Start of speech.
        if (speech_prob >= threshold) and not triggered:
            triggered = True
            current_speech["start"] = cur_sample
            continue

        # Max speech length reached: decide where to cut (inert at the default
        # max_speech_duration_s=inf, ported faithfully for non-default callers).
        if triggered and (cur_sample - current_speech["start"] > max_speech_samples):
            if use_max_poss_sil_at_max_speech and possible_ends:
                prev_end, dur = max(possible_ends, key=lambda x: x[1])
                current_speech["end"] = prev_end
                speeches.append(current_speech)
                current_speech = {}
                next_start = prev_end + dur
                if next_start < prev_end + cur_sample:
                    current_speech["start"] = next_start
                else:
                    triggered = False
                prev_end = next_start = temp_end = 0
                possible_ends = []
            else:
                if prev_end:
                    current_speech["end"] = prev_end
                    speeches.append(current_speech)
                    current_speech = {}
                    if next_start < prev_end:
                        triggered = False
                    else:
                        current_speech["start"] = next_start
                    prev_end = next_start = temp_end = 0
                    possible_ends = []
                else:
                    current_speech["end"] = cur_sample
                    speeches.append(current_speech)
                    current_speech = {}
                    prev_end = next_start = temp_end = 0
                    triggered = False
                    possible_ends = []
                    continue

        # Silence detection while in speech.
        if (speech_prob < neg_threshold) and triggered:
            if not temp_end:
                temp_end = cur_sample
            sil_dur_now = cur_sample - temp_end
            if not use_max_poss_sil_at_max_speech and sil_dur_now > min_silence_samples_at_max_speech:
                prev_end = temp_end
            if sil_dur_now < min_silence_samples:
                continue
            else:
                current_speech["end"] = temp_end
                if (current_speech["end"] - current_speech["start"]) > min_speech_samples:
                    speeches.append(current_speech)
                current_speech = {}
                prev_end = next_start = temp_end = 0
                triggered = False
                possible_ends = []
                continue

    if current_speech and (audio_length_samples - current_speech["start"]) > min_speech_samples:
        current_speech["end"] = audio_length_samples
        speeches.append(current_speech)

    # Two-sided padding + adjacent-segment stitching (upstream, verbatim).
    for i, speech in enumerate(speeches):
        if i == 0:
            speech["start"] = int(max(0, speech["start"] - speech_pad_samples))
        if i != len(speeches) - 1:
            silence_duration = speeches[i + 1]["start"] - speech["end"]
            if silence_duration < 2 * speech_pad_samples:
                speech["end"] += int(silence_duration // 2)
                speeches[i + 1]["start"] = int(
                    max(0, speeches[i + 1]["start"] - silence_duration // 2)
                )
            else:
                speech["end"] = int(min(audio_length_samples, speech["end"] + speech_pad_samples))
                speeches[i + 1]["start"] = int(
                    max(0, speeches[i + 1]["start"] - speech_pad_samples)
                )
        else:
            speech["end"] = int(min(audio_length_samples, speech["end"] + speech_pad_samples))

    if step > 1:
        for speech_dict in speeches:
            speech_dict["start"] *= step
            speech_dict["end"] *= step
    return speeches


def _get_speech_timestamps(audio, sampling_rate, session):
    """Direct-onnxruntime equivalent of silero-vad's ``get_speech_timestamps``.

    Runs the bundled ONNX model over ``audio`` and returns speech segments as a
    list of ``{"start": int, "end": int}`` in ORIGINAL sample coordinates, using
    silero's default parameters. No torch, no silero-vad import.
    """
    probs, alen, window, step, work_sr = _speech_probs(audio, sampling_rate, session)
    return _speech_timestamps_from_probs(probs, alen, window, step, work_sr)


def build_silero_backend() -> Callable[[List[float], int, float, int], List[bool]]:
    """Factory for the Silero-VAD per-frame activity function.

    Returns ``activity(samples, sample_rate, hop_sec, n_frames) -> List[bool]``
    of length ``n_frames`` aligned to the energy VAD's hop grid. Raises
    ``BackendUnavailable`` (never a bare ImportError, never a silent energy
    fallback) if the optional ``[neural]`` extra is not installed, or if the
    packaged model weights cannot be loaded (a broken or partial install).

    The backend runs the bundled Silero VAD ONNX weights DIRECTLY on onnxruntime
    (CPU) with a numpy re-implementation of silero-vad's segmentation, so the
    ``[neural]`` extra needs only ``onnxruntime`` + ``numpy`` -- no torch.

    This factory is registered lazily (see ``hotato.__init__``): it is only
    *called* -- and thus only imports/loads the model -- the first time
    ``--backend neural`` is actually requested. The zero-dependency energy path
    never reaches this code.
    """
    try:
        import numpy as np  # noqa: F401  # float32 waveforms + segmentation math
        import onnxruntime  # noqa: F401  # direct ONNX inference (no torch/silero-vad)
    except Exception as exc:  # ImportError, or a partial/broken install
        raise BackendUnavailable(
            "the 'neural' VAD backend requires the optional extra: "
            "pip install 'hotato[neural]'  (missing dependency: "
            f"{exc}). The energy backend stays the reproducible reference; "
            "nothing falls back to it silently."
        ) from exc

    try:
        session = _load_session()
    except Exception as exc:  # broken install / unreadable packaged weights
        raise BackendUnavailable(
            "the 'neural' extra is installed but the Silero VAD model could not "
            "be loaded. The ONNX weights ship inside this package, so this "
            "usually means a broken or partial install; reinstall with "
            "pip install --force-reinstall 'hotato[neural]'. "
            f"(underlying: {exc})"
        ) from exc

    def _activity(samples, sample_rate, hop_sec, n_frames):
        import numpy as np

        sr = int(sample_rate)
        # Mirror Silero's supported-rate contract up front so an unsupported
        # recording fails with an actionable message instead of a model-internal
        # error. Multiples of 16 kHz are decimated to 16 kHz internally and
        # timestamps are returned in the ORIGINAL sample coordinates (verified),
        # so those pass through untouched.
        if sr not in (8000, 16000) and not (sr > 16000 and sr % 16000 == 0):
            raise ValueError(
                f"the neural (Silero) backend supports 8000 Hz, 16000 Hz, and "
                f"integer multiples of 16000 Hz; this recording is {sr} Hz. "
                "Resample it first, e.g. "
                "ffmpeg -i in.wav -ar 16000 out.wav, or score it with the "
                "energy backend (the reference), which measures at any rate."
            )
        wav = np.asarray(samples, dtype=np.float32)
        segments = _get_speech_timestamps(wav, sr, session)
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
