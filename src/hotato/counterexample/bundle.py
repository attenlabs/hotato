"""Atomic compiler, independent verifier, inspector, and share-safe exporter."""

from __future__ import annotations

import copy
import ctypes
import errno
import io
import json
import os
import posixpath
import re
import shutil
import stat
import sys
import tempfile
from typing import Any, Dict, List, Optional, Set, Tuple

from .. import __version__
from .. import conversation_test as CT
from .. import scenario as SC
from .model import (
    ALGORITHM,
    CERTIFICATE_KIND,
    DEFAULT_BUDGET,
    KIND,
    MAX_BUDGET,
    MAX_CAPSULE_BYTES,
    MAX_CAPSULE_FILES,
    MAX_CAPSULE_MEMBER_BYTES,
    ORACLE_KIND,
    PRIVATE_PROFILE,
    REDUCER_SET,
    SHARE_PROFILE,
    VERSION,
    CounterexampleRefusal,
    assert_finite,
    canonical_json,
    count_leaves,
    digest_obj,
    inventory_files,
    load_json,
    mode_private,
    prefixed_digest,
    read_regular_bytes,
    require_within_workspace,
    sha256_bytes,
    validate_budget,
)
from .oracle import (
    FAILURE_ATOM_FIELDS,
    FailureOracle,
    failure_atom_sort_key,
    failure_identity_digest,
    projected_test,
    target_assertion,
)
from .reducers import (
    enumerate_units,
    final_single_unit_pass,
    hierarchical_reduce,
    verify_single_units,
)
from .render import render_html, render_markdown, render_svg
from .search import SearchState, apply_deletion_transform

_MANIFEST_KIND = "hotato.counterexample-manifest.v1"

_ASSERTION_KINDS = frozenset({
    "phrase", "pii", "policy", "tool_call", "outcome", "tool_result",
    "tool_error", "state", "state_change", "handoff", "dtmf",
    "termination", "latency", "entity_accuracy", "sequence", "count",
})
_DIMENSIONS = frozenset({
    "outcome", "policy", "conversation", "speech", "reliability",
})
_FROZEN_COMPONENTS = frozenset({
    "script", "tools", "state", "handoff", "termination",
})
_REDUCER_COMPONENTS = frozenset({
    "top-level-optional", "goal-optional", "caller-optional",
    "variation_matrix", "facts", "environment", "interruptions", "behavior",
    "script", "script-field", "tools", "tool-field", "handoff-field",
    "handoff", "termination-field", "termination", "state",
    "agent-mock-optional", "agent_mock",
})
_MINIMALITY_CODES = frozenset({
    None,
    "budget_exhausted",
    "simulator_invalid",
    "assertion_contract_invalid",
    "assertion_inconclusive",
    "target_failed",
    "target_absent",
    "candidate_invalid",
    "failure_identity_drift",
    "failure_atom_unavailable",
    "resource_limit_exceeded",
})
_MAX_JOURNAL_ROWS = MAX_BUDGET * 8 + 1
_PROOF_ARTIFACTS = {
    "source_scenario": "source/scenario.json",
    "source_conversation_test": "source/conversation-test.json",
    "source_scenario_file": "source/scenario.original",
    "source_conversation_test_file": "source/conversation-test.original",
    "scenario": "input/scenario.json",
    "conversation_test": "input/conversation-test.json",
    "expected_result": "expected/assertion-result.json",
    "certificate": "certificate.json",
    "journal": "reduction.jsonl",
    "minimality": "minimality.json",
}
_DERIVED_ARTIFACTS = {
    "report_markdown": "report.md",
    "report_html": "report.html",
    "share_card": "card.svg",
    "reproduce_script": "reproduce.sh",
    "predicate_script": "predicate.sh",
}
_ARTIFACTS = {**_PROOF_ARTIFACTS, **_DERIVED_ARTIFACTS}
_PRIVATE_MEMBER_PATHS = frozenset({
    "capsule.json", "oracle.json", *_ARTIFACTS.values(),
})
_SHARE_MEMBER_PATHS = frozenset({
    "capsule.json", "report.md", "report.html", "card.svg", "README.md",
})
_PRIVATE_CONTENT = ["scenario", "conversation_test", "assertion_result"]
_SHARE_OMITTED = [
    "audio", "transcript_body", "scenario_body", "assertion_body",
    "tool_payload", "state_value", "credentials", "absolute_paths",
    "provider_identifiers", "deletion_paths",
]
_MINIMALITY_CLAIMS = {
    "one_minimal": (
        "1-minimal under hotato.reducers.v1 with the recorded "
        "observation-scope freezes"
    ),
    "budget_exhausted": (
        "failure preserved; minimality incomplete because the candidate budget ended"
    ),
}
_SHARE_README = (
    "# Share-safe Hotato counterexample\n\n"
    "This projection contains no runnable conversation, transcript, audio, "
    "tool payload, state value, source-derived reducer path, credential, "
    "provider identifier, or absolute path. Verify the corresponding private "
    "capsule to reproduce the failure. SHA-256 values are correlators.\n"
)
_VERSION_STRING = re.compile(
    r"^[0-9]+(?:\.[0-9]+){1,3}(?:[A-Za-z0-9._+-]*)?$"
)


def _write(path: str, data: bytes, *, executable: bool = False) -> None:
    parent = os.path.dirname(path)
    os.makedirs(parent, mode=0o700, exist_ok=True)
    mode_private(parent)
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    fd = os.open(path, flags, 0o700 if executable else 0o600)
    try:
        with os.fdopen(fd, "wb") as fh:
            fh.write(data)
    except BaseException:
        try:
            os.unlink(path)
        except OSError:
            pass
        raise
    try:
        os.chmod(path, 0o700 if executable else 0o600)
    except OSError:
        pass


def _write_text(path: str, text: str, *, executable: bool = False) -> None:
    _write(path, text.encode("utf-8"), executable=executable)


def _write_json(path: str, value: Any) -> None:
    assert_finite(value, os.path.basename(path))
    _write_text(path, canonical_json(value, pretty=True))


def _jsonl(rows: List[Dict[str, Any]]) -> str:
    return "".join(canonical_json(row) for row in rows)


def _safe_output_parent(out_dir: str) -> Tuple[str, str]:
    absolute = os.path.abspath(out_dir)
    if os.path.lexists(absolute):
        raise CounterexampleRefusal(
            "output_exists", f"output {out_dir!r} already exists; refusing to overwrite it"
        )
    parent = os.path.abspath(os.path.dirname(absolute) or ".")

    def check_existing_components() -> None:
        drive, tail = os.path.splitdrive(parent)
        rooted = tail.startswith(os.path.sep)
        cursor = drive + os.path.sep if rooted else drive
        missing = False
        for part in [p for p in tail.split(os.path.sep) if p]:
            cursor = os.path.join(cursor, part) if cursor else part
            if missing:
                continue
            try:
                lst = os.lstat(cursor)
            except FileNotFoundError:
                missing = True
                continue
            if stat.S_ISLNK(lst.st_mode):
                raise CounterexampleRefusal(
                    "output_symlink_refused",
                    f"output parent {parent!r} contains a symlink component",
                )
            if not stat.S_ISDIR(lst.st_mode):
                raise CounterexampleRefusal(
                    "output_parent_invalid", f"output parent component {cursor!r} is not a directory"
                )

    # Refuse existing symlink ancestors before creating anything.  Re-check
    # after mkdir to narrow the race window and fail closed if the path changed.
    check_existing_components()
    os.makedirs(parent, mode=0o700, exist_ok=True)
    check_existing_components()
    if not os.path.isdir(parent):
        raise CounterexampleRefusal("output_parent_invalid", f"output parent {parent!r} is not a directory")
    return absolute, parent


def _rename_no_replace(source: str, destination: str) -> None:
    """Atomically publish a directory while refusing an existing destination."""
    if os.name == "nt":  # Windows rename already refuses an existing target.
        try:
            os.rename(source, destination)
            return
        except FileExistsError as exc:
            raise CounterexampleRefusal(
                "output_exists", f"output {destination!r} appeared during commit"
            ) from exc
    libc = ctypes.CDLL(None, use_errno=True)
    if sys.platform == "darwin":
        renamex_np = getattr(libc, "renamex_np", None)
        if renamex_np is None:
            raise CounterexampleRefusal(
                "atomic_commit_unsupported",
                "this macOS runtime lacks atomic no-replace directory rename",
            )
        renamex_np.argtypes = [ctypes.c_char_p, ctypes.c_char_p, ctypes.c_uint]
        renamex_np.restype = ctypes.c_int
        # <stdio.h> RENAME_EXCL: fail if the destination already exists.
        result = renamex_np(os.fsencode(source), os.fsencode(destination), 0x00000004)
        if result == 0:
            return
        code = ctypes.get_errno()
        if code in {errno.EEXIST, errno.ENOTEMPTY}:
            raise CounterexampleRefusal(
                "output_exists", f"output {destination!r} appeared during commit"
            )
        raise OSError(code, os.strerror(code), destination)
    renameat2 = getattr(libc, "renameat2", None)
    if renameat2 is None:
        raise CounterexampleRefusal(
            "atomic_commit_unsupported",
            "this platform cannot atomically publish a capsule without replacement",
        )
    renameat2.argtypes = [ctypes.c_int, ctypes.c_char_p, ctypes.c_int, ctypes.c_char_p, ctypes.c_uint]
    renameat2.restype = ctypes.c_int
    at_fdcwd = -100
    rename_noreplace = 1
    result = renameat2(
        at_fdcwd,
        os.fsencode(source),
        at_fdcwd,
        os.fsencode(destination),
        rename_noreplace,
    )
    if result == 0:
        return
    code = ctypes.get_errno()
    if code in {errno.EEXIST, errno.ENOTEMPTY}:
        raise CounterexampleRefusal(
            "output_exists", f"output {destination!r} appeared during commit"
        )
    raise OSError(code, os.strerror(code), destination)


def _load_inputs(
    scenario_path: str, test_path: str, workspace: Optional[str]
) -> Tuple[Dict[str, Any], Dict[str, Any], Dict[str, Any], bytes, bytes]:
    scenario_abs = os.path.abspath(scenario_path)
    test_abs = os.path.abspath(test_path)
    if workspace is None:
        workspace = os.path.commonpath([
            os.path.dirname(scenario_abs), os.path.dirname(test_abs)
        ])
    scenario_abs = require_within_workspace(scenario_abs, workspace)
    test_abs = require_within_workspace(test_abs, workspace)
    scenario_raw = read_regular_bytes(scenario_abs)
    test_raw = read_regular_bytes(test_abs)
    # Parse the exact bytes we just hashed. Reopening either mutable path here
    # would let a rename/write race bind one byte stream while reducing another.
    try:
        scenario = SC.validate_scenario_doc(SC.parse_scenario(scenario_raw.decode("utf-8")))
        test = CT.validate_conversation_test_doc(CT.parse_conversation_test(test_raw.decode("utf-8")))
    except UnicodeDecodeError as exc:
        raise CounterexampleRefusal("invalid_utf8", "counterexample inputs must be UTF-8") from exc
    assert_finite(scenario, "scenario")
    assert_finite(test, "conversation-test")
    source = {
        "scenario_file_sha256": "sha256:" + sha256_bytes(scenario_raw),
        "test_file_sha256": "sha256:" + sha256_bytes(test_raw),
        "scenario_digest": prefixed_digest(scenario),
        "test_digest": prefixed_digest(test),
    }
    return scenario, test, source, scenario_raw, test_raw


