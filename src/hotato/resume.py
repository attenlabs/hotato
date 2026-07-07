"""Resume / restart-after-interrupt detection: once the agent has YIELDED, did
it come back, how fast, and did it re-answer from the top instead of finishing
what it was saying?

Why this exists. The barge-in scorer answers "did the agent stop?" (``did_yield``)
and "how fast?" (``seconds_to_yield``). It says nothing about what happens AFTER
the stop. But a very common real failure is the agent that stops, then restarts
its whole answer from the beginning -- the caller said one word, the agent went
quiet, and then re-read the entire paragraph it had already half-delivered. To
the timing verdict that is a clean yield; to the caller it is a wall of repeated
speech. This module measures the after-the-yield behaviour from the agent's own
VAD track so that restart is no longer invisible.

What it measures, purely from timing:

  resumed            did a FRESH agent onset appear within a window after the
                     yield (the agent started talking again), vs staying quiet
  resume_gap_sec     seconds from the yield to that fresh onset (None if no
                     resume)
  restart_suspected  is the post-resume agent speech an unusually long run --
                     the fingerprint of re-answering from the top rather than a
                     short continuation of the interrupted sentence

Explicitly OUT of scope: whether the resumed words are literally a repeat of the
earlier words. That is a transcript / semantic question (context loss), not a
timing one, and this module makes no claim about it. ``restart_suspected`` is a
timing heuristic on run length, not a transcript diff.

Everything here is stdlib-only and deterministic. It lives ENTIRELY in hotato's
own layer: it never touches the vendored ``_engine`` and it computes an ADDITIVE
optional ``signals.resume`` block; no existing number changes.
"""

from __future__ import annotations

from typing import List, Optional, Tuple

__all__ = [
    "resume_signal",
    "resume_block_from_samples",
    "agent_runs_from_samples",
    "DEFAULT_RESUME_WINDOW_SEC",
    "DEFAULT_RESTART_MIN_SEC",
    "RESUME_NOTE",
]

# A fresh agent onset this many seconds or less after the yield is counted as a
# resume attributable to the yield. Beyond it the agent has effectively handed
# the floor over and a later utterance is a new turn, not a resume.
DEFAULT_RESUME_WINDOW_SEC = 4.0
# The longest contiguous agent run at or after the resume onset must reach this
# length to read as "re-answered from the top". A short continuation (finishing
# the interrupted clause) stays well under it; a paragraph re-read runs long.
DEFAULT_RESTART_MIN_SEC = 2.0

RESUME_NOTE = (
    "resume is post-yield timing on the agent VAD track: resumed means the agent "
    "started speaking again within the window after it yielded, resume_gap_sec is "
    "how long that took, and restart_suspected flags an unusually long post-resume "
    "run (the fingerprint of re-answering from the top). Whether the words repeat "
    "is a transcript question and is out of scope here."
)


def _runs(active: List[bool], min_frames: int) -> List[Tuple[int, int]]:
    """Contiguous [start, end) frame spans where ``active`` is True and the span
    is at least ``min_frames`` long. Same run definition the scan pass uses, kept
    local so this module stays stdlib-only and independent of the engine."""
    runs: List[Tuple[int, int]] = []
    i = 0
    n = len(active)
    while i < n:
        if active[i]:
            j = i
            while j < n and active[j]:
                j += 1
            if j - i >= min_frames:
                runs.append((i, j))
            i = j
        else:
            i += 1
    return runs


