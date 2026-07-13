"""Optional, backwards-compatible interaction labels (hotato.interaction-label.v1).

An interaction label is SUPPLIED metadata about one event: whether there was
speech, whether it was addressed to the agent, what the caller's floor intent
was, and who supplied the label. It is never derived. Hotato does not infer
addressee or turn intent from timing, energy, transcript, or a model verdict:
every field here comes from a human, a trusted source, or an explicitly marked
fixture. An event with no label reads as all-unknown, so existing data stays
valid.

The schema lives at ``schema/interaction-label.v1.json``. This module is
stdlib-only (the core carries no third-party dependency); the JSON Schema is
cross-checked against this validator in the tests.
"""
from __future__ import annotations

from typing import Any, Dict, Optional

KIND = "hotato.interaction-label.v1"

SPEECH_PRESENCE = ("speech", "non-speech", "unknown")
FLOOR_INTENT = ("take", "feedback", "none", "unknown")
LABEL_AUTHORITY = ("human", "trusted-source", "fixture", "unknown")

# The authorities that can stand behind a routing decision (not "unknown").
TRUSTED_AUTHORITIES = ("human", "trusted-source", "fixture")

# The all-unknown label an event with no supplied metadata reads as.
UNKNOWN: Dict[str, Any] = {
    "kind": KIND,
    "speech_presence": "unknown",
    "addressed_to_agent": None,
    "floor_intent": "unknown",
    "label_authority": "unknown",
    "label_ref": None,
}

_ALLOWED_KEYS = set(UNKNOWN.keys())


class InteractionLabelError(ValueError):
    """The interaction label is malformed or breaks a semantic rule."""


def validate(doc: Any) -> Dict[str, Any]:
    """Validate an interaction-label dict against hotato.interaction-label.v1
    and its two conditional rules. Returns the dict; raises
    InteractionLabelError otherwise. Pure Python, no jsonschema at runtime."""
    if not isinstance(doc, dict):
        raise InteractionLabelError("interaction label must be an object")
    extra = set(doc) - _ALLOWED_KEYS
    if extra:
        raise InteractionLabelError(f"unknown field(s): {sorted(extra)}")
    for req in ("kind", "speech_presence", "addressed_to_agent",
                "floor_intent", "label_authority"):
        if req not in doc:
            raise InteractionLabelError(f"missing required field: {req}")
    if doc["kind"] != KIND:
        raise InteractionLabelError(f"kind must be {KIND!r}, got {doc['kind']!r}")
    if doc["speech_presence"] not in SPEECH_PRESENCE:
        raise InteractionLabelError(
            f"speech_presence must be one of {SPEECH_PRESENCE}")
    if doc["addressed_to_agent"] not in (True, False, None):
        raise InteractionLabelError(
            "addressed_to_agent must be true, false, or null")
    if doc["floor_intent"] not in FLOOR_INTENT:
        raise InteractionLabelError(f"floor_intent must be one of {FLOOR_INTENT}")
    if doc["label_authority"] not in LABEL_AUTHORITY:
        raise InteractionLabelError(
            f"label_authority must be one of {LABEL_AUTHORITY}")
    ref = doc.get("label_ref")
    if ref is not None and (not isinstance(ref, str) or len(ref) > 512):
        raise InteractionLabelError(
            "label_ref must be null or a string of at most 512 characters")
    # Conditional rule 1: non-speech carries no addressee and takes no floor.
    if doc["speech_presence"] == "non-speech":
        if doc["addressed_to_agent"] is not None:
            raise InteractionLabelError(
                "non-speech: addressed_to_agent must be null")
        if doc["floor_intent"] != "none":
            raise InteractionLabelError("non-speech: floor_intent must be 'none'")
    return doc


