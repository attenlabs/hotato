"""Echo / agent-self-interruption detection: is the caller channel carrying a
delayed, attenuated copy of the AGENT's own audio (leaked TTS) rather than an
independent human voice?

Why this exists. The barge-in scorer treats energy on the caller channel as a
caller event. If an agent without robust echo cancellation hears its own TTS
bleed back on the input channel and stops, the timing tracks alone cannot tell
that "yield" apart from a real one: energy is energy. A stop caused by the agent
hearing itself would otherwise be scored as a clean yield. This module supplies
the missing cross-channel evidence so that spurious yield can be flagged (and,
opt-in, held out of the verdict).

The signal is a deterministic cross-channel coherence computed on the same two
per-frame RMS envelopes the VAD already frames. Leaked TTS is, by construction, a
delayed scaled copy of the agent envelope, so the caller envelope aligns with a
lag-shifted agent envelope with high cosine similarity. Independent speech (real
turn-taking, backchannels, genuine barge-in) does not: the caller is loud when
the agent is quiet, so the envelopes barely overlap and the cosine stays low.

Everything here is stdlib-only and deterministic. It lives ENTIRELY in hotato's
own layer: it never touches the vendored ``_engine`` and it computes an ADDITIVE
optional ``signals.echo`` block; no existing number changes.
"""

from __future__ import annotations

from typing import List, Optional

__all__ = [
    "echo_signal",
    "echo_block_from_samples",
    "window_cosine",
    "DEFAULT_MAX_LAG_SEC",
    "DEFAULT_COHERENCE_THRESHOLD",
    "ECHO_NOTE",
]

# Echo round-trip delays on speakerphone / in-car / bad-mix paths sit well under
# half a second; the search covers 0 (a zero-delay mix) up to this bound.
DEFAULT_MAX_LAG_SEC = 0.5
# Cosine similarity at or above this, at the best lag, reads as "the caller
# envelope is a copy of the agent envelope". Calibrated so the bundled clean
# two-speaker fixtures stay well below it and the echo-bleed fixture sits near 1.
DEFAULT_COHERENCE_THRESHOLD = 0.7
# The minimum number of overlapping frames required before a coherence number is
# trusted; too few frames make a high cosine meaningless.
_MIN_OVERLAP_FRAMES = 8

ECHO_NOTE = (
    "echo coherence is cross-channel similarity, not intent: a high value means "
    "the caller channel looks like a delayed copy of the agent's own audio "
    "(likely leaked TTS / missing echo cancellation), so a yield here may be the "
    "agent hearing itself rather than a real caller."
)


def _cosine_at_lag(caller: List[float], agent: List[float], lag: int) -> Optional[float]:
    """Through-origin normalized cross-correlation (cosine similarity) of the
    caller envelope against the agent envelope shifted so the caller LAGS the
    agent by ``lag`` frames: pairs ``caller[i]`` with ``agent[i - lag]``.

    Returns None when the overlap is too short or either side has no energy in
    the window (an undefined cosine, never a fabricated number)."""
    n = min(len(caller), len(agent))
    if lag < 0 or lag >= n:
        return None
    dot = 0.0
    nc = 0.0
    na = 0.0
    count = 0
    for i in range(lag, n):
        c = caller[i]
        a = agent[i - lag]
        dot += c * a
        nc += c * c
        na += a * a
        count += 1
    if count < _MIN_OVERLAP_FRAMES or nc <= 0.0 or na <= 0.0:
        return None
    return dot / ((nc ** 0.5) * (na ** 0.5))


def window_cosine(
    caller_rms: List[float],
    agent_rms: List[float],
    start: int,
    end: int,
    lag: int,
) -> float:
    """Cosine similarity of ``caller_rms[start:end]`` against the agent envelope
    shifted so the caller lags the agent by ``lag`` frames. Returns 0.0 when the
    window is undefined (too short, or no energy on a side), never a fabricated
    number. Used by ``scan`` to score whether one caller run is echo-correlated."""
    dot = 0.0
    nc = 0.0
    na = 0.0
    count = 0
    lo = max(start, lag)
    hi = min(end, len(caller_rms))
    for i in range(lo, hi):
        j = i - lag
        if j < 0 or j >= len(agent_rms):
            continue
        c = caller_rms[i]
        a = agent_rms[j]
        dot += c * a
        nc += c * c
        na += a * a
        count += 1
    if count < _MIN_OVERLAP_FRAMES or nc <= 0.0 or na <= 0.0:
        return 0.0
    return dot / ((nc ** 0.5) * (na ** 0.5))


def echo_signal(
    caller_rms: List[float],
    agent_rms: List[float],
    hop_sec: float,
    *,
    max_lag_sec: float = DEFAULT_MAX_LAG_SEC,
    coherence_threshold: float = DEFAULT_COHERENCE_THRESHOLD,
) -> dict:
    """Cross-channel echo coherence for one recording (or one segment).

    ``caller_rms`` / ``agent_rms`` are the per-frame linear RMS envelopes (the
    same tracks the VAD frames), ``hop_sec`` their frame spacing. Returns the
    additive signal block::

        {"coherence": float in [0, 1], "lag_sec": float, "echo_suspected": bool}

    ``coherence`` is the best (highest) cosine similarity over lags 0..max_lag;
    ``lag_sec`` is the lag that achieved it; ``echo_suspected`` is coherence at
    or above ``coherence_threshold``. Deterministic and stdlib-only.

    When coherence is undefined (silent channel, too few frames) the block is
    ``coherence=0.0, lag_sec=0.0, echo_suspected=False`` -- never a false echo.
    """
    if hop_sec <= 0:
        raise ValueError(f"hop_sec must be > 0; got {hop_sec}.")
    n = min(len(caller_rms), len(agent_rms))
    max_lag = max(0, int(round(max_lag_sec / hop_sec)))
    max_lag = min(max_lag, max(0, n - 1))

    best_coh = 0.0
    best_lag = 0
    for lag in range(0, max_lag + 1):
        coh = _cosine_at_lag(caller_rms, agent_rms, lag)
        if coh is None:
            continue
        if coh > best_coh:
            best_coh = coh
            best_lag = lag

    coherence = round(best_coh, 3)
    return {
        "coherence": coherence,
        "lag_sec": round(best_lag * hop_sec, 3),
        "echo_suspected": coherence >= coherence_threshold,
    }


def echo_block_from_samples(
    caller_samples: List[float],
    agent_samples: List[float],
    sample_rate: int,
    *,
    frame_ms: float = 20.0,
    hop_ms: float = 10.0,
    max_lag_sec: float = DEFAULT_MAX_LAG_SEC,
    coherence_threshold: float = DEFAULT_COHERENCE_THRESHOLD,
) -> dict:
    """Compute the echo signal block directly from two channels of samples.

    Frames each channel with the SAME reference ``frame_rms`` the engine uses
    (read, never modified), then delegates to ``echo_signal``. This is the entry
    point the scorer calls so ``signals.echo`` is derived from the identical
    framing as the rest of the pipeline.
    """
    from ._engine.audio import frame_rms  # reference framing; not modified here

    rms_c, hop_sec = frame_rms(caller_samples, sample_rate, frame_ms, hop_ms)
    rms_a, _ = frame_rms(agent_samples, sample_rate, frame_ms, hop_ms)
    return echo_signal(
        rms_c,
        rms_a,
        hop_sec,
        max_lag_sec=max_lag_sec,
        coherence_threshold=coherence_threshold,
    )
