"""``hotato fixture create``: turn one bad call moment into a permanent
regression fixture.

The input is a recording you already have (one two-channel WAV, or two aligned
mono WAVs), the moment the caller took or attempted the floor (``--onset``),
and YOUR label for what the agent should have done (``--expect yield`` or
``--expect hold``). The output is a scenario JSON plus a two-channel example
WAV in the exact shape ``hotato run --scenarios DIR --audio DIR`` scores, so
the moment becomes a test the same command can run forever.

Hotato does not infer intent. You label the expected behavior for the event:
yield means the agent should stop for the caller. hold means the agent should
keep speaking through a backchannel/noise/acknowledgement. Hotato then
measures whether the timing matched that label.

By default the fixture is CLIPPED around the event (``--pre`` seconds before
the onset, ``--post`` after) and ``caller_onset_sec`` is re-based to the clip,
so the fixture stays small and the measured timings match the uncut original.
``--no-clip`` keeps the full recording and the original onset.

Every write is validated immediately by scoring the created fixture through
the same suite runner. An input that cannot be scored (for example the agent
was not talking at the onset on a should-yield label) is refused with the
honest reason and exit code 2, never written as a fixture that would report a
meaningless verdict. Everything runs offline; no audio leaves the machine.
"""

from __future__ import annotations

import json
import os
import re
import tempfile
from typing import Optional

from ._engine.audio import write_wav
from .core import _read_wav, _require_channel, run_suite

__all__ = ["create_fixture", "CREATED_BY"]

CREATED_BY = "hotato fixture create"

# Same slug rule as the corpus label schema (corpus/label.schema.json).
_SLUG_RE = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")

_WHY_YIELD = ("Created from a real call moment. The label says the agent "
              "should yield.")
_WHY_HOLD = ("Created from a real call moment. The label says the agent "
             "should hold.")


def _load_channels(*, stereo, caller, agent, caller_channel, agent_channel):
    """Resolve the input form into (caller_samples, agent_samples,
    sample_rate, source_name). Malformed input raises ValueError (exit 2)."""
    if stereo and (caller or agent):
        raise ValueError(
            "provide ONE input form: --stereo FILE, or both --caller FILE "
            "and --agent FILE, not both forms at once"
        )
    if stereo:
        signal = _read_wav(stereo)
        if signal.num_channels < 2:
            raise ValueError(
                "--stereo file has one channel; a single mixed mono call is "
                "not enough to attribute talk-over reliably. Pass --caller "
                "and --agent as two mono files, or export a real two-channel "
                "recording."
            )
        _require_channel(signal, caller_channel, "caller")
        _require_channel(signal, agent_channel, "agent")
        return (signal.get(caller_channel), signal.get(agent_channel),
                signal.sample_rate, os.path.basename(stereo))
    if caller and agent:
        c = _read_wav(caller)
        a = _read_wav(agent)
        if c.sample_rate != a.sample_rate:
            raise ValueError(
                f"sample-rate mismatch (caller {c.sample_rate} Hz, agent "
                f"{a.sample_rate} Hz); resample so both match."
            )
        n = min(c.num_samples, a.num_samples)
        return (c.get(0)[:n], a.get(0)[:n], c.sample_rate,
                f"{os.path.basename(caller)}+{os.path.basename(agent)}")
    raise ValueError(
        "provide --stereo FILE, or both --caller FILE and --agent FILE"
    )


def _parse_tags(tags: Optional[str]) -> list:
    if not tags:
        return []
    return [t.strip() for t in tags.split(",") if t.strip()]


def _validate_created(scenario: dict, audio_dir: str,
                      stack: Optional[str]) -> dict:
    """Score the created fixture through the SAME suite runner `hotato run
    --scenarios --audio` uses, isolated to just this scenario, and return the
    envelope. This is the round-trip guarantee: if this envelope exists, the
    fixture is runnable as written."""
    with tempfile.TemporaryDirectory(prefix="hotato-fixture-") as tmp:
        with open(os.path.join(tmp, scenario["id"] + ".json"), "w",
                  encoding="utf-8") as fh:
            json.dump(scenario, fh)
        return run_suite(scenarios_dir=tmp, audio_dir=audio_dir, stack=stack)


