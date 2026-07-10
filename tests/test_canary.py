"""Approval-gated canary + tested rollback (plan §10)."""
from hotato.fleet import canary, adapters


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
