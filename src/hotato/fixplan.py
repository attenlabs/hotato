"""Level 2 of the guarded fix ladder: a guarded, reviewable fix plan.

``hotato plan`` combines a diagnosis (``diagnose.py``) with the inspected
current config (``inspectcfg.py``) into one fix-plan JSON, schema
``hotato.fixplan.v1``. The plan is a PROPOSAL document: this phase ships no
apply command, nothing here mutates any platform config, and every plan pins
``approval.production_apply`` to false.

The policy engine is data plus a small evaluator, and every rule is tested.
A change is proposed ONLY when ALL of these hold:

  (a) the failure class maps cleanly to ONE setting (the _KNOBS table);
  (b) the proposed value is ONE bounded step in an unambiguous direction
      within documented bounds - never an absolute magic value; ``from`` is
      the inspected current value, and when inspection was not run or not
      possible the plan carries direction and bounds only, with
      ``current_unknown: true``;
  (c) the run's battery contains at least one passing OPPOSITE-RISK fixture,
      else the plan downgrades to ``insufficient_coverage`` and names the
      exact fixture family to add before tuning;
  (d) the diagnosis marked the failure ``config_only_safe``.

Refusals are first-class outputs:

* threshold_funnel (the battery misses a real interruption AND false-stops on
  a backchannel) produces ``do_not_tune_single_threshold`` with a
  vendor-neutral engagement-control pointer - no product names, no digits.
* a slow yield without a clear layer produces a diagnostic CHECKLIST
  (instrumentation steps), never a knob change.
"""

from __future__ import annotations

from typing import Optional

from . import fixmap as _fixmap
from .diagnose import OPPOSITE_RISK

SCHEMA_ID = "hotato.fixplan.v1"

REQUIRED_VERIFICATION = [
    "real_interruption_fixture_must_pass",
    "backchannel_fixture_must_not_regress",
    "slow_yield_p95_must_not_worsen",
]

APPROVAL = {"default": "manual", "production_apply": False}

DECISIONS = (
    "propose_one_step",
    "do_not_tune_single_threshold",
    "diagnostic_checklist",
    "insufficient_coverage",
    "at_documented_bound",
    "no_change",
)

# Refusal pointer for the threshold funnel. Vendor-neutral by policy: it names
# the fix CLASS and generic mechanisms only - no product names and no digits
# anywhere in these strings (tested).
ENGAGEMENT_CONTROL_FIX = {
    "class": "engagement-control",
    "examples": [
        "enable adaptive interruption handling where available",
        "use a backchannel-aware interruption classifier",
        "add addressee/turn-intent discrimination before stopping TTS",
    ],
}

# Which single tuning intent each finding maps to (policy rule (a)).
# threshold_funnel and not_scorable never map; a non-config-safe diagnosis
# never reaches this table (rule (d) runs first).
_INTENTS = {
    "missed_real_interruption": "more_sensitive",
    "false_stop_on_backchannel": "suppress_false_trigger",
    "slow_yield": "faster_yield",
    "excess_talk_over": "less_talk_over",
    "endpointing_miss": "faster_endpointing",
}

# The regression risk each intent trades against (stated in every change).
_RISKS = {
    "more_sensitive": (
        "more false stops on short acknowledgements; the backchannel fixture "
        "must not regress"
    ),
    "suppress_false_trigger": (
        "short genuine interruptions such as 'stop' may be delayed or "
        "dropped; the real interruption fixture must still pass"
    ),
    "faster_yield": "may clip the agent on noisy lines or ordinary listener noise",
    "less_talk_over": "may clip the agent on brief benign overlaps",
    "faster_endpointing": (
        "may start speaking inside the caller's natural pauses "
        "(premature starts)"
    ),
}

