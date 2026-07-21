"""``hotato.assert_packs``: curated, deterministic assertion packs (assert.v1).

A small index over a set of ready-made ``assert.v1`` assertion bundles that ship
INSIDE the package (under ``hotato/data/assertions/packs/``), so a bare
``pip install hotato`` can run the common voice-agent compliance checks with no
authoring. Each pack is a set of assertions built ONLY from the deterministic,
offline, model-free kinds :mod:`hotato.assert_` already evaluates (``phrase``,
``pii``, ``order``, ...); this module ADDS a curated library and its lookup plus
a merge, never a new evaluator and never a new kind.

The manifest (``packs/manifest.json``) is the machine-readable index the CLI
reads for ``hotato assert packs``; each pack file sits next to it. This module
is the loader and the ONE place that resolves a pack NAME to its packaged
assertions and merges the named packs' assertions into an ``assert.v1`` run.

Merging is deterministic and byte-stable: pack assertions are appended in the
manifest-declared order, after any ``--assertions`` document's own assertions,
and a duplicate assertion ``id`` -- across two packs, or between a pack and the
``--assertions`` file -- is refused up front (``ValueError``, the caller's
exit-2 usage-error path) BEFORE anything is evaluated. Every check measures a
deterministic signal (a phrase present or absent, a detector hit, a phrase
ordering), never intent, and no pack makes a certification claim.
"""

from __future__ import annotations

import json
from typing import Any, Dict, List, Optional

from . import assert_ as _assert
from .errors import open_regular as _open_regular

__all__ = [
    "PACK_SET_NAME",
    "PACK_DIR",
    "MANIFEST_FILENAME",
    "load_manifest",
    "list_entries",
    "names",
    "is_pack_name",
    "pack_path",
    "load_pack",
    "pack_assertions",
    "merge_pack_assertions",
    "merge_into_doc",
    "load_and_merge",
]

# The curated pack set's stable id and the package-relative directory its files
# ship in (installed with the wheel; see pyproject ``package-data``).
PACK_SET_NAME = "hotato-compliance-packs"
PACK_DIR = ("data", "assertions", "packs")
MANIFEST_FILENAME = "manifest.json"


def _pack_resource(filename: str):
    """The importlib resource for a file inside the packaged packs directory.
    Mirrors :func:`hotato.simulate_pack._pack_resource` -- the same posture the
    package uses for its other bundled data (installed package data, never a
    user-supplied path)."""
    from importlib import resources  # deferred: import cost at interpreter start

    return resources.files("hotato").joinpath(*PACK_DIR, filename)


def load_manifest() -> Dict[str, Any]:
    """Load and lightly validate the packs manifest (``packs/manifest.json``).

    Returns the parsed manifest ``{pack_set, version, description, packs: [...]}``.
    Each ``packs`` entry carries ``name``, ``file``, ``title``, and
    ``description``. Raises ``ValueError`` on a malformed manifest -- a broken
    bundled index is a packaging defect, surfaced up front."""
    # open-ok: bundled importlib resource (installed package data, not a user path)
    text = _pack_resource(MANIFEST_FILENAME).read_text(encoding="utf-8")
    try:
        manifest = json.loads(text)
    except ValueError as exc:  # pragma: no cover - a shipped-file defect
        raise ValueError(f"assertion packs manifest is not valid JSON: {exc}") from exc
    if not isinstance(manifest, dict):
        raise ValueError("assertion packs manifest must be a mapping")
    entries = manifest.get("packs")
    if not isinstance(entries, list) or not entries:
        raise ValueError("assertion packs manifest must carry a non-empty 'packs' list")
    seen: set = set()
    for i, entry in enumerate(entries):
        if not isinstance(entry, dict):
            raise ValueError(f"assertion packs manifest packs[{i}] must be a mapping")
        name = entry.get("name")
        if not name or not isinstance(name, str):
            raise ValueError(
                f"assertion packs manifest packs[{i}] is missing a string 'name'"
            )
        if name in seen:
            raise ValueError(
                f"assertion packs manifest has a duplicate pack name {name!r}"
            )
        seen.add(name)
        fname = entry.get("file")
        if not fname or not isinstance(fname, str):
            raise ValueError(f"assertion pack {name!r} is missing a string 'file'")
    return manifest


def list_entries() -> List[Dict[str, Any]]:
    """The pack entries as a list of ``{name, title, description, file,
    assertion_ids, kinds}`` mappings, sorted by ``name`` for a stable,
    byte-reproducible listing regardless of manifest order. ``assertion_ids``
    and ``kinds`` are read from each pack file so a caller can see what a pack
    checks without loading it."""
    out: List[Dict[str, Any]] = []
    for entry in load_manifest().get("packs") or []:
        assertions = pack_assertions(entry["name"])
        out.append(
            {
                "name": entry["name"],
                "title": entry.get("title"),
                "description": entry.get("description"),
                "file": entry.get("file"),
                "assertion_ids": [a["id"] for a in assertions],
                "kinds": sorted({a["kind"] for a in assertions}),
            }
        )
    return sorted(out, key=lambda e: e["name"])


def names() -> List[str]:
    """The pack names, sorted."""
    return sorted(entry["name"] for entry in load_manifest().get("packs") or [])


def _entry_for(name: str) -> Optional[Dict[str, Any]]:
    for entry in load_manifest().get("packs") or []:
        if entry.get("name") == name:
            return entry
    return None


