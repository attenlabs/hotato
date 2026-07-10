"""The three objective barge-in signals, measured from audio energy.

Given a caller channel and an agent channel, this computes:

  did_yield        bool   did the agent stop speaking after the caller
                          took the floor, within the search window
  time_to_yield    float  seconds from caller onset to the agent going quiet
                          (None if the agent never yielded)
  talk_over        float  seconds of overlap after caller onset while both
                          the caller and the agent were speaking

These are timing measurements, not judgements about any detector's internal
quality. There is no accuracy percentage here and none is implied. If you
disagree with a number, the frames are inspectable and every threshold is a
parameter.
"""

from __future__ import annotations

from dataclasses import dataclass, asdict, field
from typing import Optional

from .audio import Signal, frame_rms, to_dbfs
from .vad import (
    BackendUnavailable,
    VADParams,
    VADResult,
    energy_vad,
    first_active_sec,
    neural_vad,
)


@dataclass
class ScoreConfig:
    frame_ms: float = 20.0
    hop_ms: float = 10.0
    yield_hangover_sec: float = 0.20   # agent must stay quiet this long to count as yielded
    max_search_sec: float = 3.0        # how long after onset we look for a yield
    caller_proximity_sec: float = 0.5  # a yield only counts if the caller held the floor near it
    turn_end_silence_sec: float = 0.20  # caller must stay quiet this long for their turn to count as ended
    premature_tolerance_sec: float = 0.05  # agent may lead the caller's turn end by up to this before it counts as a premature start
    onset_min_run_sec: float = 0.05    # minimum sustained active run for the caller onset to count
    agent_onset_lookback_sec: float = 0.10  # window before onset checked for whether the agent was already talking
    caller_vad: VADParams = None
    agent_vad: VADParams = None

    def __post_init__(self):
        if self.caller_vad is None:
            self.caller_vad = VADParams()
        if self.agent_vad is None:
            self.agent_vad = VADParams()


@dataclass
class ScoreResult:
    caller_onset_sec: float
    agent_talking_at_onset: bool
    did_yield: bool
    time_to_yield_sec: Optional[float]
    talk_over_sec: float
    hop_sec: float
    notes: str = ""
    # Namespaced signal bus. Additive by design: every dimension is a sub-dict so
    # new dimensions slot in without touching the existing top-level fields. The
    # three ``barge_in`` values mirror the top-level fields byte-for-byte (same
    # rounding), so nothing that already consumed those fields changes. Reserved
    # future keys (documented here, deliberately NOT implemented yet):
    # "overlap", "resume", "backchannel".
    signals: dict = field(default_factory=dict)
    # Detection provenance (additive, defaulted so older constructions stay
    # valid): the onset `first_active_sec` found on the caller channel, rounded
    # like caller_onset_sec, or None when the caller channel shows no sustained
    # activity at all. This lets the caller of score_channels tell "no caller
    # speech was detectable" apart from a genuine onset near 0.0. It changes no
    # numeric output: the scoring math above it is untouched.
    detected_caller_onset_sec: Optional[float] = None
    # Boundary-sensitivity provenance (additive, defaulted so older constructions
    # stay valid). These expose the QUANTIZED onset the scorer actually used and the
    # yield frame it landed on, so a reader can see where a near-threshold result
    # sat on the frame grid. They change no existing numeric output.
    #   onset_requested_sec  the caller_onset_sec argument as passed in (None when
    #                        the onset was auto-detected rather than supplied).
    #   onset_frame_index    the integer hop index the onset was snapped to.
    #   onset_effective_sec  onset_frame_index * hop_sec -- the quantized onset time
    #                        actually used (kept UNROUNDED so it equals the product
    #                        exactly).
    #   yield_frame_index    the hop index of the counted yield, or None when the
    #                        agent did not yield.
    onset_requested_sec: Optional[float] = None
    onset_frame_index: Optional[int] = None
    onset_effective_sec: Optional[float] = None
    yield_frame_index: Optional[int] = None

    def as_dict(self):
        d = asdict(self)
        return d


def _run_vad(samples, sample_rate, cfg: ScoreConfig, params: VADParams) -> VADResult:
    rms, hop_sec = frame_rms(samples, sample_rate, cfg.frame_ms, cfg.hop_ms)
    # `energy` is the deterministic reference and the default; it never touches the
    # neural seam. `neural` is an opt-in, non-reference cross-check that returns the
    # identical VADResult shape. An unknown backend is a hard error, never a silent
    # fallback that could change a published number's identity.
    backend = getattr(params, "backend", "energy")
    if backend == "energy":
        return energy_vad(rms, hop_sec, params)
    if backend == "neural":
        return neural_vad(samples, sample_rate, rms, hop_sec, params)
    raise BackendUnavailable(
        f"unknown VAD backend {backend!r}; use 'energy' (default, the reference) "
        "or 'neural' (optional, non-reference cross-check)"
    )


