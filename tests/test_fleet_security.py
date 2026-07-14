"""Workspace isolation + secret scope (plan §7.1 / Wave 3 security).

What IS isolated, stated honestly: every REGISTRY row carries workspace_id, and
every registry query is workspace-scoped, so no workspace-scoped query, guessed
id, provider call id, or path reaches another workspace's rows. The
content-addressed ArtifactStore is a SHARED CAS keyed by digest (a digest is a
capability); its reads are not workspace-scoped by design. Isolation of blobs is
therefore enforced at the registry/reference layer that hands out digests, not at
the blob layer -- the tests below assert exactly that boundary, without
overclaiming blob-layer isolation. A scoring worker holds no provider secret."""
import json

from hotato.fleet import adapters
from hotato.fleet.api import FleetAPI
from hotato.fleet.jobs import idempotency_key
from hotato.fleet.registry import Registry
from hotato.fleet.store import ArtifactStore
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


def test_idempotency_key_join_is_collision_free():
    """A field's contents can never be mistaken for a field boundary. Under a
    bare '|' join these two inputs BOTH produced 'a|b|c|score|...', so two
    DIFFERENT workspaces' jobs deduped to ONE global job_id (job_id is the
    primary key). The serialized join keeps them distinct."""
    k1 = idempotency_key(workspace_id="a|b", agent_id="c", operation="score")
    k2 = idempotency_key(workspace_id="a", agent_id="b|c", operation="score")
    assert k1 != k2
    # the same guarantee holds when the delimiter straddles the operation field
    k3 = idempotency_key(workspace_id="ws", agent_id="a|b", operation="c",
                         source_pcm_hash="")
    k4 = idempotency_key(workspace_id="ws", agent_id="a", operation="b|c",
                         source_pcm_hash="")
    assert k3 != k4


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


def test_partial_ingest_reingest_recreates_lost_recording(tmp_path):
    """put_file -> add_call -> add_recording are separately committed. A crash
    between add_call and add_recording leaves an orphan call row with no
    recording. Re-ingest must SELF-HEAL (create the recording), not treat the
    orphan call as a completed duplicate and lose the recording forever."""
    api = FleetAPI(home=str(tmp_path / "home"))
    api.init_workspace("wsA")
    api.agent_add("wsA", "bot", stack="vapi")
    wav = str(tmp_path / "c.wav"); ta.talkover_call(wav)

    ing = api.ingest_recording("wsA", "bot", wav)          # full, clean ingest
    assert ing["deduped"] is False
    call_id, rec_id = ing["call_id"], ing["recording_id"]

    # reproduce the crash aftermath exactly: the recording row is gone, the call
    # row remains. (Under the old has_call dedup, has_call is now True, so the
    # retry would return deduped=True and the recording would never come back.)
    api.registry.conn.execute("DELETE FROM recordings WHERE workspace_id='wsA'")
    api.registry.conn.commit()
    assert api.registry.has_call("wsA", call_id)            # orphan call present
    assert api.registry._all(
        "SELECT * FROM recordings WHERE workspace_id='wsA'") == []  # recording lost

    out = api.ingest_recording("wsA", "bot", wav)           # re-ingest self-heals
    assert out["deduped"] is False
    assert out["recording_id"] == rec_id
    recs = api.registry._all("SELECT * FROM recordings WHERE workspace_id='wsA'")
    assert len(recs) == 1 and recs[0]["recording_id"] == rec_id

    # a FULLY-ingested recording still dedups: idempotent, no duplicate row
    again = api.ingest_recording("wsA", "bot", wav)
    assert again["deduped"] is True
    assert len(api.registry._all(
        "SELECT * FROM recordings WHERE workspace_id='wsA'")) == 1
    api.close()


def test_isolation_is_registry_layer_not_blob_layer(tmp_path):
    """Honest scope of the guarantee: the content-addressed store is a SHARED CAS
    keyed by digest -- get_bytes takes no workspace_id, so identical bytes from
    two workspaces collapse to ONE blob that either can read given the digest.
    Workspace isolation lives at the REGISTRY/reference layer: the row that NAMES
    a digest is workspace-scoped, so a workspace that never received a digest
    cannot obtain it through any workspace-scoped query. We assert exactly this
    boundary and do NOT overclaim blob-layer isolation."""
    store = ArtifactStore(str(tmp_path / "art"))
    same = b"identical recording bytes across two workspaces"
    dA = store.put_bytes(same, kind="recording", workspace_id="wsA")
    dB = store.put_bytes(same, kind="recording", workspace_id="wsB")
    # CAS: identical content -> one shared blob (NOT isolated at the blob layer),
    # readable from the digest alone with no workspace scoping.
    assert dA == dB
    assert store.get_bytes(dA) == same

    reg = Registry(home=str(tmp_path / "reg"))
    reg.add_recording("wsA", "rec-A", artifact_digest=dA)
    # the REFERENCE (which digest belongs to which workspace) is scoped:
    row = reg._one("SELECT artifact_digest FROM recordings "
                   "WHERE workspace_id=? AND recording_id=?", ("wsA", "rec-A"))
    assert row["artifact_digest"] == dA
    # wsB never received the digest and cannot discover it via a scoped query
    assert reg._all("SELECT artifact_digest FROM recordings "
                    "WHERE workspace_id='wsB'") == []
    reg.close()


