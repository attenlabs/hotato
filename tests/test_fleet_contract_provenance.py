"""Fleet contract sealing: provenance + collision-free ids + atomic labeling.

Covers two HIGH integrity defects in the Fleet one-click
(``FleetAPI.contract_from_candidate``):

* H-03 -- the signed contract must carry the SAME reviewer, rationale, source
  stack, and candidate reference/kind the fleet label row does, not the
  ``unknown-reviewer`` / ``generic`` / ``null`` fallbacks; the signature must
  authenticate that real identity (tampering the reviewer breaks the digest).
* H-04 -- sibling candidates from one recording must mint DISTINCT contracts
  (no 12-char-truncation collision), a mint failure must not partially commit
  the human label / candidate status, and ``--contract-id`` must recover from a
  prior collision while a taken id collides loudly.
"""
import json
from unittest import mock

from hotato import attest as _attest
from hotato import contract as _contract
from hotato import labelrecord as _labelrecord
from hotato.fleet.api import FleetAPI
from tests import _trial_audio as ta

_KEY = "test-fleet-signing-key-abc123"


def _api_with_candidate(tmp_path, monkeypatch, *, stack="vapi"):
    """A fleet with one ingested talk-over recording and its first discovered
    candidate, plus an HMAC signing key so the contract is genuinely sealed."""
    monkeypatch.setenv("HOTATO_ATTEST_KEY", _KEY)
    api = FleetAPI(home=str(tmp_path / "home"))
    api.init_workspace("ws1")
    api.agent_add("ws1", "support-bot", stack=stack)
    wav = str(tmp_path / "call.wav"); ta.talkover_call(wav)
    api.ingest_recording("ws1", "support-bot", wav)
    disc = api.discover("ws1", "support-bot", wav)
    assert disc["scorable"] and disc["candidates"]
    return api, disc["candidates"][0]["candidate_id"]


def test_sealed_contract_carries_reviewer_stack_rationale_candidate(tmp_path, monkeypatch):
    """(a) The SIGNED contract carries reviewer=qa-bob, the rationale, stack=vapi,
    and the candidate reference + kind -- asserted on the sealed record, not just
    the fleet label row -- and its attestation authenticates that identity."""
    api, cid = _api_with_candidate(tmp_path, monkeypatch)
    cand = dict(api.registry._one(
        "SELECT * FROM candidates WHERE workspace_id='ws1' AND candidate_id=?", (cid,)))
    rationale = "caller clearly interrupted; the agent must yield the floor"
    res = api.contract_from_candidate("ws1", cid, reviewer="qa-bob", decision="yield",
                                      rationale=rationale)
    contract = json.load(open(f"{res['dir']}/contract.json"))

    # the sealed record's identity + provenance == the fleet label row's
    assert contract["identity"]["reviewer"] == "qa-bob"
    assert contract["label"]["rationale"] == rationale
    assert contract["source"]["stack"] == "vapi"
    assert contract["source"]["candidate_ref"] == cid
    assert contract["source"]["candidate_kind"] == cand["cluster"]

    # the signed label-record proof carries the same reviewer + rationale
    lr = contract["label_record"]
    assert lr is not None
    assert lr["reviewer_principal"] == "qa-bob"
    assert lr["rationale"] == rationale
    v = _labelrecord.verify_label_record_local(
        lr, event_pcm_sha256=contract["source"]["bundle_pcm_sha256"])
    assert v["ok"] and v["authority"] == "human-shared"

    # the attestation authenticates that identity, and it is genuinely bound:
    # tampering the reviewer breaks the canonical digest.
    key = _KEY.encode("utf-8")
    assessed = _attest.assess_contract(contract, bundle_dir=res["dir"], key=key)
    assert assessed["authenticated"] and assessed["authenticity"] == "authenticated"
    tampered = json.loads(json.dumps(contract))
    tampered["identity"]["reviewer"] = "attacker"
    assert _attest.assess_contract(
        tampered, bundle_dir=res["dir"], key=key)["authenticity"] == "tampered"

    # the fleet label row agrees (parity, not the source of the assertions above)
    lbl = dict(api.registry._one(
        "SELECT * FROM labels WHERE workspace_id='ws1' AND label_id=?", (res["label_id"],)))
    assert lbl["reviewer"] == "qa-bob" and lbl["rationale"] == rationale
    api.close()


