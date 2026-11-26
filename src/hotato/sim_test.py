"""``hotato test run <cases> --agent <target>``: persona-driven simulated
conversations with goals, per-criterion verdicts, repeated runs and a pass rate.

Shape of one run:

  * a CASE file (YAML when PyYAML is importable, else JSON) names a persona, a
    goal, plain-language success criteria and optional forbidden behaviours and
    expected tool calls;
  * an LLM plays the caller turn by turn toward the goal against a chat AGENT
    TARGET (the hotato chat HTTP contract, Vapi's chat API, or Retell's chat
    API) and ends the conversation when the goal is reached, impossible, or
    ``max_turns`` is hit;
  * a JUDGE LLM returns one typed verdict per criterion and per forbidden item.
    A verdict is ``pass`` / ``fail`` / ``unknown``; a ``pass`` on a criterion
    needs an evidence quote that really occurs in the transcript, otherwise it
    is downgraded to ``unknown``. Expected tool calls are checked
    deterministically when the target reports tool calls;
  * each case repeats N times; the result reports pass_rate per criterion and
    per case plus ``pass_all`` (passed on every repeat, the pass^k idea).

The result document is ``hotato.testrun.v1``.

EGRESS: nothing leaves the machine by default. The default LLM is a local
Ollama; any non-local LLM base URL, Anthropic, or Gemini needs
``--judge-egress-opt-in``; any non-local chat URL, and the Vapi / Retell
targets, need ``--egress-opt-in``. Refusals happen before any request.

The core is stdlib-only; PyYAML is imported lazily and only for YAML case
files.
"""

from __future__ import annotations

import json
import os
import re
import secrets
import time
from datetime import datetime, timezone
from html import escape
from typing import Any, Dict, List, Optional, Sequence, Tuple

from . import rubric as _rubric
from .errors import open_regular as _open_regular

__all__ = [
    "SCHEMA", "LLM", "AgentTarget", "AgentReply", "SimError", "load_cases",
    "parse_case", "make_llm", "make_agent", "run_case", "run_cases",
    "judge_run", "check_tool_calls", "case_from_transcript", "starter_case",
    "render_text", "render_html", "dump_case",
]

SCHEMA = "hotato.testrun.v1"
DEFAULT_MAX_TURNS = 12
DEFAULT_REPEAT = 3
END_REACHED = "[END:goal_reached]"
END_IMPOSSIBLE = "[END:impossible]"
_UA = "hotato-test-run"
_CASE_KEYS = {"name", "persona", "goal", "success_criteria", "forbidden",
              "expected_tool_calls", "max_turns", "repeat", "variables"}
_HTTP_TIMEOUT = 120.0


class SimError(ValueError):
    """Bad input or no usable judge: the CLI's exit-2 path."""


class LLMError(SimError):
    """The caller/judge LLM failed (unreachable, bad key, unknown model)."""




# =========================================================================
# Case files
# =========================================================================

def _parse_text(text: str, path: str) -> Any:
    stripped = text.lstrip()
    if stripped[:1] in ("{", "["):
        try:
            return json.loads(text)
        except json.JSONDecodeError as exc:
            raise SimError(f"{path}: not valid JSON ({exc})") from exc
    try:
        import yaml  # type: ignore
    except ImportError:
        yaml = None
    if yaml is not None:
        try:
            return yaml.safe_load(text)
        except Exception as exc:  # yaml.YAMLError and friends
            raise SimError(f"{path}: not valid YAML ({exc})") from exc
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass
    try:
        from .assert_ import parse_assertions_yaml
        return parse_assertions_yaml(text)
    except Exception as exc:
        raise SimError(
            f"{path}: could not parse as JSON, and PyYAML is not installed "
            f"for YAML ({exc}). Write the case as JSON or `pip install "
            "pyyaml`."
        ) from exc


def _subst(text: str, variables: Dict[str, str], field: str) -> str:
    def repl(m):
        key = m.group(1)
        if key not in variables:
            raise SimError(f"{field} uses {{{{{key}}}}} but variables has no {key!r}")
        return variables[key]
    return re.sub(r"\{\{\s*([A-Za-z0-9_.-]+)\s*\}\}", repl, text)


def _str_list(doc: dict, key: str, path: str) -> List[str]:
    val = doc.get(key)
    if val is None:
        return []
    if not isinstance(val, list) or not all(isinstance(v, str) and v.strip() for v in val):
        raise SimError(f"{path}: {key} must be a list of non-empty strings")
    return [v.strip() for v in val]


