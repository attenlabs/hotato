#!/usr/bin/env python3
"""Mint the intentionally-republished Voice Failure Atlas records from their
structured reproduction metadata.

Growth blocker #1 (public Atlas reproduction). Each record page displays a
self-contained command chain -- one ``contract create`` (its own), one
``contract verify`` (count=1), one ``record render`` -- generated from
``reproduction_metadata`` (see ``build_atlas.generate_repro_commands``). This
tool executes that chain in a fresh empty directory with the PACKAGED hotato
(no result injection, no version patch) and publishes the reproduced
``record_id`` as both the embedded ``failure_record.record_id`` and
``reproduction_metadata.expected_record_id``. A reader who runs the displayed
chain in an empty directory with the same packaged hotato lands on that id.

Because the failure-record content includes the packaged ``__version__`` (sealed
into provenance), every hotato release re-mints these records: the id a record
cites is the id the SHIPPING code produces, never a frozen id from an earlier
wheel that lacked the current projection.

The immediate-prior content address is retained under ``supersedes`` -- a
content-addressed record is history, never a silent rewrite. The prior id is
read from the record ON DISK (the current-HEAD ``failure_record.record_id``, or
a previously-recorded ``supersedes.record_id`` on a re-run), never a hardcoded
constant, so it always names the real content address this republish replaces.

Run from anywhere:

    python scripts/regen_atlas_records.py            # rewrite atlas/records/*.json
    python scripts/regen_atlas_records.py --check     # fail if any file would change
"""
from __future__ import annotations

import importlib.util
import json
import os
import sys
import tempfile
from collections import OrderedDict
from typing import Any, Dict

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(HERE)


def _load_build_atlas():
    spec = importlib.util.spec_from_file_location(
        "build_atlas", os.path.join(HERE, "build_atlas.py"))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


BA = _load_build_atlas()

# The reproduction recipe for each intentionally-republished record. Numeric
# CLI values are the literal display tokens so the generated command is
# byte-stable. hotato_version, bundle.digest, and expected_record_id are filled
# in from the packaged version / a live reproduction below -- never hand-set.
# The prior (superseded) record_id is NOT a constant here: it is read from the
# record on disk, so it always names the actual content address this republish
# supersedes.
RECIPES: Dict[str, Dict[str, Any]] = {
    "addressed-interruption-missed": {
        "bundle_committed_path": "examples/funnel-demo/audio/fd-01-missed-interruption.example.wav",
        "contract": {
            "id": "fd-01-missed-interruption",
            "expect": "yield",
            "onset": "2.00",
            "stack": "generic",
            "max_talk_over": "0.80",
            "max_time_to_yield": "0.70",
            "rationale": "Funnel-demo fixture: the caller takes the floor at 2.00s with an addressed, on-topic interruption; the agent is expected to yield.",
        },
        "selector": "fd-01-missed-interruption",
        "supersedes_reason": "The prior record_id was minted by an earlier hotato release; this record is re-minted by the shipping code so its displayed clean-directory chain -- one contract create, one verify (count=1), one render -- reproduces this record_id with the packaged hotato version. The earlier content address is retained as history.",
    },
    "addressed-backchannel-yielded": {
        "bundle_committed_path": "examples/funnel-demo/audio/fd-02-backchannel-yielded.example.wav",
        "contract": {
            "id": "fd-02-backchannel-yielded",
            "expect": "hold",
            "onset": "2.20",
            "stack": "generic",
            "rationale": "Funnel-demo fixture: the caller offers a bare addressed backchannel at 2.20s; the agent is expected to hold the floor.",
        },
        "selector": "fd-02-backchannel-yielded",
        "supersedes_reason": "The prior record_id was minted by an earlier hotato release; this record is re-minted by the shipping code so its displayed clean-directory chain -- one contract create, one verify (count=1), one render -- reproduces this record_id with the packaged hotato version. The earlier content address is retained as history.",
    },
}