def _evaluator_digest() -> str:
    """Pin every local module that can change the scripted target verdict."""
    import importlib

    from .. import __name__ as package_name
    from .. import assert_ as assertion_module
    from .. import conversation as conversation_module
    from .. import conversation_test as conversation_test_module
    from .. import errors as errors_module
    from .. import scenario as scenario_module
    from .. import simulate as simulate_module
    from .. import state_adapter as state_module
    from .. import synth as synth_module
    from .. import trace as trace_module
    from . import model as counterexample_model_module
    from . import oracle as counterexample_oracle_module
    from . import reducers as counterexample_reducers_module
    from . import render as counterexample_render_module
    from . import search as counterexample_search_module

    package_module = importlib.import_module(package_name)

    rows = []
    modules = (
        package_module,
        assertion_module,
        conversation_test_module,
        scenario_module,
        simulate_module,
        state_module,
        synth_module,
        conversation_module,
        trace_module,
        errors_module,
        counterexample_model_module,
        counterexample_oracle_module,
        counterexample_reducers_module,
        counterexample_render_module,
        counterexample_search_module,
    )
    for module in modules:
        path = getattr(module, "__file__", None)
        if not path or not os.path.isfile(path):
            raise CounterexampleRefusal(
                "evaluator_unverifiable",
                f"cannot content-address evaluator module {getattr(module, '__name__', '<unknown>')!r}",
            )
        rows.append([
            getattr(module, "__name__", os.path.basename(path)),
            sha256_bytes(read_regular_bytes(path, max_bytes=8 * 1024 * 1024)),
        ])
    rows.append([__name__, sha256_bytes(read_regular_bytes(__file__, max_bytes=8 * 1024 * 1024))])
    return prefixed_digest(sorted(rows))


def _stats(scenario: Dict[str, Any], evaluation: Dict[str, Any]) -> Dict[str, int]:
    produced = evaluation.get("produced") or {}
    return {
        "bytes": len(canonical_json(scenario).encode("utf-8")),
        "turns": len((scenario.get("caller") or {}).get("script") or []),
        "tools": len((scenario.get("agent_mock") or {}).get("tools") or []),
        "state_leaves": count_leaves((scenario.get("agent_mock") or {}).get("state") or {}),
        "transcript_segments": len(((produced.get("transcript") or {}).get("segments") or [])),
        "trace_spans": len(((produced.get("trace") or {}).get("spans") or [])),
    }


def _preservation_doc(
    source_one: Dict[str, Any],
    source_two: Dict[str, Any],
    final_one: Dict[str, Any],
    final_two: Dict[str, Any],
) -> Dict[str, Any]:
    return {
        "source_executions": 2,
        "source_matching_failures": 2,
        "final_executions": 2,
        "final_matching_failures": 2,
        "source_result_digests": [source_one["result_digest"], source_two["result_digest"]],
        "final_result_digests": [final_one["result_digest"], final_two["result_digest"]],
        "source_content_hashes": [
            source_one["produced"]["content_hash"], source_two["produced"]["content_hash"],
        ],
        "final_content_hashes": [
            final_one["produced"]["content_hash"], final_two["produced"]["content_hash"],
        ],
        "source_trace_digests": [
            prefixed_digest(source_one["produced"]["trace"]),
            prefixed_digest(source_two["produced"]["trace"]),
        ],
        "final_trace_digests": [
            prefixed_digest(final_one["produced"]["trace"]),
            prefixed_digest(final_two["produced"]["trace"]),
        ],
        "same_failure_fingerprint": True,
    }


def _evaluation_signature(evaluation: Dict[str, Any]) -> Tuple[str, str, str]:
    produced = evaluation.get("produced") or {}
    return (
        str(evaluation.get("result_digest")),
        str(produced.get("content_hash")),
        prefixed_digest(produced.get("trace")),
    )


def _capsule_id(capsule: Dict[str, Any]) -> str:
    value = copy.deepcopy(capsule)
    value.pop("counterexample_id", None)
    return prefixed_digest(value)


def _manifest(root: str) -> Dict[str, Any]:
    return {
        "kind": _MANIFEST_KIND,
        "version": 1,
        "algorithm": "sha256",
        "files": inventory_files(root, exclude=("MANIFEST.sha256.json",)),
    }


def _finalize_manifest(root: str) -> None:
    _write_json(os.path.join(root, "MANIFEST.sha256.json"), _manifest(root))


def _reproduce_script() -> str:
    return """#!/bin/sh
set -eu
HERE=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
exec hotato counterexample reproduce "$HERE"
"""


def _predicate_script() -> str:
    return """#!/bin/sh
set -eu
HERE=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
set +e
hotato counterexample reproduce "$HERE" >/dev/null 2>&1
rc=$?
set -e
case "$rc" in
  0) exit 1 ;;
  1) exit 0 ;;
  *) exit 125 ;;
esac
"""


def _derived_artifact_bytes(capsule: Dict[str, Any]) -> Dict[str, bytes]:
    """Return every human/helper artifact whose bytes are bound by the capsule."""
    return {
        "report_markdown": render_markdown(capsule).encode("utf-8"),
        "report_html": render_html(capsule).encode("utf-8"),
        "share_card": render_svg(capsule).encode("utf-8"),
        "reproduce_script": _reproduce_script().encode("utf-8"),
        "predicate_script": _predicate_script().encode("utf-8"),
    }


def _verify_derived_artifacts(root: str, capsule: Dict[str, Any]) -> None:
    expected = _derived_artifact_bytes(capsule)
    artifacts = capsule["artifacts"]
    for name, expected_bytes in expected.items():
        observed = read_regular_bytes(_bundle_member(root, artifacts[name]))
        if observed != expected_bytes:
            raise CounterexampleRefusal(
                "derived_artifact_mismatch",
                f"derived artifact {artifacts[name]!r} is not the canonical capsule projection",
            )


def _share_artifact_bytes(capsule: Dict[str, Any]) -> Dict[str, bytes]:
    renderable = copy.deepcopy(capsule)
    renderable["target"]["assertion_id"] = capsule["target"]["assertion_ref"][:22]
    return {
        "capsule.json": canonical_json(capsule, pretty=True).encode("utf-8"),
        "report.md": render_markdown(renderable).encode("utf-8"),
        "report.html": render_html(renderable).encode("utf-8"),
        "card.svg": render_svg(renderable).encode("utf-8"),
        "README.md": _SHARE_README.encode("utf-8"),
    }


def _verify_share_artifacts(root: str, capsule: Dict[str, Any]) -> None:
    for relative, expected in _share_artifact_bytes(capsule).items():
        observed = read_regular_bytes(_bundle_member(root, relative))
        if observed != expected:
            raise CounterexampleRefusal(
                "derived_artifact_mismatch",
                f"share artifact {relative!r} is not the canonical capsule projection",
            )


def compile_counterexample(
    scenario_path: str,
    test_path: str,
    *,
    target: str,
    out_dir: str,
    workspace: Optional[str] = None,
    budget: int = DEFAULT_BUDGET,
    seed: Optional[int] = None,
    profile: str = PRIVATE_PROFILE,
) -> Dict[str, Any]:
    """Compile one deterministic failure into an atomic `.hotato-repro` dir."""
    if profile != PRIVATE_PROFILE:
        raise CounterexampleRefusal(
            "compile_profile_refused",
            f"compile emits {PRIVATE_PROFILE!r}; use counterexample export for {SHARE_PROFILE!r}",
        )
    budget = validate_budget(budget)
    scenario, test_doc, source, scenario_raw, test_raw = _load_inputs(
        scenario_path, test_path, workspace
    )
    if "" in scenario:
        raise CounterexampleRefusal(
            "unsupported_source",
            "counterexample v1 cannot represent an empty top-level scenario key",
        )
    assertion = target_assertion(test_doc, target)
    selected_seed = scenario.get("seed", 0) if seed is None else seed
    if isinstance(selected_seed, bool) or not isinstance(selected_seed, int) or selected_seed < 0:
        raise CounterexampleRefusal("invalid_seed", "seed must be an integer >= 0")

    oracle = FailureOracle(test_doc, assertion, selected_seed)
    source_eval = oracle.freeze_source(scenario)
    source_replay = oracle.evaluate(copy.deepcopy(scenario))
    if source_replay.get("status") != "PRESERVED":
        raise CounterexampleRefusal("source_replay_failed", "the source failure did not survive a second replay")
    if _evaluation_signature(source_eval) != _evaluation_signature(source_replay):
        raise CounterexampleRefusal("source_replay_drift", "the two source replays produced different result, content, or trace bytes")
    projected = projected_test(test_doc, assertion)

    search = SearchState(budget, oracle.evaluate)
    # Seed the cache with the already-evaluated source without charging budget.
    search.cache[digest_obj(scenario)] = {
        key: value for key, value in source_eval.items() if key != "produced"
    }
    reduced = hierarchical_reduce(scenario, search, oracle.frozen)
    reduced, minimality_checks, complete = final_single_unit_pass(
        reduced, search, oracle.frozen
    )

    final_one = oracle.evaluate(reduced)
    final_two = oracle.evaluate(copy.deepcopy(reduced))
    if final_one.get("status") != "PRESERVED" or final_two.get("status") != "PRESERVED":
        raise CounterexampleRefusal(
            "final_replay_failed", "the reduced candidate did not preserve the target on both final replays"
        )
    if _evaluation_signature(final_one) != _evaluation_signature(final_two):
        raise CounterexampleRefusal(
            "final_replay_drift", "the two final deterministic replays produced different result, content, or trace bytes"
        )
    if final_one["identity"]["fingerprint"] != oracle.source_identity["fingerprint"]:
        raise CounterexampleRefusal("failure_identity_drift", "the final failure fingerprint changed")

    initial_stats = _stats(scenario, source_eval)
    final_stats = _stats(reduced, final_one)
    minimality_status = "one_minimal" if complete else "budget_exhausted"

    journal_text = _jsonl(search.journal)
    minimality_doc = {
        "status": minimality_status,
        "reducer_set": REDUCER_SET,
        "claim": _MINIMALITY_CLAIMS[minimality_status],
        "remaining_unit_checks": minimality_checks,
        "frozen_components": sorted(oracle.frozen),
    }
    certificate = {
        "kind": CERTIFICATE_KIND,
        "version": VERSION,
        "algorithm": ALGORITHM,
        "reducer_set": REDUCER_SET,
        "source_scenario_digest": source["scenario_digest"],
        "final_scenario_digest": prefixed_digest(reduced),
        "source_test_digest": source["test_digest"],
        "final_test_digest": prefixed_digest(projected),
        "failure_fingerprint": oracle.source_identity["fingerprint"],
        "accepted_steps": search.accepted_steps,
        "journal_sha256": "sha256:" + sha256_bytes(journal_text.encode("utf-8")),
        "budget": budget,
        "candidate_evaluations": search.evaluations,
        "cache_hits": search.cache_hits,
        "termination": minimality_status,
    }
    oracle_doc = oracle.oracle_document()
    capsule: Dict[str, Any] = {
        "kind": KIND,
        "version": VERSION,
        "counterexample_id": "pending",
        "source": source,
        "target": oracle.source_identity,
        "oracle": {
            "path": "oracle.json",
            "digest": prefixed_digest(oracle_doc),
        },
        "artifacts": dict(_ARTIFACTS),
        "artifact_digests": {
            "source_scenario": prefixed_digest(scenario),
            "source_conversation_test": prefixed_digest(test_doc),
            "source_scenario_file": "sha256:" + sha256_bytes(scenario_raw),
            "source_conversation_test_file": "sha256:" + sha256_bytes(test_raw),
            "scenario": prefixed_digest(reduced),
            "conversation_test": prefixed_digest(projected),
            "expected_result": prefixed_digest(final_one["result"]),
            "certificate": prefixed_digest(certificate),
            "journal": "sha256:" + sha256_bytes(journal_text.encode("utf-8")),
            "minimality": prefixed_digest(minimality_doc),
        },
        "reduction": {
            "algorithm": ALGORITHM,
            "reducer_set": REDUCER_SET,
            "initial": initial_stats,
            "final": final_stats,
            "attempts": len(search.journal),
            "candidate_evaluations": search.evaluations,
            "qualification_evaluations": 4,
            "total_evaluations": search.evaluations + 4,
            "accepted": search.accepted,
            "cache_hits": search.cache_hits,
            "budget": budget,
            "termination": minimality_status,
        },
        "minimality": minimality_doc,
        "preservation": _preservation_doc(source_eval, source_replay, final_one, final_two),
        "privacy": {
            "profile": PRIVATE_PROFILE,
            "content_included": list(_PRIVATE_CONTENT),
            "network_egress": False,
            "hashes_are_correlators": True,
        },
        "provenance": {
            "hotato_version": __version__,
            "evaluator_digest": _evaluator_digest(),
            "seed": selected_seed,
            "scenario_selection": {
                "mode": "base-scenario",
                "seed": selected_seed,
                "variation_matrix_applied": False,
            },
        },
    }
    derived_artifacts = _derived_artifact_bytes(capsule)
    capsule["artifact_digests"].update({
        name: "sha256:" + sha256_bytes(data)
        for name, data in derived_artifacts.items()
    })
    capsule["counterexample_id"] = _capsule_id(capsule)

    output, parent = _safe_output_parent(out_dir)
    stage = tempfile.mkdtemp(prefix=".hotato-counterexample-", dir=parent)
    mode_private(stage)
    try:
        _write_json(os.path.join(stage, "capsule.json"), capsule)
        _write_json(os.path.join(stage, "oracle.json"), oracle_doc)
        _write_json(os.path.join(stage, "certificate.json"), certificate)
        _write_json(os.path.join(stage, "minimality.json"), minimality_doc)
        _write_text(os.path.join(stage, "reduction.jsonl"), journal_text)
        _write_json(os.path.join(stage, "source", "scenario.json"), scenario)
        _write_json(os.path.join(stage, "source", "conversation-test.json"), test_doc)
        _write(os.path.join(stage, "source", "scenario.original"), scenario_raw)
        _write(os.path.join(stage, "source", "conversation-test.original"), test_raw)
        _write_json(os.path.join(stage, "input", "scenario.json"), reduced)
        _write_json(os.path.join(stage, "input", "conversation-test.json"), projected)
        _write_json(os.path.join(stage, "expected", "assertion-result.json"), final_one["result"])
        for name, data in derived_artifacts.items():
            _write(
                os.path.join(stage, _DERIVED_ARTIFACTS[name]),
                data,
                executable=name in {"reproduce_script", "predicate_script"},
            )
        _finalize_manifest(stage)
        _rename_no_replace(stage, output)
        stage = ""
    finally:
        if stage and os.path.isdir(stage):
            shutil.rmtree(stage)

    return {
        "kind": "counterexample-compile",
        # A reproducible capsule with unfinished minimization is useful, but it
        # is not the same automation outcome as an earned one-minimal proof.
        # Keep the artifact and make the distinction visible without requiring
        # a caller to parse a nested field.
        "exit_code": 0 if minimality_status == "one_minimal" else 1,
        "counterexample_id": capsule["counterexample_id"],
        "target": capsule["target"],
        "minimality": minimality_status,
        "reduction": capsule["reduction"],
        "output": output,
        "reproduce": os.path.join(output, "reproduce.sh"),
        "predicate": os.path.join(output, "predicate.sh"),
    }


