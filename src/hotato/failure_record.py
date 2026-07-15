"""The Failure Record v1: a share-safe PROJECTION of one failed, inconclusive,
or error result into the single canonical ``hotato.failure-record.v1`` dict.

This module performs NO evaluation. Every status is copied (or derived by a
fixed, documented precedence) from an already-evaluated source result -- a
``hotato.test-run`` result, one test entry of a ``hotato.suite-run`` result, or
one contract of a ``contract-verify`` envelope. A projection can never change a
source verdict, and a record whose evidence is missing, malformed, or
unsupported can never come out as PASS -- an all-pass source is REFUSED with
:class:`NoFailureError` instead of mislabeled.

Structural honesty invariants (each enforced by :func:`validate_record`, the
same oracle the reference conformance kit runs):

* FIVE separate lanes (outcome / policy / conversation / speech / reliability),
  each with its OWN status. There is no aggregate, blended, or overall score
  anywhere in the record.
* The deterministic gate is derived ONLY from deterministic assertions. The
  model-judged advisory lane is reported separately; when its gate is not
  enabled it cannot change the record status, and an unavailable advisory
  backend never changes the deterministic verdict.
* An OUTCOME claim must cite tool-call / state / trace evidence. Transcript
  text can never establish an outcome: a transcript-only assertion kind tagged
  into the outcome lane is refused at projection time, and a rendered outcome
  claim without tool/state-class evidence is refused at validation time.
* Evidence references are RELATIVE paths plus sha256 digests -- never an
  absolute path, parent traversal, or inline payload. Digests are re-verified
  against the files when they are present.
* SAFE PROJECTION by default: no raw audio, transcript body, tool payload
  value, state value, credential, environment value, or absolute path is
  copied into the record. Assertion summaries use the evaluators' own safe
  reason language, bounded and scrubbed of path-like strings.
* ``record_id`` is a full content address: ``"sha256:" + hex(sha256())`` over
  the canonical identity JSON, which excludes ``record_id`` itself and contains
  no wall-clock field (the record carries none, so regeneration is
  byte-deterministic).
"""

from __future__ import annotations

import copy
import hashlib
import json
import math
import os
import re
from typing import Any, Dict, List, Optional, Tuple

from . import __version__
from .errors import format_public_number as _fmt_public_number

__all__ = [
    "KIND",
    "VERSION",
    "LANES",
    "NoFailureError",
    "SelectorError",
    "canonical_identity_bytes",
    "compute_record_id",
    "digest_bytes",
    "project",
    "select_source",
    "validate_record",
]

KIND = "hotato.failure-record.v1"
VERSION = "1.0"
LANES = ("outcome", "policy", "conversation", "speech", "reliability")

# The closed top-level contract (identical to the reference conformance kit).
TOP_LEVEL_KEYS = frozenset({
    "kind", "version", "record_id", "status", "headline", "subject", "origin",
    "gate", "advisory", "dimensions", "evidence", "reproduction", "privacy",
    "provenance",
})

PRIVACY_FALSE_FIELDS = (
    "raw_audio_embedded", "transcript_body_embedded", "tool_payload_embedded",
    "state_value_embedded", "credential_embedded", "absolute_path_embedded",
)

_SAFE_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:@/+~-]*$")

# Assertion kinds whose evaluators read ONLY transcript text. Transcript text
# can never establish an outcome claim (an agent SAYING "I issued the refund"
# is not a refund), so these kinds are refused in the outcome lane.
TRANSCRIPT_ONLY_KINDS = frozenset({"phrase", "pii", "policy", "entity_accuracy"})

# Evidence-reference kinds that CAN support an outcome claim (tool spans,
# state snapshots/changes, the authenticated trace, machine timing).
OUTCOME_EVIDENCE_KINDS = frozenset({
    "tool_call", "tool_result", "state_snapshot", "state_change",
    "trace_span", "timing_measurement",
})

# assert.v1 result kind -> the evidence-reference kind its supporting evidence
# is catalogued under.
_ASSERT_KIND_TO_EVIDENCE_KIND = {
    "tool_call": "tool_call",
    "tool_result": "tool_result",
    "tool_error": "tool_result",
    "state": "state_snapshot",
    "state_change": "state_change",
    "outcome": "trace_span",
    "handoff": "trace_span",
    "dtmf": "trace_span",
    "termination": "trace_span",
    "sequence": "trace_span",
    "count": "trace_span",
    "latency": "timing_measurement",
    "timing_contract": "timing_measurement",
    "phrase": "transcript_span",
    "pii": "transcript_span",
    "entity_accuracy": "transcript_span",
    "policy": "policy_match",
}

# An UNTAGGED assertion falls back to its kind's natural dimension (a fixed,
# documented mapping -- structure, never a judgment). An explicit ``dimension``
# tag on the assertion always wins. speech / reliability are reachable only by
# an explicit tag: nothing is silently promoted into them.
_KIND_DEFAULT_DIMENSION = {
    "tool_call": "outcome", "tool_result": "outcome", "tool_error": "outcome",
    "state": "outcome", "state_change": "outcome", "outcome": "outcome",
    "phrase": "policy", "pii": "policy", "policy": "policy",
    "entity_accuracy": "policy",
    "latency": "conversation", "timing_contract": "conversation",
    "handoff": "conversation", "dtmf": "conversation",
    "termination": "conversation", "sequence": "conversation",
    "count": "conversation",
}

# conversation.v1 manifest artifact name -> evidence-reference kind + the
# redaction classes its referenced content carries.
_ARTIFACT_EVIDENCE = {
    "audio": ("audio_interval", False, ()),
    "transcript": ("transcript_span", True, ("transcript-body",)),
    "trace": ("trace_span", True, ("tool-arguments", "tool-result")),
    "timing": ("timing_measurement", False, ()),
    "assertions": ("configuration", False, ()),
}

_SUMMARY_LIMIT = 240

# Path-like substrings are scrubbed out of evaluator reason text before it
# enters the record, and refused ANYWHERE in the record at validation time: an
# exception message can embed an absolute filesystem path, and the safe
# projection excludes absolute paths outright. Recognizes a POSIX root, a ``~``
# home path, a Windows drive path (``C:\``), and a UNC path
# (``\\server\share``) -- either separator, mixed, and EMBEDDED mid-sentence,
# not only as a value that starts with one. The leading boundary keeps it off
# relative locators (``artifacts/foo/bar``, ``application/json``) and URL
# schemes (``https://...``), whose separators are preceded by a path/segment
# character.
_PATH_SEG = r"[^\s\\/'\":]+"
_ABS_PATH_RE = re.compile(
    r"(?<![\w.~/\\-])(?:"
    r"[A-Za-z]:[\\/](?:" + _PATH_SEG + r"[\\/]?)*"             # C:\...  /  C:/...
    r"|\\\\" + _PATH_SEG + r"(?:[\\/]" + _PATH_SEG + r")*[\\/]?"  # \\server\share\...
    r"|~(?:[\\/]" + _PATH_SEG + r")+[\\/]?"                     # ~/...
    r"|(?:[\\/]" + _PATH_SEG + r"){2,}[\\/]?"                  # /a/b  /  \a\b (mixed)
    r")"
)


