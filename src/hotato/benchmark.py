"""Reproducible measurement-error harness for the turn-taking scorer.

Run it:

    PYTHONPATH=src python3 -m hotato.benchmark

What it does, and only what it does: it takes a set of
``(dual-channel recording, ground-truth label)`` pairs and reports, per signal,
the **measurement error in milliseconds** between what the scorer measured and
what the fixture was rendered/labelled to be, plus a ``did_yield`` **confusion
matrix** against the ``should_yield`` / ``should_not_yield`` labels.

It does NOT emit an accuracy percentage. There is no single number here and none
is implied. A scorer can look "95% accurate" while quietly failing the rare,
expensive missed-yield case; collapsing the error distribution and the confusion
matrix into one figure hides exactly the trade-off an operator feels. So the
report is the distribution (median / mean / worst-case, in ms) and the four
confusion cells, and that is deliberately all it is. See ``docs/BENCHMARK.md``.

Ground truth comes from each scenario's ``reference_render`` block (the exact
segment timings the synthetic fixture was rendered from) and its ``expected``
label. The bundled + example fixtures are **synthetic**: a deterministic,
runnable floor and a regression guard, not recorded speech and not a production
validity claim. To measure real validity, point the harness at your own labelled
dual-channel recordings -- the ``--scenarios`` / ``--audio`` flags are exactly
that "bring your own labelled recordings" path (see ``docs/BENCHMARK.md`` and
``corpus/``). Nothing here fabricates a real-model number.

This module is standalone: it is runnable as ``python -m hotato.benchmark`` and
is intentionally NOT wired into the ``hotato`` CLI or the packaged entry points.
It scores with the vendored engine (``hotato._engine``) unchanged.
"""

from __future__ import annotations

import argparse
import json
import os
import statistics
import sys
from dataclasses import dataclass, field
from importlib import resources
from typing import List, Optional, Tuple

from ._engine.score import ScoreConfig, score_stereo
from .core import _read_wav
from .errors import open_regular as _open_regular

__all__ = [
    "rendered_references",
    "measure_fixture",
    "run_benchmark",
    "render_markdown",
    "default_fixture_sets",
    "FixtureSet",
]

SCHEMA_VERSION = "1"

# The three timing signals we hold to a rendered/labelled ground truth. Kept as a
# tuple so the aggregate report, the markdown, and the tests iterate one list.
ERROR_SIGNALS = ("onset_sec", "time_to_yield_sec", "response_gap_sec")

_EPS = 1e-9

# Stated up front, in the artifact, in the docs, and here. This is the whole
# point of an open eval: it produces honest numbers, it does not assert them.
HONESTY = {
    "no_accuracy_percent": (
        "This harness reports per-signal measurement error in milliseconds and a "
        "did_yield confusion matrix. It never aggregates them into an accuracy "
        "percentage; doing so would hide the missed-yield / false-yield trade-off."
    ),
    "energy_not_intent": (
        "The scorer measures speech-level energy over time. It is not speaker ID, "
        "diarization, transcription, or emotion. Energy is not intent."
    ),
    "synthetic_is_a_floor": (
        "The bundled and example fixtures are synthetic: deterministic rendered "
        "audio with exact known timings. They are a reproducible floor and a "
        "regression guard, not recorded speech and not a production validity "
        "claim. For real validity, bring your own labelled dual-channel "
        "recordings (see docs/BENCHMARK.md and corpus/)."
    ),
    "error_is_config_driven": (
        "The reported error is what the DEFAULT shipped config measures, so it is "
        "the number a real user sees. It includes the VAD hangover and the frame "
        "hop, both exposed ScoreConfig parameters; neutralising the hangover "
        "shrinks the error toward the one-hop framing floor."
    ),
}


# --------------------------------------------------------------------------
# Ground truth: derive the rendered/true reference timings from a scenario.
# --------------------------------------------------------------------------

