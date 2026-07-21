"""``hotato.simulate``: the deterministic scripted-caller simulator (Phase-2 2.1).

The trustworthy regression foundation BEFORE any generative caller: render a
:mod:`hotato.scenario` into a labelled ``origin=simulated``
:mod:`hotato.conversation` artifact -- a transcript plus a ``voice_trace.v1`` --
that a fixed ``(scenario, seed)`` reproduces BYTE-FOR-BYTE. There is NO live
agent, NO TTS, and NO network on this path (those are later opt-in slices with
their own EGRESS rows). Everything here is the deterministic INPUT side.

The five honesty invariants are kept STRUCTURAL, not documented-and-hoped:

* ``origin.kind == "simulated"`` on EVERY produced conversation -- never 'real'.
  :func:`render` always stamps a simulated origin (model_id ``"scripted"``,
  the scenario id, the seed); :func:`write_artifact` refuses to write anything
  whose origin is not simulated. Synthetic is never conflated with real, and a
  simulated conversation is never merged into a real bucket.
* A bad simulation is reported as ``SIMULATOR_INVALID`` (:func:`validate_simulation`),
  NEVER scored as an agent PASS/FAIL. Scoring is the SEPARATE Phase-1 assert
  layer's job, over the produced artifact; the simulator only decides whether
  the produced conversation is a faithful rendering of its scenario.
* Reproducibility is scoped to "a SEEDED REPLAY is byte-identical", never "the
  model is deterministic" -- there is no model. A fixed ``(scenario, seed)``
  produces the same transcript bytes every time (content-hashed); different
  seeds differ ONLY where the scenario allows it (probabilistic backchannels).
* No ``overall_score`` / blended number anywhere. Reliability
  (:func:`reliability`) reports pass@1 / pass@k / pass^k as its OWN dimension;
  for the scripted deterministic caller pass^k == pass@1 (a replay is
  byte-identical, so every run has the same outcome) -- expected: deterministic
  replay produces zero variance.
* Per-run seeds are derived by :func:`expand` from ``sha256(scenario_id +
  variation-tuple)`` -- no ``Math.random``, no time-based seed. The seeded PRNG
  reuses :mod:`hotato.synth`'s LCG, the same deterministic generator that stamps
  synthetic provenance without ever raising the confidence tier.
"""

from __future__ import annotations

import concurrent.futures as _futures
import copy
import datetime as _dt
import hashlib
import json
import math
import os
from typing import Any, Dict, List, Optional

from . import assert_ as _assert
from . import conversation as CV
from . import conversation_test as _ct
from . import scenario as _scn
from . import synth as _synth
from .errors import (
    open_regular as _open_regular,  # noqa: F401  (parity import; scenario/CV own the FIFO guard)
)

__all__ = [
    "MODEL_ID",
    "MATRIX_KIND",
    "SIMULATOR_INVALID",
    "VOICE_TRACE_SCHEMA",
    "render",
    "write_artifact",
    "run_scripted",
    "validate_simulation",
    "expand",
    "reliability",
    "run_matrix",
    "resolve_source_date_epoch",
    "deterministic_created_at",
]

# The simulator's model_id: this is a SCRIPTED caller, not a generative model.
# It rides on the conversation manifest's origin.simulator block so the artifact
# always says exactly what produced it.
MODEL_ID = "scripted"

# The status returned for a simulation that did NOT faithfully render its
# scenario. It is NEVER an agent PASS/FAIL -- a broken simulation is a broken
# fixture, not evidence about an agent.
SIMULATOR_INVALID = "SIMULATOR_INVALID"

# The `kind` stamped on a run_matrix summary (parallels the "simulate" kind the
# single-run CLI emits). A summary is an ATTRIBUTABLE aggregate, never a blended
# score -- there is no overall_score anywhere in it, by construction.
MATRIX_KIND = "simulate-matrix"

# Mirrors hotato.trace.SCHEMA. Hardcoded (like hotato.synth's TOOL constant) so
# this deterministic renderer stays free of the trace->contract->report import
# chain; the value is pinned by tests that round-trip the produced trace.
VOICE_TRACE_SCHEMA = "hotato.voice_trace.v1"
_CREATED_BY = "hotato simulate (scripted caller)"

# --- the deterministic timing model (fixed constants; no wallclock) --------
_LEAD_IN_SEC = 0.5        # a short lead-in before the first caller turn
_GAP_SEC = 0.5            # silence between consecutive caller turns
_MIN_TURN_SEC = 0.4       # floor on a turn's duration
_SEC_PER_WORD = 0.3       # nominal speech pace, scaled by speaking_rate
_BACKCHANNEL_SEC = 0.3    # a short acknowledgement
_BACKCHANNEL_TOKENS = ("mm-hmm", "right", "okay", "yeah")

# variation-matrix dimension defaults (a dimension the matrix omits collapses to
# the scenario's own single declared value, so expand() always yields >= 1 run).
_DEFAULT_LOCALE = "en-US"
_DEFAULT_NOISE = "clean"
_DEFAULT_BEHAVIOR = "default"


# =========================================================================
# canonical serialization (byte-stability lives here)
# =========================================================================

def _canonical_json(obj: Any) -> str:
    """The ONE serialization the transcript/trace are written with, so a fixed
    ``(scenario, seed)`` yields identical bytes and the file's sha256 (bound in
    the manifest) equals :data:`render`'s ``content_hash``. sort_keys + a fixed
    indent make it order-stable across machines and Python versions."""
    return json.dumps(obj, indent=2, sort_keys=True) + "\n"


def _dump_trace_jsonl(vt: Dict[str, Any]) -> str:
    """Serialize a ``voice_trace.v1`` object as the meta-line-then-one-span-per
    -line JSONL :func:`hotato.trace.load_voice_trace_jsonl` reads back (the same
    shape :func:`hotato.trace._dump_voice_trace_jsonl` writes). Kept local so the
    deterministic renderer needs no import of the trace/contract/report chain."""
    meta = {k: v for k, v in vt.items() if k != "spans"}
    meta["_meta"] = True
    lines = [json.dumps(meta, sort_keys=True)]
    for span in vt.get("spans") or []:
        lines.append(json.dumps(span, sort_keys=True))
    return "\n".join(lines) + "\n"


def _write_owned(path: str, text: str) -> None:
    # open-ok: path is inside the --out directory this run created/owns (the same
    # posture hotato.test_run._write_json documents for the artifact children).
    # newline="": the manifest binds this file's sha256 to render's content_hash
    # (computed over the canonical "\n" string); text mode's default
    # newline=None would rewrite "\n" to os.linesep ("\r\n" on Windows, per the
    # io docs) and break that byte identity.
    with open(path, "w", encoding="utf-8", newline="") as fh:
        fh.write(text)


