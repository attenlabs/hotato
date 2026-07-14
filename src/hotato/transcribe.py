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

Reproducibility, stated precisely (never oversold, mirrors ``hotato.rubric``'s
posture on its model judge): ``transcribe()`` is NOT claimed bit-for-bit
deterministic across a machine/library/CTranslate2-build change, and
faster-whisper can escalate a hard-to-decode segment from its default
temperature-0 first pass to temperature > 0 on low confidence. What IS
deterministic is REPLAY: :func:`transcribe_cached` content-addresses every
call by ``sha256(model:device:compute_type:language:word_timestamps:
vad_filter\\n sha256(audio bytes))`` (reusing :class:`hotato.fleet.store.
ArtifactStore`, exactly like :class:`hotato.rubric.VerdictCache`); a cache hit
replays the byte-identical stored :class:`Transcript` and skips the model
entirely. ``no_cache=True`` (the CLI's ``--no-transcribe-cache``) always
re-transcribes fresh and DIFFS it against any cached baseline, surfacing drift
-- never silently overwriting, never hiding a mismatch. The cache is purely
advisory provenance around the timing path, never a gate: attaching it never
changes ``did_yield`` / ``talk_over_sec`` / ``time_to_yield`` or any other
verdict/measurement field.
"""

from __future__ import annotations

import difflib
import hashlib
import json
import os
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Sequence

from ._engine.vad import BackendUnavailable
from .errors import open_regular as _open_regular
from .errors import require_regular_file
from .manifest import _sha256_str as _sha256_text
from .manifest import canonical_json as _canonical

__all__ = [
    "TranscriptSegment",
    "Transcript",
    "transcribe",
    "align_transcript_to_events",
    "TranscriptCache",
    "CachedTranscribeResult",
    "transcribe_cached",
    "transcript_cache_key",
    "default_transcript_cache_dir",
    "build_transcript_cache",
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


# =========================================================================
# Content-addressed transcript cache (mirrors hotato.rubric.VerdictCache)
# + verify-by-diff. Advisory provenance only -- never a gate, never on the
# timing/verdict path (see the module docstring's reproducibility note).
# =========================================================================

_DIFF_SUMMARY_MAX_LINES = 40


def _stream_sha256(path) -> str:
    """Streamed sha256 of the raw audio bytes, in fixed-size chunks -- the
    same shape ``core.py``'s own audio-provenance hash uses, kept local here
    so ``hotato.transcribe`` never imports ``hotato.core`` (avoiding a
    cycle; ``core.py`` already imports this module lazily)."""
    h = hashlib.sha256()
    # open-ok: callers guard `path` with require_regular_file before reaching
    # here (transcribe_cached does so up front).
    with _open_regular(path) as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def transcript_cache_key(
    *, model: str, device: str, compute_type: str, language: Optional[str],
    word_timestamps: bool, vad_filter: bool, audio_sha256: str,
) -> str:
    """Content address of one transcription call: every parameter that can
    change the output (model, the RESOLVED device/compute_type -- not the
    literal ``"auto"``, since that is not itself determinism-affecting --
    language, word_timestamps, vad_filter) plus the exact audio bytes.
    Mirrors ``hotato.rubric``'s ``cache_key = sha256(provider:model +
    prompt_sha256 + input_sha256)`` formula."""
    key_text = (
        f"{model}:{device}:{compute_type}:{language}:{word_timestamps}:"
        f"{vad_filter}\n{audio_sha256}"
    )
    return _sha256_text(key_text)


def _segment_to_dict(seg: TranscriptSegment) -> Dict[str, Any]:
    return {"start": seg.start, "end": seg.end, "text": seg.text, "words": seg.words}


def _segment_from_dict(d: Dict[str, Any]) -> TranscriptSegment:
    return TranscriptSegment(
        start=d["start"], end=d["end"], text=d.get("text", ""), words=d.get("words"),
    )


def _transcript_to_dict(t: Transcript) -> Dict[str, Any]:
    return {
        "text": t.text,
        "segments": [_segment_to_dict(s) for s in t.segments],
        "language": t.language,
        "model": t.model,
        "device": t.device,
        "compute_type": t.compute_type,
    }


def _transcript_from_dict(d: Dict[str, Any]) -> Transcript:
    return Transcript(
        text=d.get("text", ""),
        segments=[_segment_from_dict(s) for s in (d.get("segments") or [])],
        language=d.get("language"),
        model=d.get("model", "unknown"),
        device=d.get("device", "unknown"),
        compute_type=d.get("compute_type", "unknown"),
    )


def _diff_transcripts(cached: Dict[str, Any], fresh: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """Diff a freshly re-transcribed result against the cached baseline.
    Returns None when the two are byte-identical (same content digest), else a
    drift object naming exactly what changed -- surfaced on the result, never
    hidden. Mirrors ``hotato.rubric._diff_verdicts``."""
    c_sha = _sha256_text(_canonical(cached))
    f_sha = _sha256_text(_canonical(fresh))
    if c_sha == f_sha:
        return None
    cached_text = cached.get("text") or ""
    fresh_text = fresh.get("text") or ""
    diff_lines = list(difflib.unified_diff(
        cached_text.splitlines(), fresh_text.splitlines(),
        fromfile="cached", tofile="fresh", lineterm="",
    ))
    if diff_lines:
        shown = diff_lines[:_DIFF_SUMMARY_MAX_LINES]
        if len(diff_lines) > _DIFF_SUMMARY_MAX_LINES:
            shown.append(
                f"... ({len(diff_lines) - _DIFF_SUMMARY_MAX_LINES} more diff "
                "line(s) truncated)"
            )
        diff_summary = "\n".join(shown)
    else:
        diff_summary = (
            "transcript text is identical; a non-text field (segment timing, "
            "language, model, device, or compute_type) differs"
        )
    return {
        "changed": True,
        "cached_sha256": c_sha,
        "fresh_sha256": f_sha,
        "diff_summary": diff_summary,
        "note": (
            "the fresh transcript differs from the cached one -- faster-whisper "
            "is not claimed bit-for-bit deterministic (a low-confidence segment "
            "can fall back to temperature > 0 on noisy audio, or the model/"
            "device/library build changed); only cached replay is byte-identical"
        ),
    }


class TranscriptCache:
    """A content-addressed transcript cache, mirroring
    :class:`hotato.rubric.VerdictCache` exactly: the serialized
    :class:`Transcript` BLOB is stored via
    :class:`hotato.fleet.store.ArtifactStore` (sha256 of its bytes ->
    integrity, de-dup, ``verify``); a thin key index maps ``cache_key`` (the
    content address of model+device+compute_type+language+word_timestamps+
    vad_filter+audio bytes, see :func:`transcript_cache_key`) to that blob
    digest. A hit returns the byte-identical stored transcript and SKIPS the
    model."""

    def __init__(self, root: str):
        from .fleet.store import ArtifactStore
        self.root = os.path.abspath(root)
        self.store = ArtifactStore(os.path.join(self.root, "store"))
        self.index_dir = os.path.join(self.root, "keys")
        os.makedirs(self.index_dir, exist_ok=True)

    def _key_path(self, cache_key: str) -> str:
        sub = os.path.join(self.index_dir, cache_key[:2])
        os.makedirs(sub, exist_ok=True)
        return os.path.join(sub, cache_key)

    def get(self, cache_key: str) -> Optional[Dict[str, Any]]:
        path = self._key_path(cache_key)
        if not os.path.isfile(path):
            return None
        try:
            with open(path, encoding="utf-8") as fh:  # open-ok: our own index file
                digest = fh.read().strip()
        except OSError:
            return None
        # A corrupted index file could hold a non-canonical digest; the store
        # rejects those with ValueError, so treat it as a cache miss (return
        # None) rather than crashing the read.
        try:
            if not digest or not self.store.has(digest):
                return None
            if not self.store.verify(digest):
                return None
            return self.store.get_json(digest)
        except ValueError:
            return None

    def put(self, cache_key: str, transcript_dict: Dict[str, Any]) -> str:
        stored = json.loads(_canonical(transcript_dict))
        digest = self.store.put_json(stored, kind="transcript")
        tmp = self._key_path(cache_key) + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:  # open-ok: our own index file
            fh.write(digest)
        os.replace(tmp, self._key_path(cache_key))
        return digest


def default_transcript_cache_dir() -> str:
    return os.path.join(os.path.expanduser("~"), ".hotato", "transcribe-cache")


def build_transcript_cache(cache_dir: Optional[str] = None):
    """Construct a :class:`TranscriptCache`, gracefully degrading to
    ``(None, warning)`` when the DEFAULT cache location is unwritable --
    mirrors ``hotato.cli._build_cache`` (the rubric verdict cache's own
    graceful-degrade). An EXPLICITLY requested ``cache_dir`` is a persistence
    REQUEST and is never degraded: an ``OSError`` there propagates so a
    replay/drift baseline is never silently discarded.

    Returns ``(cache_or_None, warning_or_None)``; the caller decides how to
    surface the warning (the CLI prints it to stderr, MCP attaches it as
    advisory provenance on the response)."""
    explicit = cache_dir is not None
    resolved = cache_dir or default_transcript_cache_dir()
    try:
        return TranscriptCache(resolved), None
    except OSError as exc:
        if explicit:
            raise
        return None, (
            "the default transcript cache is unavailable "
            f"({resolved}: {exc}); continuing without transcript caching."
        )


@dataclass
class CachedTranscribeResult:
    """The outcome of one :func:`transcribe_cached` call: the
    :class:`Transcript` itself, plus advisory cache provenance (never a gate).
    ``cached`` is True only on a byte-identical replay that skipped the model
    entirely. ``drift`` is populated only when ``no_cache=True`` forced a
    fresh re-transcription AND it differs from the cached baseline; ``None``
    on a normal cache hit, a normal fresh miss (no baseline yet), or a
    ``no_cache`` re-query that reproduced the same transcript."""

    transcript: Transcript
    cache_key: str
    cached: bool
    drift: Optional[Dict[str, Any]] = None


def transcribe_cached(
    path,
    model: str = "base.en",
    device: str = "auto",
    *,
    compute_type: Optional[str] = None,
    word_timestamps: bool = False,
    vad_filter: bool = False,
    language: Optional[str] = None,
    cache: Optional[TranscriptCache] = None,
    no_cache: bool = False,
) -> CachedTranscribeResult:
    """The cache-aware entry point every call site (``core.run_single``,
    ``hotato assert run --transcribe``, the MCP ``voice_eval_run`` tool)
    uses instead of calling :func:`transcribe` directly.

    * ``cache=None`` (the default) -- caching is off entirely; behaves exactly
      like calling :func:`transcribe` directly (``cached=False``,
      ``drift=None``), just with a cache_key computed for free.
    * A cache HIT (and not ``no_cache``) -- returns the byte-identical stored
      Transcript, SKIPPING the model call entirely.
    * ``no_cache=True`` -- ALWAYS re-transcribes fresh. If a cached baseline
      exists, the fresh Transcript is DIFFED against it and the drift is
      surfaced on the result (never silently overwritten, never hidden); the
      stored baseline is left untouched so drift stays visible on the next
      default (cache-hitting) run -- mirrors ``hotato.rubric``'s
      ``--no-cache``.
    * A fresh MISS (no cached baseline) -- transcribes, then persists the
      result (unless ``cache`` is None).

    This never changes the timing/verdict path: it is purely an optional
    provenance/performance layer around the same :func:`transcribe` this
    module has always shipped, and callers that never pass a ``cache`` see
    identical behaviour to before this cache existed.
    """
    require_regular_file(path)
    resolved_device = _resolve_device(device)
    resolved_compute_type = compute_type or (
        "float16" if resolved_device == "cuda" else "int8"
    )
    audio_sha256 = _stream_sha256(path)
    cache_key = transcript_cache_key(
        model=model, device=resolved_device, compute_type=resolved_compute_type,
        language=language, word_timestamps=word_timestamps, vad_filter=vad_filter,
        audio_sha256=audio_sha256,
    )

    cached_dict = cache.get(cache_key) if cache is not None else None

    # Cache hit (and not no_cache): replay the byte-identical stored transcript
    # and skip the model entirely.
    if cached_dict is not None and not no_cache:
        return CachedTranscribeResult(
            transcript=_transcript_from_dict(cached_dict),
            cache_key=cache_key, cached=True, drift=None,
        )

    fresh = transcribe(
        path, model=model, device=resolved_device,
        compute_type=resolved_compute_type, word_timestamps=word_timestamps,
        vad_filter=vad_filter, language=language,
    )
    fresh_dict = _transcript_to_dict(fresh)

    # --no-transcribe-cache drift: diff the fresh transcript against the
    # cached one, never hide it.
    drift = None
    if cached_dict is not None and no_cache:
        drift = _diff_transcripts(cached_dict, fresh_dict)

    # Persist the fresh transcript (unless this was an explicit no-cache
    # re-query against an existing entry -- leave the cached baseline intact
    # so drift stays visible on the next default run).
    if cache is not None and not (no_cache and cached_dict is not None):
        cache.put(cache_key, fresh_dict)

    return CachedTranscribeResult(
        transcript=fresh, cache_key=cache_key, cached=False, drift=drift,
    )
