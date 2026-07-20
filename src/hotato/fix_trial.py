"""``hotato fix trial``: S4 -- compose the shipped, already-guarded primitives
into ONE before/after proof that a candidate fix actually holds.

This adds NO new scoring engine and NO new networked path. Every number here
is something ``hotato apply``, ``hotato verify``, ``hotato contract verify``,
``hotato explain``, or the Evidence Kernel already measures. ``run_trial`` (and
the CLI wrapper it backs, ``hotato fix trial``):

1. Evaluates the candidate patch through ``hotato apply``'s EXACT offline gate
   (:func:`hotato.apply.build_apply`, ``clone=True``): refusal-first on the
   both-axes threshold funnel, opposite-risk-battery-required, clone-only. fix
   trial never creates a clone itself and never touches the network -- this
   module never imports :func:`hotato.apply.create_clone` or
   :func:`hotato.apply._http_json`, so it carries the SAME clone-only,
   production-unmutatable guarantee apply's own dry run gives.
2. Pins an immutable trial MANIFEST from the battery (:func:`hotato.manifest`):
   one scorer, one policy, the complete fixture universe, each fixture's onset
   and scripted-stimulus identity. The nonce is DETERMINISTIC (derived from the
   battery + patch), so a re-run over the same inputs pins the same manifest.
3. RECOMPUTES both sides from the on-disk audio under that manifest
   (:func:`hotato.recompute.recompute_trial`): it never reads a stored
   ``verdict.passed`` to decide a result; it reads it only to DETECT tampering.
   The recompute HARD-REFUSES a re-scored verdict (stored != recomputed audio),
   the same conversation re-scored (identical decoded PCM), an incomplete
   fixture set, or an after side whose caller stimulus does not match the
   before side.
4. Feeds the RECOMPUTED verdicts into :func:`hotato.verify.verify_sides` (via
   temp envelopes), so every count / claim / verdict_model rule runs UNCHANGED,
   but on trustworthy, audio-derived pass/fail -- never a stored verdict.
5. Classifies the strength of the result on the Evidence Kernel
   (:func:`hotato.evidence.classify`), enriched by a ``hotato`` trust preflight
   (input health + channel mapping). A green ``improved`` requires the evidence
   tier to reach PAIRED; anything weaker downgrades to ``inconclusive``.
6. Optionally re-verifies a ``--contracts`` directory (another neighbouring-
   cases check) and folds in :func:`hotato.explain.explain`'s root-cause
   attribution for the BEFORE evidence.

The verdict is FAIL-CLOSED, never a soft pass:

* ``improved`` -- verify's claim is supported on the RECOMPUTED verdicts (>=
  ``min_n`` previously-failing fixtures, at least one now passes, nothing
  regressed anywhere in the battery), no contract regressed, ``policy`` (if
  given) passed, the recompute raised no refusal, AND the evidence tier reaches
  PAIRED (both sides recomputed from audio under one pinned policy, input clean,
  channel mapping confirmed).
* ``regressed`` -- any fixture regressed on the recomputed verdicts, a contract
  regressed, or the policy failed. Exits the same non-zero code as
  ``inconclusive``.
* ``inconclusive`` -- neither improved nor regressed: too few previously-failing
  fixtures, nothing that used to fail now passes, or verify passes but the
  evidence tier is below PAIRED (a caution/suspect input, a missing recompute).
  INCONCLUSIVE IS NOT A PASS.
* ``refused`` -- apply's both-axes threshold-funnel gate fires before any
  before/after evidence is read; OR the recompute hard-refuses (a tampered
  verdict, a re-scored conversation, an incomplete fixture set, a mismatched
  stimulus). The refusal is a FEATURE: every path shares apply's distinct exit
  code.

Hotato does not infer intent and does not prove authorization, identity,
compliance, or policy safety. Every number here is a measurement; verify's
coincidence-not-causation rule still applies throughout. This is an offline
tool: a user who controls every input can always lie to themselves. The guard's
job is narrower: recompute what can be recomputed from the actual files, make
the motivated failure modes impossible or loud, and state exactly what was and
was NOT verified.
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import tempfile
from datetime import datetime, timezone
from typing import Optional

from . import apply as _apply
from . import contract as _contract
from . import evidence as _evidence
from . import explain as _explain
from . import manifest as _manifest
from . import recompute as _recompute
from . import report as _report
from . import verify as _verify

__all__ = [
    "SCHEMA",
    "VERDICT_IMPROVED",
    "VERDICT_REGRESSED",
    "VERDICT_INCONCLUSIVE",
    "VERDICT_REFUSED",
    "EXIT_IMPROVED",
    "EXIT_FAIL",
    "EXIT_REFUSED",
    "run_trial",
    "render_text",
    "render_html",
]

SCHEMA = "hotato.fix_trial.v1"

VERDICT_IMPROVED = "improved"
VERDICT_REGRESSED = "regressed"
VERDICT_INCONCLUSIVE = "inconclusive"
VERDICT_REFUSED = "refused"

# CI exit codes. 0 is the ONLY passing outcome; inconclusive and regressed
# share the same fail-closed code (1) so a script cannot mistake "we could
# not tell" for green. 3 mirrors hotato apply's own refusal code exactly.
EXIT_IMPROVED = 0
EXIT_FAIL = 1
EXIT_REFUSED = _apply.REFUSAL_EXIT_CODE  # 3

_HONEST = (
    "hotato fix trial never creates or mutates a clone itself and never "
    "touches production config: it evaluates the same offline gate hotato "
    "apply --clone enforces, then RECOMPUTES the before/after verdicts from the "
    "on-disk audio under one pinned manifest. hotato reports coincidence, not "
    "causation."
)

# The recompute proves the specific fresh capture scored above, at the revision
# it was captured from; it does not certify a later deploy or every future call,
# and it does not re-run itself. Rendered wherever the evidence section renders,
# independent of the final verdict, so a reader sees the limit even on an
# improved run.
_PROVENANCE_CAUTION = (
    "Provenance caution: this proves the specific fresh capture scored "
    "above, at the revision it was captured from. It does not certify a "
    "later deploy or every future call, and it does not re-run itself; "
    "recapture again after the next change."
)

# fix trial calls apply.build_apply (never apply.create_clone), on every path,
# every verdict: the "apply" step this trial evaluates is ALWAYS a dry-run
# preview of the patch, never an execution against a real clone or agent. The
# receipt is rendered next to the verdict in every surface.
_APPLY_RECEIPT_NOTE = (
    "this fix trial evaluated a DRY-RUN patch proposal; it does not attest "
    "that the change was applied to a clone or an agent."
)


def _now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


# --- attribution: reuse hotato explain on the BEFORE evidence ---------------

def _attribution_for(before: str) -> dict:
    """Best-effort root-cause attribution for the BEFORE evidence, reusing
    ``hotato explain`` exactly (no new scoring). ``before`` is a single run
    envelope path or a directory of them; every ``.json`` file explain can read
    contributes its own explanation. A file explain cannot read degrades to a
    recorded unknown -- the timing proof above never depends on attribution."""
    paths = [before]
    if os.path.isdir(before):
        paths = [
            os.path.join(before, name)
            for name in sorted(os.listdir(before))
            if name.endswith(".json")
        ]

    explanations, unreadable = [], []
    for p in paths:
        try:
            explanations.append(_explain.explain(p))
        except (ValueError, OSError) as exc:
            unreadable.append({"source": p, "reason": str(exc)})

    attributions, refusals, unknowns = [], [], []
    for exp in explanations:
        attributions.extend(exp.get("attributions") or [])
        refusals.extend(exp.get("refusals") or [])
        unknowns.extend(exp.get("unknowns") or [])
    for u in unreadable:
        unknowns.append(f"{u['source']}: could not be explained ({u['reason']})")

    return {
        "schema": _explain.SCHEMA,
        "sources": paths,
        "explanations": explanations,
        "attributions": attributions,
        "refusals": refusals,
        "unknowns": unknowns,
        "unreadable": unreadable,
    }


# --- loading run envelopes from --before / --after / --battery paths ---------

def _load_env(path: str, label: str) -> dict:
    """Load a run envelope (a single ``.json`` file, or a directory of them)
    into ONE envelope dict. Reuses :func:`hotato.verify._load_side` so fix trial
    reads a side EXACTLY the way verify does (a directory merges every
    envelope's events, deduped by fixture id). Raises ValueError (CLI exit 2)
    for unusable input, the same error verify already raises."""
    _envs, events = _verify._load_side(path, label)
    # verify tags each event with its source file; drop that internal marker so
    # the rebuilt envelope round-trips cleanly.
    events = [
        {k: val for k, val in e.items() if k != "_source_file"} for e in events
    ]
    passed = sum(1 for e in events if _verify._passed(e))
    return {
        "tool": "hotato",
        "kind": "suite",
        "offline": True,
        "events": events,
        # is_envelope() requires a summary dict; recompute recounts it on the
        # rebuilt copy, so this is only the loaded-side tally.
        "summary": {
            "events": len(events),
            "passed": passed,
            "failed": len(events) - passed,
        },
    }


def _deterministic_nonce(battery_env: dict, patch: dict,
                         patch_source: Optional[str]) -> str:
    """A REPRODUCIBLE manifest nonce, not randomness (the kernel forbids
    Date/random): sha256 over the canonical battery fixture ids + the patch
    source + the patch body. A re-run over the same battery and patch pins the
    SAME manifest, so the proof is reproducible; a fleet runner that wants a
    real random nonce supplies its own to ``build_manifest``."""
    material = {
        "battery_fixtures": sorted(
            _manifest.fixture_key(ev) for ev in battery_env.get("events", [])
            if isinstance(ev, dict)),
        "patch_source": patch_source,
        "patch": patch,
    }
    return hashlib.sha256(
        _manifest.canonical_json(material).encode("utf-8")).hexdigest()


# --- trust preflight: input health + channel mapping ------------------------
#
# The recompute leaves input_health unknown and floors channel_mapping at
# "inferred" (a recompute-only tier reads MEASURED). A trust preflight -- the
# SAME hardened input-health report `hotato scan` runs -- lifts those to
# clean/confirmed when the audio is genuinely clean, which is what carries the
# tier up to PAIRED. Best-effort + fail-closed: a trust import failure, an
# unresolvable file, or a decode failure contributes NOTHING, so the tier never
# RISES on a guess; only a confirmed-clean preflight lifts it, and any caution
# or possible-swap pulls it DOWN.

_TRUST_HEALTH_ORDER = {"not_scorable": 0, "caution": 1, "clean": 2}


def _event_audio_files(base: Optional[str], event: dict) -> list:
    """``(role, resolved_path)`` for every audio side an event records, resolved
    next to its envelope (only a basename survives capture)."""
    prov = event.get("audio_provenance") or {}
    out = []
    for s in prov.get("sides") or []:
        if not isinstance(s, dict):
            continue
        p = s.get("path")
        if isinstance(p, str) and p:
            name = os.path.basename(p)
            out.append((s.get("role"), os.path.join(base, name) if base else name))
    return out


def _trust_preflight(before_arg: str, after_arg: str, before_env: dict,
                     after_env: dict) -> tuple:
    """Fold the WORST trust result across every resolvable stereo recording on
    both sides into ``(input_health, channel_mapping)``. Returns ``(None, None)``
    when nothing could be inspected, so recompute's honest floors stand."""
    try:
        from . import trust as _trust
    except Exception:  # pragma: no cover - trust is a first-party module
        return None, None

    safe = getattr(_trust, "SAFE_RECOMMENDATION", "eligible for scan")
    before_base = (before_arg if os.path.isdir(before_arg)
                   else os.path.dirname(before_arg)) or "."
    after_base = (after_arg if os.path.isdir(after_arg)
                  else os.path.dirname(after_arg)) or "."

    def _side_reports(base, env):
        out, uninspected = [], 0
        for ev in env.get("events", []):
            if not isinstance(ev, dict) or ev.get("scorable") is False:
                continue
            files = list(_event_audio_files(base, ev))
            stereo = [(role, fp) for role, fp in files
                      if role == "stereo" and os.path.isfile(fp)]
            if not stereo:
                # a dual-mono / non-single-file fixture that recompute WILL still
                # score: its input health was not inspected, so it must not let
                # the proof claim "clean" over audio never health-checked.
                uninspected += 1
                continue
            for role, fp in stereo:
                try:
                    out.append(_trust.trust_report(fp))
                except Exception:  # noqa: BLE001 - a bad file contributes nothing
                    uninspected += 1
        return out, uninspected

    def _health_of(r, *, mapping_confirmed):
        ih = r.get("input_health")
        if ih not in _TRUST_HEALTH_ORDER:
            if not r.get("scorable", False):
                ih = "not_scorable"
            elif r.get("recommendation") == safe:
                ih = "clean"
            else:
                ih = "caution"
        # A caution driven ONLY by the possible-swap heuristic is not a
        # signal-quality problem: a working barge-in fix makes the caller hold
        # the floor longer, which trips "the agent usually holds longer" even
        # though the channel mapping (established by the before side) is
        # unchanged. When the mapping is confirmed and the sole warning is the
        # swap heuristic, the input is clean for the trial.
        if ih == "caution" and mapping_confirmed:
            warnings = r.get("warnings") or []
            swap = bool((r.get("channels") or {}).get("possible_swap"))
            non_swap = [w for w in warnings if "revers" not in w.lower()]
            if swap and not non_swap:
                ih = "clean"
        return ih

    before_reports, before_uninspected = _side_reports(before_base, before_env)
    after_reports, after_uninspected = _side_reports(after_base, after_env)
    if not before_reports and not after_reports:
        return None, None

    # Channel mapping is a property of the capture setup, established by the
    # BEFORE (baseline) side; the after side reuses the same channel indices.
    before_swap = any((r.get("channels") or {}).get("possible_swap") for r in before_reports)
    channel_mapping = "suspect" if before_swap else "confirmed"
    mapping_confirmed = channel_mapping == "confirmed"

    healths = [_health_of(r, mapping_confirmed=mapping_confirmed)
               for r in (before_reports + after_reports)]
    input_health = min(healths, key=lambda h: _TRUST_HEALTH_ORDER.get(h, 0)) if healths else None
    # Any scorable fixture that could not be health-inspected (dual-mono) means
    # the "clean" claim would cover audio never checked: cap at caution.
    if (before_uninspected or after_uninspected) and input_health == "clean":
        input_health = "caution"
    return input_health, channel_mapping


# --- refusal shape ----------------------------------------------------------

def _refusal(headline: str, reason: str, recommended: str, why: str) -> dict:
    """The SAME refusal shape apply's own gate uses (headline / reason /
    recommended / lines / why), so renderers and callers already branching on
    ``t["refusal"]["lines"]`` handle every refusal identically."""
    return {
        "headline": headline,
        "reason": reason,
        "recommended": recommended,
        "lines": [headline, f"Reason: {reason}", f"Recommended: {recommended}"],
        "why": why,
    }


# Headline + recommended action per recompute hard-refusal kind. The reason is
# recompute's own (it names the offending fixtures and the exact failure).
_RECOMPUTE_REFUSAL_META = {
    "score_mismatch": (
        "No fix will be certified from a tampered verdict",
        "recapture the fixture(s) through the applied clone so every stored "
        "verdict is the one hotato recomputes from the audio, and re-run",
    ),
    "same_audio": (
        "No fix will be certified from re-scored audio",
        "recapture the fixture(s) through the applied clone "
        "(hotato apply --clone --yes) and re-run against the new after evidence",
    ),
    "incomplete_fixture_set": (
        "No fix will be certified from an incomplete fixture set",
        "recapture the FULL battery through the applied clone so every pinned "
        "fixture is present on both sides, and re-run",
    ),
    "stimulus_mismatch": (
        "No fix will be certified from a mismatched stimulus",
        "recapture the fixture(s) replaying the SAME caller stimulus the before "
        "side used (a fix changes the agent, not the caller), and re-run",
    ),
}


def _recompute_refusal(rc_refusal: dict) -> dict:
    kind = rc_refusal.get("kind")
    headline, recommended = _RECOMPUTE_REFUSAL_META.get(
        kind, ("No fix will be certified", "recapture through the applied clone "
               "and re-run"))
    reason = rc_refusal.get("reason", "the recompute could not certify the pair")
    return _refusal(headline, reason, recommended, why=reason)


# --- the trial ---------------------------------------------------------------

def run_trial(
    patch: dict,
    *,
    name: Optional[str],
    before: str,
    after: str,
    battery: Optional[str] = None,
    contracts: Optional[str] = None,
    policy: Optional[dict] = None,
    min_n: int = _verify.DEFAULT_MIN_N,
    patch_source: Optional[str] = None,
    plan: Optional[dict] = None,
    agent_id: Optional[str] = None,
    deployment_id: Optional[str] = None,
    source_config_hash: Optional[str] = None,
    candidate_config_hash: Optional[str] = None,
) -> dict:
    """Run the S4 before/after proof. Pure and OFFLINE: never creates a clone
    and never touches the network. Raises ``ValueError`` / ``OSError`` (CLI exit
    2) for anything unusable -- the SAME errors ``apply`` / ``verify`` /
    ``contract verify`` already raise, never a new error class.

    ``agent_id`` / ``deployment_id`` / ``source_config_hash`` /
    ``candidate_config_hash`` are OPTIONAL deployment-identity fields plumbed
    straight into the pinned trial manifest (:func:`hotato.manifest.build_manifest`).
    They all default to ``None``, which reproduces the previous manifest body
    byte-for-byte (build_manifest already wrote these keys as ``None``), so an
    existing caller sees no change.

    They exist to support a RELEASE proof, which is strictly stronger than the
    paired proof this function's verdict already gates on: a release proof
    additionally requires the candidate deployment identity to be
    config-hash-bound (see :func:`hotato.evidence.meets_release_proof`), so a
    fresh scored after side is bound to the intended agent revision, not merely
    to a pinned manifest. This function only PLUMBS caller-supplied identity into
    the manifest; the actual provider-fetch of the candidate's true deployment
    identity is a separate, operator-gated LIVE step, and this call does NOT
    itself contact any provider, place a call, or touch the network."""
    battery_dir = battery or before

    # 1. The exact apply gate, clone-only, refusal-first, offline. If this is
    # the both-axes threshold funnel, it refuses BEFORE any before/after
    # evidence is even read (mirrors apply's own refusal-first ordering).
    apply_result = _apply.build_apply(
        patch, name=name, clone=True, battery_dir=battery_dir,
        patch_source=patch_source, plan=plan,
    )

    apply_dry_run = apply_result.get("dry_run", True)
    apply_created = bool(apply_result.get("created", False))
    apply_applies_change = bool(apply_result.get("applies_change", False))

    base = {
        "tool": "hotato",
        "kind": "fix-trial",
        "schema": SCHEMA,
        "schema_version": "1",
        "offline": True,
        "created_at": _now_iso(),
        "clone_only": True,
        "production_apply_supported": False,
        "patch_source": patch_source,
        "name": name,
        "before": before,
        "after": after,
        "battery": battery_dir,
        "contracts": contracts,
        "min_n": min_n,
        "apply_dry_run": apply_dry_run,
        "apply_created": apply_created,
        "apply_applies_change": apply_applies_change,
        "apply_receipt_note": _APPLY_RECEIPT_NOTE,
    }

    if apply_result.get("refused"):
        refusal = apply_result["refusal"]
        return {
            **base,
            "apply": apply_result,
            "verdict": VERDICT_REFUSED,
            "exit_code": EXIT_REFUSED,
            "verify": None,
            "contract_verify": None,
            "attribution": None,
            "refusal": refusal,
            "refusal_kind": "threshold_funnel",
            "evidence": None,
            "evidence_sentence": None,
            "recompute": None,
            "conclusion": (
                refusal["headline"] + ": " + refusal["reason"] + ". "
                "Recommended: " + refusal["recommended"] + "."
            ),
            "honest": _HONEST,
        }

    # 2. Load both sides + the opposite-risk battery into envelopes.
    before_env = _load_env(before, "before")
    after_env = _load_env(after, "after")
    battery_env = _load_env(battery_dir, "battery")

    # 3. Pin an immutable manifest (deterministic nonce; no randomness): one
    # scorer, one policy, the complete fixture universe, each onset + stimulus.
    nonce = _deterministic_nonce(battery_env, patch, patch_source)
    man = _manifest.build_manifest(
        battery_env, trial_id=name or "trial", nonce=nonce, policy=policy,
        min_n=min_n,
        # Optional deployment identity, plumbed additively. All default None,
        # which build_manifest already wrote as None -- so the manifest body is
        # byte-identical to before when a caller supplies nothing. Supplying a
        # config-hash-bound candidate identity is what a RELEASE proof requires
        # on top of this paired proof (hotato.evidence.meets_release_proof); the
        # provider-fetch of that identity stays a separate, operator-gated live
        # step and no provider is contacted here.
        agent_id=agent_id,
        deployment_id=deployment_id,
        source_config_hash=source_config_hash,
        candidate_config_hash=candidate_config_hash,
    )

    # 4. RECOMPUTE both sides from the on-disk audio under the manifest. This
    # never trusts a stored verdict; it re-derives every verdict and hard-refuses
    # a tampered verdict, a re-scored conversation, an incomplete fixture set, or
    # a mismatched caller stimulus.
    rc = _recompute.recompute_trial(before_env, before, after_env, after, man)

    # 5. Enrich the evidence vector with a trust input-health preflight, then
    # re-classify for the FINAL tier (input clean + channel confirmed lifts a
    # recompute-only MEASURED tier to PAIRED; a caution/suspect pulls it down).
    vector = dict(rc["evidence"]["vector"])
    input_health, channel_mapping = _trust_preflight(
        before, after, before_env, after_env)
    if input_health is not None:
        vector["input_health"] = input_health
    if channel_mapping is not None:
        vector["channel_mapping"] = channel_mapping
    evidence = _evidence.classify(vector)

    # 6. Feed the RECOMPUTED verdicts into verify_sides unchanged: write the
    # rebuilt envelopes to temp JSON and run the existing rollup on THEM, so
    # every count / claim / verdict_model rule runs on trustworthy pass/fail.
    tmpdir = tempfile.mkdtemp(prefix="hotato-fix-trial-")
    try:
        tmp_before = os.path.join(tmpdir, "before.json")
        tmp_after = os.path.join(tmpdir, "after.json")
        with open(tmp_before, "w", encoding="utf-8") as fh:
            json.dump(rc["before_rebuilt"], fh)
        with open(tmp_after, "w", encoding="utf-8") as fh:
            json.dump(rc["after_rebuilt"], fh)
        v = _verify.verify_sides(tmp_before, tmp_after, min_n=min_n)
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)

    policy_result = None
    if policy is not None:
        policy_result = _verify.evaluate_policy(v, policy)
        v["policy"] = policy_result

    # contract verify + attribution run on the ORIGINAL labelled inputs. A bad /
    # empty --contracts is a usage error (ValueError propagates, CLI exit 2).
    cv = _contract.verify_contracts(contracts) if contracts else None
    contract_regressed = bool(cv and cv["summary"]["failed"] > 0)
    attribution = _attribution_for(before)

    vm = _verify.verdict_model(v)
    regressed_any = bool(v["regressions"])
    policy_failed = policy_result is not None and not policy_result["passed"]

    # 7. Verdict decision. A recompute refusal fires BEFORE an improved (a
    # tampered / re-scored / incomplete / mismatched pair is never a fix). A
    # green verify claim additionally requires the evidence tier to reach PAIRED;
    # below that it downgrades to inconclusive (fail-closed, never a soft pass).
    refusal = None
    refusal_kind = None
    if rc["refusal"] is not None:
        verdict = VERDICT_REFUSED
        refusal = _recompute_refusal(rc["refusal"])
        refusal_kind = rc["refusal"]["kind"]
    elif regressed_any or contract_regressed or policy_failed:
        verdict = VERDICT_REGRESSED
    elif vm["passed"]:
        if evidence["tier"] >= _evidence.TIER_PAIRED:
            verdict = VERDICT_IMPROVED
        else:
            verdict = VERDICT_INCONCLUSIVE
    else:
        verdict = VERDICT_INCONCLUSIVE

    # 8. Nested-claim suppression (rank 4): once the parent is not IMPROVED, the
    # embedded verify claim must not read positive to ANY consumer -- force the
    # data itself, not just an annotation.
    if verdict != VERDICT_IMPROVED:
        v["claim"]["supported"] = False
        v["claim"]["superseded_by"] = verdict

    exit_code = (
        EXIT_REFUSED if verdict == VERDICT_REFUSED
        else EXIT_IMPROVED if verdict == VERDICT_IMPROVED
        else EXIT_FAIL
    )
    recompute_summary = {
        "flags": rc["flags"],
        "coverage": rc["coverage"],
        "manifest_hash": rc["manifest_hash"],
    }
    conclusion = _conclusion(verdict, v, cv, policy_result, contract_regressed,
                             evidence, rc["refusal"])

    return {
        **base,
        "apply": apply_result,
        "verdict": verdict,
        "exit_code": exit_code,
        "verify": v,
        "contract_verify": cv,
        "attribution": attribution,
        "refusal": refusal,
        "refusal_kind": refusal_kind,
        "evidence": evidence,
        "evidence_sentence": _evidence.one_sentence(evidence),
        "recompute": recompute_summary,
        "conclusion": conclusion,
        "honest": _HONEST,
    }


