"""Level 3 of the guarded fix ladder: turn a fix plan into a paste-ready patch.

``hotato patch <fixplan.json>`` reads a plan (schema ``hotato.fixplan.v1``,
written by ``hotato plan``) and renders its abstract ``{field, from, to}``
recommendation into a LITERAL, paste-ready artifact for the target platform:

* Vapi / Retell (REST config): a JSON merge-patch body plus a ready ``curl``
  against the platform's real config-update endpoint, using the exact field
  names from the plan (which come from fixmap's verified knob catalogue).
* LiveKit / Pipecat (config-in-source): there is NO config-update REST call to
  hit, so patch emits the exact source edit instead -- the constructor kwarg and
  the literal value to set -- never a fabricated endpoint.
* generic / unknown stack: no concrete field exists, so patch names the knob
  FAMILY and asks for a stack target; it emits no literal body.

HARD honesty rules, enforced here:

* patch PRODUCES the change; it NEVER applies it. Nothing in this module makes a
  network call or mutates any platform. Every artifact says so, and
  ``applies_change`` is pinned to false.
* patch only handles the config-fixable classes. For a plan whose decision is
  ``do_not_tune_single_threshold`` (the genuine both-axes / threshold-funnel
  case) it emits NO config patch: it prints the vendor-neutral,
  numbers-free engagement-control pointer instead (the same pointer the plan and
  fixmap carry -- it names the problem class and the KIND of fix, no product, no
  digits, so it can never read as an upsell).
* every other non-propose decision (diagnostic_checklist, insufficient_coverage,
  at_documented_bound, no_change) yields no patch either, with the honest reason
  pointing back at the plan; the engagement-control pointer is NOT shown for
  those (it fires only on the real both-axes case).

The config-update endpoints are read-only DOCUMENTATION here (patch never calls
them). Field-name and endpoint basis, verified 2026-07-06:

* Vapi:   PATCH https://api.vapi.ai/assistant/{id} (Bearer VAPI_API_KEY);
          startSpeakingPlan / stopSpeakingPlan fields as in
          docs.vapi.ai/api-reference/assistants/update.
* Retell: PATCH https://api.retellai.com/update-agent/{agent_id} (Bearer
          RETELL_API_KEY); responsiveness / interruption_sensitivity as in
          docs.retellai.com/api-references/update-agent.
"""

from __future__ import annotations

import json
from typing import Optional

from . import fixmap as _fixmap

SCHEMA_ID = "hotato.patch.v1"
_PLAN_SCHEMA_ID = "hotato.fixplan.v1"

# The honest banner attached to every artifact: producing a diff is not applying
# it. Stated in the machine output AND the text render.
_PRODUCES_NOT_APPLIES = (
    "hotato patch PRODUCES this change; it never applies it. Review it, apply it "
    "yourself in your own stack, then re-capture the failing moment and prove the "
    "movement with hotato verify."
)

# Config-update endpoints, as DOCUMENTATION only. patch renders a curl a human
# runs; it never issues the request itself. url_template's {id} is filled from
# the plan's inspected target when present, else a clear placeholder.
_REST_ENDPOINTS = {
    "vapi": {
        "method": "PATCH",
        "url_template": "https://api.vapi.ai/assistant/{id}",
        "id_key": "assistant_id",
        "id_placeholder": "<assistant-id>",
        "auth": "Bearer $VAPI_API_KEY",
        "provenance": (
            "PATCH /assistant/{id} verified against "
            "docs.vapi.ai/api-reference/assistants/update, 2026-07-06"
        ),
    },
    "retell": {
        "method": "PATCH",
        "url_template": "https://api.retellai.com/update-agent/{id}",
        "id_key": "agent_id",
        "id_placeholder": "<agent-id>",
        "auth": "Bearer $RETELL_API_KEY",
        "provenance": (
            "PATCH /update-agent/{agent_id} verified against "
            "docs.retellai.com/api-references/update-agent, 2026-07-06"
        ),
    },
}

# LiveKit / Pipecat turn-taking config lives in the agent SOURCE, not behind a
# REST call. Map the plan's stack-specific field to the constructor + kwarg a
# human edits. Pipecat fields already arrive as "Constructor.kwarg"; LiveKit's
# nested "turn_handling.<group>.<kwarg>" is mapped explicitly.
_LIVEKIT_SOURCE = {
    "turn_handling.interruption.min_words": ("InterruptionOptions", "min_words"),
    "turn_handling.interruption.min_duration": ("InterruptionOptions", "min_duration"),
    "turn_handling.endpointing.min_delay": ("EndpointingOptions", "min_delay"),
}