def rendered_references(sc: dict) -> dict:
    """Return the rendered/true reference timings for a scenario, in seconds.

    Only keys that are genuinely derivable from ``reference_render`` + ``expected``
    are present. Nothing is invented: if the ground truth for a signal is not in
    the fixture, that signal is simply absent here and no error is reported for
    it (an honest gap beats a fabricated reference).

    Keys, when derivable:
      onset_sec          first caller-speech onset (start of the first caller
                         segment); absent when the caller channel carries no
                         independent speech (e.g. an echo-of-agent fixture).
      time_to_yield_sec  seconds from caller onset to the moment the agent's
                         in-progress turn ends (the agent goes quiet); only for
                         should-yield fixtures where the agent was talking at
                         onset and actually stops before the clip ends.
      response_gap_sec   rendered endpointing dead-air gap from the caller's turn
                         end to the agent's next onset; only when the fixture
                         records it.
    """
    rr = sc.get("reference_render", {}) or {}
    expected = sc.get("expected", {}) or {}
    caller_segs = rr.get("caller_segments_sec") or []
    agent_segs = rr.get("agent_segments_sec") or []
    duration = sc.get("duration_sec")
    refs: dict = {}

    # --- onset: first real caller speech ----------------------------------
    if caller_segs and not rr.get("caller_is_echo_of_agent"):
        refs["onset_sec"] = float(caller_segs[0][0])

    # --- time_to_yield: the agent's turn end after the caller takes the floor
    should_yield = bool(expected.get("yield", True))
    if should_yield and caller_segs and agent_segs:
        onset = float(caller_segs[0][0])
        yield_end = None
        for seg in agent_segs:
            a_s, a_e = float(seg[0]), float(seg[1])
            # the agent segment that is live when the caller barges in
            if a_s <= onset + _EPS < a_e - _EPS:
                yield_end = a_e
                break
        # Only a real, in-clip yield counts: an agent still talking at the clip
        # boundary has not yielded, so we produce no yield reference for it (the
        # confusion matrix records the miss instead).
        if yield_end is not None and (duration is None or yield_end < float(duration) - _EPS):
            refs["time_to_yield_sec"] = max(0.0, yield_end - onset)

    # --- response_gap: the rendered endpointing gap ------------------------
    if rr.get("rendered_response_gap_sec") is not None:
        refs["response_gap_sec"] = float(rr["rendered_response_gap_sec"])
    else:
        off = rr.get("caller_offset_sec")
        on = rr.get("agent_response_onset_sec")
        if off is not None and on is not None:
            gap = float(on) - float(off)
            if gap >= -_EPS:
                refs["response_gap_sec"] = max(0.0, gap)

    return refs


# --------------------------------------------------------------------------
# Per-fixture measurement.
# --------------------------------------------------------------------------

@dataclass
class FixtureSet:
    name: str
    kind: str  # "synthetic" | "byo" (bring-your-own labelled recordings)
    note: str
    # each item: (scenario_dict, absolute_wav_path)
    fixtures: List[Tuple[dict, str]] = field(default_factory=list)


def _abs_error_ms(measured: Optional[float], reference: Optional[float]) -> Optional[float]:
    if measured is None or reference is None:
        return None
    return round(abs(float(measured) - float(reference)) * 1000.0, 3)