def _conclusion(verdict, v, cv, policy_result, contract_regressed,
                 evidence, rc_refusal=None) -> str:
    ra, ha = v["regression_axis"], v["hold_axis"]
    bits = [
        f"{ra['now_pass']} of {ra['used_to_fail']} previously-failing "
        f"fixture(s) now pass, {ha['still_pass']} of {ha['hold_guards']} "
        "hold fixture(s) still pass",
        f"min-n {v['min_n']}",
    ]
    if v["regressions"]:
        bits.append(f"{len(v['regressions'])} fixture(s) REGRESSED")
    if contract_regressed and cv:
        bits.append(
            f"{cv['summary']['failed']} of {cv['count']} contract(s) regressed")
    if policy_result is not None:
        bits.append("policy " + ("PASSED" if policy_result["passed"] else "FAILED"))
    if evidence is not None:
        bits.append(f"evidence tier {evidence['tier']} ({evidence['headline']})")
    detail = "; ".join(bits)
    if verdict == VERDICT_IMPROVED:
        head = "IMPROVED"
    elif verdict == VERDICT_REGRESSED:
        head = "REGRESSED -- fail-closed, this is not a pass"
    elif verdict == VERDICT_REFUSED:
        head = "REFUSED -- fail-closed, this is not a pass"
    else:
        head = "INCONCLUSIVE -- fail-closed, not a soft pass"
    tail = f"{head}: {detail}. hotato reports coincidence, not causation."
    if verdict == VERDICT_REFUSED and rc_refusal:
        tail += " " + rc_refusal.get("reason", "") + "."
    elif (verdict == VERDICT_INCONCLUSIVE and evidence is not None
          and evidence["tier"] < _evidence.TIER_PAIRED and evidence.get("limited_by")):
        dims = ", ".join(
            f"{d['dimension']}={d['state']}" for d in evidence["limited_by"])
        tail += (" Evidence is below a paired proof; limited by: " + dims + ".")
    return tail


