"""``hotato prove``: compose the evidence lanes you already ran into ONE
portable, content-addressed release proof.

It adds no new scoring engine: every number here is one existing command
already measures -- ``hotato contract verify`` (the contracts lane, via
:func:`hotato.contract.verify_contracts`), ``hotato suite run`` (the suite
lane, via :func:`hotato.suite_run.run_suite`), ``hotato verify`` (the
before/after lane, via :func:`hotato.verify.verify_sides`), and ``hotato
gauntlet`` (the bundled stress suite, via
:func:`hotato.gauntlet.run_gauntlet`). :func:`run_prove` imports and calls the
same module functions those commands dispatch to; it never recomputes or
re-implements a verdict.

The verdict is FAIL-CLOSED:

* ``pass`` -- EVERY activated lane passed. This is the only exit-0 outcome.
* ``fail`` -- any lane failed or regressed.
* ``inconclusive`` -- no lane failed, but at least one refused its input or
  came back inconclusive (a below ``--min-n`` battery, an unusable contracts
  directory). Exits non-zero, so CI never reads "could not tell" as green.
* Zero activated lanes is a usage error (exit 2): a proof of nothing is
  refused, not an empty pass.

Share-safe by construction: the proof carries verdicts, counts, relative
input names, and sha256 digests only -- no transcript text, no audio bytes,
no absolute path, no environment value. ``content_id`` is a full content
address (the failure-record pattern): sha256 over the canonical proof JSON
without the ``content_id`` field, so the same inputs under a pinned
``SOURCE_DATE_EPOCH`` regenerate the same bytes and the same address.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
from typing import Any, Dict, List, Optional

from . import __version__
from .errors import open_regular

__all__ = [
    "SCHEMA_VERSION",
    "LANES",
    "run_prove",
    "compute_content_id",
    "serialize",
    "render_text",
    "render_md",
]

SCHEMA_VERSION = "hotato.proof.v1"

# The fixed lane order (activated lanes appear in this order in ``lanes``).
LANES = ("contracts", "suite", "verify", "gauntlet")

VERDICT_PASS = "pass"
VERDICT_FAIL = "fail"
VERDICT_INCONCLUSIVE = "inconclusive"
VERDICT_REFUSED = "refused"

# =========================================================================
# claim scope + evidence authority: exactly what the proof establishes, and
# on what strength of evidence. Both fail DOWN -- the proof may never claim
# more than the activated lanes actually support.
# =========================================================================

# claim_scope, weakest -> strongest. The proof's scope is the STRONGEST scope
# any activated lane supports, where each lane's own scope is already capped
# by the evidence it holds (the before/after lane cannot reach a candidate or
# deployed revision without the candidate identity bound alongside it).
SCOPE_CAPTURED_EVIDENCE = "captured_evidence"   # re-measures stored evidence
SCOPE_TEST_SUITE = "test_suite"                 # a suite/battery executed
SCOPE_CANDIDATE_REVISION = "candidate_revision"  # bound candidate identity
SCOPE_DEPLOYED_REVISION = "deployed_revision"    # bound + a deployment id
CLAIM_SCOPES = (
    SCOPE_CAPTURED_EVIDENCE,
    SCOPE_TEST_SUITE,
    SCOPE_CANDIDATE_REVISION,
    SCOPE_DEPLOYED_REVISION,
)

# The base scope each lane can support on its own evidence. The before/after
# (``verify``) lane is elevated above ``test_suite`` only when the caller also
# grounds the candidate identity (see :func:`_lane_claim_scope`).
_LANE_BASE_SCOPE = {
    "contracts": SCOPE_CAPTURED_EVIDENCE,
    "suite": SCOPE_TEST_SUITE,
    "verify": SCOPE_TEST_SUITE,
    "gauntlet": SCOPE_TEST_SUITE,
}

# evidence_authority, weakest -> strongest. This release ships only the two
# lower rungs: every current lane scores deterministically, so it is
# ``measured``; a future asserted-only lane would be ``asserted``. The upper
# rungs (paired / runner_authenticated / organization_signed /
# independently_attested) are NOT claimable yet and are never emitted.
AUTHORITY_ASSERTED = "asserted"
AUTHORITY_MEASURED = "measured"
AUTHORITY_PAIRED = "paired"
AUTHORITY_RUNNER_AUTHENTICATED = "runner_authenticated"
AUTHORITY_ORGANIZATION_SIGNED = "organization_signed"
AUTHORITY_INDEPENDENTLY_ATTESTED = "independently_attested"
EVIDENCE_AUTHORITIES = (
    AUTHORITY_ASSERTED,
    AUTHORITY_MEASURED,
    AUTHORITY_PAIRED,
    AUTHORITY_RUNNER_AUTHENTICATED,
    AUTHORITY_ORGANIZATION_SIGNED,
    AUTHORITY_INDEPENDENTLY_ATTESTED,
)

# Every lane in this release scores deterministically -- it MEASURES its
# verdict rather than asserting it.
_MEASURING_LANES = frozenset(LANES)

# Human-facing claim-scope titles for the rendered headline.
SCOPE_TITLE = {
    SCOPE_CAPTURED_EVIDENCE: "Captured Evidence",
    SCOPE_TEST_SUITE: "Test Suite",
    SCOPE_CANDIDATE_REVISION: "Candidate Revision",
    SCOPE_DEPLOYED_REVISION: "Deployed Revision",
}

# The one-line description of what a PASS at each scope establishes.
_SCOPE_PASS_SUBLINE = {
    SCOPE_CAPTURED_EVIDENCE: "All stored contracts re-measured successfully "
                             "under their pinned policies.",
    SCOPE_TEST_SUITE: "A test suite executed and every activated lane passed.",
    SCOPE_CANDIDATE_REVISION: "The bound candidate revision passed every "
                              "activated lane.",
    SCOPE_DEPLOYED_REVISION: "The deployed revision passed every activated "
                             "lane.",
}


def _lane_claim_scope(lane_name: str, *, candidate_bound: bool,
                      deployment_bound: bool) -> str:
    """The scope a single activated lane supports, given whether the caller
    grounded a candidate revision (config hash + provider) and, on top of
    that, a deployment id. Only the before/after lane can be elevated; every
    other lane keeps its base scope."""
    if lane_name == "verify" and candidate_bound:
        return SCOPE_DEPLOYED_REVISION if deployment_bound \
            else SCOPE_CANDIDATE_REVISION
    return _LANE_BASE_SCOPE[lane_name]


def _derive_claim_scope(lanes: List[Dict[str, Any]], *,
                        candidate_bound: bool,
                        deployment_bound: bool) -> str:
    """The proof's overall claim scope: the STRONGEST scope any activated lane
    supports on its own capped evidence. Contracts-only stays
    ``captured_evidence`` (adding nothing cannot elevate it); a suite or
    gauntlet lane establishes that a ``test_suite`` ran; the before/after lane
    reaches a candidate or deployed revision only with the identity bound."""
    scopes = [
        _lane_claim_scope(entry["lane"], candidate_bound=candidate_bound,
                          deployment_bound=deployment_bound)
        for entry in lanes
    ]
    return max(scopes, key=CLAIM_SCOPES.index)


def _derive_evidence_authority(lanes: List[Dict[str, Any]]) -> str:
    """The strength of the evidence under the claim. Every lane in this
    release measures deterministically, so any activated lane yields
    ``measured``; the paired / signed / attested rungs are not claimable yet
    and a candidate binding does NOT raise this to ``runner_authenticated``
    (the runner is not authenticated in this release)."""
    if any(entry["lane"] in _MEASURING_LANES for entry in lanes):
        return AUTHORITY_MEASURED
    return AUTHORITY_ASSERTED

EXIT_PASS = 0
EXIT_FAIL = 1
EXIT_INCONCLUSIVE = 2

_SAFE_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,119}$")


# =========================================================================
# canonical JSON + content address (the failure_record.py pattern)
# =========================================================================

def _canonical_json_bytes(obj: Any) -> bytes:
    return json.dumps(
        obj, sort_keys=True, separators=(",", ":"),
        ensure_ascii=False, allow_nan=False,
    ).encode("utf-8")


def _digest_bytes(data: bytes) -> str:
    return "sha256:" + hashlib.sha256(data).hexdigest()


def compute_content_id(proof: Dict[str, Any]) -> str:
    """The proof's content address: sha256 over the canonical JSON of every
    field EXCEPT ``content_id`` itself (sorted keys, no insignificant
    whitespace, UTF-8, finite numbers only)."""
    identity = {k: v for k, v in proof.items() if k != "content_id"}
    return _digest_bytes(_canonical_json_bytes(identity))


def serialize(proof: Dict[str, Any]) -> str:
    """The single proof.json serialization: deterministic (sorted keys,
    2-space indent, trailing newline) so the same proof dict always writes
    the same bytes."""
    return json.dumps(
        proof, indent=2, sort_keys=True, ensure_ascii=False, allow_nan=False,
    ) + "\n"


# =========================================================================
# share-safe input evidence: relative name + sha256 of the file / the
# directory's own manifest (relative paths + per-file digests)
# =========================================================================

def _file_sha256(path: str) -> str:
    h = hashlib.sha256()
    with open_regular(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return "sha256:" + h.hexdigest()


def _dir_manifest_digest(path: str) -> str:
    """Digest of the directory's manifest: the sorted list of
    ``{path: <relative, forward-slash>, sha256: <file digest>}`` rows,
    canonically serialized. Only the digest ships; the manifest (which
    contains only relative paths) is not embedded."""
    rows = []
    for base, _dirs, files in os.walk(path):
        for fname in files:
            full = os.path.join(base, fname)
            rel = os.path.relpath(full, path).replace(os.sep, "/")
            rows.append({"path": rel, "sha256": _file_sha256(full)})
    rows.sort(key=lambda r: r["path"])
    return _digest_bytes(_canonical_json_bytes(rows))


def _input_evidence(path: str) -> Dict[str, Any]:
    """One evidence entry for a lane input: the input's RELATIVE name (its
    basename -- never the absolute path) plus the sha256 digest of the file,
    or of the directory's manifest. A missing input carries a null digest;
    the lane's own refusal states why."""
    name = os.path.basename(os.path.normpath(path)) or "input"
    digest: Optional[str] = None
    try:
        if os.path.isdir(path):
            digest = _dir_manifest_digest(path)
        elif os.path.isfile(path):
            digest = _file_sha256(path)
    except OSError:
        digest = None
    return {"input": name, "digest": digest}


