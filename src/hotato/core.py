"""Core evaluation: one recording, or the bundled 8-scenario battery.

Both entry points return the SAME machine-readable dict (see ``README.md`` for
the schema) so an agent or a CI job can consume one shape regardless of mode.

Everything here runs fully offline. No audio, transcript, or result ever leaves
the machine: the only I/O is reading the WAV files you point at and reading the
bundled scenario labels shipped inside this package.

The scoring itself is delegated unchanged to the vendored ``_engine`` (the MIT
``barge_scoring`` engine: energy-VAD framing + three objective timing signals).
This module adds only: a stable output envelope, the per-event fix routing, and
the honest limits block. It introduces no new accuracy claim.
"""

from __future__ import annotations

from .errors import open_regular as _open_regular
from .errors import require_regular_file as _require_regular_file

from .errors import wav_read as _wav_read

import array
import hashlib
import json
import math
import os
import re
import stat
import struct
import sys
import wave
from typing import Optional

from . import _engine
from ._engine.score import (
    ScoreConfig,
    evaluate,
    frame_dump,
    score_channels,
    score_stereo,
)
from .errors import ChannelRangeError
from .fixmap import (
    classify_event,
    downgrade_lone_engagement_fix,
    is_non_speech_ambient_label,
    systemic_pointer,
)

# A scenario ``id`` becomes a filesystem path (``<audio_dir>/<id><suffix>``), so
# it MUST be a safe single path segment or a crafted scenarios pack could read
# (and, via --embed-audio, exfiltrate) an arbitrary local WAV outside --audio.
# The rule mirrors the corpus intake schema (corpus/label.schema.json): a plain
# slug with NO path separator, not absolute, and not starting with '.', so
# '..', '../x', '/etc/x', 'C:\\x' and 'a/b' are all rejected before any join.
_SCENARIO_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")


def _safe_scenario_id(sid) -> str:
    """Validate a scenario ``id`` as a safe path segment, or raise a clean
    ValueError (-> exit 2). Never sanitizes silently: a bad id is refused, never
    quietly rewritten into a different file."""
    s = str(sid)
    if not _SCENARIO_ID_RE.match(s):
        raise ValueError(
            f"scenario id {sid!r} is not a safe id: use letters, digits, '.', "
            "'-', '_' and no path separators (a scenario id becomes a file "
            "name under --audio, so '/', '..' or an absolute path is refused)."
        )
    return s

__all__ = [
    "run_single",
    "run_suite",
    "dump_frames_for_input",
    "process_exit_code",
    "LIMITS",
    "SUITE_ID",
]

SUITE_ID = "barge-in"

# Honest scope + ceiling. This is stated up front in every result and in the MCP
# tool schema. It is the credibility of the tool: we do not hide the ceiling.
LIMITS = {
    "method": "energy-based VAD framing over aligned caller/agent channels; three objective timing signals (did_yield, seconds_to_yield, talk_over).",
    "accuracy_claim": None,
    "reproducible": "deterministic given the same audio and config; every threshold is an exposed parameter and every frame is inspectable.",
    "ceiling": (
        "Automated sub-second scoring on a single channel using neural or energy "
        "VAD has a real ceiling. Treat these as reproducible timing measurements, "
        "not ground-truth judgements of a detector's internal quality."
    ),
    "best_input": "stereo / two-channel recording with the caller and the agent on separate channels is the gold reference. A single-channel (mono) recording is scorable via the opt-in, quality-gated [diarize] front-end (hotato run --mono call.wav --diarize); below the confidence bar its verdict is labeled indicative-only, and it is never equivalent to a true dual-channel measurement.",
    "does_not_do": [
        "no speaker identification (a diarizer assigns anonymous SPEAKER_00/01; it never says who a person is)",
        "no speech-to-text / transcription",
        "no emotion or intent detection",
        "no claim about any specific vendor's internal accuracy",
    ],
    "scope": "barge-in, turn-taking, overlap/talk-over, and backchannel handling from call audio. Latency of the yield is measured; word-level semantics are out of scope.",
    "offline": "runs locally; no network egress of user audio.",
}


def _engine_meta() -> dict:
    return {
        "name": "barge_scoring (vendored, MIT)",
        "version": getattr(_engine, "__version__", "unknown"),
        "upstream": "https://github.com/quantumCF/voice-agent-barge-in-tests",
    }


# --- audio provenance ------------------------------------------------------
#
# Identity of the exact bytes an event was scored from, so a later before/after
# comparison (``hotato fix trial``) can tell a fresh recapture from a re-score
# of the SAME recording under a different threshold. STREAMED: the hash reads
# the file in fixed-size chunks (the same chunked-read shape
# ``contract.py``'s ``_sha256_file`` already uses for contract bundles), so a
# multi-hour recording costs one extra sequential read, never a second
# in-memory copy of the samples ``_load_signal`` already avoids materializing.
# Additive and versioned (``schema_version``): a run envelope from before this
# field existed simply omits it, and every reader (``verify``, ``fix_trial``)
# must treat that absence as UNKNOWN provenance, never as proof of anything.
#
# ``sha256`` (raw file bytes) is identity of the CONTAINER: it changes on any
# byte anywhere in the file, including a header field no decoder ever reads
# (a byte_rate/block_align edit, a re-write with a different chunk order, a
# byte appended past the declared data length) or a lossless transcode that
# reorders/repads the container without touching a single sample value.
# ``pcm_sha256`` is identity of the DECODED SAMPLES: a streamed sha256 over
# only the WAV data-chunk's sample bytes, read via ``wave.readframes()`` in
# fixed-size frame chunks -- the same primitive ``_load_signal``/
# ``_engine.read_wav`` already call to decode, never a hand-rolled second
# parser. Because it is bounded by the header's own frame count and never
# touches RIFF/fmt metadata or anything past the last declared frame, a
# header-only edit or a trailing-byte append leaves it UNCHANGED while
# ``sha256`` differs; a change to even one sample value changes it. It is
# additive and OPTIONAL in the schema for the same reason ``sha256`` is
# effectively load-bearing today: a reader must treat its absence (an older
# envelope, or one hand-built without it) as UNKNOWN, never as proof either
# way.

_AUDIO_PROVENANCE_SCHEMA_VERSION = "1"

# Frames per ``wave.readframes()`` call when streaming the PCM digest: bounds
# peak memory on a multi-hour recording to this many frames' worth of bytes
# (well under a MiB at typical mono/stereo 16-bit rates) regardless of file
# length, mirroring ``_stream_sha256``'s fixed-size chunked read over the raw
# file.
_PCM_HASH_CHUNK_FRAMES = 1 << 16


def _stream_sha256(path: str) -> str:
    h = hashlib.sha256()
    _require_regular_file(path)
    # open-ok: _require_regular_file(path) guards on the line above (kept as builtin
    # open so tests can monkeypatch core.open to spy on chunked reads)
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _stream_pcm_sha256(path: str) -> str:
    """Streamed sha256 over the DECODED PCM sample bytes only.

    Reads the WAV data chunk via the same ``wave.open()`` /
    ``readframes()`` call ``_load_signal`` and the vendored
    ``_engine.read_wav`` already decode with -- fixed sample width,
    little-endian (the WAV container's own on-disk byte order, independent
    of host architecture), channel-interleaved, exactly as stored -- in
    bounded-size frame chunks, never the whole file in one call. This never
    reads RIFF/fmt container metadata or any byte past the header's own
    frame count, so it names the same identity for two files that decode to
    identical samples even when their containers differ byte-for-byte (a
    header field edited, trailing bytes appended past the declared data
    length), while still changing on any edit to a sample value.
    """
    h = hashlib.sha256()
    with _wav_read(path) as wf:
        while True:
            chunk = wf.readframes(_PCM_HASH_CHUNK_FRAMES)
            if not chunk:
                break
            h.update(chunk)
    return h.hexdigest()


