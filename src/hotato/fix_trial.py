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
* ``refused`` -- the patch is the both-axes threshold funnel; apply's own
  refusal-first gate fires before any verify/contract/explain work runs (no
  before/after evidence is even read), and fix trial refuses too, printing
  the exact canon recommendation. The refusal is a FEATURE, not an error:
  the same distinct exit code apply's own refusal uses.

Hotato does not infer intent and does not prove authorization, identity,
compliance, or policy safety. Every number here is a real measurement;
verify's coincidence-not-causation rule still applies throughout.
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

    if regressed_any or contract_regressed or policy_failed:
        verdict = VERDICT_REGRESSED
    elif vm["passed"]:
        verdict = VERDICT_IMPROVED
    else:
        verdict = VERDICT_INCONCLUSIVE

    exit_code = EXIT_IMPROVED if verdict == VERDICT_IMPROVED else EXIT_FAIL
    conclusion = _conclusion(verdict, v, cv, policy_result, contract_regressed)

    return {
        **base,
        "apply": apply_result,
        "verdict": verdict,
        "exit_code": exit_code,
        "verify": v,
        "contract_verify": cv,
        "attribution": attribution,
        "refusal": None,
        "conclusion": conclusion,
        "honest": _HONEST,
    }


def _conclusion(verdict, v, cv, policy_result, contract_regressed) -> str:
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
    else:
        head = "INCONCLUSIVE -- fail-closed, not a soft pass"
    return f"{head}: {detail}. hotato reports coincidence, not causation."


# --- text rendering ------------------------------------------------------

def render_text(t: dict) -> str:
    lines = [
        f"hotato fix trial [{t['verdict'].upper()}] "
        f"patch={t.get('patch_source')!r} name={t.get('name')!r}",
        f"  {t['conclusion']}",
    ]
    if t["verdict"] == VERDICT_REFUSED:
        lines.append("")
        lines.extend(f"  {ln}" for ln in t["apply"]["refusal"]["lines"])
        lines.append(f"  {t['honest']}")
        return "\n".join(lines)

    lines.append("")
    lines.append("-- verify: battery-scale before/after proof --")
    lines.append(_verify.render_text(t["verify"]))
    if t.get("contract_verify"):
        lines.append("")
        lines.append("-- contract verify (neighbouring cases) --")
        lines.append(_contract.render_verify_text(t["contract_verify"]))
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

    if verdict == VERDICT_REFUSED:
        refusal = t["apply"]["refusal"]
        extra = (
            '<section class="card"><div class="ctitle">'
            + esc(refusal["headline"]) + '</div>'
            '<div class="reasons">' + esc(refusal["reason"]) + '</div>'
            '<div class="does">Recommended: ' + esc(refusal["recommended"])
            + '</div></section>'
        )
        body = f'<div class="wrap">{head}<main>{summary}{concl}{extra}</main></div>'
        return _wrap_html(f"hotato fix trial: {verdict}", body)

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

    attribution_section = _attribution_html(esc, t.get("attribution"))

    body = (
        f'<div class="wrap">{head}<main>{summary}{concl}{verify_section}'
        f'{contract_section}{attribution_section}</main></div>'
    )
    return _wrap_html(f"hotato fix trial: {verdict}", body)
