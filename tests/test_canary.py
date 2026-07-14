"""Approval-gated canary + tested rollback (plan §10)."""
import pytest

from hotato.fleet import adapters, canary


def _policy():
    return canary.approval_policy(agent_id="bot", parameter_family="interrupt_sensitivity",
                                  min_canary_calls=50, max_traffic_pct=5.0)


def test_gate_requires_improved_paired_and_all_hard_gates():
    p = _policy()
    ok = canary.evaluate_gate(p, trial_verdict="improved", evidence_tier=3,
                              full_battery_ran=True, high_stakes_all_pass=True,
                              input_health_degraded=False,
                              parameter_family="interrupt_sensitivity", within_bounds=True)
    assert ok["eligible"] and ok["reasons"] == []
    # any single failed gate -> ineligible
    bad = canary.evaluate_gate(p, trial_verdict="improved", evidence_tier=2,  # not paired
                               full_battery_ran=True, high_stakes_all_pass=True,
                               input_health_degraded=False,
                               parameter_family="interrupt_sensitivity", within_bounds=True)
    assert not bad["eligible"] and any("paired" in r for r in bad["reasons"])
    hs = canary.evaluate_gate(p, trial_verdict="improved", evidence_tier=3,
                              full_battery_ran=True, high_stakes_all_pass=False,
                              input_health_degraded=False,
                              parameter_family="interrupt_sensitivity", within_bounds=True)
    assert not hs["eligible"]


# The all-pass baseline every hard-gate case is derived from (asserts eligible).
_GATE_BASELINE = dict(trial_verdict="improved", evidence_tier=3, full_battery_ran=True,
                      high_stakes_all_pass=True, input_health_degraded=False,
                      parameter_family="interrupt_sensitivity", within_bounds=True)


def test_gate_baseline_all_pass_is_eligible():
    ok = canary.evaluate_gate(_policy(), **_GATE_BASELINE)
    assert ok["eligible"] and ok["reasons"] == []


@pytest.mark.parametrize("override, expected_reason_substring", [
    ({"trial_verdict": "regressed"}, "not 'improved'"),
    ({"parameter_family": "something-else"}, "not permitted"),
    ({"within_bounds": False}, "documented bounds"),
    ({"full_battery_ran": False}, "not run"),
    ({"input_health_degraded": True}, "input health"),
])
def test_each_hard_gate_blocks_when_flipped(override, expected_reason_substring):
    """Flip exactly ONE input from the all-pass baseline and confirm that single
    gate makes the variant ineligible and names itself in `reasons`. Without a
    per-gate negative case any of these gates could break silently."""
    kwargs = {**_GATE_BASELINE, **override}
    result = canary.evaluate_gate(_policy(), **kwargs)
    assert not result["eligible"]
    assert any(expected_reason_substring in r for r in result["reasons"])


def test_canary_plan_routes_no_traffic_and_is_observational():
    plan = canary.canary_plan(_policy(), variant_id="v1")
    assert plan["routes_traffic"] is False
    assert plan["requires_operator_approval_token"] is True
    assert plan["evidence_class"] == "observational"


def test_observe_is_a_separate_axis_and_flags_regression():
    obs = canary.observe([{"high_stakes_regression": False}] * 40
                         + [{"high_stakes_regression": True}])
    assert obs["evidence_class"] == "observational"
    assert obs["high_stakes_regressions"] == 1 and obs["rollback_recommended"]


def test_rollback_uses_adapter_and_emits_receipt(tmp_path):
    adapter = adapters.get_adapter("mock", work_dir=str(tmp_path))
    r = canary.rollback(adapter, ref="mock-clone-1", revision=3, reason="canary regression",
                        actor="oncall", at=123.0)
    assert r["restored_revision"] == 3
    assert len(r["receipt_digest"]) == 64
    assert r["adapter_result"]["restored_revision"] == 3


def test_full_canary_cycle_gate_plan_observe_rollback(tmp_path):
    """Approve gate -> plan (no traffic) -> observe with an injected regression
    -> rollback through the adapter with a receipt. End-to-end, no real traffic."""
    p = _policy()
    gate = canary.evaluate_gate(p, trial_verdict="improved", evidence_tier=3,
                                full_battery_ran=True, high_stakes_all_pass=True,
                                input_health_degraded=False,
                                parameter_family="interrupt_sensitivity", within_bounds=True)
    assert gate["eligible"]
    plan = canary.canary_plan(p, variant_id="v1")
    assert plan["routes_traffic"] is False and plan["requires_operator_approval_token"]
    # simulate the canary observation surfacing a high-stakes regression
    obs = canary.observe([{"high_stakes_regression": False}] * 49
                         + [{"high_stakes_regression": True}])
    assert obs["rollback_recommended"]
    # auto-rollback on the predeclared trigger, with a durable receipt
    adapter = adapters.get_adapter("mock", work_dir=str(tmp_path))
    receipt = canary.rollback(adapter, ref="v1-clone", revision=7,
                              reason=plan["auto_rollback_trigger"], actor="policy", at=1.0)
    assert receipt["restored_revision"] == 7 and len(receipt["receipt_digest"]) == 64