def _assert_member_path(root: str, path: str, rel: str) -> None:
    resolved = os.path.realpath(path)
    try:
        inside = os.path.commonpath([root, resolved]) == root
    except ValueError:
        inside = False
    if not inside:
        raise CounterexampleRefusal("unsafe_member", f"bundle member {rel!r} escapes the capsule")
    cursor = root
    for part in os.path.relpath(path, root).split(os.sep):
        cursor = os.path.join(cursor, part)
        try:
            info = os.lstat(cursor)
        except FileNotFoundError:
            raise CounterexampleRefusal(
                "capsule_missing", f"bundle member {rel!r} is missing"
            )
        if stat.S_ISLNK(info.st_mode):
            raise CounterexampleRefusal(
                "symlink_refused", f"bundle member {rel!r} contains a symlink component"
            )


def _bundle_member(
    root: str, rel: str, *, max_bytes: int = MAX_CAPSULE_MEMBER_BYTES
) -> str:
    if (
        not isinstance(rel, str)
        or not rel
        or "\\" in rel
        or os.path.isabs(rel)
        or posixpath.isabs(rel)
    ):
        raise CounterexampleRefusal("unsafe_member", f"unsafe bundle member {rel!r}")
    norm = posixpath.normpath(rel)
    if norm in {".", ".."} or norm.startswith("../") or norm != rel:
        raise CounterexampleRefusal("unsafe_member", f"bundle member {rel!r} escapes the capsule")
    path = os.path.join(root, *norm.split("/"))
    _assert_member_path(root, path, rel)
    read_regular_bytes(path, max_bytes=max_bytes)
    # Recheck after opening. A concurrent ancestor substitution must not turn
    # a path validated inside the capsule into an external regular file.
    _assert_member_path(root, path, rel)
    return path


def _verify_manifest(root: str) -> Dict[str, Any]:
    manifest_path = _bundle_member(root, "MANIFEST.sha256.json")
    manifest = load_json(manifest_path)
    if not isinstance(manifest, dict) or set(manifest) != {"kind", "version", "algorithm", "files"}:
        raise CounterexampleRefusal("manifest_schema", "counterexample manifest fields are malformed")
    if (
        manifest.get("kind") != _MANIFEST_KIND
        or isinstance(manifest.get("version"), bool)
        or not isinstance(manifest.get("version"), int)
        or manifest.get("version") != 1
    ):
        raise CounterexampleRefusal("manifest_schema", "counterexample manifest kind/version mismatch")
    if manifest.get("algorithm") != "sha256":
        raise CounterexampleRefusal("manifest_schema", "counterexample manifest algorithm must be sha256")
    rows = manifest.get("files")
    if not isinstance(rows, list):
        raise CounterexampleRefusal("manifest_schema", "manifest files must be a list")
    # Validate the bounded inventory before opening any member.  This prevents
    # a forged manifest with thousands of duplicate rows from turning the
    # verifier's own integrity pass into unbounded file IO.
    if len(rows) > MAX_CAPSULE_FILES:
        raise CounterexampleRefusal(
            "capsule_too_many_files",
            f"capsule exceeds {MAX_CAPSULE_FILES} manifested files",
        )
    declared: List[str] = []
    declared_bytes = 0
    for row in rows:
        if not isinstance(row, dict) or set(row) != {"path", "sha256", "bytes"}:
            raise CounterexampleRefusal("manifest_schema", "manifest file row is malformed")
        relative = row.get("path")
        if (
            not isinstance(relative, str)
            or not relative
            or "\\" in relative
            or os.path.isabs(relative)
            or posixpath.isabs(relative)
        ):
            raise CounterexampleRefusal("unsafe_member", f"unsafe bundle member {relative!r}")
        normalized = posixpath.normpath(relative)
        if (
            normalized in {".", ".."}
            or normalized.startswith("../")
            or normalized != relative
        ):
            raise CounterexampleRefusal(
                "unsafe_member", f"bundle member {relative!r} is not a canonical relative path"
            )
        digest = row.get("sha256")
        size = row.get("bytes")
        if not isinstance(digest, str) or len(digest) != 64 or any(ch not in "0123456789abcdef" for ch in digest):
            raise CounterexampleRefusal("manifest_schema", "manifest sha256 value is malformed")
        if isinstance(size, bool) or not isinstance(size, int) or size < 0:
            raise CounterexampleRefusal("manifest_schema", "manifest byte count is malformed")
        if size > MAX_CAPSULE_MEMBER_BYTES:
            raise CounterexampleRefusal(
                "capsule_member_too_large",
                f"bundle member {relative!r} exceeds {MAX_CAPSULE_MEMBER_BYTES} bytes",
            )
        declared_bytes += size
        if declared_bytes > MAX_CAPSULE_BYTES:
            raise CounterexampleRefusal(
                "capsule_too_large",
                f"capsule exceeds {MAX_CAPSULE_BYTES} manifested bytes",
            )
        declared.append(relative)
    if len(declared) != len(set(declared)):
        raise CounterexampleRefusal(
            "manifest_inventory", "manifest paths must be unique"
        )

    for row in rows:
        member = _bundle_member(
            root, row["path"], max_bytes=MAX_CAPSULE_MEMBER_BYTES
        )
        data = read_regular_bytes(member, max_bytes=MAX_CAPSULE_MEMBER_BYTES)
        if len(data) != row["bytes"] or sha256_bytes(data) != row["sha256"]:
            raise CounterexampleRefusal("digest_mismatch", f"bundle member {row['path']!r} failed sha256 verification")
    actual = [row["path"] for row in inventory_files(root, exclude=("MANIFEST.sha256.json",))]
    if declared != actual:
        raise CounterexampleRefusal("manifest_inventory", "bundle has missing, duplicate, reordered, or undeclared files")
    # Files alone do not describe empty directories. Reject them so archive
    # extraction cannot smuggle an unbounded, unmanifested directory tree into
    # an otherwise valid capsule.
    declared_directories: Set[str] = set()
    for relative in declared:
        parent = posixpath.dirname(relative)
        while parent:
            declared_directories.add(parent)
            parent = posixpath.dirname(parent)
    for current, directories, _files in os.walk(root, topdown=True, followlinks=False):
        for name in directories:
            path = os.path.join(current, name)
            if os.path.islink(path):
                raise CounterexampleRefusal(
                    "symlink_refused", f"capsule directory {path!r} is a symlink"
                )
            relative = os.path.relpath(path, root).replace(os.sep, "/")
            if relative not in declared_directories:
                raise CounterexampleRefusal(
                    "manifest_inventory",
                    f"capsule contains undeclared directory {relative!r}",
                )
    return manifest


def _validate_profile_inventory(
    manifest: Dict[str, Any], capsule: Dict[str, Any]
) -> None:
    profile = capsule.get("privacy", {}).get("profile")
    expected = (
        _PRIVATE_MEMBER_PATHS
        if profile == PRIVATE_PROFILE
        else _SHARE_MEMBER_PATHS
        if profile == SHARE_PROFILE
        else frozenset()
    )
    observed = {row["path"] for row in manifest["files"]}
    if observed != expected:
        raise CounterexampleRefusal(
            "profile_inventory",
            f"{profile!r} capsule members do not match the closed profile inventory",
        )


def _valid_digest(value: Any, *, prefixed: bool = True) -> bool:
    if not isinstance(value, str):
        return False
    raw = value[7:] if prefixed and value.startswith("sha256:") else value
    if prefixed and not value.startswith("sha256:"):
        return False
    return len(raw) == 64 and all(ch in "0123456789abcdef" for ch in raw)


def _nonnegative_integer(value: Any, *, maximum: Optional[int] = None) -> bool:
    return (
        not isinstance(value, bool)
        and isinstance(value, int)
        and value >= 0
        and (maximum is None or value <= maximum)
    )


