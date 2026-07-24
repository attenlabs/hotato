"""``hotato pin <autopsy-ref>``: the graduation bridge from autopsy to CI.

An autopsy prints content-derived incident ids (``apx-<12hex>#<rank>``);
``hotato pin`` turns one of them into a portable ``.hotato`` failure
contract by delegating to the EXISTING contract machinery
(:func:`hotato.contract.create_contract` on the recording at the incident's
onset) -- no contract-minting logic lives here, exactly like the console's
pin route delegates to the fleet machinery.

Ref forms:

  apx-<12hex>#<rank>   one specific incident (the rank the autopsy printed)
  apx-<12hex>          the call's top critical incident

Resolution is offline, from the autopsy envelope ``hotato autopsy`` /
``hotato scan`` persisted (``autopsy-<id>.json`` under ``./hotato-output/``,
or ``--from DIR``). Fail-closed identity check before delegating: the
envelope names the source recording, and the CURRENT file's bytes must
still hash to the pinned autopsy id -- a recording that changed on disk
since the autopsy refuses rather than pinning a different moment.

The incident kind maps to the contract's expect decision the way the
console pin path labels a caught moment: a BARGE-IN or TALK-OVER incident
pins ``--expect yield`` (the caller held the floor; the agent's job was to
yield) unless the human passes ``--expect hold`` (the agent was right to
keep talking -- the human's call, exactly like the console form). The
other incident kinds carry no yield/hold floor-holding decision:

  * a mono-derived incident refuses -- a contract requires the two-channel
    deterministic path (caller and agent on separate channels);
  * DEAD AIR / LATENCY SPIKE are silence-timing measurements with no
    caller onset where the agent held or ceded the floor;
  * ECHO SUSPECTED is a caveat about leaked agent audio, not a turn-taking
    event.

Every refusal -- a malformed ref, an unknown id, a rank out of range, a
missing or changed source file, a mono-derived incident, a kind with no
decision to pin -- raises ``ValueError`` (CLI exit 2) with the reason and
leaves no artifact (``create_contract`` itself is atomic, so a downstream
refusal cannot leave a partial bundle either).
"""

from __future__ import annotations

import json
import os
import re
import shlex
from typing import Optional

from . import autopsy as _autopsy
from .errors import open_regular as _open_regular

__all__ = [
    "DEFAULT_FROM_DIR",
    "DEFAULT_OUT_DIR",
    "EXPECT_BY_SCAN_KIND",
    "parse_pin_ref",
    "run_pin",
    "render_text",
]

DEFAULT_FROM_DIR = _autopsy.OUT_DIR
DEFAULT_OUT_DIR = "contracts"

# scan candidate kind -> the default expect decision. The two overlap kinds
# are the floor-holding events the yield/hold contract vocabulary expresses;
# the caller held the floor, so the default label is yield -- the same
# default the console pin form leads with -- and --expect hold stays the
# human's override for a moment where the agent was right to keep talking.
EXPECT_BY_SCAN_KIND = {
    "overlap_while_agent_talking": "yield",
    "agent_start_during_caller": "yield",
}

_REF_RE = re.compile(r"^(apx-[0-9a-f]{12})(?:#([0-9]+))?$")

# Refusal reasons for the incident kinds that carry no yield/hold decision.
_UNPINNABLE_REASON = {
    "dead-air": (
        "a DEAD AIR incident is a silence-timing measurement with no caller "
        "onset where the agent held or ceded the floor, so it carries no "
        "yield/hold decision for a contract to pin. The pinnable incidents "
        "are BARGE-IN and TALK-OVER."
    ),
    "latency-spike": (
        "a LATENCY SPIKE incident is a silence-timing measurement with no "
        "caller onset where the agent held or ceded the floor, so it "
        "carries no yield/hold decision for a contract to pin. The "
        "pinnable incidents are BARGE-IN and TALK-OVER."
    ),
    "echo-suspected": (
        "an ECHO SUSPECTED incident is a caveat (the caller channel may be "
        "carrying the agent's own leaked audio), not a turn-taking event, "
        "so it carries no yield/hold decision for a contract to pin. The "
        "pinnable incidents are BARGE-IN and TALK-OVER."
    ),
}


def parse_pin_ref(ref: str):
    """Split an autopsy ref into ``(autopsy_id, rank_or_None)``. ``rank`` is
    1-based (the rank the autopsy output printed); ``None`` selects the
    call's top critical incident. A malformed ref raises ValueError."""
    m = _REF_RE.match((ref or "").strip())
    if not m:
        raise ValueError(
            f"{ref!r} is not an autopsy ref; use apx-<12hex>#<rank> for one "
            "incident or apx-<12hex> for the call's top critical incident "
            "(the ids hotato autopsy prints on its pin: line)"
        )
    rank = int(m.group(2)) if m.group(2) else None
    if rank is not None and rank < 1:
        raise ValueError(
            f"incident ranks start at 1 (got {ref!r}); #1 is the top-ranked "
            "incident, the same rank the autopsy output shows"
        )
    return m.group(1), rank