# --- text rendering ------------------------------------------------------

def _evidence_text_lines(t: dict) -> list:
    ev = t.get("evidence")
    if not ev:
        return []
    lines = [
        "",
        "-- evidence: what this before/after proof supports --",
        f"  Evidence: {ev['headline']} (tier {ev['tier']})",
        f"  {t.get('evidence_sentence') or ''}",
    ]
    if ev.get("limited_by") and ev["tier"] < _evidence.TIER_PAIRED:
        dims = ", ".join(
            f"{d['dimension']}={d['state']}" for d in ev["limited_by"])
        lines.append(f"  limited by: {dims}")
    rc = t.get("recompute") or {}
    flags = rc.get("flags") or {}
    lines.append(
        "  recompute flags: "
        f"score_mismatch={flags.get('score_mismatch')} "
        f"same_pcm={flags.get('same_pcm')} "
        f"stimulus_mismatch={flags.get('stimulus_mismatch')} "
        f"unrecomputable={flags.get('unrecomputable')}"
    )
    lines.append(f"  {_PROVENANCE_CAUTION}")
    return lines


def render_text(t: dict) -> str:
    # The apply receipt renders right beside the verdict, on EVERY path
    # (including the apply-gate refusal, which returns early below): fix trial
    # always evaluates a dry-run patch preview, never an applied change.
    lines = [
        f"hotato fix trial [{t['verdict'].upper()}] "
        f"patch={t.get('patch_source')!r} name={t.get('name')!r} "
        f"min-n={t.get('min_n')}",
        f"  apply: dry_run={t['apply_dry_run']} created={t['apply_created']} "
        f"applies_change={t['apply_applies_change']}",
        f"  {t['apply_receipt_note']}",
        f"  {t['conclusion']}",
    ]
    # The apply-gate refusal fires BEFORE any before/after evidence is read
    # (t["verify"] is None): render the minimal canon-refusal-only report. The
    # recompute refusal fires AFTER verify/contract/attribution already ran, so
    # it falls through to the full report below, with its own banner prepended.
    if t["verdict"] == VERDICT_REFUSED and t.get("verify") is None:
        lines.append("")
        lines.extend(f"  {ln}" for ln in t["refusal"]["lines"])
        lines.append(f"  {t['honest']}")
        return "\n".join(lines)

    if t["verdict"] == VERDICT_REFUSED and t.get("refusal"):
        lines.append("")
        lines.extend(f"  {ln}" for ln in t["refusal"]["lines"])

    lines.append("")
    lines.append("-- verify: battery-scale before/after proof (recomputed) --")
    # A verify claim that reads "supported" on its own does not stand alone when
    # the PARENT verdict is not improved; mark the nested block with the verdict
    # that actually controls (the claim.supported flag is also forced False in
    # the data), so a cropped view cannot read as a clean pass.
    superseded = None if t["verdict"] == VERDICT_IMPROVED else t["verdict"]
    lines.append(_verify.render_text(t["verify"], superseded_by=superseded))
    if t.get("contract_verify"):
        lines.append("")
        lines.append("-- contract verify (neighbouring cases) --")
        lines.append(_contract.render_verify_text(t["contract_verify"]))

    lines.extend(_evidence_text_lines(t))

    a = t.get("attribution")
    if a:
        lines.append("")
        lines.append("-- attribution: root cause of the original failure --")
        if not a["explanations"] and not a.get("unreadable"):
            lines.append("  nothing to explain")
        for exp in a["explanations"]:
            lines.append(_explain.render_text(exp))
        for u in a.get("unreadable") or []:
            lines.append(f"  unknown: {u['source']}: {u['reason']}")
    lines.append("")
    lines.append(f"  {t['honest']}")
    return "\n".join(lines)


