"""Fleet local-mode control plane: registry, store, jobs, and the Guardian loop."""
import json
import os

from hotato import core, evidence as ev
from hotato.fleet.api import FleetAPI
from hotato.fleet.registry import Registry
from hotato.fleet.store import ArtifactStore
from hotato.fleet.jobs import JobQueue, idempotency_key
from tests import _trial_audio as ta


def test_registry_scopes_by_workspace_and_scales(tmp_path):
    reg = Registry(home=str(tmp_path))
    for i in range(1200):
        reg.add_agent("ws1", f"agent-{i}", stack="vapi")
    reg.add_agent("ws2", "other", stack="retell")
    assert reg.counts("ws1")["agents"] == 1200      # no product-level cap
    assert reg.counts("ws2")["agents"] == 1          # workspaces are isolated
    assert reg.list_agents("ws2")[0]["agent_id"] == "other"
    reg.close()


def test_artifact_store_is_content_addressed_and_dedupes(tmp_path):
    st = ArtifactStore(str(tmp_path))
    h1 = st.put_bytes(b"same", kind="envelope", workspace_id="ws1")
    h2 = st.put_bytes(b"same", kind="envelope", workspace_id="ws1")
    assert h1 == h2 and st.verify(h1)
    child = st.put_json({"x": 1}, parents=[h1], workspace_id="ws1")
    assert any(h1 in (r.get("parents") or []) for r in st.lineage(h1))
    assert st.get_json(child) == {"x": 1}


def test_jobs_idempotent_and_dead_letters(tmp_path):
    reg = Registry(home=str(tmp_path))
    q = JobQueue(reg.conn)
    a = q.enqueue(workspace_id="ws1", capability="score", operation="s", source_pcm_hash="h")
    b = q.enqueue(workspace_id="ws1", capability="score", operation="s", source_pcm_hash="h")
    assert not a["deduped"] and b["deduped"] and a["job_id"] == b["job_id"]
    job = q.claim(capability="score", owner="w1")
    assert q.heartbeat(job["job_id"], owner="w1")
    assert q.complete(job["job_id"], owner="w1", output_hashes=["o"])
    assert q.claim(capability="score", owner="w2") is None
    # retries exhaust into dead-letter
    q.enqueue(workspace_id="ws1", capability="cap", operation="c", source_pcm_hash="z", max_attempts=2)
    states = []
    for _ in range(3):
        jb = q.claim(capability="cap", owner="w1")
        if not jb:
            break
        states.append(q.fail(jb["job_id"], owner="w1", reason="boom")["state"])
    assert states[-1] == "dead"
    reg.close()


def _build_trial_envs(tmp_path):
    scen = tmp_path / "scen"; bdir = tmp_path / "before"; adir = tmp_path / "after"
    for d in (scen, bdir, adir):
        d.mkdir()
    json.dump({"id": "f1-yield", "caller_onset_sec": 2.0,
               "expected": {"yield": True, "max_time_to_yield_sec": 1.0, "max_talk_over_sec": 1.0}},
              open(scen / "f1-yield.json", "w"))
    ta.talkover_call(str(bdir / "f1-yield.example.wav"))
    ta.yielding_call(str(adir / "f1-yield.example.wav"))
    before = core.run_suite(scenarios_dir=str(scen), audio_dir=str(bdir), suffix=".example.wav")
    after = core.run_suite(scenarios_dir=str(scen), audio_dir=str(adir), suffix=".example.wav")
    return before, str(bdir), after, str(adir)


def test_guardian_loop_ingest_discover_label(tmp_path):
    api = FleetAPI(home=str(tmp_path / "home"))
    api.init_workspace("ws1", "Acme")
    api.agent_add("ws1", "support-bot", stack="vapi", external_ref="asst_1")
    wav = str(tmp_path / "call.wav"); ta.talkover_call(wav)
    ing = api.ingest_recording("ws1", "support-bot", wav)
    assert not ing["deduped"]
    assert api.ingest_recording("ws1", "support-bot", wav)["deduped"]  # duplicate webhook
    disc = api.discover("ws1", "support-bot", wav, recording_id=ing["recording_id"])
    assert disc["scorable"] and disc["candidates"]
    cid = api.review_queue("ws1")[0]["candidate_id"]
    lab = api.label("ws1", cid, decision="yield", reviewer="alice")
    assert lab["status"] == "labeled"
    api.close()


def test_experiment_run_recommends_but_never_auto_deploys(tmp_path):
    api = FleetAPI(home=str(tmp_path / "home"))
    api.init_workspace("ws1")
    api.agent_add("ws1", "support-bot", stack="vapi")
    before, bdir, after, adir = _build_trial_envs(tmp_path)
    res = api.experiment_run("ws1", "support-bot", trial_id="t1", battery_env=before,
                             before_env=before, before_dir=bdir, after_env=after,
                             after_dir=adir, policy={"max_talk_over_sec": 1.0,
                             "max_time_to_yield_sec": 1.0}, min_n=1)
    assert res["verdict"] in ("improved", "inconclusive")
    assert "approval is required" in res["recommendation"] or res["verdict"] == "inconclusive"
    # a decision row exists and is NOT approved
    dec = api.registry._all("SELECT * FROM decisions WHERE workspace_id='ws1'")
    assert dec and dec[0]["approved"] == 0
    api.close()


