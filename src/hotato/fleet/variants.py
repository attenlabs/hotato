"""Bounded, rule-based variant generation (plan section 9.4).

The first experiment engine does NOT do unconstrained optimization. Given a
``(stack, intent)`` and the inspected current config, it emits a small, bounded
set of candidate clones off the typed catalogue (``catalogue.py``):

* a baseline (no change),
* one step LOWER and one step HIGHER (concrete values = current -/+ the
  catalogue step, clamped to the documented bounds; when the current value is
  unknown, a bounded RELATIVE delta instead of a magic absolute),
* at most two ADJACENT steps (the second ring, current -/+ 2*step) -- only when
  the catalogue marks the semantic family safe for it,
* at most one two-PARAMETER combination, and only after single-parameter
  variants exist,
* capped at ``max_variants`` (default 6). If the set would exceed the cap it
  drops the two-parameter combo first, then adjacent steps -- and NEVER silently
  exceeds: the baseline carries a ``dropped`` ledger of what was removed.

Every variant carries an ``expected`` block stating its directional
consequences BEFORE execution (the plan's "Expected:" block), derived from the
catalogue's directional + opposite-risk fields, and an ``observed`` field left
None for the caller to fill after a trial.

Deterministic: no clock, no randomness. Slugs and ordering are a pure function
of the inputs. Pure stdlib.
"""

from __future__ import annotations

import re
from typing import Optional

from ..fixplan import _current_value
from . import catalogue as _catalogue

# --- plain-language effect phrasing (derived, not knob values) ---------------
#
# The PRIMARY yield-target consequence of moving a knob in its intent-achieving
# direction, and the reverse consequence of moving the other way. hold_guards
# (the opposite-risk axis) comes straight from the catalogue's
# ``expected_opposite_risk`` (fixplan._RISKS), so it is never paraphrased here.

_YIELD_EFFECT = {
    "more_sensitive": "faster (catches genuine interruptions sooner)",
    "suppress_false_trigger": "steadier holds (lone backchannels ignored)",
    "faster_yield": "faster yield after the caller takes the floor",
    "less_talk_over": "less overlap before the agent yields",
    "faster_endpointing": "faster turn-boundary detection",
}
_YIELD_EFFECT_REVERSE = {
    "more_sensitive": "slower (genuine interruptions caught later)",
    "suppress_false_trigger": "more sensitive (backchannels may take the floor)",
    "faster_yield": "slower yield after the caller takes the floor",
    "less_talk_over": "more overlap before the agent yields",
    "faster_endpointing": "slower turn-boundary detection",
}
_LOW_SIGNAL = {
    "more_sensitive": "possible increased noise/ambient sensitivity",
    "suppress_false_trigger": "possible reduced sensitivity to quiet real interruptions",
    "faster_yield": "possible increased sensitivity to line noise",
    "less_talk_over": "possible clipping on benign overlaps",
    "faster_endpointing": "possible premature starts in low-signal pauses",
}

_NO_CHANGE = "no change (baseline reference)"


def _slug(*parts) -> str:
    """A deterministic, filesystem-safe slug. No randomness, no timestamp."""
    raw = "-".join(str(p) for p in parts).lower()
    raw = re.sub(r"[^a-z0-9]+", "-", raw)
    return raw.strip("-")


def _achieving_move(entry: dict) -> str:
    """The numeric move ("lower"/"higher") that achieves the entry's intent."""
    return "lower" if entry["directional_effect"]["direction"] == "decrease" else "higher"


def _read_current(entry: dict, current_config) -> Optional[float]:
    """The inspected current value for this entry's field, or None.

    Prefers the normalized-model read fixplan uses (``turn_taking``/``raw`` by
    the canonical source), then falls back to a flat dict keyed by the vendor
    field path or the semantic key."""
    if not current_config:
        return None
    fam = entry["canonical_semantic_family"]
    val = _current_value(current_config, (fam["section"], fam["key"]))
    if val is not None:
        return val
    for key in (entry["vendor_field_path"], fam["key"]):
        v = (current_config or {}).get(key)
        if isinstance(v, bool):
            continue
        if isinstance(v, (int, float)):
            return v
    return None


