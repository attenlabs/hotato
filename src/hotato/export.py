"""Research exports: the same measurements as flat CSVs plus the JSON envelope.

``hotato export`` scores a recording (or the bundled battery) exactly like
``hotato run`` and writes three files into a directory:

* ``events.csv``  one row per scored event: every measured signal + verdict
* ``frames.csv``  one row per VAD frame (the same frame dump behind
  ``--dump-frames`` and the report's frame inspector)
* ``envelope.json``  the standard machine envelope, unchanged

Stdlib ``csv`` only. Column meanings are documented in ``#`` comment lines at
the top of each CSV. Every value is a real measurement; empty cell = the
measurement was not derivable (never fabricated).

The manifest returned by ``run_export`` (and the JSON keys the CLI can print)
additionally carries ``latency_summary``: mean/median/p90/p95 of talk-over,
time-to-yield, and response-gap (dead air before the agent speaks), pooled
across the exported events -- the same pooled definitions ``hotato team``
uses, computed here for a single export instead of across many run files.
``latency_sla`` gates the pooled p95 response-gap against an optional
``--max-response-gap`` bound, matching ``--max-talk-over`` /
``--max-time-to-yield``. Neither key is written into ``envelope.json``, which
stays byte-identical to a plain ``hotato run`` / ``hotato run --suite``.
"""

from __future__ import annotations

import csv
import json
import os
from typing import Optional

from ._engine.score import ScoreConfig
from ._stats import dist_summary, latency_sla
from .core import dump_frames_for_input, run_single, run_suite
from .report import _frames_for_suite_event

__all__ = ["run_export", "EVENT_COLUMNS", "FRAME_COLUMNS"]

EVENT_COLUMNS = [
    "event_id", "scenario_id", "title", "category", "expected_yield",
    "passed", "did_yield", "seconds_to_yield", "talk_over_sec",
    "caller_onset_sec", "agent_talking_at_onset",
    "response_gap_sec", "premature_start_sec",
    "fix_class", "fix_title", "reasons",
]

FRAME_COLUMNS = [
    "event_id", "t_sec", "caller_dbfs", "agent_dbfs",
    "caller_active", "agent_active",
    "caller_threshold_db", "caller_noise_floor_db",
    "agent_threshold_db", "agent_noise_floor_db",
]

_EVENTS_HEADER_DOC = """\
# hotato events export. One row per scored event; every value is a real
# measurement from the scorer (empty cell = not derivable, never fabricated).
# Columns:
#   event_id              stable id of the event (file basename or scenario id)
#   scenario_id           battery scenario id (empty for a single recording)
#   title                 human label of the event
#   category              scenario category (e.g. should_yield)
#   expected_yield        true = the agent was expected to stop for the caller
#   passed                verdict against the expectation
#   did_yield             did the agent stop for the caller
#   seconds_to_yield      caller onset to agent quiet, seconds
#   talk_over_sec         overlap seconds after onset before the yield
#   caller_onset_sec      caller onset, seconds from recording start
#   agent_talking_at_onset  was the agent speaking when the caller came in
#   response_gap_sec      caller turn end to agent next onset, seconds
#   premature_start_sec   seconds the agent led the caller's turn end
#   fix_class             config | engagement-control (failures only)
#   fix_title             short fix label (failures only)
#   reasons               failure reasons, '; ' separated
"""

_FRAMES_HEADER_DOC = """\
# hotato frames export. One row per VAD frame (the evidence behind every
# reported number; re-derivable by hand with the thresholds below).
# Columns:
#   event_id                event this frame belongs to
#   t_sec                   frame start time, seconds
#   caller_dbfs             caller channel energy, dBFS
#   agent_dbfs              agent channel energy, dBFS
#   caller_active           energy VAD marked the caller frame active
#   agent_active            energy VAD marked the agent frame active
#   caller_threshold_db     caller activity threshold used (constant per event)
#   caller_noise_floor_db   caller noise floor estimate (constant per event)
#   agent_threshold_db      agent activity threshold used (constant per event)
#   agent_noise_floor_db    agent noise floor estimate (constant per event)
"""


def _cell(v):
    if v is None:
        return ""
    if isinstance(v, bool):
        return "true" if v else "false"
    return v


def _event_row(e: dict) -> list:
    v = e.get("verdict", {})
    m = e.get("measurements", {})
    sig = e.get("signals", {}) or {}
    latency = sig.get("latency", {}) if isinstance(sig, dict) else {}
    fx = e.get("fix") or {}
    return [_cell(x) for x in [
        e.get("event_id"), e.get("scenario_id"), e.get("title"),
        e.get("category"), e.get("expected_yield"),
        v.get("passed"), v.get("did_yield"), v.get("seconds_to_yield"),
        v.get("talk_over_sec"), m.get("caller_onset_sec"),
        m.get("agent_talking_at_onset"),
        latency.get("response_gap_sec"), latency.get("premature_start_sec"),
        fx.get("fix_class"), fx.get("title"),
        "; ".join(v.get("reasons") or []),
    ]]


