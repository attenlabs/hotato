"""``hotato explain``: root-cause-by-layer analysis, composed from what already
exists (``diagnose`` + ``fixplan``'s policy tables, a contract's own trust and
policy blocks, and an attached voice trace when present). This module adds no
new scoring engine: it reframes ``diagnose_envelope``'s per-event diagnoses and
battery decision (and, for a contract bundle, the same measured fields the
bundle already carries) into a layer-general attribution record and, when the
evidence cannot support picking one cause, a refusal instead of a guess.

Three input shapes, auto-detected from the one argument:

* a finished run envelope (``hotato run --format json > result.json``);
* a sweep/analyze candidate ref (``hotato-sweep.json#N`` or
  ``hotato-sweep.json#call_id:N``), the same ref
  :func:`hotato.fixture.parse_candidate_ref` resolves. A candidate carries no
  human label, so it can never be attributed to a layer; explain always
  REFUSES it and prints the exact promote command for both labels;
* a contract bundle directory (``<id>.hotato/``). ``contract.json`` does not
  carry the raw scorer ``reasons`` text a run envelope's event does, so a
  false-stop-on-hold contract is attributed only when its own
  ``source.candidate_kind`` disambiguates it (an echo-tagged candidate);
  otherwise explain REFUSES rather than guess between backchannel
  discrimination, ambient noise, and echo bleed. A ``did_yield`` failure that
  still violates its policy bound is attributed by comparing the MEASURED
  ``seconds_to_yield`` / ``talk_over_sec`` against the contract's OWN
  ``policy.pass_conditions`` -- a bound comparison, not a guess. When a voice
  trace is attached (``traces/voice_trace.jsonl``), its findings
  (:func:`hotato.trace._findings_lines`) are folded into the evidence.

Every attribution record uses the layer-general shape
(``failure_layer``/``type``/``confidence``/``fixability``/``opposite_risk``)
so the schema can grow new layers (asr, tool, policy, latency, handoff, ...)
without a version bump; only the ``turn_taking`` layer is populated in this
build. ``fixability`` is one of ``safe_to_patch`` (the SAME policy gate
``hotato plan`` enforces already passes: config-only-safe, a mapped knob, and
a passing opposite-risk fixture in the battery), ``do_not_patch`` (the
threshold-funnel refusal: the battery fails on both discrimination axes at
once), ``needs_human`` (an audio-path problem, e.g. echo bleed), or
``insufficient_evidence`` (a mapped knob with no opposite-risk fixture yet, or
a genuinely ambiguous root cause).

Hotato does not infer intent and does not prove authorization, identity,
compliance, or policy safety. Every attribution here is evidence-based, never
a proof of root cause.
"""

from __future__ import annotations

from .errors import open_regular as _open_regular

import json
import os
from datetime import datetime, timezone
from typing import Optional

from . import analyze as _analyze
from . import contract as _contract
from . import diagnose as _diagnose
from . import fixture as _fixture
from . import report as _report
from . import trace as _trace
from . import trust as _trust
from .diagnose import OPPOSITE_RISK
from .fixplan import ENGAGEMENT_CONTROL_FIX

__all__ = [
    "SCHEMA",
    "FAILURE_LAYER",
    "explain",
    "render_text",
    "render_html",
]

SCHEMA = "hotato.explain.v1"
FAILURE_LAYER = "turn_taking"

_INPUT_KINDS = ("run_envelope", "sweep_candidate", "contract_bundle")

_NO_TRACE_UNKNOWN = (
    "no client-side playout trace attached to this contract (hotato trace "
    "ingest/attach); TTS-cancellation lag, transport latency, and VAD "
    "smoothing remain indistinguishable from timing alone"
)

_CHIP_COLOR = {
    "safe_to_patch": "green",
    "do_not_patch": "red",
    "needs_human": "ember",
    "insufficient_evidence": "ember",
}


def _now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


# --- input dispatch ----------------------------------------------------------

