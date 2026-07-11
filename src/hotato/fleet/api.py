"""Fleet domain API: the local Guardian loop over the evidence kernel.

Wires the registry + content-addressed store + the deterministic kernel
(trust, scan, contract, manifest, recompute, evidence) into one workflow:

    ingest -> discover candidates -> human review -> label -> contract
           -> manifest-bound before/after trial -> recommendation

It NEVER auto-labels (every failure needs a human label) and NEVER auto-deploys
(a trial produces a recommendation; production deployment stays an explicit human
approval in this release). Live clone/recapture/canary require a connected stack
and credentials and are surfaced as recommendation-only until enabled.
"""
from __future__ import annotations

import hashlib
import json
import os
from typing import Optional

from .. import core as _core
from .. import evidence as _evidence
from .. import manifest as _manifest
from .. import recompute as _recompute
from .. import scan as _scan
from .. import trust as _trust
from .registry import Registry, DEFAULT_HOME
from .store import ArtifactStore
from .jobs import JobQueue


def _short(s: str, n: int = 12) -> str:
    return (s or "")[:n]


class FleetAPI:
    def __init__(self, home: str = DEFAULT_HOME):
        self.home = os.path.abspath(home)
        self.registry = Registry(home=self.home)
        self.store = ArtifactStore(os.path.join(self.home, "artifacts"))
        self.jobs = JobQueue(self.registry.conn)

    def close(self):
        self.registry.close()

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()

    # --- setup ----------------------------------------------------------
    def init_workspace(self, workspace_id: str, name: Optional[str] = None) -> dict:
        self.registry.ensure_workspace(workspace_id, name)
        return {"workspace_id": workspace_id, "home": self.home, "mode": "local"}

    def agent_add(self, workspace_id, agent_id, *, stack, name=None, connection_id=None,
                  external_ref=None) -> dict:
        self.registry.add_agent(workspace_id, agent_id, name=name, stack=stack,
                                connection_id=connection_id, external_ref=external_ref)
        return {"workspace_id": workspace_id, "agent_id": agent_id, "stack": stack}

    def agent_list(self, workspace_id) -> list:
        return self.registry.list_agents(workspace_id)

    # --- ingest ---------------------------------------------------------
    def ingest_recording(self, workspace_id, agent_id, wav_path, *, call_id=None,
                         deployment_id=None) -> dict:
        """Register a completed call's recording. Idempotent on (workspace, call_id)
        and content-addressed by decoded PCM: a duplicate webhook / re-pull
        converges on one recording, never a duplicate candidate later."""
        self.registry.ensure_workspace(workspace_id)
        prov = _core._audio_provenance(("stereo", wav_path))
        pcm = prov["sides"][0]["pcm_sha256"]
        raw = prov["sides"][0]["sha256"]
        call_id = call_id or f"call-{_short(pcm)}"
        recording_id = f"rec-{_short(pcm)}"
        # Dedup on the RECORDING, not the call. put_file -> add_call ->
        # add_recording are three separately-committed writes; a crash between
        # add_call and add_recording leaves an orphan call row with no recording.
        # Gating on has_call would then treat the retry as a completed duplicate
        # and the recording would be lost forever. Gating on the recording makes
        # re-ingest self-healing: the recording is created idempotently, and a
        # fully-ingested recording still dedups (add_call/add_recording are
        # INSERT OR REPLACE, so replaying the writes is a no-op).
        if self._has_recording(workspace_id, recording_id):
            return {"call_id": call_id, "recording_id": recording_id, "deduped": True}
        # store audio SEPARATELY (content-addressed); never embedded in a report
        digest = self.store.put_file(wav_path, kind="recording", workspace_id=workspace_id,
                                     meta={"call_id": call_id, "pcm_sha256": pcm})
        self.registry.add_call(workspace_id, call_id, deployment_id=deployment_id,
                               agent_id=agent_id, provider_locator=os.path.basename(wav_path))
        self.registry.add_recording(workspace_id, recording_id, call_id=call_id,
                                    raw_sha256=raw, pcm_sha256=pcm, artifact_digest=digest,
                                    channel_layout="stereo")
        return {"call_id": call_id, "recording_id": recording_id, "artifact_digest": digest,
                "deduped": False}

    def _has_recording(self, workspace_id, recording_id) -> bool:
        """Recording-existence check, workspace-scoped (mirrors registry.has_call
        for the recordings table). Kept here so ingest self-heals after a partial
        write without widening the Registry surface."""
        return self.registry._one(
            "SELECT 1 FROM recordings WHERE workspace_id=? AND recording_id=?",
            (workspace_id, recording_id)) is not None

    # --- discover -------------------------------------------------------
    def discover(self, workspace_id, agent_id, wav_path, *, recording_id=None,
                 caller_channel=0, agent_channel=1) -> dict:
        """Trust-preflight, then scan for candidate moments. Refuses unscorable
        input; never promotes a timing candidate into a failure (no auto-label).
        Ranking components are stored visibly, not hidden in one opaque score."""
        report = _trust.trust_report(wav_path, caller_channel=caller_channel,
                                     agent_channel=agent_channel)
        input_health = report.get("input_health")
        if input_health is None:
            input_health = "clean" if report.get("scorable") else "not_scorable"
        if not report.get("scorable"):
            return {"scorable": False, "recommendation": report.get("recommendation"),
                    "candidates": []}
        scanned = _scan.scan_recording(wav_path, caller_channel=caller_channel,
                                       agent_channel=agent_channel)
        cands = scanned.get("candidates", scanned.get("moments", []))
        out = []
        for i, c in enumerate(cands[:5]):
            salience = c.get("overlap_sec") or c.get("gap_sec") or 0.0
            cid = f"cand-{_short(recording_id or wav_path)}-{i}"
            components = {
                "severity": salience,
                "input_health": input_health,
                "recurrence": None,       # filled by clustering across calls (future)
                "novelty": None,
                "covered_by_contract": False,
            }
            self.registry.add_candidate(workspace_id, cid, recording_id=recording_id,
                                        agent_id=agent_id, onset_sec=c.get("onset_sec"),
                                        measured_json=json.dumps({**c, "components": components}),
                                        severity=salience, cluster=c.get("kind"))
            out.append({"candidate_id": cid, "onset_sec": c.get("onset_sec"),
                        "severity": salience, "components": components})
        return {"scorable": True, "input_health": input_health, "candidates": out}

    def review_queue(self, workspace_id, *, agent_id=None, limit=5) -> list:
        return self.registry.list_candidates(workspace_id, agent_id=agent_id,
                                              status="new", limit=limit)

    # --- label -> contract ---------------------------------------------
    def label(self, workspace_id, candidate_id, *, decision, reviewer, rationale=None) -> dict:
        """Record a HUMAN label on a candidate. yield/hold promote to a labeled
        failure; not_a_useful_event/bad_input dismiss it. No model may do this."""
        if decision not in ("yield", "hold", "not_a_useful_event", "bad_input"):
            raise ValueError("decision must be yield|hold|not_a_useful_event|bad_input")
        label_id = f"label-{_short(candidate_id)}"
        self.registry.add_label(workspace_id, label_id, candidate_id=candidate_id,
                                reviewer=reviewer, decision=decision, rationale=rationale)
        status = "labeled" if decision in ("yield", "hold") else "dismissed"
        self.registry.set_candidate_status(workspace_id, candidate_id, status)
        return {"label_id": label_id, "candidate_id": candidate_id, "decision": decision,
                "status": status}

    # --- experiment (manifest-bound; recommendation-only) --------------
    def experiment_run(self, workspace_id, agent_id, *, trial_id, battery_env, before_env,
                       before_dir, after_env, after_dir, policy=None, min_n=1,
                       capture_receipts=None, capture_context="operator") -> dict:
        """Recompute a before/after trial under an immutable manifest and record a
        recommendation. Hard gates (score mismatch, same audio, dropped fixtures,
        unrelated audio) refuse; a green paired proof also requires evidence tier
        >= PAIRED. NEVER deploys."""
        nonce = hashlib.sha256(
            (_manifest.canonical_json([m_ev.get("event_id") for m_ev in battery_env.get("events", [])])
             + trial_id).encode("utf-8")).hexdigest()
        man = _manifest.build_manifest(battery_env, trial_id=trial_id, nonce=nonce,
                                       policy=policy, min_n=min_n, agent_id=agent_id,
                                       workspace_id=workspace_id)
        man_digest = self.store.put_json(man, kind="trial_manifest", workspace_id=workspace_id)
        rc = _recompute.recompute_trial(before_env, before_dir, after_env, after_dir, man,
                                        capture_receipts=capture_receipts,
                                        capture_context=capture_context)
        # Enrich the evidence with a trust preflight (input health + channel
        # mapping), the SAME shared logic `hotato fix trial` uses, so an
        # API-driven trial reaches PAIRED for a genuinely clean fix rather than
        # stalling at MEASURED.
        try:
            from .. import fix_trial as _fix_trial
            from .. import evidence as _ev
            ih, cm = _fix_trial._trust_preflight(before_dir, after_dir, before_env, after_env)
            vec = dict(rc["evidence"]["vector"])
            if ih is not None:
                vec["input_health"] = ih
            if cm is not None:
                vec["channel_mapping"] = cm
            rc["evidence"] = _ev.classify(vec)
        except Exception:  # noqa: BLE001 - enrichment is best-effort
            pass
        tier = rc["evidence"]["tier"]
        if rc["refusal"]:
            verdict = "refused"
            recommendation = f"refused: {rc['refusal']['reason']}"
        elif tier >= _evidence.TIER_PAIRED and rc["after_rebuilt"]["summary"]["failed"] == 0:
            verdict = "improved"
            recommendation = ("passed the pinned fresh-recapture battery; approval is required "
                              "before any deployment (no auto-deploy in this release).")
        else:
            verdict = "inconclusive"
            limited = ", ".join(d["dimension"] for d in rc["evidence"]["limited_by"])
            recommendation = f"inconclusive: evidence tier {tier} (limited by {limited})."
        self.registry.add_trial(workspace_id, trial_id, agent_id=agent_id,
                                manifest_hash=man["manifest_hash"], manifest_digest=man_digest,
                                verdict=verdict, evidence_tier=tier)
        decision_id = f"decision-{_short(trial_id)}"
        self.registry.add_decision(workspace_id, decision_id, trial_id=trial_id,
                                   recommendation=recommendation,
                                   hard_gate_json=json.dumps(rc["flags"]), approved=0)
        return {"trial_id": trial_id, "verdict": verdict, "evidence_tier": tier,
                "recommendation": recommendation, "manifest_hash": man["manifest_hash"],
                "refusal": rc["refusal"], "flags": rc["flags"],
                "evidence": rc["evidence"]}

    # --- automatic experiment loop (clone -> apply -> recapture -> recompute) --
    def experiment_clone_run(self, workspace_id, agent_id, *, trial_id, adapter,
                             source_ref, variant, scenarios, before_env, before_dir,
                             battery_env=None, policy=None, min_n=1,
                             work_dir=None) -> dict:
        """The complete automatic experiment path (plan rank 5 / §22.5): clone the
        source agent, apply a bounded variant to the CLONE only, run each scenario
        against it, capture the fresh recordings, score them, and recompute the
        before/after trial under a pinned manifest. Recommends; never deploys.

        With the mock adapter this runs fully offline; with a live adapter the
        networked steps refuse without credentials (production is never mutated).
        Returns the same shape as experiment_run plus the clone/capture record."""
        import os as _os, json as _json
        from .. import core as _core
        work_dir = work_dir or _os.path.join(self.home, "clones", trial_id)
        _os.makedirs(work_dir, exist_ok=True)
        # 1) clone (clone-only; production untouched) + apply the variant to the clone
        clone = adapter.clone_agent(source_ref, name=f"hotato-staging-{trial_id}")
        applied = adapter.apply_variant(clone, variant)
        # 2) run each scenario against the clone and capture fresh audio
        after_dir = _os.path.join(work_dir, "after")
        _os.makedirs(after_dir, exist_ok=True)
        for sc in scenarios:
            cap = adapter.run_scenario(clone, sc)
            rec = cap["recording"]
            dest = _os.path.join(after_dir, f"{sc['id']}.example.wav")
            if _os.path.abspath(rec) != _os.path.abspath(dest):
                _os.replace(rec, dest)
        # 3) score the fresh recaptures into an after envelope
        scen_dir = _os.path.join(work_dir, "scen")
        _os.makedirs(scen_dir, exist_ok=True)
        for sc in scenarios:
            _json.dump(sc, open(_os.path.join(scen_dir, f"{sc['id']}.json"), "w"))
        after_env = _core.run_suite(scenarios_dir=scen_dir, audio_dir=after_dir,
                                    suffix=".example.wav")
        _json.dump(after_env, open(_os.path.join(after_dir, "run.json"), "w"))
        # 4) recompute the before/after trial under a pinned manifest
        result = self.experiment_run(
            workspace_id, agent_id, trial_id=trial_id,
            battery_env=battery_env or before_env, before_env=before_env,
            before_dir=before_dir, after_env=after_env, after_dir=after_dir,
            policy=policy, min_n=min_n)
        result["clone"] = {"ref": clone, "config_hash": applied.get("config_hash"),
                           "cleaned_up": False}
        # 5) clean up the test clone (best-effort; never leaves prod state)
        try:
            adapter.delete_clone(clone)
            result["clone"]["cleaned_up"] = True
        except Exception:  # noqa: BLE001
            pass
        return result

    # --- private fleet benchmark (workspace-scoped; NO public leaderboard) -----
    def benchmark(self, workspace_id, *, min_evidence_tier=None,
                  exclude_unknown_health=True) -> dict:
        """Compare the agents in ONE workspace on their recorded trials and
        contracts (plan §13, private-first). Reports per-agent counts, verdict
        mix, and evidence-tier distribution from real registry data -- never a
        cross-workspace or public leaderboard, and no result whose evidence tier
        is below a floor (when set) enters the comparison.

        Honest by construction: it aggregates only what was actually recorded;
        real and any synthetic trials would be separate axes (synthetic trials
        are not registered here). No blended score -- the component counts stay
        visible."""
        from .. import evidence as _ev
        agents = self.registry.list_agents(workspace_id)
        rows = []
        for a in agents:
            aid = a["agent_id"]
            trials = self.registry._all(
                "SELECT verdict, evidence_tier FROM trials "
                "WHERE workspace_id=? AND agent_id=?", (workspace_id, aid))
            if min_evidence_tier is not None:
                trials = [t for t in trials
                          if (t.get("evidence_tier") or 0) >= min_evidence_tier]
            verdicts = {}
            tiers = {}
            for t in trials:
                verdicts[t["verdict"]] = verdicts.get(t["verdict"], 0) + 1
                tk = t.get("evidence_tier")
                tiers[tk] = tiers.get(tk, 0) + 1
            contracts = self.registry._one(
                "SELECT COUNT(*) c, SUM(high_stakes) hs FROM contracts "
                "WHERE workspace_id=? AND agent_id=?", (workspace_id, aid))
            rows.append({
                "agent_id": aid, "stack": a["stack"],
                "trials": len(trials),
                "verdicts": verdicts,
                "improved": verdicts.get("improved", 0),
                "refused": verdicts.get("refused", 0),
                "inconclusive": verdicts.get("inconclusive", 0),
                "evidence_tier_distribution": {str(k): v for k, v in sorted(
                    tiers.items(), key=lambda kv: (kv[0] is None, kv[0]))},
                "paired_or_better": sum(v for k, v in tiers.items()
                                        if (k or 0) >= _ev.TIER_PAIRED),
                "contracts": (contracts["c"] if contracts else 0) or 0,
                "high_stakes_contracts": (contracts["hs"] if contracts else 0) or 0,
            })
        rows.sort(key=lambda r: (-r["paired_or_better"], -r["improved"], r["agent_id"]))
        return {
            "tool": "hotato", "kind": "fleet_benchmark", "workspace_id": workspace_id,
            "scope": "private-single-workspace",
            "min_evidence_tier": min_evidence_tier,
            "agents": rows,
            "note": ("private to this workspace; component counts stay visible, not "
                     "collapsed into one score. Not a public leaderboard (that needs "
                     "a standardized capture protocol + independent attestation)."),
        }

    # --- status / rollup -----------------------------------------------
    def status(self, workspace_id) -> dict:
        return {"workspace_id": workspace_id, "mode": "local", "home": self.home,
                "counts": self.registry.counts(workspace_id),
                "jobs": self.jobs.stats(workspace_id)}


__all__ = ["FleetAPI"]
