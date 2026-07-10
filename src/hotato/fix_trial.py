"""``hotato fix trial``: S4 -- compose the shipped, already-guarded primitives
into ONE before/after proof that a candidate fix actually holds.

This adds NO new scoring engine and NO new networked path. Every number here
is something ``hotato apply``, ``hotato verify``, ``hotato contract verify``,
or ``hotato explain`` already measures. ``run_trial`` (and the CLI wrapper it
backs, ``hotato fix trial``):

1. Evaluates the candidate patch through ``hotato apply``'s EXACT offline gate
   (:func:`hotato.apply.build_apply`, ``clone=True``): refusal-first on the
   both-axes threshold funnel, opposite-risk-battery-required, clone-only.
   fix trial never creates a clone itself and never touches the network --
   this module never imports :func:`hotato.apply.create_clone` or
   :func:`hotato.apply._http_json`, so it carries the SAME clone-only,
   production-unmutatable guarantee apply's own dry run gives, by
   construction, not by promise.
2. Scores the BEFORE run (the original failure evidence, captured through
   the source) against the AFTER run (re-captured through the staging clone
   you created separately with ``hotato apply --clone --yes``) with
   :func:`hotato.verify.verify_sides`: EVERY paired fixture in the battery,
   not just the target failure, so a hold/backchannel regression anywhere
   (the "neighbouring cases" check) is caught the same way a naive
   single-threshold bandaid already gets caught by verify's anti-bandaid
   guardrails.
3. Optionally re-verifies a ``--contracts`` directory against its own
   recorded policy (:func:`hotato.contract.verify_contracts`) -- another
   neighbouring-cases check, on real labelled moments outside the battery.
4. Folds in :func:`hotato.explain.explain`'s root-cause attribution for the
   BEFORE evidence as the report's attribution section.

The verdict is FAIL-CLOSED, never a soft pass:

* ``improved`` -- the verify claim is supported (>= ``min_n`` previously-
  failing fixtures), at least one now passes, NOTHING regressed anywhere in
  the battery (including the hold/opposite-risk axis -- a hold fixture
  flipping pass-to-fail is ``compare.classify_pair``'s ``"regressed"``,
  whichever axis it is on), no contract regressed, and ``policy`` (if given)
  passed.
* ``regressed`` -- any fixture regressed, a contract regressed, or the
  policy failed. Exits the same non-zero code as ``inconclusive``.
* ``inconclusive`` -- neither improved nor regressed: too few previously-
  failing fixtures to characterize (below ``min_n``), or nothing that used
  to fail now passes. INCONCLUSIVE IS NOT A PASS: it is fail-closed, exactly
  like a real regression, so CI never treats "we could not tell" as green.
* ``refused`` -- either apply's both-axes threshold-funnel gate fires before
  any before/after evidence is even read, OR (the fresh-capture provenance
  guard, below) every other bar is cleared but the AFTER evidence for a
  fixture the claim rests on is a re-score of the SAME recording the BEFORE
  run scored. Either way the refusal is a FEATURE, not an error: both share
  the same distinct exit code apply's own refusal uses.

Hotato does not infer intent and does not prove authorization, identity,
compliance, or policy safety. Every number here is a real measurement;
verify's coincidence-not-causation rule still applies throughout.

Fresh-capture provenance guard: an ``improved`` verdict is never reachable
from a re-score of frozen evidence. For every fixture the improvement claim
rests on (previously failing, now passing -- exactly the fixtures composing
verify's "N of M" headline), this module compares the ``audio_provenance``
sha256 the BEFORE and AFTER envelopes recorded for that fixture:

* identical digest -- the after run scored the SAME bytes as the before run,
  just against a different threshold or scorer config. That is not a fix
  claim; it is a re-score. Verdict downgrades to ``refused`` (never a soft
  pass), the SAME exit code as apply's own refusal.
* a digest missing on either side (an older envelope, or one built by hand,
  never carried ``audio_provenance``) -- provenance is UNKNOWN, and an
  unknown can never be assumed to be a fresh capture. Verdict downgrades to
  ``inconclusive``, with the reason naming which side lacked provenance.
* distinct, known digests on every target fixture -- proceed exactly as
  before; the digests are still surfaced in the report so the reader can see
  the recapture happened, not just take it on faith.
"""

