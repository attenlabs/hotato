"""Bounded variant generation (plan 9.4): a small, ordered, deterministic set
of catalogue-bound candidate clones, each stating its expected consequences
before execution, and never proposing a value outside the documented bounds."""

import pytest

from hotato.fleet import catalogue
from hotato.fleet.variants import generate_variants

# A vapi barge-in ("catch interruptions") tuning: field stopSpeakingPlan.numWords,
# documented bounds [0, 10], step 1. A mid-range current so both neighbours move.
STACK = "vapi"
INTENT = "more_sensitive"
CFG_MID = {"turn_taking": {"interrupt_min_words": 3, "interrupt_voice_seconds": 0.3}}


def _by_kind(variants, kind):
    return [v for v in variants if v["kind"] == kind]


def test_baseline_present_and_is_no_change():
    vs = generate_variants(stack=STACK, intent=INTENT, current_config=CFG_MID)
    base = _by_kind(vs, "baseline")
    assert len(base) == 1
    assert base[0] is vs[0]  # baseline leads the set
    assert base[0]["config_delta"] is None
    assert base[0]["expected"] == {
        "yield_targets": "no change (baseline reference)",
        "hold_guards": "no change (baseline reference)",
        "low_signal": "no change (baseline reference)",
    }


def test_lower_and_higher_present_with_concrete_clamped_values():
    vs = generate_variants(stack=STACK, intent=INTENT, current_config=CFG_MID)
    singles = _by_kind(vs, "single")
    dirs = {v["config_delta"]["direction"]: v for v in singles}
    assert set(dirs) == {"lower", "higher"}
    lo, hi = dirs["lower"]["config_delta"], dirs["higher"]["config_delta"]
    # concrete values = current -/+ step (current=3, step=1).
    assert lo["from"] == 3 and lo["to"] == 2
    assert hi["from"] == 3 and hi["to"] == 4
    assert lo["current_unknown"] is False and hi["current_unknown"] is False


def test_total_capped_at_six_by_default():
    vs = generate_variants(stack=STACK, intent=INTENT, current_config=CFG_MID)
    assert len(vs) <= 6


def test_every_variant_has_expected_and_observed_fields():
    vs = generate_variants(stack=STACK, intent=INTENT, current_config=CFG_MID)
    for v in vs:
        assert set(v["expected"]) >= {"yield_targets", "hold_guards", "low_signal"}
        assert all(v["expected"][k] for k in ("yield_targets", "hold_guards", "low_signal"))
        assert v["observed"] is None
    # The intent-achieving single carries the catalogue's opposite-risk verbatim
    # on the hold_guards axis (the plan's "Expected:" block).
    entry = catalogue.lookup(STACK, INTENT)
    achieving = "lower" if entry["directional_effect"]["direction"] == "decrease" else "higher"
    single = next(v for v in _by_kind(vs, "single")
                  if v["config_delta"]["direction"] == achieving)
    assert single["expected"]["hold_guards"] == entry["expected_opposite_risk"]


def test_two_param_combo_appears_once_and_only_after_singles():
    vs = generate_variants(stack=STACK, intent=INTENT, current_config=CFG_MID)
    combos = _by_kind(vs, "two_param")
    assert len(combos) == 1
    combo = combos[0]
    assert set(combo["config_delta"]) == {"params"}
    params = combo["config_delta"]["params"]
    assert len(params) == 2
    assert params[0]["field"] != params[1]["field"]  # two DISTINCT parameters
    # ordering: the combo comes after every single-parameter variant.
    combo_i = vs.index(combo)
    last_single_i = max(vs.index(v) for v in _by_kind(vs, "single"))
    assert combo_i > last_single_i


def test_deterministic_variant_ids_across_two_calls():
    a = generate_variants(stack=STACK, intent=INTENT, current_config=CFG_MID)
    b = generate_variants(stack=STACK, intent=INTENT, current_config=CFG_MID)
    assert [v["variant_id"] for v in a] == [v["variant_id"] for v in b]
    # ids are unique within a set.
    ids = [v["variant_id"] for v in a]
    assert len(ids) == len(set(ids))


def test_bounds_respected_never_below_low_bound():
    # current already at the low bound (0): no variant may propose a value < 0.
    vs = generate_variants(stack=STACK, intent=INTENT,
                           current_config={"turn_taking": {"interrupt_min_words": 0}})
    for v in vs:
        cd = v["config_delta"]
        deltas = cd["params"] if (cd and "params" in cd) else ([cd] if cd else [])
        for d in deltas:
            lo, hi = d["bounds"]
            if d["to"] is not None:
                assert lo <= d["to"] <= hi
                assert d["to"] >= 0
    # the lower single is impossible at the bound, so it is simply absent.
    lower_singles = [v for v in _by_kind(vs, "single")
                     if v["config_delta"]["direction"] == "lower"]
    assert lower_singles == []


def test_bounds_respected_never_above_high_bound():
    # current at the high bound (10): no higher move may be proposed.
    vs = generate_variants(stack=STACK, intent=INTENT,
                           current_config={"turn_taking": {"interrupt_min_words": 10}})
    for v in vs:
        cd = v["config_delta"]
        deltas = cd["params"] if (cd and "params" in cd) else ([cd] if cd else [])
        for d in deltas:
            if d["to"] is not None:
                assert d["to"] <= 10
    higher_singles = [v for v in _by_kind(vs, "single")
                      if v["config_delta"]["direction"] == "higher"]
    assert higher_singles == []


def test_cap_drops_two_param_first_then_adjacent_with_ledger():
    vs = generate_variants(stack=STACK, intent=INTENT, current_config=CFG_MID, max_variants=4)
    assert len(vs) == 4
    # two-param and one adjacent were dropped; singles + baseline are protected.
    assert _by_kind(vs, "two_param") == []
    kinds_dropped = [d["kind"] for d in vs[0]["dropped"]]
    assert kinds_dropped[0] == "two_param"  # combo goes first
    assert "adjacent" in kinds_dropped
    assert len(_by_kind(vs, "single")) == 2  # singles never dropped


def test_current_unknown_yields_bounded_relative_delta():
    vs = generate_variants(stack=STACK, intent=INTENT, current_config={})
    singles = _by_kind(vs, "single")
    assert singles  # still produced, as bounded relative moves
    for v in singles:
        cd = v["config_delta"]
        assert cd["from"] is None and cd["to"] is None
        assert cd["current_unknown"] is True
        # a bounded relative step, never a magic absolute value.
        assert cd["relative"]["bounds"] == [0, 10]
        assert cd["relative"]["direction"] in ("lower", "higher")


def test_unknown_stack_intent_raises():
    with pytest.raises(ValueError):
        generate_variants(stack="nope", intent="more_sensitive", current_config={})
    with pytest.raises(ValueError):
        generate_variants(stack="vapi", intent="nope", current_config={})


def test_livekit_family_not_adjacent_safe_has_no_adjacent_variants():
    # livekit is not documented-safe for adjacent steps -> only lower/higher.
    vs = generate_variants(stack="livekit", intent="more_sensitive",
                           current_config={"turn_taking": {"interrupt_min_words": 3}})
    assert _by_kind(vs, "adjacent") == []