# =========================================================================
# the four lanes -- each one calls the EXISTING module function the CLI
# already dispatches to, and projects verdict + native counts only
# =========================================================================

def _lane(lane: str, verdict: str, counts: Dict[str, Any],
          evidence: List[Dict[str, Any]],
          refusal: Optional[str] = None) -> Dict[str, Any]:
    entry = {"lane": lane, "verdict": verdict, "counts": counts,
             "evidence": evidence}
    if refusal is not None:
        entry["refusal"] = refusal
    return entry


def _lane_contracts(path: str) -> Dict[str, Any]:
    from . import contract as _contract

    evidence = [_input_evidence(path)]
    try:
        res = _contract.verify_contracts(path)
    except ValueError:
        # verify_contracts raises for a missing/corrupt contract or a
        # directory with no contracts. The message can carry the caller's
        # path, so a FIXED share-safe reason ships instead.
        return _lane("contracts", VERDICT_REFUSED, {}, evidence,
                     refusal="contract verify refused the input: no usable "
                             "contracts (missing, corrupt, or an empty "
                             "directory)")
    counts = {
        "contracts": res["count"],
        "passed": res["summary"]["passed"],
        "failed": res["summary"]["failed"],
        "tampered": res["tampered"],
        "refused": res["refused"],
        "assertions_failed": res["assertions_failed"],
    }
    verdict = VERDICT_PASS if res["exit_code"] == 0 else VERDICT_FAIL
    return _lane("contracts", verdict, counts, evidence)


