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

import json
import os
import struct
import wave
from importlib import resources
from typing import Optional

from . import _engine
from ._engine.score import (
    ScoreConfig,
    evaluate,
    frame_dump,
    score_channels,
    score_stereo,
)
from .fixmap import classify_event, systemic_pointer

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
    "best_input": "stereo / two-channel recording with the caller and the agent on separate channels. Mono is accepted but the caller/agent separation is then only as good as the VAD, which lowers the ceiling further.",
    "does_not_do": [
        "no speaker identification",
        "no diarization",
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
    tags: Optional[list] = None,
    title: Optional[str] = None,
    onset_provided: bool = True,
) -> dict:
    verdict = evaluate(result, expected)
    expected_yield = bool(expected.get("yield", True))
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
            "caller_onset_sec": result.caller_onset_sec,
            "agent_talking_at_onset": result.agent_talking_at_onset,
            "hop_sec": result.hop_sec,
            "notes": result.notes,
        },
        # Namespaced signal bus (additive; schema_version stays "1"). signals.barge_in
        # mirrors the verdict's three original values byte-for-byte; signals.latency
        # adds the pure-timing endpointing measurements. New dimensions slot in here
        # without changing the existing verdict or measurements blocks.
        "signals": result.signals,
        "fix": None,
    }
    reason = _not_scorable_reason(
        result=result, expected_yield=expected_yield, onset_provided=onset_provided
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
        "funnel": systemic_pointer(scorable),
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
        signal = _engine.read_wav(path)
    except (wave.Error, EOFError, struct.error) as exc:
        raise ValueError(
            f"{path!r} is not a readable PCM WAV ({exc}). Export a PCM WAV, "
            "e.g. ffmpeg -i input -acodec pcm_s16le output.wav"
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
    # Truncation guard: for a well-formed file the wave header's declared frame
    # count equals the number of decoded samples per channel; a short data chunk
    # decodes fewer. Re-reading the header is best-effort and must never itself
    # fail the read.
    try:
        with wave.open(path, "rb") as wf:
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
    """Fail cleanly (ValueError -> exit 2) on an out-of-range channel index rather
    than let ``Signal.get`` raise a bare IndexError traceback deep in the engine."""
    if index < 0 or index >= signal.num_channels:
        raise ValueError(
            f"--{role}-channel {index} is out of range for a "
            f"{signal.num_channels}-channel recording "
            f"(valid channels: 0..{signal.num_channels - 1})."
        )


def _check_onset(onset_sec: Optional[float]) -> None:
    if onset_sec is not None and onset_sec < 0:
        raise ValueError(
            f"--onset must be >= 0 seconds (time from the start of the "
            f"recording); got {onset_sec}."
        )


# --- single recording -----------------------------------------------------

def run_single(
    *,
    stereo: Optional[str] = None,
    caller: Optional[str] = None,
    agent: Optional[str] = None,
    caller_channel: int = 0,
    agent_channel: int = 1,
    onset_sec: Optional[float] = None,
    expect: str = "yield",
    stack: Optional[str] = None,
    max_talk_over_sec: Optional[float] = None,
    max_time_to_yield_sec: Optional[float] = None,
    cfg: Optional[ScoreConfig] = None,
) -> dict:
    """Score ONE recording and return the standard envelope.

    Provide either ``stereo`` (a two-channel WAV) or both ``caller`` and
    ``agent`` mono WAVs. ``expect`` is 'yield' (the agent should stop for the
    caller) or 'hold' (the caller event is a backchannel and the agent should
    keep the floor).
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
        _require_channel(signal, caller_channel, "caller")
        _require_channel(signal, agent_channel, "agent")
        result = score_stereo(
            signal, caller_channel, agent_channel, caller_onset_sec=onset_sec, cfg=cfg
        )
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
        result = score_channels(
            c.get(0)[:n], a.get(0)[:n], c.sample_rate, caller_onset_sec=onset_sec, cfg=cfg
        )
        source = f"{os.path.basename(caller)}+{os.path.basename(agent)}"
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
        _require_channel(signal, caller_channel, "caller")
        _require_channel(signal, agent_channel, "agent")
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
    scenarios = []
    pkg = resources.files("hotato").joinpath("data", "scenarios")
    for entry in sorted(pkg.iterdir(), key=lambda p: p.name):
        if not entry.name.endswith(".json") or entry.name == "manifest.json":
            continue
        scenarios.append(json.loads(entry.read_text(encoding="utf-8")))
    return scenarios


def _bundled_audio_path(scenario_id: str, suffix: str = ".example.wav") -> str:
    return str(
        resources.files("hotato").joinpath("data", "audio", scenario_id + suffix)
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
                with open(os.path.join(scenarios_dir, name), encoding="utf-8") as fh:
                    scenarios.append(json.load(fh))
    else:
        scenarios = _load_bundled_scenarios()

    events = []
    for sc in scenarios:
        sid = sc["id"]
        if audio_dir:
            wav_path = os.path.join(audio_dir, sid + suffix)
        else:
            wav_path = _bundled_audio_path(sid, suffix)

        expected = sc.get("expected", {"yield": True})
        if not os.path.exists(wav_path):
            events.append(
                {
                    "event_id": sid,
                    "scenario_id": sid,
                    "title": sc.get("title"),
                    "category": sc.get("category"),
                    "expected_yield": bool(expected.get("yield", True)),
                    "verdict": {
                        "passed": False,
                        "did_yield": False,
                        "seconds_to_yield": None,
                        "talk_over_sec": 0.0,
                        "reasons": [f"missing audio: {wav_path}"],
                    },
                    "measurements": {},
                    "signals": {
                        "barge_in": {
                            "did_yield": False,
                            "time_to_yield_sec": None,
                            "talk_over_sec": 0.0,
                        },
                        "latency": {
                            "response_gap_sec": None,
                            "premature_start_sec": None,
                        },
                    },
                    "fix": {
                        "fix_class": "config",
                        "title": "Missing fixture audio",
                        "detail": f"Expected a recording at {wav_path}.",
                        "knob": None,
                        "pointer": None,
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
        events.append(
            _event_from_result(
                event_id=sid,
                result=result,
                expected=expected,
                stack=stack,
                scenario_id=sid,
                category=sc.get("category"),
                tags=sc.get("tags"),
                title=sc.get("title"),
                onset_provided=scenario_onset is not None,
            )
        )

    env = _envelope(mode="suite", stack=stack, events=events)
    env["suite"] = suite
    return env