# =========================================================================
# deterministic provenance timestamp (the manifest created_at)
# =========================================================================
#
# A written conversation.v1 manifest carries a ``created_at``. For a DETERMINISTIC
# replay (same scenario -> same seeds -> byte-identical artifact) the default
# MUST NOT be the wall clock, or two seeded runs of the same matrix diverge on
# that one field alone. So the default is a fixed, reproducible instant, honoring
# the reproducible-builds ``SOURCE_DATE_EPOCH`` convention:
#
#   explicit caller value  >  $SOURCE_DATE_EPOCH  >  fixed default (epoch 0)
#
# A caller who wants the mint wall-clock (e.g. a one-off `hotato
# simulate ... --out`) passes it explicitly; run_matrix's own default stays
# reproducible so the "simulate hundreds -> byte-identical" contract holds for
# the WRITTEN artifacts, not just the summary.
_DEFAULT_SOURCE_DATE_EPOCH = 0  # 1970-01-01T00:00:00Z -- an obvious placeholder


def resolve_source_date_epoch(explicit: Optional[int] = None) -> int:
    """Resolve the reproducible-build epoch (integer seconds since 1970 UTC) used
    for a deterministic artifact's ``created_at``. ``explicit`` wins; else the
    ``SOURCE_DATE_EPOCH`` environment variable (the reproducible-builds
    convention); else a fixed default (epoch 0) -- NEVER the wall clock, so two
    seeded runs are byte-identical. Raises ``ValueError`` on a non-integer env
    value."""
    if explicit is not None:
        return int(explicit)
    env = os.environ.get("SOURCE_DATE_EPOCH")
    if env is not None and env.strip() != "":
        try:
            return int(env.strip())
        except ValueError as exc:
            raise ValueError(
                "SOURCE_DATE_EPOCH must be an integer number of seconds since "
                f"the Unix epoch, got {env!r}"
            ) from exc
    return _DEFAULT_SOURCE_DATE_EPOCH


def _epoch_to_iso(epoch: int) -> str:
    """Format an integer epoch (seconds, UTC) as the ``%Y-%m-%dT%H:%M:%SZ`` string
    the conversation.v1 manifest's ``created_at`` uses."""
    return _dt.datetime.fromtimestamp(
        int(epoch), _dt.timezone.utc
    ).strftime("%Y-%m-%dT%H:%M:%SZ")


def deterministic_created_at(explicit: Optional[str] = None) -> str:
    """The manifest ``created_at`` for a deterministic replay: the caller's
    explicit ISO string when given, else a reproducible instant derived from
    :func:`resolve_source_date_epoch` (NEVER the wall clock)."""
    if explicit is not None:
        return explicit
    return _epoch_to_iso(resolve_source_date_epoch())


# =========================================================================
# render: scenario + seed -> the deterministic caller-side conversation
# =========================================================================

def _u01(rng) -> float:
    """One draw in [0, 1) from :func:`hotato.synth._lcg` (which yields [-1, 1)).
    Reusing synth's seeded LCG keeps every deterministic generator in hotato on
    the SAME provenance-safe PRNG -- no os.urandom, no time seed."""
    return (next(rng) + 1.0) / 2.0


# --- the mock agent (Phase-2 tool mocks + state sandbox; gated, additive) ---
_TOOL_SEC = 0.4  # nominal duration of a mock tool call


def _render_agent_mock(agent_mock: Dict[str, Any], cursor: float) -> List[Dict[str, Any]]:
    """Render a scenario's optional ``agent_mock`` block into AGENT-side trace
    spans, deterministically placed after the caller turns (or at each tool's
    declared ``latency_ms`` offset). This is the deterministic MOCK agent
    (Phase-2 tool mocks; 1.3 item 9) -- the produced conversation stays
    ``origin=simulated`` and these spans are the SIMULATOR's mock evidence,
    never a live agent's. ``tool_call`` spans (Authority 1) carry the declared
    ``arguments`` and ``result``/``error`` so :mod:`hotato.assert_`'s
    ``tool_result``/``tool_error`` read them; an optional ``handoff`` /
    ``termination`` render as their own spans. No wallclock enters any span, so
    a fixed ``(scenario, seed)`` stays byte-stable. Absent ``agent_mock`` this
    function is never called, so a scenario without a mock is byte-identical."""
    spans: List[Dict[str, Any]] = []
    t = round(cursor, 3)
    for tool in agent_mock.get("tools") or []:
        # A tool may pin its START at an explicit ``at_ms`` (e.g. to model a call
        # placed mid-turn); otherwise it renders SEQUENTIALLY after the previous
        # tool, so the declared tool ORDER is preserved for `sequence` assertions.
        # ``latency_ms`` is the tool's DURATION/latency (what the `latency`
        # assertion reads), never its position.
        at = tool.get("at_ms")
        lat = tool.get("latency_ms")
        start = round(int(at) / 1000.0, 3) if at is not None else round(t, 3)
        dur = (int(lat) / 1000.0) if lat is not None else _TOOL_SEC
        end = round(start + dur, 3)
        span: Dict[str, Any] = {
            "type": "tool_call", "name": tool["name"],
            "start_sec": start, "end_sec": end,
            "latency_ms": int(lat) if lat is not None else int(_TOOL_SEC * 1000),
        }
        if "arguments" in tool:
            span["arguments"] = tool["arguments"]
        if "error" in tool:
            span["error"] = tool["error"]
            span["status"] = "error"
        else:
            # A tool with no declared result still records an empty result dict,
            # so a `tool_result` with no `result_subset` sees the call succeeded.
            span["result"] = tool.get("result", {})
        spans.append(span)
        t = round(end + _GAP_SEC, 3)
    handoff = agent_mock.get("handoff")
    if handoff:
        spans.append({"type": "handoff", "to": handoff["to"],
                      "time_sec": round(t, 3)})
        t = round(t + _GAP_SEC, 3)
    term = agent_mock.get("termination")
    if term:
        span = {"type": "termination", "time_sec": round(t, 3)}
        if term.get("reason") is not None:
            span["reason"] = term["reason"]
        if term.get("by") is not None:
            span["by"] = term["by"]
        spans.append(span)
    return spans