def _lane_suite(suite_path: str, agent: str) -> Dict[str, Any]:
    from . import suite_run as _suite_run

    evidence = [_input_evidence(suite_path)]
    try:
        suite_doc, base_dir = _suite_run.load_suite_file(suite_path)
        res = _suite_run.run_suite(
            suite_doc, base_dir, agent_id=agent, registry=None,
        )
    except ValueError:
        return _lane("suite", VERDICT_REFUSED, {}, evidence,
                     refusal="the suite runner refused the input: a "
                             "malformed suite.v1 file or an unresolvable "
                             "test ref")
    counts = {
        "tests": res["counts"]["tests"],
        "runs": res["counts"]["runs"],
        "passed_tests": res["counts"]["passed_tests"],
        "failed_tests": res["counts"]["failed_tests"],
        "refused_tests": res["counts"]["refused_tests"],
        "simulator_invalid": res["counts"]["simulator_invalid"],
    }
    if res["exit_code"] == 0:
        verdict = VERDICT_PASS
    elif res["exit_code"] == 1:
        verdict = VERDICT_FAIL
    else:
        verdict = VERDICT_REFUSED
    return _lane("suite", verdict, counts, evidence)


def _lane_verify(before: str, after: str, min_n: int) -> Dict[str, Any]:
    from . import verify as _verify

    evidence = [_input_evidence(before), _input_evidence(after)]
    try:
        res = _verify.verify_sides(before, after, min_n=min_n)
    except ValueError:
        return _lane("verify", VERDICT_REFUSED, {}, evidence,
                     refusal="verify refused the input: unusable run "
                             "envelopes or no fixtures pair between the "
                             "before and after sides")
    axis = res["regression_axis"]
    counts = {
        "paired": res["paired"],
        "used_to_fail": axis["used_to_fail"],
        "fixed": axis["now_pass"],
        "still_fail": axis["still_fail"],
        "regressed": len(res["regressions"]),
        "min_n": res["min_n"],
        "claim_supported": bool(res["claim"]["supported"]),
    }
    # The same fail-closed reading hotato fix trial applies to verify's
    # rollup: any regression fails; a below-min-n or zero-improvement
    # battery is inconclusive, never a soft pass.
    if res["regressions"]:
        verdict = VERDICT_FAIL
    elif not res["claim"]["supported"] or axis["now_pass"] == 0:
        verdict = VERDICT_INCONCLUSIVE
    else:
        verdict = VERDICT_PASS
    return _lane("verify", verdict, counts, evidence)