def explain(source: str) -> dict:
    """Explain ONE finished result. ``source`` is a run envelope path, a
    ``FILE#N`` / ``FILE#CALL:N`` sweep/analyze candidate ref, or a contract
    bundle directory. Pure and read-only: no argument here ever mutates a
    platform or a file. Raises ``ValueError``/``OSError`` (exit 2 at the CLI)
    for anything unusable, with the same honest reason the underlying reader
    (``diagnose``/``fixture``/``contract``) already gives."""
    source = (source or "").strip()
    if not source:
        raise ValueError(
            "provide a result to explain: a run envelope (hotato run "
            "--format json > result.json), a sweep/analyze candidate ref "
            "(hotato-sweep.json#N), or a contract bundle directory "
            "(<id>.hotato)"
        )
    if os.path.isdir(source) or source.rstrip("/\\").endswith(
        _contract.BUNDLE_SUFFIX
    ):
        parts = _explain_contract_bundle(source)
    elif "#" in source:
        parts = _explain_candidate(source)
    else:
        parts = _explain_run_envelope(source)
    return {"schema": SCHEMA, "kind": "explanation", "created_at": _now_iso(), **parts}


# --- run envelope --------------------------------------------------------------

def _load_run_envelope(path: str) -> dict:
    with _open_regular(path, "r", encoding="utf-8") as fh:
        try:
            env = json.load(fh)
        except json.JSONDecodeError as exc:
            raise ValueError(f"{path!r} is not JSON ({exc})") from exc
    if not (
        isinstance(env, dict)
        and env.get("tool") == "hotato"
        and env.get("kind") is None
        and isinstance(env.get("events"), list)
    ):
        raise ValueError(
            f"{path!r} is not a hotato run envelope JSON (frame dumps, "
            "benchmark results, and compare results are not run envelopes). "
            "Save one with: hotato run --suite barge-in --format json > "
            "result.json"
        )
    return env


def _measured_bits(evidence: dict) -> list:
    return [
        f"{k}={v}" for k, v in (evidence or {}).items()
        if v is not None and k != "reasons"
    ]


def _classify_diagnosis(d: dict, *, coverage: dict, funnel: bool) -> dict:
    """One diagnose.py finding -> either an attribution record or a refusal
    record (discriminated by the internal ``_refuse`` key, popped by the
    caller). Reuses the SAME evidence, likely_layer, and coverage the battery
    already computed; adds no new measurement."""
    base = {"event_id": d.get("event_id"), "scenario_id": d.get("scenario_id")}
    finding = d["finding"]
    notes = d.get("notes") or ""
    measured = _measured_bits(d.get("evidence") or {})
    evidence_for = ([notes] if notes else []) + (
        ["measured: " + ", ".join(measured)] if measured else []
    )

    if finding == "not_scorable":
        return dict(
            base, _refuse=True,
            reason="input problem, not an agent failure: " + notes,
            evidence_for=evidence_for,
            unknowns=[
                "the recording, onset, or channel mapping is unusable for "
                "this event; no layer can be attributed to unusable input",
            ],
            safe_next_action=(
                "fix the recording/onset/channel mapping and re-run: hotato "
                "run --suite barge-in --format json > result.json"
            ),
        )

    layer = d.get("likely_layer")
    if layer == "unknown_root_cause":
        if finding == "false_stop_on_backchannel":
            causes = "TTS/audio bleed (echo), transport routing, and VAD smoothing"
            hint = (
                "confirm the agent's TTS output is not mixed into the input "
                "track (keep caller and agent on separate channels end to "
                "end), enable echo cancellation, then re-capture"
            )
        else:
            causes = "TTS buffering, transport latency, and VAD smoothing"
            hint = (
                "log the timestamp the framework issues its stop-speaking "
                "command and the timestamp the audio actually goes quiet; "
                "the difference separates TTS buffering from detection "
                "latency"
            )
        return dict(
            base, _refuse=True,
            reason=(
                f"evidence cannot support one root cause for this {finding} "
                f"event: {causes} are indistinguishable from one recording"
            ),
            evidence_for=evidence_for,
            unknowns=[
                "no client-side playout trace or instrumentation is "
                "attached to separate the candidate causes",
            ],
            safe_next_action=hint,
        )

    # Layer is known (interruption_detection or endpointing).
    if funnel and finding in (
        "missed_real_interruption", "false_stop_on_backchannel",
    ):
        return dict(
            base, _refuse=False,
            failure_layer=FAILURE_LAYER, type=finding, turn_taking_layer=layer,
            confidence="high", fixability="do_not_patch",
            opposite_risk=(
                "any single-threshold change trades the two failing axes "
                "against each other"
            ),
            evidence_for=evidence_for,
            evidence_against=[
                "this battery ALSO fails on the opposite axis (see the "
                "threshold_funnel attribution); a single-threshold theory "
                "does not explain a safe fix here",
            ],
            unknowns=[],
            safe_next_action=(
                "do not tune a single threshold; recommend engagement-"
                "control (adaptive interruption handling or a "
                "backchannel-aware classifier), not calibration"
            ),
        )

    risk_info = OPPOSITE_RISK.get(finding)
    opposite_risk = risk_info["why"] if risk_info else None
    unknowns = [
        "the live platform config was not inspected (no --stack/target was "
        "given to hotato explain); the exact from/to value is unknown",
    ]
    evidence_against = []
    if risk_info and coverage.get(risk_info["coverage_key"]):
        fixability, confidence = "safe_to_patch", "high"
        safe_next_action = (
            "hotato plan result.json --stack YOUR_STACK "
            "--assistant-id/--agent-id/--config TARGET for a bounded "
            "one-step change, then verify with hotato compare"
        )
    elif risk_info:
        fixability, confidence = "insufficient_evidence", "medium"
        evidence_against.append(
            f"no passing {risk_info['family']} in this battery yet, so a "
            "config change here could not be verified against regression"
        )
        safe_next_action = f"add {risk_info['family']} before tuning"
    else:
        fixability, confidence = "insufficient_evidence", "medium"
        safe_next_action = "gather the missing opposite-risk fixture before tuning"

    return dict(
        base, _refuse=False,
        failure_layer=FAILURE_LAYER, type=finding, turn_taking_layer=layer,
        confidence=confidence, fixability=fixability,
        opposite_risk=opposite_risk,
        evidence_for=evidence_for, evidence_against=evidence_against,
        unknowns=unknowns, safe_next_action=safe_next_action,
    )


