"""Stack benchmark harness: identical scenarios, YOUR stack, comparable results.

``hotato benchmark`` scores a directory of the USER'S captured recordings (one
dual-channel recording per scenario, named by scenario id) against a named
scenario set: the bundled battery by default, or any scenarios dir such as
``corpus/suites/gold/scenarios``. Because every stack answers the same
scenarios under the same labels and thresholds, the result files are
comparable across stacks. ``hotato benchmark compare`` renders that
side-by-side view.

Honesty is the design constraint, stated here and enforced below:

- The harness measures; the USER brings the stack. It ships no vendor numbers,
  no leaderboard, and no ranking. Every number in a result file is a timing
  measurement of a recording the user provided.
- Scoring is ``core.run_suite`` unchanged (the same vendored engine and the
  same envelope events); nothing is re-implemented or re-tuned here.
- A scenario with no matching recording is listed plainly under
  ``not_captured``. It is never scored, never counted as a failure, and never
  filled in.
- The result timestamp derives from the input files' mtimes, not wall clock,
  so the same inputs produce the same result JSON.
"""

from __future__ import annotations

from .errors import open_regular as _open_regular

import getpass
import json
import os
import socket
import statistics
import tempfile
from datetime import datetime, timezone
from typing import List, Optional, Sequence, Tuple

from . import __version__
from ._engine.score import ScoreConfig
from .core import (
    SUITE_ID,
    _config_block,
    _load_bundled_scenarios,
    _safe_scenario_id,
    run_suite,
)

__all__ = [
    "BENCH_STACKS",
    "KIND",
    "COMPARE_KIND",
    "run_stackbench",
    "load_result",
    "compare_results",
    "render_comparison_md",
]

KIND = "stack-benchmark"
COMPARE_KIND = "stack-benchmark-comparison"

# The stacks the benchmark labels results with. "generic" covers everything the
# capture adapters do not name; the label tunes provenance and fix-knob naming
# only, it never changes a measurement.
BENCH_STACKS = ("vapi", "twilio", "livekit", "pipecat", "generic")

# Recording filename suffixes tried, in order, when the user does not pass one.
SUFFIX_CANDIDATES = (".wav", ".stereo.wav", ".example.wav")


# --------------------------------------------------------------------------
# Scenario loading (mirrors run_suite's dir mechanics; ids drive file names).
# --------------------------------------------------------------------------

def _load_scenarios(scenarios_dir: Optional[str]) -> list:
    if scenarios_dir is None:
        return _load_bundled_scenarios()
    if not os.path.isdir(scenarios_dir):
        raise ValueError(
            f"--scenarios {scenarios_dir!r} is not a directory of scenario "
            "JSON labels (e.g. corpus/suites/gold/scenarios)"
        )
    scenarios = []
    for name in sorted(os.listdir(scenarios_dir)):
        if name.endswith(".json") and name != "manifest.json":
            with _open_regular(os.path.join(scenarios_dir, name), "r", encoding="utf-8") as fh:
                scenarios.append(json.load(fh))
    if not scenarios:
        raise ValueError(f"no scenario JSONs found in {scenarios_dir!r}")
    return scenarios


def _detect_suffix(recordings_dir: str, ids: List[str], explicit: Optional[str]) -> str:
    """Pick the recording filename suffix: the explicit one, else the candidate
    matching the most scenario ids on disk (ties go to the earlier candidate)."""
    if explicit:
        return explicit
    best, best_count = SUFFIX_CANDIDATES[0], 0
    for cand in SUFFIX_CANDIDATES:
        count = sum(
            1 for sid in ids
            if os.path.exists(os.path.join(recordings_dir, sid + cand))
        )
        if count > best_count:
            best, best_count = cand, count
    return best


def _iso_utc(ts: float) -> str:
    return datetime.fromtimestamp(ts, tz=timezone.utc).isoformat(timespec="seconds")