def create_fixture(
    *,
    stereo: Optional[str] = None,
    caller: Optional[str] = None,
    agent: Optional[str] = None,
    fixture_id: str,
    title: Optional[str] = None,
    onset_sec: float,
    expect: str = "yield",
    out_dir: str,
    stack: Optional[str] = None,
    max_talk_over_sec: Optional[float] = None,
    max_time_to_yield_sec: Optional[float] = None,
    tags: Optional[str] = None,
    category: Optional[str] = None,
    pre_sec: float = 2.0,
    post_sec: float = 6.0,
    no_clip: bool = False,
    force: bool = False,
    caller_channel: int = 0,
    agent_channel: int = 1,
) -> dict:
    """Create one regression fixture and validate it. Returns a result dict:
    ``{"id", "paths", "scenario", "validation", "onset", "next"}``.

    Raises ValueError (CLI exit 2) for a malformed input, an unusable label,
    an existing fixture without ``force``, or a created fixture that is not
    scorable (the honest reason is in the message; partial outputs are
    removed unless ``force``).
    """
    if not _SLUG_RE.match(fixture_id or ""):
        raise ValueError(
            f"--id {fixture_id!r} is not a valid fixture id; use a lowercase "
            "slug like refund-interruption-001 (letters, digits, hyphens)"
        )
    want_yield = str(expect).strip().lower() not in ("hold", "no", "false",
                                                     "hold-floor")
    resolved_category = category or ("should_yield" if want_yield
                                     else "should_not_yield")
    if resolved_category not in ("should_yield", "should_not_yield"):
        raise ValueError(
            f"--category {resolved_category!r} is not a scenario category; "
            "use should_yield or should_not_yield"
        )
    if want_yield != (resolved_category == "should_yield"):
        raise ValueError(
            f"--category {resolved_category!r} contradicts --expect "
            f"{'yield' if want_yield else 'hold'}; drop one of the two"
        )
    if not want_yield and (max_talk_over_sec is not None
                           or max_time_to_yield_sec is not None):
        raise ValueError(
            "--max-talk-over and --max-time-to-yield bound a yield; they do "
            "not apply to --expect hold (a hold fixture fails exactly when "
            "the agent yields)"
        )
    if onset_sec is None or onset_sec < 0:
        raise ValueError(
            f"--onset must be >= 0 seconds (time from the start of the "
            f"recording); got {onset_sec}."
        )
    if pre_sec < 0 or post_sec <= 0:
        raise ValueError(
            f"--pre must be >= 0 and --post must be > 0 seconds; got "
            f"pre={pre_sec}, post={post_sec}."
        )
    if not out_dir:
        raise ValueError("--out DIR is required (e.g. --out tests/hotato)")

    caller_samples, agent_samples, sample_rate, source = _load_channels(
        stereo=stereo, caller=caller, agent=agent,
        caller_channel=caller_channel, agent_channel=agent_channel,
    )
    n = min(len(caller_samples), len(agent_samples))
    duration = n / sample_rate
    if onset_sec >= duration:
        raise ValueError(
            f"--onset {onset_sec}s is beyond the end of the recording "
            f"({duration:.2f}s)."
        )

    # Clip bounds in samples. Default: [onset - pre, onset + post], clamped
    # to the recording; an onset near the start clips from 0 and the fixture
    # onset is re-based accordingly.
    if no_clip:
        start_idx, end_idx = 0, n
    else:
        start_idx = max(0, int(round((onset_sec - pre_sec) * sample_rate)))
        end_idx = min(n, int(round((onset_sec + post_sec) * sample_rate)))
    fixture_onset = round(onset_sec - start_idx / sample_rate, 3)
    clip_start_sec = round(start_idx / sample_rate, 3)
    clip_end_sec = round(end_idx / sample_rate, 3)

    expected = {
        "yield": want_yield,
        "max_time_to_yield_sec": max_time_to_yield_sec if want_yield else None,
        "max_talk_over_sec": max_talk_over_sec if want_yield else None,
    }
    scenario = {
        "id": fixture_id,
        "title": title or fixture_id.replace("-", " "),
        "category": resolved_category,
        "tags": _parse_tags(tags),
        "sample_rate": sample_rate,
        "duration_sec": round((end_idx - start_idx) / sample_rate, 3),
        "caller_onset_sec": fixture_onset,
        "expected": expected,
        "why_it_matters": _WHY_YIELD if want_yield else _WHY_HOLD,
        "related_signals": (["did_yield", "time_to_yield", "talk_over"]
                            if want_yield else ["did_yield"]),
        "provenance": {
            "source": source,
            "source_onset_sec": round(onset_sec, 3),
            "clip_start_sec": clip_start_sec,
            "clip_end_sec": clip_end_sec,
            "created_by": CREATED_BY,
        },
    }

    scenarios_dir = os.path.join(out_dir, "scenarios")
    audio_dir = os.path.join(out_dir, "audio")
    scenario_path = os.path.join(scenarios_dir, fixture_id + ".json")
    audio_path = os.path.join(audio_dir, fixture_id + ".example.wav")
    existing = [p for p in (scenario_path, audio_path) if os.path.exists(p)]
    if existing and not force:
        raise ValueError(
            f"fixture {fixture_id!r} already exists ({', '.join(existing)}); "
            "pass --force to overwrite it, or pick a new --id"
        )
    os.makedirs(scenarios_dir, exist_ok=True)
    os.makedirs(audio_dir, exist_ok=True)

    # The audio is ALWAYS one two-channel WAV (caller on channel 0, agent on
    # channel 1), also when the input arrived as two mono files.
    write_wav(audio_path, sample_rate,
              [caller_samples[start_idx:end_idx],
               agent_samples[start_idx:end_idx]])
    with open(scenario_path, "w", encoding="utf-8") as fh:
        json.dump(scenario, fh, indent=2)
        fh.write("\n")

    validation = _validate_created(scenario, audio_dir, stack)
    event = next(e for e in validation["events"]
                 if e["event_id"] == fixture_id)
    if event.get("scorable") is False:
        reason = event.get("not_scorable_reason") or "not scorable"
        if not force:
            for p in (scenario_path, audio_path):
                try:
                    os.remove(p)
                except OSError:
                    pass
            kept = "the partial outputs were removed"
        else:
            kept = "--force kept the files on disk for inspection"
        raise ValueError(
            f"the created fixture is not scorable, so it was refused "
            f"({kept}). Reason: {reason}"
        )

    return {
        "id": fixture_id,
        "paths": {"scenario": scenario_path, "audio": audio_path},
        "scenario": scenario,
        "validation": validation,
        "onset": {"source_sec": round(onset_sec, 3),
                  "fixture_sec": fixture_onset},
        "next": (f"hotato run --scenarios {scenarios_dir} "
                 f"--audio {audio_dir} --format text"),
    }


def render_text(result: dict) -> str:
    onset = result["onset"]
    lines = [
        f"created Hotato fixture: {result['id']}",
        f"  scenario: {result['paths']['scenario']}",
        f"  audio:    {result['paths']['audio']}",
        f"  onset:    {onset['source_sec']:.2f}s source -> "
        f"{onset['fixture_sec']:.2f}s fixture",
        f"  expect:   "
        f"{'yield' if result['scenario']['expected']['yield'] else 'hold'}",
        "  check:    scorable",
        "next:",
        f"  {result['next']}",
    ]
    return "\n".join(lines)


def result_json(result: dict) -> dict:
    """The machine shape printed by ``--format json``."""
    return {
        "tool": "hotato",
        "kind": "fixture",
        "schema_version": "1",
        "id": result["id"],
        "paths": result["paths"],
        "onset": result["onset"],
        "scenario": result["scenario"],
        "validation": result["validation"],
        "next": result["next"],
    }
