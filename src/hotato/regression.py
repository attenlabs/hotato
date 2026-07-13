"""``hotato regression prepare``: project ONE confirmed failure into a
sanitized, deterministic, committed regression candidate bundle, LOCALLY.

This command turns a confirmed failure (a ``hotato test run`` / ``suite run`` /
``contract verify`` result, or an already-projected ``hotato.failure-record.v1``
document) plus operator-supplied metadata into a share-safe contribution
bundle on disk. It NEVER uploads, commits, opens a pull request, changes an
agent, or judges whether the operator's rights/consent statements are TRUE. It
prepares files and then stops for a human review.

It builds ONLY on the surfaces that already exist:

* :mod:`hotato.failure_record` projects the source into the single canonical,
  share-safe ``hotato.failure-record.v1`` dict (five separate lanes, no
  aggregate score, no raw payload). Its safe-projection + redaction rules are
  reused verbatim -- this module adds no new evaluation and can never change a
  source verdict.
* :func:`hotato.failure_render.render_json` renders the committed
  ``expected/failure-record.json`` byte-for-byte the way ``hotato record
  render`` does, so the generated reproduction command regenerates it exactly.
* :mod:`hotato.conversation_test` validates the minimal ``test.json`` (a real
  ``hotato.conversation-test`` document, an existing schema).
* :mod:`hotato.errors` supplies the FIFO-safe file reads, the finite-JSON
  emitter, and the shared handled-error contract (a refusal is a clean
  ``ValueError`` -> CLI exit 2).
* ``corpus/validate.py`` (the existing corpus conformance validator) is run,
  unchanged, when a PUBLIC contribution supplies a corpus label.

Metadata (rights, redaction) is read from VERSIONED FILES, never free-form
consent/license strings on the command line. The tool validates that the
required statements are PRESENT and well-typed; it cannot and does not judge
whether they are correct -- that is exactly what the printed human-review
checklist is for.

Privacy is the default and the floor. The bundle omits raw audio, transcript,
tool, and state payloads; it keeps evidence references and their sha256 digests
only. It never copies a credential, an authorization header, a tokenized URL,
an absolute path, an environment value, or a provider secret, and it never
clones a caller voice. A sha256 digest can still be a sensitive correlator, and
the bundle says so.

Determinism: identical validated inputs and metadata produce byte-identical
canonical files and digests. No wall-clock value is written into the bundle;
the only volatile value (the review timestamp) is printed to stdout, outside
the committed identity content.

Failure closes: every refusal below leaves NO partial output at the
destination. The whole bundle is built in a temporary directory and moved into
place with one atomic rename, so a refused or crashed run never promotes a
half-written bundle.
"""

from __future__ import annotations

import os
import re
import shutil
import tempfile
from importlib import util as _importlib_util
from typing import Any, Dict, List, Optional, Tuple

from . import __version__
from . import failure_record as _fr
from . import failure_render as _frr
from . import conversation_test as _ct
from .errors import load_json_file as _load_json_file
from .errors import open_regular as _open_regular
from .errors import safe_json_dumps as _safe_json_dumps

__all__ = [
    "prepare",
    "render_text",
    "result_json",
    "PREPARED_BY",
    "BUNDLE_KIND",
    "PROFILE_PRIVATE",
    "PROFILE_PUBLIC",
]

PREPARED_BY = "hotato regression prepare"
BUNDLE_KIND = "hotato.regression-candidate"
BUNDLE_VERSION = "1"

PROFILE_PRIVATE = "private-regression-v1"
PROFILE_PUBLIC = "public-corpus-v1"

_ORIGINS = ("captured", "simulated")
_INTENDED_USES = ("private_regression", "public_corpus", "benchmark_candidate")
_PUBLIC_USES = ("public_corpus", "benchmark_candidate")

# A codec an evidence recording may carry and still be a clean two-channel PCM
# WAV. Anything else (a compressed/opaque codec, or a mixed-down mono track) is
# refused: talk-over cannot be attributed reliably from it.
_SUPPORTED_AUDIO_CODECS = ("pcm_s16le", "pcm", "wav")

# The declared corpus-label validator (an existing script at repo root). Reused
# unchanged for a PUBLIC contribution; located on disk, never reimplemented.
_CORPUS_VALIDATOR_ENV = "HOTATO_CORPUS_VALIDATOR"

# An absolute-path shape (POSIX or Windows) is never allowed to enter the
# bundle: the safe projection excludes absolute paths outright, and operator
# metadata must not smuggle one back in.
_ABS_PATH_RE = re.compile(r"(?:^|[\s:=\"'(])((?:[A-Za-z]:[\\/])|/)[^\s\"';]+")