# Per-stack knob table: for each intent, the ONE concrete field, the step
# direction, one bounded step size, and the documented bounds.
#
# source = where the inspected current value lives: ("turn_taking", key) for
# the normalized model, ("raw", key) for stacks that only expose unitless
# scales (Retell).
#
# Bounds provenance: Vapi ranges from docs.vapi.ai speech-configuration
# (numWords 0-10, voiceSeconds 0-0.5, backoffSeconds 0-10, waitSeconds 0-5);
# Retell responsiveness / interruption_sensitivity 0-1 from
# docs.retellai.com/api-references/get-agent (all verified 2026-07-06).
# LiveKit and Pipecat do not document hard ranges for these options, so their
# bounds are conservative working ranges around the documented defaults and
# every change says to verify against the installed version.
_KNOBS = {
    "vapi": {
        "more_sensitive": {
            "field": "stopSpeakingPlan.numWords",
            "source": ("turn_taking", "interrupt_min_words"),
            "direction": "decrease", "step": 1, "bounds": [0, 10],
            "basis": "documented range 0-10 (docs.vapi.ai, 2026-07-06)",
        },
        "suppress_false_trigger": {
            "field": "stopSpeakingPlan.numWords",
            "source": ("turn_taking", "interrupt_min_words"),
            "direction": "increase", "step": 1, "bounds": [0, 10],
            "basis": "documented range 0-10 (docs.vapi.ai, 2026-07-06)",
        },
        "faster_yield": {
            "field": "stopSpeakingPlan.voiceSeconds",
            "source": ("turn_taking", "interrupt_voice_seconds"),
            "direction": "decrease", "step": 0.1, "bounds": [0, 0.5],
            "basis": "documented range 0-0.5 (docs.vapi.ai, 2026-07-06)",
        },
        "less_talk_over": {
            "field": "stopSpeakingPlan.voiceSeconds",
            "source": ("turn_taking", "interrupt_voice_seconds"),
            "direction": "decrease", "step": 0.1, "bounds": [0, 0.5],
            "basis": "documented range 0-0.5 (docs.vapi.ai, 2026-07-06)",
        },
        "faster_endpointing": {
            "field": "startSpeakingPlan.waitSeconds",
            "source": ("turn_taking", "endpointing_wait_seconds"),
            "direction": "decrease", "step": 0.1, "bounds": [0, 5],
            "basis": "documented range 0-5 (docs.vapi.ai, 2026-07-06)",
        },
    },
    "retell": {
        "more_sensitive": {
            "field": "interruption_sensitivity",
            "source": ("raw", "interruption_sensitivity"),
            "direction": "increase", "step": 0.1, "bounds": [0, 1],
            "basis": "documented range 0-1 (docs.retellai.com, 2026-07-06)",
        },
        "suppress_false_trigger": {
            "field": "interruption_sensitivity",
            "source": ("raw", "interruption_sensitivity"),
            "direction": "decrease", "step": 0.1, "bounds": [0, 1],
            "basis": "documented range 0-1 (docs.retellai.com, 2026-07-06)",
        },
        "faster_yield": {
            "field": "interruption_sensitivity",
            "source": ("raw", "interruption_sensitivity"),
            "direction": "increase", "step": 0.1, "bounds": [0, 1],
            "basis": "documented range 0-1 (docs.retellai.com, 2026-07-06)",
        },
        "less_talk_over": {
            "field": "interruption_sensitivity",
            "source": ("raw", "interruption_sensitivity"),
            "direction": "increase", "step": 0.1, "bounds": [0, 1],
            "basis": "documented range 0-1 (docs.retellai.com, 2026-07-06)",
        },
        "faster_endpointing": {
            "field": "responsiveness",
            "source": ("raw", "responsiveness"),
            "direction": "increase", "step": 0.1, "bounds": [0, 1],
            "basis": "documented range 0-1 (docs.retellai.com, 2026-07-06)",
        },
    },
    "livekit": {
        "more_sensitive": {
            "field": "turn_handling.interruption.min_words",
            "source": ("turn_taking", "interrupt_min_words"),
            "direction": "decrease", "step": 1, "bounds": [0, 10],
            "basis": ("default 0 documented; no hard range published, "
                      "conservative working range - verify against your "
                      "installed livekit-agents version"),
        },
        "suppress_false_trigger": {
            "field": "turn_handling.interruption.min_words",
            "source": ("turn_taking", "interrupt_min_words"),
            "direction": "increase", "step": 1, "bounds": [0, 10],
            "basis": ("default 0 documented; no hard range published, "
                      "conservative working range - verify against your "
                      "installed livekit-agents version"),
        },
        "faster_yield": {
            "field": "turn_handling.interruption.min_duration",
            "source": ("turn_taking", "interrupt_voice_seconds"),
            "direction": "decrease", "step": 0.1, "bounds": [0, 3],
            "basis": ("default 0.5 documented; no hard range published, "
                      "conservative working range"),
        },
        "less_talk_over": {
            "field": "turn_handling.interruption.min_duration",
            "source": ("turn_taking", "interrupt_voice_seconds"),
            "direction": "decrease", "step": 0.1, "bounds": [0, 3],
            "basis": ("default 0.5 documented; no hard range published, "
                      "conservative working range"),
        },
        "faster_endpointing": {
            "field": "turn_handling.endpointing.min_delay",
            "source": ("turn_taking", "endpointing_wait_seconds"),
            "direction": "decrease", "step": 0.1, "bounds": [0, 3],
            "basis": ("defaults 0.5 (min) / 3.0 (max) documented; "
                      "conservative working range"),
        },
    },
    "pipecat": {
        "more_sensitive": {
            "field": "MinWordsUserTurnStartStrategy.min_words",
            "source": ("turn_taking", "interrupt_min_words"),
            "direction": "decrease", "step": 1, "bounds": [1, 10],
            "basis": ("no hard range published; conservative working range - "
                      "verify against your installed pipecat version"),
        },
        "suppress_false_trigger": {
            "field": "MinWordsUserTurnStartStrategy.min_words",
            "source": ("turn_taking", "interrupt_min_words"),
            "direction": "increase", "step": 1, "bounds": [1, 10],
            "basis": ("no hard range published; conservative working range - "
                      "verify against your installed pipecat version"),
        },
        "faster_yield": {
            "field": "VADParams.stop_secs",
            "source": ("turn_taking", "endpointing_wait_seconds"),
            "direction": "decrease", "step": 0.1, "bounds": [0.1, 2],
            "basis": ("default 0.8 documented; conservative working range"),
        },
        "less_talk_over": {
            "field": "VADParams.stop_secs",
            "source": ("turn_taking", "endpointing_wait_seconds"),
            "direction": "decrease", "step": 0.1, "bounds": [0.1, 2],
            "basis": ("default 0.8 documented; conservative working range"),
        },
        "faster_endpointing": {
            "field": "SpeechTimeoutUserTurnStopStrategy.user_speech_timeout",
            "source": ("turn_taking", "endpointing_wait_seconds"),
            "direction": "decrease", "step": 0.1, "bounds": [0.2, 3],
            "basis": ("default 0.6 documented; conservative working range"),
        },
    },
}