def render(scenario: Dict[str, Any], seed: int) -> Dict[str, Any]:
    """Render ``scenario`` at ``seed`` into the deterministic caller-side
    conversation (no live agent, no TTS, no network).

    Returns a dict carrying the ``transcript`` (``{"segments": [...]}`` of the
    caller's scripted turns plus any fired backchannels), the ``trace`` (a
    ``voice_trace.v1`` of the caller-audio, backchannel, and declared barge-in
    spans), a simulated ``origin`` block, and a ``content_hash`` (sha256 of the
    canonical transcript bytes). A fixed ``(scenario, seed)`` yields identical
    bytes every call -- a SEEDED REPLAY is byte-identical (this is NOT a claim
    that a model is deterministic; there is no model). Different seeds differ
    ONLY in the probabilistic backchannels the scenario's behavior allows; the
    scripted turns are seed-invariant.

    The renderer only ever speaks as the CALLER -- there is no agent-side turn,
    so a rendering can never solve the task for the agent (an invariant
    :func:`validate_simulation` re-checks structurally)."""
    doc = _scn.validate_scenario_doc(scenario)
    sid = doc["id"]
    caller = doc["caller"]
    behavior = caller.get("behavior") or {}
    rate = float(behavior.get("speaking_rate", _scn.DEFAULT_SPEAKING_RATE))
    interruptions = behavior.get("interruptions") or []
    backchannels = behavior.get("backchannels") or {}
    prob = float(backchannels.get("probability", 0.0))
    script = caller["script"]

    rng = _synth._lcg(int(seed))
    segments: List[Dict[str, Any]] = []
    spans: List[Dict[str, Any]] = []
    cursor = _LEAD_IN_SEC
    for cand_idx, turn in enumerate(script):
        text = turn["say"]
        words = max(1, len(text.split()))
        dur = round(max(_MIN_TURN_SEC, words * _SEC_PER_WORD / rate), 3)
        start = round(cursor, 3)
        end = round(start + dur, 3)
        segments.append({"role": "caller", "text": text, "start": start,
                         "end": end, "kind": "scripted"})
        spans.append({"type": "caller_audio_active", "start_sec": start,
                      "end_sec": end})
        cursor = round(end + _GAP_SEC, 3)
        # ONE PRNG draw per candidate, whether or not it fires, so the seeded
        # stream stays fixed. A backchannel overlaps the gap and never shifts a
        # scripted turn, so the scripted timeline is seed-invariant.
        if _u01(rng) < prob:
            bstart = round(end + _GAP_SEC * 0.4, 3)
            bend = round(bstart + _BACKCHANNEL_SEC, 3)
            btext = _BACKCHANNEL_TOKENS[cand_idx % len(_BACKCHANNEL_TOKENS)]
            segments.append({"role": "caller", "text": btext, "start": bstart,
                             "end": bend, "kind": "backchannel"})
            spans.append({"type": "backchannel", "start_sec": bstart,
                          "end_sec": bend})

    # Declared interruptions render at their fixed offsets -- deterministic (the
    # same barge-in every seed), the perturbation validate_simulation re-checks.
    for itr in interruptions:
        off = round(int(itr["offset_ms"]) / 1000.0, 3)
        spans.append({"type": "caller_barge_in", "time_sec": off,
                      "attributes": {"trigger": itr["trigger"]}})

    # The OPTIONAL deterministic mock agent (Phase-2 tool mocks; 1.3 item 9):
    # gated on an explicit ``agent_mock`` block, so a scenario without one is
    # byte-identical. Renders AGENT-side tool/handoff/termination spans (never a
    # caller turn); the transcript stays caller-only, so the content_hash (over
    # the transcript) is unchanged and the caller-only invariant still holds.
    agent_mock = doc.get("agent_mock")
    if agent_mock:
        spans.extend(_render_agent_mock(agent_mock, cursor))

    segments.sort(key=lambda s: (s["start"], s["end"], s["text"]))
    spans.sort(key=lambda s: (
        s.get("start_sec", s.get("time_sec", 0.0)), s.get("type", "")))

    transcript = {"segments": segments}
    env = doc.get("environment") or {}
    trace = {
        "schema": VOICE_TRACE_SCHEMA,
        "created_by": _CREATED_BY,
        # NO wallclock created_at on this deterministic path: the trace file is
        # byte-stable for a fixed (scenario, seed).
        "call_id": None,
        "deployment": {"stack": "scripted-sim", "agent_id": None,
                       "git_sha": None, "config_hash": None},
        "spans": spans,
        "source": {"format": "scripted-sim", "span_count": len(spans)},
    }
    content_hash = hashlib.sha256(
        _canonical_json(transcript).encode("utf-8")
    ).hexdigest()

    return {
        "scenario_id": sid,
        "seed": int(seed),
        "model_id": MODEL_ID,
        "origin": {
            "kind": "simulated",
            "simulator": {"model_id": MODEL_ID, "scenario_id": sid,
                          "seed": int(seed)},
        },
        "transcript": transcript,
        "trace": trace,
        "content_hash": content_hash,
        "facts": doc.get("facts", {}),
        "goal": doc.get("goal"),
        "perturbation": {
            "noise": env.get("noise"),
            "locale": env.get("locale"),
            "speaking_rate": rate,
        },
        "backchannel_probability": prob,
        "declared_interruptions": [
            {"trigger": i["trigger"], "offset_ms": int(i["offset_ms"])}
            for i in interruptions
        ],
    }


# =========================================================================
# write the render into a conversation.v1 artifact (origin=simulated)
# =========================================================================

def write_artifact(
    render_result: Dict[str, Any],
    out_dir: str,
    *,
    created_at: str,
    agent_id: str = "unbound",
    conversation_id: Optional[str] = None,
    scenario_digest: Optional[str] = None,
) -> Dict[str, Any]:
    """Write a :func:`render` result into ``out_dir`` as a
    ``hotato.conversation.v1`` artifact (``conversation.json`` + the bound
    ``transcript.json`` and ``trace.jsonl``) and return the manifest.

    The manifest's ``origin`` is the render's SIMULATED origin -- this function
    REFUSES (``ValueError``) to write anything whose origin is not
    ``kind="simulated"``, so a simulated conversation can never be minted as
    real. ``agent_id`` defaults to ``"unbound"``: this is the caller-side
    stimulus, not bound to an agent until a later live-play slice.
    ``created_at`` is caller-supplied (never Date.now() on this path)."""
    origin = render_result.get("origin") or {}
    if origin.get("kind") != "simulated":
        raise ValueError(
            "refusing to write a simulated artifact whose origin.kind is not "
            "'simulated'; a simulator never mints a real conversation"
        )
    sid = render_result["scenario_id"]
    seed = render_result["seed"]
    cid = conversation_id or f"{sid}-seed{seed}"

    os.makedirs(out_dir, exist_ok=True)
    transcript_path = os.path.join(out_dir, "transcript.json")
    _write_owned(transcript_path, _canonical_json(render_result["transcript"]))
    trace_path = os.path.join(out_dir, "trace.jsonl")
    _write_owned(trace_path, _dump_trace_jsonl(render_result["trace"]))

    manifest = CV.build_manifest(
        conversation_id=cid,
        agent_id=agent_id,
        origin=origin,
        created_at=created_at,
        artifact_files={"transcript": transcript_path, "trace": trace_path},
        base_dir=out_dir,
        scenario_digest=scenario_digest,
    )
    CV.write_conversation(manifest, out_dir)
    return manifest