class RefusedError(ValueError):
    """A regression-prepare refusal. A ``ValueError`` subclass so the CLI's
    shared handled-error contract catches it (clean exit 2, structured error),
    but a distinct type so a caller can tell a deliberate refusal apart from an
    unexpected fault."""


# =========================================================================
# small deterministic helpers
# =========================================================================

def _canonical_bytes(obj: Any) -> bytes:
    """Deterministic, human-readable JSON bytes for a committed bundle file:
    sorted keys, two-space indent, UTF-8, a trailing newline, finite numbers
    only. Two equal objects serialize to identical bytes on any machine."""
    return (_safe_json_dumps(obj, indent=2, sort_keys=True, ensure_ascii=False)
            + "\n").encode("utf-8")


def _write_bytes(path: str, data: bytes) -> None:
    d = os.path.dirname(path)
    if d:
        os.makedirs(d, exist_ok=True)
    # open-ok: write mode into a freshly created temp bundle dir the tool owns
    with open(path, "wb") as fh:
        fh.write(data)


def _digest_file(path: str) -> str:
    with _open_regular(path, "rb") as fh:
        return _fr.digest_bytes(fh.read())


def _require(cond: bool, message: str) -> None:
    if not cond:
        raise RefusedError(message)


def _require_nonempty_str(obj: Dict[str, Any], key: str, where: str) -> str:
    value = obj.get(key)
    _require(
        isinstance(value, str) and value.strip() != "",
        f"missing required metadata: {where}.{key} must be a non-empty string",
    )
    return value


# =========================================================================
# workspace containment (traversal + symlink escape)
# =========================================================================

def _safe_relative(value: str) -> bool:
    """True for a relative, traversal-free, non-Windows-drive locator (the same
    rule the Failure Record uses for evidence locators)."""
    if value == ".":
        return True
    if not value or value.startswith(("/", "\\")) or re.match(r"^[A-Za-z]:", value):
        return False
    parts = [p for p in value.split("#", 1)[0].replace("\\", "/").split("/") if p]
    return ".." not in parts


def _resolve_in_workspace(workspace: str, locator: str) -> Optional[str]:
    """Resolve ``locator`` (a relative evidence path) strictly inside
    ``workspace``. Returns the resolved absolute path when the file EXISTS
    inside the workspace, ``None`` when the file is simply absent (its digest
    is still pinned in the record; absence is not an error), and REFUSES a
    traversal or a symlink that escapes the declared workspace."""
    _require(
        _safe_relative(locator),
        f"unsafe path: evidence locator {locator!r} is absolute or traverses "
        "outside the bundle; refusing to resolve it",
    )
    bare = locator.split("#", 1)[0]
    real_ws = os.path.realpath(workspace)
    candidate = os.path.join(workspace, bare)
    real_candidate = os.path.realpath(candidate)
    inside = (real_candidate == real_ws
              or real_candidate.startswith(real_ws + os.sep))
    _require(
        inside,
        f"unsafe path: evidence locator {locator!r} resolves outside the "
        "declared workspace (a symlink escape or traversal); refusing it",
    )
    return real_candidate if os.path.isfile(real_candidate) else None


def _scan_no_absolute_paths(text: str, where: str) -> None:
    """Refuse any absolute-path-like substring in committed metadata."""
    m = _ABS_PATH_RE.search(text)
    _require(
        m is None,
        f"unsafe path: {where} contains an absolute path "
        f"({m.group(1) if m else ''}...); the bundle must carry relative "
        "locators only, never an absolute filesystem path",
    )


# =========================================================================
# metadata: rights.json + redaction.json (versioned files)
# =========================================================================