def _wav_identity(role: str, path: str) -> dict:
    """One file's provenance: a streamed sha256 of its raw bytes, a streamed
    sha256 of its decoded PCM samples, plus the sample rate and frame count
    read straight from the WAV header (the same cheap header read
    ``_load_signal`` already does; no float decode here)."""
    sha256 = _stream_sha256(path)
    pcm_sha256 = _stream_pcm_sha256(path)
    with _wav_read(path) as wf:
        sample_rate = wf.getframerate()
        num_samples = wf.getnframes()
    duration_sec = (num_samples / sample_rate) if sample_rate else None
    return {
        "role": role,
        "path": os.path.basename(path),
        "sha256": sha256,
        "pcm_sha256": pcm_sha256,
        "sample_rate": sample_rate,
        "num_samples": num_samples,
        "duration_sec": duration_sec,
    }


def _audio_provenance(*role_paths) -> dict:
    """Versioned audio-provenance block for one event. ``role_paths`` is one
    ``(role, path)`` pair for a single-file input (``stereo``/``mono``) or two
    for a caller+agent dual-mono input. The top-level ``sha256`` is the single
    file's own hash, or (mirroring ``contract.py``'s order-stable
    ``_sha256_two_files``) an order-stable combination of both sides' hashes,
    so a caller-only or agent-only re-recording still changes the combined
    identity."""
    sides = [_wav_identity(role, path) for role, path in role_paths]
    if len(sides) == 1:
        combined = sides[0]["sha256"]
    else:
        h = hashlib.sha256()
        for s in sides:
            h.update(s["sha256"].encode("ascii"))
        combined = h.hexdigest()
    return {
        "schema_version": _AUDIO_PROVENANCE_SCHEMA_VERSION,
        "sha256": combined,
        "sides": sides,
    }


def _not_scorable_reason(*, result, expected_yield: bool, onset_provided: bool):
    """Why this recording cannot be judged at all, or None when it can.

    Two malformed-input shapes (both confirmed by an external correctness
    review) must never surface as a normal pass or fail:

      (a) nothing to score: no onset was provided and the caller channel shows
          no detectable speech, so there is no caller event to react to. The
          engine used to clamp the missing onset to frame 0 and score anyway.
      (b) a should-yield expectation with the agent silent at the caller
          onset: there was nothing to yield, so did_yield carries no meaning.
          The input is wrong (onset time, channel mapping, or expectation),
          and that is not an agent verdict.

    The check is deterministic: it reads only what the engine already
    measured. Scorable events are returned untouched, byte for byte.
    """
    if not onset_provided and result.detected_caller_onset_sec is None:
        return (
            "no caller speech was detected on the caller channel and no onset "
            "was provided, so there is no caller event to score. Check the "
            "caller/agent channel mapping, or pass the onset time explicitly."
        )
    if expected_yield and not result.agent_talking_at_onset:
        return (
            "the agent was not talking at the caller onset, so a should-yield "
            "verdict has no meaning for this recording. Check the onset time, "
            "the caller/agent channel mapping, or the expectation."
        )
    return None


