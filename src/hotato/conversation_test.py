"""``hotato.conversation-test.v1``: parse + validate the PRIMARY user unit.

A conversation-test file (schema ``schema/conversation-test.v1.json``) defines
ONE testable conversation: the agent under test, the simulated caller, the test
environment, the two SEPARATE assertion lanes, and an explicit success
condition. This module is the honesty wall made structural for that file:

* Success is a BOOLEAN over a small CLOSED vocabulary of named conditions
  (:data:`SUCCESS_CONDITIONS`) -- never a weighting, never a merged
  ``overall_score``. The scorecard the report renders groups results by the
  ``dimension`` TAG on each assertion (:data:`REPORT_DIMENSIONS`); it never
  blends them into one number. ``validate_conversation_test_doc`` rejects any
  ``overall_score`` key structurally, matching the ``assert.v1`` guard.
* Deterministic and model-judged assertions live in two named lanes
  (``assertions.deterministic`` vs ``assertions.rubric``); this module keeps
  them separate at the envelope level. It validates only the TEST-FILE shape --
  each assertion's inner kind-specific fields are validated at run time
  (:mod:`hotato.assert_` today; the expanded kinds in a later slice), not here.
* Malformed input raises ``ValueError`` immediately -- validation runs before
  any use, so a bad file never produces a partial result -- exactly the
  contract :func:`hotato.assert_.validate_assertions_doc` sets (the caller's
  usage-error / exit-2 path, see :mod:`hotato.errors`).

Also validates the two organizing schemas from the Phase-1 design (§E):
``suite.v1`` (a named set of conversation-test refs) and ``release.v1`` (a
content-addressed snapshot of what was tested). Both are additive; neither
carries an ``overall_score``.
"""

from __future__ import annotations

from typing import Any, Dict

from .assert_ import (
    DEFAULT_INCONCLUSIVE_POLICY,
    INCONCLUSIVE_POLICIES,
    parse_assertions_yaml,
)
from .errors import open_regular as _open_regular

__all__ = [
    "KIND",
    "VERSION",
    "SUCCESS_CONDITIONS",
    "REPORT_DIMENSIONS",
    "INCONCLUSIVE_POLICIES",
    "DEFAULT_INCONCLUSIVE_POLICY",
    "validate_conversation_test_doc",
    "parse_conversation_test",
    "load_conversation_test_file",
    "SUITE_KIND",
    "SUITE_VERSION",
    "validate_suite",
    "RELEASE_KIND",
    "RELEASE_VERSION",
    "validate_release",
]

KIND = "hotato.conversation-test"
VERSION = 1

# The CLOSED vocabulary of `success.required` conditions. Success is the
# conjunction of the named conditions listed -- a boolean, never a score. A
# condition outside this set is a usage error (ValueError, exit 2), NOT a
# free-form code hook: the vocabulary is deliberately small and enumerable so a
# test file can never smuggle a bespoke scorer past the honesty wall.
SUCCESS_CONDITIONS = (
    "all_deterministic_assertions_pass",  # every deterministic result is PASS
    "no_deterministic_fail",              # no deterministic result is FAIL (INCONCLUSIVE allowed)
    "no_rubric_failure",                  # no model-judged rubric result is a failure
    "no_inconclusive",                    # no result is INCONCLUSIVE (all inputs present)
)

# The five report DIMENSIONS. A `dimension` tag on an assertion groups its
# result into one of these for the per-dimension scorecard -- a grouping key,
# never a weight and never a blended number.
REPORT_DIMENSIONS = ("outcome", "policy", "conversation", "speech", "reliability")


def _reject_overall_score(obj: Any, where: str) -> None:
    """Reject an ``overall_score`` key wherever the honesty invariant forbids
    one (top level and inside ``success``). The schema forbids it structurally
    too; this is the same guard on the code path, so a hand-built dict can
    never slip a blended score past :func:`validate_conversation_test_doc`."""
    if isinstance(obj, dict) and "overall_score" in obj:
        raise ValueError(
            f"{where}: 'overall_score' is forbidden -- success is a boolean "
            "over named conditions, never a blended score"
        )


def _validate_assertion_lane(lane_name: str, items: Any, seen_ids: set) -> None:
    """Validate one assertion lane (``deterministic`` or ``rubric``) at the
    TEST-FILE envelope level: it must be a list of mappings, each with a string
    ``id`` (unique across both lanes) and a string ``kind``, with an optional
    ``dimension`` from :data:`REPORT_DIMENSIONS`. Inner kind-specific fields are
    NOT validated here -- that happens at run time (assert_ / a later slice's
    expanded kinds), so a test file may legitimately reference a kind this build
    cannot yet evaluate."""
    if items is None:
        return
    if not isinstance(items, list):
        raise ValueError(f"assertions.{lane_name} must be a list")
    for idx, item in enumerate(items):
        if not isinstance(item, dict):
            raise ValueError(f"assertions.{lane_name}[{idx}] must be a mapping")
        aid = item.get("id")
        if not aid or not isinstance(aid, str):
            raise ValueError(
                f"assertions.{lane_name}[{idx}] is missing a string 'id'"
            )
        if aid in seen_ids:
            raise ValueError(f"duplicate assertion id {aid!r}")
        seen_ids.add(aid)
        kind = item.get("kind")
        if not kind or not isinstance(kind, str):
            raise ValueError(
                f"assertion {aid!r} (assertions.{lane_name}) is missing a "
                "string 'kind'"
            )
        dim = item.get("dimension")
        if dim is not None and dim not in REPORT_DIMENSIONS:
            raise ValueError(
                f"assertion {aid!r}: 'dimension' must be one of "
                f"{REPORT_DIMENSIONS}, got {dim!r}"
            )


