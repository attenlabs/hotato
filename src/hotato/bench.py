"""``hotato bench run/verify``: the frozen, byte-reproducible turn-taking bench.

hotato bench is a versioned FREEZE of the scenario batteries this repository
already ships: the packaged 8-scenario battery (always installed) and the
tiered ``corpus/suites/`` batteries (silver / silver-defects / gold /
gold-defects) in a source checkout. Every battery is synthetic shaped noise
rendered deterministically from its own labelled timings (seed =
``sha256(scenario_id)``), so two renders are byte-identical on any machine and
the frozen set is pinned by a content hash over the exact scenario JSONs and
audio bytes the run consumes. See ``BENCH-SPEC.md`` (docs/BENCH-SPEC.md) for
the full protocol.

What a bench result reports, and all it reports: per-suite pass counts,
per-signal measurement-error distributions in milliseconds, and the four
``did_yield`` confusion cells. There is NO blended score and NO
``overall_score`` anywhere (the same invariant :func:`hotato.errors.
reject_overall_score` enforces across every other schema); collapsing the
error distribution and the confusion matrix into one figure would hide exactly
the missed-yield / false-yield trade-off the bench exists to surface.

Grading is RE-EXECUTION plus hash comparison, never a judge: ``bench verify``
resolves the suite a result pins, checks the local frozen battery against the
pinned suite content hash, re-runs the same scoring end to end, and compares
the recomputed result body to the stored one via their canonical-JSON sha256
addresses (the same canonical form :func:`hotato.manifest.canonical_json` /
:func:`hotato.attest.canonical_json` define). The embedded ``content_hash`` is
an integrity address that catches a result edited in place; the re-execution
is the check that the numbers still come out of the audio.

This module deliberately WRAPS the existing primitives instead of
reimplementing them:

* :func:`hotato.core.run_suite` produces the pass/fail verdicts (the standard
  envelope, verbatim);
* :func:`hotato.benchmark.run_benchmark` /
  :func:`hotato.benchmark.load_set_from_dirs` /
  :func:`hotato.benchmark.load_bundled_set` produce the measurement-error
  distributions and the confusion cells;
* :func:`hotato.manifest.canonical_json` is the canonical byte form every
  content hash here is taken over.

Offline by construction: every input is a local file (or packaged data), and
nothing here touches the network.
"""

from __future__ import annotations

import hashlib
import json
import os
from importlib import resources
from typing import Any, Dict, List, Optional, Tuple

from . import benchmark as _benchmark
from .core import run_suite as _run_suite
from .errors import open_regular as _open_regular
from .errors import reject_overall_score as _reject_overall_score
from .manifest import canonical_json as _canonical_json

__all__ = [
    "KIND",
    "SCHEMA_VERSION",
    "BENCH_VERSION",
    "BUNDLED_SUITE",
    "available_suites",
    "resolve_suite",
    "suite_content_hash",
    "result_content_hash",
    "run_bench",
    "load_result",
    "verify_bench",
    "render_run_summary",
    "render_verify_text",
]

KIND = "hotato.bench-result"
SCHEMA_VERSION = "1"
# The bench protocol + frozen-set version (semver). Any change to the frozen
# scenario set, the audio suffix, the hashed body shape, or the hashing rules
# is a version bump here and in BENCH-SPEC.md.
BENCH_VERSION = "0.1"

BUNDLED_SUITE = "bundled"
AUDIO_SUFFIX = ".example.wav"

# The result body keys, in the order they are emitted. ``content_hash`` is the
# sha256 address of the canonical-JSON body and is never part of its own input.
_BODY_KEYS = (
    "tool", "kind", "schema_version", "bench_version", "suite", "engine",
    "config", "pass_counts", "error_stats_ms", "confusion",
    "confusion_off_diagonal",
)


# =========================================================================
# suite resolution: the packaged battery + the corpus/suites freeze
# =========================================================================

def _suites_root(suites_dir: Optional[str] = None) -> Optional[str]:
    """The ``corpus/suites`` tree, if one is reachable: an explicit
    ``suites_dir`` first, then package-relative (a source checkout), then the
    current working directory (an extracted sdist run from the tree root; the
    same fallback order :func:`hotato.benchmark._examples_root` uses).
    ``None`` when no tree carries a suites manifest."""
    candidates: List[str] = []
    if suites_dir:
        candidates.append(suites_dir)
    else:
        pkg_root = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
        candidates.append(os.path.join(pkg_root, "corpus", "suites"))
        candidates.append(os.path.join(os.getcwd(), "corpus", "suites"))
    for cand in candidates:
        if os.path.isfile(os.path.join(cand, "manifest.json")):
            return cand
    return None


