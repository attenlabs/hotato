"""``hotato.rubric`` -- the REAL local-model rubric judge (schema ``rubric.v1``).

This is the model-judged lane, kept structurally SEPARATE from the deterministic
``assert.v1`` wall. Every result here carries ``deterministic: false`` with full
provenance and lives in its own schema and its own report shelf; it is never an
``assert.v1`` result, never merged into a deterministic count, and never part of
an ``overall_score``. ``assert.v1``'s five kinds keep ``deterministic:const:true``
byte-for-byte -- a rubric verdict physically cannot be one.

What is REAL here (no stub, no mock in the shipped path):

* :class:`OllamaJudge` -- the DEFAULT judge. A stdlib-only (``urllib``) HTTP
  client to a LOCAL Ollama daemon (``http://localhost:11434`` by default). It
  pins a model id, records that model's content DIGEST from ``/api/tags`` for
  provenance, calls ``/api/chat`` at ``temperature 0`` with ``format:"json"``,
  parses a strict categorical verdict, and retries ONCE with a repair prompt on
  a parse miss. Local => ZERO egress (invariant 4); needs no opt-in.
* :class:`HostedJudge` -- an OPT-IN egress judge to an OpenAI/Anthropic-style
  ``/chat/completions`` endpoint, refused unless ``egress_opt_in=True`` (mirrors
  ``--diarizer pyannoteai --egress-opt-in``). Not the default; never called
  without the flag. See ``docs/EGRESS.md`` / ``docs/THREAT-MODEL.md``.

Reproducibility, stated precisely (never oversold): the model call is NOT
claimed deterministic. What IS deterministic is REPLAY. Every verdict is
content-addressed by ``sha256(provider:model + prompt_sha256 + input_sha256)``
and cached (reusing :class:`hotato.fleet.store.ArtifactStore`); a cache hit is
byte-identical forever (same ``verdict_sha256``). ``--no-cache`` re-queries and
DIFFS the fresh verdict against the cached one, SURFACING drift, never hiding
it. A cached verdict may optionally be signed with the ``sign.py`` /
``labelrecord.py`` Ed25519 (``human``) or HMAC (``human-shared``) tiers as a
"judge-record", so a stored verdict is provably unmutated.

Honesty invariants enforced here: ``deterministic:false`` + full provenance on
every result; missing/insufficient evidence -> INCONCLUSIVE (no model call);
a ``human_rubric`` item is NEVER scored by a model (stays INCONCLUSIVE with
``human_required``); no ``overall_score`` anywhere; ADVISORY by default
(``exit_code`` stays 0 unless the caller opts into ``--gate``). Core hotato
stays zero-dependency: only ``urllib`` from the stdlib is touched, lazily.
"""
from __future__ import annotations

import hashlib
import json
import os
from typing import Any, Dict, List, Optional, Sequence

from .errors import (
    load_json_file as _load_json_file,
    open_regular as _open_regular,
    reject_overall_score as _reject_overall_score,
)
# Canonical-JSON + sha256-of-canonical are the established shared primitives in
# hotato.manifest (already reused by labelrecord/ledger/receipt); import them
# here instead of reimplementing (audit finding #2). ``_sha256_json`` stays a
# local one-line composition of the two.
from .manifest import canonical_json as _canonical, _sha256_str as _sha256_text

SCHEMA = "rubric.v1"
KIND = "rubric"

# The versioned judge prompt. prompt_sha256 is derived from the template TEXT
# below, so any change to the wording is a new content address (and a cache
# miss) -- provenance can never silently drift under a fixed prompt_id/version.
PROMPT_ID = "rubric/categorical-verdict"
PROMPT_VERSION = 1

CATEGORICAL_VALUES = ("pass", "fail", "inconclusive")
STATUSES = ("PASS", "FAIL", "INCONCLUSIVE", "ERROR")
EVIDENCE_KINDS = ("transcript", "tool_trace")
AGGREGATION = "unanimous_or_inconclusive"

DEFAULT_JUDGE_MODEL = "qwen2.5vl:3b"
DEFAULT_OLLAMA_ENDPOINT = "http://localhost:11434"
DEFAULT_REPETITIONS = 1
DEFAULT_CONFIDENCE_REQUIRED = 0.85
DEFAULT_HUMAN_REQUIRED_ON = ("fail", "disagreement", "confidence_below_threshold")

# Env overrides so an operator (or a test) can point the default judge at a
# different endpoint/model without a code change. CLI flags win over these.
ENDPOINT_ENV = "HOTATO_JUDGE_ENDPOINT"
MODEL_ENV = "HOTATO_JUDGE_MODEL"

_SYSTEM_PROMPT = (
    "You are a strict conversation-QA rubric evaluator for a voice agent. "
    "You are given ONE rubric criterion and the EVIDENCE from a single call "
    "(a numbered transcript, and optionally tool-trace events). Decide whether "
    "the evidence shows the criterion is met.\n"
    "Rules:\n"
    "- Respond with ONLY a JSON object, no prose around it.\n"
    "- Shape: {\"verdict\": \"pass\"|\"fail\"|\"inconclusive\", "
    "\"rationale\": \"one or two sentences grounded in the evidence\", "
    "\"citations\": [{\"turn\": <int>, \"quote\": \"<short exact quote>\"}]}.\n"
    "- Use \"pass\" only if the evidence clearly satisfies the criterion, "
    "\"fail\" only if it clearly does not, and \"inconclusive\" when the "
    "evidence is insufficient to decide.\n"
    "- Never invent evidence. Cite the specific transcript turn numbers that "
    "justify your verdict.\n"
    "- Judge only the stated criterion, nothing else."
)


class JudgeError(RuntimeError):
    """A judge backend failed at the transport level (unreachable endpoint,
    timeout, HTTP error, unparseable envelope). Distinct from a model that
    simply could not decide (that is an honest INCONCLUSIVE, not an error):
    a :class:`JudgeError` becomes an ERROR status -- advisory, never a fake
    verdict."""


class EgressRefused(ValueError):
    """A hosted (off-box) judge was requested without ``--judge-egress-opt-in``.
    Mirrors the ``--diarizer pyannoteai`` egress refusal exactly: a usage error
    (exit 2), never a silent network call."""


# =========================================================================
# Prompt rendering + content addressing
# =========================================================================

def _sha256_json(obj: Any) -> str:
    return _sha256_text(_canonical(obj))


def prompt_sha256() -> str:
    """Content address of the versioned judge prompt template. Bound into
    every verdict's provenance so a reader can prove which prompt produced it."""
    return _sha256_text(_SYSTEM_PROMPT)