def _lane_gauntlet() -> Dict[str, Any]:
    from . import gauntlet as _gauntlet

    manifest = _gauntlet.load_manifest()
    res = _gauntlet.run_gauntlet(out_dir=None)
    evidence = [{
        "input": res["suite"],
        "digest": _digest_bytes(_canonical_json_bytes(manifest)),
    }]
    counts = {"passed": res["passed"], "total": res["total"]}
    verdict = VERDICT_PASS if res["all_passed"] else VERDICT_FAIL
    return _lane("gauntlet", verdict, counts, evidence)


# =========================================================================
# composition: activated lanes -> one proof envelope
# =========================================================================

def _overall(lanes: List[Dict[str, Any]]) -> str:
    verdicts = [entry["verdict"] for entry in lanes]
    if any(v == VERDICT_FAIL for v in verdicts):
        return VERDICT_FAIL
    if all(v == VERDICT_PASS for v in verdicts):
        return VERDICT_PASS
    return VERDICT_INCONCLUSIVE


def _deterministic_created_at() -> str:
    # The repo's reproducible-timestamp convention (simulate.py):
    # $SOURCE_DATE_EPOCH when set, else a fixed instant -- never the wall
    # clock, so the same inputs write byte-identical proof.json.
    from . import simulate as _simulate

    return _simulate.deterministic_created_at(None)