def measure_fixture(sc: dict, wav_path: str, *, cfg: Optional[ScoreConfig] = None) -> dict:
    """Score ONE labelled recording two ways and report its measurement errors.

    - Onset error is measured in DETECT mode (the scorer is given no onset label),
      so it is a real test of the onset detector against the rendered onset.
    - Yield / talk-over / response-gap / did_yield are measured in LABEL mode
      (the scorer is given the human ``caller_onset_sec``), exactly as the shipped
      tool runs the battery, so those numbers are the ones a user actually sees.

    Errors are only reported where a rendered/true reference exists AND the scorer
    produced a value; otherwise the entry is ``None`` and never fabricated.
    """
    if cfg is None:
        cfg = ScoreConfig()

    sid = sc.get("id", os.path.basename(wav_path))
    label_onset = sc.get("caller_onset_sec")
    refs = rendered_references(sc)
    # Hardened wrapper (never the vendored engine's raw wave.Error/struct.error/
    # RuntimeError on a malformed BYO recording -- see docs/BENCHMARK.md).
    signal = _read_wav(wav_path)

    # DETECT mode: measure the onset detector.
    detected = score_stereo(signal, 0, 1, caller_onset_sec=None, cfg=cfg)
    detected_onset = (
        detected.caller_onset_sec if detected.caller_onset_sec is not None and detected.caller_onset_sec >= 0
        else None
    )

    # LABEL mode: the canonical shipped numbers.
    scored = score_stereo(signal, 0, 1, caller_onset_sec=label_onset, cfg=cfg)
    lat = scored.signals.get("latency", {})
    measured = {
        "onset_sec": detected_onset,
        "time_to_yield_sec": scored.time_to_yield_sec,
        "response_gap_sec": lat.get("response_gap_sec"),
    }

    errors_ms = {
        "onset_sec": _abs_error_ms(measured["onset_sec"], refs.get("onset_sec")),
        "time_to_yield_sec": _abs_error_ms(measured["time_to_yield_sec"], refs.get("time_to_yield_sec")),
        "response_gap_sec": _abs_error_ms(measured["response_gap_sec"], refs.get("response_gap_sec")),
    }

    should_yield = bool(sc.get("expected", {}).get("yield", True))
    did_yield = bool(scored.did_yield)

    return {
        "id": sid,
        "title": sc.get("title"),
        "category": sc.get("category"),
        "expected_yield": should_yield,
        "did_yield": did_yield,
        "agent_talking_at_onset": scored.agent_talking_at_onset,
        "hop_sec": scored.hop_sec,
        "confusion_cell": _confusion_cell(should_yield, did_yield),
        "reference_sec": {k: round(v, 6) for k, v in refs.items()},
        "measured_sec": {
            k: (round(v, 6) if v is not None else None) for k, v in measured.items()
        },
        "error_ms": errors_ms,
    }


def _confusion_cell(should_yield: bool, did_yield: bool) -> str:
    if should_yield and did_yield:
        return "correct_yield"
    if should_yield and not did_yield:
        return "missed_yield"
    if not should_yield and did_yield:
        return "false_yield"
    return "correct_hold"


# --------------------------------------------------------------------------
# Aggregate report.
# --------------------------------------------------------------------------

def _error_stats(values_ms: List[float]) -> dict:
    """Distribution of an error signal, in ms. Never a single 'accuracy' figure."""
    vals = [v for v in values_ms if v is not None]
    if not vals:
        return {"n": 0, "median_ms": None, "mean_ms": None, "max_ms": None, "min_ms": None}
    return {
        "n": len(vals),
        "median_ms": round(statistics.median(vals), 3),
        "mean_ms": round(statistics.fmean(vals), 3),
        "max_ms": round(max(vals), 3),
        "min_ms": round(min(vals), 3),
    }


def _empty_confusion() -> dict:
    return {"correct_yield": 0, "missed_yield": 0, "false_yield": 0, "correct_hold": 0}


def run_benchmark(fixture_sets: List[FixtureSet], *, cfg: Optional[ScoreConfig] = None) -> dict:
    """Measure every fixture across every set and assemble the honest report."""
    if cfg is None:
        cfg = ScoreConfig()

    per_set = []
    all_rows: List[dict] = []
    overall_conf = _empty_confusion()

    for fs in fixture_sets:
        rows = [measure_fixture(sc, wav, cfg=cfg) for sc, wav in fs.fixtures]
        conf = _empty_confusion()
        for r in rows:
            conf[r["confusion_cell"]] += 1
            overall_conf[r["confusion_cell"]] += 1
        per_set.append(
            {
                "name": fs.name,
                "kind": fs.kind,
                "note": fs.note,
                "fixtures": len(rows),
                "confusion": conf,
                "error_stats_ms": _set_error_stats(rows),
                "rows": rows,
            }
        )
        all_rows.extend(rows)

    aggregate_stats = _set_error_stats(all_rows)
    off_diag = overall_conf["missed_yield"] + overall_conf["false_yield"]

    return {
        "tool": "hotato",
        "kind": "measurement-error-report",
        "schema_version": SCHEMA_VERSION,
        "honesty": HONESTY,
        "config": _config_snapshot(cfg),
        "fixtures_total": len(all_rows),
        "sets": [
            {k: v for k, v in s.items() if k != "rows"} | {"rows": s["rows"]} for s in per_set
        ],
        "aggregate": {
            "error_stats_ms": aggregate_stats,
            "confusion": overall_conf,
            "confusion_off_diagonal": off_diag,
        },
    }