# Generic (no target flags / unknown stack): the change references the knob
# FAMILY from fixmap's catalogue; there is no inspected value and no documented
# bound, so the plan carries direction only and current_unknown is true.
_GENERIC_FIXMAP_INTENT = {
    "more_sensitive": "more_sensitive",
    "suppress_false_trigger": "suppress_false_trigger",
    "faster_yield": "faster_yield",
    "less_talk_over": "less_talk_over",
    "faster_endpointing": "faster_yield",
}

_GENERIC_FIELDS = {
    "more_sensitive": ("interrupt_min_words", "decrease"),
    "suppress_false_trigger": ("interrupt_min_words", "increase"),
    "faster_yield": ("interrupt_voice_seconds", "decrease"),
    "less_talk_over": ("interrupt_voice_seconds", "decrease"),
    "faster_endpointing": ("endpointing_wait_seconds", "decrease"),
}

# Instrumentation checklists (never a knob change). No absolute values.
_SLOW_YIELD_CHECKLIST = [
    "Log the timestamp the framework issues its stop-speaking command and the "
    "timestamp the audio actually goes quiet in the recording; the difference "
    "separates TTS buffering from detection latency.",
    "Re-run the same fixture over a local loopback transport; if the yield is "
    "fast locally, the latency lives in the transport path, not in detection "
    "config.",
    "Run hotato run --dump-frames on the recording and read when the caller "
    "channel goes active versus when the agent channel goes quiet; that "
    "isolates VAD smoothing from everything upstream.",
    "Add an opposite-risk backchannel fixture that passes, so any later "
    "config step is verifiable against regression.",
]

