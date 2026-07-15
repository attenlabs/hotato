"""Adversarial regression tests for the coupled fleet-lifecycle integrity defects.

One coherent receipt + transaction + eligibility model across four findings:

* P0.1 -- destructive clone cleanup must be authorized by a DURABLE clone receipt
  this tool recorded at clone-creation time, never by a mutable provider display
  name; an unregistered (e.g. production) assistant is refused, and a receipt
  cannot be replayed across workspaces or providers.
* P0.3 -- ONE shared eligibility predicate gates BOTH deployment approval and the
  canary; an inconclusive (or any non-'improved') trial, or a below-paired tier,
  is refused by both and writes NO approved decision row.
* P0.6 -- contract_from_candidate commits label + contract + status in ONE atomic
  SQLite transaction; a failure before/after any boundary leaves no partial state
  and a retry converges to exactly one label, one contract, one labeled candidate.
* P1.1 -- experiment_clone_run cleans the staging clone up in an OUTER finally even
  when a downstream step raises, retains the primary exception, records cleanup
  failure separately, and persists a janitor-retry receipt at clone creation.
"""
import itertools
import json
from unittest import mock

import pytest

from hotato import core
from hotato.fleet import adapters
from hotato.fleet import canary as _canary
from hotato.fleet.api import FleetAPI
from tests import _trial_audio as ta

# --- shared fixtures --------------------------------------------------------

def _seeded_candidate(tmp_path, monkeypatch, *, stack="vapi"):
    monkeypatch.setenv("HOTATO_ATTEST_KEY", "lifecycle-test-key")
    api = FleetAPI(home=str(tmp_path / "home"))
    api.init_workspace("ws1")
    api.agent_add("ws1", "support-bot", stack=stack)
    wav = str(tmp_path / "call.wav"); ta.talkover_call(wav)
    api.ingest_recording("ws1", "support-bot", wav)
    cid = api.discover("ws1", "support-bot", wav)["candidates"][0]["candidate_id"]
    return api, cid


def _state(api, cid):
    status = dict(api.registry._one(
        "SELECT status FROM candidates WHERE workspace_id='ws1' AND candidate_id=?",
        (cid,)))["status"]
    labels = dict(api.registry._one(
        "SELECT COUNT(*) n FROM labels WHERE workspace_id='ws1'"))["n"]
    contracts = dict(api.registry._one(
        "SELECT COUNT(*) n FROM contracts WHERE workspace_id='ws1'"))["n"]
    return status, labels, contracts


def _minimal_before(tmp_path):
    scen = tmp_path / "scen"; bdir = tmp_path / "before"
    scen.mkdir(); bdir.mkdir()
    json.dump({"id": "f1-yield", "caller_onset_sec": 2.0,
               "expected": {"yield": True, "max_time_to_yield_sec": 1.0,
                            "max_talk_over_sec": 1.0}},
              open(scen / "f1-yield.json", "w"))
    ta.talkover_call(str(bdir / "f1-yield.example.wav"))
    before = core.run_suite(scenarios_dir=str(scen), audio_dir=str(bdir),
                            suffix=".example.wav")
    json.dump(before, open(bdir / "run.json", "w"))
    return before, str(bdir)


# ==========================================================================
# P0.6 -- atomic contract_from_candidate (transaction + idempotent retry)
# ==========================================================================

def test_register_failure_rolls_back_and_retry_converges(tmp_path, monkeypatch):
    """A failure at the register_contract boundary (AFTER the label write) must
    ROLL BACK the label too -- the OLD code left an orphan label with no contract.
    The already-minted bundle makes the retry idempotent, converging to exactly
    one label, one contract, one labeled candidate."""
    api, cid = _seeded_candidate(tmp_path, monkeypatch)
    with mock.patch.object(api, "register_contract",
                           side_effect=RuntimeError("register boom")):
        with pytest.raises(RuntimeError):
            api.contract_from_candidate("ws1", cid, reviewer="auditor", decision="yield")
    # NOTHING partially committed: no orphan label, no contract, candidate 'new'
    assert _state(api, cid) == ("new", 0, 0)
    # retry (bundle already on disk) converges to exactly one of each
    res = api.contract_from_candidate("ws1", cid, reviewer="auditor", decision="yield")
    assert _state(api, cid) == ("labeled", 1, 1)
    assert res["contract_id"] == f"ct-{cid}"
    api.close()