def _validate_failure_atom(kind: str, atom: Any) -> None:
    branches = FAILURE_ATOM_FIELDS.get(kind)
    if not isinstance(atom, dict) or not branches:
        raise CounterexampleRefusal("capsule_schema", "failure atom is malformed")
    code = atom.get("code")
    fields = branches.get(code) if isinstance(code, str) else None
    if fields is None or set(atom) != {"code", *fields}:
        raise CounterexampleRefusal(
            "capsule_schema", "failure atom branch or fields are malformed"
        )
    for field in fields:
        value = atom[field]
        if field == "index":
            if not _nonnegative_integer(value):
                raise CounterexampleRefusal(
                    "capsule_schema", "failure atom index is malformed"
                )
        elif not isinstance(value, str) or not value:
            raise CounterexampleRefusal(
                "capsule_schema", f"failure atom {field} is malformed"
            )
    if kind == "pii" and atom.get("detector") not in {
        "ssn", "card_luhn", "email", "phone",
    }:
        raise CounterexampleRefusal(
            "capsule_schema", "failure atom detector is unsupported"
        )
    if kind == "policy" and atom.get("type") not in {
        "banned", "required_disclosure_missing",
    }:
        raise CounterexampleRefusal(
            "capsule_schema", "failure atom policy type is unsupported"
        )
def _validate_oracle_document(document: Any, target: Dict[str, Any]) -> None:
    if not isinstance(document, dict) or set(document) != {
        "kind", "version", "authority", "ci_gate_eligible", "target",
        "observation_scope",
    }:
        raise CounterexampleRefusal("oracle_schema", "oracle document fields are malformed")
    if (
        document.get("kind") != ORACLE_KIND
        or isinstance(document.get("version"), bool)
        or not isinstance(document.get("version"), int)
        or document.get("version") != VERSION
        or document.get("authority") != "deterministic"
        or document.get("ci_gate_eligible") is not True
        or document.get("target") != target
    ):
        raise CounterexampleRefusal("oracle_schema", "oracle contract is malformed")
    scope = document.get("observation_scope")
    if not isinstance(scope, dict) or set(scope) != {
        "frozen_components", "rule", "minimum_caller_turns", "transforms",
    }:
        raise CounterexampleRefusal("oracle_schema", "oracle observation scope is malformed")
    frozen = scope.get("frozen_components")
    if (
        not isinstance(frozen, list)
        or not all(isinstance(item, str) for item in frozen)
        or len(frozen) != len(set(frozen))
        or not set(frozen).issubset(_FROZEN_COMPONENTS)
        or isinstance(scope.get("minimum_caller_turns"), bool)
        or not isinstance(scope.get("minimum_caller_turns"), int)
        or scope.get("minimum_caller_turns") != 1
        or scope.get("transforms") != "deletion-only"
        or scope.get("rule") != (
            "candidate is a complete schema-valid scripted session and must preserve "
            "the source-selected structured failure branch"
        )
    ):
        raise CounterexampleRefusal("oracle_schema", "oracle observation scope is malformed")


