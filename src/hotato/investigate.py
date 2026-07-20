"""``hotato investigate``: one recording (a local WAV you hand it, or a live
pull from a connected stack by call id) -> ranked candidate turn-taking
moments, an honestly-authenticated capture origin, the K6 verdict-eligibility
gate, and -- for every candidate -- the EXACT next command that turns it into
a signed, CI-ready contract.

This is discovery + guidance, nothing more: ``hotato investigate`` never
labels intent (energy is not intent) and never mints a label itself. The
human keeps the one decision that matters -- which candidate is a real bug,
and whether it should have yielded or held -- by running the command this
tool prints: ``hotato investigate label <candidate_ref> --expect yield|hold``.

Every step reuses an existing, shipped primitive; nothing here reimplements
one:

  * audio in            -- ``hotato.capture`` (``fetch_one``/``resolve_creds``,
                            the SAME per-stack fetch ``hotato pull`` uses), or
                            the local file the operator handed us directly.
  * capture-origin note  -- authenticated from what we actually know: a
                            previously-frozen fixture clip, a fetch from the
                            stack's own API for a named call id, or an
                            operator-asserted local file. Never conflated with
                            ``hotato.receipt``'s signed, machine-verified
                            fresh-recapture tier (a stronger, distinct claim
                            this command never makes).
  * input health + K6    -- ``hotato.trust.trust_report`` (contract mode: the
                            same stricter bar ``contract create`` itself
                            checks, since this whole command is aimed at
                            producing a contract). A suspected channel swap or
                            crosstalk refuses the VERDICT path, never the
                            advisory candidates.
  * candidate discovery  -- ``hotato.scan.scan_recording`` (whole-call
                            candidate scanner; timing facts, never intent).
  * the label itself     -- ``hotato.contract.create_contract`` (which itself
                            calls ``fixture.create_fixture`` ->
                            ``labelrecord.mint_label_record``): a REAL signed
                            human label-record bound to the exact decoded
                            audio, when a signing key is configured. Never
                            fabricated here.

State is persisted to ``.hotato/investigate-state.json`` (mirroring
``loop.py``'s state-file precedent: schema-tagged, atomically written,
run-numbered, with a history log). Additively, the persisted file is written
in the SAME ``kind: "analyze"`` shape ``hotato analyze``/``hotato sweep``
already write, so it IS a valid ``FILE#N`` candidate ref: ``hotato fixture
promote`` and ``hotato contract create --from-candidate`` can read it
directly, with no second ref-resolution path anywhere.
"""

from __future__ import annotations

import json
import os
import re
import shlex
from datetime import datetime, timezone
from typing import Optional, Tuple

from ._engine.score import ScoreConfig
from .errors import open_regular as _open_regular

STATE_SCHEMA_ID = "hotato.investigate-state.v1"
DEFAULT_OUT_DIR = "contracts"

__all__ = [
    "STATE_SCHEMA_ID",
    "DEFAULT_OUT_DIR",
    "default_state_path",
    "load_state",
    "save_state",
    "run_investigate",
    "render_text",
    "run_investigate_label",
    "render_label_text",
    "label_result_json",
]


def default_state_path() -> str:
    """``.hotato/investigate-state.json`` under the current directory
    (project-local, git-ignorable), mirroring ``loop.default_state_path``.
    Override with ``--state PATH``."""
    return os.path.join(os.getcwd(), ".hotato", "investigate-state.json")


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def load_state(path: str) -> Optional[dict]:
    """Read a prior investigate-state file, or ``None`` if it does not exist.
    A malformed/foreign file raises ``ValueError`` (exit 2), never a silent
    reset -- mirrors ``loop.load_state``."""
    if not os.path.exists(path):
        return None
    try:
        with _open_regular(path, "r", encoding="utf-8") as fh:
            obj = json.load(fh)
    except (OSError, ValueError) as exc:
        raise ValueError(
            f"investigate state {path!r} is not readable JSON ({exc}). Fix "
            "or delete it and re-run hotato investigate."
        ) from exc
    if not isinstance(obj, dict) or obj.get("schema") != STATE_SCHEMA_ID:
        raise ValueError(
            f"{path!r} is not a hotato investigate-state file. Delete it and "
            "re-run."
        )
    run = obj.get("run", 0)
    if isinstance(run, bool) or not isinstance(run, int):
        raise ValueError(
            f"{path!r} has a corrupt 'run' field ({run!r}; expected an "
            "integer). Delete the investigate state and re-run."
        )
    if not isinstance(obj.get("history", []), list):
        raise ValueError(
            f"{path!r} has a corrupt 'history' field (expected a list). "
            "Delete the investigate state and re-run."
        )
    return obj