_SOURCE_EDIT_NOTE = {
    "livekit": (
        "LiveKit turn-taking config lives in your agent source "
        "(AgentSession(turn_handling=TurnHandlingOptions(...))), not behind a "
        "config-update API. There is no endpoint to curl; set the kwarg below "
        "and redeploy. Verify the option name against your installed "
        "livekit-agents version."
    ),
    "pipecat": (
        "Pipecat turn-taking config lives in your agent source (the turn "
        "strategies and VADParams you pass into the pipeline), not behind a "
        "config-update API. There is no endpoint to curl; set the kwarg below "
        "and redeploy. Verify the option name against your installed pipecat "
        "version."
    ),
}


def _validate_plan(plan: dict) -> dict:
    if not isinstance(plan, dict) or plan.get("schema") != _PLAN_SCHEMA_ID:
        raise ValueError(
            "not a hotato fix plan (schema hotato.fixplan.v1). Write one first: "
            "hotato plan result.json --out fixplan.json"
        )
    return plan


def _nest(keys: list, value) -> dict:
    """Build a nested merge-patch dict from a dotted field path."""
    body: dict = {}
    cur = body
    for k in keys[:-1]:
        cur[k] = {}
        cur = cur[k]
    cur[keys[-1]] = value
    return body


def _curl(endpoint: dict, url: str, body: dict) -> str:
    payload = json.dumps(body, sort_keys=True)
    return (
        f"curl -X {endpoint['method']} {url} \\\n"
        f"  -H \"Authorization: {endpoint['auth']}\" \\\n"
        "  -H \"Content-Type: application/json\" \\\n"
        f"  -d '{payload}'"
    )


def _rest_artifact(stack: str, change: dict, target: dict) -> dict:
    endpoint = _REST_ENDPOINTS[stack]
    ident = (target or {}).get(endpoint["id_key"]) or endpoint["id_placeholder"]
    url = endpoint["url_template"].format(id=ident)
    to = change.get("to")
    field = change["field"]
    artifact = {
        "apply_method": "rest-merge-patch",
        "endpoint": {
            "method": endpoint["method"],
            "url": url,
            "auth": endpoint["auth"],
            "provenance": endpoint["provenance"],
            "id_resolved": (target or {}).get(endpoint["id_key"]) is not None,
        },
    }
    if to is None:
        # The plan could not read the current value, so there is no literal to
        # set. Emit the body template + direction, never a fabricated number.
        artifact["merge_patch"] = None
        artifact["curl"] = None
        artifact["note"] = (
            f"The plan has no concrete target value for {field} "
            f"(direction: {change['direction']}, bounds {change['bounds']}). "
            "Inspect the live config to resolve the current value, then re-plan: "
            f"hotato inspect --stack {stack} ...  Nothing to paste yet."
        )
        return artifact
    body = _nest(field.split("."), to)
    artifact["merge_patch"] = body
    artifact["curl"] = _curl(endpoint, url, body)
    artifact["note"] = (
        f"Sets {field} to {to} (one bounded step {change['direction']} from "
        f"{change['from']}). This is a config-update request you run; hotato "
        "does not run it."
    )
    return artifact


def _source_edit_target(stack: str, field: str):
    if stack == "livekit":
        return _LIVEKIT_SOURCE.get(field)
    if stack == "pipecat":
        if "." in field:
            ctor, kwarg = field.rsplit(".", 1)
            return (ctor, kwarg)
    return None