def _load_rights(path: str) -> Dict[str, Any]:
    doc = _load_json_file(path, label=f"rights file {path!r}")
    _require(isinstance(doc, dict), "missing required metadata: rights file "
             "must be a JSON object")
    _require_nonempty_str(doc, "contributor", "rights")
    _require_nonempty_str(doc, "source_description", "rights")
    _require_nonempty_str(doc, "rights_basis", "rights")
    _require_nonempty_str(doc, "license", "rights")
    _require_nonempty_str(doc, "consent", "rights")
    _require_nonempty_str(doc, "private_data_review", "rights")
    origin = doc.get("origin")
    _require(
        origin in _ORIGINS,
        f"missing required metadata: rights.origin must be one of {_ORIGINS} "
        "(captured versus simulated origin)",
    )
    use = doc.get("intended_use")
    _require(
        use in _INTENDED_USES,
        f"missing required metadata: rights.intended_use must be one of "
        f"{_INTENDED_USES}",
    )
    _require(
        isinstance(doc.get("public_release"), bool),
        "missing required metadata: rights.public_release must be a boolean "
        "(may this material be released for a public contribution)",
    )
    if use in _PUBLIC_USES:
        _require(
            doc.get("public_release") is True,
            "public contribution requests a private-only artifact: "
            f"rights.intended_use is {use!r} but rights.public_release is "
            "false. Set public_release true only when every included artifact "
            "may actually be released, or use intended_use "
            "'private_regression'.",
        )
    corpus_label = doc.get("corpus_label")
    if corpus_label is not None:
        _require(
            isinstance(corpus_label, str) and corpus_label.strip() != "",
            "missing required metadata: rights.corpus_label, when present, "
            "must be a non-empty path string",
        )
    _scan_no_absolute_paths(_canonical_bytes(doc).decode("utf-8"), "rights.json")
    return doc


def _load_redaction(path: str) -> Dict[str, Any]:
    doc = _load_json_file(path, label=f"redaction file {path!r}")
    _require(isinstance(doc, dict), "missing required metadata: redaction file "
             "must be a JSON object")
    _require_nonempty_str(doc, "method", "redaction")
    _require_nonempty_str(doc, "reviewer", "redaction")
    _require(
        doc.get("completeness_declared") is True,
        "missing required metadata: redaction.completeness_declared must be "
        "true -- the reviewer must declare the redaction complete (the tool "
        "cannot judge whether it actually is; a human still reviews it)",
    )
    sentinels = doc.get("unredacted_sentinels", [])
    _require(
        isinstance(sentinels, list)
        and all(isinstance(s, str) and s for s in sentinels),
        "missing required metadata: redaction.unredacted_sentinels, when "
        "present, must be a list of non-empty marker strings",
    )
    audio = doc.get("audio")
    if audio is not None:
        _require(isinstance(audio, dict),
                 "missing required metadata: redaction.audio must be an object")
        codec = audio.get("codec")
        _require(
            isinstance(codec, str) and codec in _SUPPORTED_AUDIO_CODECS,
            "unsupported/mixed audio: redaction.audio.codec "
            f"{codec!r} is not a supported two-channel PCM WAV codec "
            f"{_SUPPORTED_AUDIO_CODECS}; hotato cannot attribute talk-over "
            "from a compressed or opaque track",
        )
        _require(
            audio.get("mixed") is not True,
            "unsupported/mixed audio: redaction.audio.mixed is true -- a "
            "mixed-down single track cannot separate caller and agent; "
            "provide a two-channel recording",
        )
        channels = audio.get("channels")
        if channels is not None:
            _require(isinstance(channels, dict),
                     "missing required metadata: redaction.audio.channels must "
                     "be an object with caller and agent indices")
            caller = channels.get("caller")
            agent = channels.get("agent")
            _require(
                isinstance(caller, int) and isinstance(agent, int)
                and not isinstance(caller, bool) and not isinstance(agent, bool),
                "missing required metadata: redaction.audio.channels.caller "
                "and .agent must be integer channel indices",
            )
            _require(
                caller != agent,
                "ambiguous channel mapping: redaction.audio.channels maps the "
                f"caller and the agent to the same channel ({caller}); they "
                "must be distinct channels",
            )
    _scan_no_absolute_paths(_canonical_bytes(doc).decode("utf-8"),
                            "redaction.json")
    return doc


# =========================================================================
# source -> Failure Record (the confirmed failure)
# =========================================================================

def _split_selector(raw: str) -> Tuple[str, Optional[str]]:
    if "#" in raw:
        path, selector = raw.split("#", 1)
        _require(
            selector != "",
            "empty selector after the number sign; use SOURCE#TEST_ID (for "
            "example suite-run.json#greeting-test) or drop the number sign",
        )
        return path, selector
    return raw, None