def save_state(path: str, state: dict) -> None:
    """Atomic write: a temp file in the same directory, then ``os.replace``
    (mirrors ``loop.save_state``): a crash mid-write never leaves a
    truncated state file in place."""
    d = os.path.dirname(path)
    if d:
        os.makedirs(d, exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(state, fh, indent=2)
        fh.write("\n")
    os.replace(tmp, path)


# --- capture origin: authenticate what we actually know, nothing more ------

def _sanitize_slug_part(text: str) -> str:
    s = re.sub(r"[^a-z0-9]+", "-", (text or "").lower()).strip("-")
    s = re.sub(r"-+", "-", s)
    return s or "x"


def _frozen_regression_scenario(path: str) -> Optional[str]:
    """If ``path`` is a fixture example WAV already written by
    ``hotato fixture create``/``promote`` (``DIR/audio/<id>.example.wav``
    with a sibling ``DIR/scenarios/<id>.json``), return that scenario path:
    this recording is a previously-frozen regression clip, not fresh
    evidence. ``None`` otherwise -- never guessed; the sibling file must
    actually exist on disk."""
    ap = os.path.abspath(path)
    parent = os.path.basename(os.path.dirname(ap))
    if parent != "audio":
        return None
    root = os.path.dirname(os.path.dirname(ap))
    stem = os.path.splitext(os.path.basename(ap))[0]
    sid = stem[: -len(".example")] if stem.endswith(".example") else stem
    scenario_path = os.path.join(root, "scenarios", sid + ".json")
    return scenario_path if os.path.isfile(scenario_path) else None


def _capture_origin(path: str, *, stack: Optional[str],
                    call_id: Optional[str]) -> dict:
    """Authenticate where this audio came from, honestly. Three distinct
    kinds -- deliberately NOT the ``runner_attested`` / ``operator_asserted``
    vocabulary :mod:`hotato.receipt` / :mod:`hotato.evidence` use for a
    before/after fresh-recapture PAIR (a stronger, distinct claim this
    single-shot discovery command never makes):

      frozen_regression        a previously-created fixture clip (a sibling
                                scenario file names this exact audio): a
                                pinned regression, not a live call.
      provider_pulled          fetched just now from the STACK's own
                                recording API for this exact call id
                                (``capture.fetch_one``). Stronger than an
                                arbitrary file (the vendor itself served it
                                for a named call id), but this is NOT a
                                signed capture receipt -- never read it as
                                "runner-attested" or "machine-verified".
      operator_asserted_local  you handed hotato a local WAV path directly;
                                nothing here independently verifies it.
    """
    frozen = _frozen_regression_scenario(path)
    if frozen is not None:
        return {
            "kind": "frozen_regression",
            "note": (
                "this recording is a previously-created hotato fixture clip "
                f"({os.path.basename(frozen)}), not a live call: a pinned "
                "regression, not fresh evidence"
            ),
            "scenario_path": frozen,
        }
    if stack and call_id:
        return {
            "kind": "provider_pulled",
            "stack": stack,
            "call_id": call_id,
            "note": (
                f"fetched directly from {stack}'s own recording API for "
                f"call {call_id!r}. Stronger provenance than an arbitrary "
                "file, but this is NOT a signed capture receipt (see "
                "hotato.receipt): never read it as a machine-verified or "
                "runner-attested recapture."
            ),
        }
    return {
        "kind": "operator_asserted_local",
        "path": os.path.basename(path),
        "note": (
            "you supplied this WAV path directly; hotato has not "
            "independently verified it against any vendor, so its origin "
            "is operator-asserted only"
        ),
    }


# --- config identity: auto-attach the live turn-taking config snapshot -----

def _capture_config_snapshot(
    stack: str, *, agent_id: Optional[str], api_key: Optional[str],
) -> dict:
    """Best-effort: run the read-only ``hotato inspect`` config snapshot for a
    LIVE stack and wrap it in the bundle's ``{stack, config, note}`` shape, so
    ``investigate --stack`` fills ``source/stack_config_snapshot.json`` with a
    real turn-taking baseline instead of the bare placeholder (silent config
    drift is a confirmed cross-provider failure class).

    Never raises and never fabricates: an inspect that cannot run -- an
    unsupported stack, a static-config stack with no call-time file, a missing
    ``--agent-id`` or credential, or a network/parse error -- returns an honest
    ``captured: False`` note naming the real reason, so the bundle records WHY
    the baseline is absent rather than a fabricated snapshot. Non-identifying by
    construction: only the normalized turn-taking config and observations are
    kept, never the assistant/agent id or raw target."""
    from . import inspectcfg as _inspect

    stack_l = (stack or "").strip().lower()

    def _note_only(reason: str) -> dict:
        return {"stack": stack_l or "generic", "config": {},
                "captured": False, "note": reason}

    if stack_l not in _inspect.INSPECT_STACKS:
        return _note_only(
            f"hotato inspect has no live config reader for {stack_l!r}; run "
            "`hotato inspect` for a supported stack and attach it by hand."
        )
    if stack_l in ("livekit", "pipecat"):
        return _note_only(
            f"{stack_l} turn-taking config lives in a local agent file, not a "
            f"call pull; run `hotato inspect --stack {stack_l} --config "
            "<file.py>` and attach it by hand."
        )
    if not agent_id:
        return _note_only(
            f"no assistant/agent id given (--agent-id), so `hotato inspect "
            f"--stack {stack_l}` could not fetch the live config; pass "
            "--agent-id to auto-capture the baseline."
        )
    try:
        if stack_l == "vapi":
            result = _inspect.run_inspect(
                stack="vapi", assistant_id=agent_id, api_key=api_key)
        else:  # retell
            result = _inspect.run_inspect(
                stack="retell", agent_id=agent_id, api_key=api_key)
    except Exception as exc:  # best-effort: never fail investigate on inspect
        return _note_only(
            f"`hotato inspect --stack {stack_l}` could not run ({exc}); the "
            "config baseline was not captured."
        )
    prov = result.get("fetched_at_provenance") or {}
    # The provenance method is a request line like "GET .../assistant/<id>", so
    # the assistant/agent id is embedded in its URL. Strip it before the method
    # enters the share-safe bundle -- the endpoint shape is useful provenance,
    # the id is not, and this snapshot is a share-safe surface.
    method = prov.get("method")
    if method and agent_id:
        method = method.replace(agent_id, "<agent-id>")
    return {
        "stack": stack_l,
        "config": result.get("turn_taking", {}),
        "observations": result.get("observations", []),
        "captured": True,
        "inspect_method": method,
        "note": (
            "captured live from `hotato inspect` at investigate time "
            "(read-only GET); a later config drift can be diffed against this "
            "baseline."
        ),
    }


# --- audio in: reuse capture.py wholesale, never a second fetch path -------

def _resolve_audio(
    source: Optional[str], *, stack: Optional[str], call_id: Optional[str],
    api_key: Optional[str], account_sid: Optional[str],
    auth_token: Optional[str], model_id: Optional[str],
    agent_id: Optional[str], base_url: Optional[str], allow_mono: bool,
) -> Tuple[str, dict]:
    """Resolve the local WAV path to investigate, plus its fetch metadata
    (``{"stack", "call_id"}``, empty for a local SOURCE). Reuses
    ``capture.resolve_creds`` / ``capture.fetch_one`` -- the exact per-stack
    fetch ``hotato pull`` loops over -- so a live pull is never a second HTTP
    client or a re-implemented adapter."""
    from . import capture as _capture

    if source and (stack or call_id):
        raise ValueError(
            "provide either a local SOURCE path or --stack/--call-id, not "
            "both"
        )
    if source:
        if not os.path.isfile(source):
            raise ValueError(f"{source!r}: no such file.")
        return source, {}
    if not (stack and call_id):
        raise ValueError(
            "hotato investigate needs either a local SOURCE WAV path, or "
            "--stack STACK --call-id ID to pull one from a connected stack"
        )
    stack = stack.strip().lower()
    if stack not in _capture.PULL_STACKS:
        raise ValueError(
            f"{stack!r} has no direct fetch for hotato investigate; "
            f"connectable stacks: {', '.join(_capture.PULL_STACKS)}. "
            "LiveKit/Pipecat are capture-in-your-infra: run `hotato setup "
            f"--stack {stack}` and pass the resulting WAV as SOURCE instead."
        )
    overrides = _capture._overrides_from(
        api_key=api_key, account_sid=account_sid, auth_token=auth_token,
        model_id=model_id, agent_id=agent_id, base_url=base_url,
    )
    creds = _capture.resolve_creds(stack, overrides)
    path = _capture.fetch_one(stack, call_id, creds, allow_mono=allow_mono)
    return path, {"stack": stack, "call_id": call_id}


# --- investigate: trust (K6) + scan, persisted, with the label commands ----

def run_investigate(
    source: Optional[str] = None,
    *,
    stack: Optional[str] = None,
    call_id: Optional[str] = None,
    api_key: Optional[str] = None,
    account_sid: Optional[str] = None,
    auth_token: Optional[str] = None,
    model_id: Optional[str] = None,
    agent_id: Optional[str] = None,
    base_url: Optional[str] = None,
    allow_mono: bool = False,
    caller_channel: int = 0,
    agent_channel: int = 1,
    min_gap: float = 2.0,
    top: int = 10,
    state_path: Optional[str] = None,
    channel_map_confirmed: bool = False,
) -> Tuple[dict, int]:
    """Investigate one recording end to end: pull/open it, authenticate its
    capture origin, run the K6 trust gate, scan it for candidate moments, and
    persist all of it. Returns ``(result, exit_code)``.

    ``exit_code`` is 0 when the recording is candidate-eligible (scan ran; a
    real yield/hold VERDICT may still be refused -- see
    ``result["verdict_status"]``) and 2 when it is not scorable at all,
    mirroring ``hotato trust``'s own exit-code convention. Raises
    ``ValueError`` (exit 2) for a bad usage combination (both SOURCE and
    --stack/--call-id, neither given, a bad --min-gap, or an out-of-range
    channel flag) before any network call or file read.
    """
    from . import analyze as _analyze
    from . import scan as _scan
    from . import trust as _trust

    _analyze.validate_scan_args(
        caller_channel=caller_channel, agent_channel=agent_channel,
        min_gap_sec=min_gap,
    )
    state_path = state_path or default_state_path()

    path, fetch_meta = _resolve_audio(
        source, stack=stack, call_id=call_id, api_key=api_key,
        account_sid=account_sid, auth_token=auth_token, model_id=model_id,
        agent_id=agent_id, base_url=base_url, allow_mono=allow_mono,
    )
    origin = _capture_origin(
        path, stack=fetch_meta.get("stack"), call_id=fetch_meta.get("call_id"),
    )

    # Config identity auto-attach: only for a LIVE stack pull (never a bare
    # local WAV), best-effort and fail-closed. Carried in this state file so
    # `investigate label` writes it into the contract bundle's
    # source/stack_config_snapshot.json instead of the bare placeholder.
    config_snapshot = None
    if fetch_meta.get("stack"):
        config_snapshot = _capture_config_snapshot(
            fetch_meta["stack"], agent_id=agent_id, api_key=api_key)

    cfg = ScoreConfig()
    # K6, contract mode: the SAME stricter crosstalk/leakage bar
    # `contract create` itself checks, since this discovery is aimed at
    # producing a contract -- a false-confident pass here would ship as a
    # false-confident pass there too.
    trust_rep = _trust.trust_report(
        path, caller_channel=caller_channel, agent_channel=agent_channel,
        cfg=cfg, mode=_trust.VERDICT_MODE_CONTRACT,
        channel_map_confirmed=channel_map_confirmed,
    )

    candidates: list = []
    scan_note = _scan.SCAN_NOTE
    if trust_rep.get("scorable"):
        # Only scan a candidate-eligible input: scan_recording itself raises
        # a hard ValueError on a mono/undersized file, which trust's own
        # not-scorable report (with the honest reason + next step) already
        # covers more gracefully -- so a not-scorable input never reaches it.
        scan_result = _scan.scan_recording(
            path, caller_channel=caller_channel, agent_channel=agent_channel,
            cfg=cfg, min_gap_sec=min_gap,
        )
        candidates = scan_result["candidates"]
        scan_note = scan_result["note"]

    source_name = os.path.basename(path)
    folder_path = os.path.dirname(os.path.abspath(path))
    for c in candidates:
        # scan_recording's per-candidate dicts carry no "source" (a
        # single-file scan names it once, at the top level); add it here so
        # this state file's candidates match the SAME shape
        # fixture.parse_candidate_ref / _resolve_source_audio already read
        # (analyze.py's aggregation does the identical thing across many
        # files) -- one candidate-ref resolver, never a second one.
        c["source"] = source_name

    # K6: verdict_eligible is a NARROWER gate than candidate_eligible (scan
    # already ran above regardless): a suspected swap or crosstalk/leakage at
    # the contract-mode bar refuses a real yield/hold VERDICT, never the
    # advisory timing candidates.
    verdict_status = {
        "eligible": bool(trust_rep.get("verdict_eligible")),
        "reason": trust_rep.get("verdict_ineligible_reason"),
        "mode": trust_rep.get("verdict_mode"),
    }

    prior = load_state(state_path)
    run_no = (prior.get("run", 0) if prior else 0) + 1
    history = list(prior.get("history") or []) if prior else []
    updated_at = _now()

    state = {
        "schema": STATE_SCHEMA_ID,
        "tool": "hotato",
        # Additive: makes this state file ITSELF a valid FILE#N candidate
        # ref for `hotato fixture promote` / `hotato contract create
        # --from-candidate` (both read exactly this shape) -- never a
        # second ref-resolution path.
        "kind": "analyze",
        "schema_version": "1",
        "run": run_no,
        "created_at": (prior or {}).get("created_at") or updated_at,
        "updated_at": updated_at,
        "source_path": os.path.abspath(path),
        "folder": os.path.basename(folder_path) or folder_path,
        "folder_path": folder_path,
        "note": scan_note,
        "capture_origin": origin,
        "stack_config_snapshot": config_snapshot,
        "trust": trust_rep,
        "verdict_status": verdict_status,
        "config": {
            "caller_channel": caller_channel, "agent_channel": agent_channel,
            "min_gap_sec": min_gap,
        },
        "total_candidates": len(candidates),
        "candidates": candidates,
        "history": history,
    }
    state["history"].append({
        "run": run_no,
        "at": updated_at,
        "source": source_name,
        "capture_origin": origin["kind"],
        "candidate_eligible": bool(trust_rep.get("scorable")),
        "verdict_eligible": verdict_status["eligible"],
        "total_candidates": len(candidates),
    })
    save_state(state_path, state)

    top_n = len(candidates) if top <= 0 else min(top, len(candidates))
    shown = candidates[:top_n]
    next_cmds = []
    for i in range(1, top_n + 1):
        ref = f"{state_path}#{i}"
        next_cmds.append({
            "rank": i,
            "ref": ref,
            "command": (f"hotato investigate label {shlex.quote(ref)} "
                        "--expect yield"),
        })

    exit_code = 0 if trust_rep.get("scorable") else 2
    result = {
        "tool": "hotato",
        "kind": "investigate",
        "schema_version": "1",
        "state_path": state_path,
        "run": run_no,
        "source": source_name,
        "capture_origin": origin,
        "stack_config_snapshot": config_snapshot,
        "trust": {
            "recommendation": trust_rep.get("recommendation"),
            "scorable": trust_rep.get("scorable"),
            "not_scorable_reason": trust_rep.get("not_scorable_reason"),
            "warnings": trust_rep.get("warnings"),
        },
        "verdict_status": verdict_status,
        "note": scan_note,
        "total_candidates": len(candidates),
        "shown": len(shown),
        "candidates": shown,
        "next": next_cmds,
        "exit_code": exit_code,
    }
    return result, exit_code


def _render_capture_origin_lines(origin: dict) -> list:
    """The SAME honest three-way capture-origin classification (K6's
    provenance, not a mutation/recapture claim -- see the module docstring)
    rendered identically wherever a result carries a ``capture_origin`` block:
    ``hotato investigate``'s own report and ``hotato investigate label``'s
    terminal summary both call this, so the two can never describe the same
    origin differently."""
    lines = []
    if origin["kind"] == "provider_pulled":
        lines.append(
            f"  capture origin: pulled from {origin['stack']} call "
            f"{origin['call_id']!r}"
        )
    elif origin["kind"] == "frozen_regression":
        lines.append(
            f"  capture origin: frozen regression clip "
            f"({origin['scenario_path']})"
        )
    else:
        lines.append(f"  capture origin: operator-asserted local file "
                     f"({origin['path']})")
    lines.append(f"    {origin['note']}")
    return lines


def _investigate_input_ref(result: dict) -> str:
    """The re-runnable ``hotato investigate`` input for this result: the
    ``--stack``/``--call-id`` pair for a live pull, else the local source
    name. Used only to point the reader at ``--all`` for the full list."""
    origin = result.get("capture_origin") or {}
    if origin.get("kind") == "provider_pulled":
        return (f"--stack {origin['stack']} "
                f"--call-id {shlex.quote(str(origin['call_id']))}")
    return shlex.quote(result["source"])


def _candidate_line(cand: dict, rank: int) -> str:
    from . import scan as _scan
    return (f"    [{rank}] t={cand['t_sec']}s {cand['kind']}  "
            f"{_scan.candidate_detail(cand)}")


def _capture_and_health_lines(result: dict) -> list:
    """The provenance caveats for one investigate result: the authenticated
    capture origin, the config baseline, and the input-health recommendation
    plus any warnings. Rendered together so the same result never describes its
    provenance two ways."""
    lines = list(_render_capture_origin_lines(result["capture_origin"]))
    snap = result.get("stack_config_snapshot")
    if isinstance(snap, dict):
        if snap.get("captured"):
            lines.append("  config baseline: captured live "
                         "(source/stack_config_snapshot.json in the contract)")
        else:
            lines.append(f"  config baseline: not captured ({snap.get('note')})")
    t = result["trust"]
    lines.append(f"  input health: {t['recommendation']}")
    for w in t.get("warnings") or []:
        lines.append(f"    warning: {w}")
    return lines


def _verdict_lines(vs: dict) -> list:
    if vs["eligible"]:
        return ["  verdict path: eligible (a labeled event here can carry a "
                "yield/hold verdict)"]
    return [
        f"  verdict path: REFUSED ({vs['mode']} mode): {vs['reason']}",
        "    the candidates shown are still timing facts, never a "
        "verdict; label one only after confirming the channel mapping "
        "(--confirm-channels) or fixing the crosstalk",
    ]


def render_text(result: dict, *, show_all: bool = False) -> str:
    from . import scan as _scan

    t = result["trust"]
    lines = [f"hotato investigate [run {result['run']}]: {result['source']}"]

    # No catch to lead with (not scorable): keep the caveat-first report
    # -- capture origin, input health, the reason, and the fix.
    if not t["scorable"]:
        lines.extend(_capture_and_health_lines(result))
        lines.append(f"  NOT SCORABLE: {t['not_scorable_reason']}")
        lines.append("  no candidates scanned; fix the input and re-run.")
        lines.append(f"  state remembered at: {result['state_path']}")
        return "\n".join(lines)

    nexts = result["next"]
    cands = result["candidates"]
    vs = result["verdict_status"]

    if not nexts:
        lines.extend(_capture_and_health_lines(result))
        lines.extend(_verdict_lines(vs))
        lines.append("  no candidate moments found in this recording.")
        lines.append(f"  state remembered at: {result['state_path']}")
        return "\n".join(lines)

    # Lead with the CATCH (the hit above the hedging): the single top-ranked
    # candidate, one plain-English sentence describing it, visually isolated,
    # with the one command that turns it into a CI contract beneath it. The
    # provenance/health caveats follow, just below the hit.
    top = nexts[0]
    top_cand = cands[top["rank"] - 1]
    rule = "  " + "-" * 64
    lines.append("  most likely failure (top-ranked candidate):")
    lines.append(rule)
    lines.append(_candidate_line(top_cand, top["rank"]))
    lines.append(f"    {_scan.candidate_plain_english(top_cand)}")
    lines.append(rule)
    lines.append("  turn it into a CI contract (use --expect hold instead if "
                 "the agent was right to keep talking):")
    lines.append(f"    {top['command']}")

    # The provenance/health caveats, now BELOW the hit.
    lines.append("")
    lines.extend(_capture_and_health_lines(result))
    lines.extend(_verdict_lines(vs))

    if show_all and len(nexts) > 1:
        lines.append(f"  all {len(nexts)} candidate moment(s):")
        for n in nexts[1:]:
            lines.append(_candidate_line(cands[n["rank"] - 1], n["rank"]))
            lines.append(f"        label: {n['command']}")
    elif len(nexts) > 1:
        more = len(nexts) - 1
        lines.append(
            f"  {more} more candidate(s). See all: hotato investigate "
            f"{_investigate_input_ref(result)} --all"
        )
    lines.append(f"  state remembered at: {result['state_path']}")
    return "\n".join(lines)


# --- investigate label: the human's decision -> a real signed contract -----

def _auto_contract_id(cand: dict, expect: str) -> str:
    """A readable default contract id when ``--id`` is not given: the
    source's stem, the onset second, and the label -- never a bare counter,
    so it stays informative even skimmed out of context."""
    source = os.path.splitext(os.path.basename(str(cand.get("source") or "call")))[0]
    t = cand.get("t_sec")
    t_slug = f"{int(round(float(t)))}s" if isinstance(t, (int, float)) else "x"
    return _sanitize_slug_part(f"{source}-{t_slug}-{expect}")


def run_investigate_label(
    ref: str,
    *,
    expect: str,
    contract_id: Optional[str] = None,
    out_dir: str = DEFAULT_OUT_DIR,
    folder: Optional[str] = None,
    stack: Optional[str] = None,
    rationale: Optional[str] = None,
    max_talk_over_sec: Optional[float] = None,
    max_time_to_yield_sec: Optional[float] = None,
    pre_sec: float = 2.0,
    post_sec: float = 6.0,
    no_clip: bool = False,
    force: bool = False,
    caller_channel: int = 0,
    agent_channel: int = 1,
    include_identifiers: bool = False,
    confirm_channels: bool = False,
    reviewer: Optional[str] = None,
) -> dict:
    """The human's yield/hold decision for one ``hotato investigate``
    candidate. This IS the label step: ``expect`` goes straight to
    :func:`hotato.contract.create_contract` (``from_candidate=ref``), which
    -- via :func:`hotato.fixture.create_fixture` ->
    :func:`hotato.labelrecord.mint_label_record` -- mints a REAL signed
    label-record bound to the exact decoded audio, when a signing key is
    configured. Nothing here fabricates a label or a verdict; a human ran
    this command and chose ``--expect``.

    ``reviewer`` names that human (falling back to the env default --
    ``HOTATO_REVIEWER``/``USER``/``USERNAME`` -- when omitted); it is bound
    into the minted label-record AND the produced contract's
    ``identity.reviewer`` (see :func:`hotato.contract.create_contract`).
    Absent any signing key, minting never crashes and never fabricates a
    human attestation: the label-record stays ``None`` and the contract
    honestly floors its ``label_authority`` at ``"asserted"`` -- the SAME
    tier :func:`hotato.manifest.build_manifest` derives from an explicit,
    unsigned expectation.

    Building a CONTRACT (not a bare fixture) is deliberate: a contract
    carries the K6 trust block, the CI policy, and the exact
    ``hotato contract verify`` command -- the CI-ready artifact
    ``hotato investigate`` exists to produce. Raises ``ValueError`` (exit 2)
    for a bad ``ref``, a bad ``--expect``, or a candidate that turns out not
    scorable -- identical to ``contract create``'s own refusal, since this
    wraps it.
    """
    if str(expect).strip().lower() not in ("yield", "hold"):
        raise ValueError(f"--expect must be 'yield' or 'hold', got {expect!r}")

    from . import contract as _contract
    from . import fixture as _fixture

    path, call, number = _fixture.parse_candidate_ref(ref)
    doc = _fixture._load_result(path)
    cand = _fixture._resolve_candidate(doc, path=path, call=call, number=number)

    auto_id = contract_id is None
    cid = contract_id or _auto_contract_id(cand, expect)

    # Config identity auto-attach: when this ref resolves to an investigate-state
    # file that captured a live turn-taking config snapshot, thread it into the
    # bundle's source/stack_config_snapshot.json (a plain analyze/sweep FILE#N
    # ref carries none, so this stays None and the placeholder is written).
    config_snapshot = doc.get("stack_config_snapshot") if isinstance(doc, dict) else None

    result = _contract.create_contract(
        from_candidate=ref,
        config_snapshot=config_snapshot,
        # `investigate label` is a deliberate human-review act that already
        # requires an explicit --reviewer, so that reviewer IS the human
        # attestation (R-09); the tty/--i-attest gate applies to the plain
        # `fixture create`/`contract create` scripted path, not this one.
        human_review_attested=True,
        contract_id=cid,
        expect=expect,
        out_dir=out_dir,
        folder=folder,
        stack=stack,
        max_talk_over_sec=max_talk_over_sec,
        max_time_to_yield_sec=max_time_to_yield_sec,
        rationale=rationale,
        pre_sec=pre_sec,
        post_sec=post_sec,
        no_clip=no_clip,
        force=force,
        caller_channel=caller_channel,
        agent_channel=agent_channel,
        include_identifiers=include_identifiers,
        confirm_channels=confirm_channels,
        reviewer_principal=reviewer,
    )
    result["candidate_ref"] = ref
    result["auto_id"] = auto_id
    # Additive, honest classification: the SAME capture-origin block
    # `hotato investigate` reported for this exact recording, when the
    # candidate ref resolves to an investigate-state file (a plain analyze/
    # sweep FILE#N ref carries no such field, and this stays None -- never
    # guessed).
    result["capture_origin"] = doc.get("capture_origin")
    result["stack_config_snapshot"] = config_snapshot
    return result


def _pr_create_command(result: dict) -> str:
    """The ONE next step for a CI-ready contract: the exact ``hotato pr
    create`` invocation for THIS bundle. ``pr create`` accepts the bundle
    directory directly, stages it byte-identical under
    ``tests/hotato/contracts/``, and opens the pull request that gates CI on
    it -- so the printed command runs as-is once OWNER/REPO is filled in."""
    return (
        f"hotato pr create --fixtures {shlex.quote(result['dir'])} "
        "--repo OWNER/REPO --title "
        + shlex.quote(f"Add hotato contract {result['id']}")
    )


def _next_ladder(result: dict) -> list:
    """The one next step from a just-created contract: the ``hotato pr
    create`` command that lands this exact bundle in the repo's CI gate.
    The analog of the demo golden path's own next-steps ladder
    (``start._next_commands_text``), bound to THIS contract's real bundle
    directory.

    Shown only when the contract is CI-ready (scorable and verdict-eligible): a
    not-scorable or verdict-withheld contract keeps only
    ``render_create_text``'s fix-the-input guidance, so we never point a dev at
    a CI gate that ``contract verify`` would refuse.
    """
    c = result.get("contract") or {}
    m = c.get("measurement") or {}
    if not m.get("scorable") or not m.get("verdict_eligible", True):
        return []
    return [
        "open the pull request that adds it to your repo's CI gate:",
        f"  {_pr_create_command(result)}",
    ]


def render_label_text(result: dict) -> str:
    from . import contract as _contract

    lines = [f"hotato investigate label: {result['candidate_ref']}"]
    if result.get("auto_id"):
        lines.append("  (id auto-generated from the candidate; pass --id to "
                     "name it yourself)")
    origin = result.get("capture_origin")
    if origin:
        lines.extend(_render_capture_origin_lines(origin))
    lines.append(_contract.render_create_text(result))
    ladder = _next_ladder(result)
    if ladder:
        lines.append("")
        lines.extend(ladder)
    return "\n".join(lines)


def label_result_json(result: dict) -> dict:
    from . import contract as _contract

    out = _contract.create_result_json(result)
    out["kind"] = "investigate-label"
    out["candidate_ref"] = result["candidate_ref"]
    out["auto_id"] = result["auto_id"]
    out["capture_origin"] = result.get("capture_origin")
    if _next_ladder(result):
        # structured, not the rendered indentation: the ONE next step, the
        # pr create command that stages this exact bundle and opens the PR
        out["next_commands"] = [_pr_create_command(result)]
    return out