def _source_artifact(stack: str, change: dict) -> dict:
    field = change["field"]
    to = change.get("to")
    mapped = _source_edit_target(stack, field)
    artifact = {
        "apply_method": "source-edit",
        "endpoint": None,
        "merge_patch": None,
        "curl": None,
    }
    if mapped is None:
        # Unknown field shape for this stack: never guess a constructor.
        artifact["source_edit"] = None
        artifact["note"] = (
            f"{field} does not map to a known constructor kwarg for {stack}; "
            "set it by hand in your agent source. " + _SOURCE_EDIT_NOTE[stack]
        )
        return artifact
    ctor, kwarg = mapped
    if to is None:
        artifact["source_edit"] = {
            "constructor": ctor, "kwarg": kwarg, "value": None,
            "direction": change["direction"],
        }
        artifact["note"] = (
            f"No concrete target value: move {ctor}({kwarg}=...) one step "
            f"{change['direction']} (bounds {change['bounds']}). Inspect the live "
            f"config for the current value first. " + _SOURCE_EDIT_NOTE[stack]
        )
        return artifact
    artifact["source_edit"] = {
        "constructor": ctor,
        "kwarg": kwarg,
        "value": to,
        "snippet": f"{ctor}({kwarg}={to})",
    }
    artifact["note"] = (
        f"Set {ctor}({kwarg}={to}) in your agent config (one bounded step "
        f"{change['direction']} from {change['from']}), then redeploy. "
        + _SOURCE_EDIT_NOTE[stack]
    )
    return artifact


def _generic_artifact(change: dict) -> dict:
    """No stack target: the plan named a knob FAMILY only. Emit that family and
    ask for a concrete stack; no literal body can be produced honestly."""
    return {
        "apply_method": "none",
        "endpoint": None,
        "merge_patch": None,
        "curl": None,
        "source_edit": None,
        "note": (
            f"No stack target, so the field is the generic knob family "
            f"'{change['field']}' (direction: {change['direction']}). A literal, "
            "paste-ready patch needs a concrete stack. Re-plan against your "
            "stack: hotato plan result.json --stack vapi|retell|livekit|pipecat "
            "with its target flag."
        ),
    }


def _pointer_only(plan: dict, *, reason: str, saa_pointer: bool) -> dict:
    """A patch result that emits no config patch. When ``saa_pointer`` is true
    (the genuine both-axes case) it carries the vendor-neutral, numbers-free
    engagement-control pointer; otherwise it just states why there is no patch."""
    out = {
        "tool": "hotato",
        "kind": "patch",
        "schema_version": "1",
        "source_plan": None,
        "stack": plan["target"].get("stack"),
        "plan_finding": plan.get("finding"),
        "plan_decision": plan.get("decision"),
        "config_patchable": False,
        "applies_change": False,
        "change": None,
        "artifact": None,
        "reason": reason,
        "saa_pointer": None,
        "honest": _PRODUCES_NOT_APPLIES,
    }
    if saa_pointer:
        rec = plan.get("recommended_fix") or {}
        pointer = dict(_fixmap.ENGAGEMENT_CONTROL_POINTER)
        pointer["class"] = "engagement-control"
        pointer["examples"] = list(
            rec.get("examples") or _fixplan_examples()
        )
        out["saa_pointer"] = pointer
        out["next"] = [
            "This is the both-axes case: no single config threshold fixes it, so "
            "no config patch is emitted. See the engagement-control pointer.",
            "Verify any change with a battery, not one clip: hotato verify "
            "--before before/ --after after/",
        ]
    else:
        out["next"] = [
            f"No config patch for decision '{plan.get('decision')}'. {reason}",
        ]
    return out


def _fixplan_examples() -> list:
    # Lazy import avoids a hard cycle at module import time.
    from .fixplan import ENGAGEMENT_CONTROL_FIX
    return list(ENGAGEMENT_CONTROL_FIX["examples"])