def _validate_capsule(capsule: Dict[str, Any]) -> None:
    if not isinstance(capsule, dict):
        raise CounterexampleRefusal("capsule_schema", "capsule.json must be an object")
    if (
        capsule.get("kind") != KIND
        or isinstance(capsule.get("version"), bool)
        or not isinstance(capsule.get("version"), int)
        or capsule.get("version") != VERSION
    ):
        raise CounterexampleRefusal("capsule_schema", "capsule kind/version mismatch")
    privacy = capsule.get("privacy")
    profile = privacy.get("profile") if isinstance(privacy, dict) else None
    common = {
        "kind", "version", "counterexample_id", "source", "target", "reduction",
        "minimality", "preservation", "privacy", "provenance",
    }
    if profile == PRIVATE_PROFILE:
        expected_top = common | {"oracle", "artifacts", "artifact_digests"}
    elif profile == SHARE_PROFILE:
        expected_top = common
    else:
        raise CounterexampleRefusal("capsule_schema", "capsule privacy profile is unsupported")
    if set(capsule) != expected_top:
        raise CounterexampleRefusal("capsule_schema", "capsule fields do not match its privacy profile")
    forbidden = {"overall_score", "aggregate_score"}
    stack = [capsule]
    while stack:
        value = stack.pop()
        if isinstance(value, dict):
            if forbidden.intersection(value):
                raise CounterexampleRefusal("blended_score_refused", "counterexample schemas forbid blended score fields")
            stack.extend(value.values())
        elif isinstance(value, list):
            stack.extend(value)
    assert_finite(capsule, "capsule")

    digest = _valid_digest

    if not digest(capsule.get("counterexample_id")):
        raise CounterexampleRefusal("capsule_schema", "counterexample_id is not a sha256 digest")
    reduction = capsule.get("reduction")
    expected_reduction = {
        "algorithm", "reducer_set", "initial", "final", "attempts",
        "candidate_evaluations", "qualification_evaluations", "total_evaluations",
        "accepted", "cache_hits", "budget", "termination",
    }
    if not isinstance(reduction, dict) or set(reduction) != expected_reduction:
        raise CounterexampleRefusal("capsule_schema", "reduction summary is malformed")
    if reduction.get("algorithm") != ALGORITHM or reduction.get("reducer_set") != REDUCER_SET:
        raise CounterexampleRefusal("capsule_schema", "reduction algorithm or reducer set is unsupported")
    if reduction.get("termination") not in ("one_minimal", "budget_exhausted"):
        raise CounterexampleRefusal("capsule_schema", "reduction termination is unsupported")
    for key in (
        "attempts", "candidate_evaluations", "qualification_evaluations",
        "total_evaluations", "accepted", "cache_hits", "budget",
    ):
        value = reduction.get(key)
        if isinstance(value, bool) or not isinstance(value, int) or value < (1 if key == "budget" else 0):
            raise CounterexampleRefusal("capsule_schema", f"reduction {key} is malformed")
    if reduction["budget"] > MAX_BUDGET:
        raise CounterexampleRefusal("capsule_schema", "reduction budget exceeds the hard limit")
    if reduction["candidate_evaluations"] > reduction["budget"]:
        raise CounterexampleRefusal("capsule_schema", "candidate evaluations exceed their budget")
    if reduction["attempts"] < reduction["accepted"]:
        raise CounterexampleRefusal("capsule_schema", "accepted reductions exceed attempts")
    if reduction["qualification_evaluations"] != 4:
        raise CounterexampleRefusal("capsule_schema", "qualification evaluation count must be four")
    if reduction["total_evaluations"] != reduction["candidate_evaluations"] + 4:
        raise CounterexampleRefusal("capsule_schema", "total evaluation count is inconsistent")
    stat_keys = {"bytes", "turns", "tools", "state_leaves", "transcript_segments", "trace_spans"}
    for name in ("initial", "final"):
        stats = reduction.get(name)
        if not isinstance(stats, dict) or set(stats) != stat_keys:
            raise CounterexampleRefusal("capsule_schema", f"reduction {name} stats are malformed")
        if any(isinstance(value, bool) or not isinstance(value, int) or value < 0 for value in stats.values()):
            raise CounterexampleRefusal("capsule_schema", f"reduction {name} stats are malformed")
        if stats["bytes"] < 1 or not (1 <= stats["turns"] <= 10_000) or stats["tools"] > 10_000:
            raise CounterexampleRefusal("capsule_schema", f"reduction {name} stats are out of bounds")
    if any(reduction["final"][key] > reduction["initial"][key] for key in stat_keys):
        raise CounterexampleRefusal("capsule_schema", "delete-only reduction statistics grew")

    minimality = capsule.get("minimality")
    expected_minimality = {
        "status", "reducer_set", "claim", "frozen_components",
        "remaining_unit_checks" if profile == PRIVATE_PROFILE else "check_summary",
    }
    if not isinstance(minimality, dict) or set(minimality) != expected_minimality:
        raise CounterexampleRefusal("capsule_schema", "minimality summary is malformed")
    if minimality.get("status") != reduction["termination"] or minimality.get("reducer_set") != REDUCER_SET:
        raise CounterexampleRefusal("capsule_schema", "minimality status does not match reduction")
    if minimality.get("claim") != _MINIMALITY_CLAIMS[minimality["status"]]:
        raise CounterexampleRefusal("capsule_schema", "minimality evidence is malformed")
    frozen = minimality.get("frozen_components")
    if (
        not isinstance(frozen, list)
        or not all(isinstance(item, str) for item in frozen)
        or len(frozen) != len(set(frozen))
        or not set(frozen).issubset(_FROZEN_COMPONENTS)
    ):
        raise CounterexampleRefusal("capsule_schema", "minimality frozen components are malformed")
    if profile == PRIVATE_PROFILE:
        checks = minimality.get("remaining_unit_checks")
        if not isinstance(checks, list):
            raise CounterexampleRefusal(
                "capsule_schema", "minimality checks are malformed"
            )
        for row in checks:
            if not isinstance(row, dict) or set(row) != {
                "path", "component", "outcome", "code", "candidate_digest",
            }:
                raise CounterexampleRefusal("capsule_schema", "minimality check is malformed")
            if (
                not isinstance(row["path"], str)
                or not row["path"]
                or not isinstance(row["component"], str)
                or row["component"] not in _REDUCER_COMPONENTS
                or not isinstance(row["outcome"], str)
                or row["outcome"]
                not in {"PRESERVED", "ABSENT", "DRIFTED", "UNRESOLVED"}
                or (
                    row["code"] is not None
                    and not isinstance(row["code"], str)
                )
                or row["code"] not in _MINIMALITY_CODES
                or not digest(row["candidate_digest"], prefixed=False)
            ):
                raise CounterexampleRefusal(
                    "capsule_schema", "minimality check values are malformed"
                )
    else:
        summary = minimality.get("check_summary")
        outcomes = summary.get("outcomes") if isinstance(summary, dict) else None
        expected_outcomes = {"PRESERVED", "ABSENT", "DRIFTED", "UNRESOLVED"}
        if (
            not isinstance(summary, dict)
            or set(summary) != {"count", "outcomes"}
            or not _nonnegative_integer(summary.get("count"))
            or not isinstance(outcomes, dict)
            or set(outcomes) != expected_outcomes
            or any(not _nonnegative_integer(value) for value in outcomes.values())
            or sum(outcomes.values()) != summary["count"]
        ):
            raise CounterexampleRefusal(
                "capsule_schema", "share-safe minimality summary is malformed"
            )

    preservation = capsule.get("preservation")
    expected_preservation = {
        "source_executions", "source_matching_failures", "final_executions",
        "final_matching_failures", "source_result_digests", "final_result_digests",
        "source_content_hashes", "final_content_hashes", "source_trace_digests",
        "final_trace_digests", "same_failure_fingerprint",
    }
    if not isinstance(preservation, dict) or set(preservation) != expected_preservation:
        raise CounterexampleRefusal("capsule_schema", "preservation evidence is malformed")
    for key in ("source_executions", "source_matching_failures", "final_executions", "final_matching_failures"):
        if preservation.get(key) != 2:
            raise CounterexampleRefusal("capsule_schema", f"preservation {key} must be two")
    for key in ("source_result_digests", "final_result_digests", "source_trace_digests", "final_trace_digests"):
        values = preservation.get(key)
        if not isinstance(values, list) or len(values) != 2 or not all(digest(value) for value in values):
            raise CounterexampleRefusal("capsule_schema", f"preservation {key} is malformed")
    for key in ("source_content_hashes", "final_content_hashes"):
        values = preservation.get(key)
        if not isinstance(values, list) or len(values) != 2 or not all(digest(value, prefixed=False) for value in values):
            raise CounterexampleRefusal("capsule_schema", f"preservation {key} is malformed")
    if preservation.get("same_failure_fingerprint") is not True:
        raise CounterexampleRefusal("capsule_schema", "failure-fingerprint preservation must be true")

    source = capsule.get("source")
    target = capsule.get("target")
    provenance = capsule.get("provenance")
    if profile == PRIVATE_PROFILE:
        if not isinstance(source, dict) or set(source) != {
            "scenario_file_sha256", "test_file_sha256", "scenario_digest", "test_digest",
        } or not all(digest(value) for value in source.values()):
            raise CounterexampleRefusal("capsule_schema", "private source references are malformed")
        target_keys = {
            "test_id", "assertion_digest", "assertion_id", "kind", "dimension",
            "authority", "required_status", "failure_atom",
            "source_failure_atoms", "fingerprint",
        }
        if not isinstance(target, dict) or set(target) != target_keys:
            raise CounterexampleRefusal("capsule_schema", "private target is malformed")
        if not all(isinstance(target.get(key), str) and target.get(key) for key in ("test_id", "assertion_id", "kind")):
            raise CounterexampleRefusal("capsule_schema", "private target identifiers are malformed")
        if target.get("dimension") is not None and not isinstance(target.get("dimension"), str):
            raise CounterexampleRefusal("capsule_schema", "private target dimension is malformed")
        if target.get("kind") not in _ASSERTION_KINDS or (
            target.get("dimension") is not None and (
                not isinstance(target.get("dimension"), str)
                or target.get("dimension") not in _DIMENSIONS
            )
        ):
            raise CounterexampleRefusal("capsule_schema", "private target vocabulary is malformed")
        if target.get("authority") != "deterministic" or target.get("required_status") != "FAIL":
            raise CounterexampleRefusal("capsule_schema", "private target authority/status is malformed")
        if not digest(target.get("assertion_digest")) or not digest(target.get("fingerprint")):
            raise CounterexampleRefusal("capsule_schema", "private target proof fields are malformed")
        _validate_failure_atom(target["kind"], target.get("failure_atom"))
        source_atoms = target.get("source_failure_atoms")
        if (
            not isinstance(source_atoms, list)
            or not source_atoms
            or any(not isinstance(atom, dict) for atom in source_atoms)
        ):
            raise CounterexampleRefusal(
                "capsule_schema", "source failure atoms are malformed"
            )
        for atom in source_atoms:
            _validate_failure_atom(target["kind"], atom)
        atom_keys = [failure_atom_sort_key(atom) for atom in source_atoms]
        if (
            atom_keys != sorted(set(atom_keys))
            or target["failure_atom"] != source_atoms[0]
        ):
            raise CounterexampleRefusal(
                "capsule_schema", "source failure atoms are not sorted and anchored"
            )
        if failure_identity_digest(target) != target["fingerprint"]:
            raise CounterexampleRefusal("capsule_schema", "private target fingerprint is inconsistent")
        if not isinstance(provenance, dict) or set(provenance) != {
            "hotato_version", "evaluator_digest", "seed", "scenario_selection",
        }:
            raise CounterexampleRefusal("capsule_schema", "private provenance is malformed")
        seed = provenance.get("seed")
        hotato_version = provenance.get("hotato_version")
        if not isinstance(hotato_version, str) or not _VERSION_STRING.fullmatch(
            hotato_version
        ) or not digest(provenance.get("evaluator_digest")) or isinstance(seed, bool) or not isinstance(seed, int) or seed < 0:
            raise CounterexampleRefusal("capsule_schema", "private provenance values are malformed")
        selection = provenance.get("scenario_selection")
        if (
            not isinstance(selection, dict)
            or set(selection) != {"mode", "seed", "variation_matrix_applied"}
            or selection.get("mode") != "base-scenario"
            or isinstance(selection.get("seed"), bool)
            or not isinstance(selection.get("seed"), int)
            or selection.get("seed") != seed
            or selection.get("variation_matrix_applied") is not False
        ):
            raise CounterexampleRefusal("capsule_schema", "private scenario selection is malformed")
        if set(privacy) != {"profile", "content_included", "network_egress", "hashes_are_correlators"}:
            raise CounterexampleRefusal("capsule_schema", "private privacy declaration is malformed")
        if privacy.get("network_egress") is not False or privacy.get("hashes_are_correlators") is not True:
            raise CounterexampleRefusal("capsule_schema", "private privacy declaration is inconsistent")
        if privacy.get("content_included") != _PRIVATE_CONTENT:
            raise CounterexampleRefusal("capsule_schema", "private content inventory is inconsistent")
        oracle_ref = capsule.get("oracle")
        if not isinstance(oracle_ref, dict) or set(oracle_ref) != {
            "path", "digest",
        } or oracle_ref.get("path") != "oracle.json" or not digest(oracle_ref.get("digest")):
            raise CounterexampleRefusal("capsule_schema", "private oracle reference is malformed")
        artifact_values = capsule.get("artifacts")
        if isinstance(artifact_values, dict):
            for relative in artifact_values.values():
                if isinstance(relative, str) and (
                    os.path.isabs(relative)
                    or posixpath.isabs(relative)
                    or "\\" in relative
                    or posixpath.normpath(relative) == ".."
                    or posixpath.normpath(relative).startswith("../")
                ):
                    raise CounterexampleRefusal(
                        "unsafe_member", f"bundle member {relative!r} escapes the capsule"
                    )
        if artifact_values != _ARTIFACTS:
            raise CounterexampleRefusal("capsule_schema", "private artifact inventory is malformed")
        artifact_digests = capsule.get("artifact_digests")
        if not isinstance(artifact_digests, dict) or set(artifact_digests) != set(
            _ARTIFACTS
        ) or not all(digest(value) for value in artifact_digests.values()):
            raise CounterexampleRefusal("capsule_schema", "private artifact digests are malformed")
    else:
        if not isinstance(source, dict) or set(source) != {"scenario_digest", "test_digest"} or not all(digest(value) for value in source.values()):
            raise CounterexampleRefusal("capsule_schema", "share-safe source references are malformed")
        target_keys = {
            "assertion_ref", "kind", "dimension", "authority", "required_status",
            "fingerprint", "failure_atom_digest", "failure_code",
        }
        if not isinstance(target, dict) or set(target) != target_keys:
            raise CounterexampleRefusal("capsule_schema", "share-safe target is malformed")
        if not isinstance(target.get("kind"), str) or target.get("kind") not in _ASSERTION_KINDS or (
            target.get("dimension") is not None and (
                not isinstance(target.get("dimension"), str)
                or target.get("dimension") not in _DIMENSIONS
            )
        ) or not digest(target.get("assertion_ref")) or not digest(target.get("fingerprint")) or not digest(target.get("failure_atom_digest")):
            raise CounterexampleRefusal("capsule_schema", "share-safe target values are malformed")
        branches = FAILURE_ATOM_FIELDS.get(target["kind"], {})
        if (
            not isinstance(target.get("failure_code"), str)
            or target["failure_code"] not in branches
        ):
            raise CounterexampleRefusal(
                "capsule_schema", "share-safe failure code is malformed"
            )
        if target.get("authority") != "deterministic" or target.get("required_status") != "FAIL":
            raise CounterexampleRefusal("capsule_schema", "share-safe target authority/status is malformed")
        if not isinstance(provenance, dict) or set(provenance) != {
            "hotato_version", "evaluator_digest", "scenario_selection",
        }:
            raise CounterexampleRefusal("capsule_schema", "share-safe provenance is malformed")
        hotato_version = provenance.get("hotato_version")
        if not isinstance(hotato_version, str) or not _VERSION_STRING.fullmatch(
            hotato_version
        ) or not digest(provenance.get("evaluator_digest")):
            raise CounterexampleRefusal("capsule_schema", "share-safe provenance values are malformed")
        selection = provenance.get("scenario_selection")
        if (
            not isinstance(selection, dict)
            or set(selection) != {"mode", "seed", "variation_matrix_applied"}
            or selection.get("mode") != "base-scenario"
            or selection.get("variation_matrix_applied") is not False
        ):
            raise CounterexampleRefusal("capsule_schema", "share-safe scenario selection is malformed")
        selected_seed = selection.get("seed")
        if isinstance(selected_seed, bool) or not isinstance(selected_seed, int) or selected_seed < 0:
            raise CounterexampleRefusal("capsule_schema", "share-safe scenario seed is malformed")
        if set(privacy) != {"profile", "content_included", "omitted", "runnable", "hashes_are_correlators"}:
            raise CounterexampleRefusal("capsule_schema", "share-safe privacy declaration is malformed")
        if privacy.get("runnable") is not False or privacy.get("hashes_are_correlators") is not True:
            raise CounterexampleRefusal("capsule_schema", "share-safe privacy declaration is inconsistent")
        if privacy.get("content_included") != [] or privacy.get("omitted") != _SHARE_OMITTED:
            raise CounterexampleRefusal("capsule_schema", "share-safe content inventory is inconsistent")
    content_included = privacy.get("content_included")
    if not isinstance(content_included, list) or not all(isinstance(item, str) for item in content_included):
        raise CounterexampleRefusal("capsule_schema", "privacy content inventory is malformed")
    if capsule.get("counterexample_id") != _capsule_id(capsule):
        raise CounterexampleRefusal("counterexample_id_mismatch", "counterexample_id does not match capsule content")


