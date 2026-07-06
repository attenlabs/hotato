"""Level 0 of the guarded fix ladder: turn an envelope into an honest diagnosis.

``hotato diagnose`` reads a finished hotato envelope (the output of ``hotato run``
or ``hotato capture``) and emits, per failing event, a structured diagnosis:

    {"finding":          one of FINDINGS,
     "evidence":         the real measured fields for that event,
     "likely_layer":     interruption_detection | endpointing | unknown_root_cause,
     "config_only_safe": bool,
     "notes":            plain language}

plus one battery-level decision. Everything here is derived from fields the
scorer actually measured; nothing is fabricated and no accuracy is claimed.

Honesty rules encoded here (each one is tested):

* threshold_funnel: when the battery contains BOTH a missed real interruption
  AND a false stop on a backchannel, no single sensitivity threshold can fix
  both. The battery decision is ``do_not_tune_single_threshold`` and the two
  participating findings are marked ``config_only_safe = false``.
* slow_yield ambiguity: TTS buffering, transport latency, and VAD smoothing are
  indistinguishable from one recording. A slow yield is therefore
  ``unknown_root_cause`` and NOT config-only-safe unless the battery contains an
  opposite-risk fixture that passes (a should-hold backchannel fixture), which
  makes a one-step change verifiable against regression.
* not_scorable events are input problems. They are surfaced with their reason
  and are never diagnosed as agent failures, never counted toward the battery
  decision, and never feed a fix plan.

This module is read-only: it never touches any platform config.
"""

from __future__ import annotations

from typing import Optional

FINDINGS = (
    "missed_real_interruption",
    "false_stop_on_backchannel",
    "slow_yield",
    "excess_talk_over",
    "endpointing_miss",
    "not_scorable",
    "threshold_funnel",
)

LAYERS = ("interruption_detection", "endpointing", "unknown_root_cause")

BATTERY_DECISIONS = (
    "do_not_tune_single_threshold",
    "no_failures",
    "tune_one_step_with_verification",
    "insufficient_coverage",
    "needs_instrumentation",
)

# Opposite-risk fixture family required before a one-step change for each
# finding can be verified against regression. The key into the coverage block
# says WHICH passing fixture kind provides that verification; the family string
# names the fixture kind in plain language (used verbatim when a plan
# downgrades to insufficient_coverage).
OPPOSITE_RISK = {
    "missed_real_interruption": {
        "coverage_key": "backchannel_hold_pass",
        "family": "a should-hold backchannel fixture (expected_yield=false) that passes",
        "why": "raising sensitivity risks false stops on short acknowledgements",
    },
    "false_stop_on_backchannel": {
        "coverage_key": "real_interruption_pass",
        "family": "a should-yield real interruption fixture (expected_yield=true) that passes",
        "why": "lowering sensitivity risks missing genuine short interruptions",
    },
    "slow_yield": {
        "coverage_key": "backchannel_hold_pass",
        "family": "a should-hold backchannel fixture (expected_yield=false) that passes",
        "why": "cutting yield latency risks clipping the agent on listener noise",
    },
    "excess_talk_over": {
        "coverage_key": "backchannel_hold_pass",
        "family": "a should-hold backchannel fixture (expected_yield=false) that passes",
        "why": "tightening the overlap debounce risks clipping on ordinary noise",
    },
    "endpointing_miss": {
        "coverage_key": "measured_latency_pass",
        "family": "a prompt-response latency fixture with a measured response gap that passes",
        "why": "lowering the endpointing wait risks premature starts inside caller pauses",
    },
}