def build_patch(plan: dict, *, source: Optional[str] = None) -> dict:
    """Render a fix plan into a paste-ready patch artifact (or an honest
    no-patch result). Pure: reads the plan, returns the patch dict. Never
    touches the network or any platform config."""
    _validate_plan(plan)
    decision = plan.get("decision")
    stack = (plan["target"].get("stack") or "generic").strip().lower()

    # The genuine both-axes / threshold-funnel case: no config patch, the
    # vendor-neutral engagement-control pointer instead.
    if decision == "do_not_tune_single_threshold":
        out = _pointer_only(
            plan,
            reason=(
                "The plan refused single-threshold tuning: the battery misses a "
                "real interruption AND false-stops on a backchannel, so no one "
                "config value fixes both. No config patch is produced."
            ),
            saa_pointer=True,
        )
        out["source_plan"] = source
        return out

    # Every other non-propose decision: no patch, honest pointer back at the plan.
    if decision != "propose_one_step" or not plan.get("changes"):
        reasons = {
            "diagnostic_checklist": (
                "The plan is an instrumentation checklist, not a knob change "
                "(the layer at fault cannot be identified from this evidence). "
                "Work the plan's checklist first."
            ),
            "insufficient_coverage": (
                "The plan needs a passing opposite-risk fixture before any "
                "config step can be verified. Add "
                f"{plan.get('required_fixture_family', 'that fixture family')} "
                "first."
            ),
            "at_documented_bound": (
                "The inspected value is already at the documented bound; there "
                "is no further single-step config change on this axis."
            ),
            "no_change": "No scorable event failed; there is nothing to patch.",
        }
        out = _pointer_only(
            plan,
            reason=reasons.get(
                decision, f"decision '{decision}' produces no config patch."
            ),
            saa_pointer=False,
        )
        out["source_plan"] = source
        return out

    # propose_one_step: render the literal, paste-ready artifact per platform.
    change = plan["changes"][0]
    if stack in _REST_ENDPOINTS:
        artifact = _rest_artifact(stack, change, plan["target"])
    elif stack in ("livekit", "pipecat"):
        artifact = _source_artifact(stack, change)
    else:
        artifact = _generic_artifact(change)

    return {
        "tool": "hotato",
        "kind": "patch",
        "schema_version": "1",
        "source_plan": source,
        "stack": stack,
        "plan_finding": plan.get("finding"),
        "plan_decision": decision,
        "config_patchable": True,
        "applies_change": False,
        "change": {
            "field": change["field"],
            "from": change.get("from"),
            "to": change.get("to"),
            "direction": change["direction"],
            "bounds": change.get("bounds"),
            "risk": change.get("risk"),
        },
        "artifact": artifact,
        "saa_pointer": None,
        "next": [
            "review the artifact above and apply it yourself; hotato never "
            "applies it",
            "re-capture the failing moment through your stack after applying it",
            "prove the movement across the battery: hotato verify --before "
            "before/ --after after/",
        ],
        "honest": _PRODUCES_NOT_APPLIES,
    }


def render_text(patch: dict) -> str:
    lines = [
        f"hotato patch [{patch['stack']}] finding={patch.get('plan_finding')} "
        f"decision={patch.get('plan_decision')}",
        f"  config_patchable={str(patch['config_patchable']).lower()}  "
        f"applies_change={str(patch['applies_change']).lower()} "
        "(hotato produces the change; it never applies it)",
    ]
    change = patch.get("change")
    if change:
        frm = "?" if change["from"] is None else change["from"]
        to = "?" if change["to"] is None else change["to"]
        lines.append(
            f"  change: {change['field']}  {frm} -> {to}  "
            f"({change['direction']}, bounds {change['bounds']})"
        )
        if change.get("risk"):
            lines.append(f"    risk: {change['risk']}")
    art = patch.get("artifact")
    if art:
        lines.append(f"  apply method: {art['apply_method']}")
        ep = art.get("endpoint")
        if ep:
            lines.append(f"    endpoint: {ep['method']} {ep['url']}")
            lines.append(f"    basis: {ep['provenance']}")
        if art.get("merge_patch") is not None:
            lines.append("    merge-patch body: "
                         + json.dumps(art["merge_patch"], sort_keys=True))
        if art.get("curl"):
            lines.append("    curl (you run this; hotato does not):")
            for cl in art["curl"].split("\n"):
                lines.append(f"      {cl}")
        se = art.get("source_edit")
        if se:
            if se.get("snippet"):
                lines.append(f"    source edit: {se['snippet']}")
            else:
                lines.append(
                    f"    source edit: {se['constructor']}({se['kwarg']}=...) "
                    f"one step {se.get('direction')}"
                )
        if art.get("note"):
            lines.append(f"    note: {art['note']}")
    if patch.get("reason"):
        lines.append(f"  no patch: {patch['reason']}")
    ptr = patch.get("saa_pointer")
    if ptr:
        lines.append(f"  recommended fix class: {ptr['class']}")
        lines.append(f"    {ptr['what']}")
        for ex in ptr.get("examples", []):
            lines.append(f"    - {ex}")
    for cmd in patch.get("next") or []:
        lines.append(f"  next: {cmd}")
    lines.append(f"  {patch['honest']}")
    return "\n".join(lines)