def _event_from_result(
    *,
    event_id: str,
    result,
    expected: dict,
    stack: Optional[str],
    scenario_id: Optional[str] = None,
    category: Optional[str] = None,
    family: Optional[str] = None,
    tags: Optional[list] = None,
    title: Optional[str] = None,
    onset_provided: bool = True,
    echo: Optional[dict] = None,
    echo_gate: bool = False,
    resume: Optional[dict] = None,
    audio_provenance: Optional[dict] = None,
) -> dict:
    verdict = evaluate(result, expected)
    expected_yield = bool(expected.get("yield", True))
    # Namespaced signal bus (additive; schema_version stays "1"). signals.barge_in
    # mirrors the verdict's three original values byte-for-byte; signals.latency
    # adds the pure-timing endpointing measurements; signals.echo is the ADDITIVE
    # cross-channel coherence block; signals.resume (this addition) is the ADDITIVE
    # post-yield resume/restart block, present only when the agent actually
    # yielded. All computed entirely in hotato's own layer. New dimensions slot in
    # here without changing the existing verdict or measurements blocks.
    signals = result.signals if echo is None else {**result.signals, "echo": echo}
    if resume is not None:
        signals = {**signals, "resume": resume}
    # The engine reports a `-1.0` sentinel for caller_onset_sec when no caller
    # onset was detected/provided (a physically impossible negative timestamp).
    # Surface it as null instead, mirroring how detected_caller_onset_sec is
    # already emitted, so a fabricated number never flows into the envelope or an
    # export CSV. Every valid recording keeps a real float here, unchanged.
    onset_measurement = result.caller_onset_sec
    if onset_measurement is not None and onset_measurement < 0:
        onset_measurement = None
    # Boundary-sensitivity, derived ENTIRELY in hotato's layer from the engine's
    # existing outputs (the vendored _engine stays byte-identical to upstream):
    # the onset the caller requested, the quantized onset/yield frame the scorer
    # landed on, and how far the result sits from the binding pass/fail bound. A
    # result within one hop of flipping is boundary_sensitive.
    hop = result.hop_sec or 0.0
    onset_used = onset_measurement
    if onset_used is None:
        onset_used = result.detected_caller_onset_sec
    if onset_used is not None and hop > 0:
        onset_frame_index = max(0, int(round(onset_used / hop)))
        onset_effective_sec = onset_frame_index * hop
    else:
        onset_frame_index = None
        onset_effective_sec = None
    onset_requested_sec = onset_measurement if onset_provided else None
    if (result.did_yield and result.time_to_yield_sec is not None
            and onset_frame_index is not None and hop > 0):
        yield_frame_index = onset_frame_index + int(round(result.time_to_yield_sec / hop))
    else:
        yield_frame_index = None
    _margins = []
    _max_ttoy = expected.get("max_time_to_yield_sec")
    if _max_ttoy is not None and result.time_to_yield_sec is not None:
        _margins.append(_max_ttoy - result.time_to_yield_sec)
    _max_over = expected.get("max_talk_over_sec")
    if _max_over is not None:
        _margins.append(_max_over - result.talk_over_sec)
    if _margins and hop > 0:
        decision_margin_sec = round(min(_margins), 6)   # tightest slack (smallest)
        decision_margin_hops = int(round(decision_margin_sec / hop))
        boundary_sensitive = abs(decision_margin_hops) <= 1
    else:
        decision_margin_sec = None
        decision_margin_hops = None
        boundary_sensitive = False
    event = {
        "event_id": event_id,
        "scenario_id": scenario_id,
        "title": title,
        "category": category,
        "expected_yield": expected_yield,
        "verdict": {
            "passed": verdict.passed,
            "did_yield": result.did_yield,
            "seconds_to_yield": result.time_to_yield_sec,
            "talk_over_sec": result.talk_over_sec,
            "reasons": verdict.reasons,
        },
        "measurements": {
            "caller_onset_sec": onset_measurement,
            "agent_talking_at_onset": result.agent_talking_at_onset,
            "hop_sec": result.hop_sec,
            "notes": result.notes,
            # Additive boundary-sensitivity block (schema_version stays "1"). These
            # expose the onset the caller requested, the quantized onset/yield frame
            # the scorer actually landed on, and how far the result sits from the
            # binding pass/fail threshold. A result one hop from flipping carries
            # boundary_sensitive: true. All default to null/false when not
            # derivable; none of the pre-existing keys above are touched.
            "onset_requested_sec": onset_requested_sec,
            "onset_frame_index": onset_frame_index,
            "onset_effective_sec": onset_effective_sec,
            "yield_frame_index": yield_frame_index,
            "decision_margin_sec": decision_margin_sec,
            "decision_margin_hops": decision_margin_hops,
            "boundary_sensitive": boundary_sensitive,
        },
        "signals": signals,
        "fix": None,
    }
    # Additive: present whenever the caller resolved a real audio file for
    # this event (every scored path below does); omitted only for an event
    # with no file to hash (a not-scorable / missing-audio placeholder never
    # reaches this line). A reader built before this key existed simply does
    # not look at it -- schema_version stays "1" and every field it already
    # reads is unchanged.
    if audio_provenance is not None:
        event["audio_provenance"] = audio_provenance
    # A corpus label marking a NON-SPEECH ambient fixture (family "noise-hold" /
    # tag "non-speech"): a VAD/noise-floor sensitivity case, not a backchannel.
    # Recorded as a durable, additive marker ONLY when true, so a scored envelope
    # for a real call (no scenario labels) and the bundled golden stay
    # byte-identical. Downstream fix routing (fixmap / diagnose) reads it to keep
    # an ambient false-yield off the engagement-control pointer.
    non_speech = is_non_speech_ambient_label(tags, family)
    if non_speech:
        event["non_speech"] = True
    reason = _not_scorable_reason(
        result=result, expected_yield=expected_yield, onset_provided=onset_provided
    )
    # Echo gate (OPT-IN, off by default): a yield that coincides with high
    # cross-channel echo coherence is most likely the agent hearing its own TTS
    # bleed, not a real caller event. Rather than count that spurious yield, hold
    # it out of the verdict exactly like any other not-scorable input problem.
    # This never fires on clean audio and, being opt-in, never changes a default
    # run: it is a stricter mode a user asks for explicitly.
    if (
        reason is None
        and echo_gate
        and echo is not None
        and echo.get("echo_suspected")
        and result.did_yield
    ):
        reason = (
            "the yield coincides with high cross-channel echo coherence "
            f"(coherence {echo.get('coherence')} at lag {echo.get('lag_sec')}s), "
            "so it is most likely the agent hearing its own audio bleed rather "
            "than a real caller. Fix the audio path (echo cancellation, channel "
            "separation) before treating this as a yield."
        )
    if reason is not None:
        # The `scorable` key is emitted ONLY here, on not-scorable events, so
        # every envelope for a valid recording stays byte-identical to before.
        # The verdict is fail-closed (never a pass) but it is NOT a normal
        # fail either: _envelope excludes it from passed/failed, regression,
        # fix routing, the funnel, and the envelope exit_code.
        event["scorable"] = False
        event["not_scorable_reason"] = reason
        event["verdict"]["passed"] = False
        event["verdict"]["reasons"] = [reason]
        return event
    if not verdict.passed:
        event["fix"] = classify_event(
            expected_yield=expected_yield,
            did_yield=result.did_yield,
            reasons=verdict.reasons,
            stack=stack,
            tags=tags,
            category=category,
            scenario_id=scenario_id,
            # The measured cross-channel echo signal is authoritative: a self-echo
            # yield routes to the audio-routing (config) fix, never to the
            # engagement-control pointer with fabricated caller-intent wording.
            # curator tags/ids remain a fallback for scenarios that carry no audio.
            echo_suspected=bool(echo and echo.get("echo_suspected")),
            family=family,
            non_speech=non_speech,
        )
    return event


def _envelope(*, mode: str, stack: Optional[str], events: list) -> dict:
    # Not-scorable events (malformed input, see _not_scorable_reason) are
    # listed with their reason but excluded from every judgement: they are not
    # passes, not failures, never a regression, never a fix, never a funnel
    # signal, and never the envelope exit_code. For valid recordings the two
    # lists below equal `events` and the envelope is byte-identical to before.
    not_scorable = [e for e in events if e.get("scorable") is False]
    scorable = [e for e in events if e.get("scorable") is not False]
    failed = [e for e in scorable if not e["verdict"]["passed"]]

    # The engagement-control pointer is honest ONLY on the both-axes case: a
    # battery that BOTH missed a real interruption AND false-stopped on a
    # backchannel, where no single threshold can win. A LONE backchannel
    # false-stop (no missed-real-interruption anywhere in the battery -- always
    # true for a single `hotato run`/`hotato capture`) is config-tunable: raise
    # the words-to-interrupt threshold one step, exactly as `hotato plan`/
    # `diagnose` already treat it. So compute the funnel FIRST and, when it is
    # absent, downgrade any lone engagement-control fix to that config fix before
    # the fix_map is built -- the pointer never rides on a single occurrence.
    funnel = systemic_pointer(scorable)
    if funnel is None:
        for e in failed:
            if e.get("fix"):
                downgrade_lone_engagement_fix(e, stack)

    fix_map = [
        {
            "event_id": e["event_id"],
            "scenario_id": e.get("scenario_id"),
            "fix_class": e["fix"]["fix_class"],
            "title": e["fix"]["title"],
            "detail": e["fix"]["detail"],
            "knob": e["fix"]["knob"],
            "pointer": e["fix"]["pointer"],
        }
        for e in failed
        if e.get("fix")
    ]
    summary = {
        "events": len(events),
        "passed": len(scorable) - len(failed),
        "failed": len(failed),
        "regression": len(failed) > 0,
    }
    if not_scorable:
        # Additive: the key appears only when at least one event is not
        # scorable, so every existing summary stays byte-identical.
        summary["not_scorable"] = len(not_scorable)
    return {
        "tool": "hotato",
        "schema_version": "1",
        "mode": mode,
        "stack": (stack or "generic").strip().lower(),
        "offline": True,
        "engine": _engine_meta(),
        "limits": LIMITS,
        "summary": summary,
        "events": events,
        "fix_map": fix_map,
        "funnel": funnel,
        "exit_code": 1 if failed else 0,
    }


def process_exit_code(envelope: dict) -> int:
    """The process exit code a CLI should return for a finished envelope.

    The envelope's own ``exit_code`` is frozen by the schema to 0 or 1
    (1 exactly when a SCORABLE event failed). The CLI already reserves exit 2
    for unusable input (a corrupt WAV, a bad flag), and a single recording
    that is not scorable is precisely that: an input problem, not an agent
    verdict. So a mode=single run whose every event is not scorable maps to
    process exit 2. Suite runs never map to 2 here: their not-scorable events
    are listed with a reason and do not fail the suite by themselves.

    Not yet wired into cli.py (owned separately). The wiring is one line per
    entry point: replace ``return env["exit_code"]`` with
    ``return process_exit_code(env)``.
    """
    summary = envelope.get("summary", {})
    n_events = summary.get("events", 0)
    if (
        envelope.get("mode") == "single"
        and n_events > 0
        and summary.get("not_scorable", 0) == n_events
    ):
        return 2
    return int(envelope.get("exit_code", 0))