# The Level 0 advisory, printed in text mode: plain language, the honest
# tradeoff always stated. Keyed by (finding, config_only_safe).
_ADVISORIES = {
    ("missed_real_interruption", True): (
        "Missed real interruption. Likely config layer. Try lowering the "
        "stop-speaking word threshold one step. Tradeoff: may increase false "
        "stops on short acknowledgements."
    ),
    ("missed_real_interruption", False): (
        "Missed real interruption, but this battery also false-stops on a "
        "backchannel. Do not tune a single threshold: fixing one axis worsens "
        "the other. See the battery decision."
    ),
    ("false_stop_on_backchannel", True): (
        "False stop on a backchannel. Likely config layer. Try raising the "
        "stop-speaking word threshold one step. Tradeoff: may delay or miss "
        "short genuine interruptions such as 'stop'."
    ),
    ("false_stop_on_backchannel", False): (
        "False stop, but tuning a single threshold is not safe here. See the "
        "notes for this event and the battery decision."
    ),
    ("slow_yield", True): (
        "Slow yield: the agent stopped, but late. Likely endpointing layer. Try "
        "lowering the endpointing or voice-window setting one step. Tradeoff: "
        "may clip the agent on noisy lines; verify against the passing "
        "backchannel fixture."
    ),
    ("slow_yield", False): (
        "Slow yield with an ambiguous root cause. TTS buffering, transport "
        "latency, and VAD smoothing are indistinguishable from one recording. "
        "Instrument before tuning; add an opposite-risk backchannel fixture so "
        "a later change is verifiable."
    ),
    ("excess_talk_over", True): (
        "Excess talk-over before the yield. Likely config layer. Try lowering "
        "the interruption voice window one step. Tradeoff: may clip the agent "
        "on ordinary listener noise."
    ),
    ("endpointing_miss", True): (
        "Endpointing miss: dead air or a premature start at the turn boundary. "
        "Likely endpointing layer. Try lowering the endpointing wait one step. "
        "Tradeoff: may cut into the caller's natural pauses."
    ),
    ("not_scorable", False): (
        "Not scorable: an input problem, never an agent verdict. Fix the "
        "recording, the onset time, or the channel mapping and re-run."
    ),
}


def advisory_for(finding: str, config_only_safe: bool) -> str:
    """The Level 0 plain-language advisory for a diagnosis.

    The fallback is fail-closed: a finding marked NOT config-only-safe never
    falls back to a 'try turning this knob' text, even when no unsafe-variant
    advisory exists for it."""
    specific = _ADVISORIES.get((finding, config_only_safe))
    if specific is not None:
        return specific
    if not config_only_safe:
        return (
            "This failure is not safe to fix with a single threshold change "
            "from this evidence. See the event notes and the battery decision."
        )
    return _ADVISORIES.get((finding, True), "No advisory for this finding.")


# --- envelope plumbing ------------------------------------------------------

def _require_envelope(env) -> dict:
    if not (
        isinstance(env, dict)
        and env.get("tool") == "hotato"
        and env.get("kind") != "frame-dump"
        and isinstance(env.get("events"), list)
    ):
        raise ValueError(
            "input is not a hotato envelope JSON. Save one with: "
            "hotato run --suite barge-in --format json > result.json"
        )
    return env


def _is_missing_audio(event: dict) -> bool:
    reasons = (event.get("verdict") or {}).get("reasons") or []
    return any(str(r).startswith("missing audio") for r in reasons)


def _is_not_scorable(event: dict) -> bool:
    return event.get("scorable") is False or _is_missing_audio(event)


def _is_failing(event: dict) -> bool:
    return not (event.get("verdict") or {}).get("passed", False)


def _is_echo(event: dict) -> bool:
    return "echo" in (event.get("scenario_id") or "").lower()


def _latency(event: dict) -> dict:
    return (event.get("signals") or {}).get("latency") or {}


def _evidence(event: dict) -> dict:
    """The real measured fields for one event; nothing derived, nothing guessed."""
    verdict = event.get("verdict") or {}
    meas = event.get("measurements") or {}
    lat = _latency(event)
    ev = {
        "expected_yield": event.get("expected_yield"),
        "did_yield": verdict.get("did_yield"),
        "seconds_to_yield": verdict.get("seconds_to_yield"),
        "talk_over_sec": verdict.get("talk_over_sec"),
        "reasons": list(verdict.get("reasons") or []),
    }
    for key in ("caller_onset_sec", "agent_talking_at_onset"):
        if key in meas:
            ev[key] = meas[key]
    for key in ("response_gap_sec", "premature_start_sec"):
        if lat.get(key) is not None:
            ev[key] = lat[key]
    return ev