def run_scripted(
    scenario: Dict[str, Any],
    seed: int,
    *,
    out_dir: str,
    created_at: Optional[str] = None,
    agent_id: str = "unbound",
    conversation_id: Optional[str] = None,
) -> Dict[str, Any]:
    """Render ``scenario`` at ``seed`` and write it into ``out_dir`` as a
    ``hotato.conversation.v1`` artifact, returning the manifest (origin
    ``kind="simulated"``). ``created_at`` defaults to now (UTC) when omitted --
    pass it for a byte-reproducible manifest; the transcript/trace are byte-
    stable regardless, since no timestamp enters them (a SEEDED REPLAY is
    byte-identical)."""
    render_result = render(scenario, seed)
    if created_at is None:
        created_at = _dt.datetime.now(_dt.timezone.utc).strftime(
            "%Y-%m-%dT%H:%M:%SZ"
        )
    return write_artifact(
        render_result, out_dir, created_at=created_at, agent_id=agent_id,
        conversation_id=conversation_id,
    )


# =========================================================================
# validate_simulation: ok | SIMULATOR_INVALID (never an agent PASS/FAIL)
# =========================================================================

def _invalid(reason: str, checks: Dict[str, Any]) -> Dict[str, Any]:
    return {"ok": False, "status": SIMULATOR_INVALID, "reason": reason,
            "checks": dict(checks)}


def validate_simulation(
    scenario: Dict[str, Any], produced: Dict[str, Any]
) -> Dict[str, Any]:
    """Decide whether ``produced`` (a :func:`render` result) is a FAITHFUL
    rendering of ``scenario``. Returns ``{"ok": True, "status": "ok", ...}`` or
    ``{"ok": False, "status": "SIMULATOR_INVALID", "reason": ...}`` -- NEVER an
    agent PASS/FAIL. A broken simulation is a broken fixture, not evidence about
    an agent; scoring is the SEPARATE assert layer's job.

    The four checks (Phase-2 design 2.1): the produced conversation (1) is
    labelled ``simulated`` for THIS scenario and speaks only as the caller (it
    did NOT solve the task for the agent); (2) renders the scenario's script
    verbatim and in order and does not alter the declared facts -- and states
    each scalar ground-truth fact (a script that never conveys a fact it holds
    has drifted from / violates the scenario); (3) stays within the allowed
    behavior (no backchannel when probability is 0; never more than one per
    turn); (4) applies every declared perturbation (each declared interruption
    is rendered as a barge-in span)."""
    doc = _scn.validate_scenario_doc(scenario)
    checks: Dict[str, Any] = {}

    # (1a) origin is simulated for THIS scenario (never real, never another id).
    origin = produced.get("origin") or {}
    if origin.get("kind") != "simulated":
        return _invalid(
            "origin.kind must be 'simulated'; a simulator never mints a real "
            "conversation", checks)
    sim = origin.get("simulator") or {}
    if sim.get("scenario_id") != doc["id"]:
        return _invalid(
            f"origin.simulator.scenario_id {sim.get('scenario_id')!r} does not "
            f"match scenario id {doc['id']!r}", checks)
    checks["origin_simulated"] = True

    segments = (produced.get("transcript") or {}).get("segments") or []

    # (1b) caller-only: any non-caller turn means the sim spoke for the agent.
    if any(s.get("role") != "caller" for s in segments):
        return _invalid(
            "the simulation contains a non-caller turn; a scripted caller never "
            "speaks for the agent (it must not solve the task for the agent)",
            checks)
    checks["caller_only"] = True

    # (2a) the scripted turns render the scenario script verbatim and in order.
    scripted = [s.get("text") for s in segments if s.get("kind") == "scripted"]
    want = [t["say"] for t in doc["caller"]["script"]]
    if scripted != want:
        return _invalid(
            "the produced scripted turns do not match the scenario script "
            "verbatim/in order; a valid simulation preserves the caller's "
            "declared turns", checks)
    checks["script_preserved"] = True

    # (2b) the render did not alter the declared ground-truth facts.
    if (produced.get("facts") or {}) != doc.get("facts", {}):
        return _invalid(
            "the produced facts differ from the scenario's declared facts; the "
            "simulator must not alter the caller's ground-truth", checks)

    # (2c) the caller actually conveys each scalar ground-truth fact it holds; a
    # script that never states a declared fact has drifted from / violates it.
    spoken = "   ".join(s.get("text", "") for s in segments).lower()
    for key, value in (doc.get("facts") or {}).items():
        if isinstance(value, bool) or not isinstance(value, (str, int, float)):
            continue  # only scalar, speakable facts are checked
        if str(value).strip() and str(value).lower() not in spoken:
            return _invalid(
                f"the caller never states its declared fact {key}={value!r}; "
                "the produced script violates the scenario's ground-truth",
                checks)
    checks["facts_preserved"] = True

    spans = (produced.get("trace") or {}).get("spans") or []

    # (3) behavior within bounds: no backchannel when probability is 0; never
    # more barge-ins-as-backchannel than candidate turns.
    bc_spans = [s for s in spans if s.get("type") == "backchannel"]
    prob = float(
        ((doc["caller"].get("behavior") or {}).get("backchannels") or {})
        .get("probability", 0.0)
    )
    if prob <= 0.0 and bc_spans:
        return _invalid(
            "backchannel spans present though the scenario declares backchannel "
            "probability 0; the caller acted outside its allowed behavior",
            checks)
    if len(bc_spans) > len(want):
        return _invalid(
            "more backchannel spans than scripted turns; the caller barged in "
            "more than the behavior allows", checks)
    checks["behavior_within_bounds"] = True

    # (4) every declared perturbation (interruption) is applied as a barge-in.
    declared = (doc["caller"].get("behavior") or {}).get("interruptions") or []
    want_offsets = sorted(
        round(int(i["offset_ms"]) / 1000.0, 3) for i in declared)
    got_offsets = sorted(
        round(s.get("time_sec", -1.0), 3)
        for s in spans if s.get("type") == "caller_barge_in")
    if got_offsets != want_offsets:
        return _invalid(
            f"declared interruptions {want_offsets} were not applied as barge-in "
            f"spans {got_offsets}; the declared perturbation must be rendered",
            checks)
    checks["perturbation_applied"] = True

    # (5) OPTIONAL mock agent (gated): every declared mock tool renders as a
    # tool_call span, in order. Absent agent_mock -> this check is skipped and
    # the verdict is byte-identical to a scenario without a mock.
    agent_mock = doc.get("agent_mock")
    if agent_mock:
        want_tools = [t["name"] for t in (agent_mock.get("tools") or [])]
        got_tools = [s.get("name") for s in spans if s.get("type") == "tool_call"]
        if got_tools != want_tools:
            return _invalid(
                f"agent_mock declared tools {want_tools} but the produced "
                f"tool_call spans are {got_tools}; the mock agent's declared "
                "tool calls must be rendered", checks)
        checks["agent_mock_rendered"] = True

    return {
        "ok": True,
        "status": "ok",
        "reason": ("the simulation preserved the scenario facts, stayed within "
                   "allowed behavior, did not solve the task for the agent, and "
                   "applied the declared perturbation"),
        "checks": checks,
        "scenario_id": doc["id"],
    }