# --- input hardening ------------------------------------------------------
#
# The scorer itself lives in the vendored, drift-guarded ``_engine`` and must not
# be edited. These wrappers sit ABOVE it and turn every hostile / malformed input
# into a clean ValueError (which the CLI surfaces as exit code 2), so a corrupt,
# empty, truncated, or non-WAV file -- or an out-of-range channel / negative onset
# -- can never escape as a Python traceback or masquerade as a real low score.

def _load_signal(path: str):
    """Decode a PCM WAV into an ``_engine`` ``Signal`` with the SAME float values
    the vendored ``_engine.read_wav`` produces, but without materializing a
    per-sample Python ``list`` of tens of millions of floats.

    When numpy is available (the same optional acceleration the engine keys off,
    ``_engine.audio._np``), the integer samples are converted to float in one
    vectorized step and each channel is kept as a numpy array. That is the
    difference between a multi-GB Python-object list and a compact float64 buffer
    on a multi-hour recording, so ``run``/``capture``/``compare``/``verify``/
    ``fixture create``/``benchmark`` scale like the streaming ``scan``/``analyze``
    path instead of OOM-ing on the same long call. The decoded sample VALUES are
    byte-identical either way: the engine's ``frame_rms`` sees the identical
    inputs whether numpy did the byte->float conversion or the stdlib list
    comprehension did. (``frame_rms``'s own per-frame summation is a separate
    step; with numpy it uses pairwise summation and without it a sequential
    accumulator, which can differ in the last double-precision bit -- a difference
    masked everywhere numbers are surfaced by the 3-decimal rounding, and pinned
    by test_core.py's fuzzed numpy-vs-stdlib envelope parity check.)

    When numpy is absent (or a test has forced ``_engine.audio._np = None`` to
    exercise the pure-stdlib path), it delegates unchanged to the engine's own
    list-based ``read_wav`` so behaviour and published numbers are untouched.
    """
    # Reject anything that is not a regular file BEFORE any open()/wave.open()
    # call. os.stat() never blocks (unlike opening a FIFO for reading, which
    # hangs at the OS level until a writer opens the other end); it is always
    # safe to run first. This single check covers both branches below, since
    # both eventually call wave.open() on ``path``. os.stat() follows symlinks,
    # so a symlink to a FIFO is caught too. A missing/unreadable path still
    # raises the normal OSError here (same as open() would), so file_not_found
    # classification is unaffected -- only a non-regular file gets the new
    # ValueError.
    _require_regular_file(path)

    from ._engine import audio as _audio

    _np = _audio._np
    if _np is None:
        # Pure-stdlib decode path; also the path tests pin with _np = None so the
        # numpy-vs-stdlib parity check stays honest.
        return _engine.read_wav(path)

    with _wav_read(path) as wf:
        n_channels = wf.getnchannels()
        sampwidth = wf.getsampwidth()
        sample_rate = wf.getframerate()
        n_frames = wf.getnframes()
        raw = wf.readframes(n_frames)

    # array.array first: it decodes the integers cheaply (2 bytes/sample for
    # 16-bit) AND raises the exact "bytes length not a multiple of item size"
    # ValueError the engine does on a corrupt data chunk, which _read_wav already
    # knows how to re-wrap. numpy then does the byte->float conversion in one
    # vectorized shot rather than a per-sample Python list comprehension.
    if sampwidth == 1:
        a = array.array("B")
        a.frombytes(raw)
        floats = (_np.frombuffer(a, dtype=_np.uint8).astype(_np.float64) - 128.0) / 128.0
    elif sampwidth == 2:
        a = array.array("h")
        a.frombytes(raw)
        if sys.byteorder == "big":  # WAV is little-endian; match the engine
            a.byteswap()
        floats = _np.frombuffer(a, dtype=_np.int16).astype(_np.float64) / 32768.0
    elif sampwidth == 4:
        a = array.array("i")
        a.frombytes(raw)
        if sys.byteorder == "big":
            a.byteswap()
        floats = _np.frombuffer(a, dtype=_np.int32).astype(_np.float64) / 2147483648.0
    else:
        raise ValueError(
            f"unsupported sample width {sampwidth * 8}-bit; "
            "please convert to 16-bit PCM (for example with ffmpeg -acodec pcm_s16le)"
        )

    channels = [floats[ch::n_channels] for ch in range(max(1, n_channels))]
    return _audio.Signal(sample_rate=sample_rate, channels=channels)


def _read_wav(path: str):
    """Read a WAV via the vendored engine, translating low-level parse failures
    into a clean, actionable ValueError.

    ``_engine.read_wav`` uses the stdlib ``wave`` module, which raises
    ``wave.Error`` / ``EOFError`` (and, on some malformed headers,
    ``struct.error``) for an empty, truncated, or non-WAV file. Left unwrapped
    those escape as an ugly traceback. A header that declares more frames than the
    file actually contains (a truncated / corrupt recording) is also caught here,
    so a partial read can never masquerade as a genuine (low) score.
    """
    try:
        signal = _load_signal(path)
    except (wave.Error, EOFError, struct.error, RuntimeError) as exc:
        # RuntimeError: a well-formed RIFF/WAVE header whose inner sub-chunk is
        # malformed/oversized makes stdlib ``wave`` raise a bare RuntimeError from
        # Chunk.skip()/seek() (what a partial/interrupted recording write looks
        # like). Normalize it to the same clean ValueError so `run`/`compare`/
        # `verify`/`fixture` return the exit-2 usage contract, not a traceback.
        raise ValueError(
            f"{path!r} is not a readable PCM WAV ({exc or type(exc).__name__}). "
            "Export a PCM WAV, e.g. ffmpeg -i input -acodec pcm_s16le output.wav"
        ) from exc
    except ValueError as exc:
        # A corrupt data chunk whose byte length is not a whole number of samples
        # surfaces from array.frombytes as "bytes length not a multiple of item
        # size". Re-wrap that one into the same clean message; leave the engine's
        # own actionable ValueErrors (e.g. an unsupported sample width) untouched.
        if "multiple of item size" in str(exc):
            raise ValueError(
                f"{path!r} is not a readable PCM WAV (corrupt or truncated data "
                "chunk). Export a PCM WAV, e.g. ffmpeg -i input -acodec pcm_s16le "
                "output.wav"
            ) from exc
        raise
    if signal.num_samples == 0:
        raise ValueError(
            f"{path!r} contains no audio samples (empty or header-only WAV)."
        )
    # A zero (or negative) sample rate is a corrupt header: Python's ``wave``
    # reads it back happily but every downstream duration/hop computation divides
    # by it. Reject it here so it surfaces as a clean exit-2 error rather than a
    # bare ZeroDivisionError traceback from ``num_samples / sample_rate``.
    if signal.sample_rate <= 0:
        raise ValueError(
            f"{path!r} declares an invalid sample rate ({signal.sample_rate} Hz); "
            "the file is corrupt or was mis-exported. Re-export a PCM WAV, e.g. "
            "ffmpeg -i input -acodec pcm_s16le -ar 16000 output.wav"
        )
    # Truncation guard: for a well-formed file the wave header's declared frame
    # count equals the number of decoded samples per channel; a short data chunk
    # decodes fewer. Re-reading the header is best-effort and must never itself
    # fail the read.
    try:
        with _wav_read(path) as wf:
            declared = wf.getnframes()
    except Exception:  # pragma: no cover - header re-read is defensive only
        declared = signal.num_samples
    if declared and signal.num_samples < declared:
        raise ValueError(
            f"{path!r} is truncated or corrupt: its header declares {declared} "
            f"frames but only {signal.num_samples} are present. Re-export the "
            "full recording."
        )
    return signal