def _funnel_attribution(diagnoses: list) -> dict:
    ids = sorted(
        str(d.get("event_id")) for d in diagnoses
        if d["finding"] in ("missed_real_interruption", "false_stop_on_backchannel")
        and d.get("likely_layer") == "interruption_detection"
    )
    return {
        "event_id": None, "scenario_id": None,
        "failure_layer": FAILURE_LAYER, "type": "threshold_funnel",
        "turn_taking_layer": "interruption_detection",
        "confidence": "high", "fixability": "do_not_patch",
        "opposite_risk": (
            "any single-threshold change trades the two failing axes "
            "against each other"
        ),
        "evidence_for": [
            "this battery missed a genuine interruption AND false-stopped "
            "on a backchannel: events " + ", ".join(ids),
        ],
        "evidence_against": [],
        "unknowns": [],
        "safe_next_action": (
            "recommend engagement-control (examples: "
            + "; ".join(ENGAGEMENT_CONTROL_FIX["examples"])
            + "); verify against BOTH a should-yield and a should-hold "
            "fixture before shipping any change"
        ),
    }


def _overall_next_action(attributions: list, refusals: list) -> str:
    funnel = next((a for a in attributions if a["type"] == "threshold_funnel"), None)
    if funnel:
        return funnel["safe_next_action"]
    safe = next((a for a in attributions if a["fixability"] == "safe_to_patch"), None)
    if safe:
        return safe["safe_next_action"]
    if attributions:
        return attributions[0]["safe_next_action"]
    if refusals:
        return refusals[0]["safe_next_action"]
    return "no attributable failure in this result; nothing to fix"


def _explain_run_envelope(path: str) -> dict:
    env = _load_run_envelope(path)
    diagnosis = _diagnose.diagnose_envelope(env, source=path)
    battery = diagnosis["battery"]
    coverage = battery.get("opposite_risk_coverage") or {}
    funnel = battery.get("decision") == "do_not_tune_single_threshold"

    attributions, refusals = [], []
    for d in diagnosis["diagnoses"]:
        rec = _classify_diagnosis(d, coverage=coverage, funnel=funnel)
        (refusals if rec.pop("_refuse") else attributions).append(rec)
    if funnel:
        attributions.append(_funnel_attribution(diagnosis["diagnoses"]))

    top_unknowns = [
        "no client-side playout trace attached (voice traces are only "
        "available on contract bundles created with hotato contract create "
        "+ hotato trace attach)",
    ]

    return {
        "input_kind": "run_envelope",
        "source": path,
        "stack": env.get("stack"),
        "battery": {
            "events": battery["events"], "failed": battery["failed"],
            "passed": battery["passed"], "not_scorable": battery["not_scorable"],
            "decision": battery["decision"], "notes": battery["notes"],
        },
        "attributions": attributions,
        "refusals": refusals,
        "unknowns": top_unknowns,
        "safe_next_action": _overall_next_action(attributions, refusals),
        "notes": (
            "Evidence-based attribution, not proof of root cause. "
            f"{battery['passed']}/{battery['events']} events pass."
        ),
    }