def _norm_turns(transcript: Optional[Sequence[Dict[str, Any]]]) -> List[Dict[str, Any]]:
    out: List[Dict[str, Any]] = []
    for t in (transcript or []):
        if isinstance(t, dict):
            out.append({"role": t.get("role"), "text": t.get("text") or ""})
        else:  # tolerate assert_.Turn-like objects
            out.append({"role": getattr(t, "role", None),
                        "text": getattr(t, "text", "") or ""})
    return out


def _norm_spans(trace: Optional[Sequence[Dict[str, Any]]]) -> List[Dict[str, Any]]:
    out: List[Dict[str, Any]] = []
    for i, s in enumerate(trace or []):
        if not isinstance(s, dict):
            continue
        out.append({
            "span_id": s.get("span_id") or s.get("id") or f"s_{i}",
            "type": s.get("type"),
            "name": s.get("name") or s.get("tool"),
            "text": None if s.get("text_redacted") else s.get("text"),
        })
    return out


def _render_user_prompt(rubric: Dict[str, Any], turns: List[Dict[str, Any]],
                        spans: List[Dict[str, Any]]) -> str:
    lines = [f"RUBRIC CRITERION: {rubric['criterion']}"]
    ex = rubric.get("examples") or {}
    if ex.get("pass"):
        lines.append(f"Example that should PASS: {ex['pass']}")
    if ex.get("fail"):
        lines.append(f"Example that should FAIL: {ex['fail']}")
    lines.append("")
    lines.append("TRANSCRIPT (numbered turns):")
    if turns:
        for i, t in enumerate(turns):
            role = t.get("role") or "?"
            lines.append(f"[{i}] {role}: {t.get('text', '')}")
    else:
        lines.append("(no transcript turns)")
    if "tool_trace" in (rubric.get("evidence") or []):
        lines.append("")
        lines.append("TOOL-TRACE EVENTS:")
        if spans:
            for s in spans:
                nm = s.get("name") or s.get("type") or "span"
                lines.append(f"({s['span_id']}) {nm}")
        else:
            lines.append("(no trace events)")
    lines.append("")
    lines.append("Return ONLY the JSON verdict object described in the system "
                 "instructions.")
    return "\n".join(lines)


# =========================================================================
# Rubric-object validation (the user-authored rubric)
# =========================================================================

def validate_rubric_object(obj: Any) -> Dict[str, Any]:
    """Validate ONE user-authored rubric object and return a NORMALIZED copy
    with defaults applied. Raises ``ValueError`` on anything malformed -- a
    verdict is invalid without a cited, well-formed rubric.

    Required: ``id`` (str), ``criterion`` (non-empty str). Optional with
    honest defaults: ``kind`` (judge_rubric), ``dimension``, ``evidence``
    (['transcript']), ``response`` ({categorical, [pass,fail,inconclusive]}),
    ``examples``, ``evaluation`` (model/repetitions/aggregation/
    confidence_required), ``review.human_required_on``. An ``overall_score``
    key is rejected structurally, matching every other schema."""
    if not isinstance(obj, dict):
        raise ValueError("a rubric must be a mapping")
    _reject_overall_score(obj, "rubric: 'overall_score' is forbidden (no blended score, ever)")
    rid = obj.get("id")
    if not rid or not isinstance(rid, str):
        raise ValueError("rubric is missing a string 'id'")
    criterion = obj.get("criterion")
    if not criterion or not isinstance(criterion, str):
        raise ValueError(f"rubric {rid!r} is missing a non-empty string 'criterion'")

    kind = obj.get("kind", "judge_rubric")
    if kind not in ("judge_rubric", "human_rubric"):
        raise ValueError(
            f"rubric {rid!r}: 'kind' must be 'judge_rubric' or 'human_rubric', "
            f"got {kind!r}"
        )

    dim = obj.get("dimension")
    if dim is not None and dim not in _REPORT_DIMENSIONS:
        raise ValueError(
            f"rubric {rid!r}: 'dimension' must be one of {_REPORT_DIMENSIONS}, "
            f"got {dim!r}"
        )

    evidence = obj.get("evidence", ["transcript"])
    if not isinstance(evidence, list) or not evidence or any(
        e not in EVIDENCE_KINDS for e in evidence
    ):
        raise ValueError(
            f"rubric {rid!r}: 'evidence' must be a non-empty list drawn from "
            f"{EVIDENCE_KINDS}, got {evidence!r}"
        )

    response = obj.get("response", {"type": "categorical", "values": list(CATEGORICAL_VALUES)})
    if not isinstance(response, dict) or response.get("type") != "categorical":
        raise ValueError(f"rubric {rid!r}: 'response.type' must be 'categorical'")
    values = response.get("values") or list(CATEGORICAL_VALUES)
    if any(v not in CATEGORICAL_VALUES for v in values):
        raise ValueError(
            f"rubric {rid!r}: 'response.values' must be drawn from {CATEGORICAL_VALUES}"
        )

    evaluation = dict(obj.get("evaluation") or {})
    reps = evaluation.get("repetitions", DEFAULT_REPETITIONS)
    if isinstance(reps, bool) or not isinstance(reps, int) or reps < 1:
        raise ValueError(f"rubric {rid!r}: 'evaluation.repetitions' must be an integer >= 1")
    agg = evaluation.get("aggregation", AGGREGATION)
    if agg != AGGREGATION:
        raise ValueError(
            f"rubric {rid!r}: 'evaluation.aggregation' must be {AGGREGATION!r}"
        )
    conf_req = evaluation.get("confidence_required", DEFAULT_CONFIDENCE_REQUIRED)
    if not isinstance(conf_req, (int, float)) or isinstance(conf_req, bool) or not (0 <= conf_req <= 1):
        raise ValueError(
            f"rubric {rid!r}: 'evaluation.confidence_required' must be a number in [0, 1]"
        )

    review = dict(obj.get("review") or {})
    hro = review.get("human_required_on", list(DEFAULT_HUMAN_REQUIRED_ON))
    if not isinstance(hro, list) or any(
        h not in ("fail", "disagreement", "confidence_below_threshold") for h in hro
    ):
        raise ValueError(
            f"rubric {rid!r}: 'review.human_required_on' must be a list drawn from "
            "['fail', 'disagreement', 'confidence_below_threshold']"
        )

    norm = dict(obj)
    norm["id"] = rid
    norm["kind"] = kind
    norm["criterion"] = criterion
    norm["evidence"] = list(evidence)
    norm["response"] = {"type": "categorical", "values": list(values)}
    norm["evaluation"] = {
        "model": evaluation.get("model"),
        "repetitions": reps,
        "aggregation": AGGREGATION,
        "confidence_required": float(conf_req),
    }
    norm["review"] = {"human_required_on": list(hro)}
    if dim is not None:
        norm["dimension"] = dim
    return norm