# =========================================================================
# expand: the variation matrix -> concrete runs with deterministic seeds
# =========================================================================

def _fmt_rate(x: Any) -> str:
    """A stable string form of a speaking-rate value for the seed key, so
    1.1 and 1.10 hash identically and the seed never drifts on float repr."""
    return f"{float(x):.6f}"


def _seed_for(scenario_id: str, variation: Dict[str, Any], base: int) -> int:
    """A deterministic per-run seed = int(sha256(scenario_id + base + variation
    -tuple)). NO Math.random, NO time -- the same expansion always mints the
    same seeds, on any machine.

    The ``variables`` binding and branch ``path`` are folded in ONLY when the
    scenario declares them, so a scenario with neither hashes byte-identically to
    before those axes existed (the pre-existing per-run seeds are unchanged); a
    branched/variabled scenario gets a distinct, stable seed per (binding, path)."""
    parts = [
        scenario_id,
        f"base={int(base)}",
        f"locale={variation['locale']}",
        f"rate={_fmt_rate(variation['speaking_rate'])}",
        f"noise={variation['noise']}",
        f"behavior={variation['behavior']}",
        f"rep={int(variation['repetition'])}",
    ]
    binding = variation.get("variables")
    if binding:
        parts.append(
            "vars=" + ",".join(f"{k}={binding[k]}" for k in sorted(binding))
        )
    path = variation.get("path")
    if path:
        parts.append("path=" + ">".join(path))
    key = "|".join(parts)
    return int(hashlib.sha256(key.encode("utf-8")).hexdigest()[:8], 16)


def _apply_variation(doc: Dict[str, Any], variation: Dict[str, Any]) -> Dict[str, Any]:
    """A concrete scenario for one variation cell: the base scenario with its
    environment locale/noise and caller speaking_rate overridden, the
    behavior-variant label recorded, the cell's ``variables`` binding substituted
    into every caller ``say`` line, and -- for a branched scenario -- the cell's
    root-to-leaf ``path`` lines appended to the caller script. Deep-copied so the
    base is never mutated.

    The consumed ``variables``/``branches`` blocks are STRIPPED from the concrete
    doc: after expansion each cell is a plain scenario whose caller script already
    carries its bound, branch-selected turns, so :func:`render` and
    :func:`validate_simulation` see a self-consistent single-path caller (the
    facts-stating and script-verbatim invariants hold per concrete cell)."""
    concrete = copy.deepcopy(doc)
    env = dict(concrete.get("environment") or {})
    env["locale"] = variation["locale"]
    env["noise"] = variation["noise"]
    concrete["environment"] = env
    caller = dict(concrete["caller"])
    behavior = dict(caller.get("behavior") or {})
    behavior["speaking_rate"] = variation["speaking_rate"]
    behavior["behavior_variant"] = variation["behavior"]
    caller["behavior"] = behavior

    binding = variation.get("variables") or {}
    path = variation.get("path")
    script = [dict(t) for t in caller.get("script") or []]
    if binding:
        for turn in script:
            turn["say"] = _scn.substitute_variables(turn.get("say", ""), binding)
    if path:
        nodes = (doc.get("branches") or {}).get("nodes") or {}
        for node_name in path:
            for line in _scn.node_say_lines(nodes[node_name]):
                script.append({"say": _scn.substitute_variables(line, binding)})
    caller["script"] = script
    concrete["caller"] = caller
    # The axes are consumed into the concrete script; drop them so the per-cell
    # doc re-validates and renders as an ordinary single-path scenario.
    concrete.pop("variables", None)
    concrete.pop("branches", None)
    return concrete


def expand(scenario: Dict[str, Any]) -> List[Dict[str, Any]]:
    """Expand a scenario into a list of concrete runs, each
    ``{"scenario", "seed", "variation", "scenario_id"}``.

    The axes, crossed in a fixed order: the optional ``variables`` bindings
    (outermost), the optional branch ``path`` (every root-to-leaf path through
    ``branches``), then the ``variation_matrix`` (locale x speaking_rate x noise
    x behavior x repetitions). Each ``variation_matrix`` dimension the matrix
    omits collapses to the scenario's single declared value, and a scenario with
    NEITHER ``variables`` nor ``branches`` collapses those two axes to a single
    pass-through cell -- so a plain scenario expands to EXACTLY the same runs
    (same count, same per-run seeds, same 5-key variation dicts) as before those
    axes existed. A branched/variabled scenario adds one cell per
    (binding, root-to-leaf path, matrix cell), each stamped with its ``variables``
    binding and ``path`` label.

    Each run's ``seed`` is derived by :func:`_seed_for` from
    ``sha256(scenario_id + base_seed + variation-tuple)`` -- fully deterministic
    (no Math.random, no time), so two expansions of the same scenario are
    identical, and every distinct (binding, path, matrix cell) gets a distinct,
    stable seed. The list order is fixed, so it is byte-stable too."""
    doc = _scn.validate_scenario_doc(scenario)
    vm = doc.get("variation_matrix") or {}
    env = doc.get("environment") or {}
    behavior = doc["caller"].get("behavior") or {}
    base = int(doc.get("seed", 0))

    locales = list(vm.get("locale") or [env.get("locale") or _DEFAULT_LOCALE])
    rates = list(vm.get("speaking_rate")
                 or [float(behavior.get("speaking_rate", _scn.DEFAULT_SPEAKING_RATE))])
    noises = list(vm.get("noise") or [env.get("noise") or _DEFAULT_NOISE])
    behaviors = list(vm.get("behavior") or [_DEFAULT_BEHAVIOR])
    reps = int(vm.get("repetitions", 1))

    # The two OPTIONAL axes. Each collapses to a single pass-through value when
    # its block is absent, so a plain scenario keeps its exact prior expansion and
    # its 5-key variation dict (the extra keys are stamped ONLY when declared).
    variables_declared = bool(doc.get("variables"))
    branches_declared = bool(doc.get("branches"))
    var_combos = _scn.variable_combinations(doc.get("variables") or {})
    branch_paths = (
        _scn.enumerate_branch_paths(doc["branches"])
        if branches_declared else [None]
    )

    runs: List[Dict[str, Any]] = []
    for var_combo in var_combos:
        for path in branch_paths:
            for locale in locales:
                for rate in rates:
                    for noise in noises:
                        for bvar in behaviors:
                            for rep in range(reps):
                                variation = {
                                    "locale": locale, "speaking_rate": rate,
                                    "noise": noise, "behavior": bvar,
                                    "repetition": rep,
                                }
                                if variables_declared:
                                    variation["variables"] = dict(var_combo)
                                if branches_declared:
                                    variation["path"] = list(path)
                                runs.append({
                                    "scenario_id": doc["id"],
                                    "scenario": _apply_variation(doc, variation),
                                    "seed": _seed_for(doc["id"], variation, base),
                                    "variation": variation,
                                })
    return runs


