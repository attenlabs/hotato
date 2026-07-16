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
from .. import receipt as _receipt
from .. import recompute as _recompute
from .. import scan as _scan
from .. import trust as _trust
from .. import verify as _verify
from ..errors import open_regular as _open_regular
from . import privacy as _privacy
from .jobs import JobQueue
from .registry import DEFAULT_HOME, Registry
from .store import ArtifactStore


def _short(s: str, n: int = 12) -> str:
    return (s or "")[:n]


class FleetAPI:
    def __init__(self, home: str = DEFAULT_HOME):
        self.home = os.path.abspath(home)
        self.registry = Registry(home=self.home)
        # Wire the registry as the store's durable reference source so shared-blob
        # GC (referencing_workspaces / workspace_has_reference) answers from the
        # workspace-scoped reference-edge rows -- the authority -- not from CAS
        # lineage (which is provenance, never an ACL).
        self.store = ArtifactStore(os.path.join(self.home, "artifacts"),
                                   registry=self.registry)
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
        job = self.jobs.record_start(workspace_id=workspace_id, capability="capture",
                                     operation="ingest", agent_id=agent_id, source_pcm_hash=pcm)
        # Dedup on the RECORDING, not the call. put_file -> add_call ->
        # add_recording are three separately-committed writes; a crash between
        # add_call and add_recording leaves an orphan call row with no recording.
        # Gating on has_call would then treat the retry as a completed duplicate
        # and the recording would be lost forever. Gating on the recording makes
        # re-ingest self-healing: the recording is created idempotently, and a
        # fully-ingested recording still dedups (add_call/add_recording are
        # INSERT OR REPLACE, so replaying the writes is a no-op).
        if self._has_recording(workspace_id, recording_id):
            self.jobs.record_done(job["job_id"], output_hashes=[recording_id])
            return {"call_id": call_id, "recording_id": recording_id, "deduped": True,
                    "job_id": job["job_id"]}
        # store audio SEPARATELY (content-addressed); never embedded in a report
        digest = self.store.put_file(wav_path, kind="recording", workspace_id=workspace_id,
                                     meta={"call_id": call_id, "pcm_sha256": pcm})
        self.registry.add_call(workspace_id, call_id, deployment_id=deployment_id,
                               agent_id=agent_id, provider_locator=os.path.basename(wav_path))
        self.registry.add_recording(workspace_id, recording_id, call_id=call_id,
                                    raw_sha256=raw, pcm_sha256=pcm, artifact_digest=digest,
                                    channel_layout="stereo")
        self.jobs.record_done(job["job_id"], output_hashes=[recording_id, digest])
        return {"call_id": call_id, "recording_id": recording_id, "artifact_digest": digest,
                "deduped": False, "job_id": job["job_id"]}

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
        # Bind every candidate to a REAL, content-addressed recording. The
        # standalone `hotato fleet discover <wav>` path is invoked with just the
        # wav (no recording_id threaded through), so resolve + persist the SAME id
        # ingest produces from the decoded PCM. Without this the candidate id would
        # derive from the wav *path* and candidate.recording_id would be NULL, so a
        # later `fleet contract create --from-candidate` finds no stored audio and
        # exits 2. ingest_recording is idempotent (dedups on the recording), so a
        # prior `fleet ingest` is a no-op here and a direct discover self-heals by
        # persisting the recording before scanning it. The `run()` path passes
        # recording_id explicitly and skips this.
        if recording_id is None:
            recording_id = self.ingest_recording(
                workspace_id, agent_id, wav_path)["recording_id"]
        job = self.jobs.record_start(workspace_id=workspace_id, capability="discover",
                                     operation="discover", agent_id=agent_id,
                                     source_pcm_hash=str(recording_id or wav_path))
        report = _trust.trust_report(wav_path, caller_channel=caller_channel,
                                     agent_channel=agent_channel)
        input_health = report.get("input_health")
        if input_health is None:
            input_health = "clean" if report.get("scorable") else "not_scorable"
        if not report.get("scorable"):
            self.jobs.record_done(job["job_id"])
            return {"scorable": False, "recommendation": report.get("recommendation"),
                    "candidates": [], "job_id": job["job_id"]}
        scanned = _scan.scan_recording(wav_path, caller_channel=caller_channel,
                                       agent_channel=agent_channel)
        cands = scanned.get("candidates", scanned.get("moments", []))
        out = []
        for i, c in enumerate(cands[:5]):
            salience = c.get("overlap_sec") or c.get("gap_sec") or 0.0
            onset = c.get("onset_sec")
            if onset is None:
                onset = c.get("t_sec")
            cid = f"cand-{_short(recording_id)}-{i}"
            components = {
                "severity": salience,
                "input_health": input_health,
                "recurrence": None,       # filled by clustering across calls (future)
                "novelty": None,
                "covered_by_contract": False,
            }
            # Idempotency gate (mirrors the _has_recording gate in
            # ingest_recording): candidate_id is deterministic from the
            # recording, so a rescan of an already-processed recording (fleet
            # run re-invoked, a retried job) would otherwise regenerate the
            # SAME candidate_id and, since add_candidate always passes
            # status="new", silently reset an already-reviewed/labeled/
            # dismissed candidate back onto the active review queue. Skip the
            # write entirely when the candidate already exists; a human
            # decision on it is never clobbered by a rescan.
            if not self.registry.has_candidate(workspace_id, cid):
                self.registry.add_candidate(
                    workspace_id, cid, recording_id=recording_id,
                    agent_id=agent_id, onset_sec=onset,
                    measured_json=json.dumps({**c, "components": components}),
                    severity=salience, cluster=c.get("kind"))
            out.append({"candidate_id": cid, "onset_sec": onset,
                        "severity": salience, "components": components})
        self.jobs.record_done(job["job_id"], output_hashes=[c["candidate_id"] for c in out])
        return {"scorable": True, "input_health": input_health, "candidates": out,
                "job_id": job["job_id"]}

    def review_queue(self, workspace_id, *, agent_id=None, limit=5) -> list:
        rows = self.registry.list_candidates(workspace_id, agent_id=agent_id,
                                              status="new", limit=limit)
        # Enrich each queued candidate with related contracts for its agent (plan
        # §9.2), read from the stored components, so a reviewer sees context.
        out = []
        for r in rows:
            r = dict(r)
            try:
                measured = json.loads(r.get("measured_json") or "{}")
            except (TypeError, ValueError):
                measured = {}
            r["components"] = measured.get("components") or {}
            # advisory-only label suggestion (plan §12); never promotes a label.
            from . import suggest as _suggest
            r["suggestion"] = _suggest.suggest(
                measured, input_health=(measured.get("components") or {}).get("input_health"))
            out.append(r)
        return out

    @staticmethod
    def _cluster_key(measured: dict) -> str:
        kind = measured.get("kind") or measured.get("cluster") or "moment"
        comp = measured.get("components") or {}
        sev = measured.get("severity") or comp.get("severity") or 0.0
        try:
            sev = float(sev)
        except (TypeError, ValueError):
            sev = 0.0
        band = "hi" if sev >= 0.5 else ("mid" if sev >= 0.2 else "lo")
        return f"{kind}:{band}"

    def recluster_agent(self, workspace_id, agent_id) -> dict:
        """Fill recurrence / novelty / covered_by_contract on an agent's candidates
        by clustering their measured SHAPES across calls (plan §9.1 ranking
        components). Visible components, never one opaque score."""
        from collections import Counter
        cands = self.registry._all(
            "SELECT candidate_id, measured_json FROM candidates "
            "WHERE workspace_id=? AND agent_id=?", (workspace_id, agent_id))
        counter = Counter()
        keys = {}
        for c in cands:
            try:
                m = json.loads(c["measured_json"] or "{}")
            except (TypeError, ValueError):
                m = {}
            k = self._cluster_key(m)
            keys[c["candidate_id"]] = k
            counter[k] += 1
        has_contract = bool(dict(self.registry._one(
            "SELECT COUNT(*) c FROM contracts WHERE workspace_id=? AND agent_id=?",
            (workspace_id, agent_id)) or {}).get("c"))
        n_total = max(1, len(cands))
        # One transaction for the whole per-candidate rewrite so it stays
        # all-or-nothing. The Registry connection runs in autocommit
        # (isolation_level=None), so an explicit BEGIN IMMEDIATE..COMMIT -- not a
        # trailing .commit() over a loop of otherwise-individually-committing
        # UPDATEs -- is what makes this batch atomic: a failure part-way through
        # (e.g. `database is locked`) rolls the whole batch back instead of
        # leaving some candidates rewritten and the rest stale.
        conn = self.registry.conn
        conn.execute("BEGIN IMMEDIATE")
        try:
            for c in cands:
                try:
                    m = json.loads(c["measured_json"] or "{}")
                except (TypeError, ValueError):
                    m = {}
                comp = m.get("components") or {}
                rec = counter[keys[c["candidate_id"]]]
                comp["recurrence"] = rec
                # a shape seen once is novel (~1.0); a recurring shape trends to 0.
                comp["novelty"] = round(1.0 - (rec - 1) / n_total, 3)
                comp["covered_by_contract"] = has_contract
                m["components"] = comp
                conn.execute(
                    "UPDATE candidates SET measured_json=? WHERE workspace_id=? AND candidate_id=?",
                    (json.dumps(m), workspace_id, c["candidate_id"]))
            conn.execute("COMMIT")
        except Exception:
            if conn.in_transaction:
                conn.execute("ROLLBACK")
            raise
        return {"clusters": dict(counter), "candidates": len(cands)}

    def run(self, workspace_id, agent_id, *, recordings=None, caller_channel=0,
            agent_channel=1) -> dict:
        """Batch discovery (plan §9.1 / §16 `fleet run`): ingest + discover a set of
        dual-channel recordings, recluster the agent's candidates to fill the
        recurrence/novelty/covered components, advance a durable watermark, and
        return the top-5 review queue + a cluster rollup. Pulling recent calls from
        a live connection needs credentials (gated); offline callers pass
        ``recordings`` (a list of wav paths). Never auto-labels."""
        self.registry.ensure_workspace(workspace_id)
        ingested = []
        for wav in (recordings or []):
            ing = self.ingest_recording(workspace_id, agent_id, wav)
            disc = self.discover(workspace_id, agent_id, wav,
                                 recording_id=ing["recording_id"],
                                 caller_channel=caller_channel, agent_channel=agent_channel)
            ingested.append({"recording_id": ing["recording_id"],
                             "scorable": disc.get("scorable"),
                             "candidates": len(disc.get("candidates", []))})
        cl = self.recluster_agent(workspace_id, agent_id)
        self.registry.set_watermark(workspace_id, agent_id, "discover", self.registry._now())
        return {"workspace_id": workspace_id, "agent_id": agent_id,
                "ingested": ingested, "clusters": cl["clusters"],
                "reviewed_candidates": cl["candidates"],
                "top_candidates": self.review_queue(workspace_id, agent_id=agent_id, limit=5)}

    # --- label -> contract ---------------------------------------------
    def label(self, workspace_id, candidate_id, *, decision, reviewer, rationale=None) -> dict:
        """Record a HUMAN label on a candidate. yield/hold promote to a labeled
        failure; not_a_useful_event/bad_input dismiss it. No model may do this."""
        if decision not in ("yield", "hold", "not_a_useful_event", "bad_input"):
            raise ValueError("decision must be yield|hold|not_a_useful_event|bad_input")
        # Reject an orphan label: a label must reference a real candidate in this
        # workspace, or a review decision would attach to nothing (and a typo'd
        # candidate id would silently create fleet data).
        if not self.registry.has_candidate(workspace_id, candidate_id):
            raise ValueError(
                f"no candidate {candidate_id!r} in workspace {workspace_id!r}; "
                "label a candidate surfaced by `hotato fleet discover`")
        # label_id is derived from the FULL candidate id, never a truncation: two
        # candidates from the same recording (cand-...-0, cand-...-1) must not
        # collapse to one label_id and overwrite each other's human decision.
        label_id = f"label-{candidate_id}"
        self.registry.add_label(workspace_id, label_id, candidate_id=candidate_id,
                                reviewer=reviewer, decision=decision, rationale=rationale)
        status = "labeled" if decision in ("yield", "hold") else "dismissed"
        self.registry.set_candidate_status(workspace_id, candidate_id, status)
        return {"label_id": label_id, "candidate_id": candidate_id, "decision": decision,
                "status": status}

    # --- contract registration (populates the contracts table) ---------
    def register_contract(self, workspace_id, *, contract_id, agent_id=None, label_id=None,
                          policy_hash=None, canonical_digest=None, artifact_digest=None,
                          high_stakes=False) -> dict:
        """Register a contract in the fleet contracts table. Setting high_stakes
        makes the canary opposite-risk gate and the private benchmark's high-stakes
        counts real (plan §14) -- otherwise the column is inert plumbing."""
        self.registry.add_contract(
            workspace_id, contract_id, label_id=label_id, agent_id=agent_id,
            policy_hash=policy_hash, canonical_digest=canonical_digest,
            artifact_digest=artifact_digest, high_stakes=1 if high_stakes else 0)
        return {"contract_id": contract_id, "high_stakes": bool(high_stakes)}

    def contract_from_candidate(self, workspace_id, candidate_id, *, reviewer, decision,
                                contract_id=None, high_stakes=False, max_talk_over_sec=None,
                                max_time_to_yield_sec=None, rationale=None) -> dict:
        """One-click (plan §9.2): mint a real .hotato failure contract from a
        reviewed candidate's stored recording at the candidate onset, record the
        human label, and register it (with an optional high-stakes flag).
        yield/hold only; a model may never do this.

        The sealed contract carries the SAME identity + provenance the fleet label
        row does: the reviewer, the rationale, the agent's source stack, and the
        candidate reference + kind are all forwarded into the contract creation +
        signing path, so the signature never authenticates a false or incomplete
        record.

        Atomic by construction: the contract is minted + sealed FIRST (the step
        that can fail -- e.g. a contract-id collision raises here), and only then
        are the human label, the contract registration, and the candidate's status
        change committed, with the review-queue-dropping status flip LAST. A
        failure while minting leaves the candidate 'new' and unlabeled -- never a
        labeled candidate with no contract. Pass ``contract_id`` to recover from a
        prior collision; a taken id collides loudly (create_contract refuses it)."""
        if decision not in ("yield", "hold"):
            raise ValueError("contract_from_candidate decision must be 'yield' or 'hold'")
        cand = self.registry._one(
            "SELECT * FROM candidates WHERE workspace_id=? AND candidate_id=?",
            (workspace_id, candidate_id))
        if not cand:
            raise ValueError(f"no candidate {candidate_id!r} in workspace {workspace_id!r}")
        cand = dict(cand)
        recording_id = cand.get("recording_id")
        rec = self.registry._one(
            "SELECT * FROM recordings WHERE workspace_id=? AND recording_id=?",
            (workspace_id, recording_id)) if recording_id else None
        rec = dict(rec) if rec else None
        if not rec or not rec.get("artifact_digest"):
            raise ValueError(
                f"candidate {candidate_id!r} has no stored recording to build a contract from")
        agent_id = cand.get("agent_id")
        # The agent's REGISTERED stack (e.g. vapi) -- sealed into the contract's
        # source provenance instead of the 'generic' fallback.
        stack = None
        if agent_id:
            _arow = self.registry._one(
                "SELECT stack FROM agents WHERE workspace_id=? AND agent_id=?",
                (workspace_id, agent_id))
            stack = dict(_arow).get("stack") if _arow else None
        import os as _os
        import tempfile as _tf

        from .. import contract as _contract
        cdir = _os.path.join(self.home, "contracts", workspace_id)
        _os.makedirs(cdir, exist_ok=True)
        # Collision-free contract id: derived from the FULL candidate id, never a
        # 12-char truncation. Sibling candidates from one recording share a
        # 12-char prefix (cand-<rec12>-0/-1/-2), so `ct-<short>` collapsed them all
        # onto one id and the second mint collided; the full id is unique per
        # candidate and is already a valid contract slug.
        cid = contract_id or f"ct-{candidate_id}"
        # label_id derived from the FULL candidate id (mirrors label()), never a
        # truncation, so sibling candidates never overwrite each other's decision.
        label_id = f"label-{candidate_id}"
        bundle_dir = _os.path.join(cdir, cid + _contract.BUNDLE_SUFFIX)

        # --- idempotent artifact staging + reconciliation -------------------
        # Minting + sealing the bundle and publishing its CAS ref are the
        # fail-prone, NON-transactional steps; the three registry writes below are
        # ONE SQLite transaction. On a retry after a partial failure the bundle may
        # already exist on disk (create_contract writes it atomically). Reconcile
        # rather than re-mint: REUSE a bundle THIS candidate already minted, but
        # REFUSE (loudly, before any registry write, so the candidate stays 'new')
        # a bundle an explicit --contract-id already bound to a DIFFERENT candidate
        # -- a real id collision. A fresh id mints a new bundle.
        contract_json_path = _os.path.join(bundle_dir, "contract.json")
        if _os.path.isdir(bundle_dir) and _os.path.exists(contract_json_path):
            try:
                with _open_regular(contract_json_path, "r", encoding="utf-8") as _fh:
                    cjson = json.load(_fh)
            except (OSError, ValueError):
                cjson = {}
            existing_ref = (cjson.get("source") or {}).get("candidate_ref")
            if existing_ref not in (None, candidate_id):
                raise ValueError(
                    f"contract id {cid!r} already exists for a different candidate "
                    f"({existing_ref!r}); pass a fresh --contract-id to mint a new "
                    "contract for this candidate.")
        else:
            # Verified read: the contract binds this exact audio; a blob that no
            # longer hashes to its digest (poisoned/bit-rot) must abort the mint,
            # never be sealed into a signed contract as authentic evidence.
            data = self.store.get_bytes(rec["artifact_digest"], verify=True)
            tf = _tf.NamedTemporaryFile(suffix=".wav", delete=False)
            try:
                tf.write(data); tf.flush(); tf.close()
                # Mint + seal the contract. Forward reviewer/stack/rationale/
                # candidate ref+kind so the signed record matches the label.
                res = _contract.create_contract(
                    stereo=tf.name, onset_sec=cand.get("onset_sec"), expect=decision,
                    contract_id=cid, out_dir=cdir, stack=stack,
                    # governed fleet approval: the reviewer comes from the
                    # approval flow, which is the human-review attestation (R-09).
                    reviewer_principal=reviewer, human_review_attested=True,
                    rationale=rationale,
                    candidate_ref=candidate_id, candidate_kind=cand.get("cluster"),
                    include_identifiers=True,
                    max_talk_over_sec=max_talk_over_sec,
                    max_time_to_yield_sec=max_time_to_yield_sec)
            finally:
                try:
                    _os.unlink(tf.name)
                except OSError:
                    pass
            bundle_dir = res["dir"]
            contract_json_path = _os.path.join(bundle_dir, "contract.json")
            cjson = res.get("contract") or {}
        # Publish the sealed bundle to the CAS. Content-addressed, so a replayed
        # put on retry converges on the SAME digest + workspace reference edge.
        digest = self.store.put_file(contract_json_path,
                                     kind="contract", workspace_id=workspace_id)
        canonical = (cjson.get("attestation") or {}).get("canonical_digest")
        # --- ONE atomic transaction: label + contract + status --------------
        # register_contract/add_label/set_candidate_status route their commit
        # through Registry._commit, which DEFERS inside this transaction() block,
        # so the three writes commit together or ROLL BACK together. A failure
        # before OR after any boundary leaves NO partial state (never a labeled
        # candidate with no contract, nor an orphan label), and a retry -- the
        # upserts are keyed by id, the status flip is idempotent -- converges to
        # exactly one label, one contract, one labeled candidate. The
        # review-queue-dropping status flip is written LAST.
        with self.registry.transaction():
            self.registry.add_label(workspace_id, label_id, candidate_id=candidate_id,
                                    reviewer=reviewer, decision=decision, rationale=rationale)
            self.register_contract(workspace_id, contract_id=cid, agent_id=agent_id,
                                   label_id=label_id, canonical_digest=canonical,
                                   artifact_digest=digest, high_stakes=high_stakes)
            self.registry.set_candidate_status(workspace_id, candidate_id, "labeled")
        return {"contract_id": cid, "dir": bundle_dir, "label_id": label_id,
                "high_stakes": bool(high_stakes), "decision": decision}

    # --- experiment (manifest-bound; recommendation-only) --------------
    def _build_trial_manifest(self, workspace_id, agent_id, *, trial_id, battery_env,
                              policy=None, min_n=1) -> dict:
        """Deterministic manifest for a trial: one scorer, one policy, the complete
        ordered fixture universe of the committed battery, each onset + stimulus."""
        nonce = hashlib.sha256(
            (_manifest.canonical_json(
                [m_ev.get("event_id") for m_ev in battery_env.get("events", [])])
             + trial_id).encode("utf-8")).hexdigest()
        return _manifest.build_manifest(battery_env, trial_id=trial_id, nonce=nonce,
                                        policy=policy, min_n=min_n, agent_id=agent_id,
                                        workspace_id=workspace_id)

    def experiment_create(self, workspace_id, agent_id, *, trial_id, battery_env,
                          policy=None, min_n=1) -> dict:
        """PRECOMMIT the trial manifest from the committed battery BEFORE any after-
        side capture, so the pinned fixture universe is fixed ahead of time and
        cannot be reduced once the results are in. ``experiment run --manifest`` then
        consumes exactly this manifest and refuses any before/after that does not
        cover it. Never captures, never deploys."""
        man = self._build_trial_manifest(workspace_id, agent_id, trial_id=trial_id,
                                          battery_env=battery_env, policy=policy, min_n=min_n)
        # Sign the pinned policy/fixture universe when a signing key is present, so a
        # clean fresh-recapture can reach the ATTESTED tier (policy_integrity=signed).
        _key = _receipt.load_key()
        if _key is not None:
            man = _manifest.sign_manifest(man, _key)
        man_digest = self.store.put_json(man, kind="trial_manifest", workspace_id=workspace_id)
        self.registry.add_trial(workspace_id, trial_id, agent_id=agent_id,
                                manifest_hash=man["manifest_hash"], manifest_digest=man_digest,
                                verdict="created", evidence_tier=None)
        fixtures = list(_manifest.fixture_index(man).keys())
        return {"trial_id": trial_id, "manifest_hash": man["manifest_hash"],
                "manifest_digest": man_digest, "nonce": man.get("nonce"),
                "fixtures": fixtures, "min_n": min_n,
                "next": ("capture the after side against the applied clone, then: "
                         f"hotato fleet experiment run --agent {agent_id} "
                         f"--trial-id {trial_id} --manifest {man_digest} "
                         "--before <dir> --after <dir>")}

    def experiment_run(self, workspace_id, agent_id, *, trial_id, battery_env=None, before_env,
                       before_dir, after_env, after_dir, policy=None, min_n=1,
                       manifest_ref=None, capture_receipts=None, capture_context="operator") -> dict:
        """Recompute a before/after trial under an immutable manifest and record a
        recommendation. Hard gates (score mismatch, same audio, dropped fixtures,
        unrelated audio) refuse; a green paired proof also requires the same
        fail-closed verdict `fix trial` uses. NEVER deploys.

        With ``manifest_ref`` (a digest from ``experiment_create``) the pinned
        universe was committed BEFORE capture and is loaded here verbatim -- the
        fixture set cannot be cherry-picked to the results. Without it the manifest
        is pinned at RUN time from ``battery_env`` (lower assurance: the universe is
        only fixed once the results already exist)."""
        if manifest_ref is not None:
            # Verified read: the pinned universe governs the whole trial; a
            # tampered manifest blob must abort rather than silently redefine the
            # committed fixture set.
            man = self.store.get_json(manifest_ref, verify=True)
            if not man or man.get("trial_id") != trial_id:
                raise ValueError(
                    f"manifest {manifest_ref!r} does not exist or is not the "
                    f"committed manifest for trial {trial_id!r}")
            man_digest = manifest_ref
        else:
            if battery_env is None:
                raise ValueError("experiment_run needs either battery_env or a committed manifest_ref")
            man = self._build_trial_manifest(workspace_id, agent_id, trial_id=trial_id,
                                             battery_env=battery_env, policy=policy, min_n=min_n)
            man_digest = self.store.put_json(man, kind="trial_manifest", workspace_id=workspace_id)
        # min_n from a committed manifest wins over any run-time argument.
        min_n = int((man.get("min_n") if isinstance(man.get("min_n"), int) else None) or min_n)
        _job = self.jobs.record_start(
            workspace_id=workspace_id, capability="experiment",
            operation=f"experiment:{trial_id}", agent_id=agent_id,
            policy_hash=str(man.get("policy_hash") or ""))
        rc = _recompute.recompute_trial(before_env, before_dir, after_env, after_dir, man,
                                        capture_receipts=capture_receipts,
                                        capture_context=capture_context)
        # Enrich the evidence with a trust preflight (input health + channel
        # mapping), the SAME shared logic `hotato fix trial` uses, so an
        # API-driven trial reaches PAIRED for a genuinely clean fix rather than
        # stalling at MEASURED.
        try:
            from .. import evidence as _ev
            from .. import fix_trial as _fix_trial
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
        # Verdict predicate: the SAME fail-closed comparison `hotato fix trial`
        # uses -- NOT merely "the after side has no failures". verdict_model
        # requires >= min_n previously-failing fixtures, at least one now passing,
        # and no regression, all recomputed from audio. An all-pass-before /
        # all-pass-after battery has zero fail->pass transitions and is
        # inconclusive, never "improved"; --min-n is ENFORCED here, not merely
        # recorded in the manifest.
        import shutil as _shutil
        import tempfile as _tempfile
        vm = None
        _tmpd = _tempfile.mkdtemp(prefix="hotato-fleet-trial-")
        try:
            _tb = os.path.join(_tmpd, "before.json")
            _ta = os.path.join(_tmpd, "after.json")
            with open(_tb, "w", encoding="utf-8") as _fh:
                json.dump(rc["before_rebuilt"], _fh)
            with open(_ta, "w", encoding="utf-8") as _fh:
                json.dump(rc["after_rebuilt"], _fh)
            _v = _verify.verify_sides(_tb, _ta, min_n=min_n)
            vm = _verify.verdict_model(_v)
        except Exception:  # noqa: BLE001 - a compare failure is inconclusive, never a soft pass
            vm = None
        finally:
            _shutil.rmtree(_tmpd, ignore_errors=True)

        if rc["refusal"]:
            verdict = "refused"
            recommendation = f"refused: {rc['refusal']['reason']}"
        elif vm is None:
            verdict = "inconclusive"
            recommendation = "inconclusive: the before/after pair could not be compared."
        elif vm["passed"] and tier >= _evidence.TIER_PAIRED:
            verdict = "improved"
            _hs = dict(self.registry._one(
                "SELECT COUNT(*) c FROM contracts WHERE workspace_id=? AND agent_id=? AND high_stakes=1",
                (workspace_id, agent_id)) or {}).get("c") or 0
            _hs_note = (f" {_hs} high-stakes contract(s) registered for this agent must be "
                        "independently re-verified before deployment." if _hs else "")
            recommendation = (
                f"{vm['now_pass']} of {vm['used_to_fail']} previously-failing fixture(s) now pass "
                f"(min-n {min_n}), {vm['hold_still_pass']} of {vm['hold_guards']} hold guard(s) still "
                "pass; approval is required before any deployment (no auto-deploy in this release)."
                + _hs_note)
        elif vm["passed"]:
            limited = ", ".join(d["dimension"] for d in rc["evidence"]["limited_by"])
            verdict = "inconclusive"
            recommendation = (f"inconclusive: the paired comparison passed but the evidence tier is "
                              f"{tier} (limited by {limited}).")
        else:
            verdict = "inconclusive"
            recommendation = f"inconclusive: {vm['conclusion']}"
        self.registry.add_trial(workspace_id, trial_id, agent_id=agent_id,
                                manifest_hash=man["manifest_hash"], manifest_digest=man_digest,
                                verdict=verdict, evidence_tier=tier)
        decision_id = f"decision-{_short(trial_id)}"
        self.registry.add_decision(workspace_id, decision_id, trial_id=trial_id,
                                   recommendation=recommendation,
                                   hard_gate_json=json.dumps(rc["flags"]), approved=0)
        self.jobs.record_done(_job["job_id"], output_hashes=[man["manifest_hash"], verdict])
        metrics = None
        if vm is not None:
            metrics = {"evidence_tier": tier,
                       "yield_success": vm.get("now_pass"), "used_to_fail": vm.get("used_to_fail"),
                       "hold_success": vm.get("hold_still_pass"), "hold_guards": vm.get("hold_guards"),
                       "regressions": len(vm.get("regressions") or []),
                       "talk_over_p95_after": vm.get("talk_over_p95_after"),
                       "new_false_yields": vm.get("new_false_yields")}
        return {"trial_id": trial_id, "verdict": verdict, "evidence_tier": tier,
                "recommendation": recommendation, "manifest_hash": man["manifest_hash"],
                "refusal": rc["refusal"], "flags": rc["flags"], "metrics": metrics,
                "evidence": rc["evidence"], "job_id": _job["job_id"]}

    # --- staging-clone lifecycle (durable receipt authorizes deletion) --------
    @staticmethod
    def _resolve_clone_id(clone, applied):
        """The concrete provider clone id that delete_clone must target. A live
        adapter's clone_agent is a pending stage; apply_variant is what creates the
        clone and returns its ``clone_id`` -- prefer that. The mock returns the id
        as a bare string from clone_agent."""
        if isinstance(applied, dict) and applied.get("clone_id"):
            return applied.get("clone_id")
        if isinstance(clone, dict):
            return clone.get("clone_id") or clone.get("id")
        return clone

    def _record_clone_receipt(self, workspace_id, *, trial_id, adapter, source_ref,
                              clone, applied, nonce, name_marker, lifecycle_state="created"):
        """Persist (or refine) the durable clone receipt that authorizes a later
        deletion. Keyed per trial, so the post-clone stage row is upserted with the
        concrete provider clone id once apply_variant returns it. Returns the
        receipt fields (incl. ``receipt_id``) so the caller can drive cleanup."""
        provider = getattr(adapter, "stack", None) or type(adapter).__name__
        clone_id = self._resolve_clone_id(clone, applied)
        receipt_id = f"clonercpt-{trial_id}"
        body = {"provider": provider, "trial_id": trial_id, "source_id": str(source_ref),
                "clone_id": clone_id, "nonce": nonce, "name_marker": name_marker}
        digest = _manifest._sha256_str(_manifest.canonical_json(body))
        self.registry.add_clone_receipt(
            workspace_id, receipt_id, provider=provider, trial_id=trial_id,
            source_id=str(source_ref), clone_id=clone_id, nonce=nonce,
            name_marker=name_marker, lifecycle_state=lifecycle_state, receipt_digest=digest)
        return {"receipt_id": receipt_id, "provider": provider, "trial_id": trial_id,
                "source_id": str(source_ref), "clone_id": clone_id, "nonce": nonce,
                "name_marker": name_marker, "receipt_digest": digest}

    def cleanup_clone(self, workspace_id, *, adapter, receipt_id=None, trial_id=None) -> dict:
        """GOVERNED staging-clone deletion (plan §22.5). Deletion is authorized ONLY
        by a durable clone receipt THIS tool recorded at clone-creation time --
        never by a caller-supplied clone id or a mutable provider display name.

        Resolves the receipt SCOPED to this workspace (a receipt cannot be replayed
        across workspaces), refuses a provider mismatch (no cross-provider replay),
        then deletes exactly the ``clone_id`` the receipt names, passing the receipt
        to the adapter as defense-in-depth binding. On an adapter delete failure it
        records ``cleanup_needed`` on the receipt (for a janitor) and returns
        structured state rather than raising, so a caller in a ``finally`` never has
        its primary exception masked. An unresolved/unregistered receipt is a hard
        refusal (ValueError): an unregistered clone id is never deletable."""
        if not receipt_id and trial_id:
            row = self.registry.find_clone_receipt(workspace_id, trial_id)
            receipt_id = dict(row)["receipt_id"] if row else None
        row = self.registry.get_clone_receipt(workspace_id, receipt_id) if receipt_id else None
        if not row:
            ref = receipt_id or (f"trial {trial_id!r}" if trial_id else "the request")
            raise ValueError(
                f"refusing clone cleanup: no durable clone receipt for {ref} in "
                f"workspace {workspace_id!r}; deletion requires a receipt this tool "
                "recorded when the staging clone was created (an unregistered clone "
                "id -- e.g. a production assistant -- is never deletable).")
        receipt = dict(row)
        provider = receipt.get("provider")
        adapter_stack = getattr(adapter, "stack", None) or type(adapter).__name__
        if provider and adapter_stack and str(provider) != str(adapter_stack):
            raise ValueError(
                f"refusing clone cleanup: receipt {receipt.get('receipt_id')!r} is "
                f"for provider {provider!r}, not {adapter_stack!r} (a receipt cannot "
                "be replayed across providers).")
        clone_id = receipt.get("clone_id")
        rid = receipt.get("receipt_id")
        if not clone_id:
            # No live clone was ever created (e.g. apply_variant raised before the
            # provider create): nothing to delete; resolve the receipt.
            self.registry.set_clone_receipt_state(workspace_id, rid, "deleted")
            return {"deleted": False, "reason": "no clone id on receipt", "receipt_id": rid}
        try:
            outcome = adapter.delete_clone(clone_id, receipt=receipt)
        except Exception as exc:  # noqa: BLE001 - record, never mask a caller's primary error
            self.registry.set_clone_receipt_state(
                workspace_id, rid, "cleanup_needed",
                detail_json=json.dumps({"error": str(exc)}))
            return {"deleted": False, "receipt_id": rid, "cleanup_error": str(exc)}
        ok = outcome.get("deleted") if isinstance(outcome, dict) else outcome
        self.registry.set_clone_receipt_state(
            workspace_id, rid, "deleted" if ok else "cleanup_needed")
        return {"deleted": bool(ok), "receipt_id": rid, "result": outcome}

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
        import json as _json
        import os as _os

        from .. import core as _core
        work_dir = work_dir or _os.path.join(self.home, "clones", trial_id)
        _os.makedirs(work_dir, exist_ok=True)
        adapter_name = (getattr(adapter, "stack", None) or getattr(adapter, "name", None)
                        or type(adapter).__name__)
        # 0) PRECOMMIT the manifest BEFORE any capture, so the fixture universe and
        # nonce are fixed ahead of the results and the capture receipts can bind to
        # this trial's nonce.
        created = self.experiment_create(
            workspace_id, agent_id, trial_id=trial_id,
            battery_env=battery_env or before_env, policy=policy, min_n=min_n)
        nonce = created.get("nonce")
        name_marker = f"hotato-staging-{trial_id}"
        receipt = None
        result = None
        try:
            # 1) clone (clone-only; production untouched) + apply the variant to the clone
            clone = adapter.clone_agent(source_ref, name=name_marker)
            # 1a) Persist a DURABLE clone receipt the MOMENT a staging resource may
            # exist -- BEFORE any downstream step that could raise. It is the ONLY
            # thing that later AUTHORIZES delete_clone (a mutable display-name marker
            # is not sufficient), and it is the janitor-retry record for a leaked
            # clone if apply/scenario/capture/score/recompute raises.
            receipt = self._record_clone_receipt(
                workspace_id, trial_id=trial_id, adapter=adapter, source_ref=source_ref,
                clone=clone, applied=None, nonce=nonce, name_marker=name_marker)
            applied = adapter.apply_variant(clone, variant)
            # 1b) refine the receipt with the concrete provider clone id: for a live
            # adapter the real clone is created HERE (apply_variant), so its id is
            # only now known; the delete target must be that id.
            receipt = self._record_clone_receipt(
                workspace_id, trial_id=trial_id, adapter=adapter, source_ref=source_ref,
                clone=clone, applied=applied, nonce=nonce, name_marker=name_marker)
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
            # 3b) EMIT a signed capture receipt per fixture, binding the fresh recording
            # to this trial + nonce + its decoded-PCM identity. A receipt is machine-
            # attested ONLY when a signing key is present (HOTATO_ATTEST_KEY or
            # ~/.hotato/attest.key); this is what lifts a clean paired result to the
            # ATTESTED tier (runner-verified fresh recapture). Without a key the pair
            # is still operator-asserted -- never silently upgraded.
            key = _receipt.load_key()
            receipts = None
            if key is not None:
                receipts = {}
                for _ev in after_env.get("events", []):
                    _sides = (_ev.get("audio_provenance") or {}).get("sides") or []
                    _pcm = _recompute._side_pcm(_sides)
                    _raw = _sides[0].get("sha256") if _sides else None
                    if not _pcm:
                        continue
                    _stim = _recompute._caller_pcm(after_dir, _ev)
                    _fkey = _manifest.fixture_key(_ev)
                    receipts[_fkey] = _receipt.build_receipt(
                        trial_id=trial_id, nonce=nonce, recording_locator=_fkey,
                        raw_sha256=_raw or "", pcm_sha256=_pcm, runner=f"{adapter_name}-runner",
                        agent_id=agent_id, deployment_id=clone,
                        scenario_stimulus_hash=_stim, channel_layout="stereo",
                        adapter=adapter_name, key=key)
            # 4) recompute the before/after trial against the COMMITTED manifest, with
            # the capture receipts (runner-attested when signed).
            result = self.experiment_run(
                workspace_id, agent_id, trial_id=trial_id,
                manifest_ref=created["manifest_digest"], before_env=before_env,
                before_dir=before_dir, after_env=after_env, after_dir=after_dir,
                min_n=min_n, capture_receipts=receipts, capture_context="operator")
            result["clone"] = {"ref": clone, "config_hash": applied.get("config_hash"),
                               "cleaned_up": False, "attested": bool(receipts)}
        finally:
            # 5) OUTER cleanup: delete the staging clone regardless of how we got
            # here (apply/scenario/capture/score/recompute may all raise), so a
            # provider clone is never leaked. Deletion is GOVERNED by the durable
            # receipt (never a raw clone id / display name). The primary exception,
            # if any, propagates through this finally UNMASKED: cleanup_clone never
            # raises on an adapter delete failure -- it records cleanup_needed on
            # the receipt for a janitor -- and any residual error here is caught and
            # recorded separately rather than replacing the caller's real error.
            if receipt is not None:
                try:
                    out = self.cleanup_clone(
                        workspace_id, adapter=adapter, receipt_id=receipt["receipt_id"])
                    if result is not None:
                        result["clone"]["cleaned_up"] = bool(out.get("deleted"))
                except Exception:  # noqa: BLE001 - cleanup failure never masks the primary error
                    try:
                        self.registry.set_clone_receipt_state(
                            workspace_id, receipt["receipt_id"], "cleanup_needed")
                    except Exception:  # noqa: BLE001
                        pass
        return result

    # --- bounded experiment engine (propose -> run -> rank; §9.4-9.6) ---------
    def experiment_propose(self, workspace_id, agent_id, *, intent, current_config=None,
                           max_variants=6, trial_id=None) -> dict:
        """Generate a BOUNDED set of config variants (baseline + lower/higher/
        adjacent/two-param, capped ~max_variants) from the typed catalogue, each
        with an expected-effects block stated BEFORE execution (plan §9.4). Persists
        the proposed variants to the registry; runs nothing, deploys nothing."""
        from . import variants as _variants
        row = self.registry._one(
            "SELECT stack FROM agents WHERE workspace_id=? AND agent_id=?",
            (workspace_id, agent_id))
        stack = (dict(row).get("stack") if row else None) or "vapi"
        vs = _variants.generate_variants(stack=stack, intent=intent,
                                         current_config=current_config or {},
                                         max_variants=max_variants)
        for v in vs:
            self.registry.add_variant(
                workspace_id, v["variant_id"], trial_id=trial_id, agent_id=agent_id,
                config_delta_json=json.dumps(v.get("config_delta")),
                expected_json=json.dumps(v.get("expected")))
        return {"agent_id": agent_id, "stack": stack, "intent": intent,
                "count": len(vs), "variants": vs}

    @staticmethod
    def _variant_config_delta(variant) -> dict:
        """Turn a catalogue variant's concrete field/to into the {key: value} delta
        an adapter's apply_variant consumes."""
        delta = variant.get("config_delta")
        if not isinstance(delta, dict):
            return {}
        fld, to = delta.get("field"), delta.get("to")
        if fld is None or to is None:
            return {}
        key = delta.get("source_key") or (delta.get("canonical") or {}).get("key") or fld.split(".")[-1]
        return {key: to}

    @staticmethod
    def _pareto_rank(items) -> list:
        """Rank eligible variants on a Pareto view over VISIBLE components (plan
        §9.6): yield/hold success up, regressions down, evidence tier up. No blended
        Hotato score -- the component metrics stay inspectable."""
        def metric(x, k, default=0):
            return (x.get("metrics") or {}).get(k) or default
        def dominates(a, b):
            ge = (metric(a, "yield_success") >= metric(b, "yield_success")
                  and metric(a, "hold_success") >= metric(b, "hold_success")
                  and (metric(a, "evidence_tier")) >= (metric(b, "evidence_tier"))
                  and metric(a, "regressions") <= metric(b, "regressions"))
            gt = (metric(a, "yield_success") > metric(b, "yield_success")
                  or metric(a, "hold_success") > metric(b, "hold_success")
                  or (metric(a, "evidence_tier")) > (metric(b, "evidence_tier"))
                  or metric(a, "regressions") < metric(b, "regressions"))
            return ge and gt
        out = []
        for x in items:
            dom = sum(1 for y in items if y is not x and dominates(y, x))
            out.append({**x, "pareto_front": dom == 0, "dominated_by": dom})
        out.sort(key=lambda x: (not x["pareto_front"],
                                -(metric(x, "yield_success") + metric(x, "hold_success")),
                                metric(x, "regressions"), x["variant_id"]))
        for i, x in enumerate(out):
            x["rank"] = i + 1
        return out

    def experiment_run_all(self, workspace_id, agent_id, *, adapter, source_ref, intent,
                           scenarios, before_env, before_dir, current_config=None,
                           max_variants=6, min_n=1, base_trial_id="exp") -> dict:
        """The bounded experiment engine end to end (plan §9.4-9.6): propose variants,
        run EACH as a manifest-bound clone trial (recompute-from-audio, capture
        receipts), gate them, and Pareto-rank the eligible ones on visible
        components. Recommends a winner; NEVER deploys. Fully offline with the mock
        adapter; live adapters refuse the networked steps without credentials."""
        proposed = self.experiment_propose(workspace_id, agent_id, intent=intent,
                                           current_config=current_config,
                                           max_variants=max_variants, trial_id=base_trial_id)
        results = []
        for v in proposed["variants"]:
            tid = f"{base_trial_id}-{v['variant_id']}"
            cd = self._variant_config_delta(v)
            try:
                r = self.experiment_clone_run(
                    workspace_id, agent_id, trial_id=tid, adapter=adapter,
                    source_ref=source_ref, variant={"config_delta": cd},
                    scenarios=scenarios, before_env=before_env, before_dir=before_dir,
                    battery_env=before_env, min_n=min_n)
            except Exception as exc:  # noqa: BLE001 - a runner failure is a per-variant refusal
                r = {"verdict": "refused", "refusal": {"kind": "runner_error", "reason": str(exc)},
                     "evidence_tier": 0, "metrics": None}
            eligible = (r.get("verdict") == "improved") and not r.get("refusal")
            results.append({"variant_id": v["variant_id"], "trial_id": tid, "kind": v.get("kind"),
                            "eligible": eligible, "verdict": r.get("verdict"),
                            "metrics": r.get("metrics") or {}, "expected": v.get("expected"),
                            "config_delta": v.get("config_delta")})
        ranked = self._pareto_rank([x for x in results if x["eligible"]])
        rank_by_id = {x["variant_id"]: x["rank"] for x in ranked}
        for x in results:
            self.registry.add_variant(
                workspace_id, x["variant_id"], trial_id=x["trial_id"], agent_id=agent_id,
                config_delta_json=json.dumps(x.get("config_delta")),
                expected_json=json.dumps(x.get("expected")),
                observed_json=json.dumps(x.get("metrics")),
                eligible=1 if x["eligible"] else 0, rank=rank_by_id.get(x["variant_id"]))
        winner = ranked[0] if ranked else None
        return {"agent_id": agent_id, "intent": intent, "proposed": proposed["count"],
                "eligible": len(ranked), "variants": results, "ranked": ranked,
                "winner": winner,
                "recommendation": (
                    f"variant {winner['variant_id']} leads the Pareto front "
                    f"({winner['metrics'].get('yield_success')} yield / "
                    f"{winner['metrics'].get('hold_success')} hold, "
                    f"{winner['metrics'].get('regressions')} regressions); approval is "
                    "required before any deployment." if winner else
                    "no variant cleared the hard gates; nothing is recommended."),
                "note": "component metrics stay visible; no single Hotato score."}

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

    # --- privacy / retention / deletion / redaction (plan §14) ---------
    def set_retention(self, workspace_id, recording_id, *, consent_basis, allowed_purposes,
                      retention_days=None, pii_class="unknown", legal_hold=False,
                      export_allowed=False, playback_allowed=False, transcript_allowed=False,
                      hosted_egress_allowed=False, public_sharing=False) -> dict:
        """Attach a retention/consent policy to a recording (plan §14). Conservative
        opt-in defaults; a legal hold blocks later expiry/deletion."""
        pol = _privacy.retention_policy(
            consent_basis=consent_basis, allowed_purposes=list(allowed_purposes),
            retention_days=retention_days, pii_class=pii_class, legal_hold=legal_hold,
            export_allowed=export_allowed, playback_allowed=playback_allowed,
            transcript_allowed=transcript_allowed, hosted_egress_allowed=hosted_egress_allowed,
            public_sharing=public_sharing)
        self.registry.set_recording_privacy(workspace_id, recording_id,
                                            retention_policy_json=json.dumps(pol),
                                            pii_class=pii_class)
        return {"recording_id": recording_id, "policy": pol}

    def _recording_policy(self, workspace_id, recording_id):
        rec = self.registry._one(
            "SELECT * FROM recordings WHERE workspace_id=? AND recording_id=?",
            (workspace_id, recording_id))
        if not rec:
            raise ValueError(f"no recording {recording_id!r} in workspace {workspace_id!r}")
        rec = dict(rec)
        try:
            pol = json.loads(rec.get("retention_policy_json") or "{}")
        except (TypeError, ValueError):
            pol = {}
        return rec, pol

    def delete_recording(self, workspace_id, recording_id, *, reason, actor) -> dict:
        """Delete a recording's stored audio and leave a durable deletion receipt
        (plan §14). A legal hold blocks deletion. The audit trail survives the audio.

        The content-addressed store is a SHARED, deduplicated blob pool: identical
        audio ingested into two workspaces collapses to ONE physical blob that both
        reference. Deleting one workspace's recording must therefore REVOKE that
        workspace's pointer (``artifact_digest`` -> NULL, ``pii_class`` -> deleted)
        and unlink the blob ONLY when no other live reference survives anywhere --
        any workspace, any owning table (recordings/contracts/trials/conversations
        and the other reference-edge columns). Unlinking a still-referenced blob
        would destroy another workspace's evidence, so the physical delete is
        gated on ``referencing_workspaces`` being empty AFTER this pointer is
        revoked. ``blob_removed`` reports whether the shared bytes were actually
        unlinked. CAS lineage is never consulted for this decision (provenance,
        not an ACL)."""
        rec, pol = self._recording_policy(workspace_id, recording_id)
        if pol.get("legal_hold"):
            # A legal hold blocks even POINTER revocation: nothing about the
            # recording's evidence may be torn down while the hold stands.
            return {"recording_id": recording_id, "deleted": False,
                    "blocked_by_legal_hold": True}
        receipt = _privacy.deletion_receipt(
            subject_id=recording_id, subject_kind="recording",
            pcm_sha256=rec.get("pcm_sha256") or "", reason=reason, actor=actor,
            at=self.registry._now())
        dig = rec.get("artifact_digest")
        # 1) Revoke THIS workspace's pointer (durable) and mark it deleted, and
        #    COMMIT before the reference query below so it sees the revoked state.
        self.registry.clear_recording_artifact(workspace_id, recording_id)
        self.registry.set_recording_privacy(workspace_id, recording_id, pii_class="deleted")
        # 2) GC the shared blob only when no other live reference remains. Never
        #    unlink a blob another workspace/table still roots (P0 data-loss fix).
        blob_removed = False
        if dig:
            survivors = self.registry.referencing_workspaces(dig)
            if not survivors:
                blob_removed = self.store.remove(dig)
        self.registry.add_attestation(
            workspace_id, f"del-{_short(recording_id)}", subject_kind="recording",
            subject_id=recording_id, signer=actor,
            subject_digest=receipt["receipt_digest"], statement="deletion-receipt")
        return {"recording_id": recording_id, "deleted": True,
                "blob_removed": blob_removed, "receipt": receipt}

    def redact_recording(self, workspace_id, recording_id, spans_sec, *, actor) -> dict:
        """Produce a DERIVED redacted copy (silenced spans) with a new PCM hash and
        parent lineage (plan §14). The derived clip is never the original evidence;
        its evidence statement is explicitly downgraded."""
        rec, _pol = self._recording_policy(workspace_id, recording_id)
        dig = rec.get("artifact_digest")
        if not dig:
            raise ValueError(f"recording {recording_id!r} has no stored audio to redact")
        import os as _os
        import tempfile as _tf
        # Verified read: redaction derives a new clip from THIS evidence; a
        # poisoned source blob must abort rather than seed a derived recording.
        data = self.store.get_bytes(dig, verify=True)
        src = _tf.NamedTemporaryFile(suffix=".wav", delete=False)
        out = _tf.NamedTemporaryFile(suffix=".wav", delete=False)
        src.write(data); src.flush(); src.close(); out.close()
        try:
            record = _privacy.redact_audio(src.name, list(spans_sec), out.name)
            derived_digest = self.store.put_file(out.name, kind="redacted-recording",
                                                 workspace_id=workspace_id,
                                                 meta={"parent": recording_id})
        finally:
            for f in (src.name, out.name):
                try:
                    _os.remove(f)
                except OSError:
                    pass
        record["derived_digest"] = derived_digest
        record["parent_recording_id"] = recording_id
        record["actor"] = actor
        return record

    def approve_trial(self, workspace_id, trial_id, *, approver, note=None) -> dict:
        """Record a HUMAN approval decision on a trial recommendation. Recorded only
        -- it NEVER deploys (no auto-deploy in this release; plan §10/§16).

        Approval is GATED on the trial's OWN evidence through the SAME shared
        eligibility predicate the canary gate uses
        (:func:`fleet.canary.trial_eligibility`), so the two authorization layers
        can never disagree: the verdict must be EXACTLY 'improved', the evidence
        tier must reach the documented paired minimum, and every recorded hard gate
        must be green. Any other verdict -- refused, inconclusive, created, an
        unknown string, or missing -- and any tier below the paired floor is
        REJECTED here as a structured refusal; NO approval decision row is written,
        rather than silently recorded as approved. An unknown trial is still a
        usage error (ValueError)."""
        from .canary import MIN_ELIGIBLE_TIER, trial_eligibility
        trial = self.registry._one(
            "SELECT * FROM trials WHERE workspace_id=? AND trial_id=?", (workspace_id, trial_id))
        if not trial:
            raise ValueError(f"no trial {trial_id!r} in workspace {workspace_id!r}")
        trial = dict(trial)
        verdict = trial.get("verdict")
        tier = trial.get("evidence_tier")
        # The trial's recorded hard-gate flags live on its recommendation decision
        # (approved=0, written at experiment_run time); every flag must be green.
        # A directly-inserted trial with no such decision row simply has no flags
        # to check (its verdict/tier still gate it).
        hard_flags = None
        dec = self.registry._one(
            "SELECT hard_gate_json FROM decisions WHERE workspace_id=? AND trial_id=? "
            "AND approved=0 AND hard_gate_json IS NOT NULL ORDER BY created_at DESC LIMIT 1",
            (workspace_id, trial_id))
        if dec:
            try:
                hard_flags = json.loads(dict(dec).get("hard_gate_json") or "null")
            except (TypeError, ValueError):
                hard_flags = None
            if not isinstance(hard_flags, dict):
                hard_flags = None
        elig = trial_eligibility(verdict=verdict, evidence_tier=tier, hard_gate_flags=hard_flags)
        if not elig["eligible"]:
            return {
                "trial_id": trial_id, "approved": False, "refused": True,
                "verdict": verdict, "evidence_tier": tier, "approver": approver,
                "reason": (
                    "approval rejected: " + "; ".join(elig["reasons"])
                    + f"; approval requires an 'improved' verdict at evidence tier "
                    f">= {MIN_ELIGIBLE_TIER} with all hard gates green (a refused, "
                    "inconclusive, or below-paired trial has no green paired proof "
                    "to deploy)."),
            }
        self.registry.add_decision(
            workspace_id, f"approval-{_short(trial_id)}", trial_id=trial_id,
            recommendation=f"approved by {approver}" + (f": {note}" if note else ""),
            hard_gate_json=json.dumps({"approver": approver, "note": note}), approved=1)
        return {"trial_id": trial_id, "approved": True, "approver": approver,
                "note": "recorded approval only; no deployment is performed in this release."}

    # --- status / rollup -----------------------------------------------
    def status(self, workspace_id) -> dict:
        return {"workspace_id": workspace_id, "mode": "local", "home": self.home,
                "counts": self.registry.counts(workspace_id),
                "jobs": self.jobs.stats(workspace_id)}


__all__ = ["FleetAPI"]
