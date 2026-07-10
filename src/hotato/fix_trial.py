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
  whichever axis it is on), no contract regressed, ``policy`` (if given)
  passed, every before target and hold has an after counterpart, AND the
  audio identity every guarded fixture rests on is VERIFIABLE (below).
* ``regressed`` -- any fixture regressed, a contract regressed, or the
  policy failed. Exits the same non-zero code as ``inconclusive``.
* ``inconclusive`` -- neither improved nor regressed: too few previously-
  failing fixtures to characterize (below ``min_n``), nothing that used to
  fail now passes, or the audio identity is present-but-UNVERIFIABLE (a
  malformed provenance block, a missing block, or a well-formed assertion
  hotato could not recompute at trial time). INCONCLUSIVE IS NOT A PASS: it
  is fail-closed, so CI never treats "we could not tell" as green.
* ``refused`` -- apply's both-axes threshold-funnel gate fires before any
  before/after evidence is read; OR the after set drops a required before
  fixture (an incomplete, cherry-picked comparison); OR a guarded fixture's
  recorded provenance does NOT match the audio present on disk; OR the
  before/after audio a guarded fixture rests on is the SAME conversation
  (identical decoded PCM -- a re-score, not a recapture). The refusal is a
  FEATURE, not an error: every path shares the distinct exit code apply's
  own refusal uses.

Hotato does not infer intent and does not prove authorization, identity,
compliance, or policy safety. Every number here is a real measurement;
verify's coincidence-not-causation rule still applies throughout. This is an
offline tool: a user who controls every input can always lie to themselves.
The guard's job is narrower and honest: make the motivated failure modes
impossible or loud, recompute what can be recomputed from the actual files,
and state exactly what was and was NOT verified.

Fresh-capture provenance guard: an ``improved`` verdict is never reachable on
unverifiable evidence. For every guarded fixture -- the fail->pass targets AND
the still-passing holds (a frozen hold is a re-score too) -- this module reads
the ``audio_provenance`` each side recorded and:

* VALIDATES it without touching disk: every ``sha256`` / ``pcm_sha256`` must
  be 64-char lowercase hex, each side's ``sample_rate`` / ``num_samples`` must
  be plausible, and the top-level digest must be consistent with the per-side
  digests it claims to combine. A malformed block is UNKNOWN (inconclusive),
  never "a distinct recording".
* RECOMPUTES it when the audio is present next to the envelope: the raw and
  decoded-PCM sha256 are recomputed at trial time; a digest that disagrees
  with the bytes on disk is a hand-edit, and the verdict is ``refused``.
* compares DECODED PCM (not raw bytes) before vs. after: identical decoded
  audio is the same conversation re-scored -- ``refused`` -- and because the
  comparison is on samples, a header-only edit or a trailing-byte append
  cannot disguise a re-score as a fresh capture. When a side records no
  ``pcm_sha256`` (an older envelope) the check falls back to the raw digest
  AND marks the fixture unverified, so it cannot reach ``improved``.
* treats a well-formed identity hotato could NOT recompute (the audio was not
  present) as UNVERIFIABLE: asserted, not proven, so ``inconclusive`` -- a fix
  claim requires provenance hotato can recompute.

