"""hotato.capability-requirement.v1 routing (delta D3).

One engagement-control timing threshold can fail in both directions, so a lone
"the agent talked over me" report does not, on its own, say which mechanism to
fix. This router reads SUPPLIED interaction labels (hotato.interaction-label.v1,
see :mod:`hotato.interaction_label`) on a paired addressee-control battery and
routes to the narrowest capability the paired evidence actually supports, or to
no recommendation at all.

It never infers addressee or intent, never reads audio, and emits a
provider-neutral verdict: capability id, evidence references, acceptance tests,
the input-health causes it checked and cleared, and an optional neutral
contract URI. It names no implementation, product, or vendor.

Truth table (from the delta contract):

  * addressed floor bid missed + non-addressed speech false trigger
        -> utterance_addressee_gate (paired_discrimination_failure)
  * addressed floor bid missed + addressed feedback false trigger
        -> turn_intent_discriminator (paired_discrimination_failure)
  * either event lacks a trusted addressee/intent label
        -> engagement_control (insufficient_labels), missing axes listed
  * echo, non-speech ambient, invalid channel map, or an unscorable input
        -> None (a config / input-health finding, never a capability)
  * a lone event with no opposite-risk pair
        -> None (no paired discrimination claim)

Pure, deterministic, stdlib only. Fail-loud on a malformed event.
"""
from __future__ import annotations

from collections.abc import Mapping
from typing import Iterable, List, Optional, Union

from . import interaction_label as _il

KIND = "hotato.capability-requirement.v1"
FIX_CLASS = "engagement-control"
FIXTURE_KIND = "hotato.routing-fixture.v1"

# A neutral Hotato specification URI a caller MAY attach to a verdict. It points
# at the capability-requirement contract itself and names no implementation
# provider. The router omits any URI by default.
DEFAULT_CONTRACT_URI = "https://hotato.dev/spec/capability-requirement/v1"

# The three input-health flags an event carries, plus scorability, are the only
# routing-relevant health signals. Kept in canonical order for deterministic
# ``excluded_causes`` output.
_INPUT_HEALTH_FLAGS = ("self_echo", "non_speech_ambient", "invalid_channel_map")
_ALL_CLEARED_CAUSES = ("self_echo", "non_speech_ambient",
                       "invalid_channel_map", "unscorable_input")

_MISSING_AXIS_ORDER = ("addressed_to_agent", "floor_intent",
                       "label_authority", "opposite_risk_fixture")

_REQUIRED_EVENT_KEYS = (
    "event_id", "battery_id", "configuration_id", "scorable",
    "expected_behavior", "observed_behavior", "failure",
    "input_health", "interaction",
)


class RoutingInputError(ValueError):
    """A routing input is not a well-formed routing-fixture.v1 event."""


def _as_events(events: Union[Mapping, Iterable[Mapping]]) -> List[Mapping]:
    if isinstance(events, Mapping):
        return [events]
    try:
        return list(events)
    except TypeError as exc:  # pragma: no cover - defensive
        raise RoutingInputError("events must be a mapping or an iterable") from exc


def _norm_event(ev: Mapping) -> dict:
    """Validate the routing shape enough to route on it; fail loud otherwise.

    The interaction label is read through :func:`hotato.interaction_label.of`,
    which validates it. Reading a supplied label is not inference.
    """
    if not isinstance(ev, Mapping):
        raise RoutingInputError(
            "routing event must be a mapping, got " + type(ev).__name__
        )
    for key in _REQUIRED_EVENT_KEYS:
        if key not in ev:
            raise RoutingInputError("routing event missing field " + repr(key))

    health = ev["input_health"]
    if not isinstance(health, Mapping):
        raise RoutingInputError("input_health must be a mapping")
    for flag in _INPUT_HEALTH_FLAGS:
        val = health.get(flag, False)
        if not isinstance(val, bool):
            raise RoutingInputError(
                "input_health." + flag + " must be a boolean, got " + repr(val)
            )

    if not isinstance(ev["scorable"], bool):
        raise RoutingInputError("scorable must be a boolean")

    label = _il.coerce(ev["interaction"])

    return {
        "event_id": ev["event_id"],
        "battery_id": ev["battery_id"],
        "configuration_id": ev["configuration_id"],
        "scorable": ev["scorable"],
        "expected_behavior": ev["expected_behavior"],
        "observed_behavior": ev["observed_behavior"],
        "self_echo": bool(health.get("self_echo", False)),
        "non_speech_ambient": bool(health.get("non_speech_ambient", False)),
        "invalid_channel_map": bool(health.get("invalid_channel_map", False)),
        "label": label,
    }


def _is_addressed_miss(ev: Mapping) -> bool:
    """The addressed floor bid that the agent failed to yield to."""
    label = ev["label"]
    return (
        ev["scorable"] is True
        and label["speech_presence"] == "speech"
        and label["addressed_to_agent"] is True
        and label["floor_intent"] == "take"
        and ev["expected_behavior"] == "yield"
        and ev["observed_behavior"] == "hold"
    )


