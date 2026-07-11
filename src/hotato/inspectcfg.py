"""Level 1 of the guarded fix ladder: inspect the CURRENT turn-taking config.

``hotato inspect`` fetches (Vapi, Retell) or statically parses (LiveKit,
Pipecat) the turn-taking configuration an agent is actually running and
normalizes it into ONE internal model:

    {"stack": ..., "target": ..., "fetched_at_provenance": {...},
     "turn_taking": {
         "interrupt_min_words":      int | None,
         "interrupt_voice_seconds":  float | None,
         "resume_backoff_seconds":   float | None,
         "endpointing_wait_seconds": float | None,
         "backchannel_aware":        bool | None,
         "raw": {the raw fields exactly as found}}}

Read-only by construction: the only network calls are GETs, nothing is ever
written back to any platform, and the static parsers never import or execute
the target file. Unknown or absent options normalize to null with a note;
values are never guessed. Suspicious values are surfaced as OBSERVATIONS,
never judgments.

Field-name basis (recorded per result in ``fetched_at_provenance``):

* Vapi: GET https://api.vapi.ai/assistant/{id} (Bearer VAPI_API_KEY).
  startSpeakingPlan.waitSeconds (range 0-5, default 0.4),
  startSpeakingPlan.smartEndpointingPlan, stopSpeakingPlan.numWords (0-10,
  default 0), stopSpeakingPlan.voiceSeconds (0-0.5, default 0.2),
  stopSpeakingPlan.backoffSeconds (0-10, default 1.0),
  stopSpeakingPlan.acknowledgementPhrases. Verified against
  docs.vapi.ai/api-reference/assistants/get and
  docs.vapi.ai/customization/speech-configuration, 2026-07-06.
* Retell: GET https://api.retellai.com/get-agent/{agent_id} (Bearer
  RETELL_API_KEY). responsiveness (0-1, default 1), interruption_sensitivity
  (0-1, default 1), enable_backchannel (default false), backchannel_frequency
  (0-1, default 0.8), backchannel_words, begin_message_delay_ms (0-5000).
  Verified against docs.retellai.com/api-references/get-agent, 2026-07-06.
* LiveKit Agents: AgentSession(..., turn_handling=TurnHandlingOptions(
  interruption=InterruptionOptions(min_duration=0.5, min_words=0,
  false_interruption_timeout=2.0, mode=...),
  endpointing=EndpointingOptions(min_delay=0.5, max_delay=3.0))). Verified
  against docs.livekit.io/agents/build/turns/ and
  docs.livekit.io/reference/agents/turn-handling-options, 2026-07-06. The
  prior-generation flat kwargs (min_interruption_duration,
  min_interruption_words, min_endpointing_delay, false_interruption_timeout)
  are parsed best-effort but are absent from current docs.
* Pipecat: UserTurnStrategies(start=[MinWordsUserTurnStartStrategy(min_words=N),
  VADUserTurnStartStrategy(), KrispVivaIPUserTurnStartStrategy(...)],
  stop=[SpeechTimeoutUserTurnStopStrategy(user_speech_timeout=0.6)]) passed via
  LLMUserAggregatorParams(user_turn_strategies=...), plus
  VADParams(confidence=0.7, start_secs=0.2, stop_secs=0.8). Verified against
  docs.pipecat.ai/server/utilities/turn-management/user-turn-strategies and
  reference-server.pipecat.ai, 2026-07-06.
"""

from __future__ import annotations

from .errors import open_regular as _open_regular

import ast
import json

from . import errors as _errors
import os
from datetime import datetime, timezone
from typing import Optional

INSPECT_STACKS = ("vapi", "retell", "livekit", "pipecat")

_NORMALIZED_FIELDS = (
    "interrupt_min_words",
    "interrupt_voice_seconds",
    "resume_backoff_seconds",
    "endpointing_wait_seconds",
    "backchannel_aware",
)