def run_prove(
    *,
    contracts: Optional[str] = None,
    suite: Optional[str] = None,
    agent: Optional[str] = None,
    before: Optional[str] = None,
    after: Optional[str] = None,
    min_n: int = 3,
    gauntlet: bool = False,
    name: Optional[str] = None,
    candidate_config_hash: Optional[str] = None,
    provider: Optional[str] = None,
    deployment_id: Optional[str] = None,
) -> Dict[str, Any]:
    """Run every activated evidence lane through its existing module function
    and compose the results into one ``hotato.proof.v1`` dict (including
    ``exit_code`` and ``content_id``). Raises ``ValueError`` (the CLI's
    exit-2 usage path) when zero lanes are activated or a flag pair is
    incomplete -- before anything runs and before anything is written."""
    if suite is not None and agent is None:
        raise ValueError("--suite needs --agent NAME: the suite lane records "
                         "which agent the suite ran against")
    if agent is not None and suite is None:
        raise ValueError("--agent only accompanies --suite SUITE.json")
    if (before is None) != (after is None):
        raise ValueError("the before/after lane needs BOTH --before and "
                         "--after (each a run envelope JSON or a directory "
                         "of them)")
    if contracts is None and suite is None and before is None and not gauntlet:
        raise ValueError(
            "no evidence lane activated. hotato prove composes lanes you "
            "already have: --contracts DIR, --suite SUITE.json --agent NAME, "
            "--before RUN --after RUN, and/or --gauntlet. A proof of nothing "
            "is refused, not an empty pass."
        )
    proof_name = name if name is not None else "proof"
    if not _SAFE_NAME_RE.match(proof_name):
        raise ValueError(
            "--name must be letters, digits, dot, underscore, or hyphen "
            "(leading alphanumeric, at most 120 chars); it names the proof "
            "and its default output directory"
        )

    lanes: List[Dict[str, Any]] = []
    if contracts is not None:
        lanes.append(_lane_contracts(contracts))
    if suite is not None:
        lanes.append(_lane_suite(suite, agent))
    if before is not None:
        lanes.append(_lane_verify(before, after, min_n))
    if gauntlet:
        lanes.append(_lane_gauntlet())

    overall = _overall(lanes)
    exit_code = {VERDICT_PASS: EXIT_PASS, VERDICT_FAIL: EXIT_FAIL,
                 VERDICT_INCONCLUSIVE: EXIT_INCONCLUSIVE}[overall]

    # The candidate identity binds only when BOTH the config hash and the
    # provider are supplied; a deployment id elevates only on top of that
    # binding. Any of the three supplied WITHOUT the before/after lane are
    # recorded as evidence below but never raise the scope (only the verify
    # lane reads them).
    candidate_bound = bool(candidate_config_hash and provider)
    deployment_bound = candidate_bound and bool(deployment_id)
    claim_scope = _derive_claim_scope(
        lanes, candidate_bound=candidate_bound,
        deployment_bound=deployment_bound,
    )
    evidence_authority = _derive_evidence_authority(lanes)

    proof = {
        "tool": "hotato",
        "schema_version": SCHEMA_VERSION,
        "name": proof_name,
        "hotato_version": __version__,
        "created_at": _deterministic_created_at(),
        "lanes": lanes,
        "claim_scope": claim_scope,
        "evidence_authority": evidence_authority,
        "overall": overall,
        "exit_code": exit_code,
    }
    # Share-safe, digest-stable record of any supplied candidate binding. These
    # are opaque identifiers (a config digest, a provider name, a deployment
    # id), never paths; they are stored verbatim so the content address covers
    # them, and only present when at least one was given.
    binding: Dict[str, Any] = {}
    if candidate_config_hash is not None:
        binding["candidate_config_hash"] = candidate_config_hash
    if provider is not None:
        binding["provider"] = provider
    if deployment_id is not None:
        binding["deployment_id"] = deployment_id
    if binding:
        proof["evidence"] = binding

    proof["content_id"] = compute_content_id(proof)
    return proof