def is_pack_name(name: Any) -> bool:
    """True when ``name`` is one of the curated pack names."""
    if not isinstance(name, str) or not name:
        return False
    return _entry_for(name) is not None


def pack_path(name: str) -> str:
    """Absolute path to the packaged pack file for ``name`` (shipped in the wheel
    under ``hotato/data/assertions/packs/``). Raises ``ValueError`` naming the
    available packs when ``name`` is not a pack."""
    entry = _entry_for(name)
    if entry is None:
        raise ValueError(
            f"unknown assertion pack {name!r}; available: {', '.join(names())}. "
            "List them with `hotato assert packs`."
        )
    return str(_pack_resource(entry["file"]))


def load_pack(name: str) -> Dict[str, Any]:
    """Load and parse the packaged pack file for ``name``, returning its raw
    ``{name, version, title, description, assertions: [...]}`` mapping. Raises
    ``ValueError`` for an unknown name or a malformed bundled file. The
    assertion SHAPE is validated by :func:`hotato.assert_.validate_assertions_doc`
    at merge/run time, exactly like a hand-authored file."""
    entry = _entry_for(name)
    if entry is None:
        raise ValueError(
            f"unknown assertion pack {name!r}; available: {', '.join(names())}. "
            "List them with `hotato assert packs`."
        )
    # open-ok: bundled importlib resource (installed package data, not a user path)
    text = _pack_resource(entry["file"]).read_text(encoding="utf-8")
    try:
        doc = json.loads(text)
    except ValueError as exc:  # pragma: no cover - a shipped-file defect
        raise ValueError(f"assertion pack {name!r} is not valid JSON: {exc}") from exc
    if not isinstance(doc, dict) or not isinstance(doc.get("assertions"), list) or not doc["assertions"]:
        raise ValueError(
            f"assertion pack {name!r} must be a mapping with a non-empty "
            "'assertions' list"
        )
    return doc


def pack_assertions(name: str) -> List[Dict[str, Any]]:
    """The list of assertion dicts a pack ships, in the pack's own order. Each
    entry is a plain ``assert.v1`` assertion mapping (id/kind/...), the same
    shape a ``--assertions`` document carries."""
    return list(load_pack(name).get("assertions") or [])


def merge_pack_assertions(
    base_assertions: List[Dict[str, Any]], pack_names: List[str]
) -> List[Dict[str, Any]]:
    """Return ``base_assertions`` followed by every named pack's assertions, in
    pack order. Refuses a duplicate assertion ``id`` -- between two packs, or
    between a pack and the base document -- with a ``ValueError`` (the caller's
    exit-2 usage-error path) BEFORE anything is evaluated, naming the id and the
    two sources that collided."""
    merged: List[Dict[str, Any]] = list(base_assertions)
    source: Dict[str, str] = {}
    for a in merged:
        if isinstance(a, dict) and isinstance(a.get("id"), str):
            source.setdefault(a["id"], "the --assertions document")
    for name in pack_names:
        for a in pack_assertions(name):
            aid = a.get("id")
            if isinstance(aid, str) and aid in source:
                raise ValueError(
                    f"duplicate assertion id {aid!r}: pack {name!r} collides with "
                    f"{source[aid]}"
                )
            if isinstance(aid, str):
                source[aid] = f"pack {name!r}"
            merged.append(dict(a))
    return merged


def merge_into_doc(
    doc: Optional[Dict[str, Any]], pack_names: List[str]
) -> Optional[Dict[str, Any]]:
    """Merge the named packs' assertions into an optional ``--assertions``
    document, returning one ``assert.v1`` document ready for
    :func:`hotato.assert_.run_assertions`.

    ``doc`` is the parsed ``--assertions`` file (or ``None`` when only packs are
    used); ``pack_names`` is the (possibly repeated) list of ``--pack`` names in
    invocation order. When ``pack_names`` is empty ``doc`` is returned unchanged
    (byte-identical to a pack-free run). With ``doc`` absent, a fresh
    ``{version, assertions}`` base is created and the packs supply every
    assertion. A duplicate id is refused up front (see
    :func:`merge_pack_assertions`); the merged document's own validation
    (unknown kind, bad field, duplicate id) still runs inside
    ``run_assertions``."""
    if not pack_names:
        return doc
    if doc is None:
        base: Dict[str, Any] = {"version": _assert.SUPPORTED_DOC_VERSION, "assertions": []}
    elif isinstance(doc, dict) and isinstance(doc.get("assertions"), list):
        base = dict(doc)
    else:
        raise ValueError(
            "cannot merge assertion packs: the --assertions document must be a "
            "mapping with an 'assertions' list"
        )
    base["assertions"] = merge_pack_assertions(
        list(base.get("assertions") or []), list(pack_names)
    )
    if "version" not in base:
        base["version"] = _assert.SUPPORTED_DOC_VERSION
    return base


def load_and_merge(
    assertions_path: Optional[str], pack_names: List[str]
) -> Optional[Dict[str, Any]]:
    """Parse an optional ``--assertions`` file (guarded by
    :func:`hotato.errors.open_regular`, so a FIFO path raises instead of
    blocking) and merge the named packs' assertions into it, returning one
    ``assert.v1`` document. See :func:`merge_into_doc` for the merge rules."""
    doc: Optional[Dict[str, Any]] = None
    if assertions_path is not None:
        with _open_regular(assertions_path, "r", encoding="utf-8") as fh:
            text = fh.read()
        doc = _assert.parse_assertions_yaml(text)
    return merge_into_doc(doc, list(pack_names or []))