def _set_error_stats(rows: List[dict]) -> dict:
    out = {}
    for sig in ERROR_SIGNALS:
        out[sig] = _error_stats([r["error_ms"][sig] for r in rows])
    return out


def _config_snapshot(cfg: ScoreConfig) -> dict:
    """The thresholds that produced every number, so the report reproduces."""
    return {
        "frame_ms": cfg.frame_ms,
        "hop_ms": cfg.hop_ms,
        "yield_hangover_sec": cfg.yield_hangover_sec,
        "max_search_sec": cfg.max_search_sec,
        "turn_end_silence_sec": cfg.turn_end_silence_sec,
        "premature_tolerance_sec": cfg.premature_tolerance_sec,
        "onset_min_run_sec": cfg.onset_min_run_sec,
        "caller_vad_hangover_sec": cfg.caller_vad.hangover_sec,
        "agent_vad_hangover_sec": cfg.agent_vad.hangover_sec,
    }


# --------------------------------------------------------------------------
# Fixture discovery.
# --------------------------------------------------------------------------

def load_bundled_set() -> FixtureSet:
    """The 8-scenario battery shipped inside the package (always available)."""
    scen_dir = resources.files("hotato").joinpath("data", "scenarios")
    fixtures = []
    for entry in sorted(scen_dir.iterdir(), key=lambda p: p.name):
        if not entry.name.endswith(".json") or entry.name == "manifest.json":
            continue
        # open-ok: bundled importlib resource (installed package data, not a user path)
        sc = json.loads(entry.read_text(encoding="utf-8"))
        wav = str(
            resources.files("hotato").joinpath("data", "audio", sc["id"] + ".example.wav")
        )
        fixtures.append((sc, wav))
    return FixtureSet(
        name="bundled",
        kind="synthetic",
        note="The frozen 8-scenario barge-in battery shipped in the package. Synthetic; a floor.",
        fixtures=fixtures,
    )


def load_set_from_dirs(
    name: str,
    scenarios_dir: str,
    audio_dir: str,
    *,
    kind: str = "synthetic",
    note: str = "",
    suffix: str = ".example.wav",
) -> FixtureSet:
    """Load a labelled set from a scenarios dir + an audio dir on disk.

    This is the extension point for real recordings: hand it a directory of your
    own scenario JSONs (same shape, with a ``reference_render`` or at least the
    labels you can defend) and a directory of your dual-channel ``<id>.example.wav``
    files, and the harness measures error the same way it does on the synthetic
    floor. Nothing about a "real" recording is fabricated -- if you do not supply
    a reference timing for a signal, no error is reported for it.
    """
    fixtures = []
    for fname in sorted(os.listdir(scenarios_dir)):
        if not fname.endswith(".json") or fname == "manifest.json":
            continue
        with _open_regular(os.path.join(scenarios_dir, fname), "r", encoding="utf-8") as fh:
            sc = json.load(fh)
        wav = os.path.join(audio_dir, sc["id"] + suffix)
        if not os.path.exists(wav):
            continue
        fixtures.append((sc, wav))
    return FixtureSet(name=name, kind=kind, note=note, fixtures=fixtures)


def _repo_root() -> str:
    # src/hotato/benchmark.py -> parents: hotato, src, <repo root>
    return os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def _examples_root() -> str:
    """The out-of-package ``examples/`` tree, found package-relative first and,
    failing that, relative to the current working directory. The CWD fallback
    matters when hotato is pip-installed (``_repo_root()`` then points inside
    site-packages) yet run from a source checkout, for example the extracted
    sdist whose tests run from the tree root. Returns the package-relative path
    unchanged when no examples tree exists, so an end-user install with no
    checkout still resolves to bundled-only."""
    for base in (_repo_root(), os.getcwd()):
        cand = os.path.join(base, "examples")
        if os.path.isdir(os.path.join(cand, "scenarios")):
            return cand
    return os.path.join(_repo_root(), "examples")