# =========================================================================
# reliability: pass@1 / pass@k / pass^k (its OWN dimension, never a blend)
# =========================================================================

def _is_pass(result: Any) -> bool:
    """A run 'passed' if it is truthy: a bool, a verdict dict with ``ok`` /
    ``passed`` True, or the string 'pass'/'ok'. In this slice a run's pass is
    whether its simulation VALIDATED (:func:`validate_simulation` ok) -- there is
    no agent to score."""
    if isinstance(result, bool):
        return result
    if isinstance(result, dict):
        if "ok" in result:
            return bool(result["ok"])
        if "passed" in result:
            return bool(result["passed"])
        if "status" in result:
            return str(result["status"]).lower() in ("ok", "pass")
        return False
    if isinstance(result, str):
        return result.strip().lower() in ("pass", "ok", "true")
    return bool(result)


def _wilson_ci(k: int, n: int, z: float = 1.96) -> Optional[Dict[str, Any]]:
    """A Wilson score 95% CI on the single-run pass rate (pass@1). Wilson, not
    normal-approx, so it stays honest at the extremes (all-pass / all-fail) and
    for small n. No SciPy -- stdlib ``math`` only."""
    if n == 0:
        return None
    phat = k / n
    denom = 1.0 + z * z / n
    center = (phat + z * z / (2.0 * n)) / denom
    margin = (z * math.sqrt((phat * (1.0 - phat) + z * z / (4.0 * n)) / n)) / denom
    return {
        "low": round(max(0.0, center - margin), 6),
        "high": round(min(1.0, center + margin), 6),
        "method": "wilson",
        "z": z,
    }


def reliability(run_results: List[Any]) -> Dict[str, Any]:
    """Compute reliability over a set of run outcomes:
    ``{"pass_at_1", "pass_at_k", "pass_caret_k", "n", "k", "passes", "ci",
    "note"}``.

    * ``pass_at_1`` -- the empirical single-run pass rate (passes / n).
    * ``pass_at_k`` -- 1.0 if AT LEAST ONE of the k runs passed, else 0.0.
    * ``pass_caret_k`` -- 1.0 if ALL k runs passed, else 0.0.

    For the scripted DETERMINISTIC caller every run has the same outcome (a
    seeded replay is byte-identical), so ``pass_caret_k == pass_at_1`` --
    expected: deterministic replay produces zero variance. pass^k earns its
    keep with the LLM caller / live agent (later slices). This is
    Reliability's OWN dimension; it is never blended into any other number,
    and there is no overall_score."""
    results = list(run_results)
    n = len(results)
    passes = sum(1 for r in results if _is_pass(r))
    note = (
        "pass^k over k=n runs; for the scripted deterministic caller a seeded "
        "replay is byte-identical, so pass^k == pass@1 because the "
        "deterministic replay produces zero run-to-run variance"
    )
    if n == 0:
        return {"pass_at_1": 0.0, "pass_at_k": 0.0, "pass_caret_k": 0.0,
                "n": 0, "k": 0, "passes": 0, "ci": None, "note": note}
    return {
        "pass_at_1": passes / n,
        "pass_at_k": 1.0 if passes >= 1 else 0.0,
        "pass_caret_k": 1.0 if passes == n else 0.0,
        "n": n,
        "k": n,
        "passes": passes,
        "ci": _wilson_ci(passes, n),
        "note": note,
    }


# =========================================================================
# run_matrix: expand() -> render+validate each run IN PARALLEL (bounded pool)
# -> optionally score against a conversation-test -> an ATTRIBUTABLE,
# reproducible per-scenario + per-variation-cell reliability summary.
# =========================================================================

def _default_workers(n_runs: int) -> int:
    """A bounded worker count: at most one thread per run, capped by a CPU-based
    ceiling (``os.cpu_count() + 4``, the same shape :class:`ThreadPoolExecutor`
    itself defaults to), never below 1. The count only affects HOW FAST the
    matrix runs, never the RESULT -- per-run seeds are pure hashes and no state
    is shared across workers, so 1 worker and 8 workers yield the identical
    summary (proven byte-for-byte by the test suite)."""
    cap = (os.cpu_count() or 1) + 4
    return max(1, min(int(n_runs), cap))


def _cell_key(variation: Dict[str, Any]) -> Dict[str, Any]:
    """The variation CELL a run belongs to: its variation tuple WITHOUT the
    repetition index (the repetitions of one cell are the k samples pass^k is
    computed over). A stable, JSON-serializable mapping. The ``variables``
    binding and branch ``path`` are part of the cell identity ONLY when the
    scenario declared them -- so a plain scenario's cell keeps exactly its four
    keys and its summary is byte-identical to before those axes existed."""
    cell = {
        "locale": variation["locale"],
        "speaking_rate": variation["speaking_rate"],
        "noise": variation["noise"],
        "behavior": variation["behavior"],
    }
    if "variables" in variation:
        cell["variables"] = variation["variables"]
    if "path" in variation:
        cell["path"] = variation["path"]
    return cell


