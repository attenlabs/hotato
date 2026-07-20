"""``hotato.simulate_pack``: the curated, seeded persona/scenario library.

A small index over a set of :mod:`hotato.scenario` (``scenario.v1``) documents
that ship INSIDE the package (under ``hotato/data/simulate/pack/``), so a bare
``pip install hotato`` can list and run the common voice-agent test cases with
no file authoring. Each entry is a real ``hotato.scenario.v1`` the existing
:mod:`hotato.simulate` renderer already understands; this module ADDS a curated
library and its lookup, never a new engine.

Every entry pins a fixed ``seed``: a fixed ``(scenario, seed)`` renders
byte-identical every run (:func:`hotato.simulate.render` content-hashes the
transcript), so a pack scenario is a stable, reusable fixture across machines
and CI runs. The pack scores nothing -- it is deterministic INPUT; scoring is
the SEPARATE assert layer's job over the produced ``origin=simulated``
conversation.

The manifest (``pack/manifest.json``) is the machine-readable index the CLI
reads for ``hotato simulate --list``; the scenario files sit next to it. This
module is the loader and is the ONE place that resolves a pack NAME to its
packaged scenario file.
"""

from __future__ import annotations

import json
from typing import Any, Dict, List, Optional

from . import scenario as _scn

__all__ = [
    "PACK_NAME",
    "PACK_DIR",
    "MANIFEST_FILENAME",
    "load_manifest",
    "list_entries",
    "names",
    "is_pack_name",
    "scenario_path",
    "load_scenario",
]

# The curated pack's stable id and the package-relative directory its files
# ship in (installed with the wheel; see pyproject ``package-data``).
PACK_NAME = "hotato-voice-personas"
PACK_DIR = ("data", "simulate", "pack")
MANIFEST_FILENAME = "manifest.json"


def _pack_resource(filename: str):
    """The importlib resource for a file inside the packaged pack directory.
    Mirrors :func:`hotato.scenario.example_scenario_path` -- the same posture the
    package uses for its other bundled data (installed package data, never a
    user-supplied path)."""
    from importlib import resources  # deferred: import cost at interpreter start

    return resources.files("hotato").joinpath(*PACK_DIR, filename)


def load_manifest() -> Dict[str, Any]:
    """Load and lightly validate the pack manifest (``pack/manifest.json``).

    Returns the parsed manifest ``{pack, version, description, scenarios: [...]}``.
    Each ``scenarios`` entry carries ``name``, ``file``, ``title``, ``category``,
    ``seed``, and a ``tests`` description. Raises ``ValueError`` on a malformed
    manifest -- a broken bundled index is a packaging defect, surfaced up front."""
    # open-ok: bundled importlib resource (installed package data, not a user path)
    text = _pack_resource(MANIFEST_FILENAME).read_text(encoding="utf-8")
    try:
        manifest = json.loads(text)
    except ValueError as exc:  # pragma: no cover - a shipped-file defect
        raise ValueError(f"pack manifest is not valid JSON: {exc}") from exc
    if not isinstance(manifest, dict):
        raise ValueError("pack manifest must be a mapping")
    entries = manifest.get("scenarios")
    if not isinstance(entries, list) or not entries:
        raise ValueError("pack manifest must carry a non-empty 'scenarios' list")
    seen: set = set()
    for i, entry in enumerate(entries):
        if not isinstance(entry, dict):
            raise ValueError(f"pack manifest scenarios[{i}] must be a mapping")
        name = entry.get("name")
        if not name or not isinstance(name, str):
            raise ValueError(f"pack manifest scenarios[{i}] is missing a string 'name'")
        if name in seen:
            raise ValueError(f"pack manifest has a duplicate scenario name {name!r}")
        seen.add(name)
        fname = entry.get("file")
        if not fname or not isinstance(fname, str):
            raise ValueError(f"pack entry {name!r} is missing a string 'file'")
    return manifest


def list_entries() -> List[Dict[str, Any]]:
    """The pack entries as a list of ``{name, file, title, category, seed,
    tests}`` mappings, sorted by ``name`` for a stable, byte-reproducible
    listing regardless of manifest order."""
    entries = list(load_manifest().get("scenarios") or [])
    return sorted(entries, key=lambda e: e["name"])


def names() -> List[str]:
    """The pack scenario names, sorted."""
    return [e["name"] for e in list_entries()]


def _entry_for(name: str) -> Optional[Dict[str, Any]]:
    for entry in load_manifest().get("scenarios") or []:
        if entry.get("name") == name:
            return entry
    return None


def is_pack_name(name: Any) -> bool:
    """True when ``name`` is one of the curated pack scenario names. Used by the
    CLI to decide whether a positional argument is a pack reference (resolved
    from packaged data) or a path to a user scenario file on disk. A path always
    wins: the CLI only consults the pack when the argument is not an existing
    file, so a local file never gets shadowed by a pack name."""
    if not isinstance(name, str) or not name:
        return False
    return _entry_for(name) is not None


def scenario_path(name: str) -> str:
    """Absolute path to the packaged ``scenario.v1`` file for pack ``name`` (the
    file shipped in the wheel under ``hotato/data/simulate/pack/``), so the CLI
    can feed it straight into the existing file-loading path. Raises
    ``ValueError`` naming the available entries when ``name`` is not in the
    pack."""
    entry = _entry_for(name)
    if entry is None:
        raise ValueError(
            f"unknown pack scenario {name!r}; available: {', '.join(names())}. "
            "List them with `hotato simulate --list`."
        )
    return str(_pack_resource(entry["file"]))


def load_scenario(name: str) -> Dict[str, Any]:
    """Load, parse, and validate the packaged scenario for pack ``name``,
    returning the normalized ``scenario.v1`` doc (via
    :func:`hotato.scenario.validate_scenario_doc`). Raises ``ValueError`` for an
    unknown name or a malformed bundled file."""
    entry = _entry_for(name)
    if entry is None:
        raise ValueError(
            f"unknown pack scenario {name!r}; available: {', '.join(names())}. "
            "List them with `hotato simulate --list`."
        )
    # open-ok: bundled importlib resource (installed package data, not a user path)
    text = _pack_resource(entry["file"]).read_text(encoding="utf-8")
    return _scn.validate_scenario_doc(_scn.parse_scenario(text))
