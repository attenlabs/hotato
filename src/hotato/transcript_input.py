"""Score a timestamped, speaker-labeled TRANSCRIPT through hotato's EXISTING
turn-taking scorer -- no audio -- so a text/chat agent is scorable.

The seam is deliberately thin and reuses the shipped primitives wholesale:

  * a transcript's per-turn ``[start, end)`` seconds are quantized to the SAME
    reference hop grid the diarizer uses (``diarize._hop_samples``), into two
    boolean caller/agent activity timelines -- the exact structure
    ``diarize.build_stub_backend(timelines=...)`` already consumes;
  * those timelines are handed to the stub diarizer in TRUTH mode and
    reconstructed into two masked tracks over a synthesized above-gate carrier,
    so the UNCHANGED ``_engine.score.score_channels`` produces the timing
    signals from a re-VAD of that carrier -- one scorer, never a second one.

HONESTY GATE (mandatory, :func:`apply_transcript_honesty_gate`): a SEQUENTIAL
transcript cannot represent two parties speaking at once, so the acoustic-overlap
signals (``talk_over_sec``, ``premature_start_sec``) are reported as ``null``
with a stated reason rather than a fabricated 0.0. The timestamp-derivable
signals (``did_yield``, ``seconds_to_yield``, ``response_gap_sec``,
``caller_onset_sec``) are honestly derived from the turn timings and left intact.
"""

from __future__ import annotations

import math
from typing import Dict, List, Optional

from . import diarize
from ._engine.score import ScoreConfig, ScoreResult, score_channels
from .diarize import SPEAKER_A, SPEAKER_B
from .errors import load_json_file as _load_json_file

__all__ = [
    "AGENT_ROLE_ALIASES",
    "CALLER_ROLE_ALIASES",
    "TRANSCRIPT_OVERLAP_REASON",
    "load_transcript_segments",
    "transcript_to_timelines",
    "score_transcript_timelines",
    "apply_transcript_honesty_gate",
]

# Case-insensitive role alias tables. An unmapped role is never silently
# dropped: it raises, naming the role, so a typo or an unexpected speaker label
# can never quietly vanish a whole channel's turns.
AGENT_ROLE_ALIASES = frozenset({"agent", "assistant", "bot", "ai", "system"})
CALLER_ROLE_ALIASES = frozenset({"caller", "user", "customer", "human"})

# The single honest reason the two acoustic-overlap signals are null on this
# path (see :func:`apply_transcript_honesty_gate`).
TRANSCRIPT_OVERLAP_REASON = (
    "requires overlap-preserving two-channel audio; a sequential transcript "
    "cannot represent acoustic overlap"
)


def load_transcript_segments(path: str) -> List[dict]:
    """Load the raw transcript turns from a JSON file, preserving each turn's
    keys (so a ``speaker``-labeled turn survives, unlike
    :func:`hotato.assert_.load_transcript_file`, which normalizes to ``role``).

    Accepts the same two shapes the rest of hotato reads: a plain JSON array of
    turn objects, or an object with a ``segments`` list (optionally nested one
    level under ``transcript``). Routed through
    :func:`hotato.errors.load_json_file`, so a FIFO/named-pipe path raises at
    once instead of blocking forever."""
    doc = _load_json_file(path)
    if isinstance(doc, list):
        return list(doc)
    if isinstance(doc, dict):
        if isinstance(doc.get("segments"), list):
            return list(doc["segments"])
        transcript = doc.get("transcript")
        if isinstance(transcript, dict) and isinstance(transcript.get("segments"), list):
            return list(transcript["segments"])
        raise ValueError(
            f"{path!r}: expected a JSON array of transcript turns, or an "
            "object with a 'segments' list (the shape hotato's transcribe / "
            "MCP surfaces write); found neither"
        )
    raise ValueError(f"{path!r}: a transcript file must be a JSON array or object")


def _is_number(x) -> bool:
    return isinstance(x, (int, float)) and not isinstance(x, bool)