_FIELD_BASIS = {
    "vapi": (
        "startSpeakingPlan/stopSpeakingPlan field names and ranges verified "
        "against docs.vapi.ai/api-reference/assistants/get and "
        "docs.vapi.ai/customization/speech-configuration, 2026-07-06"
    ),
    "retell": (
        "agent field names and ranges verified against "
        "docs.retellai.com/api-references/get-agent, 2026-07-06"
    ),
    "livekit": (
        "InterruptionOptions/EndpointingOptions names verified against "
        "docs.livekit.io/agents/build/turns/ and "
        "docs.livekit.io/reference/agents/turn-handling-options, 2026-07-06; "
        "prior-generation flat kwargs parsed best-effort"
    ),
    "pipecat": (
        "turn strategy class and parameter names verified against "
        "docs.pipecat.ai/server/utilities/turn-management/user-turn-strategies "
        "and reference-server.pipecat.ai, 2026-07-06"
    ),
}

# Suspicious-value OBSERVATIONS over the normalized model. Surfaced as
# observations only, never judgments: the threshold that is wrong for one
# deployment is deliberate in another.
_OBSERVATION_RULES = (
    (
        "interrupt_min_words",
        3,
        "interrupt_min_words is {v}. One and two word interruptions such as "
        "'stop' or 'wait' fall below this threshold and will not interrupt. "
        "Observation only; verify against your traffic.",
    ),
    (
        "interrupt_voice_seconds",
        1.0,
        "interrupt_voice_seconds is {v}. The caller must speak this long over "
        "the agent before it stops, which reads as talk-over in recordings. "
        "Observation only.",
    ),
    (
        "endpointing_wait_seconds",
        2.0,
        "endpointing_wait_seconds is {v}. Expect measurable dead air between "
        "the caller finishing and the agent responding. Observation only.",
    ),
    (
        "resume_backoff_seconds",
        5.0,
        "resume_backoff_seconds is {v}. After yielding, the agent waits this "
        "long before resuming, which can read as a stall. Observation only.",
    ),
)


def _observations(turn_taking: dict) -> list:
    out = []
    for field, threshold, template in _OBSERVATION_RULES:
        value = turn_taking.get(field)
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            if value >= threshold:
                out.append(template.format(v=value))
    return out


def _now_utc() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _result(
    *,
    stack: str,
    target: dict,
    method: str,
    turn_taking: dict,
    notes: list,
) -> dict:
    for field in _NORMALIZED_FIELDS:
        turn_taking.setdefault(field, None)
    return {
        "tool": "hotato",
        "kind": "inspect",
        "schema_version": "1",
        "stack": stack,
        "target": target,
        "fetched_at_provenance": {
            "fetched_at": _now_utc(),
            "method": method,
            "read_only": True,
            "field_basis": _FIELD_BASIS[stack],
        },
        "turn_taking": turn_taking,
        "observations": _observations(turn_taking),
        "notes": notes,
    }


# --- tiny stdlib HTTP (read-only GET; mirrors capture's conventions) --------

def _http_get_json(url: str, headers: Optional[dict] = None, timeout: int = 30) -> dict:
    import urllib.error
    import urllib.request

    _h = dict(headers or {})
    try:
        from . import __version__ as _v
    except Exception:  # pragma: no cover
        _v = "0"
    _h.setdefault("User-Agent", f"hotato/{_v} (+https://hotato.dev)")
    req = urllib.request.Request(url, headers=_h)
    if req.get_method() != "GET":  # pragma: no cover - Request with no data is GET
        raise ValueError("inspect only issues read-only GET requests")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:  # nosec - user-supplied API host
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        body = ""
        try:
            body = exc.read().decode("utf-8", "replace")[:300]
        except Exception:
            pass
        raise ValueError(
            f"HTTP {exc.code} from {url}: {exc.reason}. {body}".strip()
        ) from exc
    except urllib.error.URLError as exc:  # pragma: no cover - live path
        raise ValueError(f"network error fetching {url}: {exc.reason}") from exc