_ECHO_CHECKLIST = [
    "Confirm the agent's TTS output is not mixed into the input track: keep "
    "caller and agent on separate channels end to end.",
    "Enable echo cancellation on the input path and re-capture the same "
    "scenario.",
    "Run hotato run --dump-frames and check whether caller-channel activity "
    "aligns with agent speech segments; alignment indicates bleed, not a "
    "caller.",
    "Only after the audio path is clean, re-run the battery; do not tune "
    "turn-taking thresholds against a contaminated input.",
]

_FINDING_PRIORITY = (
    "missed_real_interruption",
    "false_stop_on_backchannel",
    "excess_talk_over",
    "slow_yield",
    "endpointing_miss",
)

# Twilio is the transport, not the turn-taking policy: no Twilio setting
# decides when the agent yields, so a twilio-targeted plan NEVER proposes an
# agent-config change. It points at the audio path and the upstream stack.
_TWILIO_CHECKLIST = [
    "Twilio carries the audio; it does not decide when the agent yields. No "
    "Twilio setting is proposed for a turn-taking failure.",
    "Confirm the recording is dual-channel and the caller/agent channel "
    "assignment is correct: hotato run --dump-frames shows which channel is "
    "active while the agent speaks.",
    "Identify the upstream voice-agent stack behind this number (for example "
    "Vapi, Retell, LiveKit, or Pipecat) and re-plan against it: "
    "hotato plan result.json --stack STACK with its target flag.",
]

_READ_ONLY_REASON = "hotato plan is read-only"


def _base_plan(*, target: dict, finding: str, hypothesis: str,
               config_only_safe: bool, decision: str, changes: list) -> dict:
    return {
        "schema": SCHEMA_ID,
        "kind": "fix-plan",
        "target": target,
        "finding": finding,
        "hypothesis": hypothesis,
        "config_only_safe": config_only_safe,
        "decision": decision,
        "changes": changes,
        "required_verification": list(REQUIRED_VERIFICATION),
        "approval": dict(APPROVAL),
        # No apply path exists in this phase; every plan states so explicitly.
        "platform_mutation": {"performed": False,
                              "reason": _READ_ONLY_REASON},
    }


def _next_commands(plan: dict) -> list:
    """What to do with this plan, as concrete commands. Applying any change is
    always a manual step in YOUR stack; hotato never mutates a platform."""
    cmds = []
    if plan["decision"] == "propose_one_step" and plan["changes"]:
        ch = plan["changes"][0]
        move = (f"{ch['from']} -> {ch['to']}" if ch["to"] is not None
                else f"one step, direction: {ch['direction']}")
        cmds.append(
            f"apply the one bounded step manually in your stack config: "
            f"{ch['field']}  {move} (hotato never applies it)"
        )
    cmds.append(
        "re-capture the same call moment through your stack, then verify the "
        "movement: hotato compare --before before.wav --after after.wav "
        "--onset 42.18 --expect yield  (use --expect hold if a hold was right)"
    )
    cmds.append(
        "re-run the battery and re-diagnose: hotato run --suite barge-in "
        "--format json > result.json && hotato diagnose result.json"
    )
    return cmds


def _finalize(plan: dict, diagnosis: dict) -> dict:
    """Attach the shared, always-present blocks: the measured evidence behind
    the plan, not-scorable events as INPUT ISSUES (never fixed), the stated
    risks, and the next commands."""
    diagnoses = (diagnosis or {}).get("diagnoses") or []
    plan["evidence"] = [
        {
            "event_id": d.get("event_id"),
            "scenario_id": d.get("scenario_id"),
            "finding": d.get("finding"),
            "measured": d.get("evidence"),
        }
        for d in diagnoses if d.get("finding") != "not_scorable"
    ]
    input_issues = [
        {
            "event_id": d.get("event_id"),
            "scenario_id": d.get("scenario_id"),
            "reason": d.get("notes"),
        }
        for d in diagnoses if d.get("finding") == "not_scorable"
    ]
    if input_issues:
        plan["input_issues"] = input_issues
    if plan["finding"] == "threshold_funnel":
        plan["risks"] = [
            "any single-threshold change trades the two failing axes "
            "against each other"
        ]
    else:
        plan["risks"] = [ch["risk"] for ch in plan["changes"]]
    plan["next_commands"] = _next_commands(plan)
    return plan