def _project_source(from_arg: str, *, staged_source: str) -> Tuple[Dict[str, Any], bool, Optional[str]]:
    """Resolve the ``--from`` source to the ONE Failure Record it represents.

    Returns ``(record, from_result, selector)``. ``from_result`` is True when
    the record was PROJECTED from a result file (so the failure can be
    re-rendered from a privately-held source-result.json), False when the
    source was already a ``hotato.failure-record.v1`` document (whose
    reproduction is its own content address).

    ``staged_source`` is a byte-identical copy of the source file named
    ``source-result.json``: projecting from it pins the reproduction command's
    relative path canonically, so a later ``hotato record render
    source-result.json`` regenerates the committed record byte-for-byte
    regardless of the original ``--from`` filename.
    """
    path, selector = _split_selector(from_arg)
    doc = _load_json_file(path, label=f"source {path!r}")
    _require(
        isinstance(doc, dict),
        f"source {path!r} is not a JSON object; expected a hotato test-run, "
        "suite-run, or contract-verify result (--format json), or a "
        "hotato.failure-record.v1 document",
    )
    if doc.get("kind") == _fr.KIND:
        # An already-projected Failure Record: re-validate it (recomputes and
        # verifies the content address and every honesty invariant). Its status
        # is guaranteed by validate_record to be FAIL/INCONCLUSIVE/ERROR.
        _fr.validate_record(doc)
        _require(
            selector is None,
            "a hotato.failure-record.v1 source projects exactly one failure; "
            "drop the number-sign selector",
        )
        return doc, False, None
    try:
        record = _fr.project(doc, selector=selector, source_path=staged_source)
    except _fr.NoFailureError as exc:
        raise RefusedError(
            "rerun does not reproduce the declared failure: " + str(exc)
        ) from exc
    return record, True, selector


# =========================================================================
# the minimal conversation-test (test.json), an existing schema
# =========================================================================

def _deterministic_assertions(record: Dict[str, Any]) -> List[Dict[str, Any]]:
    out: List[Dict[str, Any]] = []
    for lane in _fr.LANES:
        for assertion in record["dimensions"][lane]["assertions"]:
            if assertion.get("authority") != "deterministic":
                continue
            out.append({
                "id": assertion["assertion_id"],
                "kind": assertion["rule_id"],
                "dimension": assertion["dimension"],
            })
    return out


def _build_conversation_test(record: Dict[str, Any]) -> Dict[str, Any]:
    """A minimal, VALID ``hotato.conversation-test`` document that names the
    deterministic checks behind the recorded failure. It carries no payload --
    only the assertion ids, their rule kinds, and their dimensions -- and it
    passes exactly when every deterministic assertion passes, so re-running it
    against the same evidence reproduces the recorded regression."""
    subject = record["subject"]
    det = _deterministic_assertions(record)
    dims = sorted({a["dimension"] for a in det})
    doc = {
        "kind": _ct.KIND,
        "version": _ct.VERSION,
        "id": subject["test_id"],
        "agent": subject.get("agent_id", "unknown-agent"),
        "scenario": "expected/failure-record.json",
        "assertions": {"deterministic": det, "rubric": []},
        "success": {
            "required": ["all_deterministic_assertions_pass"],
            "report_dimensions": dims,
        },
        "repetitions": 1,
        "inconclusive_policy": "report",
    }
    # Validate through the real schema validator; a malformed doc is a bug here,
    # never a silent write.
    _ct.validate_conversation_test_doc(doc)
    return doc


def _evidence_index(record: Dict[str, Any]) -> Dict[str, Any]:
    """The safe evidence index: every reference and its sha256 digest, with no
    raw payload. A digest can still correlate a caller across bundles, so the
    index says so in its own note."""
    entries = []
    for item in record["evidence"]:
        entry = {
            "evidence_id": item["evidence_id"],
            "kind": item["kind"],
            "digest": item["digest"],
            "authority": item.get("authority"),
            "media_type": item.get("media_type"),
            "redacted": item.get("redacted", False),
            "redaction_classes": list(item.get("redaction_classes", [])),
        }
        if item.get("locator"):
            entry["locator"] = item["locator"]
        entries.append(entry)
    return {
        "kind": "hotato.regression-evidence-index",
        "version": "1",
        "note": (
            "Digest-pinned references only. No raw audio, transcript, tool, or "
            "state payload is included in this bundle. A sha256 digest can "
            "still be a sensitive correlator (it re-identifies the same "
            "recording across bundles); treat it as identifying."
        ),
        "evidence": entries,
    }


# =========================================================================
# corpus validator (existing script), run for a PUBLIC contribution
# =========================================================================

def _locate_corpus_validator() -> Optional[str]:
    """Find the existing ``corpus/validate.py`` on disk without importing a
    non-package path blindly: an explicit env override first, then a walk up
    from the current working directory. Returns the path or ``None``."""
    override = os.environ.get(_CORPUS_VALIDATOR_ENV)
    if override:
        return override if os.path.isfile(override) else None
    here = os.path.abspath(os.getcwd())
    while True:
        candidate = os.path.join(here, "corpus", "validate.py")
        if os.path.isfile(candidate):
            return candidate
        parent = os.path.dirname(here)
        if parent == here:
            return None
        here = parent