def _stepped_value(current, step, bounds, move: str, n: int):
    """``current`` moved ``n`` steps in ``move`` direction, clamped to bounds.

    Returns None when the clamp leaves no movement in the requested direction
    (already at/over the bound) -- so a variant is NEVER proposed below the low
    bound or above the high bound."""
    lo, hi = bounds
    delta = step * n
    to = current - delta if move == "lower" else current + delta
    to = round(to, 3)
    if lo is not None:
        to = max(lo, to)
    if hi is not None:
        to = min(hi, to)
    moved = to < current if move == "lower" else to > current
    return to if moved else None


def _expected_block(entry: dict, move: Optional[str]) -> dict:
    """The pre-execution Expected block for a variant that moves ``move``.

    ``move=None`` is the baseline (no change). Otherwise the block flips with
    whether the numeric move achieves the intent or works against it."""
    intent = entry["intent"]
    if move is None:
        return {"yield_targets": _NO_CHANGE, "hold_guards": _NO_CHANGE, "low_signal": _NO_CHANGE}
    if move == _achieving_move(entry):
        return {
            "yield_targets": _YIELD_EFFECT[intent],
            "hold_guards": entry["expected_opposite_risk"],
            "low_signal": _LOW_SIGNAL[intent],
        }
    return {
        "yield_targets": _YIELD_EFFECT_REVERSE[intent],
        "hold_guards": "reduced opposite-risk exposure (safer on the trade-off axis)",
        "low_signal": "possible reduced sensitivity",
    }


def _single_delta(entry: dict, current, bounds, move: str, n: int) -> Optional[dict]:
    """A concrete single-field config delta, or a bounded relative delta when
    the current value is unknown, or None when the move is out of bounds."""
    field = entry["vendor_field_path"]
    step = entry["safe_discrete_step"]
    if current is None:
        return {
            "field": field,
            "from": None,
            "to": None,
            "direction": move,
            "step": step,
            "steps": n,
            "bounds": list(bounds),
            "current_unknown": True,
            # bounded relative move -- never a magic absolute value
            "relative": {"direction": move, "step": step, "steps": n, "bounds": list(bounds)},
        }
    to = _stepped_value(current, step, bounds, move, n)
    if to is None:
        return None
    return {
        "field": field,
        "from": current,
        "to": to,
        "direction": move,
        "step": step,
        "steps": n,
        "bounds": list(bounds),
        "current_unknown": False,
    }


def _single_variant(entry: dict, current, bounds, *, move: str, n: int, kind: str) -> Optional[dict]:
    delta = _single_delta(entry, current, bounds, move, n)
    if delta is None:
        return None
    return {
        "variant_id": _slug(entry["stack"], entry["vendor_field_path"], move, f"{n}step"),
        "stack": entry["stack"],
        "intent": entry["intent"],
        "kind": kind,
        "config_delta": delta,
        "expected": _expected_block(entry, move),
        "expected_opposite_risk": entry["expected_opposite_risk"],
        "observed": None,
    }


def _second_entry(entry: dict, catalogue) -> Optional[dict]:
    """The first same-stack catalogue entry whose vendor field differs from
    ``entry``'s -- the second parameter for a two-parameter combination."""
    stack = entry["stack"]
    if catalogue is not None:
        pool = [e for e in catalogue["entries"] if e["stack"] == stack]
    else:
        pool = _catalogue.catalogue_for(stack)
    for cand in pool:
        if cand["vendor_field_path"] != entry["vendor_field_path"]:
            return cand
    return None