def test_experiment_run_refuses_same_audio(tmp_path):
    api = FleetAPI(home=str(tmp_path / "home"))
    api.init_workspace("ws1")
    api.agent_add("ws1", "support-bot", stack="vapi")
    before, bdir, after, adir = _build_trial_envs(tmp_path)
    res = api.experiment_run("ws1", "support-bot", trial_id="t2", battery_env=before,
                             before_env=before, before_dir=bdir, after_env=before,
                             after_dir=bdir, policy={"max_talk_over_sec": 1.0,
                             "max_time_to_yield_sec": 1.0}, min_n=1)
    assert res["verdict"] == "refused"
    assert res["flags"]["same_pcm"]
    api.close()


def test_approve_trial_rejects_refused_or_tier0(tmp_path):
    """Approval is gated on the trial's OWN evidence, never on mere existence: a
    refused verdict or a tier-0/none evidence floor has no green paired proof to
    deploy, so approve_trial must return a structured refusal and write NO
    approval decision row -- not silently record it as approved."""
    api = FleetAPI(home=str(tmp_path / "home"))
    api.init_workspace("ws1")
    api.agent_add("ws1", "support-bot", stack="vapi")
    # a refused, evidence-tier-0 trial (a hard-gate refusal) is NOT approvable
    api.registry.add_trial("ws1", "tref", agent_id="support-bot",
                           verdict="refused", evidence_tier=0)
    res = api.approve_trial("ws1", "tref", approver="director", note="ship it")
    assert res["approved"] is False and res.get("refused") is True
    assert "refused" in res["reason"]
    # no approval decision row was written for the rejected trial
    dec = api.registry._all(
        "SELECT * FROM decisions WHERE workspace_id='ws1' AND trial_id='tref'")
    assert not [d for d in dec if d["approved"] == 1]
    # a non-refused verdict at tier 0 is likewise rejected (evidence floor)
    api.registry.add_trial("ws1", "tlow", agent_id="support-bot",
                           verdict="inconclusive", evidence_tier=0)
    assert api.approve_trial("ws1", "tlow", approver="d")["approved"] is False
    # a created-but-never-run trial (tier none) is rejected too
    api.registry.add_trial("ws1", "tnew", agent_id="support-bot",
                           verdict="created", evidence_tier=None)
    assert api.approve_trial("ws1", "tnew", approver="d")["approved"] is False
    # a genuinely improved, paired-or-better trial still approves
    api.registry.add_trial("ws1", "tok", agent_id="support-bot",
                           verdict="improved", evidence_tier=3)
    ok = api.approve_trial("ws1", "tok", approver="director")
    assert ok["approved"] is True
    assert [d for d in api.registry._all(
        "SELECT * FROM decisions WHERE workspace_id='ws1' AND trial_id='tok'")
        if d["approved"] == 1]
    api.close()


def test_discover_resolves_recording_then_contract_round_trips(tmp_path):
    """The standalone `fleet discover` path is invoked with just the wav (no
    recording_id threaded through). discover must resolve + persist the SAME
    content-addressed recording id ingest produced and bind the candidate to it,
    so `contract create --from-candidate` finds real stored audio instead of
    failing with 'no stored recording'."""
    api = FleetAPI(home=str(tmp_path / "home"))
    api.init_workspace("ws1")
    api.agent_add("ws1", "support-bot", stack="vapi")
    wav = str(tmp_path / "call.wav"); ta.talkover_call(wav)
    ing = api.ingest_recording("ws1", "support-bot", wav)
    disc = api.discover("ws1", "support-bot", wav)   # NO recording_id
    assert disc["scorable"] and disc["candidates"]
    cid = disc["candidates"][0]["candidate_id"]
    # the candidate resolved to the ingested recording, not a NULL/path-derived id
    row = dict(api.registry._one(
        "SELECT recording_id FROM candidates WHERE workspace_id=? AND candidate_id=?",
        ("ws1", cid)))
    assert row["recording_id"] == ing["recording_id"]
    assert cid.startswith(f"cand-{ing['recording_id'][:12]}")
    # so contract_from_candidate finds real stored audio and mints a contract
    res = api.contract_from_candidate("ws1", cid, reviewer="alice", decision="yield")
    assert res["contract_id"] and res["decision"] == "yield"
    api.close()


def test_private_benchmark_ranks_agents_and_excludes_low_evidence(tmp_path):
    api = FleetAPI(home=str(tmp_path / "home"))
    api.init_workspace("ws1")
    api.agent_add("ws1", "bot-a", stack="vapi")
    api.agent_add("ws1", "bot-b", stack="retell")
    api.registry.add_trial("ws1", "t1", agent_id="bot-a", verdict="improved", evidence_tier=3)
    api.registry.add_trial("ws1", "t2", agent_id="bot-a", verdict="refused", evidence_tier=0)
    api.registry.add_trial("ws1", "t3", agent_id="bot-b", verdict="inconclusive", evidence_tier=2)
    api.registry.add_contract("ws1", "c1", agent_id="bot-a", high_stakes=1)
    b = api.benchmark("ws1")
    assert b["scope"] == "private-single-workspace"
    assert "Not a public leaderboard" in b["note"]
    # ranked by paired-or-better: bot-a (1 paired) first
    assert b["agents"][0]["agent_id"] == "bot-a"
    assert b["agents"][0]["paired_or_better"] == 1
    assert b["agents"][0]["high_stakes_contracts"] == 1
    # an evidence floor excludes the low-tier trials
    b2 = api.benchmark("ws1", min_evidence_tier=3)
    a_row = next(r for r in b2["agents"] if r["agent_id"] == "bot-a")
    assert a_row["trials"] == 1        # only the tier-3 trial survives the floor
    # workspace-scoped: another workspace sees nothing
    assert api.benchmark("ws2")["agents"] == []
    api.close()