def _current_user() -> str:
    try:
        return getpass.getuser()
    except Exception:  # pragma: no cover - no login database in some sandboxes
        return os.environ.get("USER", "unknown")


# --------------------------------------------------------------------------
# The benchmark run.
# --------------------------------------------------------------------------

def run_stackbench(
    *,
    stack: str,
    recordings_dir: str,
    scenarios_dir: Optional[str] = None,
    suffix: Optional[str] = None,
    caller_channel: int = 0,
    agent_channel: int = 1,
    cfg: Optional[ScoreConfig] = None,
) -> dict:
    """Score the user's captured recordings against a named scenario set.

    Reuses ``core.run_suite`` for ALL scoring: the captured subset of scenarios
    is handed to it as a scenarios dir with ``recordings_dir`` as the audio
    dir, so every verdict, signal, and fix is byte-identical to what ``hotato
    run`` would report on the same input. Scenarios with no matching recording
    are returned under ``scenarios.not_captured`` and are never scored.
    """
    stack_n = (stack or "").strip().lower()
    if stack_n not in BENCH_STACKS:
        raise ValueError(
            f"unknown stack {stack!r}; choose one of {', '.join(BENCH_STACKS)}"
        )
    if not recordings_dir or not os.path.isdir(recordings_dir):
        raise ValueError(
            f"--recordings {recordings_dir!r} is not a directory of captured "
            "recordings (one dual-channel WAV per scenario, named "
            "<scenario-id>.wav)"
        )
    if cfg is None:
        cfg = ScoreConfig()

    scenarios = _load_scenarios(scenarios_dir)
    ids = []
    for sc in scenarios:
        sid = sc.get("id")
        if not sid:
            raise ValueError(
                "every scenario needs a plain-filename 'id' so recordings can "
                "be matched to it"
            )
        # The id becomes a filesystem path here AND inside run_suite, so enforce
        # the same safe-slug rule (no path separator, not absolute, no '..')
        # rather than only screening for '/'.
        ids.append(_safe_scenario_id(sid))

    use_suffix = _detect_suffix(recordings_dir, ids, suffix)
    captured, not_captured = [], []
    for sc in scenarios:
        wav = os.path.join(recordings_dir, sc["id"] + use_suffix)
        (captured if os.path.exists(wav) else not_captured).append(sc)
    if not captured:
        expect = ", ".join(sid + use_suffix for sid in ids[:3])
        raise ValueError(
            f"no recordings in {recordings_dir!r} match the scenario ids "
            f"(expected files like {expect}). Name each captured recording "
            f"<scenario-id>{use_suffix}, or pass --suffix. See "
            "docs/BENCHMARK-STACKS.md."
        )

    # Score ONLY the captured subset, via run_suite unchanged. Writing the
    # subset to a temp scenarios dir keeps "not captured" out of scoring
    # entirely, so a missing file can never surface as a failed event.
    with tempfile.TemporaryDirectory(prefix="hotato-stackbench-") as tmp:
        for sc in captured:
            with open(os.path.join(tmp, sc["id"] + ".json"), "w",
                      encoding="utf-8") as fh:
                json.dump(sc, fh)
        env = run_suite(
            suite=SUITE_ID,
            stack=stack_n,
            scenarios_dir=tmp,
            audio_dir=recordings_dir,
            suffix=use_suffix,
            caller_channel=caller_channel,
            agent_channel=agent_channel,
            cfg=cfg,
        )

    recordings_meta = []
    latest_mtime = None
    for sc in captured:
        fname = sc["id"] + use_suffix
        st = os.stat(os.path.join(recordings_dir, fname))
        latest_mtime = (st.st_mtime if latest_mtime is None
                        else max(latest_mtime, st.st_mtime))
        recordings_meta.append({
            "scenario_id": sc["id"],
            "file": fname,
            "bytes": st.st_size,
            "mtime_utc": _iso_utc(st.st_mtime),
        })

    return {
        "tool": "hotato",
        "kind": KIND,
        "schema_version": "1",
        "stack": env["stack"],
        "suite": (SUITE_ID if scenarios_dir is None
                  else os.path.abspath(scenarios_dir)),
        "offline": True,
        "engine": env["engine"],
        "limits": env["limits"],
        "config": _config_block(cfg),
        "summary": env["summary"],
        "scenarios": {
            "total": len(scenarios),
            "captured": len(captured),
            # Plainly listed; never scored, never counted as failures.
            "not_captured": [sc["id"] for sc in not_captured],
        },
        "events": env["events"],
        "fix_map": env["fix_map"],
        "funnel": env["funnel"],
        "provenance": {
            "ran_by": _current_user(),
            "hostname": socket.gethostname(),
            "hotato_version": __version__,
            "recordings_dir": os.path.abspath(recordings_dir),
            "suffix": use_suffix,
            "scenario_source": ("bundled" if scenarios_dir is None
                                else os.path.abspath(scenarios_dir)),
            "recordings": recordings_meta,
        },
        # Deterministic: derived from the newest input recording, never wall
        # clock, so the same inputs reproduce the same result file.
        "generated_at_utc": _iso_utc(latest_mtime),
        "timestamp_source": "latest input recording mtime, not wall clock",
    }