# --- sweep/analyze candidate ref: no human label = always a refusal ------------

def _explain_candidate(ref: str) -> dict:
    path, call, number = _fixture.parse_candidate_ref(ref)
    doc = _fixture._load_result(path)
    cand = _fixture._resolve_candidate(doc, path=path, call=call, number=number)
    headline = _analyze._headline(cand)
    detail = _analyze._detail_text(cand)
    promote_yield = _analyze._promote_command(path, number, cand, "yield")
    promote_hold = _analyze._promote_command(path, number, cand, "hold")
    evidence_for = [x for x in (
        detail, (f"headline: {headline}" if headline else None),
    ) if x]

    refusal = {
        "event_id": None, "scenario_id": None,
        "reason": (
            "no human label for this candidate yet: expected behavior "
            f"(yield vs hold) has not been confirmed. kind={cand.get('kind')}"
        ),
        "evidence_for": evidence_for,
        "unknowns": [
            "expected behavior (yield vs hold) has not been labeled by a "
            "human for this candidate",
            "this is a candidate moment surfaced by pattern matching, not a "
            "scored event; it carries no pass/fail verdict",
        ],
        "safe_next_action": (
            f"label it and promote: {promote_yield}  (or, if this is a "
            f"backchannel/noise the agent should have ignored: "
            f"{promote_hold})"
        ),
    }
    return {
        "input_kind": "sweep_candidate",
        "source": ref,
        "stack": doc.get("stack"),
        "battery": None,
        "attributions": [],
        "refusals": [refusal],
        "unknowns": [
            "no human label for this candidate; root cause cannot be "
            "attributed to a layer until it is promoted with an expected "
            "behavior",
        ],
        "safe_next_action": refusal["safe_next_action"],
        "notes": f"candidate kind={cand.get('kind')}; {detail}" if detail else None,
    }


# --- contract bundle -----------------------------------------------------------

def _trace_evidence(bundle_dir: str, top_unknowns: list) -> list:
    trace_path = os.path.join(bundle_dir, "traces", "voice_trace.jsonl")
    if not os.path.isfile(trace_path):
        top_unknowns.append(_NO_TRACE_UNKNOWN)
        return []
    try:
        vt = _trace.load_voice_trace_jsonl(trace_path)
    except ValueError as exc:
        top_unknowns.append(f"attached voice trace could not be read: {exc}")
        return []
    return _trace._findings_lines(vt.get("spans") or [])