def _one_step(current, direction: str, step, bounds):
    """One bounded step from the inspected value, or None when no such step
    exists: the value is already at the documented bound, or it sits OUTSIDE
    the documented range (an out-of-range vendor value), where clamping would
    silently move opposite to the stated direction."""
    lo, hi = bounds
    to = current - step if direction == "decrease" else current + step
    to = round(to, 3)
    if lo is not None:
        to = max(lo, to)
    if hi is not None:
        to = min(hi, to)
    moved_as_stated = to < current if direction == "decrease" else to > current
    if not moved_as_stated:
        return None
    return to


def _current_value(inspected: Optional[dict], source) -> Optional[float]:
    if not inspected:
        return None
    where, key = source
    tt = inspected.get("turn_taking") or {}
    container = tt if where == "turn_taking" else (tt.get("raw") or {})
    value = container.get(key)
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return value


def _primary_diagnosis(diagnoses: list) -> Optional[dict]:
    candidates = [d for d in diagnoses if d["finding"] != "not_scorable"]
    for finding in _FINDING_PRIORITY:
        for d in candidates:
            if d["finding"] == finding:
                return d
    return None


def build_plan(
    *,
    diagnosis: dict,
    inspected: Optional[dict] = None,
    stack: Optional[str] = None,
    target_info: Optional[dict] = None,
) -> dict:
    """Evaluate the policy rules over one diagnosis (+ optional inspection).

    Pure function: reads its inputs, returns the plan dict. Never touches the
    network or any platform config (``platform_mutation.performed`` is always
    false).
    """
    plan = _build_plan_core(
        diagnosis=diagnosis,
        inspected=inspected,
        stack=stack,
        target_info=target_info,
    )
    return _finalize(plan, diagnosis)