# --- self-contained HTML report -------------------------------------------

_VERDICT_COLOR = {
    VERDICT_IMPROVED: "green",
    VERDICT_REGRESSED: "red",
    VERDICT_INCONCLUSIVE: "ember",
    VERDICT_REFUSED: "ember",
}


def _kv_table(esc, rows) -> str:
    body = "".join(
        f'<tr><td>{esc(k)}</td><td class="mono">{esc(v)}</td></tr>'
        for k, v in rows
    )
    return '<table class="basetab"><tbody>' + body + '</tbody></table>'


def _attribution_html(esc, a) -> str:
    if not a or (not a["explanations"] and not a.get("unreadable")):
        return ""
    parts = [
        '<section class="card"><div class="ctitle">Attribution: root cause '
        'of the original failure</div>'
        '<div class="cmpcap">Composed from hotato explain on the BEFORE '
        'evidence; no new scoring engine.</div>'
    ]
    for exp in a["explanations"]:
        parts.append(
            '<div class="cmpcap">source ' + esc(exp.get("source") or "")
            + ' &middot; next: ' + esc(exp.get("safe_next_action") or "")
            + '</div>'
        )
        for att in exp.get("attributions") or []:
            parts.append(
                f'<div class="does">[{esc(att.get("type"))}] '
                f'fixability={esc(att.get("fixability"))} '
                f'confidence={esc(att.get("confidence"))}</div>'
            )
        for r in exp.get("refusals") or []:
            parts.append(
                '<div class="does">REFUSED: ' + esc(r.get("reason")) + '</div>')
    for u in a.get("unreadable") or []:
        parts.append(
            '<div class="does">unknown: ' + esc(u["source"]) + ': '
            + esc(u["reason"]) + '</div>'
        )
    parts.append('</section>')
    return "".join(parts)