def _require_object(value, what: str) -> dict:
    """These endpoints return a single JSON OBJECT. A proxy/error page, a wrong
    ``--base-url``, or a vendor failure can return a JSON array/string/null, which
    ``json.loads`` accepts and which then crashes on the first ``.get()`` /
    indexing with a raw AttributeError/TypeError. Reject a non-object as a clean
    usage error instead."""
    if not isinstance(value, dict):
        raise ValueError(
            f"expected a JSON object for {what}, got {type(value).__name__}. The "
            "endpoint returned an unexpected shape (a proxy/error page, a wrong "
            "--base-url, or a vendor failure response)."
        )
    return value


def _num(value):
    """A number exactly as reported, or None; never coerced from strings."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return value


# --- Vapi: GET /assistant/{id} ----------------------------------------------

def inspect_vapi(
    *,
    assistant_id: str,
    api_key: str,
    base_url: str = "https://api.vapi.ai",
    timeout: int = 30,
) -> dict:
    url = f"{base_url.rstrip('/')}/assistant/{assistant_id}"
    assistant = _require_object(
        _http_get_json(
            url,
            headers={"Authorization": f"Bearer {api_key}", "Accept": "application/json"},
            timeout=timeout,
        ),
        f"Vapi assistant {assistant_id!r}",
    )
    start = assistant.get("startSpeakingPlan") or {}
    stop = assistant.get("stopSpeakingPlan") or {}
    notes = []

    ack = stop.get("acknowledgementPhrases")
    if isinstance(ack, list):
        backchannel_aware = len(ack) > 0
    else:
        backchannel_aware = None
        notes.append(
            "stopSpeakingPlan.acknowledgementPhrases absent; backchannel "
            "awareness unknown from the API payload."
        )
    for name, plan in (("startSpeakingPlan", start), ("stopSpeakingPlan", stop)):
        if not plan:
            notes.append(
                f"{name} absent from the assistant payload; the platform "
                "default applies and is not reported here (never guessed)."
            )

    turn_taking = {
        "interrupt_min_words": _num(stop.get("numWords")),
        "interrupt_voice_seconds": _num(stop.get("voiceSeconds")),
        "resume_backoff_seconds": _num(stop.get("backoffSeconds")),
        "endpointing_wait_seconds": _num(start.get("waitSeconds")),
        "backchannel_aware": backchannel_aware,
        "raw": {"startSpeakingPlan": start, "stopSpeakingPlan": stop},
    }
    for field, key, plan_name in (
        ("interrupt_min_words", "numWords", "stopSpeakingPlan"),
        ("interrupt_voice_seconds", "voiceSeconds", "stopSpeakingPlan"),
        ("resume_backoff_seconds", "backoffSeconds", "stopSpeakingPlan"),
        ("endpointing_wait_seconds", "waitSeconds", "startSpeakingPlan"),
    ):
        if turn_taking[field] is None:
            notes.append(f"{plan_name}.{key} not set on this assistant; null.")

    return _result(
        stack="vapi",
        target={"assistant_id": assistant_id},
        method=f"GET {url}",
        turn_taking=turn_taking,
        notes=notes,
    )


# --- Retell: GET /get-agent/{agent_id} ---------------------------------------

_RETELL_RAW_KEYS = (
    "responsiveness",
    "interruption_sensitivity",
    "enable_backchannel",
    "backchannel_frequency",
    "backchannel_words",
    "begin_message_delay_ms",
)


def inspect_retell(
    *,
    agent_id: str,
    api_key: str,
    base_url: str = "https://api.retellai.com",
    timeout: int = 30,
) -> dict:
    url = f"{base_url.rstrip('/')}/get-agent/{agent_id}"
    agent = _require_object(
        _http_get_json(
            url,
            headers={"Authorization": f"Bearer {api_key}", "Accept": "application/json"},
            timeout=timeout,
        ),
        f"Retell agent {agent_id!r}",
    )
    raw = {k: agent[k] for k in _RETELL_RAW_KEYS if k in agent}
    notes = [
        "Retell exposes unitless scales (responsiveness, "
        "interruption_sensitivity, both 0 to 1), not word counts or seconds. "
        "The word/second fields therefore normalize to null; the actual "
        "values are under turn_taking.raw."
    ]
    enable_backchannel = agent.get("enable_backchannel")
    if not isinstance(enable_backchannel, bool):
        enable_backchannel = None
        notes.append("enable_backchannel absent from the agent payload; null.")
    for key in ("responsiveness", "interruption_sensitivity"):
        if _num(agent.get(key)) is None:
            notes.append(f"{key} absent from the agent payload; see raw.")

    turn_taking = {
        "interrupt_min_words": None,
        "interrupt_voice_seconds": None,
        "resume_backoff_seconds": None,
        "endpointing_wait_seconds": None,
        "backchannel_aware": enable_backchannel,
        "raw": raw,
    }
    return _result(
        stack="retell",
        target={"agent_id": agent_id},
        method=f"GET {url}",
        turn_taking=turn_taking,
        notes=notes,
    )


# --- static parsing (LiveKit / Pipecat): ast over the user's config file -----

_NOT_LITERAL = object()


def _literal(node):
    """A literal constant value, or _NOT_LITERAL. Never evaluates code."""
    if isinstance(node, ast.Constant):
        return node.value
    if (
        isinstance(node, ast.UnaryOp)
        and isinstance(node.op, ast.USub)
        and isinstance(node.operand, ast.Constant)
        and isinstance(node.operand.value, (int, float))
    ):
        return -node.operand.value
    return _NOT_LITERAL


def _call_name(node: ast.Call) -> str:
    func = node.func
    parts = []
    while isinstance(func, ast.Attribute):
        parts.append(func.attr)
        func = func.value
    if isinstance(func, ast.Name):
        parts.append(func.id)
    return ".".join(reversed(parts))


def _collect_calls(source: str, path: str) -> list:
    """Every call in the file as (dotted_name, {kwarg: literal-or-marker})."""
    try:
        tree = ast.parse(source, filename=path)
    except SyntaxError as exc:
        raise ValueError(
            f"--config {path!r} is not parseable Python ({exc.msg}, line "
            f"{exc.lineno}). Inspect reads the file statically; fix the syntax "
            "or point at the real config module."
        ) from exc
    calls = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            kwargs = {
                kw.arg: _literal(kw.value)
                for kw in node.keywords
                if kw.arg is not None
            }
            calls.append((_call_name(node), node, kwargs))
    return calls


def _read_config(path: str) -> str:
    if not os.path.exists(path):
        raise FileNotFoundError(f"--config file not found: {path}")
    with _open_regular(path, "r", encoding="utf-8") as fh:
        return fh.read()


def _take(
    calls: list,
    turn_taking: dict,
    raw: dict,
    notes: list,
    *,
    field: str,
    kwarg: str,
    call_suffixes: tuple,
) -> None:
    """Pull one kwarg's literal value out of matching calls into the model.

    First match wins; a non-literal value stays null with an explicit note
    (never guessed); no match leaves the field untouched.
    """
    for name, _node, kwargs in calls:
        short = name.rsplit(".", 1)[-1]
        if call_suffixes and short not in call_suffixes:
            continue
        if kwarg not in kwargs:
            continue
        value = kwargs[kwarg]
        raw_key = f"{short}.{kwarg}"
        if value is _NOT_LITERAL:
            raw[raw_key] = None
            notes.append(
                f"{raw_key} is set but its value is not a literal; a static "
                "parse cannot read it, so it stays null (never guessed)."
            )
            return
        raw[raw_key] = value
        if turn_taking.get(field) is None and _num(value) is not None:
            turn_taking[field] = value
        return


def inspect_livekit_file(config_path: str) -> dict:
    source = _read_config(config_path)
    calls = _collect_calls(source, config_path)
    notes = []
    raw = {}
    turn_taking = {field: None for field in _NORMALIZED_FIELDS}

    # Current nested options first, then the prior-generation flat kwargs.
    _take(calls, turn_taking, raw, notes,
          field="interrupt_min_words", kwarg="min_words",
          call_suffixes=("InterruptionOptions",))
    _take(calls, turn_taking, raw, notes,
          field="interrupt_voice_seconds", kwarg="min_duration",
          call_suffixes=("InterruptionOptions",))
    _take(calls, turn_taking, raw, notes,
          field="resume_backoff_seconds", kwarg="false_interruption_timeout",
          call_suffixes=("InterruptionOptions", "AgentSession"))
    _take(calls, turn_taking, raw, notes,
          field="endpointing_wait_seconds", kwarg="min_delay",
          call_suffixes=("EndpointingOptions",))
    _take(calls, turn_taking, raw, notes,
          field="_endpointing_max", kwarg="max_delay",
          call_suffixes=("EndpointingOptions",))
    turn_taking.pop("_endpointing_max", None)
    _take(calls, turn_taking, raw, notes,
          field="interrupt_min_words", kwarg="min_interruption_words",
          call_suffixes=("AgentSession",))
    _take(calls, turn_taking, raw, notes,
          field="interrupt_voice_seconds", kwarg="min_interruption_duration",
          call_suffixes=("AgentSession",))
    _take(calls, turn_taking, raw, notes,
          field="endpointing_wait_seconds", kwarg="min_endpointing_delay",
          call_suffixes=("AgentSession",))

    modes = [
        kwargs["mode"]
        for name, _n, kwargs in calls
        if name.rsplit(".", 1)[-1] == "InterruptionOptions"
        and kwargs.get("mode") not in (None, _NOT_LITERAL)
    ]
    if modes:
        raw["InterruptionOptions.mode"] = modes[0]
        # 'adaptive' is LiveKit's classifier-backed interruption handling.
        turn_taking["backchannel_aware"] = modes[0] == "adaptive"
    else:
        notes.append(
            "InterruptionOptions.mode not set in this file; backchannel "
            "awareness unknown from a static parse (the runtime default "
            "depends on the configured turn-detector model)."
        )
    for field in _NORMALIZED_FIELDS:
        if field != "backchannel_aware" and turn_taking[field] is None:
            notes.append(
                f"{field}: no matching option found in {os.path.basename(config_path)}; "
                "null (the library default applies and is not guessed here)."
            )
    turn_taking["raw"] = raw
    return _result(
        stack="livekit",
        target={"config_path": config_path},
        method=f"static parse of {config_path} (ast, no code executed)",
        turn_taking=turn_taking,
        notes=notes,
    )


def inspect_pipecat_file(config_path: str) -> dict:
    source = _read_config(config_path)
    calls = _collect_calls(source, config_path)
    notes = []
    raw = {}
    turn_taking = {field: None for field in _NORMALIZED_FIELDS}

    _take(calls, turn_taking, raw, notes,
          field="interrupt_min_words", kwarg="min_words",
          call_suffixes=("MinWordsUserTurnStartStrategy",
                         "MinWordsInterruptionStrategy"))
    _take(calls, turn_taking, raw, notes,
          field="endpointing_wait_seconds", kwarg="user_speech_timeout",
          call_suffixes=("SpeechTimeoutUserTurnStopStrategy",))
    _take(calls, turn_taking, raw, notes,
          field="interrupt_voice_seconds", kwarg="start_secs",
          call_suffixes=("VADParams",))
    if turn_taking["endpointing_wait_seconds"] is None:
        _take(calls, turn_taking, raw, notes,
              field="endpointing_wait_seconds", kwarg="stop_secs",
              call_suffixes=("VADParams",))
    else:
        _take(calls, turn_taking, raw, notes,
              field="_vad_stop_secs", kwarg="stop_secs",
              call_suffixes=("VADParams",))
        turn_taking.pop("_vad_stop_secs", None)
    _take(calls, turn_taking, raw, notes,
          field="_vad_confidence", kwarg="confidence",
          call_suffixes=("VADParams",))
    turn_taking.pop("_vad_confidence", None)

    strategy_classes = sorted(
        {
            name.rsplit(".", 1)[-1]
            for name, _n, _k in calls
            if name.rsplit(".", 1)[-1].endswith(
                ("UserTurnStartStrategy", "UserTurnStopStrategy",
                 "InterruptionStrategy")
            )
        }
    )
    if strategy_classes:
        raw["turn_strategy_classes"] = strategy_classes
    classifier = [c for c in strategy_classes if c.startswith("KrispViva")]
    if classifier:
        # A classifier-backed start strategy discriminates real floor bids.
        turn_taking["backchannel_aware"] = True
        raw["classifier_strategy"] = classifier[0]
    elif strategy_classes:
        turn_taking["backchannel_aware"] = False
        notes.append(
            "turn start strategies found, none classifier-backed "
            "(no KrispViva* strategy in this file)."
        )
    else:
        notes.append(
            "no turn strategy classes found in this file; Pipecat's defaults "
            "apply and are not guessed here."
        )
    for field in _NORMALIZED_FIELDS:
        if field != "backchannel_aware" and turn_taking[field] is None:
            notes.append(
                f"{field}: no matching option found in {os.path.basename(config_path)}; "
                "null (the library default applies and is not guessed here)."
            )
    turn_taking["raw"] = raw
    return _result(
        stack="pipecat",
        target={"config_path": config_path},
        method=f"static parse of {config_path} (ast, no code executed)",
        turn_taking=turn_taking,
        notes=notes,
    )


# --- entry point --------------------------------------------------------------

def run_inspect(
    *,
    stack: str,
    assistant_id: Optional[str] = None,
    agent_id: Optional[str] = None,
    config: Optional[str] = None,
    api_key: Optional[str] = None,
) -> dict:
    """Dispatch one inspection. Raises ValueError / FileNotFoundError for a
    usage problem (the CLI surfaces those as exit code 2)."""
    stack = (stack or "").strip().lower()
    if stack == "vapi":
        if not assistant_id:
            raise ValueError("--stack vapi needs --assistant-id <id>")
        key = api_key or os.environ.get("VAPI_API_KEY")
        if not key:
            raise ValueError(
                "no Vapi credentials: pass --api-key or set VAPI_API_KEY. "
                "Inspect only issues a read-only GET."
            )
        return inspect_vapi(assistant_id=assistant_id, api_key=key)
    if stack == "retell":
        if not agent_id:
            raise ValueError("--stack retell needs --agent-id <id>")
        key = api_key or os.environ.get("RETELL_API_KEY")
        if not key:
            raise ValueError(
                "no Retell credentials: pass --api-key or set RETELL_API_KEY. "
                "Inspect only issues a read-only GET."
            )
        return inspect_retell(agent_id=agent_id, api_key=key)
    if stack in ("livekit", "pipecat"):
        if not config:
            raise ValueError(
                f"--stack {stack} needs --config <file.py> (the agent config "
                "to parse statically; nothing is imported or executed)"
            )
        if stack == "livekit":
            return inspect_livekit_file(config)
        return inspect_pipecat_file(config)
    raise ValueError(
        f"unknown --stack {stack!r}; inspect supports: {', '.join(INSPECT_STACKS)}"
    )


def render_text(result: dict) -> str:
    tt = result["turn_taking"]
    prov = result["fetched_at_provenance"]
    lines = [
        f"hotato inspect [{result['stack']}] target={result['target']}",
        f"  source: {prov['method']}  (read-only, {prov['fetched_at']})",
        "  turn_taking:",
    ]
    for field in _NORMALIZED_FIELDS:
        value = tt.get(field)
        lines.append(f"    {field} = {'null' if value is None else value}")
    if tt.get("raw"):
        lines.append(f"    raw: {_errors.safe_json_dumps(tt['raw'], sort_keys=True)}")
    for obs in result["observations"]:
        lines.append(f"  observation: {obs}")
    for note in result["notes"]:
        lines.append(f"  note: {note}")
    return "\n".join(lines)