# report dimensions -- a bare tuple (mirrors conversation_test.REPORT_DIMENSIONS;
# kept local so importing rubric.py never pulls the heavier modules).
_REPORT_DIMENSIONS = ("outcome", "policy", "conversation", "speech", "reliability")


def load_rubrics_file(path: str) -> List[Dict[str, Any]]:
    """Load + validate a rubrics file: a JSON/YAML-subset doc shaped as either
    ``{"version": 1, "rubrics": [ <rubric object> ]}`` or a bare list of rubric
    objects. Returns the normalized list. Raises ``ValueError`` on a malformed
    file or any malformed rubric (the caller's exit-2 path)."""
    from .assert_ import parse_assertions_yaml  # zero-dep YAML-subset/JSON parser
    with _open_regular(path, "r", encoding="utf-8") as fh:
        text = fh.read()
    doc = parse_assertions_yaml(text)
    if isinstance(doc, dict):
        rubrics = doc.get("rubrics")
        if rubrics is None:
            raise ValueError(
                f"{path!r}: a rubrics file needs a 'rubrics' list (or be a bare "
                "list of rubric objects)"
            )
    elif isinstance(doc, list):
        rubrics = doc
    else:
        raise ValueError(f"{path!r}: a rubrics file must be a mapping or a list")
    if not isinstance(rubrics, list) or not rubrics:
        raise ValueError(f"{path!r}: 'rubrics' must be a non-empty list")
    out = [validate_rubric_object(r) for r in rubrics]
    ids = [r["id"] for r in out]
    if len(set(ids)) != len(ids):
        raise ValueError(f"{path!r}: duplicate rubric id(s) in {ids}")
    return out


# =========================================================================
# Judge backends (REAL)
# =========================================================================