def transcript_to_timelines(
    segments: List[dict],
    hop_sec: float,
    cfg: ScoreConfig,
    *,
    caller_role: Optional[str] = None,
    agent_role: Optional[str] = None,
) -> Dict[str, List[bool]]:
    """Quantize a list of transcript turns into two boolean caller/agent
    activity timelines on the reference hop grid.

    Each turn carries a role under ``role`` OR ``speaker`` plus numeric
    ``start``/``end`` seconds. Roles map to caller/agent via case-insensitive
    alias tables (:data:`AGENT_ROLE_ALIASES` / :data:`CALLER_ROLE_ALIASES`);
    ``caller_role`` / ``agent_role`` OVERRIDE the tables for a custom label. A
    role that maps to neither raises ``ValueError`` naming it -- never a silent
    drop. Each ``[start, end)`` is marked True over frames
    ``[floor(start/hop), ceil(end/hop))``, matching the diarizer's own
    quantization (``diarize.py``'s pyannote backend). Both timelines are padded
    to a common frame count.

    Returns ``{SPEAKER_A: caller_timeline, SPEAKER_B: agent_timeline}`` -- the
    exact label->timeline structure ``diarize.build_stub_backend`` consumes."""
    if hop_sec <= 0:
        raise ValueError(f"hop_sec must be > 0; got {hop_sec!r}")

    caller_override = caller_role.strip().lower() if caller_role else None
    agent_override = agent_role.strip().lower() if agent_role else None

    parsed = []  # (who, lo, hi)
    n_frames = 0
    for idx, seg in enumerate(segments):
        pos = idx + 1
        if not isinstance(seg, dict):
            raise ValueError(
                f"transcript turn #{pos} is not an object: {seg!r}"
            )
        role_raw = seg.get("role")
        if role_raw is None:
            role_raw = seg.get("speaker")
        if role_raw is None:
            raise ValueError(
                f"transcript turn #{pos} has no 'role' or 'speaker' field"
            )
        role = str(role_raw).strip().lower()

        start = seg.get("start")
        end = seg.get("end")
        if not _is_number(start) or not _is_number(end):
            raise ValueError(
                f"transcript turn #{pos} (role {role!r}) needs numeric 'start' "
                f"and 'end' seconds; got start={start!r}, end={end!r}"
            )
        start = float(start)
        end = float(end)
        if not end > start:
            raise ValueError(
                f"transcript turn #{pos} (role {role!r}) needs end > start; "
                f"got start={start}, end={end}"
            )

        if agent_override is not None and role == agent_override:
            who = "agent"
        elif caller_override is not None and role == caller_override:
            who = "caller"
        elif role in AGENT_ROLE_ALIASES:
            who = "agent"
        elif role in CALLER_ROLE_ALIASES:
            who = "caller"
        else:
            raise ValueError(
                f"transcript turn #{pos}: role {role!r} maps to neither the "
                f"caller nor the agent. Known agent roles: "
                f"{sorted(AGENT_ROLE_ALIASES)}; caller roles: "
                f"{sorted(CALLER_ROLE_ALIASES)}. Pass --agent-role/--caller-role "
                "to map a custom role."
            )

        lo = int(math.floor(start / hop_sec))
        hi = int(math.ceil(end / hop_sec))
        parsed.append((who, lo, hi))
        if hi > n_frames:
            n_frames = hi

    caller_tl = [False] * n_frames
    agent_tl = [False] * n_frames
    for who, lo, hi in parsed:
        track = caller_tl if who == "caller" else agent_tl
        for k in range(max(0, lo), min(n_frames, hi)):
            track[k] = True

    return {SPEAKER_A: caller_tl, SPEAKER_B: agent_tl}


def _synth_carrier(n_frames: int, hop_samples: int) -> List[float]:
    """A constant above-gate carrier of length ``n_frames * hop_samples``:
    alternating +/-0.3 samples (linear RMS ~0.3, i.e. ~-10.5 dBFS, well above
    the energy VAD's -60 dBFS gate). Masked per speaker in
    :func:`diarize.reconstruct_tracks`, so an active frame re-VADs active and a
    zeroed frame re-VADs inactive -- reproducing the timeline to within a hop."""
    n = n_frames * hop_samples
    return [0.3 if (i % 2 == 0) else -0.3 for i in range(n)]


def score_transcript_timelines(
    timelines: Dict[str, List[bool]],
    *,
    sample_rate: int = 16000,
    cfg: Optional[ScoreConfig] = None,
) -> ScoreResult:
    """Score two caller/agent activity timelines through the UNCHANGED engine.

    Reconstructs the two masked tracks over a synthesized above-gate carrier
    (via the stub diarizer in TRUTH mode + ``diarize.reconstruct_tracks``) and
    hands them to ``_engine.score.score_channels`` -- the identical two-mono
    contract the audio path uses -- so the timing signals come from the same
    scorer, never a re-implementation."""
    if cfg is None:
        cfg = ScoreConfig()
    hop_samples = diarize._hop_samples(sample_rate, cfg)
    hop_sec = hop_samples / sample_rate
    caller_tl = timelines[SPEAKER_A]
    n = len(caller_tl)
    carrier = _synth_carrier(n, hop_samples)
    backend = diarize.build_stub_backend(timelines=timelines)()
    result = backend(carrier, sample_rate, hop_sec, 2)
    caller_track, agent_track = diarize.reconstruct_tracks(
        carrier, result, SPEAKER_A, SPEAKER_B, sample_rate=sample_rate, cfg=cfg
    )
    return score_channels(
        caller_track, agent_track, sample_rate, caller_onset_sec=None, cfg=cfg
    )


def apply_transcript_honesty_gate(event: dict) -> dict:
    """Null out the acoustic-overlap signals on a transcript-scored event and
    stamp the reason, in place. A sequential transcript cannot represent two
    parties speaking at once, so ``talk_over_sec`` and ``premature_start_sec``
    are reported as ``null`` (never a fabricated 0.0), and cross-channel echo is
    definitionally N/A. The timestamp-derivable signals -- ``did_yield``,
    ``seconds_to_yield``, ``response_gap_sec``, ``caller_onset_sec`` -- are left
    untouched."""
    event["verdict"]["talk_over_sec"] = None

    barge_in = event["signals"].setdefault("barge_in", {})
    barge_in["talk_over_sec"] = None
    barge_in["talk_over_reason"] = TRANSCRIPT_OVERLAP_REASON

    latency = event["signals"].setdefault("latency", {})
    latency["premature_start_sec"] = None
    latency["premature_start_reason"] = TRANSCRIPT_OVERLAP_REASON

    event["signals"]["echo"] = diarize.echo_na_block()
    return event