def _validate_success(success: Any) -> None:
    """Validate the ``success`` block: ``required`` is a list drawn from the
    CLOSED :data:`SUCCESS_CONDITIONS` vocabulary; ``report_dimensions`` is a
    list drawn from :data:`REPORT_DIMENSIONS`; and no ``overall_score`` may
    appear. A condition or dimension outside its closed set is a ValueError."""
    if not isinstance(success, dict):
        raise ValueError("'success' must be a mapping")
    _reject_overall_score(success, "success")
    required = success.get("required")
    if required is not None:
        if not isinstance(required, list):
            raise ValueError("success.required must be a list")
        for cond in required:
            if cond not in SUCCESS_CONDITIONS:
                raise ValueError(
                    f"success.required has unknown condition {cond!r}; the "
                    f"closed vocabulary is {SUCCESS_CONDITIONS}"
                )
    dims = success.get("report_dimensions")
    if dims is not None:
        if not isinstance(dims, list):
            raise ValueError("success.report_dimensions must be a list")
        for d in dims:
            if d not in REPORT_DIMENSIONS:
                raise ValueError(
                    f"success.report_dimensions has unknown dimension {d!r}; "
                    f"allowed: {REPORT_DIMENSIONS}"
                )


def validate_conversation_test_doc(doc: Any) -> Dict[str, Any]:
    """Validate a parsed conversation-test document and return a NORMALIZED
    copy with defaults applied (``repetitions`` -> 1, ``inconclusive_policy``
    -> ``"report"``, an absent ``success`` -> the safe default
    ``all_deterministic_assertions_pass``). Raises ``ValueError`` on anything
    malformed: not a mapping; a wrong ``kind``/``version`` const; a missing
    ``id``/``agent``/``assertions``; a bad ``inconclusive_policy``; a
    ``repetitions`` below 1; a ``success.required`` token outside the closed
    vocabulary; a bad ``dimension`` tag; or a forbidden ``overall_score``.

    Nothing here evaluates an assertion -- this is pure structural validation
    of the test-file envelope, run before any use, mirroring
    :func:`hotato.assert_.validate_assertions_doc`."""
    if not isinstance(doc, dict):
        raise ValueError(
            "conversation-test document must be a mapping with 'kind', "
            "'version', 'id', 'agent', and 'assertions'"
        )
    _reject_overall_score(doc, "conversation-test document")

    if doc.get("kind") != KIND:
        raise ValueError(
            f"'kind' must be {KIND!r}, got {doc.get('kind')!r}"
        )
    version = doc.get("version")
    if version != VERSION:
        raise ValueError(
            f"unsupported conversation-test version {version!r}; this build "
            f"supports version {VERSION}"
        )

    for field in ("id", "agent"):
        val = doc.get(field)
        if not val or not isinstance(val, str):
            raise ValueError(f"conversation-test is missing a string {field!r}")

    if "scenario" in doc and not isinstance(doc["scenario"], str):
        raise ValueError("'scenario' must be a string (a path/ref)")

    assertions = doc.get("assertions")
    if not isinstance(assertions, dict):
        raise ValueError("'assertions' is required and must be a mapping")
    if "deterministic" not in assertions:
        raise ValueError("assertions.deterministic is required (may be an empty list)")
    seen_ids: set = set()
    _validate_assertion_lane("deterministic", assertions.get("deterministic"), seen_ids)
    _validate_assertion_lane("rubric", assertions.get("rubric"), seen_ids)

    repetitions = doc.get("repetitions", 1)
    if isinstance(repetitions, bool) or not isinstance(repetitions, int):
        raise ValueError(f"'repetitions' must be an integer, got {repetitions!r}")
    if repetitions < 1:
        raise ValueError(f"'repetitions' must be >= 1, got {repetitions}")

    policy = doc.get("inconclusive_policy", DEFAULT_INCONCLUSIVE_POLICY)
    if policy not in INCONCLUSIVE_POLICIES:
        raise ValueError(
            f"'inconclusive_policy' must be one of {INCONCLUSIVE_POLICIES}, "
            f"got {policy!r}"
        )

    if "success" in doc:
        _validate_success(doc["success"])

    # Normalized copy with defaults applied (the raw doc is never mutated).
    norm = dict(doc)
    norm["repetitions"] = repetitions
    norm["inconclusive_policy"] = policy
    if "success" not in norm:
        norm["success"] = {
            "required": ["all_deterministic_assertions_pass"],
            "report_dimensions": [],
        }
    return norm