def _pooled_latency_summary(events: list) -> dict:
    """Pool talk-over, time-to-yield, and response-gap across ``events`` into
    the same mean/median/p90/p95 shape ``hotato team`` reports, plus the
    n-stated definitions in ``hotato._stats``. Never written into
    envelope.json; only into the manifest ``run_export`` returns."""
    tov, tty, rg = [], [], []
    for e in events:
        v = e.get("verdict", {})
        if v.get("talk_over_sec") is not None:
            tov.append(v["talk_over_sec"])
        if v.get("seconds_to_yield") is not None:
            tty.append(v["seconds_to_yield"])
        sig = e.get("signals") or {}
        lat = sig.get("latency") or {}
        if lat.get("response_gap_sec") is not None:
            rg.append(lat["response_gap_sec"])
    return {
        "talk_over_sec": dist_summary(tov),
        "seconds_to_yield": dist_summary(tty),
        "response_gap_sec": dist_summary(rg),
    }


def _frame_row(event_id: str, f: dict) -> list:
    return [_cell(x) for x in [
        event_id, f.get("t_sec"), f.get("caller_dbfs"), f.get("agent_dbfs"),
        f.get("caller_active"), f.get("agent_active"),
        f.get("caller_threshold_db"), f.get("caller_noise_floor_db"),
        f.get("agent_threshold_db"), f.get("agent_noise_floor_db"),
    ]]


def run_export(
    *,
    out_dir: str,
    stereo: Optional[str] = None,
    caller: Optional[str] = None,
    agent: Optional[str] = None,
    caller_channel: int = 0,
    agent_channel: int = 1,
    onset_sec: Optional[float] = None,
    expect: str = "yield",
    stack: Optional[str] = None,
    suite: Optional[str] = None,
    scenarios_dir: Optional[str] = None,
    audio_dir: Optional[str] = None,
    suffix: str = ".example.wav",
    max_talk_over_sec: Optional[float] = None,
    max_time_to_yield_sec: Optional[float] = None,
    max_response_gap_sec: Optional[float] = None,
    cfg: Optional[ScoreConfig] = None,
) -> dict:
    """Score the input, write events.csv + frames.csv + envelope.json into
    ``out_dir`` (created if missing) and return a small manifest dict:
    ``{"env", "events_rows", "frames_rows", "paths", "latency_summary",
    "latency_sla"}``. ``max_response_gap_sec`` optionally bounds the pooled
    p95 response-gap (the latency SLA gate); left ``None`` the gate is not
    configured and never fails."""
    cfg = cfg or ScoreConfig()

    # score exactly like `hotato run`, then resolve the per-event frames
    per_event_frames = []  # [(event_id, frames)]
    if suite:
        env = run_suite(
            suite=suite, stack=stack, scenarios_dir=scenarios_dir,
            audio_dir=audio_dir, suffix=suffix,
            caller_channel=caller_channel, agent_channel=agent_channel, cfg=cfg,
        )
        for e in env["events"]:
            frames, _hop = _frames_for_suite_event(
                e, audio_dir, suffix, caller_channel, agent_channel, cfg
            )
            per_event_frames.append((e["event_id"], frames))
    else:
        env = run_single(
            stereo=stereo, caller=caller, agent=agent,
            caller_channel=caller_channel, agent_channel=agent_channel,
            onset_sec=onset_sec, expect=expect, stack=stack,
            max_talk_over_sec=max_talk_over_sec,
            max_time_to_yield_sec=max_time_to_yield_sec, cfg=cfg,
        )
        dump = dump_frames_for_input(
            stereo=stereo, caller=caller, agent=agent,
            caller_channel=caller_channel, agent_channel=agent_channel,
            onset_sec=None, cfg=cfg,
        )
        per_event_frames.append((env["events"][0]["event_id"], dump["frames"]))

    os.makedirs(out_dir, exist_ok=True)
    events_path = os.path.join(out_dir, "events.csv")
    frames_path = os.path.join(out_dir, "frames.csv")
    envelope_path = os.path.join(out_dir, "envelope.json")

    with open(events_path, "w", encoding="utf-8", newline="") as fh:
        fh.write(_EVENTS_HEADER_DOC)
        w = csv.writer(fh)
        w.writerow(EVENT_COLUMNS)
        for e in env["events"]:
            w.writerow(_event_row(e))

    frames_rows = 0
    with open(frames_path, "w", encoding="utf-8", newline="") as fh:
        fh.write(_FRAMES_HEADER_DOC)
        w = csv.writer(fh)
        w.writerow(FRAME_COLUMNS)
        for event_id, frames in per_event_frames:
            for f in frames:
                w.writerow(_frame_row(event_id, f))
                frames_rows += 1

    with open(envelope_path, "w", encoding="utf-8") as fh:
        json.dump(env, fh, indent=2)

    latency_summary = _pooled_latency_summary(env["events"])
    sla = latency_sla(latency_summary["response_gap_sec"], max_response_gap_sec)

    return {
        "env": env,
        "events_rows": len(env["events"]),
        "frames_rows": frames_rows,
        "paths": {
            "events": events_path,
            "frames": frames_path,
            "envelope": envelope_path,
        },
        "latency_summary": latency_summary,
        "latency_sla": sla,
    }