def _load_private_bundle(
    path: str,
) -> Tuple[
    str,
    Dict[str, Any],
    Dict[str, Any],
    Dict[str, Any],
    Dict[str, Any],
    Dict[str, Any],
    Dict[str, Any],
]:
    root = os.path.realpath(os.path.abspath(path))
    if not os.path.isdir(root):
        raise CounterexampleRefusal("capsule_missing", f"counterexample {path!r} is not a directory")
    if os.path.islink(os.path.abspath(path)):
        raise CounterexampleRefusal("symlink_refused", "counterexample directory may not be a symlink")
    root_stat = os.lstat(root)
    root_identity = (root_stat.st_dev, root_stat.st_ino)
    verified_manifest = _verify_manifest(root)
    capsule = load_json(
        _bundle_member(root, "capsule.json"), max_bytes=MAX_CAPSULE_MEMBER_BYTES
    )
    _validate_capsule(capsule)
    if capsule.get("privacy", {}).get("profile") != PRIVATE_PROFILE:
        raise CounterexampleRefusal("not_runnable", "this projection is not a private runnable capsule")
    _validate_profile_inventory(verified_manifest, capsule)
    artifacts = capsule.get("artifacts")
    required_artifacts = set(_ARTIFACTS)
    if not isinstance(artifacts, dict) or set(artifacts) != required_artifacts:
        raise CounterexampleRefusal("capsule_schema", "private capsule artifact inventory is malformed")
    oracle_ref = capsule.get("oracle")
    if not isinstance(oracle_ref, dict) or set(oracle_ref) != {"path", "digest"}:
        raise CounterexampleRefusal("capsule_schema", "private capsule oracle reference is malformed")
    oracle_doc = load_json(
        _bundle_member(root, oracle_ref["path"]), max_bytes=MAX_CAPSULE_MEMBER_BYTES
    )
    _validate_oracle_document(oracle_doc, capsule["target"])
    if prefixed_digest(oracle_doc) != capsule["oracle"]["digest"]:
        raise CounterexampleRefusal("oracle_digest_mismatch", "oracle digest does not match capsule")
    source_scenario = load_json(_bundle_member(root, artifacts["source_scenario"]), max_bytes=MAX_CAPSULE_MEMBER_BYTES)
    source_test = load_json(_bundle_member(root, artifacts["source_conversation_test"]), max_bytes=MAX_CAPSULE_MEMBER_BYTES)
    scenario = load_json(_bundle_member(root, artifacts["scenario"]), max_bytes=MAX_CAPSULE_MEMBER_BYTES)
    test_doc = load_json(_bundle_member(root, artifacts["conversation_test"]), max_bytes=MAX_CAPSULE_MEMBER_BYTES)

    scenario_raw = read_regular_bytes(_bundle_member(root, artifacts["source_scenario_file"]))
    test_raw = read_regular_bytes(_bundle_member(root, artifacts["source_conversation_test_file"]))
    if "sha256:" + sha256_bytes(scenario_raw) != capsule.get("source", {}).get("scenario_file_sha256"):
        raise CounterexampleRefusal("source_digest_mismatch", "original scenario bytes do not match the capsule")
    if "sha256:" + sha256_bytes(test_raw) != capsule.get("source", {}).get("test_file_sha256"):
        raise CounterexampleRefusal("source_digest_mismatch", "original conversation-test bytes do not match the capsule")
    try:
        parsed_scenario = SC.validate_scenario_doc(SC.parse_scenario(scenario_raw.decode("utf-8")))
        parsed_test = CT.validate_conversation_test_doc(CT.parse_conversation_test(test_raw.decode("utf-8")))
    except (UnicodeDecodeError, ValueError) as exc:
        raise CounterexampleRefusal("source_parse_mismatch", f"stored source bytes no longer parse: {exc}") from exc
    if parsed_scenario != source_scenario or parsed_test != source_test:
        raise CounterexampleRefusal("source_parse_mismatch", "stored source bytes do not normalize to the frozen source")
    if prefixed_digest(source_scenario) != capsule.get("source", {}).get("scenario_digest"):
        raise CounterexampleRefusal("source_digest_mismatch", "frozen scenario digest does not match the capsule")
    if prefixed_digest(source_test) != capsule.get("source", {}).get("test_digest"):
        raise CounterexampleRefusal("source_digest_mismatch", "frozen conversation-test digest does not match the capsule")
    artifact_digests = capsule.get("artifact_digests")
    if not isinstance(artifact_digests, dict) or set(artifact_digests) != required_artifacts:
        raise CounterexampleRefusal("capsule_schema", "private capsule artifact digests are malformed")
    object_members = {
        "source_scenario": source_scenario,
        "source_conversation_test": source_test,
        "scenario": scenario,
        "conversation_test": test_doc,
        "expected_result": load_json(_bundle_member(root, artifacts["expected_result"]), max_bytes=MAX_CAPSULE_MEMBER_BYTES),
        "certificate": load_json(_bundle_member(root, artifacts["certificate"]), max_bytes=MAX_CAPSULE_MEMBER_BYTES),
        "minimality": load_json(_bundle_member(root, artifacts["minimality"]), max_bytes=MAX_CAPSULE_MEMBER_BYTES),
    }
    for name, value in object_members.items():
        if artifact_digests.get(name) != prefixed_digest(value):
            raise CounterexampleRefusal("artifact_digest_mismatch", f"artifact digest mismatch for {name}")
    byte_members = {
        "source_scenario_file": scenario_raw,
        "source_conversation_test_file": test_raw,
        "journal": read_regular_bytes(_bundle_member(root, artifacts["journal"]), max_bytes=MAX_CAPSULE_MEMBER_BYTES),
    }
    for name in _DERIVED_ARTIFACTS:
        byte_members[name] = read_regular_bytes(_bundle_member(root, artifacts[name]), max_bytes=MAX_CAPSULE_MEMBER_BYTES)
    for name, value in byte_members.items():
        if artifact_digests.get(name) != "sha256:" + sha256_bytes(value):
            raise CounterexampleRefusal("artifact_digest_mismatch", f"artifact digest mismatch for {name}")
    for name in ("reproduce_script", "predicate_script"):
        member = _bundle_member(root, artifacts[name])
        if not os.lstat(member).st_mode & stat.S_IXUSR:
            raise CounterexampleRefusal(
                "artifact_mode_mismatch", f"private helper {artifacts[name]!r} is not executable"
            )
    final_stat = os.lstat(root)
    if (final_stat.st_dev, final_stat.st_ino) != root_identity:
        raise CounterexampleRefusal(
            "capsule_changed", "counterexample directory changed during verification"
        )
    if _verify_manifest(root) != verified_manifest:
        raise CounterexampleRefusal(
            "capsule_changed", "counterexample contents changed during verification"
        )
    return root, capsule, oracle_doc, source_scenario, source_test, scenario, test_doc


def _operation_shape(operation: Any, *, journal: bool = False) -> None:
    if not isinstance(operation, dict):
        raise CounterexampleRefusal("certificate_schema", "reducer operation is malformed")
    kind = operation.get("kind")
    if not isinstance(kind, str):
        raise CounterexampleRefusal(
            "certificate_schema", "reducer operation kind is malformed"
        )
    if kind == "remove-field":
        required = {"kind", "phase", "path"}
        valid = set(operation) == required
    elif kind == "remove-path-set":
        required = {"kind", "phase", "paths"}
        paths = operation.get("paths")
        valid = (
            set(operation) == required
            and isinstance(paths, list)
            and bool(paths)
            and len(paths) <= MAX_BUDGET
            and all(isinstance(path, str) and path for path in paths)
            and paths == sorted(set(paths))
        )
    elif kind in {"remove-single-unit", "verify-single-unit"}:
        if kind == "verify-single-unit" and not journal:
            valid = False
        else:
            required = {"kind", "phase", "path", "component"}
            valid = (
                set(operation) == required
                and isinstance(operation.get("component"), str)
                and operation.get("component") in _REDUCER_COMPONENTS
            )
    else:
        valid = False
    if not valid:
        raise CounterexampleRefusal("certificate_schema", "reducer operation is malformed")
    if not isinstance(operation.get("phase"), str) or not operation["phase"]:
        raise CounterexampleRefusal("certificate_schema", "reducer phase is malformed")
    if "path" in operation and (
        not isinstance(operation["path"], str) or not operation["path"]
    ):
        raise CounterexampleRefusal("certificate_schema", "reducer path is malformed")
    if kind == "remove-single-unit" and operation.get("phase") not in {
        "one-minimality", "minimality-proof-restart",
    }:
        raise CounterexampleRefusal("certificate_schema", "single-unit reducer phase is malformed")
    if kind == "verify-single-unit" and operation.get("phase") != "minimality-proof":
        raise CounterexampleRefusal("journal_schema", "single-unit verifier phase is malformed")


def _transform_path_text(path: Any) -> str:
    if not isinstance(path, list) or not path:
        raise CounterexampleRefusal("certificate_transform", "deletion path is malformed")
    rendered = ""
    for part in path:
        if isinstance(part, bool) or not isinstance(part, (str, int)):
            raise CounterexampleRefusal("certificate_transform", "deletion path is malformed")
        if isinstance(part, int):
            if part < 0:
                raise CounterexampleRefusal("certificate_transform", "deletion index is malformed")
            rendered += f"[{part}]"
        else:
            rendered += ("." if rendered else "") + part
    return rendered


def _operation_matches_transform(operation: Dict[str, Any], transform: Any) -> None:
    _operation_shape(operation)
    if not isinstance(transform, dict) or transform.get("kind") != "hotato.delete-only.v1":
        raise CounterexampleRefusal("certificate_transform", "deletion transform is malformed")
    rows = transform.get("operations")
    if not isinstance(rows, list) or not rows or len(rows) > MAX_BUDGET:
        raise CounterexampleRefusal("certificate_transform", "deletion transform is empty")
    paths = [_transform_path_text(row.get("path") if isinstance(row, dict) else None) for row in rows]
    if operation["kind"] in {"remove-field", "remove-single-unit"}:
        if len(paths) != 1 or paths[0] != operation["path"]:
            raise CounterexampleRefusal(
                "certificate_operation_mismatch",
                "claimed reducer operation does not match its replayed deletion",
            )
    elif sorted(paths) != operation["paths"]:
            raise CounterexampleRefusal(
                "certificate_operation_mismatch",
                "claimed deletion paths do not match the replayed transform",
            )


def _validate_certificate_document(certificate: Any) -> None:
    expected = {
        "kind", "version", "algorithm", "reducer_set",
        "source_scenario_digest", "final_scenario_digest", "source_test_digest",
        "final_test_digest", "failure_fingerprint", "accepted_steps",
        "journal_sha256", "budget", "candidate_evaluations", "cache_hits",
        "termination",
    }
    if not isinstance(certificate, dict) or set(certificate) != expected:
        raise CounterexampleRefusal("certificate_schema", "certificate fields are malformed")
    if (
        certificate.get("kind") != CERTIFICATE_KIND
        or isinstance(certificate.get("version"), bool)
        or not isinstance(certificate.get("version"), int)
        or certificate.get("version") != VERSION
        or certificate.get("algorithm") != ALGORITHM
        or certificate.get("reducer_set") != REDUCER_SET
        or not isinstance(certificate.get("termination"), str)
        or certificate.get("termination") not in _MINIMALITY_CLAIMS
    ):
        raise CounterexampleRefusal("certificate_schema", "certificate contract is unsupported")
    for name in (
        "source_scenario_digest", "final_scenario_digest", "source_test_digest",
        "final_test_digest", "failure_fingerprint", "journal_sha256",
    ):
        if not _valid_digest(certificate.get(name)):
            raise CounterexampleRefusal("certificate_schema", f"certificate {name} is malformed")
    if not _nonnegative_integer(certificate.get("budget"), maximum=MAX_BUDGET) or certificate[
        "budget"
    ] < 1:
        raise CounterexampleRefusal("certificate_schema", "certificate budget is malformed")
    if not _nonnegative_integer(
        certificate.get("candidate_evaluations"), maximum=MAX_BUDGET
    ) or certificate["candidate_evaluations"] > certificate["budget"]:
        raise CounterexampleRefusal("certificate_schema", "certificate evaluations are malformed")
    if not _nonnegative_integer(certificate.get("cache_hits")):
        raise CounterexampleRefusal("certificate_schema", "certificate cache count is malformed")
    steps = certificate.get("accepted_steps")
    if not isinstance(steps, list) or len(steps) > MAX_BUDGET:
        raise CounterexampleRefusal("certificate_schema", "certificate steps are malformed")
    for index, step in enumerate(steps):
        if not isinstance(step, dict) or set(step) != {
            "step", "parent_digest", "child_digest", "operation", "transform",
            "oracle_result_digest", "failure_atom_digest",
        }:
            raise CounterexampleRefusal("certificate_schema", "accepted step is malformed")
        if not _nonnegative_integer(step.get("step")) or step.get("step") != index + 1 or not _valid_digest(
            step.get("parent_digest"), prefixed=False
        ) or not _valid_digest(step.get("child_digest"), prefixed=False) or not _valid_digest(
            step.get("oracle_result_digest")
        ) or not _valid_digest(step.get("failure_atom_digest")):
            raise CounterexampleRefusal("certificate_schema", "accepted-step values are malformed")
        _operation_matches_transform(step["operation"], step["transform"])


