"""``hotato compare``: the shareable before/after for one fixed call moment.

Score two recordings of the SAME scenario (the bad take, and the take after a
config change) with the identical expectation, bounds, and reference config,
and report what actually moved: verdict, ``did_yield``, ``seconds_to_yield``,
``talk_over_sec``. Every mark is computed from the real measurements only.

Result taxonomy (one word, machine-stable):

  fixed        the before failed and the after passes
  regressed    the before passed and the after fails
  improved     both fail, but the key metric moved the right way
  worse        both fail, and the key metric moved the wrong way
  unchanged    both fail with no real movement
  still_pass   both pass
  not_scorable either side could not be judged; no verdict is invented

Key-metric priority when both sides fail: for a yield label, the pass/fail
flip dominates, then lower talk-over, then a faster (or newly present) yield.
For a hold label the flip dominates, then not yielding beats yielding, then a
later false yield beats a fast one.

Exit codes: 0 by default (compare measures, it does not gate);
``--fail-on-worse`` exits 1 on ``regressed`` or ``worse``; 2 for unusable
input or a comparison where either side is not scorable.
"""

from __future__ import annotations

import os
from typing import Optional

from .core import run_single

__all__ = ["compare_recordings", "render_text", "RESULTS"]

RESULTS = ("fixed", "regressed", "improved", "worse", "unchanged",
           "still_pass", "not_scorable")

# Measured values are rounded to 3 decimals; movement beyond this is real.
_EPS = 0.0005


def _side(*, stereo, caller, agent, caller_channel, agent_channel, onset_sec,
          expect, stack, max_talk_over_sec, max_time_to_yield_sec):
    """Score one side exactly like ``hotato run`` and return
    (envelope, event)."""
    env = run_single(
        stereo=stereo,
        caller=caller,
        agent=agent,
        caller_channel=caller_channel,
        agent_channel=agent_channel,
        onset_sec=onset_sec,
        expect=expect,
        stack=stack,
        max_talk_over_sec=max_talk_over_sec,
        max_time_to_yield_sec=max_time_to_yield_sec,
    )
    return env, env["events"][0]


def _both_fail_result(expect_yield: bool, be: dict, ae: dict) -> str:
    bv, av = be["verdict"], ae["verdict"]
    if expect_yield:
        # Priority: lower talk-over, then a faster (or newly present) yield.
        d_tov = av["talk_over_sec"] - bv["talk_over_sec"]
        if d_tov < -_EPS:
            return "improved"
        if d_tov > _EPS:
            return "worse"
        b_tty, a_tty = bv["seconds_to_yield"], av["seconds_to_yield"]
        if b_tty is None and a_tty is not None:
            return "improved"       # a yield appeared, even if still too slow
        if b_tty is not None and a_tty is None:
            return "worse"          # the yield disappeared
        if b_tty is not None and a_tty is not None:
            if a_tty < b_tty - _EPS:
                return "improved"
            if a_tty > b_tty + _EPS:
                return "worse"
        return "unchanged"
    # Hold label: both failing means both yielded when they should not have.
    # Not yielding beats yielding (handled by the flip); among false yields a
    # later one is less disruptive than a fast one.
    b_tty, a_tty = bv["seconds_to_yield"], av["seconds_to_yield"]
    if b_tty is not None and a_tty is not None:
        if a_tty > b_tty + _EPS:
            return "improved"
        if a_tty < b_tty - _EPS:
            return "worse"
    return "unchanged"


