"""One versioned, typed parameter catalogue (plan section 9.3).

The knob values already live in two hand-maintained tables:

* ``hotato.fixplan._KNOBS`` -- the authoritative per ``(stack, intent)`` knob:
  the concrete vendor field, the normalized-model ``source`` it is inspected
  from, the step direction, one bounded step, the documented bounds, and a
  provenance ``basis`` string.
* ``hotato.fixplan._RISKS`` -- the opposite-risk each intent trades against.
* ``hotato.fixmap._KNOBS`` -- the richer human parameter description per stack.

This module does NOT restate any of those values. It IMPORTS them and
re-expresses them as one flat, typed, versioned catalogue so the fleet's
variant generator (``variants.py``) has a single lookup surface. Every entry
traces field-for-field back to those source tables; ``test_fleet_catalogue``
asserts that trace, so the catalogue can never invent a knob.

Pure stdlib. ``build_catalogue()`` is a pure function (no I/O, deterministic
order); ``lookup`` and ``catalogue_for`` are thin readers over it.
"""

from __future__ import annotations

import re
from typing import Optional

from ..fixmap import _KNOBS as _FIXMAP_KNOBS
from ..fixplan import _KNOBS as _FIXPLAN_KNOBS
from ..fixplan import _RISKS as _FIXPLAN_RISKS

# Bump only on a breaking change to the entry shape below.
SCHEMA_VERSION = "1"

# Deterministic intent ordering for a stable catalogue and stable variant sets.
_INTENT_ORDER = (
    "more_sensitive",
    "suppress_false_trigger",
    "faster_yield",
    "less_talk_over",
    "faster_endpointing",
)

# Whether hotato can apply/roll back a clone of the config programmatically.
# vapi and retell are hosted platforms with an agent-config API, so a clone can
# be applied and reverted by hotato. livekit and pipecat are source-config
# frameworks -- the knobs live in YOUR code, so hotato names the change but
# cannot apply or roll it back for you.
_CLONE_APPLICATION_SUPPORTED = {
    "vapi": True,
    "retell": True,
    "livekit": False,
    "pipecat": False,
}

# The PRIMARY (intent-achieving) directional consequence, in plain words. This
# is the "yield-target" hypothesis the plan's Expected block leads with. It is
# descriptive text, not a knob value -- the numeric knob (field/bounds/step)
# comes verbatim from fixplan.
_DIRECTIONAL_HYPOTHESIS = {
    "more_sensitive": "a real interruption registers sooner",
    "suppress_false_trigger": "a lone backchannel no longer takes the floor",
    "faster_yield": "the agent goes quiet sooner after the caller takes the floor",
    "less_talk_over": "overlapping speech ends the agent turn sooner",
    "faster_endpointing": "the turn boundary is detected sooner",
}

_DATE_RE = re.compile(r"\b(\d{4}-\d{2}-\d{2})\b")
# A real product/software version token (e.g. "v1.2" or "version 1.2.3"), NOT a
# documented value range like "0-0.5". None of the current basis strings carry
# one, so every entry is honestly "unverified" until a version is pinned.
_VERSION_RE = re.compile(r"\b(?:v|version)\s*(\d+\.\d+(?:\.\d+)?)\b", re.IGNORECASE)

_REQUIRED_FIELDS = (
    "stack",
    "intent",
    "supported_version_range",
    "canonical_semantic_family",
    "vendor_field_path",
    "data_type",
    "documented_bounds",
    "safe_discrete_step",
    "directional_effect",
    "expected_opposite_risk",
    "inspection_required",
    "clone_application_supported",
    "rollback_supported",
    "adjacent_steps_safe",
    "documentation_provenance",
    "last_verified_date",
)


def _last_verified_date(basis: str) -> Optional[str]:
    """The last-verified date parsed from the provenance basis, or None when
    the basis carries no date (livekit/pipecat working ranges)."""
    m = _DATE_RE.search(basis or "")
    return m.group(1) if m else None