def test_status_failure_rolls_back_and_retry_converges(tmp_path, monkeypatch):
    """A failure at the LAST boundary (candidate status flip) must roll back the
    label + contract too, then a retry converges to exactly one of each."""
    api, cid = _seeded_candidate(tmp_path, monkeypatch)
    with mock.patch.object(api.registry, "set_candidate_status",
                           side_effect=RuntimeError("status boom")):
        with pytest.raises(RuntimeError):
            api.contract_from_candidate("ws1", cid, reviewer="auditor", decision="yield")
    assert _state(api, cid) == ("new", 0, 0)
    api.contract_from_candidate("ws1", cid, reviewer="auditor", decision="yield")
    assert _state(api, cid) == ("labeled", 1, 1)
    api.close()


def test_contract_from_candidate_is_idempotent_on_replay(tmp_path, monkeypatch):
    """Two successful calls converge to exactly one label + one contract (the
    upserts are keyed by the full candidate id; the status flip is idempotent)."""
    api, cid = _seeded_candidate(tmp_path, monkeypatch)
    r1 = api.contract_from_candidate("ws1", cid, reviewer="auditor", decision="yield")
    r2 = api.contract_from_candidate("ws1", cid, reviewer="auditor", decision="yield")
    assert r1["contract_id"] == r2["contract_id"]
    assert _state(api, cid) == ("labeled", 1, 1)
    api.close()


# ==========================================================================
# P0.3 -- one shared eligibility predicate for approval AND canary
# ==========================================================================

_VERDICTS = ["unknown", None, "refused", "inconclusive", "improved", "unexpected"]
_TIERS = [None, 0, 1, 2, 3, 4]


@pytest.mark.parametrize("verdict,tier", list(itertools.product(_VERDICTS, _TIERS)))
def test_approval_matches_shared_predicate_over_all_verdict_tier_combos(tmp_path, verdict, tier):
    """Over EVERY {unknown, missing, refused, inconclusive, improved, unexpected}
    x tier combination, approval == the shared predicate == the canary gate's
    verdict/tier arm. Only ('improved', tier >= paired) is eligible; every other
    combination is refused and writes NO approved decision row."""
    api = FleetAPI(home=str(tmp_path / "home"))
    api.init_workspace("ws1"); api.agent_add("ws1", "bot", stack="vapi")
    api.registry.add_trial("ws1", "t", agent_id="bot", verdict=verdict, evidence_tier=tier)
    eligible = (verdict == "improved" and isinstance(tier, int)
                and tier >= _canary.MIN_ELIGIBLE_TIER)

    res = api.approve_trial("ws1", "t", approver="ops")
    assert bool(res.get("approved")) is eligible

    # the shared predicate agrees, and so does the canary gate (its verdict/tier arm)
    assert _canary.trial_eligibility(verdict=verdict, evidence_tier=tier)["eligible"] is eligible
    gate = _canary.evaluate_gate(
        _canary.approval_policy(agent_id="bot", parameter_family="pf"),
        trial_verdict=(verdict or "missing"), evidence_tier=(tier or 0),
        full_battery_ran=True, high_stakes_all_pass=True, input_health_degraded=False,
        parameter_family="pf", within_bounds=True)
    assert gate["eligible"] is eligible

    # a rejected trial writes NO approved decision row
    approved_rows = [r for r in api.registry._all(
        "SELECT approved FROM decisions WHERE workspace_id='ws1' AND trial_id='t'")
        if r["approved"] == 1]
    assert bool(approved_rows) is eligible
    api.close()


def test_inconclusive_paired_trial_refused_by_both_layers(tmp_path):
    """THE P0.3 exploit: an inconclusive verdict at a paired+ tier used to be
    APPROVED while the canary gate refused it -- the two authorization layers
    disagreed. Now BOTH refuse it, and no approved decision row is written."""
    api = FleetAPI(home=str(tmp_path / "home"))
    api.init_workspace("ws1"); api.agent_add("ws1", "bot", stack="vapi")
    api.registry.add_trial("ws1", "t", agent_id="bot", verdict="inconclusive", evidence_tier=3)
    res = api.approve_trial("ws1", "t", approver="ops")
    assert res["approved"] is False and res.get("refused") is True
    assert not api.registry._all(
        "SELECT 1 FROM decisions WHERE workspace_id='ws1' AND trial_id='t' AND approved=1")
    gate = _canary.evaluate_gate(
        _canary.approval_policy(agent_id="bot", parameter_family="pf"),
        trial_verdict="inconclusive", evidence_tier=3, full_battery_ran=True,
        high_stakes_all_pass=True, input_health_degraded=False,
        parameter_family="pf", within_bounds=True)
    assert gate["eligible"] is False
    api.close()


