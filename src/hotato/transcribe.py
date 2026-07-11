"""Optional, NON-REFERENCE transcript CONTEXT layer (faster-whisper / CTranslate2).

Hotato's published/golden/bundled numbers -- did_yield, talk_over_sec,
time_to_yield, every timing verdict -- come from the deterministic energy VAD
over the audio waveform (see ``_engine.vad.energy_vad``). This module adds
NOTHING to that computation. It runs an off-the-shelf speech-to-text model over
the same recording and hands back plain text with per-segment timestamps, so a
human (or an agent) reading a report can see WHAT was said next to WHEN the
timing engine says it was said. It is opt-in (``--transcribe`` / the
``[transcribe]`` extra), mirrors the ``neural.py`` / ``diarize.py`` seam
exactly, and is wired nowhere near the scorer: nothing in ``_engine`` imports
this module, and nothing in this module is imported by the scorer.

What it changes, honestly:
  * It gives you TEXT next to a timestamp. That is the entire feature.
  * It does NOT improve, refine, retime, or cross-check the timing measurement.
    A transcript word boundary is not a voice-activity boundary; ASR word
    timestamps are themselves a model estimate, not ground truth, and are never
    substituted for the energy VAD's frame-level activity. No WER, accuracy, or
    quality number is claimed for the model here or anywhere in hotato.
  * Adding ``--transcribe`` to a run must produce byte-identical timing numbers
    to the same run without it: this module is read-only with respect to the
    score, never mutating or re-deriving any timing/verdict field.

Primary target: faster-whisper (MIT), a CTranslate2 (MIT) re-implementation of
OpenAI's Whisper (weights MIT), run locally / fully offline once the chosen
model is cached. ``device="auto"`` picks ``cuda`` if CTranslate2 reports a CUDA
device, else ``cpu``; ``compute_type`` defaults to ``float16`` on GPU and
``int8`` on CPU (CTranslate2's documented CPU-friendly quantization), each
overridable.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Sequence

from ._engine.vad import BackendUnavailable
from .errors import require_regular_file

__all__ = [
    "TranscriptSegment",
    "Transcript",
    "transcribe",
    "align_transcript_to_events",
]


@dataclass
class TranscriptSegment:
    """One ASR segment: a time span plus the text spoken in it.

    ``words``, when requested (``word_timestamps=True``) and the model exposes
    them, is a list of ``{"start", "end", "word"}`` dicts on the same
    time base as ``start``/``end``; ``None`` when word timestamps were not
    requested or the model did not return any for this segment."""

    start: float
    end: float
    text: str
    words: Optional[List[Dict[str, Any]]] = None


@dataclass
class Transcript:
    """The whole-recording transcript: full text plus timed segments.

    ``language`` is the model's own language guess (``None`` if unavailable).
    ``model`` / ``device`` / ``compute_type`` are provenance, stamped for
    reproducibility -- the same fields a human would need to reproduce this
    exact transcript, not anything the scorer reads."""

    text: str
    segments: List[TranscriptSegment] = field(default_factory=list)
    language: Optional[str] = None
    model: str = "unknown"
    device: str = "unknown"
    compute_type: str = "unknown"


def _resolve_device(device: str) -> str:
    """``device="auto"`` -> ``"cuda"`` if CTranslate2 reports a CUDA device,
    else ``"cpu"``. Any other value passes through unchanged (so ``"cpu"`` /
    ``"cuda"`` explicitly requested are never second-guessed)."""
    if device != "auto":
        return device
    try:
        import ctranslate2

        if ctranslate2.get_cuda_device_count() > 0:
            return "cuda"
    except Exception:
        # No CTranslate2, or a build without CUDA support: cpu is always safe.
        pass
    return "cpu"


def _load_model(model: str, device: str, compute_type: str):
    """Import + construct the faster-whisper model, translating any failure
    (missing extra, or a broken/partial install, or a bad device/compute_type
    combination) into a clean ``BackendUnavailable`` -- never a bare
    ImportError, never a fallback to a different backend or to skipping the
    transcript silently."""
    try:
        from faster_whisper import WhisperModel
    except Exception as exc:  # ImportError, or a partial/broken install
        raise BackendUnavailable(
            "transcription requires the optional extra: "
            "pip install 'hotato[transcribe]'  (missing dependency: "
            f"{exc}). A transcript is an optional CONTEXT aid; hotato's timing "
            "score (did_yield / talk_over_sec / time_to_yield) is computed by "
            "the energy VAD and is completely unaffected whether or not this "
            "extra is installed."
        ) from exc

    try:
        return WhisperModel(model, device=device, compute_type=compute_type)
    except Exception as exc:
        raise BackendUnavailable(
            "the '[transcribe]' extra is installed but faster-whisper could not "
            f"load model {model!r} on device {device!r} with compute_type "
            f"{compute_type!r}. This is usually a bad model name, an unavailable "
            "device, or a first-run download failure. (underlying: "
            f"{exc})"
        ) from exc


def transcribe(
    path,
    model: str = "base.en",
    device: str = "auto",
    *,
    compute_type: Optional[str] = None,
    word_timestamps: bool = False,
    vad_filter: bool = False,
    language: Optional[str] = None,
) -> Transcript:
    """Transcribe one audio file with faster-whisper (CTranslate2), lazily.

    ``faster_whisper`` is imported ONLY inside this call -- never at module
    import time -- so importing ``hotato.transcribe`` costs nothing and the
    ``[transcribe]`` extra stays strictly opt-in. Absent the extra (or a broken
    install), raises :class:`~hotato._engine.vad.BackendUnavailable` naming
    ``pip install 'hotato[transcribe]'``; never falls back to any other
    behaviour.

    ``path`` is guarded with :func:`hotato.errors.require_regular_file` before
    any read, so a FIFO/named pipe/socket raises a clean, immediate error
    instead of blocking the process forever waiting on a writer.

    ``device="auto"`` (the default) picks ``"cuda"`` if CTranslate2 reports a
    CUDA device, else ``"cpu"``. ``compute_type`` defaults to ``"float16"`` on
    GPU and ``"int8"`` on CPU (both overridable); these are CTranslate2's
    documented fast/accurate defaults for each device, not an accuracy claim.

    Returns a :class:`Transcript`: ``.text`` is the whole-recording text,
    ``.segments`` is the list of timed :class:`TranscriptSegment` (optionally
    carrying ``.words`` when ``word_timestamps=True``). This function never
    touches, computes, or is called by anything in ``_engine`` -- it produces
    CONTEXT only; see :func:`align_transcript_to_events` to attach it to
    already-scored events without changing them."""
    require_regular_file(path)

    resolved_device = _resolve_device(device)
    resolved_compute_type = compute_type or ("float16" if resolved_device == "cuda" else "int8")

    wm = _load_model(model, resolved_device, resolved_compute_type)

    segments_iter, info = wm.transcribe(
        str(path),
        word_timestamps=word_timestamps,
        vad_filter=vad_filter,
        language=language,
    )

    segments: List[TranscriptSegment] = []
    parts: List[str] = []
    for seg in segments_iter:
        text = (seg.text or "").strip()
        words = None
        raw_words = getattr(seg, "words", None)
        if word_timestamps and raw_words:
            words = [
                {"start": float(w.start), "end": float(w.end), "word": w.word}
                for w in raw_words
            ]
        segments.append(
            TranscriptSegment(
                start=float(seg.start),
                end=float(seg.end),
                text=text,
                words=words,
            )
        )
        if text:
            parts.append(text)

    return Transcript(
        text=" ".join(parts).strip(),
        segments=segments,
        language=getattr(info, "language", None),
        model=model,
        device=resolved_device,
        compute_type=resolved_compute_type,
    )


# --------------------------------------------------------------------------- #
# Pure context-alignment helper: read-only with respect to timing/verdicts.
# --------------------------------------------------------------------------- #

def _event_span(event: Dict[str, Any]) -> Optional[tuple]:
    """Read the ``(start, end)`` seconds span off one event dict, in hotato's
    existing ``start_sec``/``end_sec``/``time_sec`` convention (see
    ``trace.py``). A point event (``time_sec`` only) is treated as a
    zero-width instant at that time. Returns ``None`` when neither shape is
    present. READ-ONLY: this never writes back into ``event``."""
    start = event.get("start_sec")
    end = event.get("end_sec")
    if start is not None and end is not None:
        return float(start), float(end)
    t = event.get("time_sec")
    if t is not None:
        return float(t), float(t)
    return None


def align_transcript_to_events(
    transcript: Transcript, events: Sequence[Dict[str, Any]]
) -> List[Dict[str, Any]]:
    """Attach the overlapping transcript span to each scored event as CONTEXT.

    Pure and read-only: returns a NEW list of NEW dicts (each a shallow copy of
    the input event, plus exactly one added key, ``"transcript_context"``).
    Every existing key on an event -- timing (``start_sec``/``end_sec``/
    ``time_sec``), verdict (``did_yield``, ``talk_over_sec``, or anything
    else), or otherwise -- is copied through UNCHANGED; the only timing fields
    this function reads are the ones above, and only to compute overlap, never
    to recompute or alter them. The input ``events`` sequence and its dicts are
    never mutated.

    An event with neither a ``start_sec``/``end_sec`` pair nor a ``time_sec``
    gets an empty context (``text: ""``, ``segments: []``) rather than being
    skipped, so the output has the same length and order as the input.

    ``transcript_context`` is ``{"text": str, "segments": [{"start", "end",
    "text"}, ...]}`` -- the transcript segments whose span overlaps the
    event's span (inclusive touch counts as overlap), concatenated in
    transcript order.

    This is a labelling convenience for a report or a human/agent reading it
    next to a transcript. It NEVER feeds back into scoring: nothing here
    recomputes ``did_yield``, ``talk_over_sec``, or any other verdict/timing
    field, and nothing in ``_engine`` calls this function -- a caller who wants
    the context wires it in downstream, strictly after scoring is already
    final."""
    aligned: List[Dict[str, Any]] = []
    for event in events:
        span = _event_span(event)
        matches: List[TranscriptSegment] = []
        if span is not None:
            e_start, e_end = span
            for seg in transcript.segments:
                if seg.end >= e_start and seg.start <= e_end:
                    matches.append(seg)
        new_event = dict(event)
        new_event["transcript_context"] = {
            "text": " ".join(m.text for m in matches if m.text).strip(),
            "segments": [
                {"start": m.start, "end": m.end, "text": m.text} for m in matches
            ],
        }
        aligned.append(new_event)
    return aligned