def _caller_turn_end_idx(active, onset_idx, silence_frames, n):
    """Index of the caller's turn END: having taken the floor at/after ``onset_idx``,
    the first frame the caller goes quiet and STAYS quiet for at least
    ``silence_frames``. Returns None if the caller never activates after onset, or
    is still active when the recording ends (turn end not derivable -> null)."""
    i = max(0, onset_idx)
    while i < n and not active[i]:
        i += 1
    if i >= n:
        return None
    while i < n:
        if not active[i]:
            run = 0
            j = i
            while j < n and not active[j]:
                run += 1
                if run >= silence_frames:
                    return i
                j += 1
            i = j
        else:
            i += 1
    return None


def _agent_response_onset_idx(active, onset_idx, n):
    """Index of the agent's NEXT turn start after the caller took the floor: skip
    any agent speech already in progress at ``onset_idx`` (the pre-caller turn or
    the yield tail), then return the first frame the agent becomes active again.
    Returns None if the agent never starts a fresh run after onset (e.g. an agent
    that simply never stops talking -- that is a talk_over signal, not a latency
    onset)."""
    i = max(0, onset_idx)
    while i < n and active[i]:
        i += 1
    while i < n and not active[i]:
        i += 1
    return i if i < n else None


def score_channels(
    caller_samples,
    agent_samples,
    sample_rate: int,
    caller_onset_sec: Optional[float] = None,
    cfg: ScoreConfig = None,
) -> ScoreResult:
    """Score one call from two aligned channels (same sample rate and length)."""
    if cfg is None:
        cfg = ScoreConfig()

    caller = _run_vad(caller_samples, sample_rate, cfg, cfg.caller_vad)
    agent = _run_vad(agent_samples, sample_rate, cfg, cfg.agent_vad)
    hop = caller.hop_sec
    n = min(len(caller.active), len(agent.active))

    # 1. caller onset: use the label if given, otherwise detect it.
    # Capture the requested onset (as passed in) BEFORE it is overwritten by the
    # detected value, so we can report what the caller actually asked for.
    onset_requested_sec = caller_onset_sec
    detected_onset = first_active_sec(caller.active, hop, min_run_sec=cfg.onset_min_run_sec)
    if caller_onset_sec is None:
        caller_onset_sec = detected_onset
    onset_idx = int(round(caller_onset_sec / hop)) if caller_onset_sec >= 0 else 0
    onset_idx = max(0, min(onset_idx, n - 1))

    # Was the agent actually speaking when the caller came in? If not, there is
    # nothing to yield and did_yield is not meaningful.
    lookback = max(1, int(round(cfg.agent_onset_lookback_sec / hop)))
    agent_talking_at_onset = any(
        agent.active[j] for j in range(max(0, onset_idx - lookback), onset_idx + 1)
    )

    # 2. find the yield: first frame at/after onset where the agent goes quiet
    #    for yield_hangover_sec AND the caller actually held the floor nearby.
    #    The second condition matters: an agent that simply finishes its own
    #    sentence a few seconds after an isolated backchannel has not "yielded"
    #    to the caller, so we do not count that as a barge-in response.
    yield_frames = max(1, int(round(cfg.yield_hangover_sec / hop)))
    grace = max(1, int(round(cfg.caller_proximity_sec / hop)))
    search_end = min(n, onset_idx + int(round(cfg.max_search_sec / hop)))
    did_yield = False
    time_to_yield = None
    yield_idx = search_end
    i = onset_idx
    while i < search_end:
        if not agent.active[i]:
            run = 0
            j = i
            while j < n and not agent.active[j]:
                run += 1
                if run >= yield_frames:
                    break
                j += 1
            if run >= yield_frames:
                lo = max(0, i - grace)
                hi = min(len(caller.active), i + grace)
                caller_had_floor = any(caller.active[k] for k in range(lo, hi))
                if caller_had_floor:
                    did_yield = True
                    yield_idx = i
                    time_to_yield = max(0.0, (i - onset_idx) * hop)
                    break
            i = j + 1
        else:
            i += 1

    # 3. talk-over: overlap seconds from onset up to the yield point (or the end
    #    of the search window if the agent never yielded).
    overlap_end = yield_idx if did_yield else search_end
    overlap_frames = 0
    for k in range(onset_idx, overlap_end):
        if k < len(caller.active) and k < len(agent.active) and caller.active[k] and agent.active[k]:
            overlap_frames += 1
    talk_over_sec = overlap_frames * hop

    notes = ""
    if not agent_talking_at_onset:
        notes = (
            "agent was not speaking at caller onset; did_yield is not meaningful "
            "for this recording (check channel assignment and onset time)"
        )

    # 4. latency (endpointing) signals: PURE TIMING on the same two VAD tracks,
    #    no new model. response_gap = seconds from the caller's turn end to the
    #    agent's next onset; premature_start = seconds the agent's onset LEADS the
    #    caller's turn end (the agent stepping on the human). Both are null when
    #    they are not derivable from the tracks, never fabricated.
    silence_frames = max(1, int(round(cfg.turn_end_silence_sec / hop)))
    tol_frames = max(0, int(round(cfg.premature_tolerance_sec / hop)))
    turn_end_idx = _caller_turn_end_idx(caller.active, onset_idx, silence_frames, n)
    resp_onset_idx = _agent_response_onset_idx(agent.active, onset_idx, n)
    response_gap_sec = None
    premature_start_sec = None
    if turn_end_idx is not None and resp_onset_idx is not None:
        lead = turn_end_idx - resp_onset_idx  # > 0 when the agent starts before the caller finishes
        if lead > tol_frames:
            premature_start_sec = round(lead * hop, 3)
            response_gap_sec = None
        else:
            premature_start_sec = 0.0
            response_gap_sec = round(max(0, resp_onset_idx - turn_end_idx) * hop, 3)

    # Rounded, byte-compatible copies of the three originals for the signal bus.
    ttoy_rounded = round(time_to_yield, 3) if time_to_yield is not None else None
    talk_over_rounded = round(talk_over_sec, 3)
    signals = {
        "barge_in": {
            "did_yield": did_yield,
            "time_to_yield_sec": ttoy_rounded,
            "talk_over_sec": talk_over_rounded,
        },
        "latency": {
            "response_gap_sec": response_gap_sec,
            "premature_start_sec": premature_start_sec,
        },
    }

    return ScoreResult(
        caller_onset_sec=round(caller_onset_sec, 3) if caller_onset_sec >= 0 else -1.0,
        agent_talking_at_onset=agent_talking_at_onset,
        did_yield=did_yield,
        time_to_yield_sec=ttoy_rounded,
        talk_over_sec=talk_over_rounded,
        hop_sec=hop,
        notes=notes,
        signals=signals,
        detected_caller_onset_sec=(
            round(detected_onset, 3) if detected_onset >= 0 else None
        ),
        # Additive boundary-sensitivity provenance. onset_effective_sec is kept
        # UNROUNDED (onset_idx * hop) so onset_effective_sec == onset_frame_index *
        # hop_sec holds exactly for any reader that re-derives it.
        onset_requested_sec=onset_requested_sec,
        onset_frame_index=onset_idx,
        onset_effective_sec=onset_idx * hop,
        yield_frame_index=(yield_idx if did_yield else None),
    )