The per-fixture identities and their status are surfaced in every report so a
reader sees exactly what was verified, not just the final verdict.
"""

from __future__ import annotations

import hashlib
import os
import re
import wave
from datetime import datetime, timezone
from typing import Optional

from . import apply as _apply
from . import contract as _contract
from . import core as _core
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

# fix trial calls apply.build_apply (never apply.create_clone), on every
# path, every verdict: the "apply" step this trial evaluates is ALWAYS a
# dry-run preview of the patch, never an execution against a real clone or
# agent. A reader who sees only the verdict chip -- IMPROVED, in green --
# must not be able to mistake that for "and it was applied": the receipt is
# rendered next to the verdict in every surface (text, JSON, HTML), not left
# to the buried "apply" sub-object.
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
#
# An "improved" verdict is only reachable when the audio identity the report
# rests on is VERIFIABLE: well-formed, internally consistent, freshly captured
# (distinct decoded PCM before vs. after), and -- when the audio is present next
# to the envelope -- recomputed at trial time to match what the envelope claims.
# The guard never trusts a digest string on its own. Every downgrade path is
# fail-closed: an envelope that merely ASSERTS an identity hotato cannot
# recompute can never earn "improved". The philosophy is not attacker-proof (a
# user who controls every input can always lie to themselves offline); it is
# that the honest-but-motivated failure modes become impossible or loud, and
# the report states exactly what was and was NOT verified.

# A recorded sha256 / pcm_sha256 must be a real digest shape before it means
# anything: 64 lowercase hex characters (what hashlib.sha256().hexdigest()
# emits). A non-hex or wrong-length string is not "a different recording", it is
# an unvalidated assertion, and the fixture it names cannot reach "improved".
_HEX64_RE = re.compile(r"^[0-9a-f]{64}$")

# Plausible-audio metadata bounds. A sample rate outside this window or a
# non-positive frame count is absurd on its face (recon's forged block used
# sample_rate 123, num_samples -5): the block is malformed, treated as UNKNOWN.
_MIN_SAMPLE_RATE = 4000
_MAX_SAMPLE_RATE = 384000


def _is_hex64(s) -> bool:
    return isinstance(s, str) and bool(_HEX64_RE.match(s))


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


def _hold_fixtures(v: dict) -> list:
    """The hold / opposite-risk fixtures that passed on BOTH sides (verify's
    ``hold_axis.hold_guards`` set). A naive bandaid can freeze a hold's audio
    (re-score byte-identical evidence) so it never appears to regress; holds
    therefore get the SAME fresh-capture / identity guard the targets get."""
    return [
        r for r in v.get("per_fixture", [])
        if r["expect"] == "hold" and r["before"]["passed"] and r["after"]["passed"]
    ]


def _guarded_fixtures(v: dict) -> list:
    """Every fixture the verdict rests on: the fail->pass targets AND the
    still-passing holds. Both must clear the identity guard for 'improved'."""
    seen, out = set(), []
    for r in _target_fixtures(v) + _hold_fixtures(v):
        if r["fixture"] not in seen:
            seen.add(r["fixture"])
            out.append(r)
    return out


def _short_sha(sha) -> Optional[str]:
    return sha[:12] if isinstance(sha, str) and sha else None


def _validate_provenance(prov: dict) -> tuple:
    """Format + metadata + internal-consistency validation of one
    ``audio_provenance`` block, WITHOUT touching disk. Returns ``(ok, reason)``.

    Checks (each failure => the block is UNKNOWN, never 'a distinct recording'):
    every present ``sha256`` / ``pcm_sha256`` is 64-char lowercase hex; each
    side carries a plausible ``sample_rate`` and a positive ``num_samples``; and
    the top-level ``sha256`` is exactly what ``core._audio_provenance`` would
    compose from the per-side digests (one side's own digest, or the order-stable
    sha256 of the concatenated side digests). An inconsistent top-level digest
    means the nested structure and the headline disagree -- unverifiable."""
    if not isinstance(prov, dict):
        return False, "audio_provenance is not an object"
    top = prov.get("sha256")
    if not _is_hex64(top):
        return False, "top-level sha256 is not 64-char lowercase hex"
    sides = prov.get("sides")
    if not isinstance(sides, list) or not sides:
        return False, "audio_provenance carries no sides"
    side_shas = []
    for i, s in enumerate(sides):
        if not isinstance(s, dict):
            return False, f"side {i} is not an object"
        ssha = s.get("sha256")
        if not _is_hex64(ssha):
            return False, f"side {i} sha256 is not 64-char lowercase hex"
        side_shas.append(ssha)
        pcm = s.get("pcm_sha256")
        if pcm is not None and not _is_hex64(pcm):
            return False, f"side {i} pcm_sha256 is not 64-char lowercase hex"
        sr = s.get("sample_rate")
        if (not isinstance(sr, int) or isinstance(sr, bool)
                or not _MIN_SAMPLE_RATE <= sr <= _MAX_SAMPLE_RATE):
            return False, (
                f"side {i} sample_rate {sr!r} is not a plausible audio rate "
                f"({_MIN_SAMPLE_RATE}..{_MAX_SAMPLE_RATE} Hz)")
        ns = s.get("num_samples")
        if not isinstance(ns, int) or isinstance(ns, bool) or ns <= 0:
            return False, f"side {i} num_samples {ns!r} is not a positive integer"
    if len(side_shas) == 1:
        expected = side_shas[0]
    else:
        h = hashlib.sha256()
        for ssha in side_shas:
            h.update(ssha.encode("ascii"))
        expected = h.hexdigest()
    if top != expected:
        return False, (
            "top-level sha256 does not match the per-side digests it claims to "
            "combine")
    return True, None


def _identity(prov: dict) -> tuple:
    """A comparable identity for a validated block, plus whether it is a DECODED
    (pcm) identity. When every side records a ``pcm_sha256`` the identity is the
    order-stable combination of those (content of the samples, immune to header
    edits / trailing-byte appends); otherwise it falls back to the raw top-level
    ``sha256`` (container identity) and reports ``pcm=False`` so the caller marks
    the fixture unverified."""
    sides = prov.get("sides") or []
    pcms = [s.get("pcm_sha256") for s in sides if isinstance(s, dict)]
    if sides and len(pcms) == len(sides) and all(_is_hex64(p) for p in pcms):
        if len(pcms) == 1:
            return pcms[0], True
        h = hashlib.sha256()
        for p in pcms:
            h.update(p.encode("ascii"))
        return h.hexdigest(), True
    return prov.get("sha256"), False


def _recompute_side(base_dir: Optional[str], side: dict) -> tuple:
    """Best-effort recompute of ONE side against the file it names, resolved
    next to the envelope (only a basename survives capture). Returns
    ``(status, detail)`` where status is:

    * ``"match"``    -- the file is present and its recomputed raw (and, when
      recorded, decoded PCM) sha256 equals what the envelope claims;
    * ``"mismatch"`` -- the file is present but its bytes/samples do NOT match
      the envelope: the provenance was hand-edited or the audio was swapped;
    * ``"absent"``   -- no file to recompute from (nothing is asserted false,
      but nothing is confirmed either)."""
    path = side.get("path")
    if not isinstance(path, str) or not path:
        return "absent", "no path recorded"
    name = os.path.basename(path)
    if not base_dir:
        return "absent", f"{name} not resolvable (envelope directory unknown)"
    fp = os.path.join(base_dir, name)
    if not os.path.isfile(fp):
        return "absent", f"{name} is not present next to the envelope"
    try:
        raw = _core._stream_sha256(fp)
    except OSError as exc:
        return "absent", f"{name} could not be read ({exc})"
    if raw != side.get("sha256"):
        return "mismatch", f"{name} raw sha256 does not match the envelope"
    pcm_recorded = side.get("pcm_sha256")
    if pcm_recorded is not None:
        try:
            pcm = _core._stream_pcm_sha256(fp)
        except (OSError, wave.Error, EOFError) as exc:
            return "absent", f"{name} is not a decodable WAV ({exc})"
        if pcm != pcm_recorded:
            return "mismatch", f"{name} decoded PCM does not match the envelope"
    return "match", name


def _fixture_provenance(r: dict, before_base: Optional[str],
                        after_base: Optional[str]) -> dict:
    """Evaluate one guarded fixture's before/after audio identity end to end:
    presence, format/metadata/consistency validation, recompute-against-disk,
    and the decoded-PCM fresh-capture comparison. ``status`` is one of
    ``verified`` (distinct, recomputed, decodable), ``same_audio`` (a re-score
    of the same conversation), ``mismatch`` (envelope disagrees with disk),
    ``invalid`` (malformed block), ``missing`` (no block on a side), or
    ``unverifiable`` (a well-formed but un-recomputable assertion)."""
    bp = r["before"].get("audio_provenance")
    ap = r["after"].get("audio_provenance")
    b_present = isinstance(bp, dict)
    a_present = isinstance(ap, dict)
    b_sha = bp.get("sha256") if b_present else None
    a_sha = ap.get("sha256") if a_present else None
    out = {
        "fixture": r["fixture"],
        "role": r["expect"],
        "before_sha256": b_sha,
        "after_sha256": a_sha,
        "before_short": _short_sha(b_sha),
        "after_short": _short_sha(a_sha),
    }

    if not b_present or not a_present:
        missing = []
        if not b_present:
            missing.append("before")
        if not a_present:
            missing.append("after")
        out["status"] = "missing"
        out["detail"] = " and ".join(missing) + " missing"
        return out

    ok_b, reason_b = _validate_provenance(bp)
    ok_a, reason_a = _validate_provenance(ap)
    if not ok_b or not ok_a:
        side = "before" if not ok_b else "after"
        out["status"] = "invalid"
        out["detail"] = f"{side}: {reason_b if not ok_b else reason_a}"
        return out

    b_id, b_pcm = _identity(bp)
    a_id, a_pcm = _identity(ap)
    pcm_basis = b_pcm and a_pcm

    # Same conversation: identical decoded PCM (or, when PCM is unrecorded,
    # identical raw bytes). A re-score of frozen evidence is never a fix.
    if b_id == a_id:
        out["status"] = "same_audio"
        out["identity_basis"] = "pcm" if pcm_basis else "raw"
        return out

    # Recompute both blocks against any audio present on disk.
    any_mismatch = None
    all_present = True
    for base, prov in ((before_base, bp), (after_base, ap)):
        for s in prov["sides"]:
            st, detail = _recompute_side(base, s)
            if st == "mismatch":
                any_mismatch = detail
                break
            if st != "match":
                all_present = False
        if any_mismatch:
            break

    if any_mismatch:
        out["status"] = "mismatch"
        out["detail"] = any_mismatch
        return out

    if not pcm_basis:
        # Distinct raw containers, but at least one side records no decoded-PCM
        # digest: a header edit alone can make two byte-different files decode
        # to the SAME conversation, so raw-distinct is not proof of a fresh
        # capture. Fall back to raw for the same-audio check above, and mark
        # this unverified so it can never reach 'improved'.
        out["status"] = "unverifiable"
        out["detail"] = (
            "a decoded-PCM digest is missing on at least one side, so a fresh "
            "capture cannot be confirmed from the raw digest alone")
        return out

    if not all_present:
        out["status"] = "unverifiable"
        out["detail"] = (
            "audio identity was asserted by the envelope but not recomputed at "
            "trial time (the audio was not present)")
        return out

    out["status"] = "verified"
    return out


def _provenance_report(v: dict, before_base: Optional[str] = None,
                       after_base: Optional[str] = None) -> dict:
    """Per-fixture provenance for every GUARDED fixture (targets + holds), plus
    the single issue that must gate the verdict. Precedence, strongest first: a
    recompute ``mismatch`` and a ``same_audio`` re-score are confirmed forgeries
    (refuse); an ``invalid`` block, a ``missing`` block, and an ``unverifiable``
    assertion are unknowns (inconclusive, never 'improved')."""
    fixtures = [
        _fixture_provenance(r, before_base, after_base)
        for r in _guarded_fixtures(v)
    ]

    def pick(status):
        return [f for f in fixtures if f["status"] == status]

    mismatch = pick("mismatch")
    same = pick("same_audio")
    invalid = pick("invalid")
    missing = pick("missing")
    unverifiable = pick("unverifiable")
    if mismatch:
        issue = {"kind": "recompute_mismatch", "fixtures": mismatch}
    elif same:
        issue = {"kind": "same_audio", "fixtures": same}
    elif invalid:
        issue = {"kind": "invalid_provenance", "fixtures": invalid}
    elif missing:
        issue = {"kind": "unknown_provenance", "fixtures": missing}
    elif unverifiable:
        issue = {"kind": "unverifiable", "fixtures": unverifiable}
    else:
        issue = None
    return {"target_fixtures": fixtures, "issue": issue}


_PROVENANCE_REFUSAL_HEADLINE = "No fix will be certified from re-scored audio"
_MISMATCH_REFUSAL_HEADLINE = (
    "Envelope provenance does not match the audio on disk")
_INCOMPLETE_REFUSAL_HEADLINE = (
    "No fix will be certified from an incomplete after set")


def _refusal(headline: str, reason: str, recommended: str, why: str) -> dict:
    """Build the SAME refusal shape apply's own gate uses (headline / reason /
    recommended / lines / why), so both renderers and callers already branching
    on ``t["refusal"]["lines"]`` handle every refusal identically."""
    return {
        "headline": headline,
        "reason": reason,
        "recommended": recommended,
        "lines": [headline, f"Reason: {reason}", f"Recommended: {recommended}"],
        "why": why,
    }


def _same_audio_refusal(issue: dict) -> dict:
    names = ", ".join(f["fixture"] for f in issue["fixtures"])
    basis = ("decoded PCM" if all(
        f.get("identity_basis") == "pcm" for f in issue["fixtures"])
        else "audio")
    reason = (
        f"{len(issue['fixtures'])} fixture(s) this verdict rests on ({names}) "
        f"have identical before/after {basis}: the after run re-scored the SAME "
        "conversation the before run scored, just against a different threshold "
        "or scorer config"
    )
    recommended = (
        "recapture the fixture(s) through the applied clone "
        "(hotato apply --clone --yes) and re-run hotato fix trial against "
        "the new after evidence"
    )
    return _refusal(
        _PROVENANCE_REFUSAL_HEADLINE, reason, recommended,
        why=(
            "A legitimate improvement claim requires the AFTER evidence to be "
            "a fresh recording, not a re-score of the BEFORE recording under a "
            "looser threshold. Two runs over the same decoded audio show "
            "nothing about a code, config, or model change; they only show the "
            "scorer's threshold moved. Comparing decoded PCM (not raw bytes) "
            "means a header-only edit or a trailing-byte append cannot dress a "
            "re-score up as a fresh capture."
        ),
    )


def _mismatch_refusal(issue: dict) -> dict:
    names = ", ".join(f["fixture"] for f in issue["fixtures"])
    details = "; ".join(
        f["detail"] for f in issue["fixtures"] if f.get("detail"))
    reason = (
        f"{len(issue['fixtures'])} fixture(s) this verdict rests on ({names}) "
        "carry an audio_provenance digest that does NOT match the audio present "
        f"on disk ({details}): the recorded identity was hand-edited or the "
        "audio was swapped after capture"
    )
    recommended = (
        "recapture the fixture(s) with a current hotato build so the envelope "
        "records the identity of the audio actually scored, and re-run"
    )
    return _refusal(
        _MISMATCH_REFUSAL_HEADLINE, reason, recommended,
        why=(
            "When the audio is present next to the envelope, hotato recomputes "
            "its raw and decoded-PCM sha256 at trial time and refuses if the "
            "envelope disagrees with the file. A digest a reader cannot "
            "reproduce from the bytes on disk is an assertion, not evidence."
        ),
    )


def _incomplete_after_refusal(required: list) -> dict:
    names = ", ".join(f"{r['fixture']} ({r['role']})" for r in required)
    reason = (
        f"{len(required)} fixture(s) present in the before battery are missing "
        f"from the after battery ({names}): a fail->pass target or a hold guard "
        "was dropped from the after set, so the before/after comparison is over "
        "a cherry-picked subset"
    )
    recommended = (
        "re-capture the FULL battery through the applied clone so every before "
        "fixture has an after counterpart, and re-run"
    )
    return _refusal(
        _INCOMPLETE_REFUSAL_HEADLINE, reason, recommended,
        why=(
            "An 'improved' verdict must compare the same battery on both sides. "
            "Omitting a target that did not get fixed, or a hold that would "
            "regress, would let a cherry-picked after set read as a clean pass. "
            "Every before target and every before hold must reappear after."
        ),
    )


def _invalid_provenance_reason(issue: dict) -> str:
    parts = [
        f"{f['fixture']} ({f['detail']})"
        for f in issue["fixtures"]
    ]
    return (
        "Provenance guard: audio identity is MALFORMED for "
        + ", ".join(parts)
        + ". A digest that is not well-formed hex, an implausible sample rate "
        "or frame count, or a top-level digest inconsistent with the per-side "
        "digests is an unvalidated assertion, not a distinct recording, so this "
        "cannot be certified 'improved'; recapture with a current hotato build "
        "and re-run."
    )


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


def _unverifiable_reason(issue: dict) -> str:
    names = ", ".join(f["fixture"] for f in issue["fixtures"])
    return (
        "Provenance guard: audio identity was asserted by the envelope but not "
        f"recomputed at trial time for {names} (the audio was not present); a "
        "fix claim requires provenance hotato can recompute. Re-run with the "
        "captured audio present next to the envelopes (or recapture through the "
        "applied clone) so the identity is verified, not just declared."
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

    # The apply receipt: fix trial never calls apply.create_clone, so this
    # dry_run/created/applies_change trio is ALWAYS dry_run=True /
    # created=False / applies_change=False, on every path, every verdict --
    # even improved. It is promoted out of the buried "apply" sub-object into
    # top-level fields every render surfaces beside the verdict, because a
    # reader must never have to open the raw JSON to learn the trial evaluated
    # a preview, not a change actually applied to a clone or an agent.
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
        # The effective min_n is echoed in every surface (text/json/html) so a
        # lowered floor is always visible: a caller cannot quietly drop the bar
        # to 1 and have it pass unremarked.
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
    # (the report always shows the guarded fixtures' before/after digests), but
    # it can only ever DOWNGRADE an otherwise-improved verdict -- a real
    # regression stays a regression whether or not the recapture can be
    # verified; fail-closed needs no proof to reject, only to accept. Side audio
    # is resolved (best effort) next to the envelope it came from, so a captured
    # WAV kept beside its run.json is recomputed at trial time.
    before_base = before if os.path.isdir(before) else os.path.dirname(before)
    after_base = after if os.path.isdir(after) else os.path.dirname(after)
    provenance = _provenance_report(v, before_base or ".", after_base or ".")
    issue = provenance["issue"]

    # Completeness: every before target AND before hold must reappear in the
    # after set. A required fixture present only in before means the comparison
    # is over a cherry-picked subset (an omitted hold that would regress, or an
    # unfixed target quietly dropped), so 'improved' is not reachable.
    required_only_before = v["unpaired"].get("only_before_required") or []

    refusal = None
    refusal_kind = None

    if regressed_any or contract_regressed or policy_failed:
        verdict = VERDICT_REGRESSED
    elif vm["passed"]:
        if required_only_before:
            verdict = VERDICT_REFUSED
            refusal = _incomplete_after_refusal(required_only_before)
            refusal_kind = "incomplete_after"
        elif issue and issue["kind"] == "recompute_mismatch":
            verdict = VERDICT_REFUSED
            refusal = _mismatch_refusal(issue)
            refusal_kind = "recompute_mismatch"
        elif issue and issue["kind"] == "same_audio":
            verdict = VERDICT_REFUSED
            refusal = _same_audio_refusal(issue)
            refusal_kind = "same_audio_recapture"
        elif issue:  # invalid / unknown / unverifiable -> inconclusive
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
    conclusion = _conclusion(verdict, v, cv, policy_result, contract_regressed,
                             issue, refusal_kind, required_only_before)

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
                 provenance_issue=None, refusal_kind=None,
                 required_only_before=None) -> str:
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
    # Append the precise reason for a downgrade, matching the verdict path.
    if verdict == VERDICT_REFUSED:
        if refusal_kind == "incomplete_after" and required_only_before:
            tail += " " + _incomplete_after_refusal(required_only_before)["reason"] + "."
        elif refusal_kind == "recompute_mismatch" and provenance_issue:
            tail += " " + _mismatch_refusal(provenance_issue)["reason"] + "."
        elif refusal_kind == "same_audio_recapture" and provenance_issue:
            tail += " " + _same_audio_refusal(provenance_issue)["reason"] + "."
    elif verdict == VERDICT_INCONCLUSIVE and provenance_issue:
        kind = provenance_issue["kind"]
        if kind == "invalid_provenance":
            tail += " " + _invalid_provenance_reason(provenance_issue)
        elif kind == "unknown_provenance":
            tail += " " + _unknown_provenance_reason(provenance_issue)
        elif kind == "unverifiable":
            tail += " " + _unverifiable_reason(provenance_issue)
    return tail


# --- text rendering ------------------------------------------------------

def render_text(t: dict) -> str:
    # The apply receipt renders right beside the verdict, on EVERY path
    # (including the apply-gate refusal, which returns early below): fix
    # trial always evaluates a dry-run patch preview, never an applied
    # change, so a reader must never have to open the raw JSON to learn that.
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
    # A verify claim that reads "supported" on its own does not stand alone
    # when the PARENT fix-trial verdict is not improved (a provenance,
    # completeness, contract, or policy issue downgraded it): mark the nested
    # CLAIM line with the verdict that actually controls, so a cropped view
    # of just this block cannot read as a clean pass.
    superseded = None if t["verdict"] == VERDICT_IMPROVED else t["verdict"]
    lines.append(_verify.render_text(t["verify"], superseded_by=superseded))
    if t.get("contract_verify"):
        lines.append("")
        lines.append("-- contract verify (neighbouring cases) --")
        lines.append(_contract.render_verify_text(t["contract_verify"]))
    prov = t.get("provenance")
    if prov and prov.get("target_fixtures"):
        lines.append("")
        lines.append("-- audio provenance: before vs after recapture --")
        lines.append(
            "  identity of the exact audio each side scored, for every target "
            "and hold fixture the verdict rests on. 'verified' means the "
            "digests are well-formed, freshly distinct (decoded PCM), and "
            "recomputed from the audio on disk; anything else is named plainly "
            "and cannot earn 'improved'. A fresh take proves the same "
            "human-labeled contract passed on new evidence, never that the "
            "change caused it."
        )
        lines.append(f"  {_PROVENANCE_CAUTION}")
        for f in prov["target_fixtures"]:
            detail = f" -- {f['detail']}" if f.get("detail") else ""
            lines.append(
                f"  {f['fixture']} ({f.get('role', '?')}): "
                f"before={f['before_short'] or 'unknown'} "
                f"after={f['after_short'] or 'unknown'} "
                f"[{f['status']}]{detail}"
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
    rows = []
    for f in prov["target_fixtures"]:
        detail = f" -- {f['detail']}" if f.get("detail") else ""
        rows.append((
            f"{f['fixture']} ({f.get('role', '?')})",
            f"{f['before_short'] or 'unknown'} vs "
            f"{f['after_short'] or 'unknown'} [{f['status']}]{detail}"))
    return (
        '<section class="card"><div class="ctitle">Audio provenance: before '
        'vs after recapture</div><div class="cmpcap">Streamed sha256 '
        'identity of the exact audio each side scored, for every target and '
        'hold fixture the verdict rests on. "verified" means the digests are '
        'well-formed, freshly distinct (decoded PCM), and recomputed from the '
        'audio on disk at trial time; anything else is named plainly and '
        'cannot earn "improved". A fresh take proves the same human-labeled '
        'contract passed on new evidence; identical decoded audio means the '
        'after run re-scored the same conversation, not a fresh capture.</div>'
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

    # The apply receipt renders in the HEADER, not buried in a later card:
    # fix trial always evaluates a dry-run patch preview (it never calls
    # apply.create_clone), so a green verdict must not read as "and it was
    # applied" without this next to it.
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
    # A "supported" claim in this nested card does not stand alone when the
    # PARENT verdict is not improved: mark it with the verdict that actually
    # controls, so a cropped screenshot of just this card cannot read green
    # while the whole trial is red.
    claim_supported = v["claim"]["supported"]
    superseded = None if verdict == VERDICT_IMPROVED else verdict
    if superseded and claim_supported:
        claim_display = f"SUPERSEDED BY {superseded.upper()} (verdict controls)"
    else:
        claim_display = "supported" if claim_supported else "refused (low n)"
    verify_rows = [
        ("previously-failing fixtures now passing",
         f"{ra['now_pass']} of {ra['used_to_fail']}"),
        ("hold fixtures still passing",
         f"{ha['still_pass']} of {ha['hold_guards']}"),
        ("regressions", str(len(v["regressions"]))),
        ("claim", claim_display),
    ]
    verify_note = ""
    if superseded and claim_supported:
        verify_note = (
            '<div class="does">This claim does not stand alone: the '
            f'fix-trial verdict is {esc(superseded.upper())}, and the '
            'verdict controls, not this line.</div>'
        )
    verify_section = (
        '<section class="card"><div class="ctitle">Verify: battery-scale '
        'proof</div><div class="cmpcap">'
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

    provenance_section = _provenance_html(esc, t.get("provenance"))
    attribution_section = _attribution_html(esc, t.get("attribution"))

    body = (
        f'<div class="wrap">{head}<main>{summary}{concl}{refusal_section}'
        f'{verify_section}{contract_section}{provenance_section}'
        f'{attribution_section}</main></div>'
    )
    return _wrap_html(f"hotato fix trial: {verdict}", body)
