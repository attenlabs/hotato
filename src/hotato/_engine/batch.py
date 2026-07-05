"""Run the scorer over a folder of scenarios and emit a results table.

Produces the Markdown table you can paste into a pull request to show a
change did not regress interruption handling, plus a machine-readable JSON
summary you can gate CI on.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from statistics import median
from typing import List, Optional

from .audio import read_wav
from .score import ScoreConfig, evaluate, score_stereo


@dataclass
class Row:
    scenario_id: str
    category: str
    expected_yield: bool
    did_yield: bool
    time_to_yield_sec: Optional[float]
    talk_over_sec: float
    passed: bool
    reasons: List[str]


def load_scenarios(scenarios_dir: str) -> List[dict]:
    scenarios = []
    for name in sorted(os.listdir(scenarios_dir)):
        if not name.endswith(".json"):
            continue
        if name == "manifest.json":
            continue
        with open(os.path.join(scenarios_dir, name), "r", encoding="utf-8") as fh:
            scenarios.append(json.load(fh))
    return scenarios


def run(
    scenarios_dir: str,
    audio_dir: str,
    suffix: str = ".example.wav",
    caller_channel: int = 0,
    agent_channel: int = 1,
    cfg: ScoreConfig = None,
) -> List[Row]:
    rows: List[Row] = []
    for sc in load_scenarios(scenarios_dir):
        wav_path = os.path.join(audio_dir, sc["id"] + suffix)
        if not os.path.exists(wav_path):
            rows.append(
                Row(
                    scenario_id=sc["id"],
                    category=sc.get("category", ""),
                    expected_yield=bool(sc.get("expected", {}).get("yield", True)),
                    did_yield=False,
                    time_to_yield_sec=None,
                    talk_over_sec=0.0,
                    passed=False,
                    reasons=[f"missing audio: {wav_path} (run scenarios/generate_fixtures.py)"],
                )
            )
            continue
        signal = read_wav(wav_path)
        result = score_stereo(
            signal,
            caller_channel,
            agent_channel,
            caller_onset_sec=sc.get("caller_onset_sec"),
            cfg=cfg,
        )
        verdict = evaluate(result, sc.get("expected", {}))
        rows.append(
            Row(
                scenario_id=sc["id"],
                category=sc.get("category", ""),
                expected_yield=bool(sc.get("expected", {}).get("yield", True)),
                did_yield=result.did_yield,
                time_to_yield_sec=result.time_to_yield_sec,
                talk_over_sec=result.talk_over_sec,
                passed=verdict.passed,
                reasons=verdict.reasons,
            )
        )
    return rows


def summarize(rows: List[Row]) -> dict:
    yield_cases = [r for r in rows if r.expected_yield]
    yielded = [r for r in yield_cases if r.did_yield]
    ttoys = [r.time_to_yield_sec for r in yielded if r.time_to_yield_sec is not None]
    overs = [r.talk_over_sec for r in yield_cases]
    return {
        "scenarios": len(rows),
        "passed": sum(1 for r in rows if r.passed),
        "failed": sum(1 for r in rows if not r.passed),
        "yield_rate": (len(yielded) / len(yield_cases)) if yield_cases else None,
        "median_time_to_yield_sec": round(median(ttoys), 3) if ttoys else None,
        "median_talk_over_sec": round(median(overs), 3) if overs else None,
        "max_talk_over_sec": round(max(overs), 3) if overs else None,
    }


def _fmt(v) -> str:
    return "-" if v is None else (f"{v:.2f}" if isinstance(v, float) else str(v))


def to_markdown(rows: List[Row], title: str = "Barge-in regression results") -> str:
    s = summarize(rows)
    lines = [f"### {title}", ""]
    yr = f"{s['yield_rate'] * 100:.0f}%" if s["yield_rate"] is not None else "-"
    lines.append(
        f"**{s['passed']}/{s['scenarios']} scenarios pass** | "
        f"yield rate on should-yield cases: {yr} | "
        f"median time-to-yield: {_fmt(s['median_time_to_yield_sec'])}s | "
        f"median talk-over: {_fmt(s['median_talk_over_sec'])}s"
    )
    lines.append("")
    lines.append("| scenario | expected | did yield | time-to-yield (s) | talk-over (s) | result |")
    lines.append("|---|---|---|---|---|---|")
    for r in rows:
        exp = "yield" if r.expected_yield else "hold floor"
        did = "yes" if r.did_yield else "no"
        res = "pass" if r.passed else "FAIL"
        lines.append(
            f"| {r.scenario_id} | {exp} | {did} | {_fmt(r.time_to_yield_sec)} | "
            f"{_fmt(r.talk_over_sec)} | {res} |"
        )
    fails = [r for r in rows if not r.passed]
    if fails:
        lines.append("")
        lines.append("**Failures**")
        for r in fails:
            for why in r.reasons:
                lines.append(f"- `{r.scenario_id}`: {why}")
    return "\n".join(lines) + "\n"


def to_json(rows: List[Row]) -> dict:
    return {
        "summary": summarize(rows),
        "rows": [
            {
                "scenario_id": r.scenario_id,
                "category": r.category,
                "expected_yield": r.expected_yield,
                "did_yield": r.did_yield,
                "time_to_yield_sec": r.time_to_yield_sec,
                "talk_over_sec": r.talk_over_sec,
                "passed": r.passed,
                "reasons": r.reasons,
            }
            for r in rows
        ],
    }