def _urllib_json_call(url: str, *, data: Optional[bytes], headers: Dict[str, str],
                      method: str, timeout: float,
                      unreachable_subject: str, failed_subject: str) -> str:
    """Shared stdlib-urllib transport for the judge HTTP clients (audit finding
    #8): install the hardened process-wide opener FIRST, fire the request, and
    return the decoded body TEXT. Both :class:`OllamaJudge` and
    :class:`HostedJudge` route through here; each keeps its OWN vendor
    response-envelope parser (the JSON shapes differ), which is intentionally not
    merged.

    The ``_ensure_safe_opener`` install (finding #1, added in d054676) lives here
    so it can never be forgotten on a judge path: the opener strips
    ``Authorization``/``Cookie`` on cross-host redirects and re-runs the SSRF
    guard on every redirect target, so a redirecting judge endpoint can never
    exfiltrate the judge API key. A judge command may be the first
    network-touching command in a process, so this must run before ANY request."""
    import urllib.error
    import urllib.request
    from . import capture as _capture
    _capture._ensure_safe_opener()
    req = urllib.request.Request(url, data=data, method=method, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:  # nosec - opener hardened above
            return resp.read().decode("utf-8")
    except urllib.error.URLError as exc:
        raise JudgeError(f"{unreachable_subject} {url!r} unreachable: {exc}") from exc
    except (TimeoutError, OSError) as exc:
        raise JudgeError(f"{failed_subject} request to {url!r} failed: {exc}") from exc


class Judge:
    """Judge interface. ``complete(system, user)`` returns the model's RAW text
    for one call (temperature 0). ``model_digest()`` returns the backend's
    content digest for the pinned model (provenance), or None if the backend
    cannot report one. Subclasses implement a real backend; tests inject a
    deterministic double."""

    provider = "base"
    model = ""

    def complete(self, system: str, user: str) -> str:  # pragma: no cover - interface
        raise NotImplementedError

    def model_digest(self) -> Optional[str]:  # pragma: no cover - interface
        return None


class OllamaJudge(Judge):
    """The DEFAULT judge: a real, stdlib-only client to a local Ollama daemon.

    Zero egress -- talks only to ``endpoint`` (``localhost`` by default). Pins
    ``model`` and records its digest from ``/api/tags``. Calls ``/api/chat`` at
    temperature 0 with ``format:"json"`` for a structured verdict."""

    provider = "ollama"

    def __init__(self, model: Optional[str] = None, endpoint: Optional[str] = None,
                 *, timeout: float = 120.0, egress_opt_in: bool = False):
        self.model = model or os.environ.get(MODEL_ENV) or DEFAULT_JUDGE_MODEL
        self.endpoint = (endpoint or os.environ.get(ENDPOINT_ENV)
                         or DEFAULT_OLLAMA_ENDPOINT).rstrip("/")
        self.timeout = timeout
        self._digest: Optional[str] = None
        self._digest_fetched = False
        # A non-local endpoint is off-box egress and must be opted into,
        # exactly like a hosted judge. localhost/127.0.0.1/[::1] stay ungated.
        if not egress_opt_in and not _is_local_endpoint(self.endpoint):
            raise EgressRefused(
                f"the Ollama endpoint {self.endpoint!r} is not local; reaching a "
                "remote model host is off-box egress. Pass --judge-egress-opt-in "
                "(or use a localhost endpoint) -- the default judge never leaves "
                "the box."
            )

    def _http_json(self, path: str, payload: Optional[dict], method: str = "POST") -> dict:
        url = f"{self.endpoint}{path}"
        data = _canonical(payload).encode("utf-8") if payload is not None else None
        # Shared transport installs the hardened opener before the request
        # (finding #1) and normalizes transport errors to JudgeError (finding #8).
        body = _urllib_json_call(
            url, data=data, headers={"Content-Type": "application/json"},
            method=method, timeout=self.timeout,
            unreachable_subject="ollama endpoint", failed_subject="ollama",
        )
        try:
            return json.loads(body)
        except json.JSONDecodeError as exc:
            raise JudgeError(f"ollama returned non-JSON from {url!r}: {exc}") from exc

    def model_digest(self) -> Optional[str]:
        if self._digest_fetched:
            return self._digest
        self._digest_fetched = True
        try:
            tags = self._http_json("/api/tags", None, method="GET")
        except JudgeError:
            self._digest = None
            return None
        for m in tags.get("models") or []:
            if m.get("name") == self.model or m.get("model") == self.model:
                self._digest = m.get("digest")
                return self._digest
        self._digest = None
        return None

    def complete(self, system: str, user: str) -> str:
        payload = {
            "model": self.model,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            "stream": False,
            "format": "json",
            "options": {"temperature": 0},
        }
        resp = self._http_json("/api/chat", payload)
        content = (resp.get("message") or {}).get("content")
        if not isinstance(content, str):
            raise JudgeError("ollama /api/chat response missing message.content")
        return content


def _is_local_endpoint(endpoint: str) -> bool:
    from urllib.parse import urlparse
    host = (urlparse(endpoint).hostname or "").lower()
    return host in ("localhost", "127.0.0.1", "::1", "0.0.0.0")


class HostedJudge(Judge):
    """An OPT-IN egress judge to an OpenAI/Anthropic-compatible
    ``/chat/completions`` endpoint. REFUSED unless ``egress_opt_in=True`` -- it
    sends the transcript off-box, so it is gated exactly like
    ``--diarizer pyannoteai --egress-opt-in`` and documented in
    ``docs/EGRESS.md`` / ``docs/THREAT-MODEL.md``. Never the default; never
    called without the flag."""

    provider = "hosted"

    def __init__(self, model: str, endpoint: str, *, egress_opt_in: bool = False,
                 api_key_env: str = "HOTATO_JUDGE_API_KEY", timeout: float = 120.0):
        if not egress_opt_in:
            raise EgressRefused(
                "a hosted judge sends the transcript off-box (egress). Refused "
                "unless you pass --judge-egress-opt-in. The default judge is a "
                "LOCAL Ollama model and never leaves the box."
            )
        self.model = model
        self.endpoint = endpoint.rstrip("/")
        self.api_key_env = api_key_env
        self.timeout = timeout

    def model_digest(self) -> Optional[str]:
        # A hosted endpoint rarely exposes a weights digest; provenance still
        # pins the model id + provider + endpoint. None is honest here.
        return None

    def complete(self, system: str, user: str) -> str:
        # This request carries the judge API key. The shared transport installs
        # the hardened opener before the request (finding #1) so a redirecting
        # hosted endpoint can never exfiltrate the key to another host, and
        # normalizes transport errors to JudgeError (finding #8).
        key = os.environ.get(self.api_key_env)
        headers = {"Content-Type": "application/json"}
        if key:
            headers["Authorization"] = f"Bearer {key}"
        payload = {
            "model": self.model,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            "temperature": 0,
        }
        url = f"{self.endpoint}/chat/completions"
        body = _urllib_json_call(
            url, data=_canonical(payload).encode("utf-8"), headers=headers,
            method="POST", timeout=self.timeout,
            unreachable_subject="hosted judge", failed_subject="hosted judge",
        )
        try:
            data = json.loads(body)
            return data["choices"][0]["message"]["content"]
        except (json.JSONDecodeError, KeyError, IndexError, TypeError) as exc:
            raise JudgeError(f"hosted judge returned an unexpected envelope: {exc}") from exc


# =========================================================================
# Verdict parsing (+ one repair retry)
# =========================================================================

class ParseMiss(Exception):
    """The model output was not a usable categorical verdict object."""


def parse_verdict(text: str) -> Dict[str, Any]:
    """Parse one model response into ``{"verdict", "rationale", "citations"}``.
    Raises :class:`ParseMiss` if it is not JSON with a recognizable categorical
    verdict -- the caller retries once with a repair prompt, then treats a
    second miss as an honest ``inconclusive`` vote (never a coin-flip)."""
    if not isinstance(text, str):
        raise ParseMiss("response is not text")
    obj = _extract_json(text)
    if not isinstance(obj, dict):
        raise ParseMiss("response is not a JSON object")
    raw = obj.get("verdict")
    if raw is None:
        for alt in ("result", "label", "decision", "answer"):
            if obj.get(alt) is not None:
                raw = obj[alt]
                break
    verdict = _normalize_verdict(raw)
    if verdict is None:
        raise ParseMiss(f"no recognizable categorical verdict in {obj!r}")
    citations = obj.get("citations")
    norm_cites: List[Dict[str, Any]] = []
    if isinstance(citations, list):
        for c in citations:
            if not isinstance(c, dict):
                continue
            cite: Dict[str, Any] = {"type": "transcript_span"}
            if isinstance(c.get("turn"), int):
                cite["turn"] = c["turn"]
            if isinstance(c.get("quote"), str) and c["quote"]:
                cite["quote"] = c["quote"][:280]
            if "turn" in cite or "quote" in cite:
                norm_cites.append(cite)
    rationale = obj.get("rationale")
    if not isinstance(rationale, str):
        rationale = ""
    return {"verdict": verdict, "rationale": rationale[:1000], "citations": norm_cites}


def _extract_json(text: str) -> Any:
    text = text.strip()
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass
    # tolerate a JSON object embedded in surrounding prose / code fences
    start = text.find("{")
    end = text.rfind("}")
    if start != -1 and end != -1 and end > start:
        try:
            return json.loads(text[start:end + 1])
        except json.JSONDecodeError:
            return None
    return None


def _normalize_verdict(raw: Any) -> Optional[str]:
    if not isinstance(raw, str):
        if isinstance(raw, bool):
            return "pass" if raw else "fail"
        return None
    v = raw.strip().lower()
    if v in CATEGORICAL_VALUES:
        return v
    mapping = {
        "true": "pass", "yes": "pass", "passed": "pass", "met": "pass",
        "false": "fail", "no": "fail", "failed": "fail", "not met": "fail",
        "unknown": "inconclusive", "unsure": "inconclusive", "n/a": "inconclusive",
        "insufficient": "inconclusive",
    }
    return mapping.get(v)


_REPAIR_SUFFIX = (
    "\n\nYour previous response was NOT a valid JSON verdict object. Respond "
    "again with ONLY this JSON, nothing else: {\"verdict\": "
    "\"pass\"|\"fail\"|\"inconclusive\", \"rationale\": \"...\", "
    "\"citations\": [{\"turn\": <int>, \"quote\": \"...\"}]}."
)


def _one_vote(judge: Judge, system: str, user: str) -> Dict[str, Any]:
    """One repetition: call the model, parse; on a parse miss retry ONCE with a
    repair prompt; a second miss is an honest 'inconclusive' vote."""
    raw = judge.complete(system, user)
    try:
        return parse_verdict(raw)
    except ParseMiss:
        repaired = judge.complete(system, user + _REPAIR_SUFFIX)
        try:
            return parse_verdict(repaired)
        except ParseMiss:
            return {"verdict": "inconclusive",
                    "rationale": "the model did not return a parseable categorical "
                                 "verdict after a repair retry",
                    "citations": []}


# =========================================================================
# Aggregation
# =========================================================================

def _aggregate(votes: List[str]) -> Dict[str, Any]:
    """Aggregate categorical votes under ``unanimous_or_inconclusive``: a
    decisive PASS/FAIL only if every vote agrees on it; any disagreement or any
    inconclusive vote -> INCONCLUSIVE. ``confidence`` is the majority fraction,
    ``disagreement`` true when the votes are not unanimous."""
    n = len(votes) or 1
    counts = {v: votes.count(v) for v in set(votes)}
    top = max(counts.values()) if counts else 0
    confidence = top / n
    disagreement = len(set(votes)) > 1
    unanimous = not disagreement and votes and votes[0] in ("pass", "fail")
    if unanimous:
        status = "PASS" if votes[0] == "pass" else "FAIL"
    else:
        status = "INCONCLUSIVE"
    return {"status": status, "confidence": confidence, "disagreement": disagreement}


# =========================================================================
# Core evaluation
# =========================================================================

def _evidence_present(rubric: Dict[str, Any], turns: List[Dict[str, Any]],
                      spans: List[Dict[str, Any]]) -> Optional[str]:
    """Return a reason string if required evidence is absent, else None. This is
    the missing-evidence -> INCONCLUSIVE invariant, checked BEFORE any model
    call (a rubric that needs a transcript but has none is never guessed)."""
    ev = rubric.get("evidence") or ["transcript"]
    if "transcript" in ev and not turns:
        return "required evidence absent: transcript (no turns supplied)"
    if "tool_trace" in ev and not spans:
        return "required evidence absent: tool_trace (no trace events supplied)"
    return None


def _base_result(rubric: Dict[str, Any]) -> Dict[str, Any]:
    r: Dict[str, Any] = {"id": rubric["id"], "kind": KIND, "deterministic": False}
    if rubric.get("dimension"):
        r["dimension"] = rubric["dimension"]
    return r


def _review_block(rubric: Dict[str, Any], status: str, disagreement: bool,
                  confidence: float, conf_required: float) -> Dict[str, Any]:
    triggers = rubric.get("review", {}).get("human_required_on", list(DEFAULT_HUMAN_REQUIRED_ON))
    reasons: List[str] = []
    if "fail" in triggers and status == "FAIL":
        reasons.append("verdict is FAIL")
    if "disagreement" in triggers and disagreement:
        reasons.append("the votes disagreed")
    if "confidence_below_threshold" in triggers and confidence < conf_required:
        reasons.append(f"confidence {confidence:.2f} < required {conf_required:.2f}")
    return {"human_required": bool(reasons), "reasons": reasons}


def evaluate_rubric(
    rubric: Dict[str, Any],
    *,
    transcript: Optional[Sequence[Dict[str, Any]]] = None,
    trace: Optional[Sequence[Dict[str, Any]]] = None,
    judge: Optional[Judge] = None,
    cache: Optional["VerdictCache"] = None,
    no_cache: bool = False,
    sign: bool = False,
) -> Dict[str, Any]:
    """Evaluate ONE (already-normalized) rubric against the supplied evidence and
    return a ``rubric.v1`` result dict (``deterministic: false`` + full
    provenance). REAL model call via ``judge`` (default caller supplies an
    :class:`OllamaJudge`).

    Honest branches, none of them a stub:
    * ``human_rubric`` -> INCONCLUSIVE, ``human_required`` (a model never scores
      a human_rubric item).
    * required evidence absent -> INCONCLUSIVE (no model call).
    * judge backend failure -> ERROR (honest, advisory) -- never a fake verdict.
    * a cache hit -> the byte-identical stored verdict, ``cached: true``.
    * ``no_cache`` -> a fresh query that DIFFS against any cached verdict
      (``judge.drift``), surfacing drift.
    """
    rubric = validate_rubric_object(rubric)
    turns = _norm_turns(transcript)
    spans = _norm_spans(trace)
    transcript_sha = _sha256_json(turns) if transcript is not None else None
    trace_sha = _sha256_json(spans) if trace is not None else None
    result = _base_result(rubric)
    result["input_refs"] = {"transcript_sha256": transcript_sha, "trace_sha256": trace_sha}

    # 1. A human_rubric is never scored by a model.
    if rubric["kind"] == "human_rubric":
        result["status"] = "INCONCLUSIVE"
        result["rationale"] = ("human_rubric: a person must decide this; a model "
                               "never scores a human_rubric item.")
        result["review"] = {"human_required": True,
                            "reasons": ["human_rubric requires a human reviewer"]}
        result["judge"] = _no_model_judge(rubric, transcript_sha, trace_sha,
                                          reason="human_rubric (no model call)")
        return result

    # 2. Missing required evidence -> INCONCLUSIVE (no model call).
    missing = _evidence_present(rubric, turns, spans)
    if missing is not None:
        result["status"] = "INCONCLUSIVE"
        result["rationale"] = missing
        result["review"] = {"human_required": False, "reasons": []}
        result["judge"] = _no_model_judge(rubric, transcript_sha, trace_sha, reason=missing)
        return result

    if judge is None:
        judge = OllamaJudge(model=rubric["evaluation"].get("model"))

    # Content addressing: cache_key = sha256(provider:model + prompt_sha256 + input_sha256).
    input_payload = {
        "criterion": rubric["criterion"],
        "evidence": rubric["evidence"],
        "examples": rubric.get("examples") or {},
        "response_values": rubric["response"]["values"],
        "repetitions": rubric["evaluation"]["repetitions"],
        "aggregation": rubric["evaluation"]["aggregation"],
        "confidence_required": rubric["evaluation"]["confidence_required"],
        "transcript": turns,
        "trace": spans if "tool_trace" in rubric["evidence"] else [],
    }
    input_sha = _sha256_json(input_payload)
    p_sha = prompt_sha256()
    model_id = judge.model
    cache_key = _sha256_text(f"{judge.provider}:{model_id}\n{p_sha}\n{input_sha}")

    cached_record = cache.get(cache_key) if cache is not None else None

    # 3. Cache hit (and not --no-cache): replay the byte-identical stored verdict.
    if cached_record is not None and not no_cache:
        return _from_cached(cached_record)

    # 4. Fresh query: run the model `repetitions` times, aggregate.
    system = _SYSTEM_PROMPT
    user = _render_user_prompt(rubric, turns, spans)
    reps = rubric["evaluation"]["repetitions"]
    try:
        digest = judge.model_digest()
        votes_full = [_one_vote(judge, system, user) for _ in range(reps)]
    except JudgeError as exc:
        result["status"] = "ERROR"
        result["rationale"] = f"judge backend failed: {exc}"
        result["review"] = {"human_required": False, "reasons": []}
        j = _no_model_judge(rubric, transcript_sha, trace_sha, reason=str(exc))
        j.update({"model": model_id, "provider": judge.provider,
                  "input_sha256": input_sha, "cache_key": cache_key})
        result["judge"] = j
        return result

    votes = [v["verdict"] for v in votes_full]
    agg = _aggregate(votes)
    conf_req = rubric["evaluation"]["confidence_required"]
    status = agg["status"]
    # Low confidence on a would-be decisive verdict is honest INCONCLUSIVE.
    if status in ("PASS", "FAIL") and agg["confidence"] < conf_req:
        status = "INCONCLUSIVE"
    # rationale/citations from the first vote matching the decisive verdict,
    # else the first vote.
    decisive = None
    if status in ("PASS", "FAIL"):
        want = status.lower()
        decisive = next((v for v in votes_full if v["verdict"] == want), None)
    chosen = decisive or votes_full[0]

    record = {
        "id": rubric["id"],
        "kind": KIND,
        "deterministic": False,
        "status": status,
        "rationale": chosen["rationale"] or _default_rationale(status, votes),
        "judge": {
            "model": model_id,
            "model_digest": digest,
            "provider": judge.provider,
            "prompt_id": PROMPT_ID,
            "prompt_version": PROMPT_VERSION,
            "prompt_sha256": p_sha,
            "temperature": 0,
            "input_sha256": input_sha,
            "cache_key": cache_key,
            "votes": votes,
            "repetitions": reps,
            "aggregation": AGGREGATION,
            "disagreement": agg["disagreement"],
            "confidence": agg["confidence"],
            "confidence_required": conf_req,
            "citations": chosen["citations"],
        },
        "input_refs": {"transcript_sha256": transcript_sha, "trace_sha256": trace_sha},
        "review": _review_block(rubric, status, agg["disagreement"], agg["confidence"], conf_req),
    }
    if rubric.get("dimension"):
        record["dimension"] = rubric["dimension"]

    verdict_sha = _sha256_json(_signable(record))
    record["judge"]["verdict_sha256"] = verdict_sha

    if sign:
        _sign_record(record)

    # --no-cache drift: diff the fresh verdict against the cached one, never hide it.
    if cached_record is not None and no_cache:
        record["judge"]["drift"] = _diff_verdicts(cached_record, record)

    # Persist the fresh verdict (unless this was an explicit no-cache re-query
    # against an existing entry -- leave the cached baseline intact so drift
    # stays visible on the next default run).
    if cache is not None and not (no_cache and cached_record is not None):
        cache.put(cache_key, record)

    out = json.loads(_canonical(record))
    out["judge"]["cached"] = False
    return out


def _default_rationale(status: str, votes: List[str]) -> str:
    if status == "INCONCLUSIVE":
        return f"votes {votes} were not a confident unanimous decision"
    return f"aggregated verdict {status} from votes {votes}"


def _no_model_judge(rubric: Dict[str, Any], transcript_sha, trace_sha,
                    *, reason: str) -> Dict[str, Any]:
    """A ``judge`` provenance block for a result that did NOT call a model
    (human_rubric, missing evidence). It still records the pinned model,
    prompt, and temperature the run would have used, so provenance is complete
    and honest; ``model_digest`` is null (no call was made), ``cached`` false,
    ``votes`` empty."""
    return {
        "model": (rubric.get("evaluation") or {}).get("model") or DEFAULT_JUDGE_MODEL,
        "model_digest": None,
        "provider": "none",
        "prompt_id": PROMPT_ID,
        "prompt_version": PROMPT_VERSION,
        "prompt_sha256": prompt_sha256(),
        "temperature": 0,
        "input_sha256": _sha256_json({"reason": reason}),
        "cache_key": _sha256_text(reason),
        "cached": False,
        "votes": [],
        "repetitions": (rubric.get("evaluation") or {}).get("repetitions", DEFAULT_REPETITIONS),
        "aggregation": AGGREGATION,
        "disagreement": False,
        "confidence": 0.0,
        "confidence_required": (rubric.get("evaluation") or {}).get(
            "confidence_required", DEFAULT_CONFIDENCE_REQUIRED),
        "citations": [],
    }


def _signable(record: Dict[str, Any]) -> Dict[str, Any]:
    """The record body a verdict_sha256 / signature covers: everything EXCEPT
    the runtime-only fields (``cached``, ``drift``, ``verdict_sha256``,
    ``signer``, ``signature``), so a cache hit and a fresh compute of the same
    verdict produce the same digest, and a signature can never be lifted onto a
    different body."""
    r = json.loads(_canonical(record))
    j = r.get("judge") or {}
    for k in ("cached", "drift", "verdict_sha256", "signer", "signature"):
        j.pop(k, None)
    r["judge"] = j
    return r


def _sign_record(record: Dict[str, Any]) -> None:
    """Optionally attach an Ed25519 ('human') or HMAC ('human-shared')
    "judge-record" signature over the verdict, reusing sign.py / receipt.py.
    Opt-in and graceful: no key configured -> the verdict is simply unsigned
    (never a hollow/false signature)."""
    subject = _canonical(_signable(record)).encode("utf-8")
    try:
        from . import sign as _sign
        saved = _sign.load_signing_key()
        if saved is not None:
            kid, priv = saved
            sig = _sign.sign(subject, priv)
            record["judge"]["signer"] = {"key_id": kid, "algo": "ed25519"}
            record["judge"]["signature"] = sig
            return
    except Exception:
        pass
    try:
        import hashlib as _h
        import hmac as _hmac
        from . import receipt as _receipt
        shared = _receipt.load_key()
        if shared is not None:
            sig = _hmac.new(shared, subject, _h.sha256).hexdigest()
            record["judge"]["signer"] = {
                "key_id": _h.sha256(shared).hexdigest()[:16], "algo": "hmac"}
            record["judge"]["signature"] = sig
    except Exception:
        pass


def _from_cached(record: Dict[str, Any]) -> Dict[str, Any]:
    out = json.loads(_canonical(record))
    out["judge"]["cached"] = True
    return out


def _diff_verdicts(cached: Dict[str, Any], fresh: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """Diff a fresh verdict against the cached baseline. Returns None if the
    verdict content is byte-identical (same verdict_sha256), else a drift object
    naming exactly what changed -- surfaced on the result, never hidden."""
    c_sha = (cached.get("judge") or {}).get("verdict_sha256") or _sha256_json(_signable(cached))
    f_sha = (fresh.get("judge") or {}).get("verdict_sha256") or _sha256_json(_signable(fresh))
    if c_sha == f_sha:
        return None
    return {
        "changed": True,
        "cached_status": cached.get("status"),
        "fresh_status": fresh.get("status"),
        "cached_votes": (cached.get("judge") or {}).get("votes"),
        "fresh_votes": (fresh.get("judge") or {}).get("votes"),
        "cached_verdict_sha256": c_sha,
        "fresh_verdict_sha256": f_sha,
        "note": ("the fresh model verdict differs from the cached one -- the "
                 "model call is not claimed deterministic; only cached replay is "
                 "byte-identical"),
    }


# =========================================================================
# Lane + envelope
# =========================================================================

def evaluate_rubric_lane(
    rubrics: List[Dict[str, Any]],
    *,
    transcript: Optional[Sequence[Dict[str, Any]]] = None,
    trace: Optional[Sequence[Dict[str, Any]]] = None,
    judge: Optional[Judge] = None,
    cache: Optional["VerdictCache"] = None,
    no_cache: bool = False,
    gate: bool = False,
    sign: bool = False,
) -> Dict[str, Any]:
    """Evaluate a whole rubric lane and return a ``rubric.v1`` envelope. ADVISORY
    by default (``exit_code`` 0 regardless of verdicts); ``gate=True`` makes any
    FAIL gate (exit 1). Never merges into a deterministic count and never emits
    an ``overall_score``."""
    results = [
        evaluate_rubric(r, transcript=transcript, trace=trace, judge=judge,
                        cache=cache, no_cache=no_cache, sign=sign)
        for r in rubrics
    ]
    return rubric_envelope(results, gate=gate)


def rubric_envelope(results: List[Dict[str, Any]], *, gate: bool = False) -> Dict[str, Any]:
    counts = {"pass": 0, "fail": 0, "inconclusive": 0, "error": 0}
    for r in results:
        st = r.get("status")
        if st == "PASS":
            counts["pass"] += 1
        elif st == "FAIL":
            counts["fail"] += 1
        elif st == "ERROR":
            counts["error"] += 1
        else:
            counts["inconclusive"] += 1
    # Advisory by default: a rubric FAIL is reported but never gates. With
    # --gate, a FAIL gates like a deterministic FAIL (exit 1). INCONCLUSIVE and
    # ERROR stay advisory even under --gate -- a model that could not decide, or
    # a backend that was down, never silently blocks a release.
    exit_code = 1 if (gate and counts["fail"] > 0) else 0
    note = (
        f"{counts['pass']} pass, {counts['fail']} fail, "
        f"{counts['inconclusive']} inconclusive, {counts['error']} error across "
        f"{len(results)} rubric result(s); model-judged (advisory), "
        "deterministic:false, never merged into the deterministic counts and "
        "never a blended or overall number. "
        + ("GATED: a FAIL gates CI." if gate else "ADVISORY: no verdict gates CI.")
    )
    return {
        "schema": SCHEMA,
        "exit_code": exit_code,
        "advisory": not gate,
        "gated": gate,
        "results": results,
        "summary": {**counts, "note": note},
    }


# =========================================================================
# Content-addressed verdict cache (reuses fleet/store.py's ArtifactStore)
# =========================================================================

class VerdictCache:
    """A content-addressed verdict cache. The verdict BLOB is stored via
    :class:`hotato.fleet.store.ArtifactStore` (sha256 of its bytes -> integrity,
    de-dup, ``verify``); a thin key index maps ``cache_key`` (the content
    address of model+prompt+input) to that blob digest. A hit returns the
    byte-identical stored verdict."""

    def __init__(self, root: str):
        from .fleet.store import ArtifactStore
        self.root = os.path.abspath(root)
        self.store = ArtifactStore(os.path.join(self.root, "store"))
        self.index_dir = os.path.join(self.root, "keys")
        os.makedirs(self.index_dir, exist_ok=True)

    def _key_path(self, cache_key: str) -> str:
        sub = os.path.join(self.index_dir, cache_key[:2])
        os.makedirs(sub, exist_ok=True)
        return os.path.join(sub, cache_key)

    def get(self, cache_key: str) -> Optional[Dict[str, Any]]:
        path = self._key_path(cache_key)
        if not os.path.isfile(path):
            return None
        try:
            with open(path, encoding="utf-8") as fh:  # open-ok: our own index file
                digest = fh.read().strip()
        except OSError:
            return None
        if not digest or not self.store.has(digest):
            return None
        if not self.store.verify(digest):
            return None
        return self.store.get_json(digest)

    def put(self, cache_key: str, record: Dict[str, Any]) -> str:
        # Store the verdict WITHOUT runtime-only fields so a cache hit replays a
        # stable, byte-identical record.
        stored = json.loads(_canonical(record))
        stored.get("judge", {}).pop("cached", None)
        stored.get("judge", {}).pop("drift", None)
        digest = self.store.put_json(stored, kind="judge-verdict")
        tmp = self._key_path(cache_key) + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:  # open-ok: our own index file
            fh.write(digest)
        os.replace(tmp, self._key_path(cache_key))
        return digest


# =========================================================================
# Calibration: agreement + selective accuracy on a human-labeled corpus
# =========================================================================

def load_labeled_corpus(directory: str) -> List[Dict[str, Any]]:
    """Load a human-labeled calibration corpus: every ``*.json`` file under
    ``directory`` is one item ``{id?, rubric, transcript, trace?, label,
    split?}`` where ``label`` is the human ground truth (pass|fail|inconclusive)
    and ``split`` is optionally 'train' or 'held_out'. Human labels are
    MANDATORY here -- calibration is exactly where a person's judgment is
    required; the model is scored AGAINST it, never used to create it."""
    items: List[Dict[str, Any]] = []
    if not os.path.isdir(directory):
        raise ValueError(f"labeled corpus directory not found: {directory!r}")
    for name in sorted(os.listdir(directory)):
        if not name.endswith(".json"):
            continue
        path = os.path.join(directory, name)
        obj = _load_json_file(path)
        if not isinstance(obj, dict) or "rubric" not in obj or "label" not in obj:
            raise ValueError(
                f"{path!r}: a calibration item needs a 'rubric' and a human "
                "'label' (pass|fail|inconclusive)"
            )
        label = str(obj["label"]).strip().lower()
        if label not in CATEGORICAL_VALUES:
            raise ValueError(
                f"{path!r}: 'label' must be one of {CATEGORICAL_VALUES}, got "
                f"{obj['label']!r}"
            )
        obj.setdefault("id", os.path.splitext(name)[0])
        obj["label"] = label
        items.append(obj)
    if not items:
        raise ValueError(f"no *.json calibration items found in {directory!r}")
    return items


def _held_out(item: Dict[str, Any], held_out_pct: int) -> bool:
    """Deterministic held-out membership: an explicit ``split: held_out`` wins;
    otherwise a stable hash of the item id partitions reproducibly (same corpus
    -> same split, no RNG)."""
    split = item.get("split")
    if split in ("held_out", "test"):
        return True
    if split in ("train",):
        return False
    bucket = int(hashlib.sha256(str(item["id"]).encode("utf-8")).hexdigest(), 16) % 100
    return bucket < held_out_pct


def calibrate(
    items: List[Dict[str, Any]],
    *,
    judge: Optional[Judge] = None,
    cache: Optional[VerdictCache] = None,
    held_out_pct: int = 30,
) -> Dict[str, Any]:
    """Score each labeled item with the judge and compute AGREEMENT (fraction of
    the held-out set where the model verdict equals the human label) and
    SELECTIVE ACCURACY (agreement restricted to items where the model did NOT
    abstain -- did not return INCONCLUSIVE/ERROR). Returns a reproducible
    ARTIFACT (raw counts + per-item detail + provenance), NEVER a marketing
    number. The status status map: PASS->pass, FAIL->fail, INCONCLUSIVE/ERROR->
    abstain."""
    per_item: List[Dict[str, Any]] = []
    held_agree = held_total = held_answered = held_answered_agree = 0
    model_id = judge.model if judge is not None else DEFAULT_JUDGE_MODEL
    model_digest = judge.model_digest() if judge is not None else None
    for it in items:
        rub = validate_rubric_object(it["rubric"])
        res = evaluate_rubric(rub, transcript=it.get("transcript"),
                              trace=it.get("trace"), judge=judge, cache=cache)
        status = res["status"]
        model_verdict = {"PASS": "pass", "FAIL": "fail"}.get(status, "abstain")
        human = it["label"]
        held = _held_out(it, held_out_pct)
        agrees = (model_verdict == human)
        answered = model_verdict != "abstain"
        row = {
            "id": it["id"], "rubric_id": rub["id"], "human_label": human,
            "model_status": status, "model_verdict": model_verdict,
            "held_out": held, "agrees": agrees, "answered": answered,
        }
        per_item.append(row)
        if held:
            held_total += 1
            if agrees:
                held_agree += 1
            if answered:
                held_answered += 1
                if agrees:
                    held_answered_agree += 1

    agreement = (held_agree / held_total) if held_total else None
    selective_accuracy = (held_answered_agree / held_answered) if held_answered else None
    coverage = (held_answered / held_total) if held_total else None
    return {
        "schema": "hotato.rubric-calibration.v1",
        "method": {
            "held_out_pct": held_out_pct,
            "held_out_rule": "explicit split field, else stable sha256(id) % 100 < pct",
            "agreement": "held-out items where model verdict == human label / all held-out items",
            "selective_accuracy": "held-out ANSWERED items (model did not abstain) that agree / answered",
            "abstain": "INCONCLUSIVE or ERROR counts as an abstention, not a disagreement",
        },
        "judge": {"model": model_id, "model_digest": model_digest,
                  "prompt_id": PROMPT_ID, "prompt_version": PROMPT_VERSION,
                  "prompt_sha256": prompt_sha256(), "temperature": 0},
        "counts": {
            "total": len(items), "held_out": held_total,
            "held_out_agree": held_agree, "held_out_answered": held_answered,
            "held_out_answered_agree": held_answered_agree,
        },
        "agreement": agreement,
        "selective_accuracy": selective_accuracy,
        "answer_coverage": coverage,
        "note": ("A reproducible calibration artifact on a HUMAN-labeled held-out "
                 "set -- raw counts and method, not a marketing number. Re-running "
                 "on the same corpus with the same model reproduces the same split "
                 "and (via the verdict cache) the same verdicts."),
        "per_item": per_item,
    }


# =========================================================================
# Text rendering (CLI)
# =========================================================================

def render_run_text(envelope: Dict[str, Any]) -> str:
    s = envelope["summary"]
    lines = [
        f"hotato rubric: {s['pass']} pass / {s['fail']} fail / "
        f"{s['inconclusive']} inconclusive / {s['error']} error "
        f"({'GATED' if envelope.get('gated') else 'advisory'})",
        "model-judged (deterministic:false); never merged with deterministic "
        "counts; no blended or overall number.",
    ]
    for r in envelope["results"]:
        j = r.get("judge") or {}
        cached = "cached" if j.get("cached") else "fresh"
        model = j.get("model", "?")
        dim = f" [{r['dimension']}]" if r.get("dimension") else ""
        lines.append(f"  [{r['status']:<12}] {r['id']}{dim}  ({model}, {cached})")
        if r.get("rationale"):
            lines.append(f"      {r['rationale']}")
        if j.get("drift"):
            lines.append(f"      DRIFT: cached={j['drift'].get('cached_status')} "
                        f"-> fresh={j['drift'].get('fresh_status')}")
        rev = r.get("review") or {}
        if rev.get("human_required"):
            lines.append(f"      human review required: {', '.join(rev.get('reasons') or [])}")
    return "\n".join(lines) + "\n"


__all__ = [
    "SCHEMA", "KIND", "PROMPT_ID", "PROMPT_VERSION", "CATEGORICAL_VALUES",
    "STATUSES", "EVIDENCE_KINDS", "AGGREGATION", "DEFAULT_JUDGE_MODEL",
    "DEFAULT_OLLAMA_ENDPOINT", "ENDPOINT_ENV", "MODEL_ENV",
    "JudgeError", "EgressRefused", "ParseMiss",
    "Judge", "OllamaJudge", "HostedJudge", "VerdictCache",
    "prompt_sha256", "validate_rubric_object", "load_rubrics_file",
    "parse_verdict", "evaluate_rubric", "evaluate_rubric_lane",
    "rubric_envelope", "load_labeled_corpus", "calibrate", "render_run_text",
]