def _run_corpus_validator(label_path: str, audio_path: Optional[str]) -> Dict[str, Any]:
    """Run the EXISTING corpus conformance validator (unchanged) on a supplied
    corpus label. Refuses when it cannot be located or when the pair does not
    conform. Returns a small conformance summary for the manifest."""
    validator = _locate_corpus_validator()
    _require(
        validator is not None,
        "public contribution supplies a corpus label but the existing corpus "
        "validator (corpus/validate.py) could not be located; run from the "
        f"repository, or set {_CORPUS_VALIDATOR_ENV} to its path",
    )
    spec = _importlib_util.spec_from_file_location("hotato_corpus_validate",
                                                   validator)
    module = _importlib_util.module_from_spec(spec)
    spec.loader.exec_module(module)
    report = module.validate(label_path, audio_path)
    _require(
        bool(getattr(report, "ok", False)),
        "public contribution corpus label does not conform: "
        + "; ".join(getattr(report, "errors", []) or ["unknown error"]),
    )
    return {
        "validator": "corpus/validate.py",
        "conforms": True,
        "warnings": list(getattr(report, "warnings", []) or []),
    }


# =========================================================================
# the bundle
# =========================================================================

def _reproduction_block(record: Dict[str, Any], *, from_result: bool,
                        selector: Optional[str],
                        source_digest: Optional[str]) -> Dict[str, Any]:
    """The bundle-level, authoritative reproduction. For a projected source it
    is the paste-safe ``hotato record render source-result.json`` argv plus the
    privately-held source pinned by digest; for a Failure Record source it is
    the record's own content-address self-check."""
    if from_result:
        ref = "source-result.json" + (f"#{selector}" if selector else "")
        return {
            "method": "record-render",
            "argv": ["hotato", "record", "render", ref, "--out", "expected"],
            "working_directory": ".",
            "required_artifacts": [{
                "role": "source-result",
                "digest": source_digest,
                "relative_path": "source-result.json",
                "note": ("held privately by the operator; NOT included in this "
                         "share-safe bundle. Place it here, pinned to this "
                         "digest, to regenerate expected/failure-record.json."),
            }],
        }
    return {
        "method": "content-address",
        "argv": None,
        "working_directory": ".",
        "record_id": record["record_id"],
        "note": ("this candidate was prepared from an already-projected "
                 "hotato.failure-record.v1; its reproduction is its content "
                 "address (recompute record_id over expected/failure-record."
                 "json and compare)."),
    }


_CHECKLIST = (
    "Consent is on file for every audible party.",
    "Every direct identifier and quasi-identifier is removed from the "
    "evidence you hold.",
    "No PHI is present, regardless of consent.",
    "The rights basis and license in rights.json are correct for every "
    "included artifact.",
    "The redaction method and reviewer in redaction.json are accurate and the "
    "redaction is actually complete.",
    "You accept that a sha256 digest in this bundle can re-identify the same "
    "recording across bundles.",
    "hotato validated that these statements are PRESENT and well-typed; it "
    "did NOT and cannot judge whether they are TRUE. You are the reviewer.",
)


def _readme_text(*, profile: str, record: Dict[str, Any],
                 reproduction: Dict[str, Any], corpus: Optional[Dict[str, Any]]) -> str:
    subject = record["subject"]
    lines = [
        "# Regression candidate (prepared locally, NOT promoted)",
        "",
        f"Profile: {profile}",
        f"Subject: {subject['test_id']}",
        f"Recorded status: {record['status']}",
        f"Record id: {record['record_id']}",
        "",
        "This bundle was prepared by `" + PREPARED_BY + "`. It was not "
        "uploaded, committed, or opened as a pull request, and no agent was "
        "changed. It is a share-safe projection of one confirmed failure: it "
        "omits raw audio, transcript, tool, and state payloads and keeps "
        "evidence references and their sha256 digests only.",
        "",
        "## Files",
        "",
        "- `manifest.json` -- provenance, the authoritative reproduction argv, "
        "the evidence inventory, and a digest of every other file.",
        "- `rights.json` / `redaction.json` -- the operator metadata this was "
        "prepared from (validated for presence and type, never for truth).",
        "- `test.json` -- a minimal `hotato.conversation-test` naming the "
        "deterministic checks behind the failure.",
        "- `expected/failure-record.json` -- the canonical Failure Record.",
        "- `evidence/evidence-index.json` -- digest-pinned references, no "
        "payload.",
    ]
    if reproduction.get("method") == "record-render":
        lines.append("- `reproduce.sh` -- the paste-safe reproduction command "
                     "(re-renders the Failure Record from your privately-held "
                     "source-result.json).")
    if corpus is not None:
        lines.append("- `corpus-label.json` -- the corpus label, validated by "
                     "the existing `corpus/validate.py`.")
    lines += [
        "",
        "## Reproduce",
        "",
        "The authoritative reproduction is `manifest.json` -> `reproduction` "
        "-> `argv`.",
    ]
    if reproduction.get("method") == "record-render":
        lines += [
            "Place your privately-held `source-result.json` (pinned to the "
            "digest in the manifest) next to this file and run `reproduce.sh`, "
            "then compare the regenerated `expected/failure-record.json`.",
        ]
    else:
        lines += [
            "Recompute the `record_id` over `expected/failure-record.json` and "
            "compare it to the manifest.",
        ]
    lines += [
        "",
        "## Human review checklist (nothing is promoted until you sign off)",
        "",
    ]
    for item in _CHECKLIST:
        lines.append(f"- [ ] {item}")
    lines.append("")
    return "\n".join(lines)