def _explain_contract_bundle(path: str) -> dict:
    contract = _contract.inspect_contract(path)
    bundle_dir = path if os.path.isdir(path) else (
        os.path.dirname(os.path.abspath(path)) or "."
    )
    m = contract["measurement"]
    lbl = contract["label"]
    pol = (contract.get("policy") or {}).get("pass_conditions") or {}
    trust = contract.get("trust") or {}
    cid = contract.get("id")
    stack = (contract.get("source") or {}).get("stack")

    top_unknowns = []
    trace_evidence = _trace_evidence(bundle_dir, top_unknowns)
    status = trust.get("status")
    if status not in (_trust.SAFE_RECOMMENDATION, None) or trust.get("warnings"):
        top_unknowns.append(
            f"input-health (trust) status={status}; warnings: "
            + ("; ".join(trust.get("warnings") or []) or "none listed")
        )

    if not m.get("scorable"):
        reason = "not scorable: " + str(m.get("not_scorable_reason") or "unknown reason")
        refusal = {
            "event_id": cid, "scenario_id": None, "reason": reason,
            "evidence_for": [], "unknowns": [
                "no timing measurement exists for this contract",
            ],
            "safe_next_action": (
                "recreate the contract from a scorable moment: hotato "
                "contract create --from-candidate <ref> --expect "
                f"{lbl.get('expected_behavior')} --id {cid}-2 --out contracts"
            ),
        }
        return {
            "input_kind": "contract_bundle", "source": path, "stack": stack,
            "battery": None, "attributions": [], "refusals": [refusal],
            "unknowns": top_unknowns, "safe_next_action": refusal["safe_next_action"],
            "notes": reason,
        }

    expect = lbl.get("expected_behavior")
    did_yield = m.get("did_yield")
    passed = bool(m.get("passed"))
    base_evt = {"event_id": cid, "scenario_id": None}
    measured_line = "measured: " + ", ".join([
        f"expected={expect}", f"did_yield={did_yield}",
        f"seconds_to_yield={m.get('seconds_to_yield')}",
        f"talk_over_sec={m.get('talk_over_sec')}",
    ])

    attributions, refusals = [], []
    if passed:
        battery = {
            "events": 1, "failed": 0, "passed": 1, "not_scorable": 0,
            "decision": "no_failures",
            "notes": "the contract's re-scored timing passes its policy",
        }
    else:
        battery = {
            "events": 1, "failed": 1, "passed": 0, "not_scorable": 0,
            "decision": "attributed", "notes": None,
        }
        if expect == "yield" and not did_yield:
            attributions.append(dict(
                base_evt, failure_layer=FAILURE_LAYER,
                type="missed_real_interruption",
                turn_taking_layer="interruption_detection",
                confidence="high", fixability="insufficient_evidence",
                opposite_risk=OPPOSITE_RISK["missed_real_interruption"]["why"],
                evidence_for=[measured_line] + trace_evidence,
                evidence_against=[],
                unknowns=[
                    "this bundle contains only one moment; there is no "
                    "opposite-risk companion contract/fixture in it to "
                    "verify a sensitivity change safely",
                ],
                safe_next_action=(
                    "add a passing should-hold backchannel fixture/contract "
                    "for the opposite-risk check, then hotato plan the run "
                    "envelope this moment came from"
                ),
            ))
        elif expect == "hold" and did_yield:
            kind = (contract.get("source") or {}).get("candidate_kind")
            if kind == "echo_correlated_activity":
                refusals.append(dict(
                    base_evt,
                    reason=(
                        "this contract's source candidate is tagged "
                        "echo_correlated_activity: the agent most likely "
                        "yielded to its own TTS bleeding into the input "
                        "track, an audio-path problem, not a turn-taking "
                        "threshold"
                    ),
                    evidence_for=[measured_line] + trace_evidence,
                    unknowns=[
                        "TTS bleed, transport routing, and VAD behaviour are "
                        "indistinguishable from one recording",
                    ],
                    safe_next_action=(
                        "confirm the agent's TTS output is not mixed into "
                        "the input track (separate channels end to end), "
                        "enable echo cancellation, and re-capture before "
                        "touching any threshold"
                    ),
                ))
            else:
                refusals.append(dict(
                    base_evt,
                    reason=(
                        "evidence cannot support one root cause from this "
                        "contract alone: a false stop on hold could be a "
                        "genuine backchannel discrimination miss, ambient "
                        "non-speech noise, or echo bleed, and contract.json "
                        "does not carry the echo/ambient signal a full run "
                        "envelope's diagnose has"
                    ),
                    evidence_for=[measured_line] + trace_evidence,
                    unknowns=[
                        "echo/ambient-noise discrimination is not available "
                        "from a contract bundle alone",
                    ],
                    safe_next_action=(
                        "re-run the original recording through hotato run "
                        "--dump-frames or hotato diagnose on the full run "
                        "envelope this contract came from to rule out echo "
                        "bleed and ambient noise"
                    ),
                ))
        else:
            over_time = (
                pol.get("max_time_to_yield_sec") is not None
                and m.get("seconds_to_yield") is not None
                and m["seconds_to_yield"] > pol["max_time_to_yield_sec"]
            )
            over_talk = (
                pol.get("max_talk_over_sec") is not None
                and m.get("talk_over_sec") is not None
                and m["talk_over_sec"] > pol["max_talk_over_sec"]
            )
            if over_time:
                attributions.append(dict(
                    base_evt, failure_layer=FAILURE_LAYER, type="slow_yield",
                    turn_taking_layer="endpointing",
                    confidence="medium", fixability="insufficient_evidence",
                    opposite_risk=OPPOSITE_RISK["slow_yield"]["why"],
                    evidence_for=[
                        f"measured seconds_to_yield={m['seconds_to_yield']} "
                        "exceeds policy max_time_to_yield_sec="
                        f"{pol['max_time_to_yield_sec']}",
                    ] + trace_evidence,
                    evidence_against=[
                        "TTS buffering, transport latency, and VAD smoothing "
                        "are indistinguishable from timing alone without an "
                        "attached trace",
                    ],
                    unknowns=[
                        "this bundle contains only one moment; there is no "
                        "opposite-risk companion to verify a latency change "
                        "safely",
                    ],
                    safe_next_action=(
                        "attach a voice trace (hotato trace ingest/attach) "
                        "before tuning, or instrument the yield path"
                    ),
                ))
            if over_talk:
                attributions.append(dict(
                    base_evt, failure_layer=FAILURE_LAYER,
                    type="excess_talk_over",
                    turn_taking_layer="interruption_detection",
                    confidence="medium", fixability="insufficient_evidence",
                    opposite_risk=OPPOSITE_RISK["excess_talk_over"]["why"],
                    evidence_for=[
                        f"measured talk_over_sec={m['talk_over_sec']} "
                        "exceeds policy max_talk_over_sec="
                        f"{pol['max_talk_over_sec']}",
                    ] + trace_evidence,
                    evidence_against=[],
                    unknowns=[
                        "this bundle contains only one moment; there is no "
                        "opposite-risk companion to verify a change safely",
                    ],
                    safe_next_action=(
                        "add a passing should-hold backchannel fixture/"
                        "contract, then plan a one-step tightening of the "
                        "overlap/voice-window setting"
                    ),
                ))
            if not over_time and not over_talk:
                refusals.append(dict(
                    base_evt,
                    reason=(
                        "the contract failed its policy but the measured "
                        "values do not exceed either documented bound; the "
                        "cause cannot be attributed from contract.json alone"
                    ),
                    evidence_for=[measured_line] + trace_evidence,
                    unknowns=[
                        "the exact failing condition is not recoverable "
                        "from this bundle's fields",
                    ],
                    safe_next_action=(
                        "re-run hotato contract verify --format json and "
                        "inspect the full measurement, or re-diagnose the "
                        "original run envelope"
                    ),
                ))

    return {
        "input_kind": "contract_bundle", "source": path, "stack": stack,
        "battery": battery, "attributions": attributions, "refusals": refusals,
        "unknowns": top_unknowns,
        "safe_next_action": _overall_next_action(attributions, refusals),
        "notes": f"contract {cid}: {'passes' if passed else 'fails'} its policy",
    }


