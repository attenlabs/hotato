"""Energy-based voice activity detection.

This is intentionally simple and fully transparent: per-frame RMS in dBFS,
a noise-floor estimate from the quiet tail of the distribution, a relative
threshold above that floor, and a hangover so short gaps between words do
not fragment a single utterance. It is not a speech model and makes no
accuracy claim. It measures energy over time, which is all the three
barge-in signals need. Every parameter is exposed so you can tune it to
your own recordings.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import List

from .audio import to_dbfs


@dataclass
class VADParams:
    rel_db: float = 15.0          # threshold this many dB above the noise floor
    abs_gate_db: float = -60.0    # never treat anything below this as active
    hangover_sec: float = 0.15    # keep "active" this long after energy drops
    noise_percentile: float = 0.10  # quietest fraction used to estimate the floor
    dyn_margin_db: float = 22.0   # if a channel is almost never quiet, keep the
                                  # threshold at least this far below its loudest
                                  # frames so speech still registers as active
    backend: str = "energy"       # which VAD produces `active`: "energy" (default,
                                  # the deterministic REFERENCE that yields every
                                  # published/golden/bundled number) or "neural" (an
                                  # OPTIONAL, explicitly NON-REFERENCE model cross-check
                                  # -- see neural_vad + register_neural_backend). Adding
                                  # this field changes no energy result: the energy path
                                  # never reads it, and the default keeps it at "energy".


@dataclass
class VADResult:
    active: List[bool]
    hop_sec: float
    threshold_db: float
    noise_floor_db: float


def _percentile(sorted_vals: List[float], q: float) -> float:
    if not sorted_vals:
        return 0.0
    idx = int(q * (len(sorted_vals) - 1))
    return sorted_vals[idx]


def energy_vad(rms: List[float], hop_sec: float, params: VADParams = None) -> VADResult:
    if params is None:
        params = VADParams()
    db = to_dbfs(rms)
    noise_floor = _percentile(sorted(db), params.noise_percentile)
    threshold = max(noise_floor + params.rel_db, params.abs_gate_db)

    # Robustness for channels that are almost never silent (for example an
    # agent that talks the whole clip): the percentile lands inside speech and
    # would push the threshold above the speech itself, hiding all activity.
    # When there is clearly loud content, keep the threshold a fixed margin
    # below the loudest frames so that content still registers. This never
    # rescues a genuinely silent channel because the guard requires loud
    # content to exist above the absolute gate.
    max_db = max(db) if db else params.abs_gate_db
    cap = max_db - params.dyn_margin_db
    if cap > params.abs_gate_db:
        threshold = min(threshold, cap)

    raw = [d >= threshold for d in db]

    hang_frames = max(0, int(round(params.hangover_sec / hop_sec)))
    active = [False] * len(raw)
    countdown = 0
    for i, a in enumerate(raw):
        if a:
            countdown = hang_frames
            active[i] = True
        elif countdown > 0:
            countdown -= 1
            active[i] = True
    return VADResult(
        active=active,
        hop_sec=hop_sec,
        threshold_db=threshold,
        noise_floor_db=noise_floor,
    )


def first_active_sec(active: List[bool], hop_sec: float, min_run_sec: float = 0.05) -> float:
    """Time of the first sustained active run, or -1 if the channel is silent."""
    min_frames = max(1, int(round(min_run_sec / hop_sec)))
    run = 0
    for i, a in enumerate(active):
        if a:
            run += 1
            if run >= min_frames:
                return (i - min_frames + 1) * hop_sec
        else:
            run = 0
    return -1.0


# --- optional, NON-REFERENCE neural backend seam --------------------------
#
# `energy_vad` above is the deterministic REFERENCE: every published, golden,
# and bundled number this engine reports comes from it, byte-for-byte. The
# neural backend below is OPTIONAL and, by construction, a NON-REFERENCE
# cross-check. It exists to answer one objection -- "this is just energy VAD,
# rebuildable in a weekend" -- by letting the SAME timing math run over a
# learned, model-backed speech track and be compared against the energy track.
#
# Honesty (binds this whole seam): a neural VAD TIGHTENS onset precision on clean
# speech, but it does NOT close the energy-vs-intent gap. A cough, a laugh, a
# door slam, or crosstalk still carries speech-band energy, and a VAD -- energy
# OR neural -- can mark it active. Whether a sound is a genuine bid for the
# conversational turn is not decidable from one channel's activity alone. No
# accuracy number is claimed for either backend, here or anywhere.
#
# This engine keeps ZERO third-party dependencies: it never imports a model. A
# concrete backend (e.g. Silero VAD, MIT) is *injected* by the hosting package
# or a test via `register_neural_backend`, and resolved LAZILY -- nothing here
# imports or runs a model until backend="neural" is actually requested -- so the
# energy path is completely untouched by this code merely existing.


class BackendUnavailable(RuntimeError):
    """A non-energy VAD backend was requested but is not available.

    Raised when backend="neural" is requested and either no backend has been
    registered, or the registered backend's optional dependency (its extra) is
    not installed. This is deliberately a hard, explicit error: the engine NEVER
    silently falls back to energy, because a published/reference number must
    never change identity just because an optional model happened to be missing.
    """


# A zero-arg factory returning a per-frame activity function of the shape
#   activity(samples, sample_rate, hop_sec, n_frames) -> List[bool]
# whose result is length n_frames on the SAME hop grid the energy VAD uses.
# Registered via `register_neural_backend`; resolved lazily on first neural use.
_NEURAL_FACTORY = None
_NEURAL_CACHE = None


def register_neural_backend(factory) -> None:
    """Register the optional neural VAD backend factory (or a test stub).

    ``factory`` is a zero-arg callable returning ``activity(samples,
    sample_rate, hop_sec, n_frames) -> List[bool]``. It is invoked lazily, once,
    on the first neural request; loading/importing the model MUST happen inside
    it (never at registration) so registering costs nothing and the energy path
    stays dependency-free. The factory should raise ``BackendUnavailable`` if its
    optional extra is not installed.
    """
    global _NEURAL_FACTORY, _NEURAL_CACHE
    _NEURAL_FACTORY = factory
    _NEURAL_CACHE = None


def clear_neural_backend() -> None:
    """Unregister any neural backend (restores a clean, energy-only state)."""
    global _NEURAL_FACTORY, _NEURAL_CACHE
    _NEURAL_FACTORY = None
    _NEURAL_CACHE = None


def _resolve_neural_activity():
    if _NEURAL_FACTORY is None:
        raise BackendUnavailable(
            "backend='neural' was requested but no neural VAD backend is "
            "registered. Install the optional extra (for the packaged tool: "
            "pip install 'hotato[neural]') or register one with "
            "register_neural_backend(). The energy backend is the reproducible "
            "reference and is never substituted for it silently."
        )
    global _NEURAL_CACHE
    if _NEURAL_CACHE is None:
        _NEURAL_CACHE = _NEURAL_FACTORY()  # may raise BackendUnavailable
    return _NEURAL_CACHE


def neural_vad(samples, sample_rate, rms, hop_sec, params: VADParams = None) -> VADResult:
    """OPTIONAL, non-reference model-backed VAD. Returns the IDENTICAL VADResult
    shape as ``energy_vad`` (``active``, ``hop_sec``, ``threshold_db``,
    ``noise_floor_db``) so every downstream consumer is byte-shape-compatible.

    The injected model decides ``active`` from the waveform. The two dB
    descriptors have NO direct analog in a neural model, so they are SYNTHESIZED
    and documented as such: they are the energy-domain description of the SAME
    audio (its measured noise floor, and the energy threshold that floor would
    imply), reported for inspection/provenance only. They did NOT produce the
    neural ``active`` decision -- that is a learned speech probability, not a dB
    crossing. ``active`` is aligned to the energy track's hop grid and length.
    """
    if params is None:
        params = VADParams()
    activity = _resolve_neural_activity()
    n = len(rms)
    active = list(activity(samples, sample_rate, hop_sec, n))
    # Contract: identical length to the energy track on the same hop grid. A
    # backend must map onto that grid; align defensively rather than emit a
    # different-length track that would silently corrupt frame indexing.
    if len(active) != n:
        active = (active + [False] * n)[:n]
    active = [bool(a) for a in active]
    # Synthesized energy-domain descriptors of the same audio (see docstring):
    # computed with the SAME formula energy_vad uses, so the numbers are real,
    # finite, and inspectable -- never a claim that they drove the neural gate.
    db = to_dbfs(rms)
    noise_floor = _percentile(sorted(db), params.noise_percentile)
    threshold = max(noise_floor + params.rel_db, params.abs_gate_db)
    max_db = max(db) if db else params.abs_gate_db
    cap = max_db - params.dyn_margin_db
    if cap > params.abs_gate_db:
        threshold = min(threshold, cap)
    return VADResult(
        active=active,
        hop_sec=hop_sec,
        threshold_db=threshold,
        noise_floor_db=noise_floor,
    )