def _validate_journal(
    journal: bytes, certificate: Dict[str, Any], reduction: Dict[str, Any]
) -> None:
    evaluated = 0
    cached = 0
    accepted = certificate["accepted_steps"]
    accepted_index = 0
    row_count = 0
    for number, raw_line in enumerate(io.BytesIO(journal), 1):
        if number > _MAX_JOURNAL_ROWS:
            raise CounterexampleRefusal(
                "journal_too_many_rows",
                f"reduction journal exceeds {_MAX_JOURNAL_ROWS} rows",
            )
        try:
            line = raw_line.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise CounterexampleRefusal(
                "journal_schema", "reduction journal is not UTF-8"
            ) from exc
        try:
            row = json.loads(line)
        except (json.JSONDecodeError, ValueError, RecursionError) as exc:
            raise CounterexampleRefusal(
                "journal_schema", f"reduction journal line {number} is not JSON"
            ) from exc
        assert_finite(row, f"reduction journal line {number}")
        if not isinstance(row, dict) or set(row) not in ({
            "attempt", "operation", "candidate_digest", "status", "code", "cached",
        }, {
            "attempt", "operation", "candidate_digest", "status", "code", "cached",
            "failure_atom_digest",
        }):
            raise CounterexampleRefusal("journal_schema", "reduction journal row is malformed")
        if (
            not _nonnegative_integer(row.get("attempt"))
            or row.get("attempt") != number
            or not _valid_digest(row.get("candidate_digest"), prefixed=False)
            or not isinstance(row.get("status"), str)
            or row.get("status") not in {"PRESERVED", "ABSENT", "DRIFTED", "UNRESOLVED"}
            or not isinstance(row.get("cached"), bool)
            or (row.get("code") is not None and not isinstance(row.get("code"), str))
            or (
                "failure_atom_digest" in row
                and not _valid_digest(row["failure_atom_digest"])
            )
        ):
            raise CounterexampleRefusal("journal_schema", "reduction journal values are malformed")
        _operation_shape(row["operation"], journal=True)
        if canonical_json(row).encode("utf-8") != raw_line:
            raise CounterexampleRefusal(
                "journal_schema", "reduction journal is not canonical JSONL"
            )
        row_count = number
        if row["cached"]:
            cached += 1
        elif row.get("code") != "budget_exhausted":
            evaluated += 1
        if row["status"] == "PRESERVED":
            if accepted_index >= len(accepted):
                raise CounterexampleRefusal(
                    "journal_chain_mismatch",
                    "a preserved journal candidate is absent from the accepted proof chain",
                )
            step = accepted[accepted_index]
            if (
                row["candidate_digest"] != step["child_digest"]
                or row["operation"] != step["operation"]
                or row.get("failure_atom_digest")
                != step["failure_atom_digest"]
            ):
                raise CounterexampleRefusal(
                    "journal_chain_mismatch",
                    "a preserved journal candidate is absent from the accepted proof chain",
                )
            accepted_index += 1
    if (
        row_count != reduction["attempts"]
        or evaluated != certificate["candidate_evaluations"]
        or cached != certificate["cache_hits"]
    ):
        raise CounterexampleRefusal("journal_stats_mismatch", "journal counts do not match proof statistics")
    if accepted_index != len(accepted):
        raise CounterexampleRefusal(
            "journal_chain_mismatch",
            "an accepted proof step is absent from the reduction journal",
        )


def _verify_certificate(
    root: str,
    capsule: Dict[str, Any],
    source_scenario: Dict[str, Any],
    source_test: Dict[str, Any],
    scenario: Dict[str, Any],
    test_doc: Dict[str, Any],
    oracle: Optional[FailureOracle],
    *,
    evaluate_steps: bool = True,
) -> None:
    certificate = load_json(
        _bundle_member(root, capsule["artifacts"]["certificate"]),
        max_bytes=MAX_CAPSULE_MEMBER_BYTES,
    )
    _validate_certificate_document(certificate)
    if certificate.get("source_scenario_digest") != prefixed_digest(source_scenario):
        raise CounterexampleRefusal("certificate_source_mismatch", "certificate source scenario digest mismatch")
    if certificate.get("source_test_digest") != prefixed_digest(source_test):
        raise CounterexampleRefusal("certificate_source_mismatch", "certificate source test digest mismatch")
    if certificate.get("final_scenario_digest") != prefixed_digest(scenario):
        raise CounterexampleRefusal("certificate_final_mismatch", "certificate final scenario digest mismatch")
    if certificate.get("final_test_digest") != prefixed_digest(test_doc):
        raise CounterexampleRefusal("certificate_final_mismatch", "certificate final test digest mismatch")
    journal = read_regular_bytes(
        _bundle_member(root, capsule["artifacts"]["journal"]),
        max_bytes=MAX_CAPSULE_MEMBER_BYTES,
    )
    if certificate.get("journal_sha256") != "sha256:" + sha256_bytes(journal):
        raise CounterexampleRefusal("journal_digest_mismatch", "reduction journal digest mismatch")
    if certificate.get("failure_fingerprint") != capsule.get("target", {}).get("fingerprint"):
        raise CounterexampleRefusal("certificate_target_mismatch", "certificate failure fingerprint mismatch")
    if certificate.get("termination") != capsule.get("minimality", {}).get("status"):
        raise CounterexampleRefusal("certificate_status_mismatch", "certificate minimality status mismatch")
    reduction = capsule.get("reduction") or {}
    for key in ("budget", "candidate_evaluations", "cache_hits"):
        if certificate.get(key) != reduction.get(key):
            raise CounterexampleRefusal("certificate_stats_mismatch", f"certificate {key} does not match capsule")
    steps = certificate.get("accepted_steps")
    if not isinstance(steps, list) or len(steps) != reduction.get("accepted"):
        raise CounterexampleRefusal("certificate_schema", "accepted-step count does not match capsule")
    _validate_journal(journal, certificate, reduction)
    previous = certificate.get("source_scenario_digest")
    current = copy.deepcopy(source_scenario)
    for index, step in enumerate(steps):
        if not isinstance(step, dict) or set(step) != {
            "step", "parent_digest", "child_digest", "operation", "transform",
            "oracle_result_digest", "failure_atom_digest",
        }:
            raise CounterexampleRefusal("certificate_schema", "accepted step is malformed")
        if not _nonnegative_integer(step.get("step")) or step.get("step") != index + 1 or "sha256:" + step.get("parent_digest", "") != previous:
            raise CounterexampleRefusal("certificate_chain", "accepted-step parent chain is broken")
        try:
            child = apply_deletion_transform(current, step.get("transform"))
        except (TypeError, ValueError, KeyError, IndexError) as exc:
            raise CounterexampleRefusal("certificate_transform", f"accepted transform {index + 1} is invalid: {exc}") from exc
        child_digest = prefixed_digest(child)
        if child_digest != "sha256:" + step.get("child_digest", ""):
            raise CounterexampleRefusal("certificate_chain", "replayed transform does not match its child digest")
        if evaluate_steps:
            if oracle is None:
                raise RuntimeError("step evaluation requires an oracle")
            observed = oracle.evaluate(child)
            if observed.get("status") != "PRESERVED":
                raise CounterexampleRefusal("certificate_oracle", "accepted transform no longer preserves the target failure")
            if observed.get("result_digest") != step.get("oracle_result_digest"):
                raise CounterexampleRefusal("certificate_oracle", "accepted transform result digest mismatch")
            if observed.get("failure_atom_digest") != step.get(
                "failure_atom_digest"
            ):
                raise CounterexampleRefusal(
                    "certificate_oracle",
                    "accepted transform failure-atom digest mismatch",
                )
        current = child
        previous = child_digest
    if steps and previous != certificate.get("final_scenario_digest"):
        raise CounterexampleRefusal("certificate_chain", "accepted-step chain does not reach the final scenario")
    if not steps and certificate.get("source_scenario_digest") != certificate.get("final_scenario_digest"):
        raise CounterexampleRefusal("certificate_chain", "empty accepted-step chain cannot change the scenario")
    if current != scenario:
        raise CounterexampleRefusal("certificate_chain", "replayed accepted-step chain does not equal the reduced scenario")


def _validate_target_binding(
    source_test: Dict[str, Any],
    assertion: Dict[str, Any],
    target: Dict[str, Any],
) -> None:
    """Bind historical proof identity fields to the embedded source contract."""
    expected = {
        "test_id": source_test["id"],
        "assertion_digest": prefixed_digest(assertion),
        "assertion_id": assertion["id"],
        "kind": assertion["kind"],
        "dimension": assertion.get("dimension"),
        "authority": "deterministic",
        "required_status": "FAIL",
    }
    observed = {key: target.get(key) for key in expected}
    if observed != expected:
        raise CounterexampleRefusal(
            "target_binding_mismatch",
            "capsule target identity does not match the embedded source assertion",
        )


def verify_counterexample(path: str) -> Dict[str, Any]:
    """Independently verify integrity, exact failure, replay, and minimality."""
    (
        root, capsule, oracle_doc, source_scenario, source_test, scenario, test_doc,
    ) = _load_private_bundle(path)
    if capsule.get("provenance", {}).get("hotato_version") != __version__:
        raise CounterexampleRefusal(
            "package_version_drift",
            "full proof verification requires the Hotato version recorded by the capsule",
        )
    _verify_derived_artifacts(root, capsule)
    current_evaluator = _evaluator_digest()
    source_evaluator = capsule.get("provenance", {}).get("evaluator_digest")
    if source_evaluator != current_evaluator:
        raise CounterexampleRefusal(
            "evaluator_drift",
            "full proof verification requires the evaluator implementation recorded by the capsule",
        )
    target = capsule["target"]
    assertion = target_assertion(source_test, target["assertion_id"])
    _validate_target_binding(source_test, assertion, target)
    if projected_test(source_test, assertion) != test_doc:
        raise CounterexampleRefusal("projected_test_mismatch", "reduced test is not the canonical one-assertion projection")
    oracle = FailureOracle(source_test, assertion, int(capsule["provenance"]["seed"]))
    source_first = oracle.freeze_source(source_scenario)
    source_second = oracle.evaluate(copy.deepcopy(source_scenario))
    if source_second.get("status") != "PRESERVED" or _evaluation_signature(source_first) != _evaluation_signature(source_second):
        raise CounterexampleRefusal("source_replay_drift", "independent source replays disagreed")
    if oracle.source_identity["fingerprint"] != target["fingerprint"]:
        return {
            "kind": "counterexample-verify",
            "exit_code": 1,
            "ok": False,
            "status": "failure_identity_absent",
            "counterexample_id": capsule["counterexample_id"],
        }
    if oracle_doc != oracle.oracle_document():
        raise CounterexampleRefusal("oracle_mismatch", "stored oracle is not the oracle derived from the frozen source")
    if capsule["minimality"]["frozen_components"] != sorted(oracle.frozen):
        raise CounterexampleRefusal(
            "minimality_scope_mismatch",
            "minimality scope does not match the recorded oracle scope",
        )
    if capsule["reduction"]["initial"] != _stats(source_scenario, source_first):
        raise CounterexampleRefusal(
            "reduction_stats_mismatch", "stored source reduction statistics are incorrect"
        )
    _verify_certificate(
        root, capsule, source_scenario, source_test, scenario, test_doc, oracle,
    )
    first = oracle.evaluate(scenario)
    second = oracle.evaluate(copy.deepcopy(scenario))
    if first.get("status") != "PRESERVED" or second.get("status") != "PRESERVED" or _evaluation_signature(first) != _evaluation_signature(second):
        raise CounterexampleRefusal("replay_drift", "independent final replays disagreed")
    if capsule["reduction"]["final"] != _stats(scenario, first):
        raise CounterexampleRefusal(
            "reduction_stats_mismatch", "stored final reduction statistics are incorrect"
        )
    if capsule.get("preservation") != _preservation_doc(source_first, source_second, first, second):
        raise CounterexampleRefusal("preservation_mismatch", "stored replay evidence does not match independent execution")
    expected = load_json(
        _bundle_member(root, capsule["artifacts"]["expected_result"]),
        max_bytes=MAX_CAPSULE_MEMBER_BYTES,
    )
    if prefixed_digest(expected) != first.get("result_digest"):
        raise CounterexampleRefusal("expected_result_mismatch", "expected assertion result does not match replay")

    stored_min = load_json(
        _bundle_member(root, capsule["artifacts"]["minimality"]),
        max_bytes=MAX_CAPSULE_MEMBER_BYTES,
    )
    if stored_min != capsule.get("minimality"):
        raise CounterexampleRefusal("minimality_mismatch", "minimality.json does not match capsule.json")
    minimality_status = capsule["minimality"]["status"]
    unit_checks = 0
    if minimality_status == "one_minimal":
        unit_count = len(enumerate_units(scenario, oracle.frozen))
        verifier = SearchState(max(1, unit_count + 1), oracle.evaluate)
        checks, preserved = verify_single_units(scenario, verifier, oracle.frozen)
        unit_checks = len(checks)
        if preserved:
            return {
                "kind": "counterexample-verify",
                "exit_code": 1,
                "ok": False,
                "status": "minimality_regressed",
                "preserved_deletions": preserved,
                "counterexample_id": capsule["counterexample_id"],
            }
        if stored_min["remaining_unit_checks"] != checks:
            raise CounterexampleRefusal(
                "minimality_evidence_mismatch",
                "stored single-unit evidence does not match independent verification",
            )
    elif minimality_status != "budget_exhausted":
        raise CounterexampleRefusal("minimality_schema", f"unknown minimality status {minimality_status!r}")

    return {
        "kind": "counterexample-verify",
        "exit_code": 0,
        "ok": True,
        "status": "verified",
        "counterexample_id": capsule["counterexample_id"],
        "failure_fingerprint": target["fingerprint"],
        "minimality": minimality_status,
        "single_unit_checks": unit_checks,
        "source_replays": 2,
        "final_replays": 2,
        "accepted_steps_replayed": capsule["reduction"]["accepted"],
        "evaluator_match": True,
        "output": root,
    }