# --- rendering -----------------------------------------------------------------

def render_text(explanation: dict) -> str:
    lines = [
        f"hotato explain [{explanation.get('input_kind')}] "
        f"source={explanation.get('source')}",
    ]
    b = explanation.get("battery")
    if b:
        lines.append(
            f"  {b.get('passed')}/{b.get('events')} events pass "
            f"(failed={b.get('failed')}, not_scorable={b.get('not_scorable')}) "
            f"battery={b.get('decision')}"
        )
    for a in explanation.get("attributions") or []:
        evt = a.get("event_id") or "battery"
        sub = a.get("turn_taking_layer") or "-"
        lines.append(
            f"  ATTRIBUTION [{a['type']}] {evt}  "
            f"layer={a['failure_layer']}/{sub}  confidence={a['confidence']}  "
            f"fixability={a['fixability']}"
        )
        for f in a.get("evidence_for") or []:
            lines.append(f"    for:     {f}")
        for f in a.get("evidence_against") or []:
            lines.append(f"    against: {f}")
        if a.get("opposite_risk"):
            lines.append(f"    opposite-risk: {a['opposite_risk']}")
        for u in a.get("unknowns") or []:
            lines.append(f"    unknown: {u}")
        lines.append(f"    next:    {a['safe_next_action']}")
    for r in explanation.get("refusals") or []:
        evt = r.get("event_id") or "unlabeled"
        lines.append(f"  REFUSE {evt}: {r['reason']}")
        for f in r.get("evidence_for") or []:
            lines.append(f"    for:     {f}")
        for u in r.get("unknowns") or []:
            lines.append(f"    unknown: {u}")
        lines.append(f"    next:    {r['safe_next_action']}")
    for u in explanation.get("unknowns") or []:
        lines.append(f"  unknown: {u}")
    if not (explanation.get("attributions") or explanation.get("refusals")):
        lines.append("  nothing to explain (no attributable failure)")
    lines.append(f"  next: {explanation.get('safe_next_action')}")
    return "\n".join(lines)