def _build_plan_core(
    *,
    diagnosis: dict,
    inspected: Optional[dict] = None,
    stack: Optional[str] = None,
    target_info: Optional[dict] = None,
) -> dict:
    battery = diagnosis.get("battery") or {}
    coverage = battery.get("opposite_risk_coverage") or {}
    diagnoses = diagnosis.get("diagnoses") or []

    resolved_stack = (
        (stack or (inspected or {}).get("stack") or diagnosis.get("stack")
         or "generic")
    ).strip().lower()
    if resolved_stack not in _KNOBS and resolved_stack != "twilio":
        resolved_stack = "generic"
    target = {"stack": resolved_stack, "inspected": bool(inspected)}
    if target_info:
        target.update(target_info)

    # Refusal: the threshold funnel. No knob change can be safe here.
    if battery.get("finding") == "threshold_funnel":
        plan = _base_plan(
            target=target,
            finding="threshold_funnel",
            hypothesis=(
                "The battery missed a genuine interruption and also stopped "
                "for a backchannel. One sensitivity threshold cannot satisfy "
                "both: raising it to hold through backchannels drops real "
                "interruptions, lowering it to catch interruptions yields to "
                "backchannels. The failure class is discrimination, not "
                "calibration."
            ),
            config_only_safe=False,
            decision="do_not_tune_single_threshold",
            changes=[],
        )
        plan["recommended_fix"] = {
            "class": ENGAGEMENT_CONTROL_FIX["class"],
            "examples": list(ENGAGEMENT_CONTROL_FIX["examples"]),
        }
        return plan

    primary = _primary_diagnosis(diagnoses)
    if primary is None:
        return _base_plan(
            target=target,
            finding="none",
            hypothesis=("No fix needed: no scorable event failed; there is "
                        "nothing to tune."),
            config_only_safe=True,
            decision="no_change",
            changes=[],
        )

    finding = primary["finding"]
    others = sorted(
        {d["finding"] for d in diagnoses
         if d["finding"] not in ("not_scorable", finding)}
    )

    # Twilio rule: the transport has no turn-taking knobs, so a failing
    # finding never becomes agent-config advice here. The plan is a checklist
    # pointing at the channel assignment and the upstream voice-agent stack.
    if resolved_stack == "twilio":
        plan = _base_plan(
            target=target,
            finding=finding,
            hypothesis=(
                "The recording came through Twilio, which carries the audio "
                "but does not decide turn-taking. The fix lives in the "
                "upstream voice-agent stack; check the channel assignment "
                "first, then re-plan against the stack that runs the agent."
            ),
            config_only_safe=False,
            decision="diagnostic_checklist",
            changes=[],
        )
        plan["checklist"] = list(_TWILIO_CHECKLIST)
        if others:
            plan["other_findings"] = others
        return plan

    # Rule (d): not config-only-safe -> instrumentation checklist, never a knob.
    if not primary.get("config_only_safe"):
        checklist = (
            _ECHO_CHECKLIST
            if "echo" in (primary.get("notes") or "").lower()
            else _SLOW_YIELD_CHECKLIST
        )
        plan = _base_plan(
            target=target,
            finding=finding,
            hypothesis=(
                "The layer at fault cannot be identified from this evidence "
                f"({primary.get('notes')}). A config change would be a guess, "
                "so this plan is an instrumentation checklist, not a knob "
                "change."
            ),
            config_only_safe=False,
            decision="diagnostic_checklist",
            changes=[],
        )
        plan["checklist"] = list(checklist)
        if others:
            plan["other_findings"] = others
        return plan

    # Rule (c): the opposite-risk coverage gate.
    risk_info = OPPOSITE_RISK.get(finding)
    if risk_info and not coverage.get(risk_info["coverage_key"]):
        plan = _base_plan(
            target=target,
            finding=finding,
            hypothesis=(
                "insufficient_coverage: add an opposite-risk fixture before "
                f"tuning. A one-step change for {finding} trades against "
                f"{risk_info['why']}, and this battery has no passing fixture "
                "on that axis, so the change could not be verified."
            ),
            config_only_safe=True,
            decision="insufficient_coverage",
            changes=[],
        )
        plan["required_fixture_family"] = risk_info["family"]
        if others:
            plan["other_findings"] = others
        return plan

    # Rules (a) + (b): one setting, one bounded step.
    intent = _INTENTS[finding]
    if resolved_stack == "generic":
        field, direction = _GENERIC_FIELDS[intent]
        family = _fixmap._KNOBS["generic"][_GENERIC_FIXMAP_INTENT[intent]]
        change = {
            "field": field,
            "from": None,
            "to": None,
            "direction": direction,
            "bounds": [None, None],
            "reason": (
                f"{primary.get('notes')} Generic knob family: "
                f"{family['parameter']}. No stack target was given, so no "
                "current value and no documented bounds are claimed; move one "
                "step only and verify."
            ),
            "risk": _RISKS[intent],
        }
        target["current_unknown"] = True
        plan = _base_plan(
            target=target,
            finding=finding,
            hypothesis=(
                f"{advisory_sentence(finding)} Without a stack target the "
                "plan names the knob family and direction only; inspect the "
                "live config to get from/to values."
            ),
            config_only_safe=True,
            decision="propose_one_step",
            changes=[change],
        )
        if others:
            plan["other_findings"] = others
        return plan

    knob = _KNOBS[resolved_stack][intent]
    current = _current_value(inspected, knob["source"])
    if current is None:
        target["current_unknown"] = True
        change = {
            "field": knob["field"],
            "from": None,
            "to": None,
            "direction": knob["direction"],
            "bounds": list(knob["bounds"]),
            "reason": (
                f"{primary.get('notes')} Current value unknown "
                "(inspection not run, or the option is unset on the target), "
                "so the plan carries direction and documented bounds only. "
                f"Bounds basis: {knob['basis']}."
            ),
            "risk": _RISKS[intent],
        }
        plan = _base_plan(
            target=target,
            finding=finding,
            hypothesis=(
                f"{advisory_sentence(finding)} The current value could not be "
                "read, so no from/to pair is proposed; inspect first, then "
                "re-plan."
            ),
            config_only_safe=True,
            decision="propose_one_step",
            changes=[change],
        )
        if others:
            plan["other_findings"] = others
        return plan

    target["current_unknown"] = False
    to = _one_step(current, knob["direction"], knob["step"], knob["bounds"])
    if to is None:
        plan = _base_plan(
            target=target,
            finding=finding,
            hypothesis=(
                f"{advisory_sentence(finding)} The inspected value "
                f"({knob['field']} = {current}) is already at the documented "
                f"bound ({knob['bounds']}), so no further single-step config "
                "change exists on this axis. The remaining fix classes are "
                "outside single-threshold tuning."
            ),
            config_only_safe=True,
            decision="at_documented_bound",
            changes=[],
        )
        if others:
            plan["other_findings"] = others
        return plan

    change = {
        "field": knob["field"],
        "from": current,
        "to": to,
        "direction": knob["direction"],
        "bounds": list(knob["bounds"]),
        "reason": (
            f"{primary.get('notes')} One step {knob['direction']} from the "
            f"inspected value. Bounds basis: {knob['basis']}."
        ),
        "risk": _RISKS[intent],
    }
    plan = _base_plan(
        target=target,
        finding=finding,
        hypothesis=(
            f"{advisory_sentence(finding)} Inspected {knob['field']} is "
            f"{current}; one bounded step {knob['direction']} is the smallest "
            "verifiable change on this axis."
        ),
        config_only_safe=True,
        decision="propose_one_step",
        changes=[change],
    )
    if others:
        plan["other_findings"] = others
    return plan