from __future__ import annotations

import os
from datetime import datetime, timezone
from typing import Optional

from . import apply as _apply
from . import contract as _contract
from . import explain as _explain
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
# not tell" for green. 3 mirrors hotato apply's own refusal code exactly:
# the refusal is a feature, not a usage error, and a caller already branches
# on this code from apply, so fix trial never invents a new meaning for it.
EXIT_IMPROVED = 0
EXIT_FAIL = 1
EXIT_REFUSED = _apply.REFUSAL_EXIT_CODE  # 3

_HONEST = (
    "hotato fix trial never creates or mutates a clone itself and never "
    "touches production config: it evaluates the same offline gate hotato "
    "apply --clone enforces, then scores run envelopes and contracts you "
    "already captured. hotato reports coincidence, not causation."
)

# The audio-provenance section proves a fresh capture happened (distinct
# before/after digests), never that the capture stays valid forever. It
# speaks to the AFTER run's revision at the moment it was captured; a later
# deploy is a new revision this report says nothing about, and nothing here
# re-runs itself on a schedule. Rendered wherever the provenance section
# itself renders, independent of the final verdict, so a reader sees the
# limit even on an improved run. See docs/RECAPTURE.md ("Limits, stated
# plainly") for the same caution at length.
_PROVENANCE_CAUTION = (
    "Provenance caution: this proves the specific fresh capture scored "
    "above, at the revision it was captured from. It does not certify a "
    "later deploy or every future call, and it does not re-run itself; "
    "recapture again after the next change."
)


def _now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


# --- attribution: reuse hotato explain on the BEFORE evidence ---------------

def _attribution_for(before: str) -> dict:
    """Best-effort root-cause attribution for the BEFORE evidence, reusing
    ``hotato explain`` exactly (no new scoring). ``before`` is a single run
    envelope path or a directory of them; every ``.json`` file explain can
    read contributes its own explanation. A file explain cannot read (a
    non-envelope shape, a corrupt file) degrades to a recorded unknown --
    the timing proof above never depends on attribution succeeding."""
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


# --- fresh-capture provenance guard ------------------------------------------

def _target_fixtures(v: dict) -> list:
    """The fixtures the 'improved' claim rests on: previously failing (before
    scorable and failed) AND now passing. This is exactly the set that
    composes verify's ``regression_axis.now_pass`` count -- the fixtures a
    reader is being asked to believe the change fixed."""
    return [
        r for r in v.get("per_fixture", [])
        if r["before"]["scorable"] and not r["before"]["passed"]
        and r["after"]["passed"]
    ]


def _short_sha(sha) -> Optional[str]:
    return sha[:12] if isinstance(sha, str) and sha else None


def _fixture_provenance(r: dict) -> dict:
    """Compare one target fixture's before/after audio identity. Reads only
    the ``audio_provenance`` the envelopes already carried (passed through by
    :func:`hotato.verify.verify_sides`); invents nothing, and a missing side
    is honestly ``"unknown"``, never assumed distinct."""
    bp = r["before"].get("audio_provenance")
    ap = r["after"].get("audio_provenance")
    b_sha = bp.get("sha256") if isinstance(bp, dict) else None
    a_sha = ap.get("sha256") if isinstance(ap, dict) else None
    if b_sha is None or a_sha is None:
        status = "unknown"
    elif b_sha == a_sha:
        status = "same"
    else:
        status = "different"
    return {
        "fixture": r["fixture"],
        "before_sha256": b_sha,
        "after_sha256": a_sha,
        "before_short": _short_sha(b_sha),
        "after_short": _short_sha(a_sha),
        "status": status,
    }