def render_html(explanation: dict) -> str:
    esc = _report._esc
    parts = [
        '<div class="wrap"><header class="top"><div class="logo"></div>'
        '<div><div class="h1">hotato explain</div>'
        f'<div class="tagline">{esc(explanation.get("input_kind"))} &middot; '
        f'{esc(explanation.get("source") or "")}</div></div></header><main>'
    ]
    b = explanation.get("battery")
    if b:
        parts.append(
            '<div class="summary"><div class="bignum">'
            f'{esc(b.get("passed"))}/{esc(b.get("events"))}</div>'
            '<div class="subtle">events pass &middot; failed='
            f'{esc(b.get("failed"))} &middot; not_scorable='
            f'{esc(b.get("not_scorable"))} &middot; battery='
            f'{esc(b.get("decision"))}</div></div>'
        )
    for a in explanation.get("attributions") or []:
        color = _report._C.get(_CHIP_COLOR.get(a["fixability"], "ember"))
        sub = f'/{esc(a["turn_taking_layer"])}' if a.get("turn_taking_layer") else ""
        parts.append(
            '<div class="card"><div class="chead"><div>'
            f'<div class="ctitle">{esc(a["type"])}</div>'
            '<div class="cmeta">'
            f'<span class="tag">{esc(a.get("event_id") or "battery")}</span>'
            f'<span class="tag">layer={esc(a["failure_layer"])}{sub}</span>'
            f'<span class="tag">confidence={esc(a["confidence"])}</span>'
            '</div></div>'
            f'<div class="chip" style="background:{color}">'
            f'{esc(a["fixability"].upper())}</div></div>'
        )
        if a.get("opposite_risk"):
            parts.append(
                '<div class="fix"><b>opposite risk</b><div class="fixd">'
                f'{esc(a["opposite_risk"])}</div></div>'
            )
        if a.get("evidence_for"):
            parts.append(
                '<ul class="reasons">'
                + "".join(f"<li>{esc(x)}</li>" for x in a["evidence_for"])
                + "</ul>"
            )
        if a.get("evidence_against"):
            parts.append(
                '<div class="does">against: '
                + esc("; ".join(a["evidence_against"])) + '</div>'
            )
        if a.get("unknowns"):
            parts.append(
                '<div class="does">unknowns: '
                + esc("; ".join(a["unknowns"])) + '</div>'
            )
        parts.append(
            '<div class="foot"><div class="fline"><b>next:</b> '
            f'{esc(a["safe_next_action"])}</div></div></div>'
        )
    for r in explanation.get("refusals") or []:
        parts.append(
            '<div class="card"><div class="chead"><div>'
            '<div class="ctitle">REFUSED</div><div class="cmeta">'
            f'<span class="tag">{esc(r.get("event_id") or "unlabeled")}</span>'
            '</div></div></div>'
            f'<div class="reasons">{esc(r["reason"])}</div>'
        )
        if r.get("unknowns"):
            parts.append(
                '<div class="does">unknowns: '
                + esc("; ".join(r["unknowns"])) + '</div>'
            )
        parts.append(
            '<div class="foot"><div class="fline"><b>next:</b> '
            f'{esc(r["safe_next_action"])}</div></div></div>'
        )
    if explanation.get("unknowns"):
        parts.append(
            '<div class="foot"><div class="fline"><b>unknowns:</b> '
            + esc("; ".join(explanation["unknowns"])) + '</div></div>'
        )
    parts.append(
        '<div class="foot"><div class="fline"><b>next:</b> '
        f'{esc(explanation.get("safe_next_action"))}</div></div>'
    )
    parts.append("</main></div>")
    body = "".join(parts)
    title = f"hotato explain: {esc(explanation.get('input_kind'))}"
    return (
        "<!doctype html>\n<html lang=\"en\"><head><meta charset=\"utf-8\">"
        '<meta name="viewport" content="width=device-width, initial-scale=1">'
        f"<title>{title}</title>"
        f"<style>{_report._CSS}</style></head><body>{body}</body></html>\n"
    )