def prepare(
    *,
    from_arg: str,
    rights_path: str,
    redaction_path: str,
    out_dir: str,
    workspace: Optional[str] = None,
    record_path: Optional[str] = None,
    force: bool = False,
) -> Dict[str, Any]:
    """Prepare one regression candidate bundle at ``out_dir`` and STOP.

    ``from_arg`` is the confirmed-failure source (a result file, optionally
    ``SOURCE#SELECTOR``, or a ``hotato.failure-record.v1`` document).
    ``rights_path`` / ``redaction_path`` are the versioned metadata files.
    ``workspace`` (default: the source's directory) is the declared root that
    evidence locators must resolve inside. ``record_path`` is an optional
    already-projected Failure Record to cross-check by content address.

    Returns a result dict. Raises :class:`RefusedError` (CLI exit 2) for any
    refusal in the spec's list; on any refusal NO partial output is left at
    ``out_dir``.
    """
    _require(bool(from_arg), "provide --from SOURCE (a confirmed-failure "
             "result file or a hotato.failure-record.v1 document)")
    _require(bool(out_dir), "--out DIR is required")
    source_file = _split_selector(from_arg)[0]
    declared_workspace = workspace or (os.path.dirname(os.path.abspath(source_file)))

    # Refuse an occupied destination up front (fail fast), and again before the
    # atomic rename (so a race cannot clobber it silently).
    if os.path.exists(out_dir):
        _require(
            force,
            f"--out {out_dir!r} already exists; pass --force to replace it, or "
            "choose a new --out",
        )

    rights = _load_rights(rights_path)
    redaction = _load_redaction(redaction_path)
    intended_use = rights["intended_use"]
    profile = PROFILE_PUBLIC if intended_use in _PUBLIC_USES else PROFILE_PRIVATE

    out_parent = os.path.dirname(os.path.abspath(out_dir)) or "."
    os.makedirs(out_parent, exist_ok=True)
    tmp_root = tempfile.mkdtemp(prefix=".hotato-regression-tmp-", dir=out_parent)
    # The staging dir holds the raw source (may carry payloads) named
    # canonically so the reproduction command's relative path is stable; it is
    # SEPARATE from the bundle tmp_root and is never promoted.
    stage_dir = tempfile.mkdtemp(prefix=".hotato-regression-src-", dir=out_parent)
    try:
        # Stage a byte-identical copy of the source named source-result.json so
        # a later `hotato record render source-result.json` regenerates the
        # committed record byte-for-byte regardless of the --from filename.
        staged_source = os.path.join(stage_dir, "source-result.json")
        with _open_regular(source_file, "rb") as fh:
            source_bytes = fh.read()
        _write_bytes(staged_source, source_bytes)

        record, from_result, selector = _project_source(
            from_arg, staged_source=staged_source)

        # Structural integrity of the projected record (content address, five
        # lanes, the outcome-evidence authority wall, the share-safe privacy
        # profile). No root here: an evidence file that is absent is pinned by
        # digest and legitimately held privately, not an error.
        _fr.validate_record(record)

        # Digest validation: resolve every evidence locator strictly inside the
        # declared workspace (refusing a traversal or a symlink escape), and
        # verify the digest of every file that is PRESENT against the record's
        # declared digest.
        for item in record["evidence"]:
            locator = item.get("locator")
            if not locator:
                continue
            resolved = _resolve_in_workspace(declared_workspace, locator)
            if resolved is not None:
                _require(
                    _digest_file(resolved) == item["digest"],
                    f"digest mismatch: evidence file {locator!r} does not match "
                    "the digest the failure record pins for it; the source and "
                    "the workspace are out of sync",
                )

        # Optional cross-check against an operator-supplied Failure Record.
        if record_path is not None:
            other = _load_json_file(record_path, label=f"record {record_path!r}")
            _require(
                isinstance(other, dict)
                and other.get("record_id") == record["record_id"],
                "digest mismatch: the --record cross-check document does not "
                "match the projected failure (its record_id differs). The "
                "source and the supplied Failure Record are not the same "
                "confirmed failure.",
            )

        source_digest = record["provenance"].get("source_result_digest")
        reproduction = _reproduction_block(
            record, from_result=from_result, selector=selector,
            source_digest=source_digest)

        # Determinism/reproduction check: re-project (a projected source) or
        # re-validate (a record source) and confirm the SAME content address.
        if from_result:
            recheck, _, _ = _project_source(from_arg, staged_source=staged_source)
            _require(
                recheck["record_id"] == record["record_id"],
                "rerun does not reproduce the declared failure: re-projecting "
                "the source did not yield the same record_id",
            )
        else:
            _fr.validate_record(record)
        reproduction_check = {
            "reproduced": True,
            "method": reproduction["method"],
            "status": record["status"],
            "record_id": record["record_id"],
        }

        # PUBLIC contribution: run the existing corpus validator when a corpus
        # label is supplied.
        corpus_summary: Optional[Dict[str, Any]] = None
        corpus_label_doc: Optional[Dict[str, Any]] = None
        if profile == PROFILE_PUBLIC and rights.get("corpus_label"):
            label_rel = rights["corpus_label"]
            label_abs = _resolve_in_workspace(declared_workspace, label_rel)
            _require(
                label_abs is not None,
                f"public contribution corpus label {label_rel!r} was not found "
                "inside the declared workspace",
            )
            corpus_label_doc = _load_json_file(label_abs, label="corpus label")
            audio_rel = corpus_label_doc.get("audio") if isinstance(
                corpus_label_doc, dict) else None
            audio_abs = None
            if isinstance(audio_rel, str):
                label_dir = os.path.relpath(os.path.dirname(label_abs),
                                            os.path.realpath(declared_workspace))
                audio_locator = os.path.normpath(
                    os.path.join(label_dir, audio_rel)) if label_dir != "." \
                    else audio_rel
                audio_abs = _resolve_in_workspace(declared_workspace,
                                                  audio_locator)
            corpus_summary = _run_corpus_validator(label_abs, audio_abs)

        # --- assemble the committed bundle content --------------------------
        conversation_test = _build_conversation_test(record)
        evidence_index = _evidence_index(record)
        expected_json = _frr.render_json(record)  # byte-identical to record render

        files: Dict[str, bytes] = {
            "rights.json": _canonical_bytes(rights),
            "redaction.json": _canonical_bytes(redaction),
            "test.json": _canonical_bytes(conversation_test),
            "evidence/evidence-index.json": _canonical_bytes(evidence_index),
            "expected/failure-record.json": expected_json.encode("utf-8"),
        }
        if corpus_label_doc is not None:
            files["corpus-label.json"] = _canonical_bytes(corpus_label_doc)
        # reproduce.sh only when policy permits: a PRIVATE regression may ship a
        # local repro script; a PUBLIC corpus artifact does not ship an
        # executable that points at a privately-held source.
        if profile == PROFILE_PRIVATE and reproduction["method"] == "record-render":
            files["reproduce.sh"] = _reproduce_sh(reproduction).encode("utf-8")

        # Redaction-sentinel wall: no declared un-redacted marker may remain in
        # any committed byte.
        _enforce_no_sentinels(files, redaction.get("unredacted_sentinels", []))

        for rel, data in files.items():
            _write_bytes(os.path.join(tmp_root, rel), data)

        inventory = _inventory(record)
        provenance = {
            "prepared_by": PREPARED_BY,
            "hotato": {"name": "hotato", "version": __version__},
            "schemas": [
                {"name": _fr.KIND, "version": _fr.VERSION},
                {"name": _ct.KIND, "version": str(_ct.VERSION)},
            ],
            "source_result_digest": source_digest,
            "record_id": record["record_id"],
        }
        manifest = {
            "kind": BUNDLE_KIND,
            "version": BUNDLE_VERSION,
            "profile": profile,
            "subject": record["subject"],
            "status": record["status"],
            "origin": {"declared": rights["origin"], "record": record["origin"]},
            "intended_use": intended_use,
            "provenance": provenance,
            "reproduction": reproduction,
            "reproduction_check": reproduction_check,
            "inventory": inventory,
        }
        if corpus_summary is not None:
            manifest["corpus_validation"] = corpus_summary
        # README first (its digest goes in the manifest), then manifest LAST
        # (it digests every other committed file; it cannot digest itself).
        readme = _readme_text(profile=profile, record=record,
                              reproduction=reproduction, corpus=corpus_summary)
        _write_bytes(os.path.join(tmp_root, "README.md"), readme.encode("utf-8"))

        digest_map = {}
        for dirpath, _dirs, filenames in os.walk(tmp_root):
            for fn in sorted(filenames):
                abs_path = os.path.join(dirpath, fn)
                rel = os.path.relpath(abs_path, tmp_root)
                if rel == "manifest.json":
                    continue
                digest_map[rel.replace(os.sep, "/")] = _digest_file(abs_path)
        manifest["files"] = dict(sorted(digest_map.items()))
        _write_bytes(os.path.join(tmp_root, "manifest.json"),
                     _canonical_bytes(manifest))

        # Drop the raw staged source before promoting: it may carry payloads and
        # must never enter the share-safe bundle.
        shutil.rmtree(stage_dir, ignore_errors=True)

        # --- atomic promote -------------------------------------------------
        if os.path.exists(out_dir):
            _require(force, f"--out {out_dir!r} already exists; pass --force")
            shutil.rmtree(out_dir)
        os.replace(tmp_root, out_dir)
    except BaseException:
        shutil.rmtree(tmp_root, ignore_errors=True)
        shutil.rmtree(stage_dir, ignore_errors=True)
        raise

    return {
        "out": out_dir,
        "profile": profile,
        "record_id": record["record_id"],
        "status": record["status"],
        "subject": record["subject"],
        "reproduction": reproduction,
        "reproduction_check": reproduction_check,
        "manifest": manifest,
        "checklist": list(_CHECKLIST),
    }


