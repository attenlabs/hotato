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
  byte-identical, so every run has the same outcome) -- that is CORRECT and
  honest, not fabricated variance.
* Per-run seeds are derived by :func:`expand` from ``sha256(scenario_id +
  variation-tuple)`` -- no ``Math.random``, no time-based seed. The seeded PRNG
  reuses :mod:`hotato.synth`'s LCG, the same deterministic generator that stamps
  synthetic provenance without ever raising real confidence.
"""

from __future__ import annotations

import copy
import datetime as _dt
import hashlib
import json
import math
import os
from typing import Any, Dict, List, Optional

from . import conversation as CV
from . import scenario as _scn
from . import synth as _synth
from .errors import open_regular as _open_regular  # noqa: F401  (parity import; scenario/CV own the FIFO guard)

__all__ = [
    "MODEL_ID",
    "SIMULATOR_INVALID",
    "VOICE_TRACE_SCHEMA",
    "render",
    "write_artifact",
    "run_scripted",
    "validate_simulation",
    "expand",
    "reliability",
]

# The simulator's model_id: this is a SCRIPTED caller, not a generative model.
# It rides on the conversation manifest's origin.simulator block so the artifact
# always says exactly what produced it.
MODEL_ID = "scripted"

# The status returned for a simulation that did NOT faithfully render its
# scenario. It is NEVER an agent PASS/FAIL -- a broken simulation is a broken
# fixture, not evidence about an agent.
SIMULATOR_INVALID = "SIMULATOR_INVALID"

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
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(text)


# =========================================================================
# render: scenario + seed -> the deterministic caller-side conversation
# =========================================================================

def _u01(rng) -> float:
    """One draw in [0, 1) from :func:`hotato.synth._lcg` (which yields [-1, 1)).
    Reusing synth's seeded LCG keeps every deterministic generator in hotato on
    the SAME provenance-safe PRNG -- no os.urandom, no time seed."""
    return (next(rng) + 1.0) / 2.0


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
    same seeds, on any machine."""
    key = "|".join([
        scenario_id,
        f"base={int(base)}",
        f"locale={variation['locale']}",
        f"rate={_fmt_rate(variation['speaking_rate'])}",
        f"noise={variation['noise']}",
        f"behavior={variation['behavior']}",
        f"rep={int(variation['repetition'])}",
    ])
    return int(hashlib.sha256(key.encode("utf-8")).hexdigest()[:8], 16)


def _apply_variation(doc: Dict[str, Any], variation: Dict[str, Any]) -> Dict[str, Any]:
    """A concrete scenario for one variation cell: the base scenario with its
    environment locale/noise and caller speaking_rate overridden, and the
    behavior-variant label recorded. Deep-copied so the base is never mutated."""
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
    concrete["caller"] = caller
    return concrete


def expand(scenario: Dict[str, Any]) -> List[Dict[str, Any]]:
    """Expand a scenario's ``variation_matrix`` (locale x speaking_rate x noise
    x behavior x repetitions) into a list of concrete runs, each
    ``{"scenario", "seed", "variation", "scenario_id"}``.

    A dimension the matrix omits collapses to the scenario's single declared
    value, so expand ALWAYS yields at least one run (a matrix-less scenario ->
    one run). Each run's ``seed`` is derived by :func:`_seed_for` from
    ``sha256(scenario_id + base_seed + variation-tuple)`` -- fully deterministic
    (no Math.random, no time), so two expansions of the same scenario are
    identical. The iteration order is fixed (locale, then speaking_rate, then
    noise, then behavior, then repetition), so the list order is stable too."""
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

    runs: List[Dict[str, Any]] = []
    for locale in locales:
        for rate in rates:
            for noise in noises:
                for bvar in behaviors:
                    for rep in range(reps):
                        variation = {
                            "locale": locale, "speaking_rate": rate,
                            "noise": noise, "behavior": bvar, "repetition": rep,
                        }
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
    seeded replay is byte-identical), so ``pass_caret_k == pass_at_1`` -- that is
    CORRECT and honest, not fabricated variance. pass^k earns its keep with the
    LLM caller / live agent (later slices). This is Reliability's OWN dimension;
    it is never blended into any other number, and there is no overall_score."""
    results = list(run_results)
    n = len(results)
    passes = sum(1 for r in results if _is_pass(r))
    note = (
        "pass^k over k=n runs; for the scripted deterministic caller a seeded "
        "replay is byte-identical, so pass^k == pass@1 -- reported honestly, "
        "not fabricated variance"
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