def compare_recordings(
    *,
    before_stereo: Optional[str] = None,
    before_caller: Optional[str] = None,
    before_agent: Optional[str] = None,
    after_stereo: Optional[str] = None,
    after_caller: Optional[str] = None,
    after_agent: Optional[str] = None,
    onset_sec: Optional[float] = None,
    before_onset_sec: Optional[float] = None,
    after_onset_sec: Optional[float] = None,
    expect: str = "yield",
    stack: Optional[str] = None,
    max_talk_over_sec: Optional[float] = None,
    max_time_to_yield_sec: Optional[float] = None,
    caller_channel: int = 0,
    agent_channel: int = 1,
) -> dict:
    """Score both takes with the identical config and expectation and return
    the comparison dict (``kind: "compare"``). Raises ValueError (exit 2) on
    a malformed input form."""
    if not (before_stereo or (before_caller and before_agent)):
        raise ValueError(
            "provide the before take: --before FILE, or both "
            "--before-caller FILE and --before-agent FILE"
        )
    if not (after_stereo or (after_caller and after_agent)):
        raise ValueError(
            "provide the after take: --after FILE, or both "
            "--after-caller FILE and --after-agent FILE"
        )
    want_yield = str(expect).strip().lower() not in ("hold", "no", "false",
                                                     "hold-floor")
    shared = dict(
        expect=expect,
        stack=stack,
        max_talk_over_sec=max_talk_over_sec,
        max_time_to_yield_sec=max_time_to_yield_sec,
        caller_channel=caller_channel,
        agent_channel=agent_channel,
    )
    b_onset = before_onset_sec if before_onset_sec is not None else onset_sec
    a_onset = after_onset_sec if after_onset_sec is not None else onset_sec
    b_env, b_event = _side(stereo=before_stereo, caller=before_caller,
                           agent=before_agent, onset_sec=b_onset, **shared)
    a_env, a_event = _side(stereo=after_stereo, caller=after_caller,
                           agent=after_agent, onset_sec=a_onset, **shared)

    b_scorable = b_event.get("scorable") is not False
    a_scorable = a_event.get("scorable") is not False
    if not (b_scorable and a_scorable):
        result = "not_scorable"
    else:
        b_pass = bool(b_event["verdict"]["passed"])
        a_pass = bool(a_event["verdict"]["passed"])
        if not b_pass and a_pass:
            result = "fixed"
        elif b_pass and not a_pass:
            result = "regressed"
        elif b_pass and a_pass:
            result = "still_pass"
        else:
            result = _both_fail_result(want_yield, b_event, a_event)

    bv, av = b_event["verdict"], a_event["verdict"]
    d_tov = None
    if b_scorable and a_scorable:
        d_tov = round(av["talk_over_sec"] - bv["talk_over_sec"], 3)
    return {
        "tool": "hotato",
        "kind": "compare",
        "schema_version": "1",
        "stack": (stack or "generic").strip().lower(),
        "expect": "yield" if want_yield else "hold",
        "result": result,
        "before": {"envelope": b_env, "event": b_event},
        "after": {"envelope": a_env, "event": a_event},
        "delta": {
            "did_yield": [bv["did_yield"], av["did_yield"]],
            "seconds_to_yield_sec": [bv["seconds_to_yield"],
                                     av["seconds_to_yield"]],
            "talk_over_sec": [bv["talk_over_sec"], av["talk_over_sec"]],
            "talk_over_delta_sec": d_tov,
        },
    }


def _verdict_word(event: dict) -> str:
    if event.get("scorable") is False:
        return "NOT SCORABLE"
    return "PASS" if event["verdict"]["passed"] else "FAIL"


def _s(x) -> str:
    return "-" if x is None else f"{x:.2f}s"


def render_text(cmp_env: dict, before_name: str, after_name: str) -> str:
    b, a = cmp_env["before"]["event"], cmp_env["after"]["event"]
    d = cmp_env["delta"]
    lines = [f"hotato compare: {before_name} -> {after_name}"]
    lines.append(f"  verdict:           {_verdict_word(b)} -> "
                 f"{_verdict_word(a)}")
    for side, name in ((b, "before"), (a, "after")):
        if side.get("scorable") is False:
            lines.append(f"  reason ({name}):   "
                         f"{side.get('not_scorable_reason')}")
    if cmp_env["result"] != "not_scorable":
        lines.append(
            f"  did_yield:         "
            f"{str(d['did_yield'][0]).lower()} -> "
            f"{str(d['did_yield'][1]).lower()}"
        )
        lines.append(
            f"  seconds_to_yield:  {_s(d['seconds_to_yield_sec'][0])} -> "
            f"{_s(d['seconds_to_yield_sec'][1])}"
        )
        tov = (f"  talk_over_sec:     {_s(d['talk_over_sec'][0])} -> "
               f"{_s(d['talk_over_sec'][1])}")
        delta = d["talk_over_delta_sec"]
        if delta is not None and abs(delta) > _EPS:
            word = "improved" if delta < 0 else "worse"
            tov += f"  {word} {delta:+.2f}s"
        lines.append(tov)
    lines.append("")
    lines.append(f"result: {cmp_env['result']}")
    return "\n".join(lines)


def input_name(stereo, caller, agent) -> str:
    if stereo:
        return os.path.basename(stereo)
    return f"{os.path.basename(caller)}+{os.path.basename(agent)}"