def build(
    *,
    speech_presence: str = "unknown",
    addressed_to_agent: Optional[bool] = None,
    floor_intent: str = "unknown",
    label_authority: str = "unknown",
    label_ref: Optional[str] = None,
) -> Dict[str, Any]:
    """Construct an interaction label from EXPLICITLY SUPPLIED values.

    There is no signal input: this function cannot receive audio, timing,
    energy, a transcript, or a model verdict, so it cannot infer addressee or
    intent. Two supplied-data policies are applied before validation:

      * ``non-speech`` forces ``addressed_to_agent=None`` and
        ``floor_intent="none"`` (a non-speech event addresses no one and takes
        no floor);
      * ``label_authority="unknown"`` degrades every judged field to
        unknown/null: without a named authority there is no basis to assert
        speech presence, addressee, or intent.
    """
    if label_authority == "unknown":
        speech_presence, addressed_to_agent, floor_intent = "unknown", None, "unknown"
    if speech_presence == "non-speech":
        addressed_to_agent, floor_intent = None, "none"
    doc = {
        "kind": KIND,
        "speech_presence": speech_presence,
        "addressed_to_agent": addressed_to_agent,
        "floor_intent": floor_intent,
        "label_authority": label_authority,
        "label_ref": label_ref,
    }
    return validate(doc)


def coerce(mapping: Any) -> Dict[str, Any]:
    """Adapt a RAW label mapping into a valid interaction label: absent axes
    become unknown/null, and a non-speech row with blank addressee/intent is
    pinned to the schema's non-speech shape. An explicitly contradicting value
    (for example non-speech WITH a floor intent) still fails through validate,
    never silently. Unlike build(), this does not degrade a supplied trusted
    label, so a router can see the label as given.

    of() reads a label attached to a carrier record (under "interaction_label");
    coerce() adapts a bare label mapping (an event's "interaction" object)."""
    if not isinstance(mapping, dict):
        raise InteractionLabelError(
            f"cannot adapt {type(mapping).__name__} to an interaction label")
    sp = mapping.get("speech_presence", "unknown")
    addressed = mapping.get("addressed_to_agent", None)
    fi = mapping.get("floor_intent", "unknown")
    if sp == "non-speech":
        if "addressed_to_agent" not in mapping:
            addressed = None
        if "floor_intent" not in mapping:
            fi = "none"
    return validate({
        "kind": KIND,
        "speech_presence": sp,
        "addressed_to_agent": addressed,
        "floor_intent": fi,
        "label_authority": mapping.get("label_authority", "unknown"),
        "label_ref": mapping.get("label_ref", None),
    })


def is_trusted(label: dict) -> bool:
    """True when the label authority can stand behind a routing decision."""
    return label.get("label_authority") in TRUSTED_AUTHORITIES


def addressee_known(label: dict) -> bool:
    """True when addressed_to_agent carries a definite true/false."""
    return isinstance(label.get("addressed_to_agent"), bool)


def intent_known(label: dict) -> bool:
    """True when floor_intent carries a definite (non-unknown) value."""
    return label.get("floor_intent") in ("take", "feedback", "none")


def of(carrier: Optional[dict]) -> Dict[str, Any]:
    """Read the interaction label attached to a record/event, or the all-unknown
    label when absent. Backwards-compatible: any prior artifact reads as
    unknown, never inferred."""
    if isinstance(carrier, dict):
        lab = carrier.get("interaction_label")
        if isinstance(lab, dict):
            return validate(dict(lab))
    return dict(UNKNOWN)


def attach(carrier: dict, label: dict) -> dict:
    """Attach a validated interaction label to a record/event dict in place and
    return it. Additive: it adds one optional ``interaction_label`` key and
    changes nothing else."""
    carrier["interaction_label"] = validate(dict(label))
    return carrier


__all__ = [
    "KIND", "SPEECH_PRESENCE", "FLOOR_INTENT", "LABEL_AUTHORITY", "UNKNOWN",
    "InteractionLabelError", "validate", "build", "coerce", "of", "attach",
    "TRUSTED_AUTHORITIES", "is_trusted", "addressee_known", "intent_known",
]