def parse_conversation_test(text: str) -> Any:
    """Parse a conversation-test document from text. Reuses the dependency-free
    YAML-subset / JSON parser :func:`hotato.assert_.parse_assertions_yaml`, so
    conversation-test files stay zero-install (JSON or the same small YAML
    subset assertion files use). Raises ``ValueError`` on a malformed document.
    This only parses; call :func:`validate_conversation_test_doc` to validate."""
    return parse_assertions_yaml(text)


def load_conversation_test_file(path: str) -> Dict[str, Any]:
    """Load, parse, and validate a conversation-test file, returning the
    normalized doc. A FIFO/named-pipe path raises immediately (via
    :func:`hotato.errors.open_regular`) instead of blocking forever; a
    malformed document raises ``ValueError`` (the caller's exit-2 path)."""
    with _open_regular(path, "r", encoding="utf-8") as fh:
        text = fh.read()
    return validate_conversation_test_doc(parse_conversation_test(text))


# =========================================================================
# suite.v1 + release.v1 -- the organizing schemas (Phase-1 design E)
# =========================================================================

SUITE_KIND = "hotato.suite"
SUITE_VERSION = 1
RELEASE_KIND = "hotato.release"
RELEASE_VERSION = 1


def validate_suite(doc: Any) -> Dict[str, Any]:
    """Validate a ``suite.v1`` document (schema ``schema/suite.v1.json``) and
    return a NORMALIZED copy (``required_for_release`` -> ``False``,
    ``inconclusive_policy`` -> ``"report"`` when absent). A suite is a named set
    of conversation-test refs; it groups tests, it does not blend them (no
    ``overall_score``). Raises ``ValueError`` on anything malformed."""
    if not isinstance(doc, dict):
        raise ValueError("suite document must be a mapping")
    _reject_overall_score(doc, "suite document")
    if doc.get("kind") != SUITE_KIND:
        raise ValueError(f"'kind' must be {SUITE_KIND!r}, got {doc.get('kind')!r}")
    if doc.get("version") != SUITE_VERSION:
        raise ValueError(
            f"unsupported suite version {doc.get('version')!r}; this build "
            f"supports version {SUITE_VERSION}"
        )
    for field in ("suite_id", "name"):
        if not doc.get(field) or not isinstance(doc[field], str):
            raise ValueError(f"suite is missing a string {field!r}")
    tests = doc.get("tests")
    if not isinstance(tests, list) or not all(isinstance(t, str) for t in tests):
        raise ValueError("suite.tests must be a list of conversation-test refs (strings)")
    rfr = doc.get("required_for_release", False)
    if not isinstance(rfr, bool):
        raise ValueError("suite.required_for_release must be a boolean")
    policy = doc.get("inconclusive_policy", DEFAULT_INCONCLUSIVE_POLICY)
    if policy not in INCONCLUSIVE_POLICIES:
        raise ValueError(
            f"suite.inconclusive_policy must be one of {INCONCLUSIVE_POLICIES}, "
            f"got {policy!r}"
        )
    norm = dict(doc)
    norm["required_for_release"] = rfr
    norm["inconclusive_policy"] = policy
    return norm


# Digest fields carried by a release snapshot. Optional (a release may pin only
# some of them), but when present each must be a string (a content address).
_RELEASE_DIGEST_FIELDS = (
    "prompt_digest",
    "tool_schema_digest",
    "workflow_digest",
    "provider_config_digest",
)


def validate_release(doc: Any) -> Dict[str, Any]:
    """Validate a ``release.v1`` document (schema ``schema/release.v1.json``):
    a content-addressed SNAPSHOT of exactly what was tested, so ``release
    compare`` is digest-exact. Requires ``release_id``, ``agent_id``, and a
    caller-supplied ``created_at`` (never Date.now()); each digest field, when
    present, must be a string. No ``overall_score``. Raises ``ValueError`` on
    anything malformed; returns the doc unchanged on success."""
    if not isinstance(doc, dict):
        raise ValueError("release document must be a mapping")
    _reject_overall_score(doc, "release document")
    if doc.get("kind") != RELEASE_KIND:
        raise ValueError(f"'kind' must be {RELEASE_KIND!r}, got {doc.get('kind')!r}")
    if doc.get("version") != RELEASE_VERSION:
        raise ValueError(
            f"unsupported release version {doc.get('version')!r}; this build "
            f"supports version {RELEASE_VERSION}"
        )
    for field in ("release_id", "agent_id", "created_at"):
        if not doc.get(field) or not isinstance(doc[field], str):
            raise ValueError(f"release is missing a string {field!r}")
    for field in _RELEASE_DIGEST_FIELDS:
        if field in doc and doc[field] is not None and not isinstance(doc[field], str):
            raise ValueError(f"release.{field} must be a string (a content address)")
    return dict(doc)