REVIEWER = "hotato-examples"
_KEY_ORDER = [
    "kind", "version", "content_id", "pattern_class", "title", "summary",
    "stack", "origin", "recorded_date", "paired_with", "release",
    "routing_fixture", "interface_conformance", "behavioral_evidence",
    "failure_record", "reproduction_metadata", "supersedes",
    "evidence_provenance", "cli_transcript", "content_digest",
]


def _ordered(rec: Dict[str, Any]) -> "OrderedDict[str, Any]":
    out: "OrderedDict[str, Any]" = OrderedDict()
    for k in _KEY_ORDER:
        if k in rec:
            out[k] = rec[k]
    for k in rec:  # anything unforeseen keeps a stable trailing spot
        if k not in out:
            out[k] = rec[k]
    return out


def _prior_record_id(rec: Dict[str, Any]) -> str:
    """The immediate-prior content address this republish supersedes, read from
    the record ON DISK -- never a hardcoded constant. First regen: the record's
    current-HEAD ``failure_record.record_id``. Re-runs (after this record already
    carries ``supersedes``): the recorded prior id, so the value -- and the whole
    file -- stays byte-stable and ``--check`` is idempotent."""
    existing = (rec.get("supersedes") or {}).get("record_id")
    if existing:
        return existing
    return rec["failure_record"]["record_id"]


def regen_one(content_id: str, recipe: Dict[str, Any]) -> Dict[str, Any]:
    path = os.path.join(REPO, "atlas", "records", content_id + ".json")
    with open(path, encoding="utf-8") as fh:
        rec = json.load(fh)

    prior_record_id = _prior_record_id(rec)

    committed = recipe["bundle_committed_path"]
    digest = BA._bundle_digest(REPO, committed)
    if digest is None:
        raise SystemExit(f"{content_id}: bundle {committed!r} does not resolve")

    meta = {
        "hotato_version": BA.HOTATO_VERSION,
        "reviewer_principal": REVIEWER,
        "working_directory": ".",
        "bundle": {
            "committed_path": committed,
            "filename": os.path.basename(committed),
            "digest": digest,
        },
        "contract": dict(recipe["contract"]),
        "verify_output": "verify.json",
        "selector": recipe["selector"],
        "record_out": "record",
        "expected_record_id": "sha256:" + "0" * 64,  # placeholder, set below
    }
    rec["reproduction_metadata"] = meta

    with tempfile.TemporaryDirectory() as work:
        result = BA.reproduce_record_clean_room(rec, REPO, work)

    rendered_fr = result["failure_record"]
    record_id = result["record_id"]

    meta["expected_record_id"] = record_id
    rec["failure_record"] = rendered_fr
    rec["reproduction_metadata"] = meta
    rec["evidence_provenance"]["source_cli_commands"] = BA.generate_repro_commands(meta)
    rec["cli_transcript"] = [
        {"command": e["command"], "output": e["output"]} for e in result["transcript"]
    ]
    if prior_record_id != record_id:
        rec["supersedes"] = {
            "record_id": prior_record_id,
            "reason": recipe["supersedes_reason"],
        }
    else:  # nothing changed; drop any stale supersession
        rec.pop("supersedes", None)

    rec.pop("content_digest", None)
    rec = _ordered(rec)
    rec["content_digest"] = BA._canonical_digest(rec)
    return rec


def main(argv=None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    check = "--check" in argv
    changed = []
    for content_id, recipe in sorted(RECIPES.items()):
        new_rec = regen_one(content_id, recipe)
        path = os.path.join(REPO, "atlas", "records", content_id + ".json")
        serialized = json.dumps(new_rec, indent=2, ensure_ascii=False) + "\n"
        with open(path, encoding="utf-8") as fh:
            existing = fh.read()
        if existing != serialized:
            changed.append(content_id)
            if not check:
                with open(path, "w", encoding="utf-8") as fh:
                    fh.write(serialized)
        print(f"{content_id}: record_id {new_rec['failure_record']['record_id']}")
    if check and changed:
        print("atlas records are stale (run scripts/regen_atlas_records.py): "
              + ", ".join(changed), file=sys.stderr)
        return 1
    if not check:
        print(f"regenerated {len(RECIPES)} atlas records "
              f"({len(changed)} changed).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
