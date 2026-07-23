"""Pin-to-contract: the workspace server's one write action (spec R9).

``POST /calls/<subject>/pin`` turns ONE scored candidate moment from the
console sidecar into a portable ``.hotato`` failure contract by delegating to
the EXISTING fleet review machinery -- :meth:`hotato.fleet.api.FleetAPI.
ingest_recording` -> :meth:`~hotato.fleet.api.FleetAPI.discover` ->
:meth:`~hotato.fleet.api.FleetAPI.contract_from_candidate` -- so no contract-
minting logic lives in the server. That chain is the same one ``hotato fleet``
drives from the CLI: the trust preflight refuses unscorable input, the mint +
seal step refuses insufficient evidence (``ValueError``), and the label +
contract + candidate-status registry writes are one atomic transaction, so a
refusal on ANY step surfaces as an HTTP 4xx with its reason and never leaves a
partial artifact.

Fail-closed identity checks before delegating:

* the pin binds to the exact scored evidence log: the form carries the score
  record's ``evidence_sha256`` and a mismatch refuses (the sidecar was rebuilt
  since the page rendered);
* the recording is RE-scanned through ``discover`` and the chosen moment's
  onset must reproduce exactly -- a recording that changed on disk since
  scoring refuses rather than pinning a different moment.

Pinned contracts register in the fleet registry's ``contracts`` table for the
serve workspace under agent id :data:`PIN_AGENT_ID`; the ``/calls`` feed's
"contracts protecting this agent" count reads that same table.
"""
from __future__ import annotations

import os
from typing import Any, Dict, Optional

__all__ = ["PIN_AGENT_ID", "PIN_REVIEW_DEPTH", "PinRefused", "pin_candidate"]

# The registered agent identity for contracts pinned from the console: the
# production deployment this workspace's console watches (the production event
# schema itself carries no agent id).
PIN_AGENT_ID = "production"

# ``FleetAPI.discover`` surfaces the top five ranked moments -- the fleet
# review-queue depth. Pinning reaches exactly those moments.
PIN_REVIEW_DEPTH = 5

_EXPECTS = ("yield", "hold")


class PinRefused(Exception):
    """A refused pin: carries the HTTP status and the human-readable reason.

    Raised for every non-success outcome -- bad fields, an unknown call or
    candidate, unscorable/changed evidence, a mint refusal. No artifact exists
    when this is raised (the delegated machinery is atomic by construction).
    """

    def __init__(self, status: int, reason: str) -> None:
        super().__init__(reason)
        self.status = int(status)
        self.reason = reason


def _refuse(status: int, reason: str) -> "PinRefused":
    return PinRefused(status, reason)