def _require_channel(signal, index: int, role: str) -> None:
    """Fail cleanly (ChannelRangeError -> exit 2) on an out-of-range channel index
    rather than let ``Signal.get`` raise a bare IndexError traceback deep in the
    engine. ``ChannelRangeError`` is a ValueError subclass, so the exit-2 /
    structured-error contract is unchanged; the distinct type only lets the
    folder-batch commands treat it as a global flag error, not a per-file skip."""
    if index < 0 or index >= signal.num_channels:
        raise ChannelRangeError(
            f"--{role}-channel {index} is out of range for a "
            f"{signal.num_channels}-channel recording "
            f"(valid channels: 0..{signal.num_channels - 1})."
        )


def _require_distinct_channels(caller_channel: int, agent_channel: int) -> None:
    """Refuse identical caller/agent channels (ValueError -> exit 2). Comparing a
    channel against itself makes the agent 'hear' its own channel as the caller,
    producing a confident but meaningless verdict and -- via ``fixture create`` --
    a bogus regression fixture that passes/fails the battery forever. A
    two-channel recording carries the two parties on two DIFFERENT channels."""
    if caller_channel == agent_channel:
        raise ValueError(
            f"--caller-channel and --agent-channel must be different (both are "
            f"{caller_channel}); pass distinct channels for a 2-channel "
            "recording (the caller and the agent are on separate channels)."
        )


def _echo_block(caller_samples, agent_samples, sample_rate: int, cfg: ScoreConfig) -> dict:
    """The additive ``signals.echo`` cross-channel coherence block for one
    recording, framed exactly like the engine's VAD tracks. Computed in hotato's
    own layer (see ``echo.py``); never touches the vendored engine and never
    changes any existing number."""
    from .echo import echo_block_from_samples

    return echo_block_from_samples(
        caller_samples, agent_samples, sample_rate,
        frame_ms=cfg.frame_ms, hop_ms=cfg.hop_ms,
    )


def _resume_block(agent_samples, sample_rate: int, result, cfg: ScoreConfig) -> Optional[dict]:
    """The additive ``signals.resume`` post-yield block for one recording, or
    ``None`` when there was no scorable yield to measure after.

    Meaningful only once the agent has yielded: it measures, from the agent's own
    VAD track, whether the agent resumed, how quickly, and whether the post-resume
    run is long enough to look like a restart-from-the-top. Computed in hotato's
    own layer (see ``resume.py``); never touches the vendored engine and never
    changes any existing number."""
    if not result.did_yield or result.time_to_yield_sec is None:
        return None
    from .resume import resume_block_from_samples

    yield_time_sec = result.caller_onset_sec + result.time_to_yield_sec
    return resume_block_from_samples(
        agent_samples,
        sample_rate,
        yield_time_sec,
        cfg.agent_vad,
        frame_ms=cfg.frame_ms,
        hop_ms=cfg.hop_ms,
        onset_min_run_sec=cfg.onset_min_run_sec,
    )


def _check_onset(onset_sec: Optional[float]) -> None:
    if onset_sec is None:
        return
    # NaN / +-Inf reach here as floats but are neither a valid time nor safely
    # convertible to a frame index (int(inf) raises OverflowError deep in the
    # engine, NaN silently compares False and clamps to frame 0 -> a fabricated
    # verdict). Reject them up front as a clean usage error (exit 2), the same
    # guard fixture.py already applies to `fixture create --onset`.
    if not math.isfinite(onset_sec):
        raise ValueError(
            f"--onset must be a finite number of seconds (time from the start "
            f"of the recording); got {onset_sec}."
        )
    if onset_sec < 0:
        raise ValueError(
            f"--onset must be >= 0 seconds (time from the start of the "
            f"recording); got {onset_sec}."
        )


def _check_onset_within_duration(onset_sec: Optional[float], duration_sec: float) -> None:
    """An onset at or past the end of the recording is out of range: the engine
    silently clamps it to the last frame and then emits a confident-sounding
    verdict about a moment that is not in the audio. Refuse it here (exit 2), the
    same bound fixture.py's ``create_fixture`` already enforces."""
    if onset_sec is not None and onset_sec >= duration_sec:
        raise ValueError(
            f"--onset {onset_sec}s is beyond the end of the recording "
            f"({duration_sec:.2f}s)."
        )


# --- opt-in, quality-gated single-channel (diarized-mono) path -------------
#
# A mono recording is the coverage wall today (it is rejected as not scorable).
# This path diarizes the mono into caller/agent activity, reconstructs two masked
# tracks, and feeds the EXISTING two-mono `score_channels`, inheriting the
# envelope/fixmap/exit-code machinery unchanged (spec 5). It NEVER touches the
# default path: run_single only reaches here when `mono`/`diarize` is set.

def _diarized_not_scorable_event(*, source: str, dm, expected_yield: bool) -> dict:
    """The not-scorable event for a mono file the confidence gate REFUSED (spec 7
    refuse tier): a non-separable input, reported exactly like every other
    not-scorable input problem (scorable:false + reason -> excluded from
    passed/failed, the funnel, fix_map, and the exit code; a single run maps to
    process exit 2). Carries the diarization provenance + separation sub-block so
    the caller sees which signal failed and the next step."""
    from .diarize import echo_na_block

    reason = dm.not_scorable_reason
    return {
        "event_id": source,
        "scenario_id": None,
        "title": f"single recording ({source})",
        "category": "should_yield" if expected_yield else "should_not_yield",
        "expected_yield": expected_yield,
        "scorable": False,
        "not_scorable_reason": reason,
        "next_step": "record a dual-channel call (caller and agent on separate channels) for a confident verdict",
        "diarization": dm.provenance,
        "scorability": {"separation": dm.separation},
        "indicative_only": True,
        "verdict": {
            "passed": False,
            "did_yield": None,
            "seconds_to_yield": None,
            "talk_over_sec": None,
            "reasons": [reason],
        },
        "measurements": {
            # Additive boundary-sensitivity keys defaulted to null/false: this is a
            # not-scorable placeholder with nothing measured to be near a threshold.
            "onset_requested_sec": None,
            "onset_frame_index": None,
            "onset_effective_sec": None,
            "yield_frame_index": None,
            "decision_margin_sec": None,
            "decision_margin_hops": None,
            "boundary_sensitive": False,
        },
        "signals": {
            "barge_in": {
                "did_yield": None,
                "time_to_yield_sec": None,
                "talk_over_sec": None,
            },
            "latency": {
                "response_gap_sec": None,
                "premature_start_sec": None,
            },
            "echo": echo_na_block(),
        },
    }