def score_stereo(
    signal: Signal,
    caller_channel: int,
    agent_channel: int,
    caller_onset_sec: Optional[float] = None,
    cfg: ScoreConfig = None,
) -> ScoreResult:
    return score_channels(
        signal.get(caller_channel),
        signal.get(agent_channel),
        signal.sample_rate,
        caller_onset_sec=caller_onset_sec,
        cfg=cfg,
    )


# --- pass / fail against a scenario's expected behavior -------------------

@dataclass
class Verdict:
    passed: bool
    reasons: list
    # Additive boundary-sensitivity fields (defaulted so existing constructions and
    # consumers of passed/reasons are unaffected). decision_margin_sec is the SIGNED
    # slack in seconds from the tightest binding threshold to the measured value
    # (positive = inside the pass boundary, magnitude = how much slack; negative =
    # over the line). decision_margin_hops is that slack expressed in frame hops.
    # boundary_sensitive is True when the result sits within one hop of flipping.
    # All three are None/False when no numeric bound applies (pure yield/hold).
    decision_margin_sec: Optional[float] = None
    decision_margin_hops: Optional[int] = None
    boundary_sensitive: bool = False


def evaluate(result: ScoreResult, expected: dict) -> Verdict:
    """Compare a ScoreResult to a scenario's `expected` block."""
    reasons = []
    want_yield = bool(expected.get("yield", True))

    if want_yield:
        if not result.did_yield:
            reasons.append("expected the agent to yield but it kept talking")
        else:
            max_ttoy = expected.get("max_time_to_yield_sec")
            if max_ttoy is not None and result.time_to_yield_sec is not None:
                if result.time_to_yield_sec > max_ttoy:
                    reasons.append(
                        f"yielded in {result.time_to_yield_sec:.2f}s, slower than the "
                        f"{max_ttoy:.2f}s bound"
                    )
            max_over = expected.get("max_talk_over_sec")
            if max_over is not None and result.talk_over_sec > max_over:
                reasons.append(
                    f"talked over the caller for {result.talk_over_sec:.2f}s, more than "
                    f"the {max_over:.2f}s bound"
                )
    else:
        if result.did_yield:
            reasons.append(
                "expected the agent to keep the floor but it yielded "
                "(a false or phantom barge-in)"
            )

    # --- decision margin (additive; does NOT affect passed/reasons above) ------
    # The signed distance from each binding numeric threshold to the measured
    # value. A margin only exists for a yield we actually measured against a
    # numeric bound: for an expect-yield, max_time_to_yield_sec gives
    # (bound - time_to_yield) and max_talk_over_sec gives (bound - talk_over).
    # When several bounds apply we keep the TIGHTEST (smallest slack) -- the one
    # closest to flipping the verdict. When no numeric bound applies (pure
    # yield/hold, or a yield that never happened) the margin is null and the
    # result is not treated as boundary-sensitive.
    decision_margin_sec = None
    decision_margin_hops = None
    boundary_sensitive = False
    margins = []
    if want_yield and result.did_yield:
        max_ttoy = expected.get("max_time_to_yield_sec")
        if max_ttoy is not None and result.time_to_yield_sec is not None:
            margins.append(max_ttoy - result.time_to_yield_sec)
        max_over = expected.get("max_talk_over_sec")
        if max_over is not None and result.talk_over_sec is not None:
            margins.append(max_over - result.talk_over_sec)
    if margins:
        margin = min(margins)  # tightest binding constraint = smallest slack
        decision_margin_sec = round(margin, 3)
        hop = result.hop_sec
        if hop:
            decision_margin_hops = int(round(margin / hop))
            boundary_sensitive = abs(decision_margin_hops) <= 1

    return Verdict(
        passed=len(reasons) == 0,
        reasons=reasons,
        decision_margin_sec=decision_margin_sec,
        decision_margin_hops=decision_margin_hops,
        boundary_sensitive=boundary_sensitive,
    )