def _provenance_report(v: dict) -> dict:
    """Per-fixture provenance for every target fixture, plus the single issue
    (if any) that must gate the verdict. ``same_audio`` (a re-score of frozen
    evidence) always wins over ``unknown_provenance`` (a digest missing on
    one or both sides) when both are present, since a confirmed re-score is a
    stronger, more specific finding than an unverifiable one."""
    targets = [_fixture_provenance(r) for r in _target_fixtures(v)]
    same = [t for t in targets if t["status"] == "same"]
    unknown = [t for t in targets if t["status"] == "unknown"]
    if same:
        issue = {"kind": "same_audio", "fixtures": same}
    elif unknown:
        issue = {"kind": "unknown_provenance", "fixtures": unknown}
    else:
        issue = None
    return {"target_fixtures": targets, "issue": issue}


_PROVENANCE_REFUSAL_HEADLINE = "No fix will be certified from re-scored audio"


def _same_audio_refusal(issue: dict) -> dict:
    """Build the SAME refusal shape apply's own gate uses (headline / reason /
    recommended / lines / why), so both renderers and callers already
    branching on ``t["refusal"]["lines"]`` handle this refusal identically."""
    names = ", ".join(f["fixture"] for f in issue["fixtures"])
    reason = (
        f"{len(issue['fixtures'])} fixture(s) this claim rests on ({names}) "
        "have byte-identical before/after audio (same sha256): the after run "
        "re-scored the SAME recording the before run scored, just against a "
        "different threshold or scorer config"
    )
    recommended = (
        "recapture the fixture(s) through the applied clone "
        "(hotato apply --clone --yes) and re-run hotato fix trial against "
        "the new after evidence"
    )
    lines = (
        _PROVENANCE_REFUSAL_HEADLINE,
        f"Reason: {reason}",
        f"Recommended: {recommended}",
    )
    return {
        "headline": _PROVENANCE_REFUSAL_HEADLINE,
        "reason": reason,
        "recommended": recommended,
        "lines": list(lines),
        "why": (
            "A verified fix requires the AFTER evidence to be a fresh "
            "recording, not a re-score of the BEFORE recording under a "
            "looser threshold. Two runs over byte-identical audio prove "
            "nothing about a code, config, or model change -- they only "
            "prove the scorer's threshold moved. This refusal is the "
            "feature: it closes the exact path where the same recording is "
            "rescored with a looser threshold and passed off as a verified "
            "improvement."
        ),
    }