# =========================================================================
# rendering (deterministic given the proof dict)
# =========================================================================

def _counts_cell(counts: Dict[str, Any]) -> str:
    if not counts:
        return "-"
    return " ".join(f"{k}={v}" for k, v in counts.items())


def _table_rows(proof: Dict[str, Any]) -> List[Dict[str, str]]:
    rows = []
    for entry in proof["lanes"]:
        rows.append({
            "lane": entry["lane"],
            "verdict": entry["verdict"],
            "counts": _counts_cell(entry["counts"]),
        })
    return rows


def _scope_subline(proof: Dict[str, Any]) -> str:
    """The one line under the headline: what a PASS at this claim scope
    establishes, or why a non-pass did not establish it."""
    overall = proof["overall"]
    if overall == VERDICT_FAIL:
        return "A lane failed or regressed; see the table."
    if overall == VERDICT_INCONCLUSIVE:
        return "A lane refused or came back inconclusive; see the table."
    return _SCOPE_PASS_SUBLINE[proof["claim_scope"]]


def render_text(proof: Dict[str, Any],
                proof_path: Optional[str] = None) -> str:
    """The claim-scope headline + one-line subhead, then the tight per-lane
    table + overall verdict + content address (+ where the proof landed, when
    the caller wrote it). The headline states exactly what the proof
    establishes -- e.g. ``Captured Evidence: PASS`` for a contracts-only run,
    never a bare 'release proof'."""
    title = SCOPE_TITLE[proof["claim_scope"]]
    lines = [
        f"{title}: {proof['overall'].upper()}",
        f"  {_scope_subline(proof)}",
        f"hotato prove: {proof['name']} -- overall "
        f"{proof['overall'].upper()} (exit {proof['exit_code']})",
        f"  claim_scope: {proof['claim_scope']}  "
        f"evidence_authority: {proof['evidence_authority']}",
    ]
    rows = _table_rows(proof)
    lane_w = max(len("lane"), *(len(r["lane"]) for r in rows))
    verdict_w = max(len("verdict"), *(len(r["verdict"]) for r in rows))
    lines.append(f"  {'lane':<{lane_w}}  {'verdict':<{verdict_w}}  counts")
    for r in rows:
        lines.append(
            f"  {r['lane']:<{lane_w}}  {r['verdict']:<{verdict_w}}  "
            f"{r['counts']}"
        )
    lines.append(f"content_id: {proof['content_id']}")
    if proof_path:
        lines.append(f"proof: {proof_path}")
    return "\n".join(lines) + "\n"


def render_md(proof: Dict[str, Any]) -> str:
    """proof.md: the same per-lane table, overall verdict, and content
    address as the text rendering, in markdown."""
    title = SCOPE_TITLE[proof["claim_scope"]]
    lines = [
        f"# hotato proof: {proof['name']}",
        "",
        f"## {title}: {proof['overall'].upper()}",
        "",
        _scope_subline(proof),
        "",
        f"Overall: **{proof['overall'].upper()}** "
        f"(exit {proof['exit_code']}). Pass requires every activated lane "
        "to pass; an inconclusive or refused lane exits non-zero.",
        "",
        "| lane | verdict | counts |",
        "| --- | --- | --- |",
    ]
    for r in _table_rows(proof):
        lines.append(f"| {r['lane']} | {r['verdict']} | {r['counts']} |")
    lines += [
        "",
        f"- claim_scope: {proof['claim_scope']}",
        f"- evidence_authority: {proof['evidence_authority']}",
        f"- hotato_version: {proof['hotato_version']}",
        f"- created_at: {proof['created_at']}",
        f"- content_id: `{proof['content_id']}`",
        "",
        "Every number above is one existing command's own measurement "
        "(contract verify, suite run, verify, gauntlet); the proof carries "
        "verdicts, counts, relative input names, and sha256 digests only.",
    ]
    return "\n".join(lines) + "\n"