def test_sibling_candidates_get_distinct_contracts(tmp_path, monkeypatch):
    """(b) Three candidates sharing a 12-char prefix mint three DISTINCT contracts,
    all succeeding -- the old `ct-<12char>` truncation collapsed them to one id and
    failed the 2nd/3rd on collision."""
    api, cid0 = _api_with_candidate(tmp_path, monkeypatch)
    rid = dict(api.registry._one(
        "SELECT recording_id FROM candidates WHERE workspace_id='ws1' AND candidate_id=?",
        (cid0,)))["recording_id"]
    # sibling candidates from ONE recording: cand-<rec12>-0/-1/-2 (12-char prefix)
    cids = [f"cand-{rid[:12]}-{i}" for i in range(3)]
    for c in cids:
        api.registry.add_candidate("ws1", c, recording_id=rid, agent_id="support-bot",
                                   onset_sec=2.0, measured_json="{}", severity=0.5,
                                   cluster="overlap_while_agent_talking")
    assert len({c[:12] for c in cids}) == 1  # they really do share a 12-char prefix

    contract_ids = []
    for c in cids:
        r = api.contract_from_candidate("ws1", c, reviewer="qa-bob", decision="yield")
        contract_ids.append(r["contract_id"])
    assert len(set(contract_ids)) == 3                     # distinct
    n = dict(api.registry._one(
        "SELECT COUNT(*) c FROM contracts WHERE workspace_id='ws1'"))["c"]
    assert n == 3                                          # all three persisted
    api.close()


def test_mint_failure_does_not_partially_commit(tmp_path, monkeypatch):
    """(c) A contract-creation failure leaves the candidate 'new' and unlabeled --
    no label row, no status flip, no contract row (no partial commit)."""
    api, cid = _api_with_candidate(tmp_path, monkeypatch)
    with mock.patch.object(_contract, "create_contract",
                           side_effect=RuntimeError("mint boom")):
        try:
            api.contract_from_candidate("ws1", cid, reviewer="qa-bob", decision="yield")
            raised = False
        except RuntimeError:
            raised = True
    assert raised
    status = dict(api.registry._one(
        "SELECT status FROM candidates WHERE workspace_id='ws1' AND candidate_id=?",
        (cid,)))["status"]
    assert status == "new"                                 # still in the review queue
    assert api.registry._one(
        "SELECT 1 FROM labels WHERE workspace_id='ws1' AND label_id=?",
        (f"label-{cid}",)) is None                          # no orphan label
    assert dict(api.registry._one(
        "SELECT COUNT(*) c FROM contracts WHERE workspace_id='ws1'"))["c"] == 0
    api.close()


def test_contract_id_override_recovers_and_collides_loudly(tmp_path, monkeypatch):
    """(d) --contract-id recovers with an explicit id; a taken id collides loudly
    (create_contract refuses it) rather than silently overwriting."""
    api, cid = _api_with_candidate(tmp_path, monkeypatch)
    r = api.contract_from_candidate("ws1", cid, reviewer="qa-bob", decision="yield",
                                    contract_id="recover-001")
    assert r["contract_id"] == "recover-001"

    # a second candidate, same explicit id -> loud collision, and (atomicity) it
    # does not drop the second candidate from the queue.
    rid = dict(api.registry._one(
        "SELECT recording_id FROM candidates WHERE workspace_id='ws1' AND candidate_id=?",
        (cid,)))["recording_id"]
    api.registry.add_candidate("ws1", "cand-other-1", recording_id=rid,
                               agent_id="support-bot", onset_sec=2.0, measured_json="{}",
                               severity=0.5, cluster="overlap_while_agent_talking")
    try:
        api.contract_from_candidate("ws1", "cand-other-1", reviewer="qa-bob",
                                    decision="yield", contract_id="recover-001")
        collided = False
    except ValueError:
        collided = True
    assert collided
    assert dict(api.registry._one(
        "SELECT status FROM candidates WHERE workspace_id='ws1' AND candidate_id='cand-other-1'"
    ))["status"] == "new"
    api.close()