# --------------------------------------------------------------------------
# Comparison of two or more result files.
# --------------------------------------------------------------------------

_MEASUREMENT_NOTE = (
    "Every number is a measurement of the recordings each run was given. "
    "Nothing here ranks a vendor: results depend on each stack's "
    "configuration and each user's captures."
)


def load_result(path: str) -> dict:
    """Load one benchmark result JSON. Anything that is not a stack-benchmark
    result is a clean usage error (exit 2), never a silent zero-row compare."""
    with _open_regular(path, "r", encoding="utf-8") as fh:
        data = json.load(fh)
    if not (isinstance(data, dict) and data.get("tool") == "hotato"
            and data.get("kind") == KIND
            and isinstance(data.get("events"), list)):
        raise ValueError(
            f"{path!r} is not a hotato stack-benchmark result JSON. Save one "
            "with: hotato benchmark --stack STACK --recordings DIR "
            "--out result.json"
        )
    return data


def _delta(value: Optional[float], base: Optional[float]) -> Optional[float]:
    if value is None or base is None:
        return None
    return round(float(value) - float(base), 3)


def _median(values: List[float]) -> Optional[float]:
    return round(statistics.median(values), 3) if values else None


_LABELS = "ABCDEFGHIJKLMNOPQRSTUVWXYZ"