def _cell_sort_key(cell: Dict[str, Any]) -> tuple:
    """Total order over cells so the summary's ``variation_cells`` list is
    byte-stable regardless of dict/iteration order or worker completion order.
    The optional ``variables``/``path`` suffix is empty for a plain scenario, so
    its cell ordering is unchanged."""
    vars_key = tuple(
        sorted((k, str(v)) for k, v in (cell.get("variables") or {}).items())
    )
    path_key = tuple(cell.get("path") or ())
    return (cell["locale"], float(cell["speaking_rate"]), cell["noise"],
            cell["behavior"], vars_key, path_key)


def _score_produced(
    ct_doc: Dict[str, Any], produced: Dict[str, Any], policy: str,
    *, state_adapter: Any = None,
) -> Dict[str, Any]:
    """Score one produced simulated conversation against a conversation-test's
    DETERMINISTIC lane -- the SAME Phase-1 assert layer, over a context built
    from the produced transcript + trace (never any live agent). Returns a
    compact per-run score ``{exit_code, status, summary}`` where ``status`` is a
    plain rollup (``fail`` > ``inconclusive`` > ``pass``) and ``exit_code``
    honors the test's ``inconclusive_policy`` exactly as
    :func:`hotato.assert_.envelope_from_results` does. The rubric lane is NOT
    touched here -- it is the quarantined, model-judged capability, never scored
    on this deterministic path."""
    ctx = _assert.build_context(
        transcript=produced["transcript"]["segments"],
        spans=produced["trace"]["spans"],
        state_adapter=state_adapter,
    )
    det_list = list((ct_doc.get("assertions") or {}).get("deterministic") or [])
    if det_list:
        env = _assert.run_assertions(
            {"version": 1, "assertions": det_list, "inconclusive_policy": policy},
            ctx, inconclusive_policy=policy,
        )
    else:
        # An empty deterministic lane is a valid, honest run (nothing to check);
        # run_assertions rejects an empty list, so serve it directly.
        env = _assert.envelope_from_results([], inconclusive_policy=policy)
    det = env["summary"]["deterministic"]
    if det["fail"]:
        status = "fail"
    elif det["inconclusive"]:
        status = "inconclusive"
    else:
        status = "pass"
    return {"exit_code": env["exit_code"], "status": status, "summary": det}


def _run_one(
    index: int, run_id: str, run: Dict[str, Any], *,
    out_dir: Optional[str], created_at: str,
    ct_doc: Optional[Dict[str, Any]], policy: Optional[str], agent_id: str,
    state_adapter: Any = None,
) -> Dict[str, Any]:
    """Render + validate (+ optionally score + write) ONE concrete run. Pure
    with respect to every OTHER run: it reads only its own ``run`` (a fixed
    (scenario, seed) whose seed is a pure hash from :func:`expand`), writes only
    into its OWN ``out_dir/<run_id>/`` subdir, and shares no mutable state -- so
    the pool may run these in any order and any worker count without changing a
    single byte of the returned record."""
    produced = render(run["scenario"], run["seed"])
    verdict = validate_simulation(run["scenario"], produced)

    artifact_path: Optional[str] = None
    conversation_id: Optional[str] = None
    if out_dir is not None:
        artifact_path = os.path.join(out_dir, run_id)
        # EVERY produced conversation is labelled origin=simulated (write_artifact
        # refuses any other origin), written under its own per-run subdir.
        manifest = write_artifact(
            produced, artifact_path, created_at=created_at,
            agent_id=agent_id, conversation_id=run_id,
        )
        conversation_id = manifest["conversation_id"]

    rec: Dict[str, Any] = {
        "run_id": run_id,
        "index": index,
        "seed": run["seed"],
        "variation": run["variation"],
        "content_hash": produced["content_hash"],
        "origin_kind": produced["origin"]["kind"],
        "valid": bool(verdict["ok"]),
        "simulation_status": verdict["status"],
        "artifact": artifact_path,
        "conversation_id": conversation_id,
    }
    # A SIMULATOR_INVALID run carries its reason (a broken FIXTURE) and is NEVER
    # scored as an agent PASS/FAIL -- it is bucketed separately by run_matrix.
    if not verdict["ok"]:
        rec["reason"] = verdict["reason"]
    elif ct_doc is not None:
        rec["score"] = _score_produced(ct_doc, produced, policy,
                                       state_adapter=state_adapter)
    return rec