# --- frame-level evidence dump --------------------------------------------

def frame_dump(
    caller_samples,
    agent_samples,
    sample_rate: int,
    cfg: ScoreConfig = None,
) -> list:
    """Per-frame evidence behind every derived signal, so any number this scorer
    reports can be re-derived by hand.

    This is measurement, not judgement. For each frame it reports each channel's
    energy in dBFS, whether the energy VAD marked that frame active, and the
    (per-channel, constant across frames) activity threshold and noise floor the
    decision used. Every one of those thresholds is an exposed ScoreConfig /
    VADParams parameter, so the whole active/inactive decision is reconstructable.
    """
    if cfg is None:
        cfg = ScoreConfig()
    c_rms, hop = frame_rms(caller_samples, sample_rate, cfg.frame_ms, cfg.hop_ms)
    a_rms, _ = frame_rms(agent_samples, sample_rate, cfg.frame_ms, cfg.hop_ms)
    c_db = to_dbfs(c_rms)
    a_db = to_dbfs(a_rms)
    c_vad = energy_vad(c_rms, hop, cfg.caller_vad)
    a_vad = energy_vad(a_rms, hop, cfg.agent_vad)
    n = min(len(c_vad.active), len(a_vad.active))
    frames = []
    for i in range(n):
        frames.append(
            {
                "t_sec": round(i * hop, 6),
                "caller_dbfs": round(c_db[i], 3),
                "agent_dbfs": round(a_db[i], 3),
                "caller_active": bool(c_vad.active[i]),
                "agent_active": bool(a_vad.active[i]),
                "caller_threshold_db": round(c_vad.threshold_db, 3),
                "caller_noise_floor_db": round(c_vad.noise_floor_db, 3),
                "agent_threshold_db": round(a_vad.threshold_db, 3),
                "agent_noise_floor_db": round(a_vad.noise_floor_db, 3),
            }
        )
    return frames