def _run_diarized_mono(
    *,
    mono: Optional[str],
    diarize: bool,
    diarizer: str,
    onset_sec: Optional[float],
    expect: str,
    stack: Optional[str],
    max_talk_over_sec: Optional[float],
    max_time_to_yield_sec: Optional[float],
    caller_speaker: Optional[str],
    agent_speaker: Optional[str],
    egress_opt_in: bool,
    cfg: ScoreConfig,
) -> dict:
    from .diarize import echo_na_block, prepare_diarized_mono

    if not mono:
        raise ValueError(
            "--diarize scores a single-channel (mono) recording; pass --mono FILE. "
            "For a two-channel recording use --stereo (the gold reference)."
        )
    if not diarize:
        # No silent fallback: scoring a mixed mono requires an explicit diarizer.
        raise ValueError(
            "scoring a single-channel (mono) recording requires --diarize (it must "
            "be separated into caller/agent first). Add --diarize [--diarizer "
            "pyannote|sortformer|pyannoteai], or pass a two-channel --stereo file."
        )

    signal = _read_wav(mono)
    if signal.num_channels != 1:
        raise ValueError(
            f"--mono expects a single-channel recording; {mono!r} has "
            f"{signal.num_channels} channels. Use --stereo for a two-channel "
            "recording (the gold reference), or export a real mono file."
        )
    samples = signal.get(0)
    sample_rate = signal.sample_rate
    n = signal.num_samples
    _check_onset_within_duration(onset_sec, n / sample_rate)
    source = os.path.basename(mono)

    want_yield = str(expect).strip().lower() not in ("hold", "no", "false", "hold-floor")

    # Diarize -> assign -> gate -> reconstruct. A missing extra/token/model raises
    # BackendUnavailable here (surfaced as a clean exit-2), never a raw-mono guess.
    dm = prepare_diarized_mono(
        samples,
        sample_rate,
        backend=diarizer,
        num_speakers=2,
        caller_speaker=caller_speaker,
        agent_speaker=agent_speaker,
        egress_opt_in=egress_opt_in,
        cfg=cfg,
    )

    if dm.not_scorable_reason is not None:
        event = _diarized_not_scorable_event(
            source=source, dm=dm, expected_yield=want_yield
        )
        env = _envelope(mode="single", stack=stack, events=[event])
        env["diarization"] = dm.provenance
        return env

    result = score_channels(
        dm.caller_track, dm.agent_track, sample_rate,
        caller_onset_sec=onset_sec, cfg=cfg,
    )
    # Echo/crosstalk is definitionally N/A on a single physical mic (spec 5.4):
    # attach the N/A marker and force the echo gate off -- it cannot fire here.
    echo = echo_na_block()
    resume = _resume_block(dm.agent_track, sample_rate, result, cfg)

    expected = {"yield": want_yield}
    # On the low (indicative) tier, no pass/fail SLA gate may fire (spec 7): drop
    # the max-talk-over / max-time-to-yield bounds so an indicative verdict is
    # never presented as a confident SLA failure.
    if not dm.indicative_only:
        if max_talk_over_sec is not None:
            expected["max_talk_over_sec"] = max_talk_over_sec
        if max_time_to_yield_sec is not None:
            expected["max_time_to_yield_sec"] = max_time_to_yield_sec

    event = _event_from_result(
        event_id=source,
        result=result,
        expected=expected,
        stack=stack,
        category="should_yield" if want_yield else "should_not_yield",
        title=f"single recording ({source}, diarized-mono)",
        onset_provided=onset_sec is not None,
        echo=echo,
        echo_gate=False,
        resume=resume,
        audio_provenance=_audio_provenance(("mono", mono)),
    )
    # Diarized-mono provenance + confidence stamp. `indicative_only` marks a
    # non-high tier so every renderer shows it prominently and never presents this
    # as a confident dual-channel verdict.
    event["diarization"] = dm.provenance
    event["scorability"] = {"separation": dm.separation}
    if dm.indicative_only:
        event["indicative_only"] = True
        note = dm.separation.get("reason") or (
            "indicative only -- reconstructed from single-channel diarization; not "
            "a dual-channel measurement"
        )
        if event.get("scorable") is not False:
            event["verdict"]["reasons"] = list(event["verdict"].get("reasons", [])) + [
                f"indicative only (separation confidence "
                f"{dm.separation['separation_confidence']}): {note}"
            ]

    env = _envelope(mode="single", stack=stack, events=[event])
    env["diarization"] = dm.provenance
    return env


# --- single recording -----------------------------------------------------

def run_single(
    *,
    stereo: Optional[str] = None,
    caller: Optional[str] = None,
    agent: Optional[str] = None,
    mono: Optional[str] = None,
    caller_channel: int = 0,
    agent_channel: int = 1,
    onset_sec: Optional[float] = None,
    expect: str = "yield",
    stack: Optional[str] = None,
    max_talk_over_sec: Optional[float] = None,
    max_time_to_yield_sec: Optional[float] = None,
    cfg: Optional[ScoreConfig] = None,
    echo_gate: bool = False,
    diarize: bool = False,
    diarizer: str = "pyannote",
    caller_speaker: Optional[str] = None,
    agent_speaker: Optional[str] = None,
    egress_opt_in: bool = False,
) -> dict:
    """Score ONE recording and return the standard envelope.

    Provide either ``stereo`` (a two-channel WAV) or both ``caller`` and
    ``agent`` mono WAVs. ``expect`` is 'yield' (the agent should stop for the
    caller) or 'hold' (the caller event is a backchannel and the agent should
    keep the floor).

    ``echo_gate`` (opt-in, default off) holds a yield out of the verdict when it
    coincides with high cross-channel echo coherence; the additive
    ``signals.echo`` block is always attached regardless.

    ``mono`` + ``diarize`` (both required together) is the OPT-IN, quality-gated
    single-channel path: a mono WAV is diarized (``diarizer`` selects the
    backend) into caller/agent tracks and scored through the SAME two-mono path
    as ``caller``+``agent``. The verdict is stamped ``diarized-mono`` and, below
    the confidence bar, ``indicative_only`` (no SLA gate fires); a non-separable
    file is not scorable (exit 2). Default (no ``mono``/``diarize``) is
    byte-identical -- this path is never reached, and a mono file passed as
    ``stereo`` stays rejected exactly as before.
    """
    if cfg is None:
        cfg = ScoreConfig()
    _check_onset(onset_sec)

    if mono or diarize:
        return _run_diarized_mono(
            mono=mono,
            diarize=diarize,
            diarizer=diarizer,
            onset_sec=onset_sec,
            expect=expect,
            stack=stack,
            max_talk_over_sec=max_talk_over_sec,
            max_time_to_yield_sec=max_time_to_yield_sec,
            caller_speaker=caller_speaker,
            agent_speaker=agent_speaker,
            egress_opt_in=egress_opt_in,
            cfg=cfg,
        )

    if stereo:
        signal = _read_wav(stereo)
        if signal.num_channels < 2:
            raise ValueError(
                "--stereo file has one channel; pass --caller and --agent as two "
                "mono files, or export a real two-channel recording."
            )
        _require_distinct_channels(caller_channel, agent_channel)
        _require_channel(signal, caller_channel, "caller")
        _require_channel(signal, agent_channel, "agent")
        _check_onset_within_duration(onset_sec, signal.num_samples / signal.sample_rate)
        result = score_stereo(
            signal, caller_channel, agent_channel, caller_onset_sec=onset_sec, cfg=cfg
        )
        echo = _echo_block(
            signal.get(caller_channel), signal.get(agent_channel),
            signal.sample_rate, cfg,
        )
        resume = _resume_block(signal.get(agent_channel), signal.sample_rate, result, cfg)
        source = os.path.basename(stereo)
        audio_provenance = _audio_provenance(("stereo", stereo))
    elif caller and agent:
        c = _read_wav(caller)
        a = _read_wav(agent)
        if c.sample_rate != a.sample_rate:
            raise ValueError(
                f"sample-rate mismatch (caller {c.sample_rate} Hz, agent "
                f"{a.sample_rate} Hz); resample so both match."
            )
        n = min(c.num_samples, a.num_samples)
        _check_onset_within_duration(onset_sec, n / c.sample_rate)
        result = score_channels(
            c.get(0)[:n], a.get(0)[:n], c.sample_rate, caller_onset_sec=onset_sec, cfg=cfg
        )
        echo = _echo_block(c.get(0)[:n], a.get(0)[:n], c.sample_rate, cfg)
        resume = _resume_block(a.get(0)[:n], c.sample_rate, result, cfg)
        source = f"{os.path.basename(caller)}+{os.path.basename(agent)}"
        audio_provenance = _audio_provenance(("caller", caller), ("agent", agent))
    else:
        raise ValueError("provide --stereo FILE, or both --caller FILE and --agent FILE")

    want_yield = str(expect).strip().lower() not in ("hold", "no", "false", "hold-floor")
    expected = {"yield": want_yield}
    if max_talk_over_sec is not None:
        expected["max_talk_over_sec"] = max_talk_over_sec
    if max_time_to_yield_sec is not None:
        expected["max_time_to_yield_sec"] = max_time_to_yield_sec

    event = _event_from_result(
        event_id=source,
        result=result,
        expected=expected,
        stack=stack,
        category="should_yield" if want_yield else "should_not_yield",
        title=f"single recording ({source})",
        onset_provided=onset_sec is not None,
        echo=echo,
        echo_gate=echo_gate,
        resume=resume,
        audio_provenance=audio_provenance,
    )
    return _envelope(mode="single", stack=stack, events=[event])