def test_approval_refuses_improved_trial_with_a_tripped_hard_gate(tmp_path):
    """Approval requires COMPLETE green hard gates: an 'improved' verdict whose
    recommendation decision recorded a tripped hard gate is refused (fail closed)."""
    api = FleetAPI(home=str(tmp_path / "home"))
    api.init_workspace("ws1"); api.agent_add("ws1", "bot", stack="vapi")
    api.registry.add_trial("ws1", "t", agent_id="bot", verdict="improved", evidence_tier=3)
    api.registry.add_decision("ws1", "decision-t", trial_id="t", recommendation="rec",
                              hard_gate_json=json.dumps({"same_pcm": True}), approved=0)
    res = api.approve_trial("ws1", "t", approver="ops")
    assert res["approved"] is False and "hard gate" in res["reason"]
    assert not api.registry._all(
        "SELECT 1 FROM decisions WHERE workspace_id='ws1' AND trial_id='t' AND approved=1")
    api.close()


# ==========================================================================
# P0.1 -- receipt-governed staging-clone deletion
# ==========================================================================

def _record_receipt(api, ws, *, trial="t", provider="mock", clone_id="staging-1", nonce="n1"):
    api.registry.add_clone_receipt(
        ws, f"clonercpt-{trial}", provider=provider, trial_id=trial, source_id="src",
        clone_id=clone_id, nonce=nonce, name_marker=f"hotato-staging-{trial}")


def test_cleanup_clone_refuses_unregistered_clone(tmp_path):
    """THE P0.1 exploit: a production assistant this tool never cloned has NO
    receipt, so governed cleanup refuses -- a mutable display name (even a 'hotato'
    prefix) is never sufficient authorization."""
    api = FleetAPI(home=str(tmp_path / "home")); api.init_workspace("ws1")
    adapter = adapters.get_adapter("mock", work_dir=str(tmp_path / "w"))
    with pytest.raises(ValueError, match="no durable clone receipt"):
        api.cleanup_clone("ws1", adapter=adapter, trial_id="prod-assistant")
    api.close()


def test_clone_receipt_cannot_be_replayed_across_workspaces(tmp_path):
    """A receipt is workspace-scoped: another workspace cannot use it to delete."""
    api = FleetAPI(home=str(tmp_path / "home"))
    api.init_workspace("ws-a"); api.init_workspace("ws-b")
    _record_receipt(api, "ws-a", trial="t", clone_id="c-a", provider="mock")
    adapter = adapters.get_adapter("mock", work_dir=str(tmp_path / "w"))
    with pytest.raises(ValueError, match="no durable clone receipt"):
        api.cleanup_clone("ws-b", adapter=adapter, trial_id="t")   # ws-b never recorded it
    out = api.cleanup_clone("ws-a", adapter=adapter, trial_id="t")  # the owner can
    assert out["deleted"]
    api.close()


def test_clone_receipt_cannot_be_replayed_across_providers(tmp_path):
    """A vapi receipt cannot authorize a retell delete (no cross-provider replay);
    the mismatch is refused BEFORE the adapter's delete is ever invoked."""
    api = FleetAPI(home=str(tmp_path / "home")); api.init_workspace("ws1")
    _record_receipt(api, "ws1", trial="t", clone_id="c1", provider="vapi")
    retell = adapters.get_adapter("retell")
    with pytest.raises(ValueError, match="across providers"):
        api.cleanup_clone("ws1", adapter=retell, trial_id="t")
    api.close()