def _unknown_provenance_reason(issue: dict) -> str:
    parts = []
    for f in issue["fixtures"]:
        missing = []
        if f["before_sha256"] is None:
            missing.append("before")
        if f["after_sha256"] is None:
            missing.append("after")
        parts.append(f"{f['fixture']} ({' and '.join(missing)} missing)")
    return (
        "Provenance guard: audio identity is UNKNOWN for "
        + ", ".join(parts)
        + " (an older envelope, or one built by hand, carries no "
        "audio_provenance field). An unknown can never be assumed to be a "
        "fresh capture, so this cannot be certified 'improved'; recapture "
        "with a current hotato build (or supply --before/--after envelopes "
        "that carry audio_provenance) and re-run."
    )


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
) -> dict:
    """Run the S4 before/after proof. Pure and OFFLINE: never creates a
    clone and never touches the network (this module never references
    :func:`hotato.apply.create_clone` / :func:`hotato.apply._http_json`).
    Raises ``ValueError`` / ``OSError`` (CLI exit 2) for anything unusable --
    the SAME errors ``apply`` / ``verify`` / ``contract verify`` already
    raise, never a new error class."""
    battery_dir = battery or before

    # 1. The exact apply gate, clone-only, refusal-first, offline. If this
    # is the both-axes threshold funnel, it refuses BEFORE any before/after
    # evidence is even read (mirrors apply's own refusal-first ordering).
    apply_result = _apply.build_apply(
        patch, name=name, clone=True, battery_dir=battery_dir,
        patch_source=patch_source, plan=plan,
    )

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
            "provenance": None,
            "conclusion": (
                refusal["headline"] + ": " + refusal["reason"] + ". "
                "Recommended: " + refusal["recommended"] + "."
            ),
            "honest": _HONEST,
        }

    # 2. verify: EVERY paired fixture in the battery, not just the target
    # failure -- the neighbouring-cases + opposite-risk check.
    v = _verify.verify_sides(before, after, min_n=min_n)
    policy_result = None
    if policy is not None:
        policy_result = _verify.evaluate_policy(v, policy)
        v["policy"] = policy_result

    # 3. contract verify: another neighbouring-cases check, on real labelled
    # moments outside the battery. A bad/empty --contracts is a usage error
    # (ValueError propagates, CLI exit 2), same as `hotato contract verify`.
    cv = _contract.verify_contracts(contracts) if contracts else None
    contract_regressed = bool(cv and cv["summary"]["failed"] > 0)

    # 4. attribution: root cause of the ORIGINAL failure, reusing explain.
    attribution = _attribution_for(before)

    vm = _verify.verdict_model(v)
    regressed_any = bool(v["regressions"])
    policy_failed = policy_result is not None and not policy_result["passed"]

    # Fresh-capture provenance guard: computed regardless of the base verdict
    # (the report always shows the target fixtures' before/after digests), but
    # it can only ever DOWNGRADE an otherwise-improved verdict -- a real
    # regression stays a regression whether or not the recapture can be
    # verified; fail-closed needs no proof to reject, only to accept.
    provenance = _provenance_report(v)
    issue = provenance["issue"]
    refusal = None
    refusal_kind = None

    if regressed_any or contract_regressed or policy_failed:
        verdict = VERDICT_REGRESSED
    elif vm["passed"]:
        if issue and issue["kind"] == "same_audio":
            verdict = VERDICT_REFUSED
            refusal = _same_audio_refusal(issue)
            refusal_kind = "same_audio_recapture"
        elif issue:  # unknown_provenance
            verdict = VERDICT_INCONCLUSIVE
        else:
            verdict = VERDICT_IMPROVED
    else:
        verdict = VERDICT_INCONCLUSIVE

    if verdict == VERDICT_REFUSED:
        exit_code = EXIT_REFUSED
    elif verdict == VERDICT_IMPROVED:
        exit_code = EXIT_IMPROVED
    else:
        exit_code = EXIT_FAIL
    conclusion = _conclusion(verdict, v, cv, policy_result, contract_regressed, issue)

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
        "provenance": provenance,
        "conclusion": conclusion,
        "honest": _HONEST,
    }


def _conclusion(verdict, v, cv, policy_result, contract_regressed,
                 provenance_issue=None) -> str:
    ra, ha = v["regression_axis"], v["hold_axis"]
    bits = [
        f"{ra['now_pass']} of {ra['used_to_fail']} previously-failing "
        f"fixture(s) now pass, {ha['still_pass']} of {ha['hold_guards']} "
        "hold fixture(s) still pass",
    ]
    if v["regressions"]:
        bits.append(f"{len(v['regressions'])} fixture(s) REGRESSED")
    if contract_regressed and cv:
        bits.append(
            f"{cv['summary']['failed']} of {cv['count']} contract(s) "
            "regressed")
    if policy_result is not None:
        bits.append("policy " + ("PASSED" if policy_result["passed"] else "FAILED"))
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
    if provenance_issue and provenance_issue["kind"] == "same_audio" \
            and verdict == VERDICT_REFUSED:
        tail += " " + _same_audio_refusal(provenance_issue)["reason"] + "."
    elif provenance_issue and provenance_issue["kind"] == "unknown_provenance" \
            and verdict == VERDICT_INCONCLUSIVE:
        tail += " " + _unknown_provenance_reason(provenance_issue)
    return tail


# --- text rendering ------------------------------------------------------