def pin_candidate(
    *,
    home: str,
    workspace: str,
    production_db: Optional[str],
    subject: str,
    candidate: Optional[str],
    expect: Optional[str],
    evidence_sha256: Optional[str],
    rationale: Optional[str] = None,
    reviewer: Optional[str] = None,
) -> Dict[str, Any]:
    """Mint a portable contract from scored candidate moment ``candidate`` of
    call ``subject`` by delegating to the fleet label/contract machinery.

    Returns the pin-result model (the same dict the JSON mirror returns).
    Raises :class:`PinRefused` (-> HTTP 4xx, no artifact) on every refusal.
    """
    from ..fixture import _default_reviewer_principal
    from . import data as _data

    if str(expect or "").strip().lower() not in _EXPECTS:
        raise _refuse(400, "expect must be 'yield' or 'hold', got %r" % (expect,))
    expect = str(expect).strip().lower()
    try:
        index = int(str(candidate).strip())
    except (TypeError, ValueError):
        raise _refuse(400, "candidate must be the integer index of a ranked "
                           "candidate moment, got %r" % (candidate,)) from None
    if index < 0:
        raise _refuse(400, "candidate index must be >= 0, got %d" % index)

    if not production_db:
        raise _refuse(404, "no production evidence database is wired; there "
                           "are no scored calls to pin from")
    try:
        model = _data.build_call_detail(workspace, production_db, subject)
    except ValueError as exc:
        raise _refuse(400, str(exc)) from exc
    if model is None:
        raise _refuse(404, "no scored call %r in the console sidecar" % subject)
    score = model["score"]

    if score.get("state") != "SCORED":
        raise _refuse(409, "call %r is %s (%s); a pin needs a SCORED call's "
                           "candidate moment" % (subject, score.get("state"),
                                                 score.get("reason")))
    recorded_sha = score.get("evidence_sha256") or ""
    if (evidence_sha256 or "") != recorded_sha:
        raise _refuse(409, "the evidence log for call %r changed since this "
                           "score was rendered; reload the call and pin again"
                           % subject)
    candidates = score.get("candidates") or []
    if index >= len(candidates):
        raise _refuse(404, "call %r has %d ranked candidate moment(s); there "
                           "is no candidate #%d"
                           % (subject, len(candidates), index))
    if index >= PIN_REVIEW_DEPTH:
        raise _refuse(409, "pinning reaches the top %d ranked moments (the "
                           "fleet review-queue depth); candidate #%d ranks "
                           "below that" % (PIN_REVIEW_DEPTH, index))
    chosen = candidates[index]
    onset = chosen.get("onset_sec")
    if onset is None:
        onset = chosen.get("t_sec")

    audio = score.get("audio") or {}
    path = audio.get("path")
    if not path:
        raise _refuse(409, "this score record carries no audio reference; a "
                           "pin binds the recorded two-channel call")
    if not os.path.isfile(path):
        raise _refuse(409, "the recording %r named by the evidence is not a "
                           "readable file on this machine" % path)

    from ..fleet.api import FleetAPI

    api = FleetAPI(home=home)
    try:
        api.registry.ensure_workspace(workspace)
        try:
            ingested = api.ingest_recording(workspace, PIN_AGENT_ID, path)
            discovered = api.discover(workspace, PIN_AGENT_ID, path,
                                      recording_id=ingested["recording_id"])
        except (OSError, ValueError) as exc:
            raise _refuse(409, "the recording %r did not re-ingest cleanly: %s"
                               % (path, exc)) from exc
        if not discovered.get("scorable"):
            raise _refuse(409, "the recording is not scorable under the trust "
                               "preflight (%s); nothing was pinned"
                               % (discovered.get("recommendation"),))
        surfaced = discovered.get("candidates") or []
        if index >= len(surfaced):
            raise _refuse(409, "the recording's re-scan surfaced %d candidate "
                               "moment(s), not the %d this score recorded; "
                               "the recording changed since scoring -- rebuild "
                               "the sidecar (hotato serve --production-db DB "
                               "--rebuild-scores)" % (len(surfaced), index + 1))
        fleet_cand = surfaced[index]
        re_onset = fleet_cand.get("onset_sec")
        if (onset is None or re_onset is None
                or abs(float(re_onset) - float(onset)) > 1e-6):
            raise _refuse(409, "the recording no longer reproduces candidate "
                               "#%d at %ss; the recording changed since "
                               "scoring -- rebuild the sidecar (hotato serve "
                               "--production-db DB --rebuild-scores)"
                               % (index, onset))
        try:
            minted = api.contract_from_candidate(
                workspace, fleet_cand["candidate_id"],
                reviewer=(reviewer or "").strip() or _default_reviewer_principal(),
                decision=expect, rationale=(rationale or "").strip() or None)
        except ValueError as exc:
            raise _refuse(409, str(exc)) from exc
    finally:
        api.close()

    return {
        "view": "pin_result",
        "workspace": workspace,
        "subject": subject,
        "candidate": index,
        "candidate_kind": chosen.get("kind"),
        "onset_sec": onset,
        "expect": expect,
        "contract_id": minted["contract_id"],
        "label_id": minted["label_id"],
        "dir": minted["dir"],
        "agent_id": PIN_AGENT_ID,
        "delegated_to": "fleet.contract_from_candidate",
        "verify": "hotato contract verify %s" % minted["dir"],
    }