def default_fixture_sets() -> List[FixtureSet]:
    """Every SYNTHETIC labelled set available in this checkout.

    Always includes the bundled battery. When run from a source checkout (where
    the out-of-package ``examples/`` tree is present) it also includes the example
    reference set and the deliberately-bad-agent funnel-demo set. These are
    synthetic floors, clearly labelled as such -- never passed off as real.
    """
    sets = [load_bundled_set()]
    examples = _examples_root()
    ex_scen = os.path.join(examples, "scenarios")
    ex_aud = os.path.join(examples, "audio")
    if os.path.isdir(ex_scen) and os.path.isdir(ex_aud):
        sets.append(
            load_set_from_dirs(
                "examples",
                ex_scen,
                ex_aud,
                note="Out-of-package example references (latency + backchannel). Synthetic; a floor.",
            )
        )
    fd_scen = os.path.join(examples, "funnel-demo", "scenarios")
    fd_aud = os.path.join(examples, "funnel-demo", "audio")
    if os.path.isdir(fd_scen) and os.path.isdir(fd_aud):
        sets.append(
            load_set_from_dirs(
                "funnel-demo",
                fd_scen,
                fd_aud,
                note=(
                    "A DELIBERATELY-BAD agent (renders that miss a real interruption "
                    "AND yield on a bare backchannel). Its confusion off-diagonal cells "
                    "are the intended renders: the scorer correctly caught a misbehaving "
                    "agent, NOT scorer error."
                ),
            )
        )
    return sets


# --------------------------------------------------------------------------
# Markdown rendering.
# --------------------------------------------------------------------------

def _fmt(v) -> str:
    return "-" if v is None else (f"{v:g}" if isinstance(v, (int, float)) else str(v))


def _signal_label(sig: str) -> str:
    return {
        "onset_sec": "caller onset",
        "time_to_yield_sec": "time to yield",
        "response_gap_sec": "response gap",
    }[sig]


def render_markdown(report: dict) -> str:
    lines: List[str] = []
    lines.append("# Hotato measurement-error report")
    lines.append("")
    lines.append(
        "Measurement error, in milliseconds, between what the scorer measured and "
        "what each fixture was rendered/labelled to be. **No accuracy percentage** "
        "appears here and none is implied: the report is the error distribution and "
        "the confusion matrix, on purpose (see `docs/BENCHMARK.md`)."
    )
    lines.append("")
    lines.append(
        f"Fixtures scored: **{report['fixtures_total']}** "
        f"(synthetic floor). Config: hop {report['config']['hop_ms']:g} ms, "
        f"VAD hangover {report['config']['agent_vad_hangover_sec']:g} s, "
        f"yield hangover {report['config']['yield_hangover_sec']:g} s "
        "(all exposed `ScoreConfig` knobs)."
    )
    lines.append("")

    # --- aggregate per-signal error table ---------------------------------
    lines.append("## Per-signal measurement error (ms)")
    lines.append("")
    lines.append("| signal | n | median | mean | worst | best |")
    lines.append("|---|---:|---:|---:|---:|---:|")
    agg = report["aggregate"]["error_stats_ms"]
    for sig in ERROR_SIGNALS:
        s = agg[sig]
        lines.append(
            f"| {_signal_label(sig)} | {s['n']} | {_fmt(s['median_ms'])} | "
            f"{_fmt(s['mean_ms'])} | {_fmt(s['max_ms'])} | {_fmt(s['min_ms'])} |"
        )
    lines.append("")
    lines.append(
        "_Error is signed-magnitude `|measured - rendered|`. It is dominated by the "
        "VAD hangover and the frame hop (both exposed knobs), not by an accuracy "
        "ceiling; neutralising the hangover shrinks it toward one hop._"
    )
    lines.append("")

    # --- confusion matrix -------------------------------------------------
    conf = report["aggregate"]["confusion"]
    lines.append("## did_yield confusion matrix")
    lines.append("")
    lines.append("Rows = the label (should the agent yield?). Columns = what the scorer measured.")
    lines.append("")
    lines.append("| label \\ measured | did_yield | held floor |")
    lines.append("|---|---:|---:|")
    lines.append(
        f"| **should_yield** | {conf['correct_yield']} (correct yield) | "
        f"{conf['missed_yield']} (missed yield) |"
    )
    lines.append(
        f"| **should_not_yield** | {conf['false_yield']} (false yield) | "
        f"{conf['correct_hold']} (correct hold) |"
    )
    lines.append("")
    lines.append(
        f"Off-diagonal (missed + false yields): **{report['aggregate']['confusion_off_diagonal']}**. "
        "Off-diagonal entries from a `funnel-demo` set are the deliberately-bad-agent "
        "renders -- the scorer correctly flagged a misbehaving agent, not a scorer error."
    )
    lines.append("")

    # --- per-fixture detail ----------------------------------------------
    lines.append("## Per-fixture detail")
    lines.append("")
    lines.append(
        "| set | fixture | expected | did_yield | onset err | yield err | gap err |"
    )
    lines.append("|---|---|---|---|---:|---:|---:|")
    for s in report["sets"]:
        for r in s["rows"]:
            exp = "yield" if r["expected_yield"] else "hold"
            dy = "yes" if r["did_yield"] else "no"
            e = r["error_ms"]
            lines.append(
                f"| {s['name']} | `{r['id']}` | {exp} | {dy} | "
                f"{_fmt(e['onset_sec'])} | {_fmt(e['time_to_yield_sec'])} | "
                f"{_fmt(e['response_gap_sec'])} |"
            )
    lines.append("")
    lines.append(
        "_A `-` means the fixture carries no rendered ground truth for that signal, "
        "so no error is reported (an honest gap, never a fabricated reference). "
        "Errors are in milliseconds._"
    )
    lines.append("")
    return "\n".join(lines)