def parse_case(doc: Any, path: str, default_name: str = "case") -> Dict[str, Any]:
    if not isinstance(doc, dict):
        raise SimError(f"{path}: a case file must be an object")
    unknown = sorted(set(doc) - _CASE_KEYS)
    if unknown:
        raise SimError(f"{path}: unknown key(s) {', '.join(unknown)}; allowed: "
                       f"{', '.join(sorted(_CASE_KEYS))}")
    for key in ("persona", "goal"):
        if not isinstance(doc.get(key), str) or not doc[key].strip():
            raise SimError(f"{path}: {key} is required (a non-empty string)")
    variables = doc.get("variables") or {}
    if not isinstance(variables, dict):
        raise SimError(f"{path}: variables must be an object")
    variables = {str(k): str(v) for k, v in variables.items()}
    criteria = _str_list(doc, "success_criteria", path)
    forbidden = _str_list(doc, "forbidden", path)
    expected = doc.get("expected_tool_calls") or []
    if not isinstance(expected, list):
        raise SimError(f"{path}: expected_tool_calls must be a list")
    norm_expected: List[Dict[str, Any]] = []
    for item in expected:
        if isinstance(item, str) and item.strip():
            norm_expected.append({"name": item.strip(), "arguments": {}})
        elif isinstance(item, dict) and isinstance(item.get("name"), str):
            args = item.get("arguments") or {}
            if not isinstance(args, dict):
                raise SimError(f"{path}: expected_tool_calls arguments must be an object")
            norm_expected.append({"name": item["name"], "arguments": args})
        else:
            raise SimError(f"{path}: each expected_tool_calls entry is a tool "
                           "name or {name, arguments}")
    if not (criteria or forbidden or norm_expected):
        raise SimError(f"{path}: give at least one of success_criteria, "
                       "forbidden or expected_tool_calls")
    out = {}
    for key, default in (("max_turns", DEFAULT_MAX_TURNS), ("repeat", DEFAULT_REPEAT)):
        v = doc.get(key, default)
        if isinstance(v, bool) or not isinstance(v, int) or v < 1:
            raise SimError(f"{path}: {key} must be an integer >= 1")
        out[key] = v
    name = doc.get("name") or default_name
    if not isinstance(name, str) or not name.strip():
        raise SimError(f"{path}: name must be a non-empty string")
    return {
        "name": name.strip(),
        "persona": _subst(doc["persona"].strip(), variables, f"{path}: persona"),
        "goal": _subst(doc["goal"].strip(), variables, f"{path}: goal"),
        "success_criteria": criteria, "forbidden": forbidden,
        "expected_tool_calls": norm_expected,
        "max_turns": out["max_turns"], "repeat": out["repeat"],
        "variables": variables,
    }