def run_matrix(
    scenario: Dict[str, Any],
    *,
    conversation_test: Optional[Dict[str, Any]] = None,
    out_dir: Optional[str] = None,
    max_workers: Optional[int] = None,
    created_at: Optional[str] = None,
) -> Dict[str, Any]:
    """Run a scenario's FULL variation matrix in parallel -- the Phase-2
    "simulate hundreds of scenarios" deterministic exit -- and return a
    reproducible, ATTRIBUTABLE summary.

    :func:`expand` turns the ``variation_matrix`` into concrete runs (each a
    fixed ``(scenario, seed)`` with a pure-hash per-run seed); every run is
    rendered with :func:`run_scripted`'s scripted caller IN a bounded
    :class:`concurrent.futures.ThreadPoolExecutor` (``max_workers`` defaults to a
    CPU-based cap). Each produced conversation is labelled ``origin=simulated``
    and, when ``out_dir`` is given, written under ``out_dir/<run-id>/`` as a
    ``hotato.conversation.v1`` artifact. When ``conversation_test`` is given,
    each produced simulated conversation is scored against its DETERMINISTIC
    assertions (a context built from the produced transcript/trace; the SAME
    Phase-1 assert layer; NO live agent) into a per-run pass/fail/inconclusive.

    DETERMINISM UNDER PARALLELISM (scoped: "same scenario -> same seeds ->
    byte-identical summary AND byte-identical written artifacts", never "the model
    is deterministic" -- there is no model): per-run seeds are pure hashes from
    :func:`expand`, no mutable state is shared across workers, and every collected
    result is SORTED deterministically (runs by index, cells by variation tuple)
    before the summary is built. The returned summary is therefore byte-identical
    regardless of ``max_workers`` or worker completion order. The WRITTEN
    conversation.v1 artifacts are byte-identical too: the transcript/trace carry
    no timestamp, and the manifest ``created_at`` defaults to a reproducible
    instant (``SOURCE_DATE_EPOCH``-style, never the wall clock). Pass
    ``created_at`` (an ISO-8601 string) to pin the manifest timestamp explicitly;
    omit it for the reproducible default.

    HONESTY INVARIANTS (structural): every produced artifact is
    ``origin=simulated`` (never real, never merged into a real bucket); a run
    whose :func:`validate_simulation` fails is placed in its OWN
    ``simulator_invalid`` bucket with its reason and is EXCLUDED from the agent
    reliability aggregate (a broken fixture is never an agent PASS/FAIL); the
    reliability numbers are real aggregates over the runs (never fabricated);
    there is NO ``overall_score`` / blended number anywhere in the summary.

    The summary dict::

        {"kind", "scenario_id", "total", "counts", "scored",
         "conversation_test_id", "inconclusive_policy", "reliability_basis",
         "reliability", "variation_cells": [...], "runs": [...],
         "simulator_invalid": [...], "all_simulated": bool, "exit_code"}

    ``exit_code`` is non-zero when a scored aggregate has a failure under the
    test's ``inconclusive_policy`` (0/1/2, the refuse-precedence honored) OR when
    any run is ``SIMULATOR_INVALID`` (a broken fixture -> exit 1, as
    ``hotato simulate`` reports it); else 0."""
    doc = _scn.validate_scenario_doc(scenario)
    ct_doc = (
        _ct.validate_conversation_test_doc(conversation_test)
        if conversation_test is not None else None
    )
    scored = ct_doc is not None
    policy = ct_doc["inconclusive_policy"] if scored else None

    # The OPTIONAL post-call state sandbox (Authority 2) a scenario's mock agent
    # declares. Built ONCE from the immutable scenario doc and shared read-only
    # across workers (MockStateAdapter.query returns copies, never mutates), so
    # it never affects the byte-identical-under-parallelism guarantee. Absent
    # agent_mock.state -> None -> state assertions stay INCONCLUSIVE, exactly as
    # before.
    state_data = (doc.get("agent_mock") or {}).get("state")
    state_adapter = None
    if scored and state_data:
        from .state_adapter import MockStateAdapter
        state_adapter = MockStateAdapter(state_data)

    runs = expand(doc)
    workers = _default_workers(len(runs)) if max_workers is None else max(
        1, int(max_workers))

    # A single created_at stamped on every written manifest. It DEFAULTS to a
    # reproducible instant (SOURCE_DATE_EPOCH-style, never the wall clock) so two
    # seeded runs of the same matrix write byte-identical conversation.json --
    # the "simulate hundreds -> byte-identical" contract must hold for the WRITTEN
    # artifacts, not only the summary. A caller who wants the real mint time
    # passes ``created_at`` explicitly.
    created_at = deterministic_created_at(created_at)
    if out_dir is not None:
        os.makedirs(out_dir, exist_ok=True)

    work = [
        (i, f"{doc['id']}-{i:03d}", run) for i, run in enumerate(runs)
    ]

    def _worker(item):
        index, run_id, run = item
        return _run_one(
            index, run_id, run, out_dir=out_dir, created_at=created_at,
            ct_doc=ct_doc, policy=policy, agent_id="unbound",
            state_adapter=state_adapter,
        )

    # executor.map yields results in SUBMISSION order (not completion order), so
    # the collected list is already index-ordered; we still sort explicitly so
    # the invariant does not depend on that implementation detail.
    with _futures.ThreadPoolExecutor(max_workers=workers) as pool:
        records = list(pool.map(_worker, work))
    records.sort(key=lambda r: r["index"])

    valid = [r for r in records if r["valid"]]
    invalid = [r for r in records if not r["valid"]]

    def _pass_bool(rec: Dict[str, Any]) -> bool:
        # A scored run passes iff its deterministic envelope's exit_code is 0
        # (which already honors the test's inconclusive_policy). With no
        # conversation-test there is no agent to score, so a VALID simulation is
        # a vacuous pass (pass^k is honest but carries no agent signal -- see the
        # reliability note).
        if scored:
            return rec["score"]["exit_code"] == 0
        return True

    # Per-scenario reliability over the VALID runs only (SIMULATOR_INVALID runs
    # are bucketed, never a pass/fail here).
    scenario_reliability = reliability([_pass_bool(r) for r in valid])

    # Per-variation-cell reliability: group the valid runs by their cell (the
    # variation tuple minus the repetition) and compute pass@1/pass@k/pass^k over
    # each cell's repetitions. Sorted by cell tuple for a byte-stable list.
    cells: Dict[tuple, Dict[str, Any]] = {}
    for r in valid:
        cell = _cell_key(r["variation"])
        key = _cell_sort_key(cell)
        bucket = cells.setdefault(key, {"cell": cell, "passes": []})
        bucket["passes"].append(_pass_bool(r))
    variation_cells = [
        {"cell": cells[key]["cell"], "runs": len(cells[key]["passes"]),
         "reliability": reliability(cells[key]["passes"])}
        for key in sorted(cells)
    ]

    # ATTRIBUTABLE simulator_invalid bucket: every broken fixture, mapped to its
    # variation tuple + seed + reason + artifact path. NEVER an agent PASS/FAIL.
    simulator_invalid = [
        {"run_id": r["run_id"], "index": r["index"], "seed": r["seed"],
         "variation": r["variation"], "reason": r["reason"],
         "artifact": r["artifact"]}
        for r in invalid
    ]

    # Exit code: a scored aggregate's worst per-run exit (0/1/2, refuse-precedence
    # honored), raised to >=1 when any fixture is SIMULATOR_INVALID (a broken
    # fixture, exactly as `hotato simulate` gates it).
    scored_exit = 0
    if scored:
        scored_exit = max((r["score"]["exit_code"] for r in valid), default=0)
    exit_code = max(scored_exit, 1 if invalid else 0)

    if scored:
        basis = "agent_deterministic"
        rel_note = (
            "reliability is the agent's DETERMINISTIC pass rate over the valid "
            "simulations; SIMULATOR_INVALID runs are excluded (bucketed as broken "
            "fixtures, never an agent PASS/FAIL)"
        )
    else:
        basis = "none_scored"
        rel_note = (
            "no conversation-test scored: each VALID simulation is one sample but "
            "NO agent was scored, so pass^k is vacuous (there is nothing to fail); "
            "pass --conversation-test to score an agent"
        )

    return {
        "kind": MATRIX_KIND,
        "scenario_id": doc["id"],
        "total": len(records),
        "counts": {
            "runs": len(records),
            "valid": len(valid),
            "simulator_invalid": len(invalid),
            "scored": len(valid) if scored else 0,
        },
        "scored": scored,
        "conversation_test_id": ct_doc["id"] if scored else None,
        "inconclusive_policy": policy,
        "reliability_basis": basis,
        "reliability": scenario_reliability,
        "reliability_note": rel_note,
        "variation_cells": variation_cells,
        "runs": records,
        "simulator_invalid": simulator_invalid,
        # EVERY produced conversation is origin=simulated -- never real.
        "all_simulated": all(r["origin_kind"] == "simulated" for r in records),
        "exit_code": exit_code,
    }