def test_clone_run_records_receipt_and_cleans_up(tmp_path, monkeypatch):
    """The happy path records a durable receipt naming the concrete clone id and,
    on success, cleans up and marks the receipt 'deleted'."""
    monkeypatch.setenv("HOTATO_ATTEST_KEY", "k")
    before, bdir = _minimal_before(tmp_path)
    api = FleetAPI(home=str(tmp_path / "home"))
    api.init_workspace("ws1"); api.agent_add("ws1", "bot", stack="mock")
    adapter = adapters.get_adapter("mock", work_dir=str(tmp_path / "work"))
    res = api.experiment_clone_run(
        "ws1", "bot", trial_id="t1", adapter=adapter, source_ref="mock-src",
        variant={"config_delta": {"interrupt_min_words": 1}},
        scenarios=[{"id": "f1-yield", "caller_onset_sec": 2.0}],
        before_env=before, before_dir=bdir,
        policy={"max_talk_over_sec": 1.0, "max_time_to_yield_sec": 1.0}, min_n=1)
    assert res["clone"]["cleaned_up"] is True
    rec = dict(api.registry.find_clone_receipt("ws1", "t1"))
    assert rec["clone_id"] == "mock-clone-1"
    assert rec["provider"] == "mock" and rec["nonce"]
    assert rec["lifecycle_state"] == "deleted"
    api.close()


# ==========================================================================
# P1.1 -- exception-safe clone cleanup (outer finally + janitor receipt)
# ==========================================================================

class _FailingAdapter(adapters.MockAdapter):
    """A mock whose clone_agent DOES create a staging clone, but a chosen
    downstream step raises -- to prove the outer finally still deletes it."""

    def __init__(self, work_dir, *, fail_at="apply"):
        super().__init__(work_dir)
        self.fail_at = fail_at
        self.deleted = []

    def apply_variant(self, clone_ref, variant):
        if self.fail_at == "apply":
            raise RuntimeError("apply failed")
        return super().apply_variant(clone_ref, variant)

    def run_scenario(self, clone_ref, scenario):
        if self.fail_at == "scenario":
            raise RuntimeError("scenario failed")
        return super().run_scenario(clone_ref, scenario)

    def delete_clone(self, clone_ref, *, receipt=None):
        self.deleted.append(clone_ref)
        return super().delete_clone(clone_ref, receipt=receipt)


@pytest.mark.parametrize("fail_at,err", [("apply", "apply failed"),
                                         ("scenario", "scenario failed")])
def test_clone_leak_is_cleaned_up_on_downstream_failure(tmp_path, fail_at, err):
    """A downstream exception (apply or scenario) after clone creation must NOT
    leak the staging clone: the outer finally deletes it via its durable receipt,
    the primary exception propagates, and the receipt resolves to 'deleted'."""
    before, bdir = _minimal_before(tmp_path)
    api = FleetAPI(home=str(tmp_path / "home"))
    api.init_workspace("ws1"); api.agent_add("ws1", "bot", stack="mock")
    adapter = _FailingAdapter(str(tmp_path / "work"), fail_at=fail_at)
    with pytest.raises(RuntimeError, match=err):
        api.experiment_clone_run(
            "ws1", "bot", trial_id="t1", adapter=adapter, source_ref="mock-src",
            variant={"config_delta": {"interrupt_min_words": 1}},
            scenarios=[{"id": "f1-yield", "caller_onset_sec": 2.0}],
            before_env=before, before_dir=bdir, min_n=1)
    assert adapter.deleted == ["mock-clone-1"]           # staging clone WAS removed
    rec = dict(api.registry.find_clone_receipt("ws1", "t1"))
    assert rec["clone_id"] == "mock-clone-1" and rec["lifecycle_state"] == "deleted"
    api.close()


def test_cleanup_failure_records_cleanup_needed_and_retains_primary_error(tmp_path):
    """When cleanup itself fails, the caller's PRIMARY error is retained (not
    replaced by the delete error), and the receipt is marked 'cleanup_needed' for
    a janitor to retry."""
    before, bdir = _minimal_before(tmp_path)
    api = FleetAPI(home=str(tmp_path / "home"))
    api.init_workspace("ws1"); api.agent_add("ws1", "bot", stack="mock")

    class _A(_FailingAdapter):
        def delete_clone(self, clone_ref, *, receipt=None):
            raise RuntimeError("delete boom")

    adapter = _A(str(tmp_path / "work"), fail_at="apply")
    with pytest.raises(RuntimeError, match="apply failed"):   # NOT "delete boom"
        api.experiment_clone_run(
            "ws1", "bot", trial_id="t2", adapter=adapter, source_ref="mock-src",
            variant={"config_delta": {"interrupt_min_words": 1}},
            scenarios=[{"id": "f1-yield", "caller_onset_sec": 2.0}],
            before_env=before, before_dir=bdir, min_n=1)
    rec = dict(api.registry.find_clone_receipt("ws1", "t2"))
    assert rec["lifecycle_state"] == "cleanup_needed"
    api.close()