def _is_false_trigger(ev: Mapping) -> bool:
    """An opposite-risk event: expected to hold, observed to yield."""
    return (
        ev["scorable"] is True
        and ev["expected_behavior"] == "hold"
        and ev["observed_behavior"] == "yield"
    )


def _input_health_clean(ev: Mapping) -> bool:
    return not (ev["self_echo"] or ev["non_speech_ambient"]
                or ev["invalid_channel_map"])


def _pair_scorable_and_clean(a: Mapping, b: Mapping) -> bool:
    return (
        a["scorable"] is True and b["scorable"] is True
        and _input_health_clean(a) and _input_health_clean(b)
    )


def _missing_axes(a: Mapping, b: Mapping) -> List[str]:
    """Trusted addressee/intent axes absent on EITHER paired event."""
    la, lb = a["label"], b["label"]
    missing = set()
    for label in (la, lb):
        if not _il.addressee_known(label):
            missing.add("addressed_to_agent")
        if not _il.intent_known(label):
            missing.add("floor_intent")
        if not _il.is_trusted(label):
            missing.add("label_authority")
    return [axis for axis in _MISSING_AXIS_ORDER if axis in missing]


def _requirement(*, required_capability, trigger, evidence_refs,
                 acceptance_tests, missing_evidence, excluded_causes,
                 contract_uri) -> dict:
    req = {
        "kind": KIND,
        "fix_class": FIX_CLASS,
        "required_capability": required_capability,
        "trigger": trigger,
        "evidence_refs": list(evidence_refs),
        "acceptance_tests": list(acceptance_tests),
        "excluded_causes": list(excluded_causes),
        "missing_evidence": list(missing_evidence),
    }
    if contract_uri is not None:
        req["contract_uri"] = contract_uri
    return req


def route_capability(
    events: Union[Mapping, Iterable[Mapping]],
    *,
    contract_uri: Optional[str] = None,
) -> Optional[dict]:
    """Route a battery of labelled events to a neutral capability requirement.

    ``events`` is a routing-fixture.v1 event, or an iterable of them (a paired
    A/B battery). Returns a hotato.capability-requirement.v1 mapping, or
    ``None`` when the evidence supports no capability (a config / input-health
    finding, or no paired discrimination claim).

    ``contract_uri`` optionally attaches a neutral Hotato spec URI to the
    verdict; pass :data:`DEFAULT_CONTRACT_URI` to opt in. The verdict never
    carries any implementation, product, or vendor identifier.
    """
    evs = [_norm_event(e) for e in _as_events(events)]

    # A lone event carries no opposite-risk pair: no paired discrimination claim.
    if len(evs) < 2:
        return None

    a = next((e for e in evs if _is_addressed_miss(e)), None)
    if a is None:
        return None
    b = next((e for e in evs if e is not a and _is_false_trigger(e)), None)
    if b is None:
        return None

    # Echo, non-speech ambient, invalid channel map, or an unscorable input
    # explain the trigger as an input/config problem, never a capability.
    if not _pair_scorable_and_clean(a, b):
        return None

    evidence_refs = [a["event_id"], b["event_id"]]
    excluded = list(_ALL_CLEARED_CAUSES)

    # Insufficient trusted labels: no implementation is resolved.
    missing = _missing_axes(a, b)
    if missing:
        return _requirement(
            required_capability="engagement_control",
            trigger="insufficient_labels",
            evidence_refs=evidence_refs,
            acceptance_tests=["collect_trusted_addressee_label"],
            missing_evidence=missing,
            excluded_causes=excluded,
            contract_uri=contract_uri,
        )

    lb = b["label"]

    # Non-addressed speech that reached the agent: an addressee gate.
    if lb["addressed_to_agent"] is False and lb["floor_intent"] == "none":
        return _requirement(
            required_capability="utterance_addressee_gate",
            trigger="paired_discrimination_failure",
            evidence_refs=evidence_refs,
            acceptance_tests=[
                "addressed_interruption_reaches_agent",
                "non_addressed_speech_does_not_reach_agent",
                "opposite_risk_fixture_does_not_regress",
            ],
            missing_evidence=[],
            excluded_causes=excluded,
            contract_uri=contract_uri,
        )

    # An addressed backchannel that took the floor: an intent discriminator.
    if lb["addressed_to_agent"] is True and lb["floor_intent"] == "feedback":
        return _requirement(
            required_capability="turn_intent_discriminator",
            trigger="paired_discrimination_failure",
            evidence_refs=evidence_refs,
            acceptance_tests=[
                "addressed_feedback_does_not_take_floor",
                "opposite_risk_fixture_does_not_regress",
            ],
            missing_evidence=[],
            excluded_causes=excluded,
            contract_uri=contract_uri,
        )

    # Trusted, known labels that match no documented discrimination pattern:
    # withhold a recommendation rather than over-claim one.
    return None