def reproduce_counterexample(path: str) -> Dict[str, Any]:
    """Evaluate only the reduced fixture under the current evaluator.

    Unlike :func:`verify_counterexample`, this operation intentionally permits
    evaluator-version drift.  The capsule and deletion chain are still checked,
    but historical intermediate verdicts are not reasserted under changed code.
    This is the operation suitable for regression checks and ``git bisect`` of
    Hotato evaluator/scenario behavior.
    """
    (
        root, capsule, oracle_doc, source_scenario, source_test, scenario, test_doc,
    ) = _load_private_bundle(path)
    target = capsule["target"]
    assertion = target_assertion(source_test, target["assertion_id"])
    _validate_target_binding(source_test, assertion, target)
    if projected_test(source_test, assertion) != test_doc:
        raise CounterexampleRefusal("projected_test_mismatch", "reduced test is not the canonical one-assertion projection")
    if oracle_doc.get("target") != target:
        raise CounterexampleRefusal("oracle_mismatch", "stored oracle target does not match capsule target")
    scope = oracle_doc.get("observation_scope") or {}
    frozen = scope.get("frozen_components")
    if not isinstance(frozen, list) or not all(isinstance(item, str) for item in frozen):
        raise CounterexampleRefusal("oracle_schema", "oracle frozen_components is malformed")

    oracle = FailureOracle(source_test, assertion, int(capsule["provenance"]["seed"]))
    oracle.source_identity = copy.deepcopy(target)
    oracle.frozen = set(frozen)
    _verify_certificate(
        root, capsule, source_scenario, source_test, scenario, test_doc, None,
        evaluate_steps=False,
    )
    first = oracle.evaluate(scenario)
    second = oracle.evaluate(copy.deepcopy(scenario))
    if first.get("status") == "UNRESOLVED" or second.get("status") == "UNRESOLVED":
        raise CounterexampleRefusal("reproduction_unresolved", "the reduced fixture is inconclusive under the current evaluator")
    if first.get("status") != second.get("status") or _evaluation_signature(first) != _evaluation_signature(second):
        raise CounterexampleRefusal("reproduction_drift", "current-evaluator replays disagreed")
    current_evaluator = _evaluator_digest()
    evaluator_match = current_evaluator == capsule.get("provenance", {}).get("evaluator_digest")
    if first.get("status") == "DRIFTED":
        return {
            "kind": "counterexample-reproduce",
            "exit_code": 1,
            "ok": False,
            "status": "failure_identity_drifted",
            "counterexample_id": capsule["counterexample_id"],
            "failure_fingerprint": target["fingerprint"],
            "evaluator_match": evaluator_match,
            "output": root,
        }
    if first.get("status") != "PRESERVED":
        return {
            "kind": "counterexample-reproduce",
            "exit_code": 1,
            "ok": False,
            "status": "failure_identity_absent",
            "counterexample_id": capsule["counterexample_id"],
            "failure_fingerprint": target["fingerprint"],
            "evaluator_match": evaluator_match,
            "output": root,
        }
    return {
        "kind": "counterexample-reproduce",
        "exit_code": 0,
        "ok": True,
        "status": "failure_reproduced",
        "counterexample_id": capsule["counterexample_id"],
        "failure_fingerprint": target["fingerprint"],
        "evaluator_match": evaluator_match,
        "replays": 2,
        "output": root,
    }


def inspect_counterexample(path: str) -> Dict[str, Any]:
    absolute = os.path.abspath(path)
    if os.path.islink(absolute):
        raise CounterexampleRefusal(
            "symlink_refused", "counterexample directory may not be a symlink"
        )
    root = os.path.realpath(absolute)
    if not os.path.isdir(root):
        raise CounterexampleRefusal(
            "capsule_missing", f"counterexample {path!r} is not a directory"
        )
    verified_manifest = _verify_manifest(root)
    capsule = load_json(
        _bundle_member(root, "capsule.json"), max_bytes=MAX_CAPSULE_MEMBER_BYTES
    )
    _validate_capsule(capsule)
    _validate_profile_inventory(verified_manifest, capsule)
    if capsule["privacy"]["profile"] == SHARE_PROFILE:
        _verify_share_artifacts(root, capsule)
    else:
        (
            _private_root,
            bound_capsule,
            _oracle_doc,
            _source_scenario,
            source_test,
            _scenario,
            _test_doc,
        ) = _load_private_bundle(root)
        if bound_capsule != capsule:
            raise CounterexampleRefusal(
                "capsule_changed", "private capsule changed during inspection"
            )
        assertion = target_assertion(
            source_test, capsule["target"]["assertion_id"]
        )
        _validate_target_binding(source_test, assertion, capsule["target"])
        _verify_derived_artifacts(root, capsule)
    if _verify_manifest(root) != verified_manifest:
        raise CounterexampleRefusal(
            "capsule_changed", "counterexample contents changed during inspection"
        )
    return {
        "kind": "counterexample-inspect",
        "exit_code": 0,
        "counterexample_id": capsule["counterexample_id"],
        "profile": capsule["privacy"]["profile"],
        "target": capsule["target"],
        "reduction": capsule["reduction"],
        "minimality": capsule["minimality"],
        "preservation": capsule["preservation"],
        "output": root,
    }


def _share_capsule(private: Dict[str, Any]) -> Dict[str, Any]:
    target = private["target"]
    private_minimality = private["minimality"]
    outcome_counts = {
        outcome: sum(
            1
            for row in private_minimality["remaining_unit_checks"]
            if row["outcome"] == outcome
        )
        for outcome in ("PRESERVED", "ABSENT", "DRIFTED", "UNRESOLVED")
    }
    minimality = {
        "status": private_minimality["status"],
        "reducer_set": private_minimality["reducer_set"],
        "claim": private_minimality["claim"],
        "check_summary": {
            "count": len(private_minimality["remaining_unit_checks"]),
            "outcomes": outcome_counts,
        },
        "frozen_components": list(private_minimality["frozen_components"]),
    }
    projection: Dict[str, Any] = {
        "kind": KIND,
        "version": VERSION,
        "counterexample_id": "pending",
        "source": {
            "scenario_digest": private["source"]["scenario_digest"],
            "test_digest": private["source"]["test_digest"],
        },
        "target": {
            "assertion_ref": target["assertion_digest"],
            "kind": target["kind"],
            "dimension": target.get("dimension"),
            "authority": "deterministic",
            "required_status": "FAIL",
            "fingerprint": target["fingerprint"],
            "failure_atom_digest": prefixed_digest(target["failure_atom"]),
            "failure_code": target["failure_atom"]["code"],
        },
        "reduction": private["reduction"],
        "minimality": minimality,
        "preservation": private["preservation"],
        "privacy": {
            "profile": SHARE_PROFILE,
            "content_included": [],
            "omitted": list(_SHARE_OMITTED),
            "runnable": False,
            "hashes_are_correlators": True,
        },
        "provenance": {
            "hotato_version": private["provenance"]["hotato_version"],
            "evaluator_digest": private["provenance"]["evaluator_digest"],
            "scenario_selection": private["provenance"]["scenario_selection"],
        },
    }
    projection["counterexample_id"] = _capsule_id(projection)
    return projection


def export_counterexample(path: str, *, out_dir: str, profile: str = SHARE_PROFILE) -> Dict[str, Any]:
    if profile != SHARE_PROFILE:
        raise CounterexampleRefusal("export_profile_refused", f"only {SHARE_PROFILE!r} is supported")
    # Retain the exact validated projection before full proof verification.
    # A mutable source path must never be reopened after verification and used
    # as though it were the snapshot that was verified.
    (
        _root, private, _oracle, _source_scenario, _source_test, _scenario, _test,
    ) = _load_private_bundle(path)
    verified = verify_counterexample(path)
    if verified["exit_code"] != 0:
        raise CounterexampleRefusal("source_not_verified", "private capsule did not verify")
    if verified.get("counterexample_id") != private.get("counterexample_id"):
        raise CounterexampleRefusal(
            "source_changed", "private capsule changed while export verification was running"
        )
    projection = _share_capsule(private)
    output, parent = _safe_output_parent(out_dir)
    stage = tempfile.mkdtemp(prefix=".hotato-counterexample-share-", dir=parent)
    mode_private(stage)
    try:
        for relative, data in _share_artifact_bytes(projection).items():
            _write(os.path.join(stage, relative), data)
        _finalize_manifest(stage)
        _rename_no_replace(stage, output)
        stage = ""
    finally:
        if stage and os.path.isdir(stage):
            shutil.rmtree(stage)
    return {
        "kind": "counterexample-export",
        "exit_code": 0,
        "profile": SHARE_PROFILE,
        "counterexample_id": projection["counterexample_id"],
        "output": output,
        "runnable": False,
    }


def predicate_counterexample(path: str) -> int:
    """Map verifier semantics to `git bisect run`: bad=1, good=0, skip=125."""
    try:
        result = reproduce_counterexample(path)
    except (
        CounterexampleRefusal,
        ValueError,
        TypeError,
        AttributeError,
        OverflowError,
        OSError,
        RecursionError,
        MemoryError,
    ):
        return 125
    return 1 if result.get("exit_code") == 0 else 0