def _suites_manifest(root: str) -> dict:
    with _open_regular(os.path.join(root, "manifest.json"), "r", encoding="utf-8") as fh:
        return json.load(fh)


def available_suites(suites_dir: Optional[str] = None) -> Dict[str, dict]:
    """Every suite battery runnable from here, as ``name -> entry``.

    Always includes :data:`BUNDLED_SUITE` (the packaged 8-scenario battery).
    When a ``corpus/suites`` tree is reachable, each suite its manifest lists
    whose ``scenarios/`` directory exists is included too, with its manifest
    tier. Entries carry ``scenarios_dir`` / ``audio_dir`` (``None`` for the
    packaged battery, which resolves through ``importlib.resources``)."""
    suites: Dict[str, dict] = {
        BUNDLED_SUITE: {
            "name": BUNDLED_SUITE,
            "tier": None,
            "source": "package",
            "scenarios_dir": None,
            "audio_dir": None,
        }
    }
    root = _suites_root(suites_dir)
    if root is None:
        return suites
    manifest = _suites_manifest(root)
    for info in manifest.get("suites", []):
        name = info.get("name")
        if not isinstance(name, str) or not name:
            continue
        scen_dir = os.path.join(root, info.get("path") or name, "scenarios")
        if not os.path.isdir(scen_dir):
            continue
        suites[name] = {
            "name": name,
            "tier": info.get("tier"),
            "source": "corpus/suites",
            "scenarios_dir": scen_dir,
            "audio_dir": os.path.join(root, info.get("path") or name, "audio"),
        }
    return suites


def resolve_suite(name: str, suites_dir: Optional[str] = None) -> dict:
    """The entry for suite ``name``, or a clean ``ValueError`` (the caller's
    exit-2 refuse path) naming every suite runnable from here."""
    suites = available_suites(suites_dir)
    entry = suites.get(name)
    if entry is None:
        raise ValueError(
            f"unknown bench suite {name!r}; runnable from here: "
            + ", ".join(sorted(suites))
        )
    if entry["source"] == "corpus/suites" and not os.path.isdir(entry["audio_dir"]):
        raise ValueError(
            f"suite {name!r} has no rendered audio at {entry['audio_dir']!r}; "
            "render it first: python3 corpus/suites/build_suites.py"
        )
    return entry


# =========================================================================
# content addressing
# =========================================================================