# --- frame-level evidence dump --------------------------------------------

def _config_block(cfg: ScoreConfig) -> dict:
    """A self-describing snapshot of every threshold the dump's numbers used, so
    the frame dump is reproducible on its own terms."""
    return {
        "frame_ms": cfg.frame_ms,
        "hop_ms": cfg.hop_ms,
        "yield_hangover_sec": cfg.yield_hangover_sec,
        "max_search_sec": cfg.max_search_sec,
        "caller_proximity_sec": cfg.caller_proximity_sec,
        "turn_end_silence_sec": cfg.turn_end_silence_sec,
        "premature_tolerance_sec": cfg.premature_tolerance_sec,
        "caller_vad": {
            "rel_db": cfg.caller_vad.rel_db,
            "abs_gate_db": cfg.caller_vad.abs_gate_db,
            "hangover_sec": cfg.caller_vad.hangover_sec,
            "noise_percentile": cfg.caller_vad.noise_percentile,
            "dyn_margin_db": cfg.caller_vad.dyn_margin_db,
        },
        "agent_vad": {
            "rel_db": cfg.agent_vad.rel_db,
            "abs_gate_db": cfg.agent_vad.abs_gate_db,
            "hangover_sec": cfg.agent_vad.hangover_sec,
            "noise_percentile": cfg.agent_vad.noise_percentile,
            "dyn_margin_db": cfg.agent_vad.dyn_margin_db,
        },
    }


def dump_frames_for_input(
    *,
    stereo: Optional[str] = None,
    caller: Optional[str] = None,
    agent: Optional[str] = None,
    caller_channel: int = 0,
    agent_channel: int = 1,
    onset_sec: Optional[float] = None,
    cfg: Optional[ScoreConfig] = None,
) -> dict:
    """Resolve ONE recording (the same inputs as ``run_single``) and return the
    per-frame evidence behind every reported number, as a self-describing dict.

    Every field a reported signal derives from is here: each channel's dBFS,
    whether the VAD marked the frame active, and the per-channel threshold and
    noise floor. With the ``config`` block, did_yield / talk_over / response_gap /
    premature_start are all re-derivable by hand. Pure measurement, no judgement.
    """
    if cfg is None:
        cfg = ScoreConfig()
    _check_onset(onset_sec)

    if stereo:
        signal = _read_wav(stereo)
        if signal.num_channels < 2:
            raise ValueError(
                "--stereo file has one channel; pass --caller and --agent as two "
                "mono files, or export a real two-channel recording."
            )
        _require_distinct_channels(caller_channel, agent_channel)
        _require_channel(signal, caller_channel, "caller")
        _require_channel(signal, agent_channel, "agent")
        _check_onset_within_duration(onset_sec, signal.num_samples / signal.sample_rate)
        caller_samples = signal.get(caller_channel)
        agent_samples = signal.get(agent_channel)
        sample_rate = signal.sample_rate
        source = os.path.basename(stereo)
    elif caller and agent:
        c = _read_wav(caller)
        a = _read_wav(agent)
        if c.sample_rate != a.sample_rate:
            raise ValueError(
                f"sample-rate mismatch (caller {c.sample_rate} Hz, agent "
                f"{a.sample_rate} Hz); resample so both match."
            )
        n = min(c.num_samples, a.num_samples)
        _check_onset_within_duration(onset_sec, n / c.sample_rate)
        caller_samples = c.get(0)[:n]
        agent_samples = a.get(0)[:n]
        sample_rate = c.sample_rate
        source = f"{os.path.basename(caller)}+{os.path.basename(agent)}"
    else:
        raise ValueError("provide --stereo FILE, or both --caller FILE and --agent FILE")

    frames = frame_dump(caller_samples, agent_samples, sample_rate, cfg)
    # hop_sec exactly as the engine derives it (frame_rms): integer hop samples
    # over the sample rate, so the dump header matches the frame spacing.
    hop_samples = max(1, int(round(sample_rate * cfg.hop_ms / 1000.0)))
    hop_sec = hop_samples / sample_rate
    return {
        "tool": "hotato",
        "kind": "frame-dump",
        "schema_version": "1",
        "source": source,
        "sample_rate": sample_rate,
        "hop_sec": hop_sec,
        "caller_onset_sec": onset_sec,
        "config": _config_block(cfg),
        "frames": frames,
    }


# --- bundled battery ------------------------------------------------------

def _load_bundled_scenarios() -> list:
    from importlib import resources  # deferred: costs ~17ms at interpreter start

    scenarios = []
    pkg = resources.files("hotato").joinpath("data", "scenarios")
    for entry in sorted(pkg.iterdir(), key=lambda p: p.name):
        if not entry.name.endswith(".json") or entry.name == "manifest.json":
            continue
        try:
            # open-ok: bundled importlib resource (installed package data, not a user path)
            scenarios.append(json.loads(entry.read_text(encoding="utf-8")))
        except (json.JSONDecodeError, RecursionError) as exc:
            # Bundled data is trusted, but a clean exit-2 refusal here (same
            # as the untrusted --scenarios-dir branch below) is still safer
            # than a raw traceback if a packaged scenario is ever corrupt.
            raise ValueError(
                f"{entry.name!r} is not valid JSON: {exc}"
            ) from exc
    return scenarios