def test_artifact_reference_is_the_workspace_scoped_authorization_boundary(tmp_path):
    """The reference-edge/root-reachability primitive that gates every CAS
    evidence read. Identical bytes collapse to ONE shared blob, but authority to
    read it is scoped to the workspace that ROOTS the digest: a foreign workspace
    is refused, and deleting the last live root REVOKES access (CAS presence is
    never authority)."""
    store = ArtifactStore(str(tmp_path / "art"))
    same = b"one blob, two workspaces"
    dA = store.put_bytes(same, kind="conversation", workspace_id="wsA")
    dB = store.put_bytes(same, kind="conversation", workspace_id="wsB")
    assert dA == dB and store.has(dA)          # shared CAS: one physical blob

    reg = Registry(home=str(tmp_path / "reg"))
    reg.add_conversation("wsA", "conv-1", artifact_digest=dA)  # rooted in wsA only

    # authority is scoped to the workspace that named the digest
    assert reg.has_artifact_reference("wsA", dA) is True
    assert reg.has_artifact_reference("wsB", dA) is False
    assert dA in reg.list_root_digests("wsA")
    assert dA not in reg.list_root_digests("wsB")
    # a non-digest string is never a reference
    assert reg.has_artifact_reference("wsA", "not-a-digest") is False

    # a JSON evidence_refs root counts too (sha256:<hex> form), scoped to wsA
    reg.add_evaluation("wsA", "ev-1", conversation_id="conv-1", dimension="policy",
                       status="FAIL", evidence_refs=json.dumps(["sha256:" + ("ab" * 32)]))
    assert reg.has_artifact_reference("wsA", "ab" * 32) is True
    assert reg.has_artifact_reference("wsB", "ab" * 32) is False

    # deleting the ONLY live root revokes authority; the blob is orphaned, present
    reg.conn.execute("DELETE FROM conversations WHERE workspace_id=? AND conversation_id=?",
                     ("wsA", "conv-1"))
    reg.conn.commit()
    assert store.has(dA)                        # still physically present
    assert reg.has_artifact_reference("wsA", dA) is False
    assert dA not in reg.list_root_digests("wsA")
    reg.close()


def test_scoring_path_carries_no_provider_credentials():
    """The scoring/offline path carries no provider secret. Asserted against
    ACTUAL capability OUTPUTS (not the capability NAME strings, which trivially
    can never contain a key): the mock adapter has no credential and its outputs
    carry none, and a live adapter never surfaces its api_key through any public
    method's return value nor through the no-credential refusal message."""
    SECRET = "sk-secret-live-key-9f3a"

    # 1) mock (offline scoring) path: no credential field at all, and the real
    #    capability outputs (clone/apply) carry no secret.
    mock = adapters.get_adapter("mock", work_dir=".")
    assert getattr(mock, "api_key", None) is None
    clone = mock.clone_agent("ref", name="staging")
    applied = mock.apply_variant(clone, {"config_delta": {"interrupt_min_words": 5}})
    assert SECRET not in json.dumps({"clone": clone, "applied": applied})

    # 2) live adapter WITH a key: the key is held privately but is never returned
    #    by any public method that could carry it.
    live = adapters.get_adapter("vapi", api_key=SECRET)
    assert live.api_key == SECRET                    # held privately...
    surfaces = {
        "capabilities": sorted(live.capabilities()),
        "supports_clone": live.supports("clone_agent"),
        "snapshot": live.snapshot_config({"turn_taking": {"interrupt_min_words": 3}}),
        "clone_agent": live.clone_agent("asst_123", name="staging"),
    }
    assert SECRET not in json.dumps(surfaces)        # ...never surfaced

    # 3) the no-credential refusal names the REQUIREMENT, never any secret.
    live_nokey = adapters.get_adapter("vapi")
    try:
        live_nokey.clone_agent("asst_123", name="staging")
        assert False, "clone_agent without credentials must refuse"
    except adapters.CapabilityError as e:
        assert SECRET not in str(e)
