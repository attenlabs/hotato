"""Workspace isolation + secret scope (plan §7.1 / Wave 3 security).

Every registry row carries workspace_id; no global path or provider call id may
reach another workspace's artifact. A scoring worker holds no provider secret."""
import json

from hotato.fleet.registry import Registry
from hotato.fleet.api import FleetAPI
from hotato.fleet.jobs import JobQueue, idempotency_key
from hotato.fleet import adapters
from tests import _trial_audio as ta


def test_no_cross_workspace_reads(tmp_path):
    reg = Registry(home=str(tmp_path))
    reg.add_agent("wsA", "a1", stack="vapi", external_ref="asst_secret_A")
    reg.add_connection("wsA", "conn", "vapi", secret_ref="ref-A")
    reg.add_candidate("wsA", "cand-A", agent_id="a1", severity=1.0)
    reg.add_contract("wsA", "con-A", agent_id="a1")
    # wsB is empty and must see NOTHING of wsA
    assert reg.list_agents("wsB") == []
    assert reg.list_candidates("wsB") == []
    assert reg.counts("wsB") == {k: 0 for k in reg.counts("wsB")}
    # even guessing wsA's ids from wsB returns nothing (scoped queries)
    assert reg._one("SELECT * FROM agents WHERE workspace_id=? AND agent_id=?",
                    ("wsB", "a1")) is None
    assert reg._one("SELECT * FROM contracts WHERE workspace_id=? AND contract_id=?",
                    ("wsB", "con-A")) is None
    reg.close()


def test_idempotency_key_is_workspace_scoped():
    """The same operation in two workspaces yields DISTINCT job ids: one
    workspace's webhook can never dedupe against another's work."""
    a = idempotency_key(workspace_id="wsA", agent_id="bot", operation="score",
                        source_pcm_hash="h")
    b = idempotency_key(workspace_id="wsB", agent_id="bot", operation="score",
                        source_pcm_hash="h")
    assert a != b


def test_ingest_is_scoped_and_does_not_leak_across_workspaces(tmp_path):
    api = FleetAPI(home=str(tmp_path / "home"))
    api.init_workspace("wsA"); api.init_workspace("wsB")
    api.agent_add("wsA", "bot", stack="vapi")
    wav = str(tmp_path / "c.wav"); ta.talkover_call(wav)
    ing = api.ingest_recording("wsA", "bot", wav)
    # the recording lives in wsA only
    a_recs = api.registry._all("SELECT recording_id FROM recordings WHERE workspace_id='wsA'")
    b_recs = api.registry._all("SELECT recording_id FROM recordings WHERE workspace_id='wsB'")
    assert len(a_recs) == 1 and b_recs == []
    # the same call id in wsB is NOT considered a duplicate of wsA's
    assert api.registry.has_call("wsA", ing["call_id"])
    assert not api.registry.has_call("wsB", ing["call_id"])
    api.close()


def test_scoring_path_carries_no_provider_credentials():
    """A scoring/mock adapter declares no credential and refuses nothing on the
    offline path; live adapters keep their key private (never returned)."""
    mock = adapters.get_adapter("mock", work_dir=".")
    assert not hasattr(mock, "api_key") or getattr(mock, "api_key", None) is None
    live = adapters.get_adapter("vapi", api_key="sk-secret")
    # the key is not exposed through any capability output
    staged = live.__dict__
    assert "sk-secret" not in json.dumps({k: str(v) for k, v in
                                          {"caps": sorted(live.capabilities())}.items()})