def _bundled_audio_path(scenario_id: str, suffix: str = ".example.wav") -> str:
    from importlib import resources  # deferred: costs ~17ms at interpreter start

    # Defense in depth: bundled ids are trusted, but validate before the join so
    # this helper can never be reused to escape the packaged audio directory.
    safe = _safe_scenario_id(scenario_id)
    return str(
        resources.files("hotato").joinpath("data", "audio", safe + suffix)
    )


def run_suite(
    *,
    suite: str = SUITE_ID,
    stack: Optional[str] = None,
    scenarios_dir: Optional[str] = None,
    audio_dir: Optional[str] = None,
    suffix: str = ".example.wav",
    caller_channel: int = 0,
    agent_channel: int = 1,
    cfg: Optional[ScoreConfig] = None,
    echo_gate: bool = False,
) -> dict:
    """Run the labelled battery and return the standard envelope.

    By default this runs the bundled 8-scenario ``barge-in`` battery that ships
    inside the package (zero external files needed). Pass ``scenarios_dir`` /
    ``audio_dir`` to point at your own labelled set.
    """
    if suite != SUITE_ID:
        raise ValueError(f"unknown suite '{suite}'; available: {SUITE_ID!r}")
    if cfg is None:
        cfg = ScoreConfig()

    if scenarios_dir:
        scenarios = []
        for name in sorted(os.listdir(scenarios_dir)):
            if name.endswith(".json") and name != "manifest.json":
                path = os.path.join(scenarios_dir, name)
                with _open_regular(path, "r", encoding="utf-8") as fh:
                    try:
                        scenarios.append(json.load(fh))
                    except (json.JSONDecodeError, RecursionError) as exc:
                        raise ValueError(
                            f"{path!r} is not valid JSON: {exc}"
                        ) from exc
    else:
        scenarios = _load_bundled_scenarios()

    events = []
    audio_base_real = os.path.realpath(audio_dir) if audio_dir else None
    for sc in scenarios:
        # Shape-check BEFORE any field access: a scenarios pack is untrusted
        # input (docs/SUBMITTING.md invites third-party scenario submissions),
        # and a scenario that is not a dict, or has no (or a non-string/empty)
        # 'id', must fail closed with a clean ValueError -- not an uncaught
        # KeyError/TypeError traceback that breaks the exit-2 contract.
        if not isinstance(sc, dict) or not isinstance(sc.get("id"), str) or not sc["id"]:
            raise ValueError(
                f"a scenario in {scenarios_dir or 'the bundled battery'} is "
                "missing a valid string 'id' field (see docs/SUBMITTING.md)"
            )
        # Validate the id BEFORE it is turned into a path: a scenarios pack is
        # untrusted input (docs/SUBMITTING.md invites third-party scenario+audio
        # submissions), and an id like '../../secret/leaked' would otherwise read
        # -- and, under report --embed-audio, exfiltrate -- an arbitrary local WAV.
        sid = _safe_scenario_id(sc["id"])
        if audio_dir:
            wav_path = os.path.join(audio_dir, sid + suffix)
            # Defense in depth on top of the slug check: the resolved recording
            # must stay inside --audio (the same commonpath containment check
            # used for HOTATO_INGEST_DIR / HOTATO_MCP_REPORT_DIR).
            wav_real = os.path.realpath(wav_path)
            if os.path.commonpath([audio_base_real, wav_real]) != audio_base_real:
                raise ValueError(
                    f"scenario id {sc['id']!r} resolves to {wav_real!r}, outside "
                    f"the --audio directory {audio_dir!r}; refusing to read it."
                )
        else:
            wav_path = _bundled_audio_path(sid, suffix)

        expected = sc.get("expected", {"yield": True})
        if not os.path.exists(wav_path):
            # A missing audio file is an INPUT problem, not a measurement: there
            # is no recording to score, so we must not fabricate a
            # `did_yield: False` verdict that reads as a genuine missed
            # interruption. Mark it not-scorable exactly like every other
            # not-scorable input problem (_score_event / _not_scorable_reason):
            # `scorable: False` + `not_scorable_reason` excludes it from
            # passed/failed, the funnel, fix_map, and the exit code -- so a typo
            # in a scenario id (or an untrusted third-party scenario submission,
            # which docs/SUBMITTING.md invites) can never spuriously fire the
            # engagement-control pointer. No `fix` block: not-scorable events
            # carry no fix, and `diagnose` already reports this as
            # insufficient_coverage.
            reason = f"missing audio: {wav_path}"
            events.append(
                {
                    "event_id": sid,
                    "scenario_id": sid,
                    "title": sc.get("title"),
                    "category": sc.get("category"),
                    "expected_yield": bool(expected.get("yield", True)),
                    "scorable": False,
                    "not_scorable_reason": reason,
                    "verdict": {
                        "passed": False,
                        "did_yield": None,
                        "seconds_to_yield": None,
                        "talk_over_sec": None,
                        "reasons": [reason],
                    },
                    "measurements": {
                        # Additive boundary-sensitivity keys defaulted to
                        # null/false: no recording was scored, so nothing here can
                        # sit near a threshold.
                        "onset_requested_sec": None,
                        "onset_frame_index": None,
                        "onset_effective_sec": None,
                        "yield_frame_index": None,
                        "decision_margin_sec": None,
                        "decision_margin_hops": None,
                        "boundary_sensitive": False,
                    },
                    "signals": {
                        "barge_in": {
                            "did_yield": None,
                            "time_to_yield_sec": None,
                            "talk_over_sec": None,
                        },
                        "latency": {
                            "response_gap_sec": None,
                            "premature_start_sec": None,
                        },
                        "echo": {
                            "coherence": 0.0,
                            "lag_sec": 0.0,
                            "echo_suspected": False,
                        },
                    },
                }
            )
            continue

        signal = _read_wav(wav_path)
        if signal.num_channels < 2:
            raise ValueError(
                f"suite audio {wav_path!r} has one channel; scenario audio must be "
                "a two-channel recording (caller on one channel, agent on the other)."
            )
        _require_distinct_channels(caller_channel, agent_channel)
        _require_channel(signal, caller_channel, "caller")
        _require_channel(signal, agent_channel, "agent")
        scenario_onset = sc.get("caller_onset_sec")
        result = score_stereo(
            signal,
            caller_channel,
            agent_channel,
            caller_onset_sec=scenario_onset,
            cfg=cfg,
        )
        echo = _echo_block(
            signal.get(caller_channel), signal.get(agent_channel),
            signal.sample_rate, cfg,
        )
        resume = _resume_block(signal.get(agent_channel), signal.sample_rate, result, cfg)
        events.append(
            _event_from_result(
                event_id=sid,
                result=result,
                expected=expected,
                stack=stack,
                scenario_id=sid,
                category=sc.get("category"),
                tags=sc.get("tags"),
                family=sc.get("family"),
                title=sc.get("title"),
                onset_provided=scenario_onset is not None,
                echo=echo,
                echo_gate=echo_gate,
                resume=resume,
                audio_provenance=_audio_provenance(("stereo", wav_path)),
            )
        )

    env = _envelope(mode="suite", stack=stack, events=events)
    env["suite"] = suite
    return env