def _evidence_html(esc, t) -> str:
    ev = t.get("evidence")
    if not ev:
        return ""
    rc = t.get("recompute") or {}
    flags = rc.get("flags") or {}
    rows = [
        ("evidence tier", f"{ev['headline']} (tier {ev['tier']})"),
    ]
    if ev.get("limited_by") and ev["tier"] < _evidence.TIER_PAIRED:
        rows.append((
            "limited by",
            ", ".join(f"{d['dimension']}={d['state']}" for d in ev["limited_by"])))
    rows.append((
        "recompute flags",
        f"score_mismatch={flags.get('score_mismatch')}, "
        f"same_pcm={flags.get('same_pcm')}, "
        f"stimulus_mismatch={flags.get('stimulus_mismatch')}, "
        f"unrecomputable={flags.get('unrecomputable')}"))
    return (
        '<section class="card"><div class="ctitle">Evidence: what this '
        'before/after proof supports</div>'
        f'<div class="cmpcap">{esc(t.get("evidence_sentence") or "")}</div>'
        f'<div class="does">{esc(_PROVENANCE_CAUTION)}</div>'
        + _kv_table(esc, rows) + '</section>'
    )


def _wrap_html(title: str, body: str) -> str:
    desc = (
        "Self-contained hotato fix trial proof: apply's clone-only offline "
        "gate + a manifest-pinned recompute of both sides from audio + verify's "
        "battery-scale rollup + the Evidence Kernel tier, fail-closed."
    )
    return (
        "<!doctype html>\n<html lang=\"en\"><head><meta charset=\"utf-8\">"
        '<meta name="viewport" content="width=device-width, initial-scale=1">'
        f"<title>{title}</title>"
        f'<meta name="description" content="{desc}">'
        f"<style>{_report._CSS}</style></head><body>{body}</body></html>\n"
    )