def _supported_version_range(basis: str) -> str:
    """Best-effort software version range from the basis string, else the
    honest ``"unverified"``. Deliberately ignores documented VALUE ranges
    (those are ``documented_bounds``); only a real version token counts."""
    m = _VERSION_RE.search(basis or "")
    return m.group(1) if m else "unverified"


def _data_type(step) -> str:
    """int- vs float-valued field, inferred from the step's type: an integer
    step (numWords / min_words) is an integer field; a fractional step
    (seconds) is a float field."""
    if isinstance(step, bool):  # guard: bool is an int subclass
        return "float"
    return "int" if isinstance(step, int) else "float"


def _build_entry(stack: str, intent: str, knob: dict) -> dict:
    section, key = knob["source"]
    basis = knob["basis"]
    step = knob["step"]
    last_verified = _last_verified_date(basis)
    # A family is safe for extra adjacent steps only where the vendor documents
    # a hard range (a dated docs basis). livekit/pipecat carry conservative,
    # version-dependent working ranges, so we do NOT auto-expand past the single
    # lower/higher step there.
    documented = last_verified is not None
    clone_ok = _CLONE_APPLICATION_SUPPORTED.get(stack, False)
    fixmap_entry = (_FIXMAP_KNOBS.get(stack) or {}).get(intent) or {}
    return {
        "stack": stack,
        "intent": intent,
        # Best-effort software version range (see basis); "unverified" today.
        "supported_version_range": _supported_version_range(basis),
        # Canonical semantic family = the normalized-model source fixplan reads.
        "canonical_semantic_family": {"section": section, "key": key},
        # Concrete vendor field path / constructor argument.
        "vendor_field_path": knob["field"],
        "data_type": _data_type(step),
        "documented_bounds": list(knob["bounds"]),
        "safe_discrete_step": step,
        "directional_effect": {
            "direction": knob["direction"],  # value direction achieving the intent
            "hypothesis": _DIRECTIONAL_HYPOTHESIS[intent],
        },
        # Opposite-risk effect, imported verbatim from fixplan._RISKS.
        "expected_opposite_risk": _FIXPLAN_RISKS[intent],
        # Every proposal needs the inspected current value to compute from/to.
        "inspection_required": True,
        "clone_application_supported": clone_ok,
        # hotato-performed rollback is only possible where it can apply a clone.
        "rollback_supported": clone_ok,
        "adjacent_steps_safe": documented,
        "documentation_provenance": basis,
        "last_verified_date": last_verified,
        # Optional richer vendor description, re-expressed from fixmap where it
        # exists for this (stack, intent); None otherwise (no fabrication).
        "vendor_parameter_reference": fixmap_entry.get("parameter"),
    }


def build_catalogue() -> dict:
    """Build the whole catalogue: ``{"schema_version", "entries": [...]}``.

    Pure and deterministic: stacks sorted, intents in ``_INTENT_ORDER``. Every
    entry is derived from the imported ``fixplan``/``fixmap`` tables; nothing is
    hard-coded here."""
    entries = []
    for stack in sorted(_FIXPLAN_KNOBS):
        stack_knobs = _FIXPLAN_KNOBS[stack]
        for intent in _INTENT_ORDER:
            knob = stack_knobs.get(intent)
            if knob is not None:
                entries.append(_build_entry(stack, intent, knob))
    return {"schema_version": SCHEMA_VERSION, "entries": entries}


def lookup(stack: str, intent: str) -> Optional[dict]:
    """The single catalogue entry for ``(stack, intent)``, or None."""
    s = (stack or "").strip().lower()
    i = (intent or "").strip().lower()
    for entry in build_catalogue()["entries"]:
        if entry["stack"] == s and entry["intent"] == i:
            return entry
    return None


def catalogue_for(stack: str) -> list:
    """All catalogue entries for one stack, in canonical intent order."""
    s = (stack or "").strip().lower()
    return [e for e in build_catalogue()["entries"] if e["stack"] == s]