# --- battery-level scans ----------------------------------------------------

def opposite_risk_coverage(events: list) -> dict:
    """Which opposite-risk fixture kinds this battery contains AND passes.

    Only scorable, passing events count: a fixture that itself fails cannot
    verify anything.
    """
    passing = [
        e for e in events
        if not _is_not_scorable(e) and (e.get("verdict") or {}).get("passed")
    ]
    return {
        "real_interruption_pass": any(e.get("expected_yield") for e in passing),
        "backchannel_hold_pass": any(not e.get("expected_yield") for e in passing),
        "measured_latency_pass": any(
            _latency(e).get("response_gap_sec") is not None for e in passing
        ),
    }


def _funnel_active(events: list) -> bool:
    """Mirror of the envelope funnel rule (fixmap.systemic_pointer): the battery
    misses a real interruption AND false-stops on a non-echo backchannel."""
    scorable = [e for e in events if not _is_not_scorable(e)]
    missed = any(
        _is_failing(e)
        and e.get("expected_yield")
        and not (e.get("verdict") or {}).get("did_yield")
        for e in scorable
    )
    false_stop = any(
        _is_failing(e)
        and not e.get("expected_yield")
        and (e.get("verdict") or {}).get("did_yield")
        and not _is_echo(e)
        for e in scorable
    )
    return missed and false_stop


# --- per-event diagnosis ----------------------------------------------------