def resume_signal(
    agent_active: List[bool],
    hop_sec: float,
    yield_time_sec: float,
    *,
    resume_window_sec: float = DEFAULT_RESUME_WINDOW_SEC,
    restart_min_sec: float = DEFAULT_RESTART_MIN_SEC,
    min_run_frames: int = 1,
) -> dict:
    """Post-yield resume / restart signal from the agent VAD activity track.

    ``agent_active`` is the per-frame boolean agent-speaking track (the same VAD
    the engine frames), ``hop_sec`` its frame spacing, ``yield_time_sec`` the
    absolute time in the recording at which the agent went quiet (the yield).
    Returns the additive block::

        {"resumed": bool, "resume_gap_sec": float | None, "restart_suspected": bool}

    ``resumed`` is True when the FIRST agent run that starts at or after the yield
    begins within ``resume_window_sec`` of it; ``resume_gap_sec`` is that delay.
    ``restart_suspected`` is True when the longest contiguous agent run at or
    after the resume onset is at least ``restart_min_sec`` long -- a timing proxy
    for re-answering from the top. When there is no resume the block is
    ``resumed=False, resume_gap_sec=None, restart_suspected=False``; never a
    fabricated resume. Deterministic and stdlib-only.
    """
    if hop_sec <= 0:
        raise ValueError(f"hop_sec must be > 0; got {hop_sec}.")
    yield_frame = int(round(yield_time_sec / hop_sec))
    window_frames = int(round(resume_window_sec / hop_sec))
    runs = _runs(agent_active, max(1, min_run_frames))

    # The first fresh agent onset at or after the yield: the run whose start is
    # at/after the yield frame. Runs that began before the yield are the speech
    # that got interrupted, not a resume.
    resume_run: Optional[Tuple[int, int]] = None
    for start, end in runs:
        if start >= yield_frame:
            resume_run = (start, end)
            break

    if resume_run is None:
        return {"resumed": False, "resume_gap_sec": None, "restart_suspected": False}

    start, _ = resume_run
    gap_frames = start - yield_frame
    if gap_frames > window_frames:
        # The agent did come back, but late enough that it is a new turn rather
        # than a resume of the yielded one.
        return {"resumed": False, "resume_gap_sec": None, "restart_suspected": False}

    # Longest contiguous agent run at or after the resume onset. A brief false
    # start followed by the real paragraph re-read still trips this, so a restart
    # is caught even when the resume itself is short.
    longest_post = 0
    for r_start, r_end in runs:
        if r_start >= start:
            longest_post = max(longest_post, r_end - r_start)
    restart_min_frames = int(round(restart_min_sec / hop_sec))

    return {
        "resumed": True,
        "resume_gap_sec": round(gap_frames * hop_sec, 3),
        "restart_suspected": longest_post >= restart_min_frames,
    }


def agent_runs_from_samples(
    agent_samples: List[float],
    sample_rate: int,
    agent_vad_params,
    *,
    frame_ms: float = 20.0,
    hop_ms: float = 10.0,
    onset_min_run_sec: float = 0.05,
):
    """Frame the agent channel exactly like the engine and return
    ``(agent_active, hop_sec, min_run_frames)``.

    Uses the engine's reference ``frame_rms`` and ``energy_vad`` (read, never
    modified) so the resume signal is derived from the identical VAD the rest of
    the pipeline uses."""
    from ._engine.audio import frame_rms  # reference framing; not modified here
    from ._engine.vad import energy_vad  # reference VAD; not modified here

    rms_a, hop_sec = frame_rms(agent_samples, sample_rate, frame_ms, hop_ms)
    agent_active = energy_vad(rms_a, hop_sec, agent_vad_params).active
    min_run_frames = max(1, int(round(onset_min_run_sec / hop_sec)))
    return agent_active, hop_sec, min_run_frames


def resume_block_from_samples(
    agent_samples: List[float],
    sample_rate: int,
    yield_time_sec: float,
    agent_vad_params,
    *,
    frame_ms: float = 20.0,
    hop_ms: float = 10.0,
    onset_min_run_sec: float = 0.05,
    resume_window_sec: float = DEFAULT_RESUME_WINDOW_SEC,
    restart_min_sec: float = DEFAULT_RESTART_MIN_SEC,
) -> dict:
    """Compute the resume signal block directly from the agent channel samples
    and the yield time.

    Frames the agent channel with the SAME reference framing and VAD the engine
    uses, then delegates to ``resume_signal``. This is the entry point the scorer
    calls so ``signals.resume`` is derived from the identical VAD track as the
    rest of the pipeline.
    """
    agent_active, hop_sec, min_run_frames = agent_runs_from_samples(
        agent_samples,
        sample_rate,
        agent_vad_params,
        frame_ms=frame_ms,
        hop_ms=hop_ms,
        onset_min_run_sec=onset_min_run_sec,
    )
    return resume_signal(
        agent_active,
        hop_sec,
        yield_time_sec,
        resume_window_sec=resume_window_sec,
        restart_min_sec=restart_min_sec,
        min_run_frames=min_run_frames,
    )