def _load_envelope(apx: str, from_dir: str) -> dict:
    env_path = os.path.join(from_dir, f"autopsy-{apx}.json")
    if not os.path.isfile(env_path):
        raise ValueError(
            f"unknown autopsy id {apx}: no stored envelope at {env_path!r}. "
            "Run hotato autopsy RECORDING (or hotato scan DIR) first, or "
            "pass --from DIR pointing at the output directory that holds "
            "its autopsy-<id>.json"
        )
    try:
        with _open_regular(env_path, "r", encoding="utf-8") as fh:
            doc = json.load(fh)
    except (OSError, ValueError) as exc:
        raise ValueError(
            f"the autopsy envelope {env_path!r} is not readable JSON "
            f"({exc}); re-run hotato autopsy on the recording to rewrite it"
        ) from exc
    if (not isinstance(doc, dict) or doc.get("kind") != "autopsy"
            or doc.get("id") != apx
            or not isinstance(doc.get("incidents"), list)):
        raise ValueError(
            f"{env_path!r} is not the autopsy envelope for {apx}; re-run "
            "hotato autopsy on the recording to rewrite it"
        )
    return doc


def _resolve_incident(doc: dict, apx: str, rank: Optional[int]) -> dict:
    incidents = doc["incidents"]
    if not incidents:
        raise ValueError(
            f"{apx} recorded 0 incidents; there is nothing to pin"
        )
    if rank is None:
        for inc in incidents:
            if inc.get("severity") == "CRITICAL":
                return inc
        raise ValueError(
            f"{apx} has no critical incidents; name one explicitly: "
            f"{apx}#1..#{len(incidents)}"
        )
    if rank > len(incidents):
        raise ValueError(
            f"incident rank {rank} is out of range: {apx} recorded "
            f"{len(incidents)} incident{'s' if len(incidents) != 1 else ''}, "
            f"numbered 1..{len(incidents)} in rank order"
        )
    return incidents[rank - 1]


def run_pin(
    ref: str,
    *,
    from_dir: str = DEFAULT_FROM_DIR,
    out_dir: str = DEFAULT_OUT_DIR,
    expect: Optional[str] = None,
    contract_id: Optional[str] = None,
    reviewer: Optional[str] = None,
    force: bool = False,
) -> dict:
    """Resolve ``ref`` against the stored autopsy envelope, re-check the
    source recording's bytes against the content-derived id, and delegate
    the mint to :func:`hotato.contract.create_contract` at the incident's
    onset. Returns the ``create_contract`` result plus a ``pin`` block and
    the ``prove`` command hint. Raises ``ValueError`` (CLI exit 2) on every
    refusal, leaving no artifact."""
    from . import contract as _contract

    apx, rank = parse_pin_ref(ref)
    doc = _load_envelope(apx, from_dir)
    inc = _resolve_incident(doc, apx, rank)
    rank = int(inc["rank"])

    if doc.get("mode") != "stereo" or not inc.get("scan_kind"):
        raise ValueError(
            f"{apx}#{rank} was measured best-effort on a mono (one-channel) "
            "recording; a contract requires the two-channel deterministic "
            "path (caller and agent on separate channels). Export a "
            "two-channel recording, re-run hotato autopsy, and pin from "
            "that output."
        )

    source_path = doc.get("source_path")
    if not source_path:
        raise ValueError(
            f"the envelope for {apx} carries no source path; re-run "
            "hotato autopsy on the recording to rewrite it, then pin again"
        )
    if not os.path.isfile(source_path):
        raise ValueError(
            f"the source recording {source_path!r} named by the {apx} "
            "envelope is not a file on this machine; restore it (or re-run "
            "hotato autopsy where the recording lives) and pin again"
        )
    if _autopsy.autopsy_id(source_path) != apx:
        raise ValueError(
            f"the recording {source_path!r} changed since the autopsy: its "
            f"bytes no longer hash to {apx}, so this pin would bind a "
            "different call. Re-run hotato autopsy on the recording and pin "
            "from the fresh output."
        )

    scan_kind = inc["scan_kind"]
    default_expect = EXPECT_BY_SCAN_KIND.get(scan_kind)
    if default_expect is None:
        raise ValueError(f"{apx}#{rank}: " + _UNPINNABLE_REASON[inc["kind_key"]])
    chosen = (str(expect).strip().lower() if expect else default_expect)
    if chosen not in ("yield", "hold"):
        raise ValueError(f"--expect must be 'yield' or 'hold', got {expect!r}")

    cid = contract_id or f"pin-{apx[4:]}-{rank}-{inc['kind_key']}"
    result = _contract.create_contract(
        stereo=source_path,
        onset_sec=float(inc["t_sec"]),
        expect=chosen,
        contract_id=cid,
        out_dir=out_dir,
        candidate_ref=f"{apx}#{rank}",
        candidate_kind=scan_kind,
        reviewer_principal=reviewer,
        force=force,
    )
    result["pin"] = {
        "ref": f"{apx}#{rank}",
        "autopsy_id": apx,
        "rank": rank,
        "incident_kind": inc["kind"],
        "scan_kind": scan_kind,
        "t_sec": inc["t_sec"],
        "source": doc.get("source"),
        "expect": chosen,
        "expect_source": "--expect" if expect else "incident kind",
    }
    result["prove"] = f"hotato prove --contracts {shlex.quote(out_dir)}"
    return result


def render_text(result: dict) -> str:
    from . import contract as _contract

    pin = result["pin"]
    lines = [
        f"hotato pin: {pin['ref']} -> {pin['incident_kind']} at "
        f"t={pin['t_sec']:.2f}s in {pin['source']}",
        f"  expect: {pin['expect']} "
        + ("(your --expect)" if pin["expect_source"] == "--expect"
           else "(from the incident kind; pass --expect yield|hold to "
                "decide yourself)"),
        _contract.render_create_text(result),
        "",
        "lock it in CI:",
        f"  {result['prove']}",
    ]
    return "\n".join(lines)