class NoFailureError(ValueError):
    """The source contains no failure: nothing to record."""


class SelectorError(ValueError):
    """The selector matched zero or several candidate results."""


# =========================================================================
# canonical identity + content address
# =========================================================================

def digest_bytes(data: bytes) -> str:
    return "sha256:" + hashlib.sha256(data).hexdigest()


def canonical_identity_bytes(record: Dict[str, Any]) -> bytes:
    """The canonical identity JSON bytes of a record: every field EXCEPT
    ``record_id`` itself, serialized with sorted keys, no insignificant
    whitespace, UTF-8 text (``ensure_ascii=False``), finite numbers only.
    Byte-identical to the reference conformance kit's canonicalization, so a
    record and the kit's oracle compute the same address. The record carries
    no wall-clock field, so identity is stable across regenerations."""
    identity = copy.deepcopy(record)
    identity.pop("record_id", None)
    return json.dumps(
        identity, sort_keys=True, separators=(",", ":"),
        ensure_ascii=False, allow_nan=False,
    ).encode("utf-8")


def compute_record_id(record: Dict[str, Any]) -> str:
    return digest_bytes(canonical_identity_bytes(record))


# =========================================================================
# small shared helpers
# =========================================================================

def _canonical_json_bytes(obj: Any) -> bytes:
    return json.dumps(
        obj, sort_keys=True, separators=(",", ":"),
        ensure_ascii=False, allow_nan=False,
    ).encode("utf-8")


def _require_safe_id(value: Any, field: str) -> str:
    if not isinstance(value, str) or not (1 <= len(value) <= 160) \
            or not _SAFE_ID_RE.match(value):
        raise ValueError(
            f"{field} is not a safe identifier (letters, digits, and "
            "._:@/+~- after a leading letter or digit, at most 160 chars); "
            "refusing to project it"
        )
    return value


def _safe_relative(value: str) -> bool:
    """True for a path that is relative, traversal-free, and not a Windows
    drive path. Identical to the reference kit's rule (a bare ``.`` is the
    valid working-directory form)."""
    if value == ".":
        return True
    if not value or value.startswith(("/", "\\")) or re.match(r"^[A-Za-z]:", value):
        return False
    base = value.split("#", 1)[0]
    parts = [p for p in base.replace("\\", "/").split("/") if p]
    return ".." not in parts


def _scrub_summary(text: str) -> str:
    """Bound + scrub one evaluator-provided summary line: newlines collapse,
    absolute-path-like substrings are replaced with the literal ``[path]``,
    and the result is truncated to the schema's 240-char bound."""
    flat = " ".join(str(text).split())
    flat = _ABS_PATH_RE.sub("[path]", flat)
    if len(flat) > _SUMMARY_LIMIT:
        flat = flat[: _SUMMARY_LIMIT - 3] + "..."
    return flat or "no detail was provided by the evaluator"


# The evidence an INCONCLUSIVE result of each assert.v1 KIND was missing --
# derived from the row's structured ``kind``, never from its reason text (a
# fixed vocabulary that can carry no payload). Absent kinds map to the generic
# ``required-input``.
_KIND_MISSING_EVIDENCE = {
    "phrase": ["transcript"], "pii": ["transcript"], "policy": ["transcript"],
    "tool_call": ["trace"], "tool_result": ["trace"], "tool_error": ["trace"],
    "handoff": ["trace"], "dtmf": ["trace"], "termination": ["trace"],
    "sequence": ["trace"], "entity_accuracy": ["trace"], "count": ["trace"],
    "state": ["state-adapter"], "state_change": ["state-adapter"],
    "latency": ["timing"], "timing_contract": ["timing"],
}


def _missing_evidence_for_kind(kind: Optional[str]) -> List[str]:
    return list(_KIND_MISSING_EVIDENCE.get(kind, ["required-input"]))


def _derived_gate_status(record: Dict[str, Any]) -> str:
    """The deterministic gate status derived ONLY from the record's own
    deterministic assertions (ERROR > FAIL > INCONCLUSIVE > PASS). The same
    precedence the reference kit's oracle applies."""
    statuses: List[str] = []
    for lane in LANES:
        for assertion in record["dimensions"][lane].get("assertions", []):
            if assertion.get("authority") == "deterministic":
                statuses.append(assertion["status"])
    if "ERROR" in statuses:
        return "ERROR"
    if "FAIL" in statuses:
        return "FAIL"
    if "INCONCLUSIVE" in statuses:
        return "INCONCLUSIVE"
    return "PASS"