def advisory_sentence(finding: str) -> str:
    return {
        "missed_real_interruption": (
            "The agent missed a real interruption: the caller took the floor "
            "and the agent kept talking."
        ),
        "false_stop_on_backchannel": (
            "The agent stopped for a backchannel that was not a bid for the "
            "floor."
        ),
        "slow_yield": "The agent yielded, but slower than the bound.",
        "excess_talk_over": (
            "The agent talked over the caller longer than the bound before "
            "yielding."
        ),
        "endpointing_miss": (
            "The turn boundary was mishandled: dead air before the response "
            "or a premature start."
        ),
    }.get(finding, finding)


def render_text(plan: dict) -> str:
    lines = [
        f"hotato plan [{plan['target'].get('stack')}] finding={plan['finding']} "
        f"decision={plan['decision']}",
        f"  config_only_safe={str(plan['config_only_safe']).lower()}  "
        f"production_apply={str(plan['approval']['production_apply']).lower()} "
        f"(approval: {plan['approval']['default']})",
        f"  hypothesis: {plan['hypothesis']}",
    ]
    for ch in plan["changes"]:
        frm = "?" if ch["from"] is None else ch["from"]
        to = "?" if ch["to"] is None else ch["to"]
        lines.append(
            f"  change: {ch['field']}  {frm} -> {to}  ({ch['direction']}, "
            f"bounds {ch['bounds']})"
        )
        lines.append(f"    risk: {ch['risk']}")
    if plan.get("recommended_fix"):
        lines.append(f"  recommended fix class: {plan['recommended_fix']['class']}")
        for ex in plan["recommended_fix"]["examples"]:
            lines.append(f"    - {ex}")
    if plan.get("checklist"):
        lines.append("  checklist (instrument before tuning):")
        for item in plan["checklist"]:
            lines.append(f"    - {item}")
    if plan.get("required_fixture_family"):
        lines.append(
            f"  add before tuning: {plan['required_fixture_family']}"
        )
    for issue in plan.get("input_issues") or []:
        lines.append(
            f"  input issue (not an agent failure): {issue['event_id']}: "
            f"{issue['reason']}"
        )
    lines.append(
        "  verify after any change: " + ", ".join(plan["required_verification"])
    )
    mutation = plan.get("platform_mutation") or {}
    if mutation:
        lines.append(
            f"  platform mutation: performed="
            f"{str(mutation.get('performed')).lower()} "
            f"({mutation.get('reason')})"
        )
    if plan.get("next_commands"):
        lines.append("  next:")
        for cmd in plan["next_commands"]:
            lines.append(f"    - {cmd}")
    return "\n".join(lines)