def _diagnose_event(event: dict, coverage: dict, funnel: bool) -> Optional[dict]:
    """Diagnose ONE event. Returns None for a passing, scorable event."""
    base = {
        "event_id": event.get("event_id"),
        "scenario_id": event.get("scenario_id"),
    }

    if _is_not_scorable(event):
        reason = event.get("not_scorable_reason") or "; ".join(
            (event.get("verdict") or {}).get("reasons") or ["input problem"]
        )
        return dict(
            base,
            finding="not_scorable",
            evidence=_evidence(event),
            likely_layer=None,
            config_only_safe=False,
            notes=(
                "Input problem, never an agent failure: " + str(reason) + " "
                "This event is excluded from the battery decision and from any "
                "fix plan."
            ),
        )

    if not _is_failing(event):
        return None

    verdict = event.get("verdict") or {}
    expected_yield = bool(event.get("expected_yield"))
    did_yield = bool(verdict.get("did_yield"))
    joined = " ".join(str(r) for r in (verdict.get("reasons") or [])).lower()
    lat = _latency(event)

    if expected_yield and not did_yield:
        safe = not funnel
        notes = (
            "The caller took the floor and the agent never stopped within the "
            "search window."
        )
        if funnel:
            notes += (
                " This battery ALSO false-stops on a backchannel, so raising "
                "sensitivity to catch this interruption would make the "
                "backchannel case worse. One threshold cannot satisfy both."
            )
        else:
            notes += (
                " A one-step sensitivity increase is a config-layer candidate; "
                "the tradeoff is more false stops on short acknowledgements."
            )
        return dict(
            base,
            finding="missed_real_interruption",
            evidence=_evidence(event),
            likely_layer="interruption_detection",
            config_only_safe=safe,
            notes=notes,
        )

    if not expected_yield and did_yield:
        if _is_echo(event):
            return dict(
                base,
                finding="false_stop_on_backchannel",
                evidence=_evidence(event),
                likely_layer="unknown_root_cause",
                config_only_safe=False,
                notes=(
                    "The scenario is tagged as echo bleed: the agent most "
                    "likely yielded to its own TTS audio in the input track. "
                    "That is an audio-path problem (echo cancellation, channel "
                    "separation), not a turn-taking threshold. From one "
                    "recording, TTS bleed, transport routing, and VAD "
                    "behaviour are indistinguishable; fix and verify the audio "
                    "path before touching any threshold."
                ),
            )
        safe = not funnel
        notes = (
            "The caller only signalled 'I'm listening' but the agent gave up "
            "the floor."
        )
        if funnel:
            notes += (
                " This battery ALSO misses a real interruption, so raising the "
                "word threshold to hold through backchannels would make the "
                "missed interruption worse. One threshold cannot satisfy both."
            )
        else:
            notes += (
                " A one-step word-threshold increase is a config-layer "
                "candidate; the honest tradeoff is that the same threshold "
                "that ignores 'mhm' can also delay 'stop'."
            )
        return dict(
            base,
            finding="false_stop_on_backchannel",
            evidence=_evidence(event),
            likely_layer="interruption_detection",
            config_only_safe=safe,
            notes=notes,
        )

    # From here on the agent yielded as expected but out of bounds.
    if "slower" in joined or "time_to_yield" in joined or "yielded in" in joined:
        covered = bool(coverage.get("backchannel_hold_pass"))
        if covered:
            notes = (
                "The agent yielded, but slower than the bound. The battery "
                "contains a passing opposite-risk backchannel fixture, so a "
                "one-step latency reduction is verifiable against regression. "
                "Root cause is still inferred, not proven: TTS buffering, "
                "transport latency, and VAD smoothing are indistinguishable "
                "from one recording, so re-run both fixtures after any change."
            )
            return dict(
                base,
                finding="slow_yield",
                evidence=_evidence(event),
                likely_layer="endpointing",
                config_only_safe=True,
                notes=notes,
            )
        notes = (
            "The agent yielded, but slower than the bound. Root cause is "
            "ambiguous: TTS buffering, transport latency, and VAD smoothing "
            "are indistinguishable from one recording. No opposite-risk "
            "fixture passes in this battery, so a config change could not be "
            "verified against regression. Instrument first."
        )
        return dict(
            base,
            finding="slow_yield",
            evidence=_evidence(event),
            likely_layer="unknown_root_cause",
            config_only_safe=False,
            notes=notes,
        )

    if "talked over" in joined or "talk_over" in joined or "talk-over" in joined:
        return dict(
            base,
            finding="excess_talk_over",
            evidence=_evidence(event),
            likely_layer="interruption_detection",
            config_only_safe=True,
            notes=(
                "The agent eventually yielded but spoke over the caller for "
                "longer than the bound. A one-step tightening of the overlap "
                "or voice-window setting is a config-layer candidate; the "
                "tradeoff is clipping on ordinary listener noise."
            ),
        )

    if (
        "response gap" in joined
        or "response_gap" in joined
        or "premature" in joined
        or "dead-air" in joined
        or lat.get("premature_start_sec") not in (None, 0, 0.0)
    ):
        return dict(
            base,
            finding="endpointing_miss",
            evidence=_evidence(event),
            likely_layer="endpointing",
            config_only_safe=True,
            notes=(
                "The turn boundary was mishandled: measured dead air before "
                "the response, or a premature start into the caller's turn. "
                "This is the endpointing layer. A one-step wait adjustment is "
                "a config-layer candidate; the tradeoff runs in the opposite "
                "direction (shorter wait risks premature starts, longer wait "
                "risks dead air)."
            ),
        )

    # Defensive fallback for reason strings this version does not recognise:
    # never guess a layer.
    return dict(
        base,
        finding="slow_yield",
        evidence=_evidence(event),
        likely_layer="unknown_root_cause",
        config_only_safe=False,
        notes=(
            "The event failed its bounds but the failure reasons are not "
            "recognised by this version of the diagnoser. Treated "
            "conservatively: unknown root cause, no config change proposed."
        ),
    )


# --- battery decision -------------------------------------------------------