def _reproduce_sh(reproduction: Dict[str, Any]) -> str:
    """A paste-safe reproduction script: one command, no placeholder brackets,
    no trailing backslash, no inline comment, no shell pipe. The argv in the
    manifest is authoritative; this is a convenience mirror of it."""
    argv = reproduction["argv"]
    command = " ".join(argv)
    return (
        "#!/bin/sh\n"
        "set -eu\n"
        + command + "\n"
    )


def _enforce_no_sentinels(files: Dict[str, bytes], sentinels: List[str]) -> None:
    # redaction.json DECLARES the sentinel tokens, so it legitimately contains
    # them; every other committed file must be clean of them.
    for sentinel in sentinels:
        token = sentinel.encode("utf-8")
        for rel, data in files.items():
            if rel == "redaction.json":
                continue
            _require(
                token not in data,
                f"redaction sentinel remains: the marker {sentinel!r} still "
                f"appears in {rel}; the redaction is incomplete",
            )


def _inventory(record: Dict[str, Any]) -> Dict[str, Any]:
    """Inventory the sensitivity classes present in the projected evidence: a
    grouping of the record's own redaction-class tags, never a new judgment."""
    classes = sorted({
        cls
        for item in record["evidence"]
        for cls in item.get("redaction_classes", [])
    })
    kinds = sorted({item["kind"] for item in record["evidence"]})
    return {
        "evidence_count": len(record["evidence"]),
        "evidence_kinds": kinds,
        "sensitivity_classes": classes,
        "raw_payload_included": False,
        "note": ("payloads are excluded by the share-safe projection; only "
                 "references and sha256 digests are retained. A digest can "
                 "still correlate a recording across bundles."),
    }


# =========================================================================
# CLI rendering
# =========================================================================

def render_text(result: Dict[str, Any]) -> str:
    lines = [
        f"prepared regression candidate: {result['subject']['test_id']} "
        f"({result['status']})",
        f"  profile:   {result['profile']}",
        f"  out:       {result['out']}",
        f"  record_id: {result['record_id']}",
        f"  reproduce: {result['reproduction']['method']}",
        "  NOT uploaded, committed, or promoted. A human review is required:",
    ]
    for item in result["checklist"]:
        lines.append(f"    - {item}")
    return "\n".join(lines)


def result_json(result: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "tool": "hotato",
        "kind": "regression-prepare",
        "schema_version": "1",
        "out": result["out"],
        "profile": result["profile"],
        "status": result["status"],
        "record_id": result["record_id"],
        "subject": result["subject"],
        "reproduction": result["reproduction"],
        "reproduction_check": result["reproduction_check"],
        "checklist": result["checklist"],
    }