def render_html(t: dict) -> str:
    """Render the fix-trial proof as ONE self-contained, offline HTML file,
    reusing report.py's house style. Reads only fields ``run_trial`` already
    computed; nothing here re-scores or invents a value."""
    esc = _report._esc
    C = _report._C
    verdict = t["verdict"]
    color = C[_VERDICT_COLOR[verdict]]

    head = (
        '<header class="top"><div class="logo"></div><div>'
        '<h1 class="h1">hotato fix trial</h1>'
        '<div class="tagline">Before/after proof that a candidate fix '
        'holds, fail-closed.</div>'
        f'<div class="subtle">{esc(t.get("patch_source") or "")} &middot; '
        f'clone {esc(t.get("name") or "-")}</div>'
        '<div class="subtle" style="margin-top:4px">'
        f'<b>{esc(t.get("apply_receipt_note"))}</b></div>'
        '<div class="metarow">'
        '<span class="pill">offline <b>yes</b></span>'
        '<span class="pill">clone-only <b>yes</b></span>'
        f'<span class="pill">min-n <b>{esc(t.get("min_n"))}</b></span>'
        f'<span class="pill">apply dry_run <b>{esc(t.get("apply_dry_run"))}'
        '</b></span>'
        f'<span class="pill">apply created <b>{esc(t.get("apply_created"))}'
        '</b></span>'
        '<span class="pill">apply applies_change '
        f'<b>{esc(t.get("apply_applies_change"))}</b></span>'
        '</div></div></header>'
    )
    summary = (
        '<div class="summary">'
        f'<div><div class="bignum">{esc(verdict.upper())}</div>'
        f'<div class="subtle" style="color:{C["muted"]}">exit code '
        f'{t["exit_code"]}</div></div>'
        f'<div class="chip verdict" style="background:{color}">'
        f'{esc(verdict.upper())}</div></div>'
    )
    concl = (
        '<div class="concl">'
        f'<b>{esc(t["conclusion"])}</b>'
        '<div class="notprove">What this does not prove: hotato measures '
        'timing only; it does not run a controlled experiment and does not '
        'attribute cause. It does not prove authorization, identity, '
        'compliance, or policy safety.</div></div>'
    )

    if verdict == VERDICT_REFUSED and t.get("verify") is None:
        # The apply-gate refusal fires before any before/after evidence is
        # read: minimal report, refusal card only.
        refusal = t["refusal"]
        extra = (
            '<section class="card"><div class="ctitle">'
            + esc(refusal["headline"]) + '</div>'
            '<div class="reasons">' + esc(refusal["reason"]) + '</div>'
            '<div class="does">Recommended: ' + esc(refusal["recommended"])
            + '</div></section>'
        )
        body = f'<div class="wrap">{head}<main>{summary}{concl}{extra}</main></div>'
        return _wrap_html(f"hotato fix trial: {verdict}", body)

    # The recompute refusal fires AFTER verify/contract/attribution already ran:
    # render the SAME refusal card, but keep the full report below it.
    refusal_section = ""
    if verdict == VERDICT_REFUSED and t.get("refusal"):
        refusal = t["refusal"]
        refusal_section = (
            '<section class="card"><div class="ctitle">'
            + esc(refusal["headline"]) + '</div>'
            '<div class="reasons">' + esc(refusal["reason"]) + '</div>'
            '<div class="does">Recommended: ' + esc(refusal["recommended"])
            + '</div></section>'
        )

    v = t["verify"]
    ra, ha = v["regression_axis"], v["hold_axis"]
    # A "supported" claim in this nested card does not stand alone when the
    # PARENT verdict is not improved. The data-level claim.supported flag is
    # already forced False for a non-improved parent, so this reads honestly on
    # its own; the marker below states which verdict controls.
    claim_supported = v["claim"]["supported"]
    superseded = None if verdict == VERDICT_IMPROVED else verdict
    if superseded and claim_supported:
        claim_display = f"SUPERSEDED BY {superseded.upper()} (verdict controls)"
    elif claim_supported:
        claim_display = "supported"
    else:
        claim_display = "not supported (verdict controls)" if superseded \
            else "refused (low n)"
    verify_rows = [
        ("previously-failing fixtures now passing",
         f"{ra['now_pass']} of {ra['used_to_fail']}"),
        ("hold fixtures still passing",
         f"{ha['still_pass']} of {ha['hold_guards']}"),
        ("regressions", str(len(v["regressions"]))),
        ("claim", claim_display),
    ]
    verify_note = ""
    if superseded:
        verify_note = (
            '<div class="does">This claim does not stand alone: the '
            f'fix-trial verdict is {esc(superseded.upper())}, and the '
            'verdict controls, not this line.</div>'
        )
    verify_section = (
        '<section class="card"><div class="ctitle">Verify: battery-scale '
        'proof (recomputed)</div><div class="cmpcap">'
        + esc(v["claim"]["statement"]) + '</div>' + verify_note
        + _kv_table(esc, verify_rows) + '</section>'
    )

    contract_section = ""
    cv = t.get("contract_verify")
    if cv:
        s = cv["summary"]
        contract_section = (
            '<section class="card"><div class="ctitle">Contract verify '
            '(neighbouring cases)</div><div class="cmpcap">'
            f'{esc(cv["dir"])}</div>'
            + _kv_table(esc, [
                ("contracts", str(cv["count"])),
                ("passing", str(s["passed"])),
                ("failing", str(s["failed"])),
            ]) + '</section>'
        )

    evidence_section = _evidence_html(esc, t)
    attribution_section = _attribution_html(esc, t.get("attribution"))

    body = (
        f'<div class="wrap">{head}<main>{summary}{concl}{refusal_section}'
        f'{verify_section}{contract_section}{evidence_section}'
        f'{attribution_section}</main></div>'
    )
    return _wrap_html(f"hotato fix trial: {verdict}", body)