def _battery_block(events: list, diagnoses: list, coverage: dict, funnel: bool) -> dict:
    scorable = [e for e in events if not _is_not_scorable(e)]
    failing = [d for d in diagnoses if d["finding"] != "not_scorable"]
    n_not_scorable = len(events) - len(scorable)

    if funnel:
        finding, decision = "threshold_funnel", "do_not_tune_single_threshold"
        notes = (
            "This battery fails on both axes at once: it missed a genuine "
            "interruption AND it false-stopped on a backchannel. No single "
            "sensitivity threshold can fix both; turning it up for one makes "
            "the other worse. The fix class is discrimination (engagement "
            "control), not a threshold."
        )
    elif not failing:
        finding, decision = None, "no_failures"
        notes = "No scorable event failed. Nothing to tune."
    else:
        missing = sorted(
            {
                OPPOSITE_RISK[d["finding"]]["family"]
                for d in failing
                if d["config_only_safe"]
                and d["finding"] in OPPOSITE_RISK
                and not coverage.get(OPPOSITE_RISK[d["finding"]]["coverage_key"])
            }
        )
        if missing:
            finding, decision = None, "insufficient_coverage"
            notes = (
                "A config step maps to at least one failure, but the battery "
                "lacks the passing opposite-risk fixture that would verify it: "
                + "; ".join(missing)
                + ". Add that fixture before tuning."
            )
        elif any(d["config_only_safe"] for d in failing):
            finding, decision = None, "tune_one_step_with_verification"
            notes = (
                "At least one failure maps cleanly to a single setting and the "
                "battery contains the passing opposite-risk fixture to verify "
                "a one-step change. See hotato plan."
            )
        else:
            finding, decision = None, "needs_instrumentation"
            notes = (
                "The failures do not map safely to a single setting from this "
                "evidence. Instrument before tuning; no knob change is "
                "proposed."
            )

    n_failed = len(failing)
    return {
        "events": len(events),
        "failed": n_failed,
        "passed": len(scorable) - n_failed,
        "not_scorable": n_not_scorable,
        "finding": finding,
        "decision": decision,
        "opposite_risk_coverage": coverage,
        "notes": notes,
    }


# --- public API -------------------------------------------------------------

def diagnose_envelope(env: dict, source: Optional[str] = None) -> dict:
    """Diagnose every failing event in a hotato envelope plus the battery.

    Read-only and deterministic. Raises ValueError for anything that is not a
    hotato envelope (the CLI surfaces that as exit code 2).
    """
    env = _require_envelope(env)
    events = env["events"]
    coverage = opposite_risk_coverage(events)
    funnel = _funnel_active(events)

    diagnoses = []
    for event in events:
        d = _diagnose_event(event, coverage, funnel)
        if d is not None:
            diagnoses.append(d)

    return {
        "tool": "hotato",
        "kind": "diagnosis",
        "schema_version": "1",
        "source": source,
        "mode": env.get("mode"),
        "stack": env.get("stack", "generic"),
        "diagnoses": diagnoses,
        "battery": _battery_block(events, diagnoses, coverage, funnel),
    }


def render_text(diagnosis: dict) -> str:
    """The Level 0 advisory, human-readable. Honest tradeoffs always stated."""
    b = diagnosis["battery"]
    lines = [
        f"hotato diagnose [{diagnosis.get('mode')}] stack={diagnosis.get('stack')}",
        f"  {b['passed']}/{b['events']} events pass  (failed={b['failed']}, "
        f"not_scorable={b['not_scorable']})",
    ]
    for d in diagnosis["diagnoses"]:
        layer = d["likely_layer"] or "input"
        lines.append(
            f"  [{d['finding']}] {d['event_id']}  layer={layer} "
            f"config_only_safe={str(d['config_only_safe']).lower()}"
        )
        lines.append("    " + advisory_for(d["finding"], d["config_only_safe"]))
    lines.append(f"  battery decision: {b['decision']}")
    lines.append("    " + b["notes"])
    return "\n".join(lines)