def _slug(s: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", s.lower()).strip("-") or "case"


def load_cases(path: str) -> List[Dict[str, Any]]:
    """Load one case file or every ``*.yaml|*.yml|*.json`` in a directory."""
    if os.path.isdir(path):
        files = sorted(
            os.path.join(path, f) for f in os.listdir(path)
            if f.lower().endswith((".yaml", ".yml", ".json"))
        )
        if not files:
            raise SimError(f"{path}: no .yaml/.yml/.json case files found")
    elif os.path.isfile(path):
        files = [path]
    else:
        raise SimError(f"{path}: no such case file or directory")
    cases, seen = [], {}
    for f in files:
        with _open_regular(f, "r", encoding="utf-8") as fh:
            doc = _parse_text(fh.read(), f)
        stem = os.path.splitext(os.path.basename(f))[0]
        case = parse_case(doc, f, default_name=stem)
        cid = _slug(case["name"])
        seen[cid] = seen.get(cid, 0) + 1
        if seen[cid] > 1:
            cid = f"{cid}-{seen[cid]}"
        case["case_id"] = cid
        case["source"] = f
        cases.append(case)
    return cases


def looks_like_case_path(path: str) -> bool:
    """True when ``path`` is a directory, or a file that is a simulated-
    conversation case (not a ``hotato.conversation-test`` file)."""
    if os.path.isdir(path):
        return True
    try:
        with _open_regular(path, "r", encoding="utf-8") as fh:
            doc = _parse_text(fh.read(), path)
    except (OSError, SimError):
        return False
    return (isinstance(doc, dict) and "kind" not in doc
            and ("persona" in doc or "success_criteria" in doc))


# =========================================================================
# LLM providers (one tiny interface; transport reused from rubric.py)
# =========================================================================

class LLM:
    provider = "base"
    model = ""

    def chat(self, system: str, messages: Sequence[Dict[str, str]]) -> str:
        raise NotImplementedError

    def preflight(self) -> None:
        """Raise SimError when this LLM cannot possibly run."""


class _Guarded:
    """Wraps an LLM so its failures are LLMError (told apart from agent ones)."""

    def __init__(self, inner):
        self._i = inner
        self.provider, self.model = inner.provider, inner.model

    def chat(self, system, messages):
        try:
            return self._i.chat(system, messages)
        except LLMError:
            raise
        except SimError as exc:
            raise LLMError(str(exc)) from exc


def _post_json(url: str, payload: dict, headers: Dict[str, str], subject: str) -> dict:
    h = {"Content-Type": "application/json", "User-Agent": _UA, **headers}
    try:
        body = _rubric._urllib_json_call(
            url, data=json.dumps(payload).encode("utf-8"), headers=h,
            method="POST", timeout=_HTTP_TIMEOUT,
            unreachable_subject=subject, failed_subject=subject,
        )
        return json.loads(body)
    except _rubric.JudgeError as exc:
        raise SimError(str(exc)) from exc
    except json.JSONDecodeError as exc:
        raise SimError(f"{subject} returned non-JSON: {exc}") from exc


class OpenAICompatLLM(LLM):
    """OpenAI-compatible ``/chat/completions`` (OpenAI, Ollama's /v1, vLLM,
    many gateways)."""

    def __init__(self, provider: str, model: str, base_url: str, key: Optional[str]):
        self.provider, self.model = provider, model
        self.base_url = base_url.rstrip("/")
        self.key = key

    def preflight(self) -> None:
        if self.provider != "ollama" and not self.key and \
                not _rubric._is_local_endpoint(self.base_url):
            raise SimError("no judge API key: set HOTATO_JUDGE_API_KEY (or "
                           "OPENAI_API_KEY) for the OpenAI-compatible provider")
        if self.provider == "ollama":
            root = re.sub(r"/v1/?$", "", self.base_url)
            try:
                _rubric._urllib_json_call(
                    root + "/api/tags", data=None, headers={"User-Agent": _UA},
                    method="GET", timeout=10.0,
                    unreachable_subject="ollama", failed_subject="ollama")
            except _rubric.JudgeError as exc:
                raise SimError(
                    f"no judge configured: {exc}. Start Ollama, or choose "
                    "--judge-provider openai|anthropic|gemini with "
                    "--judge-egress-opt-in") from exc

    def chat(self, system, messages):
        headers = {"Authorization": f"Bearer {self.key}"} if self.key else {}
        payload = {"model": self.model, "temperature": 0.2,
                   "messages": [{"role": "system", "content": system}, *messages]}
        data = _post_json(f"{self.base_url}/chat/completions", payload, headers,
                          f"{self.provider} LLM")
        try:
            return str(data["choices"][0]["message"]["content"] or "")
        except (KeyError, IndexError, TypeError) as exc:
            raise SimError(f"{self.provider} LLM returned an unexpected envelope") from exc


class AnthropicLLM(LLM):
    provider = "anthropic"

    def __init__(self, model: str, key: Optional[str], base_url: str):
        self.model, self.key, self.base_url = model, key, base_url.rstrip("/")

    def preflight(self) -> None:
        if not self.key:
            raise SimError("no judge API key: set ANTHROPIC_API_KEY for "
                           "--judge-provider anthropic")

    def chat(self, system, messages):
        payload = {"model": self.model, "max_tokens": 1500, "temperature": 0.2,
                   "system": system, "messages": list(messages)}
        data = _post_json(f"{self.base_url}/v1/messages", payload,
                          {"x-api-key": self.key or "",
                           "anthropic-version": "2023-06-01"}, "anthropic LLM")
        try:
            return "".join(b.get("text", "") for b in data["content"]
                           if isinstance(b, dict) and b.get("type") == "text")
        except (KeyError, TypeError) as exc:
            raise SimError("anthropic LLM returned an unexpected envelope") from exc


class GeminiLLM(LLM):
    provider = "gemini"

    def __init__(self, model: str, key: Optional[str], base_url: str):
        self.model, self.key, self.base_url = model, key, base_url.rstrip("/")

    def preflight(self) -> None:
        if not self.key:
            raise SimError("no judge API key: set GEMINI_API_KEY (or "
                           "GOOGLE_API_KEY) for --judge-provider gemini")

    def chat(self, system, messages):
        payload = {
            "systemInstruction": {"parts": [{"text": system}]},
            "contents": [{"role": "model" if m["role"] == "assistant" else "user",
                          "parts": [{"text": m["content"]}]} for m in messages],
            "generationConfig": {"temperature": 0.2},
        }
        data = _post_json(f"{self.base_url}/v1beta/models/{self.model}:generateContent",
                          payload, {"x-goog-api-key": self.key or ""}, "gemini LLM")
        try:
            parts = data["candidates"][0]["content"]["parts"]
            return "".join(p.get("text", "") for p in parts)
        except (KeyError, IndexError, TypeError) as exc:
            raise SimError("gemini LLM returned an unexpected envelope") from exc


_DEFAULT_MODELS = {"openai": "gpt-4o-mini", "anthropic": "claude-sonnet-4-5",
                   "gemini": "gemini-2.5-flash"}


def make_llm(provider: Optional[str] = None, model: Optional[str] = None,
             base_url: Optional[str] = None, *, egress_opt_in: bool = False,
             env: Optional[Dict[str, str]] = None) -> LLM:
    """Build the caller+judge LLM. Provider: flag, else ``$HOTATO_JUDGE_PROVIDER``,
    else local ``ollama``. ``hosted`` is an alias of ``openai`` (any
    OpenAI-compatible base URL). Raises :class:`SimError` before any request when
    the choice needs egress that was not opted into."""
    env = os.environ if env is None else env
    provider = (provider or env.get("HOTATO_JUDGE_PROVIDER") or "ollama").lower()
    if provider == "hosted":
        provider = "openai"
    if provider not in ("ollama", "openai", "anthropic", "gemini"):
        raise SimError(f"unknown judge provider {provider!r}; choose ollama, "
                       "openai (hosted), anthropic or gemini")
    model = model or env.get("HOTATO_JUDGE_MODEL") or \
        _DEFAULT_MODELS.get(provider) or _rubric.DEFAULT_JUDGE_MODEL
    base_url = base_url or env.get("HOTATO_JUDGE_ENDPOINT")
    if provider == "ollama":
        root = (base_url or _rubric.DEFAULT_OLLAMA_ENDPOINT).rstrip("/")
        url, key = (root if root.endswith("/v1") else root + "/v1"), None
    elif provider == "openai":
        url = base_url or env.get("OPENAI_BASE_URL") or "https://api.openai.com/v1"
        key = env.get("HOTATO_JUDGE_API_KEY") or env.get("OPENAI_API_KEY")
    elif provider == "anthropic":
        url = base_url or "https://api.anthropic.com"
        key = env.get("ANTHROPIC_API_KEY")
    else:
        url = base_url or "https://generativelanguage.googleapis.com"
        key = env.get("GEMINI_API_KEY") or env.get("GOOGLE_API_KEY")
    if not egress_opt_in and not _rubric._is_local_endpoint(url):
        raise _rubric.EgressRefused(
            f"the {provider} LLM at {url!r} is off this machine: the simulated "
            "caller and the judge would send the conversation there. Refused "
            "before any request; pass --judge-egress-opt-in, or use a local "
            "Ollama.")
    if provider in ("ollama", "openai"):
        return OpenAICompatLLM(provider, model, url, key)
    if provider == "anthropic":
        return AnthropicLLM(model, key, url)
    return GeminiLLM(model, key, url)


# =========================================================================
# Agent targets
# =========================================================================

class AgentReply:
    def __init__(self, text: str, tool_calls: Optional[List[Dict[str, Any]]] = None):
        self.text = text
        self.tool_calls = tool_calls or []


class AgentTarget:
    stack = "base"
    id = ""
    reports_tool_calls = False

    def start(self, case: Dict[str, Any]) -> None:
        """Begin a fresh conversation for one run."""

    def send(self, text: str) -> AgentReply:
        raise NotImplementedError


class ChatHttpAgent(AgentTarget):
    """The existing ``hotato simulate --chat`` HTTP contract."""
    stack = "chat"

    def __init__(self, url: str, *, egress_opt_in: bool = False):
        from . import chat_sim
        chat_sim.check_chat_url(url, egress_opt_in=egress_opt_in)
        from .errors import sanitize_url
        self.url, self.id = url, sanitize_url(url)
        self._conv, self._turn = "", 0

    def start(self, case):
        self._conv, self._turn = f"{case['case_id']}-{secrets.token_hex(4)}", 0

    def send(self, text):
        from . import chat_sim
        reply, _ms = chat_sim._post_turn(
            self.url, {"conversation_id": self._conv, "turn_index": self._turn,
                       "text": text}, chat_sim.DEFAULT_TIMEOUT_SEC)
        self._turn += 1
        return AgentReply(reply)


def _stored_key(stack: str, env_name: str) -> str:
    key = os.environ.get(env_name)
    if not key:
        from . import connections
        key = (connections.get(stack) or {}).get("api_key")
    if not key:
        raise SimError(f"no {stack} API key: set {env_name} or run "
                       f"`hotato connect {stack}`")
    return key


class VapiChatAgent(AgentTarget):
    """Vapi chat API: ``POST https://api.vapi.ai/chat`` with ``assistantId`` +
    ``input``; later turns pass ``previousChatId``. Tool calls are not reported
    by that endpoint's documented output items."""
    stack = "vapi"

    def __init__(self, assistant_id: str, *, egress_opt_in: bool = False,
                 base_url: str = "https://api.vapi.ai"):
        _require_egress("Vapi", egress_opt_in)
        self.id, self.base_url = assistant_id, base_url.rstrip("/")
        self._key = _stored_key("vapi", "VAPI_API_KEY")
        self._prev: Optional[str] = None

    def start(self, case):
        self._prev = None

    def send(self, text):
        body: Dict[str, Any] = {"assistantId": self.id, "input": text}
        if self._prev:
            body["previousChatId"] = self._prev
        data = _post_json(f"{self.base_url}/chat", body,
                          {"Authorization": f"Bearer {self._key}"}, "vapi chat")
        self._prev = data.get("id") or self._prev
        out = data.get("output")
        if not isinstance(out, list):
            raise SimError("vapi chat response had no output list")
        parts = [str(o.get("content") or "") for o in out
                 if isinstance(o, dict) and o.get("role") == "assistant"]
        return AgentReply("\n".join(p for p in parts if p))


class RetellChatAgent(AgentTarget):
    """Retell chat API: ``POST /create-chat`` ({agent_id}) then
    ``POST /create-chat-completion`` ({chat_id, content}); the response carries
    agent messages and tool_call_invocation entries."""
    stack = "retell"
    reports_tool_calls = True

    def __init__(self, agent_id: str, *, egress_opt_in: bool = False,
                 base_url: str = "https://api.retellai.com"):
        _require_egress("Retell", egress_opt_in)
        self.id, self.base_url = agent_id, base_url.rstrip("/")
        self._key = _stored_key("retell", "RETELL_API_KEY")
        self._chat: Optional[str] = None
        self._vars: Dict[str, str] = {}

    def start(self, case):
        self._vars = dict(case.get("variables") or {})
        self._chat = None

    def _auth(self):
        return {"Authorization": f"Bearer {self._key}"}

    def send(self, text):
        if self._chat is None:
            body: Dict[str, Any] = {"agent_id": self.id}
            if self._vars:
                body["retell_llm_dynamic_variables"] = self._vars
            self._chat = _post_json(f"{self.base_url}/create-chat", body,
                                    self._auth(), "retell chat").get("chat_id")
            if not self._chat:
                raise SimError("retell create-chat returned no chat_id")
        data = _post_json(f"{self.base_url}/create-chat-completion",
                          {"chat_id": self._chat, "content": text},
                          self._auth(), "retell chat")
        msgs = data.get("messages")
        if not isinstance(msgs, list):
            raise SimError("retell chat completion had no messages list")
        texts, calls = [], []
        for m in msgs:
            if not isinstance(m, dict):
                continue
            if m.get("role") == "agent" and m.get("content"):
                texts.append(str(m["content"]))
            elif m.get("role") == "tool_call_invocation":
                args = m.get("arguments")
                try:
                    args = json.loads(args) if isinstance(args, str) else (args or {})
                except json.JSONDecodeError:
                    args = {"_raw": args}
                calls.append({"name": m.get("name", ""), "arguments": args})
        return AgentReply("\n".join(texts), calls)


def _require_egress(vendor: str, opted: bool) -> None:
    if not opted:
        raise SimError(f"the {vendor} target sends the simulated conversation "
                       f"to {vendor}'s servers. Refused before any request; "
                       "pass --egress-opt-in.")


def make_agent(spec: str, *, egress_opt_in: bool = False) -> AgentTarget:
    """``chat:URL`` (or a bare http(s) URL), ``vapi:ASSISTANT_ID``,
    ``retell:AGENT_ID``."""
    if re.match(r"^https?://", spec):
        return ChatHttpAgent(spec, egress_opt_in=egress_opt_in)
    stack, _, ident = spec.partition(":")
    stack = stack.lower()
    if stack == "chat" and ident:
        return ChatHttpAgent(ident, egress_opt_in=egress_opt_in)
    if stack == "vapi" and ident:
        return VapiChatAgent(ident, egress_opt_in=egress_opt_in)
    if stack == "retell" and ident:
        return RetellChatAgent(ident, egress_opt_in=egress_opt_in)
    raise SimError(f"--agent {spec!r}: use chat:URL (or a bare URL), "
                   "vapi:ASSISTANT_ID or retell:AGENT_ID")


# =========================================================================
# Caller
# =========================================================================

def _caller_system(case: Dict[str, Any]) -> str:
    return (
        "You are role-playing the CALLER in a conversation with a customer-"
        "service agent. Stay in character; write only what the caller says "
        "next, in one to three short sentences, no stage directions.\n\n"
        f"Persona: {case['persona']}\n\nYour goal: {case['goal']}\n\n"
        "Pursue the goal naturally and do not invent facts that contradict "
        "the persona. When the goal has been reached, reply with exactly "
        f"{END_REACHED}. When the goal cannot be reached (the agent refuses "
        f"firmly or is stuck in a loop), reply with exactly {END_IMPOSSIBLE}. "
        "You may add your final words before the marker."
    )


def _caller_turn(llm: LLM, case: Dict[str, Any], transcript: List[Dict[str, Any]]
                 ) -> Tuple[str, Optional[str]]:
    """Return (caller text, end marker or None)."""
    msgs: List[Dict[str, str]] = []
    if not transcript:
        msgs.append({"role": "user", "content": "The call connects. Say your first line."})
    for t in transcript:
        # from the caller LLM's view: its own lines are 'assistant'
        msgs.append({"role": "assistant" if t["role"] == "caller" else "user",
                     "content": t["text"]})
    raw = llm.chat(_caller_system(case), msgs).strip()
    end = None
    for marker, label in ((END_REACHED, "goal_reached"), (END_IMPOSSIBLE, "impossible")):
        if marker in raw:
            end, raw = label, raw.replace(marker, "").strip()
    return raw, end


def run_one(case: Dict[str, Any], agent: AgentTarget, llm: LLM, judge: LLM,
            run_index: int, *, clock=time.monotonic, strict: bool = False
            ) -> Dict[str, Any]:
    """``strict``: re-raise an LLM failure (the very first run uses it, so an
    unusable judge is a refusal rather than a column of errored runs)."""
    llm, judge = _Guarded(llm), _Guarded(judge)
    transcript: List[Dict[str, Any]] = []
    tool_calls: List[Dict[str, Any]] = []
    error: Optional[str] = None
    t0 = clock()
    try:
        agent.start(case)
        for _ in range(case["max_turns"]):
            text, end = _caller_turn(llm, case, transcript)
            if text:
                transcript.append({"role": "caller", "text": text,
                                   "t": round(clock() - t0, 3)})
                reply = agent.send(text)
                tool_calls.extend(reply.tool_calls)
                transcript.append({"role": "agent", "text": reply.text,
                                   "t": round(clock() - t0, 3)})
            if end or not text:
                break
        verdicts = judge_run(case, transcript, tool_calls, agent, judge)
    except LLMError as exc:
        if strict:
            raise
        error, verdicts = f"LLM failed: {exc}", []
    except (SimError, ValueError, OSError) as exc:
        error, verdicts = str(exc), []
    return {"run_index": run_index, "transcript": transcript,
            "tool_calls": tool_calls, "verdicts": verdicts, "error": error}


# =========================================================================
# Judge
# =========================================================================

_JUDGE_SYSTEM = (
    "You grade a transcript of a conversation between a CALLER and an AGENT. "
    "For every numbered item decide a verdict.\n"
    "Items of type criterion: 'pass' only if the transcript shows it was "
    "met AND you can quote the exact words that show it; 'fail' if it was "
    "not met (quote the words if any); 'unknown' if the transcript has no "
    "evidence either way.\n"
    "Items of type forbidden: 'fail' if the agent did the forbidden thing "
    "(quote it), 'pass' if it did not, 'unknown' if you cannot tell.\n"
    "Evidence must be copied EXACTLY from the transcript, one short span.\n"
    "Reply with only JSON: a list of objects {\"id\": number, \"verdict\": "
    "\"pass\"|\"fail\"|\"unknown\", \"confidence\": 0..1, \"evidence\": "
    "string, \"explanation\": one sentence}."
)


def _norm(s: str) -> str:
    return re.sub(r"\s+", " ", s).strip().lower()


def check_tool_calls(expected: List[Dict[str, Any]], actual: List[Dict[str, Any]],
                     reported: bool) -> List[Dict[str, Any]]:
    """Deterministic: each expected call must appear (name equal, expected
    arguments a subset of the actual ones). Not reported -> unknown."""
    out = []
    for exp in expected:
        label = f"tool_call: {exp['name']}"
        if not reported:
            out.append({"criterion": label, "verdict": "unknown", "confidence": 0.0,
                        "evidence": "", "explanation": "the agent target does not "
                        "report tool calls, so this cannot be checked"})
            continue
        hit = None
        for call in actual:
            if call.get("name") != exp["name"]:
                continue
            a = call.get("arguments") or {}
            if all(k in a and str(a[k]) == str(v) for k, v in exp["arguments"].items()):
                hit = call
                break
        if hit:
            out.append({"criterion": label, "verdict": "pass", "confidence": 1.0,
                        "evidence": json.dumps(hit, sort_keys=True),
                        "explanation": "the expected tool call was made"})
        else:
            out.append({"criterion": label, "verdict": "fail", "confidence": 1.0,
                        "evidence": "", "explanation": "no matching tool call "
                        f"was reported ({len(actual)} call(s) seen)"})
    return out


def judge_run(case, transcript, tool_calls, agent, judge: LLM) -> List[Dict[str, Any]]:
    items = [("criterion", c, c) for c in case["success_criteria"]] + \
            [("forbidden", f, f"forbidden: {f}") for f in case["forbidden"]]
    verdicts: List[Dict[str, Any]] = []
    if items:
        lines = "\n".join(f"{t['role'].upper()}: {t['text']}" for t in transcript)
        listing = "\n".join(f"{i}. [{k}] {text}" for i, (k, text, _n) in enumerate(items))
        user = f"TRANSCRIPT:\n{lines or '(empty)'}\n\nITEMS:\n{listing}"
        raw = judge.chat(_JUDGE_SYSTEM, [{"role": "user", "content": user}])
        try:
            parsed = _rubric._extract_json(raw)
        except Exception:
            parsed = None
        if isinstance(parsed, dict):
            parsed = parsed.get("verdicts") or parsed.get("items")
        by_id = {}
        for r in parsed if isinstance(parsed, list) else []:
            if isinstance(r, dict) and isinstance(r.get("id"), int):
                by_id[r["id"]] = r
        haystack = _norm(" ".join(t["text"] for t in transcript))
        for i, (kind, _text, label) in enumerate(items):
            verdicts.append(_clean_verdict(by_id.get(i), kind, label, haystack))
    verdicts.extend(check_tool_calls(case["expected_tool_calls"], tool_calls,
                                     agent.reports_tool_calls))
    return verdicts


def _clean_verdict(raw: Optional[dict], kind: str, label: str, haystack: str) -> Dict[str, Any]:
    if not raw:
        return {"criterion": label, "verdict": "unknown", "confidence": 0.0,
                "evidence": "", "explanation": "the judge returned no verdict for this item"}
    verdict = str(raw.get("verdict", "")).lower()
    if verdict not in ("pass", "fail", "unknown"):
        verdict = "unknown"
    try:
        conf = max(0.0, min(1.0, float(raw.get("confidence", 0.0))))
    except (TypeError, ValueError):
        conf = 0.0
    evidence = str(raw.get("evidence") or "").strip()
    expl = str(raw.get("explanation") or "").strip()
    if evidence and _norm(evidence) not in haystack:
        evidence = ""
        expl = (expl + " " if expl else "") + "(quote not found in transcript, dropped)"
        if verdict == "fail" and kind == "forbidden":
            verdict = "unknown"
    # a criterion pass must be backed by a real quote; a forbidden item that was
    # NOT violated has nothing to quote, so its pass is exempt
    if verdict == "pass" and kind == "criterion" and evidence and len(evidence.split()) < 3:
        # a one- or two-word quote ("yes", "booked") appears in almost any transcript
        evidence = ""
        expl = (expl + " " if expl else "") + "(quote too short to support a pass)"
    if verdict == "pass" and kind == "criterion" and not evidence:
        verdict = "unknown"
        expl = (expl + " " if expl else "") + "(no supporting quote, so not a pass)"
    return {"criterion": label, "verdict": verdict, "confidence": conf,
            "evidence": evidence, "explanation": expl}


# =========================================================================
# Aggregation
# =========================================================================

def _run_passed(run: Dict[str, Any]) -> bool:
    return run["error"] is None and bool(run["verdicts"]) and \
        all(v["verdict"] == "pass" for v in run["verdicts"])


def run_case(case, agent: AgentTarget, llm: LLM, judge: LLM,
             repeat: Optional[int] = None, progress=None, strict: bool = False
             ) -> Dict[str, Any]:
    n = repeat or case["repeat"]
    runs = []
    for i in range(n):
        runs.append(run_one(case, agent, llm, judge, i, strict=strict and i == 0))
        if progress:
            progress(case, i, runs[-1])
    names: List[str] = []
    for r in runs:
        for v in r["verdicts"]:
            if v["criterion"] not in names:
                names.append(v["criterion"])
    if not names:
        names = list(case["success_criteria"]) + \
            [f"forbidden: {f}" for f in case["forbidden"]] + \
            [f"tool_call: {e['name']}" for e in case["expected_tool_calls"]]
    criteria = []
    for name in names:
        ok = sum(1 for r in runs if any(v["criterion"] == name and v["verdict"] == "pass"
                                        for v in r["verdicts"]))
        criteria.append({"name": name, "pass_rate": round(ok / n, 4)})
    passed = sum(1 for r in runs if _run_passed(r))
    return {"case_id": case["case_id"], "name": case["name"],
            "persona": case["persona"], "goal": case["goal"], "repeats": n,
            "passed": passed, "pass_rate": round(passed / n, 4),
            "pass_all": passed == n, "criteria": criteria, "runs": runs}


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def run_cases(cases, agent: AgentTarget, llm: LLM, judge: LLM,
              repeat: Optional[int] = None, progress=None) -> Dict[str, Any]:
    started = _now()
    run_id = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ") + "-" + secrets.token_hex(3)
    out_cases = [run_case(c, agent, llm, judge, repeat, progress, strict=(k == 0))
                 for k, c in enumerate(cases)]
    rates = [c["pass_rate"] for c in out_cases]
    return {
        "schema": SCHEMA, "run_id": run_id, "started_at": started,
        "finished_at": _now(),
        "agent": {"stack": agent.stack, "id": agent.id},
        "judge": {"provider": judge.provider, "model": judge.model},
        "cases": out_cases,
        "summary": {"cases": len(out_cases),
                    "pass_rate": round(sum(rates) / len(rates), 4) if rates else 0.0,
                    "pass_all": sum(1 for c in out_cases if c["pass_all"])},
    }


def exit_code(result: Dict[str, Any], min_pass_rate: float = 1.0) -> int:
    for c in result["cases"]:
        if (c["pass_all"] if min_pass_rate >= 1.0 else c["pass_rate"] >= min_pass_rate):
            continue
        return 1
    return 0


# =========================================================================
# Rendering
# =========================================================================

def render_text(result: Dict[str, Any]) -> str:
    s = result["summary"]
    lines = [f"hotato test run: {s['cases']} case(s) against "
             f"{result['agent']['stack']}:{result['agent']['id']}  "
             f"(judge {result['judge']['provider']}/{result['judge']['model']})"]
    for c in result["cases"]:
        mark = "PASS" if c["pass_all"] else "FAIL"
        lines.append(f"{mark}  {c['name']}  {c['passed']}/{c['repeats']} runs "
                     f"(pass rate {c['pass_rate']:.0%}, pass_all={str(c['pass_all']).lower()})")
        for cr in c["criteria"]:
            lines.append(f"      {cr['pass_rate']:>4.0%}  {cr['name']}")
        for r in c["runs"]:
            if r["error"]:
                lines.append(f"      run {r['run_index'] + 1} error: {r['error']}")
    lines.append(f"summary: mean pass rate {s['pass_rate']:.0%}, "
                 f"{s['pass_all']}/{s['cases']} case(s) passed every repeat")
    return "\n".join(lines) + "\n"


def render_html(result: Dict[str, Any]) -> str:
    from .theme import REPORT as T
    e = escape
    css = (
        f"body{{background:{T['bg']};color:{T['cream']};font:15px/1.5 system-ui,"
        "sans-serif;margin:0;padding:24px 16px}"
        ".w{max-width:960px;margin:0 auto}"
        f"h1{{font-size:22px}}.m{{color:{T['muted']}}}"
        f".card{{background:{T['card']};border:1px solid {T['line']};"
        "border-radius:8px;padding:16px;margin:16px 0}"
        f".pass{{color:{T['green']}}}.fail{{color:{T['red']}}}.unknown{{color:{T['attention']}}}"
        "table{border-collapse:collapse;width:100%}"
        f"td,th{{border-bottom:1px solid {T['line']};padding:6px 8px;text-align:left;"
        "vertical-align:top}"
        f"details{{margin-top:8px}}summary{{cursor:pointer;color:{T['muted']}}}"
        f".caller{{color:{T['caller']}}}.agent{{color:{T['agent']}}}"
        "q{quotes:none}"
    )
    s = result["summary"]
    body = [f"<h1>hotato test run</h1><p class='m'>{e(result['agent']['stack'])}:"
            f"{e(result['agent']['id'])} &middot; judge {e(result['judge']['provider'])}/"
            f"{e(result['judge']['model'])} &middot; {e(result['started_at'])}</p>",
            f"<p>{s['cases']} case(s), mean pass rate {s['pass_rate']:.0%}, "
            f"{s['pass_all']} passed every repeat.</p>"]
    for c in result["cases"]:
        cls = "pass" if c["pass_all"] else "fail"
        body.append(f"<div class='card'><h2><span class='{cls}'>"
                    f"{'PASS' if c['pass_all'] else 'FAIL'}</span> {e(c['name'])}</h2>"
                    f"<p class='m'>Goal: {e(c['goal'])}<br>Persona: {e(c['persona'])}</p>"
                    f"<p>{c['passed']}/{c['repeats']} runs passed (pass rate "
                    f"{c['pass_rate']:.0%}).</p><table><tr><th>Criterion</th>"
                    "<th>Pass rate</th></tr>")
        for cr in c["criteria"]:
            body.append(f"<tr><td>{e(cr['name'])}</td><td>{cr['pass_rate']:.0%}</td></tr>")
        body.append("</table>")
        for r in c["runs"]:
            ok = _run_passed(r)
            body.append(f"<details><summary>Run {r['run_index'] + 1}: "
                        f"{'passed' if ok else 'not passed'}"
                        f"{' (error)' if r['error'] else ''}</summary>")
            if r["error"]:
                body.append(f"<p class='fail'>{e(r['error'])}</p>")
            for t in r["transcript"]:
                body.append(f"<p><b class='{t['role']}'>{t['role']}</b> "
                            f"<span class='m'>{t['t']:.1f}s</span> {e(t['text'])}</p>")
            if r["verdicts"]:
                body.append("<table><tr><th>Criterion</th><th>Verdict</th>"
                            "<th>Evidence</th><th>Why</th></tr>")
                for v in r["verdicts"]:
                    body.append(f"<tr><td>{e(v['criterion'])}</td><td class='{e(v['verdict'])}'>"
                                f"{e(v['verdict'])} ({v['confidence']:.2f})</td>"
                                f"<td>{e(v['evidence'])}</td><td>{e(v['explanation'])}</td></tr>")
                body.append("</table>")
            body.append("</details>")
        body.append("</div>")
    return ("<!doctype html><html lang='en'><head><meta charset='utf-8'>"
            "<meta name='viewport' content='width=device-width,initial-scale=1'>"
            f"<title>hotato test run</title><style>{css}</style></head>"
            f"<body><div class='w'>{''.join(body)}</div></body></html>\n")


# =========================================================================
# Authoring: `test new` and `test from-call`
# =========================================================================

def dump_case(case: Dict[str, Any], as_yaml: bool) -> str:
    """Write a case as YAML (JSON scalars are valid YAML) or pretty JSON."""
    if not as_yaml:
        return json.dumps(case, indent=2, ensure_ascii=False) + "\n"
    lines = []
    for k, v in case.items():
        if isinstance(v, list) and v and all(isinstance(i, str) for i in v):
            lines.append(f"{k}:")
            lines += [f"  - {json.dumps(i, ensure_ascii=False)}" for i in v]
        elif isinstance(v, (list, dict)):
            lines.append(f"{k}: {json.dumps(v, ensure_ascii=False)}")
        else:
            lines.append(f"{k}: {json.dumps(v, ensure_ascii=False)}")
    return "\n".join(lines) + "\n"


def starter_case(name: str) -> Dict[str, Any]:
    return {
        "name": name,
        "persona": "A {{mood}} customer whose order arrived damaged. Short "
                   "answers, wants this fixed on this call.",
        "goal": "Get a replacement or refund arranged for order {{order_id}}.",
        "success_criteria": [
            "The agent asks for the order number or confirms the order",
            "The agent offers a concrete next step (replacement or refund)",
        ],
        "forbidden": ["promises a refund before checking the order"],
        "max_turns": DEFAULT_MAX_TURNS,
        "repeat": DEFAULT_REPEAT,
        "variables": {"mood": "frustrated", "order_id": "A-1001"},
    }


def _clip(s: str, n: int = 160) -> str:
    s = re.sub(r"\s+", " ", s).strip()
    return s if len(s) <= n else s[: n - 1].rstrip() + "..."


def case_from_transcript(doc: Any, name: str, *, moment: Optional[str] = None,
                         at: Optional[float] = None) -> Dict[str, Any]:
    """Turn a failed production call (a transcript, or a report carrying one
    and optionally findings) into a case. Persona and goal come from the
    caller's own lines; the failing moment becomes a criterion. Offline and
    deterministic: edit the result before relying on it."""
    findings: List[str] = []
    segs = doc
    if isinstance(doc, dict):
        for key in ("findings", "incidents", "failures", "issues"):
            for f in doc.get(key) or []:
                if isinstance(f, dict):
                    txt = next((f[k] for k in ("summary", "description", "title",
                                               "message", "reason") if isinstance(f.get(k), str)), "")
                    if txt:
                        findings.append(txt)
                elif isinstance(f, str):
                    findings.append(f)
        segs = doc.get("segments")
        if not isinstance(segs, list):
            t = doc.get("transcript")
            segs = t if isinstance(t, list) else (t or {}).get("segments") if isinstance(t, dict) else None
    if not isinstance(segs, list):
        raise SimError("expected a transcript (a list of turns, or an object "
                       "with a segments/transcript list)")
    turns = []
    for s in segs:
        if not isinstance(s, dict):
            continue
        role = str(s.get("role") or s.get("speaker") or "").lower()
        text = s.get("text") or s.get("content") or ""
        if not isinstance(text, str) or not text.strip():
            continue
        who = "caller" if role in ("caller", "user", "customer", "human", "client") \
            else "agent" if role in ("agent", "assistant", "bot", "ai") else None
        if who:
            start = s.get("start", s.get("t"))
            turns.append({"role": who, "text": text.strip(),
                          "t": float(start) if isinstance(start, (int, float)) else None})
    caller = [t for t in turns if t["role"] == "caller"]
    if not caller:
        raise SimError("the transcript has no caller turns to infer a persona from")
    opener = caller[0]["text"]
    said = "; ".join(f"\"{_clip(t['text'], 90)}\"" for t in caller[:3])
    criteria: List[str] = []
    if moment:
        criteria.append(moment.strip())
    for f in findings[:3]:
        criteria.append(f"The agent avoids this failure from the original call: {_clip(f, 200)}")
    if at is not None and not criteria:
        before = [i for i, t in enumerate(turns) if t["t"] is not None and t["t"] <= at]
        i = before[-1] if before else 0
        ask = next((t for t in reversed(turns[: i + 1]) if t["role"] == "caller"), caller[0])
        ans = next((t for t in turns[i:] if t["role"] == "agent"), None)
        c = f"The agent handles the caller's \"{_clip(ask['text'], 120)}\" correctly"
        if ans:
            c += f", unlike the failed call where it answered \"{_clip(ans['text'], 120)}\""
        criteria.append(c)
    if not criteria:
        criteria.append(f"The agent resolves the caller's request: \"{_clip(opener, 140)}\"")
    return {
        "name": name,
        "persona": f"A caller who opened a production call with \"{_clip(opener, 140)}\". "
                   f"In that call they said: {said}.",
        "goal": f"Get this resolved: {_clip(opener, 200)}",
        "success_criteria": criteria,
        "max_turns": max(DEFAULT_MAX_TURNS, min(40, len(caller) * 2)),
        "repeat": DEFAULT_REPEAT,
    }