def _two_param_variant(entry: dict, current_config, bounds, current, catalogue) -> Optional[dict]:
    """At most one two-parameter combination: the primary field plus a second
    distinct field, each moved ONE step in its own intent-achieving direction."""
    second = _second_entry(entry, catalogue)
    if second is None:
        return None
    p1 = _single_delta(entry, current, bounds, _achieving_move(entry), 1)
    second_bounds = tuple(second["documented_bounds"])
    second_current = _read_current(second, current_config)
    p2 = _single_delta(second, second_current, second_bounds, _achieving_move(second), 1)
    if p1 is None or p2 is None:
        return None
    expected = dict(_expected_block(entry, _achieving_move(entry)))
    expected["note"] = (
        f"two-parameter combination: also moves {second['vendor_field_path']} one "
        f"step ({second['directional_effect']['direction']}); effects compound on "
        "both axes"
    )
    return {
        "variant_id": _slug(
            entry["stack"], "combo",
            entry["vendor_field_path"], p1["direction"],
            second["vendor_field_path"], p2["direction"],
        ),
        "stack": entry["stack"],
        "intent": entry["intent"],
        "kind": "two_param",
        "config_delta": {"params": [p1, p2]},
        "expected": expected,
        "expected_opposite_risk": entry["expected_opposite_risk"],
        "observed": None,
    }


def _entry_from(catalogue, stack: str, intent: str) -> Optional[dict]:
    if catalogue is not None:
        s = (stack or "").strip().lower()
        i = (intent or "").strip().lower()
        for e in catalogue["entries"]:
            if e["stack"] == s and e["intent"] == i:
                return e
        return None
    return _catalogue.lookup(stack, intent)


def generate_variants(
    *,
    stack: str,
    intent: str,
    current_config,
    catalogue=None,
    max_variants: int = 6,
) -> list:
    """Return the bounded variant set for one ``(stack, intent)`` experiment.

    See the module docstring for the shape and the drop rules. Raises
    ``ValueError`` when the ``(stack, intent)`` has no catalogue entry."""
    entry = _entry_from(catalogue, stack, intent)
    if entry is None:
        raise ValueError(f"no catalogue entry for stack={stack!r} intent={intent!r}")
    bounds = tuple(entry["documented_bounds"])
    current = _read_current(entry, current_config)

    baseline = {
        "variant_id": _slug(entry["stack"], entry["intent"], "baseline"),
        "stack": entry["stack"],
        "intent": entry["intent"],
        "kind": "baseline",
        "config_delta": None,  # clone as-is, no change
        "expected": _expected_block(entry, None),
        "expected_opposite_risk": entry["expected_opposite_risk"],
        "observed": None,
        "dropped": [],  # set-level ledger of anything the cap removed
    }

    # Single-parameter variants first: one lower, one higher.
    singles = []
    for move in ("lower", "higher"):
        v = _single_variant(entry, current, bounds, move=move, n=1, kind="single")
        if v is not None:
            singles.append(v)

    # Adjacent steps (the second ring) only where the family is documented-safe.
    adjacents = []
    if entry["adjacent_steps_safe"]:
        for move in ("lower", "higher"):
            v = _single_variant(entry, current, bounds, move=move, n=2, kind="adjacent")
            if v is not None:
                adjacents.append(v)

    # One two-parameter combination -- only AFTER single-parameter variants exist.
    two_param = None
    if singles:
        two_param = _two_param_variant(entry, current_config, bounds, current, catalogue)

    ordered = [baseline] + singles + adjacents + ([two_param] if two_param else [])

    # Cap: drop the two-parameter combo first, then adjacent steps (from the
    # end). Baseline and single-parameter variants are the protected floor.
    dropped = []
    while len(ordered) > max_variants:
        if two_param is not None and two_param in ordered:
            ordered.remove(two_param)
            dropped.append({"variant_id": two_param["variant_id"], "kind": "two_param"})
            continue
        remaining_adj = [a for a in adjacents if a in ordered]
        if remaining_adj:
            drop = remaining_adj[-1]
            ordered.remove(drop)
            dropped.append({"variant_id": drop["variant_id"], "kind": "adjacent"})
            continue
        # Only the protected floor (baseline + singles) remains; stop dropping
        # rather than cut a required variant. Recorded, never silent.
        dropped.append({"variant_id": None, "kind": "cap_floor",
                        "note": "kept baseline + single-parameter floor above max_variants"})
        break

    baseline["dropped"] = dropped
    return ordered