def _wilson_from_ci(ci: Optional[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    """Project the repository's reliability ``ci`` block (simulate._wilson_ci:
    low/high/method/z) onto the record's interval shape. Values are COPIED,
    never recomputed; only the z score is translated into the confidence level
    it encodes (two-sided normal mass, the textbook relation)."""
    if not ci:
        return None
    if ci.get("method") != "wilson":
        raise ValueError(
            "reliability interval method is not wilson; refusing to relabel it"
        )
    z = float(ci["z"])
    confidence = 0.95 if z == 1.96 else round(math.erf(z / math.sqrt(2.0)), 6)
    return {
        "method": "wilson",
        "confidence": confidence,
        "lower": float(ci["low"]),
        "upper": float(ci["high"]),
    }


# =========================================================================
# assertion + evidence projection
# =========================================================================

_EXPECTED_TEMPLATES = {
    "PASS": "The declared {kind} conditions hold against the supplied evidence.",
    "FAIL": "The declared {kind} conditions hold against the supplied evidence.",
    "INCONCLUSIVE": "The declared {kind} conditions hold against the supplied evidence.",
    "ERROR": "The declared {kind} conditions hold against the supplied evidence.",
}

_OBSERVED_FALLBACK = {
    "PASS": "The {kind} assertion passed against the supplied evidence.",
    "FAIL": "The {kind} assertion failed; the evaluator recorded no further detail.",
    "INCONCLUSIVE": "Required input for the {kind} assertion was absent.",
    "ERROR": "The {kind} assertion could not be evaluated.",
}

# A conservative, share-safe FAIL sentence for a LEGACY result row -- one with
# no ``public_reason`` (an assert.v1 result recorded before that field existed).
# Built from the row's KIND alone -- never from its raw ``reason``, which can
# embed a regex body, a tool argument, a state value, or a DTMF digit. A newer
# row carries its own share-safe ``public_reason`` and never reaches this table.
_CONSERVATIVE_FAIL_FALLBACK = {
    "phrase": "A declared phrase condition was not satisfied.",
    "pii": "A PII detection fired against the declared policy.",
    "policy": "A declared policy rule was not satisfied.",
    "tool_call": "No tool call satisfied the declared call conditions.",
    "tool_result": "A declared tool-result condition was not satisfied.",
    "tool_error": "A declared tool-error condition was not satisfied.",
    "state": "The declared post-call state was not satisfied.",
    "state_change": "A declared post-call state change did not occur.",
    "outcome": ("The declared outcome conditions were not supported by supplied "
                "evidence."),
    "handoff": "A declared handoff condition was not satisfied.",
    "dtmf": "The declared DTMF condition failed.",
    "termination": "The declared termination condition failed.",
    "latency": "Measured latency exceeded its declared limit.",
    "timing_contract": "A declared timing contract failed.",
    "entity_accuracy": ("A declared entity did not match the authenticated tool "
                        "arguments."),
    "sequence": "The trace did not complete the declared sequence.",
    "count": "The declared count condition failed.",
}


def _is_number(x: Any) -> bool:
    return isinstance(x, (int, float)) and not isinstance(x, bool)


def _bound_public(text: Any) -> str:
    """Bound a share-safe ``public_reason`` to one 240-char line WITHOUT the
    path scrub -- it is already built from allowlisted structured fields, so a
    path scrub here could only ever MASK a regression (the privacy oracle in
    :func:`validate_record` still refuses any absolute path anywhere)."""
    flat = " ".join(str(text).split())
    if len(flat) > _SUMMARY_LIMIT:
        flat = flat[: _SUMMARY_LIMIT - 3] + "..."
    return flat or "no detail was provided by the evaluator"


def _conservative_public_fallback(kind: Optional[str], status: str) -> str:
    """A share-safe observed sentence for a legacy result row (one with no
    ``public_reason``), derived from KIND + STATUS only -- never the raw
    ``reason``."""
    if status == "FAIL":
        return _CONSERVATIVE_FAIL_FALLBACK.get(
            kind, "The declared assertion was not satisfied.")
    return _OBSERVED_FALLBACK[status].format(kind=kind)


def _project_result_row(
    row: Dict[str, Any],
    *,
    source_digest: str,
    artifact_refs: Dict[str, str],
) -> Tuple[str, Dict[str, Any], Dict[str, Any]]:
    """Project ONE assert.v1 result row into ``(lane, assertion, evidence)``.

    The assertion carries only allowlisted fields (never the raw row): stable
    ids, the copied status, bounded evaluator reason language, and evidence
    references. The evidence entry is the row's own digest-pinned projection
    source -- the digest of the canonical row JSON identifies the stored
    evaluator output without embedding any payload value."""
    assertion_id = _require_safe_id(row.get("id"), "assertion id")
    kind = row.get("kind")
    if kind not in _ASSERT_KIND_TO_EVIDENCE_KIND:
        raise ValueError(
            f"assertion {assertion_id!r} has unsupported kind {kind!r}; "
            "a Failure Record only projects the deterministic assert.v1 kinds"
        )
    status = row.get("status")
    if status not in ("PASS", "FAIL", "INCONCLUSIVE", "ERROR"):
        raise ValueError(
            f"assertion {assertion_id!r} carries unsupported status "
            f"{status!r}; missing or malformed evidence can never be "
            "projected as a verdict"
        )

    lane = row.get("dimension") or _KIND_DEFAULT_DIMENSION.get(kind)
    if lane not in LANES:
        raise ValueError(
            f"assertion {assertion_id!r} (kind {kind!r}) has no projectable "
            "dimension; tag it with one of outcome/policy/conversation/"
            "speech/reliability"
        )
    if lane == "outcome" and kind in TRANSCRIPT_ONLY_KINDS:
        raise ValueError(
            f"assertion {assertion_id!r} is tagged dimension=outcome but its "
            f"kind {kind!r} reads transcript text only; transcript text can "
            "never establish an outcome claim -- use a tool_call, tool_result, "
            "state, or state_change assertion for outcome"
        )

    evidence_kind = _ASSERT_KIND_TO_EVIDENCE_KIND[kind]
    evidence_id = f"assertion-{assertion_id}-evidence"
    redaction_classes = {
        "tool_call": ["tool-arguments"],
        "tool_result": ["tool-arguments", "tool-result"],
        "state_snapshot": ["state-values"],
        "state_change": ["state-values"],
        "transcript_span": ["transcript-span"],
        "policy_match": ["transcript-span"],
    }.get(evidence_kind, [])
    evidence = {
        "evidence_id": evidence_id,
        "kind": evidence_kind,
        "digest": digest_bytes(_canonical_json_bytes(row)),
        "authority": "source",
        "media_type": "application/json",
        "redacted": bool(redaction_classes),
        "redaction_classes": redaction_classes,
    }

    evidence_refs = [evidence_id]
    artifact_for_kind = {
        "trace_span": "trace", "tool_call": "trace", "tool_result": "trace",
        "transcript_span": "transcript", "policy_match": "transcript",
        "timing_measurement": "timing",
    }.get(evidence_kind)
    if artifact_for_kind and artifact_for_kind in artifact_refs:
        evidence_refs.append(artifact_refs[artifact_for_kind])

    # SHARE-SAFE by construction: quote the row's own ``public_reason`` (built
    # from allowlisted structured fields by the assertion engine) and NEVER the
    # raw ``reason`` (which can embed a regex, a tool argument, a state value, a
    # DTMF digit, or an exception path). A legacy row without ``public_reason``
    # falls back to a conservative kind/status sentence, still never the reason.
    public_reason = row.get("public_reason")
    observed = (_bound_public(public_reason) if public_reason
                else _conservative_public_fallback(kind, status))
    assertion = {
        "assertion_id": assertion_id,
        "rule_id": _require_safe_id(f"assert.{kind}", "rule id"),
        "rule_version": "1",
        "dimension": lane,
        "status": status,
        "authority": "deterministic",
        "expected": _EXPECTED_TEMPLATES[status].format(kind=kind),
        "observed": observed,
        "evidence_refs": evidence_refs,
        # Missing-evidence is a fixed-vocabulary classification by the row's
        # KIND (never a substring of the reason), so it carries no payload.
        "missing_evidence": (_missing_evidence_for_kind(kind)
                             if status == "INCONCLUSIVE" else []),
        "source_result_digest": source_digest,
    }
    return lane, assertion, evidence


def _lane_block(assertions: List[Dict[str, Any]]) -> Dict[str, Any]:
    """One lane's block from its projected assertions: its OWN status (ERROR >
    FAIL > INCONCLUSIVE > PASS > NOT_RUN) and its OWN counts -- a grouping of
    copied statuses, never a blend and never a number across lanes."""
    passed = sum(1 for a in assertions if a["status"] == "PASS")
    failed = sum(1 for a in assertions if a["status"] == "FAIL")
    inconclusive = sum(1 for a in assertions if a["status"] == "INCONCLUSIVE")
    errored = sum(1 for a in assertions if a["status"] == "ERROR")
    if errored:
        status = "ERROR"
    elif failed:
        status = "FAIL"
    elif inconclusive:
        status = "INCONCLUSIVE"
    elif passed:
        status = "PASS"
    else:
        status = "NOT_RUN"
    return {
        "status": status,
        "evaluated": passed + failed,
        "passed": passed,
        "failed": failed,
        "inconclusive": inconclusive,
        "assertions": assertions,
    }


def _reliability_block(
    aggregate: Optional[Dict[str, Any]],
    assertions: List[Dict[str, Any]],
) -> Dict[str, Any]:
    """The reliability lane: values COPIED from the existing reliability
    aggregate (pass@1 / pass@k / pass^k / Wilson CI), displayed with their
    denominators. Nothing is recomputed here. With fewer than two trials there
    is no repetition data, so the rates and interval are honestly null. The
    lane STATUS comes only from reliability-tagged assertions (else NOT_RUN):
    data alone never becomes a verdict, because no acceptance condition was
    declared over it."""
    lane = _lane_block(assertions)
    trials = int(aggregate.get("n", 0)) if aggregate else 0
    passes = int(aggregate.get("passes", 0)) if aggregate else 0
    block: Dict[str, Any] = {
        "status": lane["status"],
        "trials": trials,
        "passes": passes,
        "pass_at_1": None,
        "pass_at_k": None,
        "pass_caret_k": None,
        "k": None,
        "wilson_interval": None,
        "assertions": assertions,
    }
    if aggregate and trials >= 2:
        block["pass_at_1"] = float(aggregate["pass_at_1"])
        block["pass_at_k"] = float(aggregate["pass_at_k"])
        block["pass_caret_k"] = float(aggregate["pass_caret_k"])
        block["k"] = int(aggregate.get("k", trials))
        block["wilson_interval"] = _wilson_from_ci(aggregate.get("ci"))
    return block


def _advisory_block(rubric_env: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    """The model-judged advisory lane, reported SEPARATELY from the gate. No
    rubric lane (or an empty one) is honestly UNAVAILABLE with the reason
    named -- and an unavailable advisory never changes the deterministic
    verdict (validate_record enforces that structurally)."""
    if not rubric_env or not rubric_env.get("results"):
        return {
            "status": "UNAVAILABLE",
            "gate_enabled": bool(rubric_env.get("gated")) if rubric_env else False,
            "reason_code": "backend-not-requested",
        }
    statuses = [r.get("status") for r in rubric_env["results"]]
    if "ERROR" in statuses:
        status = "ERROR"
    elif "FAIL" in statuses:
        status = "FAIL"
    elif any(s not in ("PASS",) for s in statuses):
        status = "INCONCLUSIVE"
    else:
        status = "PASS"
    return {"status": status, "gate_enabled": bool(rubric_env.get("gated"))}


def _rubric_env_from_suite_entry(unit: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """Rebuild the minimal advisory view :func:`_advisory_block` needs from a
    suite per-test entry: the suite envelope stores only ``rubric_summary``
    counts (never the rows), and gating comes from ``success.rubric_gated``."""
    summary = unit.get("rubric_summary")
    if not summary:
        return None
    results = (
        [{"status": "PASS"}] * int(summary.get("pass", 0))
        + [{"status": "FAIL"}] * int(summary.get("fail", 0))
        + [{"status": "INCONCLUSIVE"}] * int(summary.get("inconclusive", 0))
        + [{"status": "ERROR"}] * int(summary.get("error", 0))
    )
    gated = bool((unit.get("success") or {}).get("rubric_gated"))
    return {"results": results, "gated": gated}


# =========================================================================
# source selection
# =========================================================================

def select_source(doc: Dict[str, Any], selector: Optional[str]) -> Dict[str, Any]:
    """Resolve ``doc`` (+ optional ``selector``) to the ONE failing unit to
    project. Raises :class:`SelectorError` for zero or several matches (two
    DISTINCT messages) and :class:`NoFailureError` when every candidate
    passed. Returns a normalized intermediate dict consumed by
    :func:`project` -- selection only, no evaluation."""
    kind = doc.get("kind")
    if kind == "hotato.test-run":
        test_id = doc.get("test_id")
        if selector is not None and selector != test_id:
            raise SelectorError(
                f"selector {selector!r} matches no result: this test-run "
                f"source contains only test id {test_id!r}"
            )
        return {"source_kind": "test-run", "unit": doc}
    if kind == "hotato.suite-run":
        tests = doc.get("tests") or []
        if selector is not None:
            matches = [t for t in tests if t.get("test_id") == selector]
            if not matches:
                known = ", ".join(sorted(t.get("test_id", "?") for t in tests))
                raise SelectorError(
                    f"selector {selector!r} matches no test in this suite-run "
                    f"source; the suite contains: {known}"
                )
            if len(matches) > 1:
                raise SelectorError(
                    f"selector {selector!r} matches {len(matches)} tests in "
                    "this suite-run source; test ids must be unique to "
                    "project a record"
                )
            return {"source_kind": "suite-run", "unit": matches[0], "suite": doc}
        failing = [t for t in tests if t.get("exit_code", 0) != 0]
        if not failing:
            raise NoFailureError(_NO_FAILURE_MESSAGE)
        if len(failing) > 1:
            ids = ", ".join(sorted(t.get("test_id", "?") for t in failing))
            raise SelectorError(
                f"this suite-run source contains {len(failing)} failing "
                f"tests ({ids}); append a selector to pick one, for example "
                "suite-run.json#TEST_ID"
            )
        return {"source_kind": "suite-run", "unit": failing[0], "suite": doc}
    if kind == "contract-verify":
        results = doc.get("results") or []
        if selector is not None:
            matches = [r for r in results if r.get("id") == selector]
            if not matches:
                known = ", ".join(sorted(str(r.get("id", "?")) for r in results))
                raise SelectorError(
                    f"selector {selector!r} matches no contract in this "
                    f"contract-verify source; it contains: {known}"
                )
            if len(matches) > 1:
                raise SelectorError(
                    f"selector {selector!r} matches {len(matches)} contracts "
                    "in this contract-verify source; contract ids must be "
                    "unique to project a record"
                )
            return {"source_kind": "contract-verify", "unit": matches[0],
                    "envelope": doc}
        failing = [r for r in results if not r.get("passed", False)
                   or (r.get("assertions") or {}).get("exit_code", 0) != 0]
        if not failing:
            raise NoFailureError(_NO_FAILURE_MESSAGE)
        if len(failing) > 1:
            ids = ", ".join(sorted(str(r.get("id", "?")) for r in failing))
            raise SelectorError(
                f"this contract-verify source contains {len(failing)} failing "
                f"contracts ({ids}); append a selector to pick one, for "
                "example verify.json#CONTRACT_ID"
            )
        return {"source_kind": "contract-verify", "unit": failing[0],
                "envelope": doc}
    raise ValueError(
        f"unsupported source kind {kind!r}: a Failure Record projects a "
        "hotato.test-run result, a hotato.suite-run result, or a "
        "contract-verify envelope (each from --format json)"
    )


_NO_FAILURE_MESSAGE = (
    "source contains no failure: every deterministic check passed and no "
    "gated advisory failure is present. A Failure Record is only rendered "
    "for a FAIL, INCONCLUSIVE, or ERROR result; a passing result is never "
    "relabeled."
)


# =========================================================================
# projection
# =========================================================================

def _headline(primary: Optional[Dict[str, Any]], advisory: Dict[str, Any]) -> str:
    """The record's first visible sentence: ``{Lane} {status}: {one bounded
    observed fact}`` -- the LANE that owns the gate is capitalized (never the
    assertion id), and the observed fact is the assertion's share-safe public
    sentence. When the failure is purely advisory-gated, it says exactly that.
    Deterministic and bounded to the schema's 240-char maximum; it never
    introduces a claim absent from the source result."""
    if primary is None:
        return ("Model advisory failed with its gate enabled; every "
                "deterministic lane passed.")
    verb = {
        "FAIL": "failed",
        "INCONCLUSIVE": "inconclusive",
        "ERROR": "error",
    }[primary["status"]]
    text = f"{primary['dimension'].capitalize()} {verb}: {primary['observed']}"
    if len(text) > _SUMMARY_LIMIT:
        text = text[: _SUMMARY_LIMIT - 3] + "..."
    return text


def _primary_assertion(lanes: Dict[str, List[Dict[str, Any]]]) -> Optional[Dict[str, Any]]:
    # Severity precedence matches the deterministic gate: ERROR, then FAIL,
    # then INCONCLUSIVE (the same order _derived_gate_status applies), scanning
    # lanes in the fixed outcome/policy/conversation/speech/reliability order.
    for wanted in ("ERROR", "FAIL", "INCONCLUSIVE"):
        for lane in LANES:
            for assertion in lanes[lane]:
                if assertion["authority"] == "deterministic" \
                        and assertion["status"] == wanted:
                    return assertion
    return None


def _artifact_evidence(unit: Dict[str, Any]) -> Tuple[List[Dict[str, Any]], Dict[str, str]]:
    """Evidence entries for the conversation.v1 manifest children a test-run
    bound by digest (relative locators + the manifest's own sha256 values).
    Returns ``(entries, name -> evidence_id)``."""
    entries: List[Dict[str, Any]] = []
    refs: Dict[str, str] = {}
    manifest = unit.get("conversation") or {}
    for name, meta in sorted((manifest.get("artifacts") or {}).items()):
        if name not in _ARTIFACT_EVIDENCE:
            continue
        kind, redacted, classes = _ARTIFACT_EVIDENCE[name]
        locator = str(meta.get("path", ""))
        if not _safe_relative(locator):
            raise ValueError(
                f"the bound {name} artifact has an unsafe locator (absolute "
                "or traversing path); refusing to project it"
            )
        evidence_id = f"{name}-ref"
        entries.append({
            "evidence_id": evidence_id,
            "kind": kind,
            "digest": "sha256:" + str(meta["sha256"]),
            "authority": "source",
            "locator": locator,
            "media_type": ("audio/wav" if name == "audio"
                           else "application/json"),
            "redacted": redacted,
            "redaction_classes": list(classes),
        })
        refs[name] = evidence_id
    return entries, refs


def _project_test_run_lanes(
    unit: Dict[str, Any], *, source_digest: str,
) -> Tuple[Dict[str, List[Dict[str, Any]]], List[Dict[str, Any]], Dict[str, Any]]:
    """Project a test-run's deterministic assert.v1 rows into the five lanes
    plus the evidence catalog. Returns ``(lanes, evidence, reliability_agg)``."""
    artifact_entries, artifact_refs = _artifact_evidence(unit)
    evidence = list(artifact_entries)
    lanes: Dict[str, List[Dict[str, Any]]] = {lane: [] for lane in LANES}
    seen_ids = set()
    rows = (unit.get("assertions") or {}).get("results") or []
    for row in rows:
        lane, assertion, entry = _project_result_row(
            row, source_digest=source_digest, artifact_refs=artifact_refs,
        )
        if assertion["assertion_id"] in seen_ids:
            raise ValueError(
                f"duplicate assertion id {assertion['assertion_id']!r} in the "
                "source result; ids must be unique to project a record"
            )
        seen_ids.add(assertion["assertion_id"])
        lanes[lane].append(assertion)
        evidence.append(entry)
    reliability_agg = (unit.get("reliability") or {}).get("aggregate")
    return lanes, evidence, reliability_agg


def _project_suite_test_lanes(
    unit: Dict[str, Any], *, source_digest: str,
) -> Tuple[Dict[str, List[Dict[str, Any]]], List[Dict[str, Any]], Dict[str, Any]]:
    """Project one suite-run per-test entry. The suite envelope carries
    per-dimension COUNTS plus the first failing reason per dimension (not the
    full rows), so each lane's summary assertion states exactly those counts --
    a projection of what the suite recorded, never a re-evaluation."""
    lanes: Dict[str, List[Dict[str, Any]]] = {lane: [] for lane in LANES}
    evidence: List[Dict[str, Any]] = []
    dim_counts = unit.get("dim_counts") or {}
    # dim_public_reason is the SHARE-SAFE failure sentence the record quotes;
    # dim_failure_kind is the failing assertion's structured kind. dim_reason is
    # PRIVATE and kept ONLY as a legacy source of the kind (for a suite recorded
    # before dim_failure_kind existed) -- its text is never rendered.
    dim_public_reason = unit.get("dim_public_reason") or {}
    dim_failure_kind = unit.get("dim_failure_kind") or {}
    dim_reason = unit.get("dim_reason") or {}
    test_id = _require_safe_id(unit.get("test_id"), "subject.test_id")

    for lane in LANES:
        counts = dim_counts.get(lane) or {}
        fails = int(counts.get("fail", 0))
        inconclusive = int(counts.get("inconclusive", 0))
        public_reason = dim_public_reason.get(lane)
        # The failing assertion's kind: prefer the structured dim_failure_kind;
        # fall back to the legacy "kind: reason" prefix ONLY to recover the kind.
        row_kind = dim_failure_kind.get(lane)
        if row_kind not in _ASSERT_KIND_TO_EVIDENCE_KIND:
            row_kind = None
            legacy = dim_reason.get(lane)
            if legacy and ":" in legacy:
                candidate = legacy.split(":", 1)[0].strip()
                if candidate in _ASSERT_KIND_TO_EVIDENCE_KIND:
                    row_kind = candidate
        if lane == "outcome" and row_kind in TRANSCRIPT_ONLY_KINDS:
            raise ValueError(
                f"the outcome dimension of test {test_id!r} failed on a "
                f"{row_kind} assertion, which reads transcript text only; "
                "transcript text can never establish an outcome claim -- "
                "use a tool_call, tool_result, state, or state_change "
                "assertion for outcome"
            )
        # Digest-pinning payload: the share-safe structured facts only (never
        # the private reason text). A hash of it identifies the recorded lane
        # outcome without embedding any payload value.
        entry_payload = {"test_id": test_id, "dimension": lane,
                         "counts": counts, "kind": row_kind}
        evidence_kind = (_ASSERT_KIND_TO_EVIDENCE_KIND.get(row_kind)
                         if row_kind else None)
        if lane == "outcome" and fails \
                and evidence_kind not in OUTCOME_EVIDENCE_KINDS:
            raise ValueError(
                f"the outcome dimension of test {test_id!r} failed but the "
                "suite envelope does not identify a tool-call or state "
                "assertion kind behind it; render the record from the "
                "test-run result (hotato test run FILE --format json) so the "
                "outcome claim can cite its tool/state evidence"
            )
        for status, count in (("FAIL", fails), ("INCONCLUSIVE", inconclusive)):
            if not count:
                continue
            evidence_id = f"{lane}-{status.lower()}-evidence"
            evidence.append({
                "evidence_id": evidence_id,
                "kind": evidence_kind or "configuration",
                "digest": digest_bytes(_canonical_json_bytes(entry_payload)),
                "authority": "source",
                "media_type": "application/json",
                "redacted": False,
                "redaction_classes": [],
            })
            if status == "FAIL":
                # Quote the suite's SHARE-SAFE public sentence; a legacy suite
                # without one names the digest-pinned private source rather than
                # ever rendering the private reason text.
                observed = (_bound_public(public_reason) if public_reason else
                            f"{count} {lane} assertion(s) failed; inspect the "
                            "digest-pinned private source for details.")
            else:
                observed = (
                    f"{count} assertions in the {lane} dimension were "
                    "inconclusive: required input was absent."
                )
            lanes[lane].append({
                "assertion_id": f"{lane}-{status.lower()}",
                "rule_id": f"suite.dimension.{lane}",
                "rule_version": "1",
                "dimension": lane,
                "status": status,
                "authority": "deterministic",
                "expected": (f"Every deterministic assertion tagged into the "
                             f"{lane} dimension passes."),
                "observed": observed,
                "evidence_refs": [evidence_id],
                "missing_evidence": (["required-input"]
                                     if status == "INCONCLUSIVE" else []),
                "source_result_digest": source_digest,
            })

    invalid = unit.get("simulator_invalid") or []
    counts = unit.get("counts") or {}
    if invalid and not int(counts.get("valid", 0)):
        payload = {"test_id": test_id, "simulator_invalid": len(invalid)}
        evidence.append({
            "evidence_id": "simulator-validity-evidence",
            "kind": "configuration",
            "digest": digest_bytes(_canonical_json_bytes(payload)),
            "authority": "source",
            "media_type": "application/json",
            "redacted": False,
            "redaction_classes": [],
        })
        lanes["conversation"].append({
            "assertion_id": "simulator-validity",
            "rule_id": "suite.simulator-validity",
            "rule_version": "1",
            "dimension": "conversation",
            "status": "ERROR",
            "authority": "deterministic",
            "expected": "At least one simulated run renders as a valid conversation.",
            "observed": (f"{len(invalid)} simulated runs were "
                         "SIMULATOR_INVALID (broken fixtures) and no valid "
                         "run produced agent evidence."),
            "evidence_refs": ["simulator-validity-evidence"],
            "missing_evidence": ["valid-simulated-run"],
            "source_result_digest": source_digest,
        })
    return lanes, evidence, unit.get("reliability") or {}


def _contract_fail_observed(expect: Any, measurement: Dict[str, Any]) -> str:
    """A numeric, share-safe observed sentence for a FAILED contract, derived
    from the ``expect`` label and the ``measurement`` block that
    ``contract verify`` already produced (``did_yield`` / ``seconds_to_yield``
    / ``talk_over_sec``). Only numbers and a fixed vocabulary are emitted --
    never a transcript, a digit sequence, or a payload value -- and a missing
    numeric field is simply omitted (never printed as ``None``)."""
    exp = str(expect).strip().lower() if isinstance(expect, str) else None
    did_yield = measurement.get("did_yield")
    to = (_fmt_public_number(measurement.get("talk_over_sec"))
          if _is_number(measurement.get("talk_over_sec")) else None)
    sty = (_fmt_public_number(measurement.get("seconds_to_yield"))
           if _is_number(measurement.get("seconds_to_yield")) else None)

    if exp == "yield" and did_yield is False:
        return (f"Agent did not yield; measured talk-over was {to} s."
                if to is not None else "Agent did not yield.")
    if exp == "yield" and did_yield is True:
        if sty is not None and to is not None:
            return (f"Agent yielded after {sty} s with {to} s of talk-over; "
                    "the timing policy failed.")
        if to is not None:
            return f"Agent yielded with {to} s of talk-over; the timing policy failed."
        if sty is not None:
            return f"Agent yielded after {sty} s; the timing policy failed."
        return "Agent yielded but the timing policy failed."
    if exp == "hold" and did_yield is True:
        return (f"Agent yielded after {sty} s although the contract required it "
                "to hold." if sty is not None else
                "Agent yielded although the contract required it to hold.")
    return ("The contract's re-scored timing no longer meets its policy "
            "pass_conditions.")


def _project_contract_lanes(
    unit: Dict[str, Any], *, source_digest: str,
) -> Tuple[Dict[str, List[Dict[str, Any]]], List[Dict[str, Any]]]:
    """Project one contract-verify result row: the timing re-score lands in
    the conversation lane; an embedded assert.v1 envelope's rows project
    exactly like a test-run's."""
    lanes: Dict[str, List[Dict[str, Any]]] = {lane: [] for lane in LANES}
    evidence: List[Dict[str, Any]] = []
    contract_id = _require_safe_id(unit.get("id"), "contract id")

    measurement = unit.get("measurement") or {}
    evidence.append({
        "evidence_id": "timing-measurement-ref",
        "kind": "timing_measurement",
        "digest": digest_bytes(_canonical_json_bytes(measurement)),
        "authority": "machine",
        "media_type": "application/json",
        "redacted": False,
        "redaction_classes": [],
    })
    passed = bool(unit.get("passed", False))
    if passed:
        status, observed = "PASS", (
            "The contract's re-scored timing meets its policy pass_conditions."
        )
    elif unit.get("not_scorable_reason") or unit.get("verdict_eligible") is False:
        status = "INCONCLUSIVE"
        observed = _scrub_summary(
            unit.get("not_scorable_reason")
            or unit.get("verdict_ineligible_reason")
            or "the recording is no longer scorable"
        )
    else:
        status = "FAIL"
        observed = _contract_fail_observed(unit.get("expect"), measurement)
    lanes["conversation"].append({
        "assertion_id": f"{contract_id}-timing",
        "rule_id": "contract.timing-policy",
        "rule_version": "1",
        "dimension": "conversation",
        "status": status,
        "authority": "deterministic",
        "expected": "The re-scored timing meets the contract's policy pass_conditions.",
        "observed": observed,
        "evidence_refs": ["timing-measurement-ref"],
        "missing_evidence": (["scorable-recording"]
                             if status == "INCONCLUSIVE" else []),
        "source_result_digest": source_digest,
    })

    env = unit.get("assertions")
    if env is not None:
        for row in env.get("results") or []:
            lane, assertion, entry = _project_result_row(
                row, source_digest=source_digest, artifact_refs={},
            )
            lanes[lane].append(assertion)
            evidence.append(entry)
    return lanes, evidence


def project(
    doc: Dict[str, Any],
    *,
    selector: Optional[str] = None,
    source_path: Optional[str] = None,
    out_ref: str = "record",
    related: Optional[List[Dict[str, str]]] = None,
) -> Dict[str, Any]:
    """Project one failing result out of ``doc`` into the canonical
    ``hotato.failure-record.v1`` dict (validated before it is returned).

    ``selector`` picks the test/contract inside a multi-result source (the
    CLI's ``SOURCE#SELECTOR``). ``source_path`` pins the source artifact by
    file digest and names it (relative basename only) in the reproduction
    block; without it the canonical-JSON digest of ``doc`` pins the content.
    ``related`` is the optional before/after relationship: a list of
    ``{"name": "before"|"after", "digest": "sha256:..."}`` entries.

    Raises :class:`NoFailureError` on an all-pass source, :class:`SelectorError`
    on zero/several selector matches, and ``ValueError`` on an unprojectable
    field -- never a silent downgrade or a fabricated verdict."""
    selected = select_source(doc, selector)
    unit = selected["unit"]

    if source_path is not None:
        from .errors import open_regular
        with open_regular(source_path, "rb") as fh:
            source_digest = digest_bytes(fh.read())
        base = os.path.basename(source_path)
        source_name = base if _safe_relative(base) and base else "source-result.json"
    else:
        source_digest = digest_bytes(_canonical_json_bytes(doc))
        source_name = "source-result.json"

    reliability_agg: Optional[Dict[str, Any]] = None
    if selected["source_kind"] == "test-run":
        lanes, evidence, reliability_agg = _project_test_run_lanes(
            unit, source_digest=source_digest)
        rubric_env = unit.get("rubric")
        subject = {"test_id": _require_safe_id(unit.get("test_id"),
                                               "subject.test_id")}
        if unit.get("agent"):
            subject["agent_id"] = _require_safe_id(unit["agent"],
                                                   "subject.agent_id")
        origin_kind = ("simulated"
                       if (unit.get("reliability") or {}).get("origin") == "simulated"
                       else "captured")
        required = list((unit.get("success") or {}).get("required") or [])
        exit_code = int(unit.get("exit_code", 0))
        source_schema = "hotato.test-run"
    elif selected["source_kind"] == "suite-run":
        lanes, evidence, reliability_agg = _project_suite_test_lanes(
            unit, source_digest=source_digest)
        rubric_env = _rubric_env_from_suite_entry(unit)
        suite = selected["suite"]
        subject = {"test_id": _require_safe_id(unit.get("test_id"),
                                               "subject.test_id")}
        if unit.get("scenario_id"):
            subject["scenario_id"] = _require_safe_id(unit["scenario_id"],
                                                      "subject.scenario_id")
        if suite.get("suite_id"):
            subject["suite_id"] = _require_safe_id(suite["suite_id"],
                                                   "subject.suite_id")
        if unit.get("agent"):
            subject["agent_id"] = _require_safe_id(unit["agent"],
                                                   "subject.agent_id")
        if suite.get("release_id"):
            subject["release_id"] = _require_safe_id(suite["release_id"],
                                                     "subject.release_id")
        origin_kind = ("simulated" if unit.get("origin") == "simulated"
                       else "captured")
        required = list((unit.get("success") or {}).get("required") or [])
        exit_code = int(unit.get("exit_code", 0))
        source_schema = "hotato.suite-run"
    else:
        lanes, evidence = _project_contract_lanes(
            unit, source_digest=source_digest)
        rubric_env = None
        subject = {"test_id": _require_safe_id(unit.get("id"),
                                               "subject.test_id")}
        origin_kind = "captured"
        required = []
        exit_code = int(doc.get("exit_code", 0))
        source_schema = "contract-verify"

    advisory = _advisory_block(rubric_env)
    reliability_block = _reliability_block(reliability_agg,
                                           lanes.pop("reliability"))

    dimensions: Dict[str, Any] = {
        lane: _lane_block(lanes[lane]) for lane in LANES if lane != "reliability"
    }
    dimensions["reliability"] = reliability_block

    probe = {"dimensions": dimensions}
    gate_status = _derived_gate_status(probe)

    status = gate_status
    if status == "PASS":
        if advisory["gate_enabled"] and advisory["status"] in ("FAIL", "ERROR"):
            status = "FAIL" if advisory["status"] == "FAIL" else "ERROR"
        else:
            raise NoFailureError(_NO_FAILURE_MESSAGE)

    policy_tokens = [c.replace(" ", "-") for c in required] or \
        ["all-deterministic-assertions-pass"]
    gate_policy = _require_safe_id("+".join(policy_tokens), "gate.policy")

    all_lanes = {lane: dimensions[lane]["assertions"] for lane in LANES}
    primary = _primary_assertion(all_lanes)
    headline = _headline(primary, advisory)

    source_ref = source_name if selector is None else f"{source_name}#{selector}"
    reproduction = {
        "argv": ["hotato", "record", "render", source_ref, "--out", out_ref],
        "working_directory": ".",
        "required_artifacts": [
            {"role": "source-result", "digest": source_digest,
             "relative_path": source_name},
        ],
    }

    rule_components: List[Dict[str, str]] = []
    seen_rules = set()
    for lane in LANES:
        for assertion in dimensions[lane]["assertions"]:
            key = (assertion["rule_id"], assertion.get("rule_version", "1"))
            if key not in seen_rules:
                seen_rules.add(key)
                rule_components.append({"name": key[0], "version": key[1]})

    redaction_classes = sorted({
        "absolute-paths", "credentials", "state-values", "tool-payload",
        "transcript-body",
    })
    provenance: Dict[str, Any] = {
        "hotato": {"name": "hotato", "version": __version__},
        "schemas": [
            {"name": "failure-record", "version": VERSION},
            {"name": source_schema, "version": str(doc.get("version",
                                                           doc.get("schema_version", "1")))},
        ],
        "scorers": [
            {"name": "hotato-failure-record-projection", "version": "1"},
        ],
        "rules": rule_components,
        "inputs": [{"name": "source-result", "digest": source_digest}],
        "source_result_digest": source_digest,
    }
    if related:
        for item in related:
            if item.get("name") not in ("before", "after"):
                raise ValueError(
                    "related record names must be 'before' or 'after'"
                )
        provenance["related"] = [
            {"name": item["name"], "digest": item["digest"]} for item in related
        ]

    record: Dict[str, Any] = {
        "kind": KIND,
        "version": VERSION,
        "status": status,
        "headline": headline,
        "subject": subject,
        "origin": {"kind": origin_kind, "source": source_schema},
        "gate": {"status": gate_status, "policy": gate_policy,
                 "exit_code": max(0, min(255, exit_code))},
        "advisory": advisory,
        "dimensions": dimensions,
        "evidence": evidence,
        "reproduction": reproduction,
        "privacy": {
            "profile": "share-safe-v1",
            "raw_audio_embedded": False,
            "transcript_body_embedded": False,
            "tool_payload_embedded": False,
            "state_value_embedded": False,
            "credential_embedded": False,
            "absolute_path_embedded": False,
            "redaction_classes": redaction_classes,
        },
        "provenance": provenance,
    }
    record["record_id"] = compute_record_id(record)
    validate_record(record)
    return record


# =========================================================================
# validation: the ported reference-kit oracle (plus the schema file for
# external validators). Raises ValueError with the kit's own messages, so the
# CLI's HANDLED boundary reports a clean exit-2 refusal.
# =========================================================================

def _walk(value: Any, path: str = "$"):
    yield path, value
    if isinstance(value, dict):
        for key, item in value.items():
            yield from _walk(item, f"{path}/{key}")
    elif isinstance(value, list):
        for index, item in enumerate(value):
            yield from _walk(item, f"{path}/{index}")


def _looks_absolute(text: str) -> bool:
    return text.startswith(("/", "\\")) or bool(re.match(r"^[A-Za-z]:[\\/]", text))


def _has_absolute_path(text: str) -> bool:
    """True if *text* IS, or CONTAINS anywhere, an absolute filesystem path: a
    POSIX root, a ``~`` home path, a Windows drive path (``C:\\``), or a UNC
    path (``\\\\server``) -- start-anchored OR embedded mid-sentence, with
    either separator. The share-safe profile forbids all of them, so the
    privacy scan refuses a record that smuggles one into any string field."""
    return _looks_absolute(text) or bool(_ABS_PATH_RE.search(text))


def validate_record(record: Dict[str, Any], *, root: Optional[str] = None) -> List[str]:
    """Verify one Failure Record against the reference conformance oracle:
    the closed top-level contract, content address, five separate lanes with
    no aggregate score, the deterministic-gate authority boundary, evidence
    reference integrity (+ file digests under ``root`` when the files are
    present), the safe reproduction contract, the share-safe privacy profile,
    reliability semantics, and the outcome-evidence authority wall. Returns
    the list of check names; raises ``ValueError`` naming the first violated
    invariant. Validation only -- it never mutates or re-scores anything."""
    checks: List[str] = []
    # The aggregate-score wall is checked FIRST so a smuggled overall_score is
    # always named as the aggregate violation (the reference kit's refusal
    # reason for that mutation class), not as a generic key-set difference.
    for path, value in _walk(record):
        if path.endswith("/overall_score") or path.endswith("/aggregate_score"):
            raise ValueError("aggregate score is forbidden")
        if isinstance(value, float) and (
                value != value or value in (float("inf"), float("-inf"))):
            raise ValueError(f"non-finite number at {path}")
    checks.append("no aggregate or non-finite score")

    if set(record) != TOP_LEVEL_KEYS:
        extra = sorted(set(record) - TOP_LEVEL_KEYS)
        missing = sorted(TOP_LEVEL_KEYS - set(record))
        raise ValueError(
            f"top-level keys differ; extra={extra}, missing={missing}")
    checks.append("top-level contract")

    if record["kind"] != KIND or record["version"] != VERSION:
        raise ValueError("kind or version mismatch")
    if record["status"] not in ("FAIL", "INCONCLUSIVE", "ERROR"):
        raise ValueError(
            "Failure Record status must be failed, inconclusive, or error")
    checks.append("kind, version, status")

    if set(record["dimensions"]) != set(LANES):
        raise ValueError("five dimensions are required")
    checks.append("five separate dimensions")

    if record["record_id"] != compute_record_id(record):
        raise ValueError("record_id content digest mismatch")
    checks.append("content address")

    if _derived_gate_status(record) != record["gate"]["status"]:
        raise ValueError(
            "deterministic gate does not match deterministic assertions")
    if record["advisory"]["gate_enabled"] is False \
            and record["status"] != record["gate"]["status"]:
        raise ValueError(
            "advisory-disabled record changed deterministic status")
    checks.append("authority boundary")

    evidence = record["evidence"]
    evidence_ids = [item["evidence_id"] for item in evidence]
    if len(evidence_ids) != len(set(evidence_ids)):
        raise ValueError("duplicate evidence_id")
    known = set(evidence_ids)
    kind_by_id = {item["evidence_id"]: item["kind"] for item in evidence}
    for lane in LANES:
        for assertion in record["dimensions"][lane]["assertions"]:
            if assertion["dimension"] != lane:
                raise ValueError(
                    "assertion dimension does not match containing lane")
            dangling = set(assertion["evidence_refs"]) - known
            if dangling:
                raise ValueError(
                    f"dangling evidence reference: {sorted(dangling)}")
    checks.append("evidence references")

    for assertion in record["dimensions"]["outcome"]["assertions"]:
        if assertion["authority"] != "deterministic" \
                or assertion["status"] not in ("PASS", "FAIL"):
            continue
        cited = {kind_by_id[ref] for ref in assertion["evidence_refs"]}
        if not (cited & OUTCOME_EVIDENCE_KINDS):
            raise ValueError(
                "outcome claim lacks tool-call or state evidence; transcript "
                "text can never establish an outcome")
    checks.append("outcome evidence authority")

    for item in evidence:
        locator = item.get("locator")
        if locator:
            if not _safe_relative(locator):
                raise ValueError("unsafe evidence locator")
            if root is not None:
                source = os.path.join(root, locator.split("#", 1)[0])
                if not os.path.isfile(source):
                    raise ValueError(f"evidence file missing: {locator}")
                from .errors import open_regular
                with open_regular(source, "rb") as fh:
                    if digest_bytes(fh.read()) != item["digest"]:
                        raise ValueError(f"evidence digest mismatch: {locator}")
    checks.append("evidence files and digests")

    reproduction = record["reproduction"]
    if not reproduction["argv"] or not all(
            isinstance(item, str) and item for item in reproduction["argv"]):
        raise ValueError("reproduction argv must contain nonempty tokens")
    for token in reproduction["argv"]:
        if _looks_absolute(token):
            raise ValueError("reproduction argv contains an absolute path")
    if not _safe_relative(reproduction.get("working_directory", ".")):
        raise ValueError("unsafe reproduction working directory")
    for artifact in reproduction["required_artifacts"]:
        rel = artifact.get("relative_path")
        if rel:
            if not _safe_relative(rel):
                raise ValueError("unsafe required-artifact path")
            if root is not None:
                source = os.path.join(root, rel)
                if os.path.isfile(source):
                    from .errors import open_regular
                    with open_regular(source, "rb") as fh:
                        if digest_bytes(fh.read()) != artifact["digest"]:
                            raise ValueError(
                                f"required-artifact digest mismatch: {rel}")
    checks.append("reproduction contract")

    privacy = record["privacy"]
    if privacy.get("profile") != "share-safe-v1":
        raise ValueError("unexpected privacy profile")
    for field in PRIVACY_FALSE_FIELDS:
        if privacy.get(field) is not False:
            raise ValueError(f"share-safe privacy field must be false: {field}")
    for path, value in _walk(record):
        if isinstance(value, str) and _has_absolute_path(value):
            raise ValueError(f"absolute path embedded at {path}")
    checks.append("share-safe privacy profile")

    reliability = record["dimensions"]["reliability"]
    trials = reliability["trials"]
    passes = reliability["passes"]
    if passes > trials:
        raise ValueError("reliability passes exceed trials")
    if reliability["pass_at_1"] is None:
        if trials >= 2:
            raise ValueError(
                "reliability rates are null despite repetition data")
        for field in ("pass_at_k", "pass_caret_k", "wilson_interval"):
            if reliability[field] is not None:
                raise ValueError(
                    "partial reliability block: rates and interval must be "
                    "all present or all null")
    else:
        expected_rate = passes / trials if trials else 0.0
        if abs(reliability["pass_at_1"] - expected_rate) > 1e-12:
            raise ValueError("pass@1 does not equal passes/trials")
        if reliability["pass_at_k"] != (1.0 if passes >= 1 else 0.0):
            raise ValueError("pass@k semantics mismatch")
        expected_all = 1.0 if trials and passes == trials else 0.0
        if reliability["pass_caret_k"] != expected_all:
            raise ValueError("pass^k semantics mismatch")
        interval = reliability["wilson_interval"]
        if interval is not None and not (
                0 <= interval["lower"] <= expected_rate
                <= interval["upper"] <= 1):
            raise ValueError("Wilson interval bounds are inconsistent")
    checks.append("reliability semantics")
    return checks