def compare_results(inputs: Sequence[Tuple[str, dict]]) -> dict:
    """Compare two or more benchmark results, scenario by scenario.

    Only the intersection of scenarios scored in EVERY input is compared;
    everything else is listed under ``skipped`` with which files miss it.
    Deltas are signed differences against the FIRST input. Measurements only:
    no ranking, no winner.
    """
    if len(inputs) < 2:
        raise ValueError(
            "compare needs at least two stack-benchmark result files"
        )

    metas, per_input = [], []
    for i, (path, data) in enumerate(inputs):
        label = _LABELS[i] if i < len(_LABELS) else f"R{i + 1}"
        events = {}
        for e in data.get("events", []):
            sid = e.get("scenario_id") or e.get("event_id")
            v = e.get("verdict") or {}
            events[sid] = {
                "did_yield": v.get("did_yield"),
                "seconds_to_yield": v.get("seconds_to_yield"),
                "talk_over_sec": v.get("talk_over_sec"),
                "passed": v.get("passed"),
            }
        metas.append({
            "label": label,
            "file": os.path.basename(path),
            "path": path,
            "stack": data.get("stack", "generic"),
            "suite": data.get("suite"),
            "captured": len(events),
            "scenarios_total": (data.get("scenarios") or {}).get("total"),
        })
        per_input.append(events)

    # Shared scenarios, in the first input's event order; then the leftovers.
    first_order = [
        e.get("scenario_id") or e.get("event_id")
        for e in inputs[0][1].get("events", [])
    ]
    shared = [sid for sid in first_order
              if all(sid in ev for ev in per_input)]
    union, seen = [], set()
    for ev_order in ([first_order]
                     + [[e.get("scenario_id") or e.get("event_id")
                         for e in data.get("events", [])]
                        for _, data in inputs[1:]]):
        for sid in ev_order:
            if sid not in seen:
                seen.add(sid)
                union.append(sid)
    skipped = [
        {
            "scenario_id": sid,
            "missing_from": [metas[i]["file"] for i, ev in enumerate(per_input)
                             if sid not in ev],
        }
        for sid in union if sid not in shared
    ]

    per_scenario = []
    for sid in shared:
        base = per_input[0][sid]
        measurements = []
        for i, ev in enumerate(per_input):
            m = ev[sid]
            entry = {
                "input": metas[i]["label"],
                "did_yield": m["did_yield"],
                "talk_over_sec": m["talk_over_sec"],
                "seconds_to_yield": m["seconds_to_yield"],
            }
            if i > 0:
                entry["delta_vs_first"] = {
                    "talk_over_sec": _delta(m["talk_over_sec"],
                                            base["talk_over_sec"]),
                    "seconds_to_yield": _delta(m["seconds_to_yield"],
                                               base["seconds_to_yield"]),
                }
            measurements.append(entry)
        per_scenario.append({"scenario_id": sid, "measurements": measurements})

    medians, base_med = [], None
    for i, ev in enumerate(per_input):
        talk = [ev[sid]["talk_over_sec"] for sid in shared
                if ev[sid]["talk_over_sec"] is not None]
        tty = [ev[sid]["seconds_to_yield"] for sid in shared
               if ev[sid]["seconds_to_yield"] is not None]
        m = {
            "input": metas[i]["label"],
            "compared": len(shared),
            "yielded": sum(1 for sid in shared if ev[sid]["did_yield"]),
            "talk_over_median_sec": _median(talk),
            "talk_over_n": len(talk),
            "seconds_to_yield_median_sec": _median(tty),
            "seconds_to_yield_n": len(tty),
        }
        if i == 0:
            base_med = m
        else:
            m["delta_vs_first"] = {
                "talk_over_median_sec": _delta(
                    m["talk_over_median_sec"],
                    base_med["talk_over_median_sec"]),
                "seconds_to_yield_median_sec": _delta(
                    m["seconds_to_yield_median_sec"],
                    base_med["seconds_to_yield_median_sec"]),
            }
        medians.append(m)

    return {
        "tool": "hotato",
        "kind": COMPARE_KIND,
        "schema_version": "1",
        "note": _MEASUREMENT_NOTE,
        "inputs": metas,
        "compared": shared,
        "skipped": skipped,
        "suites_differ": len({m["suite"] for m in metas}) > 1,
        "per_scenario": per_scenario,
        "medians": medians,
    }


# --------------------------------------------------------------------------
# Markdown rendering.
# --------------------------------------------------------------------------

def _fmt_sec(v: Optional[float]) -> str:
    return "-" if v is None else f"{v:.3f}"


def _fmt_delta(v: Optional[float]) -> str:
    return "-" if v is None else f"{v:+.3f}"