# --------------------------------------------------------------------------
# I/O + CLI (only this layer touches the filesystem for output).
# --------------------------------------------------------------------------

def write_artifacts(report: dict, out_dir: str) -> Tuple[str, str]:
    os.makedirs(out_dir, exist_ok=True)
    json_path = os.path.join(out_dir, "measurement-error.json")
    md_path = os.path.join(out_dir, "measurement-error.md")
    with open(json_path, "w", encoding="utf-8") as fh:
        json.dump(report, fh, indent=2, sort_keys=False)
        fh.write("\n")
    with open(md_path, "w", encoding="utf-8") as fh:
        fh.write(render_markdown(report))
        fh.write("\n")
    return json_path, md_path


def _build_argparser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="python -m hotato.benchmark",
        description=(
            "Reproducible measurement-error harness: per-signal ms error + a "
            "did_yield confusion matrix over labelled dual-channel recordings. "
            "No accuracy percentage. Runs on the synthetic fixtures by default; "
            "point --scenarios/--audio at your own labelled recordings to measure "
            "real validity."
        ),
    )
    p.add_argument(
        "--scenarios",
        help="Directory of scenario JSON labels (bring-your-own labelled recordings). "
        "When given, ONLY this set is scored.",
    )
    p.add_argument("--audio", help="Directory of <id>.example.wav recordings for --scenarios.")
    p.add_argument("--name", default="byo", help="Name for the --scenarios set (default: byo).")
    p.add_argument("--suffix", default=".example.wav", help="Audio filename suffix (default: .example.wav).")
    p.add_argument(
        "--out",
        default=os.path.join(os.getcwd(), "benchmark-report"),
        help="Output directory for the JSON + markdown report (default: ./benchmark-report).",
    )
    p.add_argument("--quiet", action="store_true", help="Do not print the markdown table to stdout.")
    return p


def main(argv=None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    args = _build_argparser().parse_args(argv)

    # Same exit-2 usage-error contract the real `hotato` CLI guarantees
    # (errors.HANDLED): a malformed BYO WAV, an unreadable/missing scenarios or
    # audio directory, or an unwritable --out surfaces as a clean one-line
    # refusal here too, never a raw traceback with Python's default exit 1.
    try:
        if args.scenarios:
            if not args.audio:
                print("error: --scenarios requires --audio", file=sys.stderr)
                return 2
            fixture_sets = [
                load_set_from_dirs(
                    args.name,
                    args.scenarios,
                    args.audio,
                    kind="byo",
                    note="Bring-your-own labelled recordings. Reference timings are the contributor's; nothing here is fabricated.",
                    suffix=args.suffix,
                )
            ]
        else:
            fixture_sets = default_fixture_sets()

        report = run_benchmark(fixture_sets)
        json_path, md_path = write_artifacts(report, args.out)
    except (ValueError, OSError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2

    if not args.quiet:
        print(render_markdown(report))
        print(f"\nwrote {json_path}")
        print(f"wrote {md_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