def _sha256_file(path: str) -> str:
    h = hashlib.sha256()
    with _open_regular(path) as fh:
        for chunk in iter(lambda: fh.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def _suite_file_digests(entry: dict) -> List[Tuple[str, str]]:
    """``(relative_name, sha256_hex)`` for every file the run consumes: each
    scenario JSON plus its ``<id>.example.wav`` recording, sorted by name.
    Exactly the files :func:`run_bench` scores; nothing else is pinned."""
    pairs: List[Tuple[str, str]] = []
    if entry["source"] == "package":
        scen_root = resources.files("hotato").joinpath("data", "scenarios")
        audio_root = resources.files("hotato").joinpath("data", "audio")
        for item in sorted(scen_root.iterdir(), key=lambda p: p.name):
            if not item.name.endswith(".json") or item.name == "manifest.json":
                continue
            # open-ok: bundled importlib resource (installed package data, not a user path)
            data = item.read_bytes()
            pairs.append((f"scenarios/{item.name}", hashlib.sha256(data).hexdigest()))
            sid = json.loads(data.decode("utf-8"))["id"]
            wav = audio_root.joinpath(sid + AUDIO_SUFFIX)
            # open-ok: bundled importlib resource (installed package data, not a user path)
            wav_bytes = wav.read_bytes()
            pairs.append(
                (f"audio/{sid}{AUDIO_SUFFIX}", hashlib.sha256(wav_bytes).hexdigest())
            )
    else:
        scen_dir, audio_dir = entry["scenarios_dir"], entry["audio_dir"]
        for fname in sorted(os.listdir(scen_dir)):
            if not fname.endswith(".json") or fname == "manifest.json":
                continue
            path = os.path.join(scen_dir, fname)
            pairs.append((f"scenarios/{fname}", _sha256_file(path)))
            with _open_regular(path, "r", encoding="utf-8") as fh:
                sid = json.load(fh)["id"]
            wav = os.path.join(audio_dir, sid + AUDIO_SUFFIX)
            if not os.path.exists(wav):
                raise ValueError(
                    f"suite {entry['name']!r} is missing the recording "
                    f"{wav!r} for scenario {sid!r}; the frozen battery must be "
                    "complete before it can be hashed or run "
                    "(render it: python3 corpus/suites/build_suites.py)"
                )
            pairs.append((f"audio/{sid}{AUDIO_SUFFIX}", _sha256_file(wav)))
    return sorted(pairs)


def suite_content_hash(entry: dict) -> str:
    """The pin that freezes a battery: ``sha256:`` over one line per consumed
    file, ``<relative_name>\\0<file_sha256_hex>\\n``, sorted by name. Any change
    to any scenario JSON or any consumed recording moves this hash."""
    h = hashlib.sha256()
    for rel, digest in _suite_file_digests(entry):
        h.update(rel.encode("utf-8"))
        h.update(b"\0")
        h.update(digest.encode("ascii"))
        h.update(b"\n")
    return "sha256:" + h.hexdigest()


def result_content_hash(body: dict) -> str:
    """``sha256:`` over the canonical-JSON bytes of the result body (sorted
    keys, compact separators, ASCII, finite numbers only; the exact
    :func:`hotato.manifest.canonical_json` form). The ``content_hash`` field
    itself is never an input."""
    stripped = {k: v for k, v in body.items() if k != "content_hash"}
    return "sha256:" + hashlib.sha256(_canonical_json(stripped).encode("utf-8")).hexdigest()


# =========================================================================
# run
# =========================================================================

def _fixture_set(entry: dict) -> "_benchmark.FixtureSet":
    if entry["source"] == "package":
        return _benchmark.load_bundled_set()
    return _benchmark.load_set_from_dirs(
        entry["name"],
        entry["scenarios_dir"],
        entry["audio_dir"],
        note="frozen corpus/suites battery (synthetic; deterministic render)",
        suffix=AUDIO_SUFFIX,
    )


def run_bench(name: str, *, suites_dir: Optional[str] = None) -> dict:
    """Run suite ``name`` end to end and return the bench result.

    Two existing measurements, side by side and never blended: the standard
    envelope's pass/fail verdicts (:func:`hotato.core.run_suite`, under the
    default shipped ``ScoreConfig``) and the measurement-error report
    (:func:`hotato.benchmark.run_benchmark`: per-signal ms-error distributions
    plus the ``did_yield`` confusion cells). The result embeds the suite's
    content hash (the freeze pin) and its own canonical content hash, so
    ``bench verify`` can re-execute and hash-compare later."""
    entry = resolve_suite(name, suites_dir)
    pinned = suite_content_hash(entry)

    if entry["source"] == "package":
        env = _run_suite()
    else:
        env = _run_suite(
            scenarios_dir=entry["scenarios_dir"],
            audio_dir=entry["audio_dir"],
            suffix=AUDIO_SUFFIX,
        )
    report = _benchmark.run_benchmark([_fixture_set(entry)])

    summary = env["summary"]
    body = {
        "tool": "hotato",
        "kind": KIND,
        "schema_version": SCHEMA_VERSION,
        "bench_version": BENCH_VERSION,
        "suite": {
            "name": entry["name"],
            "tier": entry["tier"],
            "source": entry["source"],
            "scenarios": summary["events"],
            "content_hash": pinned,
        },
        "engine": env["engine"],
        "config": report["config"],
        "pass_counts": {
            "scenarios": summary["events"],
            "passed": summary["passed"],
            "failed": summary["failed"],
            "not_scorable": summary.get("not_scorable", 0),
        },
        "error_stats_ms": report["aggregate"]["error_stats_ms"],
        "confusion": report["aggregate"]["confusion"],
        "confusion_off_diagonal": report["aggregate"]["confusion_off_diagonal"],
    }
    result = {k: body[k] for k in _BODY_KEYS}
    result["content_hash"] = result_content_hash(body)
    return result


# =========================================================================
# verify: re-execute + hash-compare
# =========================================================================

def load_result(path: str) -> dict:
    """Read a bench result file. Malformed JSON or a non-object document is a
    clean ``ValueError`` (the caller's exit-2 refuse path)."""
    with _open_regular(path, "r", encoding="utf-8") as fh:
        try:
            doc = json.load(fh)
        except (json.JSONDecodeError, RecursionError) as exc:
            raise ValueError(f"{path!r} is not valid JSON: {exc}") from exc
    if not isinstance(doc, dict):
        raise ValueError(f"{path!r} is not a bench result (expected a JSON object)")
    return doc


def _reject_overall_score_deep(obj: Any, where: str) -> None:
    """The repo-wide no-``overall_score`` invariant, applied to every nested
    mapping of a result document (the top-level check other validators use,
    extended structurally because a bench result nests its sections)."""
    stack = [(obj, where)]
    while stack:
        cur, path = stack.pop()
        if isinstance(cur, dict):
            _reject_overall_score(cur, f"{path}: 'overall_score' is forbidden in a bench result")
            stack.extend((v, f"{path}.{k}") for k, v in cur.items())
        elif isinstance(cur, list):
            stack.extend((v, f"{path}[{i}]") for i, v in enumerate(cur))


def _check_result_shape(result: dict) -> None:
    if result.get("kind") != KIND:
        raise ValueError(
            f"not a bench result: expected kind {KIND!r}, got {result.get('kind')!r}"
        )
    if result.get("schema_version") != SCHEMA_VERSION:
        raise ValueError(
            f"unsupported bench result schema_version {result.get('schema_version')!r} "
            f"(this build reads {SCHEMA_VERSION!r})"
        )
    _reject_overall_score_deep(result, "bench result")
    suite = result.get("suite")
    if not isinstance(suite, dict) or not isinstance(suite.get("name"), str):
        raise ValueError("bench result carries no suite.name; nothing to re-execute")
    if not isinstance(result.get("content_hash"), str):
        raise ValueError("bench result carries no content_hash; refusing to verify")


def verify_bench(result: dict, *, suites_dir: Optional[str] = None) -> dict:
    """Re-execute the suite a result pins and hash-compare, byte for byte.

    Refusals (``ValueError``, the exit-2 path): a document that is not a bench
    result; a stored ``content_hash`` that does not match the stored body (the
    file was edited in place: tampered); a suite that is not runnable from
    here; or a local frozen battery whose content hash differs from the pinned
    one (re-execution would score different bytes, so the comparison is
    withheld rather than fabricated).

    Otherwise the suite is re-run through the SAME code path ``bench run``
    uses and the verdict is a hash comparison of the two canonical result
    bodies: ``verified`` is True exactly when the recomputed body's sha256
    address equals the stored one. ``mismatched_sections`` names the top-level
    sections that differ when it does not reproduce."""
    _check_result_shape(result)

    stored_hash = result["content_hash"]
    recomputed_stored = result_content_hash(result)
    if recomputed_stored != stored_hash:
        raise ValueError(
            "tampered bench result: its content_hash does not match its own "
            f"body (stored {stored_hash}, recomputed {recomputed_stored}); "
            "the file was edited after it was written"
        )

    name = result["suite"]["name"]
    entry = resolve_suite(name, suites_dir)
    local_pin = suite_content_hash(entry)
    pinned = result["suite"].get("content_hash")
    if pinned != local_pin:
        raise ValueError(
            f"suite {name!r} on this machine hashes to {local_pin}, but the "
            f"result pins {pinned}; the local frozen battery is not the one "
            "the result was measured on, so re-execution is withheld"
        )

    fresh = run_bench(name, suites_dir=suites_dir)
    verified = fresh["content_hash"] == stored_hash
    mismatched = [
        key for key in _BODY_KEYS if fresh.get(key) != result.get(key)
    ] if not verified else []
    return {
        "kind": "hotato.bench-verify",
        "schema_version": SCHEMA_VERSION,
        "suite": name,
        "scenarios": fresh["pass_counts"]["scenarios"],
        "verified": verified,
        "stored_hash": stored_hash,
        "recomputed_hash": fresh["content_hash"],
        "mismatched_sections": mismatched,
    }


# =========================================================================
# rendering (deterministic given the payload)
# =========================================================================

def render_run_summary(result: dict) -> str:
    s, pc = result["suite"], result["pass_counts"]
    tier = s["tier"] or "-"
    return (
        f"bench run: suite={s['name']} tier={tier} "
        f"scenarios={pc['scenarios']} passed={pc['passed']} failed={pc['failed']}\n"
        f"suite content hash: {s['content_hash']}\n"
        f"result content hash: {result['content_hash']}"
    )


def render_verify_text(verdict: dict) -> str:
    if verdict["verified"]:
        return (
            f"verified: re-executed suite {verdict['suite']!r} "
            f"({verdict['scenarios']} scenarios); the recomputed result hash "
            f"matches the stored one ({verdict['stored_hash']})"
        )
    sections = ", ".join(verdict["mismatched_sections"]) or "none"
    return (
        f"MISMATCH: re-executing suite {verdict['suite']!r} "
        f"({verdict['scenarios']} scenarios) produced {verdict['recomputed_hash']}, "
        f"but the result stores {verdict['stored_hash']}.\n"
        f"sections that differ: {sections}\n"
        "the result file is internally consistent, but this machine does not "
        "reproduce it (a different scorer version, config, or battery render)"
    )