def render_comparison_md(cmp_env: dict) -> str:
    metas = cmp_env["inputs"]
    labels = [m["label"] for m in metas]
    shared = cmp_env["compared"]
    lines: List[str] = []
    lines.append("# Hotato stack benchmark comparison")
    lines.append("")
    lines.append(cmp_env["note"])
    lines.append("")

    lines.append("## Inputs")
    lines.append("")
    lines.append("| input | stack | file | scenarios captured |")
    lines.append("|---|---|---|---:|")
    for m in metas:
        lines.append(
            f"| {m['label']} | {m['stack']} | `{m['file']}` | {m['captured']} |"
        )
    lines.append("")
    lines.append(
        f"Compared: **{len(shared)}** scenario(s) captured in every input."
    )
    if cmp_env["skipped"]:
        parts = [
            f"`{s['scenario_id']}` (missing from {', '.join(s['missing_from'])})"
            for s in cmp_env["skipped"]
        ]
        lines.append("")
        lines.append("Skipped, not captured in every input: " + "; ".join(parts) + ".")
    if cmp_env["suites_differ"]:
        lines.append("")
        lines.append(
            "The inputs used different scenario sources; only shared scenario "
            "ids are compared."
        )
    lines.append("")
    if not shared:
        lines.append("No scenarios in common; nothing to compare side by side.")
        lines.append("")
        return "\n".join(lines)

    # Look-up: scenario_id -> label -> measurement entry.
    by_sid = {
        row["scenario_id"]: {e["input"]: e for e in row["measurements"]}
        for row in cmp_env["per_scenario"]
    }
    delta_labels = labels[1:]

    def _delta_cols() -> str:
        return "".join(f" delta {lb}-{labels[0]} |" for lb in delta_labels)

    lines.append("## Yielded")
    lines.append("")
    lines.append("| scenario |" + "".join(f" {lb} |" for lb in labels))
    lines.append("|---|" + "---|" * len(labels))
    for sid in shared:
        cells = "".join(
            f" {'yes' if by_sid[sid][lb]['did_yield'] else 'no'} |"
            for lb in labels
        )
        lines.append(f"| `{sid}` |{cells}")
    lines.append("")

    for title, key in (("Talk-over (s)", "talk_over_sec"),
                       ("Time to yield (s)", "seconds_to_yield")):
        lines.append(f"## {title}")
        lines.append("")
        lines.append("| scenario |" + "".join(f" {lb} |" for lb in labels)
                      + _delta_cols())
        lines.append("|---|" + "---:|" * (len(labels) + len(delta_labels)))
        for sid in shared:
            cells = "".join(
                f" {_fmt_sec(by_sid[sid][lb][key])} |" for lb in labels
            )
            deltas = "".join(
                f" {_fmt_delta((by_sid[sid][lb].get('delta_vs_first') or {}).get(key))} |"
                for lb in delta_labels
            )
            lines.append(f"| `{sid}` |{cells}{deltas}")
        lines.append("")

    med_by_label = {m["input"]: m for m in cmp_env["medians"]}
    lines.append(f"## Summary medians (over the {len(shared)} compared scenario(s))")
    lines.append("")
    lines.append("| measurement |" + "".join(f" {lb} |" for lb in labels)
                 + _delta_cols())
    lines.append("|---|" + "---:|" * (len(labels) + len(delta_labels)))
    y_cells = "".join(
        f" {med_by_label[lb]['yielded']} of {len(shared)} |" for lb in labels
    )
    lines.append("| yielded |" + y_cells + " - |" * len(delta_labels))
    for name, key, nkey in (
        ("talk-over median (s)", "talk_over_median_sec", "talk_over_n"),
        ("time-to-yield median (s)", "seconds_to_yield_median_sec",
         "seconds_to_yield_n"),
    ):
        cells = "".join(
            f" {_fmt_sec(med_by_label[lb][key])} (n={med_by_label[lb][nkey]}) |"
            for lb in labels
        )
        deltas = "".join(
            f" {_fmt_delta((med_by_label[lb].get('delta_vs_first') or {}).get(key))} |"
            for lb in delta_labels
        )
        lines.append(f"| {name} |{cells}{deltas}")
    lines.append("")
    lines.append(
        "_A `-` means no measurement exists for that cell (for example, an "
        "agent that never yielded has no time to yield); nothing is filled "
        "in. Deltas are signed differences against the first input._"
    )
    lines.append("")
    return "\n".join(lines)