def render_text(t: dict) -> str:
    lines = [
        f"hotato fix trial [{t['verdict'].upper()}] "
        f"patch={t.get('patch_source')!r} name={t.get('name')!r}",
        f"  {t['conclusion']}",
    ]
    # The apply-gate refusal fires BEFORE any before/after evidence is read
    # (t["verify"] is None): render the minimal canon-refusal-only report.
    # The provenance-guard refusal fires AFTER verify/contract/attribution
    # already ran, so it falls through to the full report below, with its
    # own refusal banner prepended.
    if t["verdict"] == VERDICT_REFUSED and t.get("verify") is None:
        lines.append("")
        lines.extend(f"  {ln}" for ln in t["refusal"]["lines"])
        lines.append(f"  {t['honest']}")
        return "\n".join(lines)

    if t["verdict"] == VERDICT_REFUSED and t.get("refusal"):
        lines.append("")
        lines.extend(f"  {ln}" for ln in t["refusal"]["lines"])

    lines.append("")
    lines.append("-- verify: battery-scale before/after proof --")
    lines.append(_verify.render_text(t["verify"]))
    if t.get("contract_verify"):
        lines.append("")
        lines.append("-- contract verify (neighbouring cases) --")
        lines.append(_contract.render_verify_text(t["contract_verify"]))
    prov = t.get("provenance")
    if prov and prov.get("target_fixtures"):
        lines.append("")
        lines.append("-- audio provenance: before vs after recapture --")
        lines.append(
            "  identity of the exact audio each side scored, for every "
            "fixture the improvement claim rests on. A fresh take proves the "
            "same human-labeled contract passed on new evidence, never that "
            "the change caused it."
        )
        lines.append(f"  {_PROVENANCE_CAUTION}")
        for f in prov["target_fixtures"]:
            lines.append(
                f"  {f['fixture']}: before={f['before_short'] or 'unknown'} "
                f"after={f['after_short'] or 'unknown'} ({f['status']})"
            )
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


def _provenance_html(esc, prov) -> str:
    if not prov or not prov.get("target_fixtures"):
        return ""
    rows = [
        (f["fixture"],
         f"{f['before_short'] or 'unknown'} vs {f['after_short'] or 'unknown'}"
         f" ({f['status']})")
        for f in prov["target_fixtures"]
    ]
    return (
        '<section class="card"><div class="ctitle">Audio provenance: before '
        'vs after recapture</div><div class="cmpcap">Streamed sha256 '
        'identity of the exact audio each side scored, for every fixture the '
        'improvement claim rests on (previously failing, now passing). A '
        'fresh take proves the same human-labeled contract passed on new '
        'evidence; identical digests mean the after run re-scored the same '
        'recording, not a fresh capture.</div>'
        f'<div class="does">{esc(_PROVENANCE_CAUTION)}</div>'
        + _kv_table(esc, rows) + '</section>'
    )


def _wrap_html(title: str, body: str) -> str:
    desc = (
        "Self-contained hotato fix trial proof: apply's clone-only offline "
        "gate + verify's battery-scale before/after rollup + contract "
        "verify + explain's root-cause attribution, fail-closed."
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
        '<div class="metarow">'
        '<span class="pill">offline <b>yes</b></span>'
        '<span class="pill">clone-only <b>yes</b></span>'
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

    # The provenance-guard refusal fires AFTER verify/contract/attribution
    # already ran: render the SAME refusal card, but keep the full report
    # (verify, contract, provenance, attribution) below it.
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
    verify_rows = [
        ("previously-failing fixtures now passing",
         f"{ra['now_pass']} of {ra['used_to_fail']}"),
        ("hold fixtures still passing",
         f"{ha['still_pass']} of {ha['hold_guards']}"),
        ("regressions", str(len(v["regressions"]))),
        ("claim", "supported" if v["claim"]["supported"] else "refused (low n)"),
    ]
    verify_section = (
        '<section class="card"><div class="ctitle">Verify: battery-scale '
        'proof</div><div class="cmpcap">'
        + esc(v["claim"]["statement"]) + '</div>'
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

    provenance_section = _provenance_html(esc, t.get("provenance"))
    attribution_section = _attribution_html(esc, t.get("attribution"))

    body = (
        f'<div class="wrap">{head}<main>{summary}{concl}{refusal_section}'
        f'{verify_section}{contract_section}{provenance_section}'
        f'{attribution_section}</main></div>'
    )
    return _wrap_html(f"hotato fix trial: {verdict}", body)
