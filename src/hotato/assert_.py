"""``hotato assert``: the deterministic assertion engine (``assert.v1``).

This is the honesty wall made structural. Every assertion kind here --
``phrase``, ``pii``, ``policy``, ``tool_call``, ``outcome`` -- is 100%
deterministic: pure regex, checksum arithmetic, and span/dict lookups, never
a model call. Same inputs, same assertions file -> byte-identical results,
every time, offline. A ``judge`` kind (an LLM-scored rubric) is a separate,
quarantined capability that is NOT built here; this module's envelope always
reports ``summary.judge`` at ``{"pass": 0, "fail": 0}`` with a note saying so,
and NEVER emits a merged score, an ``overall_score``, or any blended
percentage across kinds -- by construction, not by convention.

Three layers:

* :func:`parse_assertions_yaml` / :func:`validate_assertions_doc` -- read an
  ``assertions.yaml`` (or an equivalent hand-built dict) into validated
  per-assertion specs. Malformed input (bad kind, missing required
  kind-specific field, a version this build does not support, an invalid
  regex) raises ``ValueError`` immediately -- validation runs before any
  assertion is evaluated, so a bad file never produces a partial result set.
  ``assertions.yaml`` is parsed with a small, dependency-free subset of YAML
  (block mappings/sequences, flow ``[...]``/``{...}`` collections, quoted or
  bare scalars, ``#`` comments): hotato's core stays zero third-party
  dependency, so no PyYAML is required. A document that is already valid
  JSON is also accepted directly (JSON is close enough to this YAML subset
  that machine-generated assertion files can just emit it).
* :func:`build_context` -- assembles the ``Context`` an assertion run is
  evaluated against: ``transcript`` (turns; role/text/start/end), ``spans``
  (``hotato.voice_trace.v1`` spans, read by :mod:`hotato.trace`), and
  ``timing`` (a run's scored events, read-only). A piece of context that was
  never supplied stays ``None`` (distinct from an empty list/dict that WAS
  supplied but happens to be empty) so an assertion whose required input is
  simply absent reports ``INCONCLUSIVE``, never a fabricated ``FAIL``.
* :func:`evaluate_assertion` / :func:`run_assertions` -- evaluate one (or a
  whole document's worth of) assertions against a ``Context``, producing the
  ``assert.v1`` envelope (schema: ``schema/assert.v1.json``). Exit-code
  convention (mirroring the rest of hotato: 0 pass / 1 fail / 2 usage error):
  under the default ``inconclusive_policy`` ``"report"`` the envelope's own
  ``exit_code`` is 1 if any assertion's deterministic status is ``FAIL``,
  else 0 -- an ``INCONCLUSIVE`` result (absent required input) never gates.
  A suite can opt into gating on missing input: ``"fail"`` makes
  ``INCONCLUSIVE`` gate like a ``FAIL`` (exit 1), ``"refuse"`` makes it exit 2
  (a refusal to return a verdict, precedence over a ``FAIL``); see
  :func:`envelope_from_results`. A malformed assertions document -- including
  a bad ``inconclusive_policy`` value -- raises ``ValueError`` before an
  envelope is ever built (the caller's existing exit-2 path, see
  :mod:`hotato.errors`).

``tool_call`` reads ONLY the ingested trace's spans, never transcript text
(Cekura's honest boundary applies here too: a tool call absent from the
ingested trace is reported not-called, independent of whatever happened at
runtime). ``pii`` never echoes the raw matched text anywhere in a result --
only a redacted transcript artifact and hit metadata (detector name, turn
index, role).
"""

from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Sequence, Tuple

from .errors import (
    is_safe_bare_token as _is_safe_bare_token,
)
from .errors import (
    load_json_file as _load_json_file,
)
from .errors import (
    open_regular as _open_regular,
)

__all__ = [
    "SCHEMA",
    "Turn",
    "Context",
    "build_context",
    "load_transcript_file",
    "load_spans_file",
    "parse_assertions_yaml",
    "validate_assertions_doc",
    "KINDS",
    "RUBRIC_KINDS",
    "ALL_KINDS",
    "RESULT_DIMENSIONS",
    "DETECTOR_NAMES",
    "DETECTORS",
    "DEFAULT_POLICY_PACK",
    "load_policy_pack",
    "evaluate_assertion",
    "run_assertions",
    "run_assertions_from_yaml",
    "run_assertions_from_file",
    "envelope_from_results",
    "INCONCLUSIVE_POLICIES",
    "DEFAULT_INCONCLUSIVE_POLICY",
    "render_run_text",
    "build_init_stub",
    "render_assertions_yaml",
]

SCHEMA = "assert.v1"
SUPPORTED_DOC_VERSION = 1
# The deterministic assertion kinds -- the honesty wall. Every one of these is
# pure regex / checksum / span-lookup / state-query / string-match arithmetic,
# never a model call, so a result always carries ``deterministic: true`` and
# an INCONCLUSIVE result reflects ABSENT input, never a non-deterministic
# judgment. The original five (``phrase`` .. ``outcome``) are unchanged; the
# Phase-1 expanded kinds are additive.
#
#   phrase/pii/policy/outcome     -- read the transcript / timing (as before)
#   tool_call                     -- reads voice_trace.v1 tool spans (as before)
#   tool_result / tool_error      -- Authority 1: read tool spans' result/error
#   state / state_change          -- Authority 2: query a post-call state adapter
#   handoff / dtmf / termination  -- read the authenticated trace's spans
#   latency / timing_contract     -- numeric timing (trace latency_ms / a .hotato
#                                    timing contract re-verified via `contract verify`)
#   entity_accuracy               -- deterministic entity/string match vs a
#                                    supplied reference (NOT WER)
#   sequence / count              -- ordered / counted spans (or phrases)
#
# HONESTY INVARIANT (structural): tool_result / tool_error / state /
# state_change (Authorities 1 & 2, listed in :data:`_AUTHORITY_1_2_KINDS`) read
# ONLY the authenticated trace spans / the state adapter -- never the
# transcript. An agent's spoken claim ("I issued the refund") can therefore
# never satisfy one of them; there is no model/LLM code path anywhere here.
KINDS = (
    "phrase", "pii", "policy", "tool_call", "outcome",
    "tool_result", "tool_error", "state", "state_change", "handoff", "dtmf",
    "termination", "latency", "timing_contract", "entity_accuracy",
    "sequence", "count",
)

# The judge lane's kinds -- NAMED so a conversation-test / assert document may
# reference them, but they do NOT run inside the deterministic assert.v1 lane:
# the real model-judge lives in the SEPARATE rubric lane (hotato.rubric), which
# emits deterministic:false rubric.v1 results with full provenance. When a
# rubric kind appears in a raw assert.v1 document its evaluator here routes it
# out honestly -- a deterministic INCONCLUSIVE pointing to the rubric lane,
# WITHOUT ever calling a model, so assert.v1's ``summary.judge`` stays the
# ``{"pass": 0, "fail": 0}`` quarantine and the deterministic guarantee holds.
RUBRIC_KINDS = ("human_rubric", "judge_rubric")

# Every recognized assertion kind -- what ``validate_assertions_doc`` accepts
# and what the schema's ``result.kind`` enum lists.
ALL_KINDS = KINDS + RUBRIC_KINDS

# The five report DIMENSIONS a result may be TAGGED with (a grouping key for
# the per-dimension scorecard the report renders -- never a weight, never part
# of a blended score). The ``dimension`` on an assertion is OPTIONAL and
# propagates verbatim onto its result (see :func:`_base_result`), so an untagged
# assertion's result is byte-identical to before this existed. Defined here --
# and mirrored in ``hotato.conversation_test.REPORT_DIMENSIONS`` and
# ``hotato.report`` -- rather than imported: ``conversation_test`` imports FROM
# this module, so importing it back would close an import cycle.
RESULT_DIMENSIONS = ("outcome", "policy", "conversation", "speech", "reliability")

# The Authority 1 & 2 kinds: their evaluators are STRUCTURALLY unable to be
# satisfied by an agent's spoken claim, because they read the authenticated
# trace spans / the state adapter only, never the transcript, and never a
# model verdict. Named here so the invariant is testable by name.
_AUTHORITY_1_2_KINDS = frozenset(
    {"tool_result", "tool_error", "state", "state_change"}
)

# How an INCONCLUSIVE result (a statement about ABSENT required input, never a
# non-deterministic judgment) gates the run's exit code. The default,
# ``"report"``, is exactly the historical behavior -- INCONCLUSIVE never
# forces a non-zero exit -- so an existing suite's exit code is unchanged when
# the policy is left unset. A CI/compliance suite that must NOT stay green on
# missing input opts into ``"fail"`` (INCONCLUSIVE gates like a FAIL) or
# ``"refuse"`` (INCONCLUSIVE refuses to return a verdict at all, exit 2).
INCONCLUSIVE_POLICIES = ("report", "fail", "refuse")
DEFAULT_INCONCLUSIVE_POLICY = "report"


def _validate_inconclusive_policy(value: Any, source: str) -> str:
    """Return ``value`` unchanged if it is one of :data:`INCONCLUSIVE_POLICIES`,
    else raise ``ValueError`` (the caller's usage-error / exit-2 path). Used
    both for the optional top-level ``inconclusive_policy`` key in an
    assertions document and for an explicit caller/CLI override."""
    if value not in INCONCLUSIVE_POLICIES:
        raise ValueError(
            f"{source}: 'inconclusive_policy' must be one of "
            f"{INCONCLUSIVE_POLICIES}, got {value!r}"
        )
    return value


# =========================================================================
# Context: transcript + spans + timing, read-only inputs to every evaluator
# =========================================================================

@dataclass
class Turn:
    """One transcript turn. ``role`` is the speaker label (``"caller"`` /
    ``"agent"`` / ``None`` for an undifferentiated single-track transcript).
    ``start``/``end`` are optional seconds, carried through for context only
    (no assertion kind here re-derives timing from them)."""

    role: Optional[str]
    text: str
    start: Optional[float] = None
    end: Optional[float] = None


@dataclass
class Context:
    """The evaluation context every assertion kind reads from.

    ``transcript`` is ``None`` when no transcript was ever supplied to
    :func:`build_context` (distinct from ``[]``, a transcript that WAS
    supplied and is genuinely empty -- e.g. a silent call). Same for
    ``spans``. ``timing`` defaults to ``None`` (no scored-events context).
    ``state_adapter`` (Authority 2, the post-call system of record) defaults
    to ``None`` -- a ``state``/``state_change`` assertion evaluated without
    one reports ``INCONCLUSIVE`` (no way to query state), never a guess.
    An evaluator that needs a piece of context which is ``None`` reports
    ``INCONCLUSIVE``, never a guessed ``PASS``/``FAIL``."""

    transcript: Optional[List[Dict[str, Any]]] = None
    spans: Optional[List[Dict[str, Any]]] = None
    timing: Any = None
    state_adapter: Any = None


def _norm_turn(t: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "role": t.get("role"),
        "text": t.get("text") or "",
        "start": t.get("start"),
        "end": t.get("end"),
    }


def _turns_from_doc(doc: Any, source: str) -> List[Dict[str, Any]]:
    """Accept a plain list of turn dicts, or the ``{"segments": [...]}``
    shape :mod:`hotato.transcribe` / the MCP surface write (optionally
    nested one level under a ``"transcript"`` key, the shape a saved
    ``assert``-input envelope carries it at)."""
    if isinstance(doc, list):
        segs = doc
    elif isinstance(doc, dict):
        if isinstance(doc.get("segments"), list):
            segs = doc["segments"]
        elif isinstance(doc.get("transcript"), dict) and isinstance(
            doc["transcript"].get("segments"), list
        ):
            segs = doc["transcript"]["segments"]
        else:
            raise ValueError(
                f"{source!r}: expected a JSON array of transcript turns, or "
                "an object with a 'segments' list (the shape hotato's "
                "transcribe / MCP surfaces write); found neither"
            )
    else:
        raise ValueError(
            f"{source!r}: a transcript file must be a JSON array or object"
        )
    return [_norm_turn(s) for s in segs]


def load_transcript_file(path: str) -> List[Dict[str, Any]]:
    """Load a transcript from a JSON file: a plain array of ``{"role",
    "text", "start", "end"}`` turns, or the ``{"segments": [...]}`` shape
    already shipped by :mod:`hotato.transcribe` / the MCP surface. This is
    the ``--transcript FILE`` seam: ``hotato assert`` works fully without the
    optional ``[transcribe]`` extra, on any transcript an operator already
    has. Routed through :func:`hotato.errors.open_regular`, so a FIFO/named
    pipe path raises immediately instead of blocking forever."""
    doc = _load_json_file(path)
    return _turns_from_doc(doc, path)


def load_spans_file(path: str) -> List[Dict[str, Any]]:
    """Load ``hotato.voice_trace.v1`` spans from a ``voice_trace.jsonl`` file
    (the shape :func:`hotato.trace.ingest_otel` writes and a bundle's
    ``traces/voice_trace.jsonl`` carries). Read-only with respect to the
    trace; ``tool_call`` assertions read exactly these spans, never
    transcript text.

    ``trace`` is imported HERE, not at module level: ``trace.py`` imports
    ``contract``, which imports ``report``, which imports this module for
    ``SCHEMA`` -- a module-level ``from . import trace`` here would close
    that into an import cycle (contract -> report -> assert_ -> trace ->
    contract). Deferring it is the same fix ``contract.py`` already applies
    to its own ``assert_`` import (see its ``_bundle_trace_spans``)."""
    from . import trace as _trace

    vt = _trace.load_voice_trace_jsonl(path)
    return list(vt.get("spans") or [])


def build_context(
    *,
    transcript: Optional[Sequence[Dict[str, Any]]] = None,
    transcript_path: Optional[str] = None,
    spans: Optional[Sequence[Dict[str, Any]]] = None,
    trace_path: Optional[str] = None,
    timing: Any = None,
    state_adapter: Any = None,
) -> Context:
    """Build the :class:`Context` an assertion run is evaluated against.

    Exactly one of ``transcript`` (a ready list of turn dicts) or
    ``transcript_path`` (a JSON file, see :func:`load_transcript_file`) may
    be given; passing both is a ``ValueError``. Same for ``spans`` /
    ``trace_path`` (see :func:`load_spans_file`). ``timing`` is the scored
    run's events (``envelope.v1``'s ``events`` list, or a single event dict)
    passed straight through as read-only context for the ``outcome`` kind's
    ``field_present`` sub-predicate; hotato never recomputes it here.
    ``state_adapter`` (a :mod:`hotato.state_adapter` adapter, Authority 2) is
    the post-call system of record the ``state``/``state_change`` kinds query;
    omitting it leaves those kinds INCONCLUSIVE (no way to check state).

    Omitting BOTH members of a pair leaves that piece of context ``None``
    (absent), which every evaluator treats as INCONCLUSIVE rather than
    guessing a result -- so a caller that genuinely has no trace for this
    call should omit ``spans``/``trace_path`` entirely, not pass ``[]``
    (which means "a trace was ingested and it has zero spans")."""
    if transcript is not None and transcript_path is not None:
        raise ValueError(
            "pass either transcript or transcript_path to build_context, "
            "not both"
        )
    if spans is not None and trace_path is not None:
        raise ValueError(
            "pass either spans or trace_path to build_context, not both"
        )

    if transcript_path is not None:
        turns: Optional[List[Dict[str, Any]]] = load_transcript_file(transcript_path)
    elif transcript is not None:
        turns = [_norm_turn(dict(t)) for t in transcript]
    else:
        turns = None

    if trace_path is not None:
        span_list: Optional[List[Dict[str, Any]]] = load_spans_file(trace_path)
    elif spans is not None:
        span_list = [dict(s) for s in spans]
    else:
        span_list = None

    return Context(
        transcript=turns, spans=span_list, timing=timing,
        state_adapter=state_adapter,
    )


# =========================================================================
# A small, dependency-free YAML subset (block + flow), plus a JSON fast path
# =========================================================================

def _yaml_strip_comment(line: str) -> str:
    """Drop a trailing ``#`` comment (one starting the line or preceded by
    whitespace); text inside quotes is never treated as a comment."""
    out = []
    quote = None
    prev_ws = True
    for ch in line:
        if quote is not None:
            out.append(ch)
            if ch == quote:
                quote = None
            continue
        if ch in ("'", '"'):
            quote = ch
            out.append(ch)
            prev_ws = False
            continue
        if ch == "#" and prev_ws:
            break
        out.append(ch)
        prev_ws = ch in (" ", "\t")
    return "".join(out)


def _yaml_scalar(raw: str) -> Any:
    """Coerce a bare YAML scalar to bool/null/int/float/string; a quoted
    string is returned verbatim (never coerced)."""
    s = raw.strip()
    if len(s) >= 2 and s[0] == s[-1] and s[0] in ("'", '"'):
        return s[1:-1]
    low = s.lower()
    if low == "true":
        return True
    if low == "false":
        return False
    if low in ("null", "~", ""):
        return None
    try:
        return int(s)
    except ValueError:
        pass
    try:
        return float(s)
    except ValueError:
        pass
    return s


class _FlowParser:
    """Recursive-descent parser for one flow collection (``[...]`` /
    ``{...}``), e.g. ``[{tool_called: issue_refund}, {phrase: "x", role:
    agent}]``. Bare (unquoted) tokens are allowed as both keys and scalar
    values (unlike strict JSON), matching plain YAML flow style."""

    def __init__(self, s: str):
        self.s = s
        self.i = 0
        self.n = len(s)

    def parse(self) -> Any:
        val = self._value()
        self._skip_ws()
        if self.i != self.n:
            raise ValueError(
                f"unexpected trailing content at position {self.i} in "
                f"{self.s!r}"
            )
        return val

    def _skip_ws(self) -> None:
        while self.i < self.n and self.s[self.i] in " \t":
            self.i += 1

    def _value(self) -> Any:
        self._skip_ws()
        if self.i >= self.n:
            return None
        c = self.s[self.i]
        if c == "[":
            return self._list()
        if c == "{":
            return self._dict()
        raw = self._token(",}]")
        return _yaml_scalar(raw)

    def _token(self, stop_chars: str) -> str:
        self._skip_ws()
        if self.i < self.n and self.s[self.i] in ("'", '"'):
            quote = self.s[self.i]
            self.i += 1
            start = self.i
            while self.i < self.n and self.s[self.i] != quote:
                self.i += 1
            if self.i >= self.n:
                raise ValueError(f"unterminated quote in {self.s!r}")
            text = self.s[start:self.i]
            self.i += 1
            return text
        start = self.i
        while self.i < self.n and self.s[self.i] not in stop_chars:
            self.i += 1
        return self.s[start:self.i].rstrip()

    def _list(self) -> list:
        self.i += 1  # "["
        out: list = []
        self._skip_ws()
        if self.i < self.n and self.s[self.i] == "]":
            self.i += 1
            return out
        while True:
            out.append(self._value())
            self._skip_ws()
            if self.i >= self.n:
                raise ValueError(f"unterminated '[' in {self.s!r}")
            if self.s[self.i] == ",":
                self.i += 1
                self._skip_ws()
                continue
            if self.s[self.i] == "]":
                self.i += 1
                break
            raise ValueError(
                f"expected ',' or ']' at position {self.i} in {self.s!r}"
            )
        return out

    def _dict(self) -> dict:
        self.i += 1  # "{"
        out: dict = {}
        self._skip_ws()
        if self.i < self.n and self.s[self.i] == "}":
            self.i += 1
            return out
        while True:
            key = self._token(":,}]").strip()
            self._skip_ws()
            if self.i >= self.n or self.s[self.i] != ":":
                raise ValueError(f"expected ':' after key {key!r} in {self.s!r}")
            self.i += 1
            value = self._value()
            out[key] = value
            self._skip_ws()
            if self.i >= self.n:
                raise ValueError(f"unterminated '{{' in {self.s!r}")
            if self.s[self.i] == ",":
                self.i += 1
                self._skip_ws()
                continue
            if self.s[self.i] == "}":
                self.i += 1
                break
            raise ValueError(
                f"expected ',' or '}}' at position {self.i} in {self.s!r}"
            )
        return out


def _parse_flow(text: str) -> Any:
    return _FlowParser(text).parse()


def _yaml_split_key(content: str) -> Tuple[str, str, str]:
    """Split ``"key: value"`` on the first unquoted ``:``. Returns
    ``(key, "", "")`` when no unquoted ``:`` is found."""
    quote = None
    for idx, ch in enumerate(content):
        if quote:
            if ch == quote:
                quote = None
            continue
        if ch in ("'", '"'):
            quote = ch
            continue
        if ch == ":":
            return content[:idx], ":", content[idx + 1:]
    return content, "", ""


def _yaml_value(text: str) -> Any:
    text = text.strip()
    if not text:
        return None
    if text[0] in "[{":
        return _parse_flow(text)
    return _yaml_scalar(text)


def _yaml_lines(text: str) -> List[Tuple[int, str, int]]:
    """Tokenize into ``(indent, content, lineno)``, dropping blank lines and
    ``#`` comments. Tab indentation is refused (matches YAML)."""
    out = []
    for lineno, raw in enumerate(text.splitlines(), 1):
        lead = raw[: len(raw) - len(raw.lstrip(" \t"))]
        if "\t" in lead:
            raise ValueError(
                f"line {lineno}: tab indentation is not allowed; use spaces"
            )
        stripped = _yaml_strip_comment(raw)
        if not stripped.strip():
            continue
        indent = len(stripped) - len(stripped.lstrip(" "))
        content = stripped[indent:].rstrip()
        out.append((indent, content, lineno))
    return out


def _is_seq_item(content: str) -> bool:
    return content == "-" or content.startswith("- ")


def _yaml_parse_block(
    lines: List[Tuple[int, str, int]], i: int, min_indent: int
) -> Tuple[Any, int]:
    """Parse a mapping or sequence starting at ``lines[i]`` (whose own
    indent must be ``>= min_indent``); returns ``(value, next_i)``. Empty
    (``i`` at end, or insufficiently indented) yields ``(None, i)``."""
    if i >= len(lines) or lines[i][0] < min_indent:
        return None, i
    indent = lines[i][0]
    if _is_seq_item(lines[i][1]):
        return _yaml_parse_sequence(lines, i, indent)
    return _yaml_parse_mapping(lines, i, indent)


def _yaml_parse_sequence(
    lines: List[Tuple[int, str, int]], i: int, indent: int
) -> Tuple[list, int]:
    out: list = []
    while i < len(lines) and lines[i][0] == indent and _is_seq_item(lines[i][1]):
        content, lineno = lines[i][1], lines[i][2]
        after_dash = content[1:]
        rest = after_dash.lstrip(" ")
        spaces = len(after_dash) - len(rest)
        child_col = indent + 1 + spaces

        if rest == "":
            value, i = _yaml_parse_block(lines, i + 1, indent + 1)
            out.append(value)
            continue
        if _is_seq_item(rest):
            raise ValueError(
                f"line {lineno}: nested inline sequences are not supported"
            )

        key, sep, val = _yaml_split_key(rest)
        if not sep:
            out.append(_yaml_value(rest))
            i += 1
            continue

        mapping: Dict[str, Any] = {}
        key = key.strip()
        valtext = val.strip()
        i += 1
        if valtext:
            mapping[key] = _yaml_value(valtext)
        else:
            nested, i = _yaml_parse_block(lines, i, child_col + 1)
            mapping[key] = nested
        while (
            i < len(lines)
            and lines[i][0] == child_col
            and not _is_seq_item(lines[i][1])
        ):
            k2, s2, v2 = _yaml_split_key(lines[i][1])
            if not s2:
                raise ValueError(f"line {lines[i][2]}: expected 'key: value'")
            k2 = k2.strip()
            v2 = v2.strip()
            i += 1
            if v2:
                mapping[k2] = _yaml_value(v2)
            else:
                nested, i = _yaml_parse_block(lines, i, child_col + 1)
                mapping[k2] = nested
        out.append(mapping)
    return out, i


def _yaml_parse_mapping(
    lines: List[Tuple[int, str, int]], i: int, indent: int
) -> Tuple[dict, int]:
    out: Dict[str, Any] = {}
    while i < len(lines) and lines[i][0] == indent and not _is_seq_item(lines[i][1]):
        key, sep, val = _yaml_split_key(lines[i][1])
        if not sep:
            raise ValueError(
                f"line {lines[i][2]}: expected 'key: value' or 'key:', got "
                f"{lines[i][1]!r}"
            )
        key = key.strip()
        valtext = val.strip()
        i += 1
        if valtext:
            out[key] = _yaml_value(valtext)
        else:
            nested, i = _yaml_parse_block(lines, i, indent + 1)
            out[key] = nested
    return out, i


def parse_assertions_yaml(text: str) -> Any:
    """Parse an ``assertions.yaml`` document.

    A document that is already valid JSON (starts with ``{`` or ``[`` and
    parses as such) is accepted directly. Otherwise it is parsed with a
    small, dependency-free subset of YAML: block mappings/sequences, flow
    ``[...]``/``{...}`` collections, quoted or bare scalars, ``#`` comments.
    Hotato's core carries no third-party runtime dependency (PyYAML
    included), so this stays zero-install regardless of what happens to be
    importable. Raises ``ValueError`` on anything outside the subset or a
    malformed line -- this is the caller's usage-error / exit-2 path."""
    stripped = text.strip()
    if stripped[:1] in ("{", "["):
        try:
            return json.loads(stripped)
        except json.JSONDecodeError:
            pass  # fall through to the YAML-subset parser below

    lines = _yaml_lines(text)
    if not lines:
        raise ValueError("assertions file is empty")
    doc, i = _yaml_parse_block(lines, 0, 0)
    if i != len(lines):
        raise ValueError(f"line {lines[i][2]}: unexpected indentation")
    return doc


# =========================================================================
# Assertion-document validation (malformed input -> ValueError, exit 2)
# =========================================================================

_PHRASE_POSITIONS = ("first", "last", "any")
DETECTOR_NAMES = ("ssn", "card_luhn", "email", "phone")
_OUTCOME_PRED_KEYS = ("tool_called", "phrase", "field_present")


def _validate_outcome_predicate(aid: str, p: Any) -> None:
    if not isinstance(p, dict):
        raise ValueError(
            f"assertion {aid!r} (outcome): each predicate must be a mapping"
        )
    present = [k for k in _OUTCOME_PRED_KEYS if k in p]
    if len(present) != 1:
        raise ValueError(
            f"assertion {aid!r} (outcome): each predicate must have exactly "
            f"one of {_OUTCOME_PRED_KEYS}, got {sorted(p.keys())}"
        )
    field = present[0]
    value = p[field]
    if not isinstance(value, str) or not value:
        raise ValueError(
            f"assertion {aid!r} (outcome): {field!r} must be a non-empty string"
        )
    if "role" in p and not isinstance(p["role"], str):
        raise ValueError(
            f"assertion {aid!r} (outcome): predicate 'role' must be a string"
        )
    if field == "phrase":
        try:
            re.compile(value)
        except re.error as exc:
            raise ValueError(
                f"assertion {aid!r} (outcome): invalid phrase regex {value!r}: {exc}"
            ) from exc


def _validate_kind_fields(aid: str, kind: str, item: Dict[str, Any]) -> None:
    if kind == "phrase":
        regex = item.get("regex")
        if not isinstance(regex, str) or not regex:
            raise ValueError(
                f"assertion {aid!r} (phrase): 'regex' is required and must "
                "be a non-empty string"
            )
        try:
            re.compile(regex)
        except re.error as exc:
            raise ValueError(
                f"assertion {aid!r} (phrase): invalid regex {regex!r}: {exc}"
            ) from exc
        position = item.get("position", "any")
        if position not in _PHRASE_POSITIONS:
            raise ValueError(
                f"assertion {aid!r} (phrase): 'position' must be one of "
                f"{_PHRASE_POSITIONS}, got {position!r}"
            )
        role = item.get("role")
        if role is not None and not isinstance(role, str):
            raise ValueError(f"assertion {aid!r} (phrase): 'role' must be a string")
        for flag in ("absent", "case_sensitive"):
            if flag in item and not isinstance(item[flag], bool):
                raise ValueError(
                    f"assertion {aid!r} (phrase): {flag!r} must be a boolean"
                )

    elif kind == "pii":
        detectors = item.get("detectors")
        if not isinstance(detectors, list) or not detectors:
            raise ValueError(
                f"assertion {aid!r} (pii): 'detectors' must be a non-empty list"
            )
        for d in detectors:
            if d not in DETECTOR_NAMES:
                raise ValueError(
                    f"assertion {aid!r} (pii): unknown detector {d!r}; "
                    f"supported: {DETECTOR_NAMES}"
                )
        if item.get("mode") != "must_not_leak":
            raise ValueError(
                f"assertion {aid!r} (pii): 'mode' must be 'must_not_leak' "
                f"(got {item.get('mode')!r})"
            )

    elif kind == "policy":
        pack = item.get("pack")
        pack_path = item.get("pack_path")
        if pack is not None and pack_path is not None:
            raise ValueError(
                f"assertion {aid!r} (policy): pass either 'pack' or "
                "'pack_path', not both"
            )
        if pack is not None and not isinstance(pack, str):
            raise ValueError(f"assertion {aid!r} (policy): 'pack' must be a string")
        if pack_path is not None and not isinstance(pack_path, str):
            raise ValueError(
                f"assertion {aid!r} (policy): 'pack_path' must be a string"
            )
        rule_ids = item.get("rule_ids")
        if rule_ids is not None and (
            not isinstance(rule_ids, list) or not all(isinstance(r, str) for r in rule_ids)
        ):
            raise ValueError(
                f"assertion {aid!r} (policy): 'rule_ids' must be a list of strings"
            )

    elif kind == "tool_call":
        if not any(k in item for k in ("name", "require_order", "never_before")):
            raise ValueError(
                f"assertion {aid!r} (tool_call): at least one of 'name', "
                "'require_order', 'never_before' is required"
            )
        if "name" in item and not isinstance(item["name"], str):
            raise ValueError(f"assertion {aid!r} (tool_call): 'name' must be a string")
        if "args_subset" in item and not isinstance(item["args_subset"], dict):
            raise ValueError(
                f"assertion {aid!r} (tool_call): 'args_subset' must be a mapping"
            )
        if "require_order" in item:
            ro = item["require_order"]
            if not isinstance(ro, list) or not ro or not all(
                isinstance(x, str) for x in ro
            ):
                raise ValueError(
                    f"assertion {aid!r} (tool_call): 'require_order' must be "
                    "a non-empty list of tool-name strings"
                )
        if "never_before" in item:
            nb = item["never_before"]
            if not isinstance(nb, dict) or "tool" not in nb or "until" not in nb:
                raise ValueError(
                    f"assertion {aid!r} (tool_call): 'never_before' must be "
                    "a mapping with 'tool' and 'until'"
                )
        if "count" in item:
            c = item["count"]
            if isinstance(c, bool) or (
                not isinstance(c, int)
                and not (isinstance(c, dict) and any(k in c for k in ("min", "max")))
            ):
                raise ValueError(
                    f"assertion {aid!r} (tool_call): 'count' must be an "
                    "integer or a {min, max} mapping"
                )

    elif kind == "outcome":
        all_of = item.get("all_of")
        any_of = item.get("any_of")
        if all_of is None and any_of is None:
            raise ValueError(
                f"assertion {aid!r} (outcome): one of 'all_of'/'any_of' is required"
            )
        for field_name, preds in (("all_of", all_of), ("any_of", any_of)):
            if preds is None:
                continue
            if not isinstance(preds, list) or not preds:
                raise ValueError(
                    f"assertion {aid!r} (outcome): {field_name!r} must be a "
                    "non-empty list"
                )
            for p in preds:
                _validate_outcome_predicate(aid, p)

    else:
        # The Phase-1 expanded kinds (and the quarantined rubric kinds). Kept in
        # a separate function so the original five branches above stay exactly
        # as they were.
        _validate_expanded_kind_fields(aid, kind, item)


def _is_number(x: Any) -> bool:
    return isinstance(x, (int, float)) and not isinstance(x, bool)


def _validate_count_spec(aid: str, kind: str, c: Any) -> None:
    if isinstance(c, bool) or (
        not isinstance(c, int)
        and not (isinstance(c, dict) and any(k in c for k in ("min", "max")))
    ):
        raise ValueError(
            f"assertion {aid!r} ({kind}): 'count' must be an integer or a "
            "{min, max} mapping"
        )


def _validate_expanded_kind_fields(aid: str, kind: str, item: Dict[str, Any]) -> None:
    """Up-front field validation for the Phase-1 expanded deterministic kinds
    (and a no-op accept for the quarantined ``human_rubric``/``judge_rubric``
    kinds, whose shape is validated when the rubric engine lands in Phase 3).
    A malformed field is the caller's usage error (``ValueError`` -> exit 2),
    raised before any assertion is evaluated -- exactly like the original five
    kinds."""
    if kind in ("tool_result", "tool_error"):
        if not isinstance(item.get("name"), str) or not item["name"]:
            raise ValueError(
                f"assertion {aid!r} ({kind}): 'name' is required and must be a "
                "non-empty tool-name string"
            )
        if kind == "tool_result" and "result_subset" in item and not isinstance(
            item["result_subset"], dict
        ):
            raise ValueError(
                f"assertion {aid!r} (tool_result): 'result_subset' must be a mapping"
            )
        if kind == "tool_error" and "error_matches" in item:
            em = item["error_matches"]
            if not isinstance(em, str) or not em:
                raise ValueError(
                    f"assertion {aid!r} (tool_error): 'error_matches' must be a "
                    "non-empty regex string"
                )
            try:
                re.compile(em)
            except re.error as exc:
                raise ValueError(
                    f"assertion {aid!r} (tool_error): invalid regex {em!r}: {exc}"
                ) from exc
        if "absent" in item and not isinstance(item["absent"], bool):
            raise ValueError(f"assertion {aid!r} ({kind}): 'absent' must be a boolean")

    elif kind == "state":
        if not isinstance(item.get("resource"), str) or not item["resource"]:
            raise ValueError(
                f"assertion {aid!r} (state): 'resource' is required and must be "
                "a non-empty string"
            )
        if not isinstance(item.get("expect"), dict) or not item["expect"]:
            raise ValueError(
                f"assertion {aid!r} (state): 'expect' is required and must be a "
                "non-empty mapping of expected post-call fields"
            )
        if "filters" in item and not isinstance(item["filters"], dict):
            raise ValueError(f"assertion {aid!r} (state): 'filters' must be a mapping")

    elif kind == "state_change":
        if not isinstance(item.get("resource"), str) or not item["resource"]:
            raise ValueError(
                f"assertion {aid!r} (state_change): 'resource' is required and "
                "must be a non-empty string"
            )
        if not isinstance(item.get("field"), str) or not item["field"]:
            raise ValueError(
                f"assertion {aid!r} (state_change): 'field' is required and must "
                "be a non-empty string"
            )
        if not any(k in item for k in ("from", "to", "changed")):
            raise ValueError(
                f"assertion {aid!r} (state_change): at least one of 'from', 'to', "
                "'changed' is required (assert something about the delta)"
            )
        if "changed" in item and not isinstance(item["changed"], bool):
            raise ValueError(
                f"assertion {aid!r} (state_change): 'changed' must be a boolean"
            )
        if "filters" in item and not isinstance(item["filters"], dict):
            raise ValueError(
                f"assertion {aid!r} (state_change): 'filters' must be a mapping"
            )

    elif kind == "handoff":
        if "to" in item and not isinstance(item["to"], str):
            raise ValueError(f"assertion {aid!r} (handoff): 'to' must be a string")
        if "absent" in item and not isinstance(item["absent"], bool):
            raise ValueError(f"assertion {aid!r} (handoff): 'absent' must be a boolean")

    elif kind == "dtmf":
        d = item.get("digits")
        if not isinstance(d, str) or not d:
            raise ValueError(
                f"assertion {aid!r} (dtmf): 'digits' is required and must be a "
                "non-empty string of expected DTMF digits"
            )
        if "absent" in item and not isinstance(item["absent"], bool):
            raise ValueError(f"assertion {aid!r} (dtmf): 'absent' must be a boolean")

    elif kind == "termination":
        for f in ("reason", "by"):
            if f in item and not isinstance(item[f], str):
                raise ValueError(
                    f"assertion {aid!r} (termination): {f!r} must be a string"
                )
        if "absent" in item and not isinstance(item["absent"], bool):
            raise ValueError(
                f"assertion {aid!r} (termination): 'absent' must be a boolean"
            )

    elif kind == "latency":
        sources = [k for k in ("tool", "span_type", "field") if k in item]
        if len(sources) != 1:
            raise ValueError(
                f"assertion {aid!r} (latency): exactly one of 'tool', 'span_type', "
                f"'field' is required, got {sources}"
            )
        if "field" in item:
            if not isinstance(item["field"], str) or not item["field"]:
                raise ValueError(
                    f"assertion {aid!r} (latency): 'field' must be a non-empty "
                    "dotted-path string into the timing context"
                )
            if not _is_number(item.get("max")):
                raise ValueError(
                    f"assertion {aid!r} (latency): 'max' (a numeric threshold in "
                    "the field's own unit) is required with 'field'"
                )
        else:
            key = "tool" if "tool" in item else "span_type"
            if not isinstance(item[key], str) or not item[key]:
                raise ValueError(
                    f"assertion {aid!r} (latency): {key!r} must be a non-empty string"
                )
            if not _is_number(item.get("max_ms")):
                raise ValueError(
                    f"assertion {aid!r} (latency): 'max_ms' (a numeric millisecond "
                    f"threshold) is required with {key!r}"
                )

    elif kind == "timing_contract":
        if not isinstance(item.get("bundle"), str) or not item["bundle"]:
            raise ValueError(
                f"assertion {aid!r} (timing_contract): 'bundle' (a path to a "
                ".hotato bundle to re-verify) is required and must be a string"
            )

    elif kind == "entity_accuracy":
        ref = item.get("reference")
        if not isinstance(ref, dict) or not ref:
            raise ValueError(
                f"assertion {aid!r} (entity_accuracy): 'reference' is required and "
                "must be a non-empty mapping of entity name -> expected value"
            )
        if "require" in item and item["require"] not in ("all", "any"):
            raise ValueError(
                f"assertion {aid!r} (entity_accuracy): 'require' must be 'all' or "
                f"'any', got {item['require']!r}"
            )
        if "case_sensitive" in item and not isinstance(item["case_sensitive"], bool):
            raise ValueError(
                f"assertion {aid!r} (entity_accuracy): 'case_sensitive' must be a "
                "boolean"
            )

    elif kind == "sequence":
        steps = item.get("steps")
        if not isinstance(steps, list) or not steps:
            raise ValueError(
                f"assertion {aid!r} (sequence): 'steps' is required and must be a "
                "non-empty ordered list"
            )
        for i, st in enumerate(steps):
            if not isinstance(st, dict) or len(
                [k for k in ("span_type", "tool") if k in st]
            ) != 1:
                raise ValueError(
                    f"assertion {aid!r} (sequence): steps[{i}] must be a mapping "
                    "with exactly one of 'span_type' or 'tool'"
                )

    elif kind == "count":
        matchers = [k for k in ("span_type", "tool", "phrase") if k in item]
        if len(matchers) != 1:
            raise ValueError(
                f"assertion {aid!r} (count): exactly one of 'span_type', 'tool', "
                f"'phrase' is required, got {matchers}"
            )
        if "phrase" in item:
            phrase = item["phrase"]
            if not isinstance(phrase, str) or not phrase:
                raise ValueError(
                    f"assertion {aid!r} (count): 'phrase' must be a non-empty regex "
                    "string"
                )
            try:
                re.compile(phrase)
            except re.error as exc:
                raise ValueError(
                    f"assertion {aid!r} (count): invalid regex {phrase!r}: {exc}"
                ) from exc
        _validate_count_spec(aid, "count", item.get("count"))

    elif kind in RUBRIC_KINDS:
        # The rubric-object shape is validated by the model-judge engine
        # (hotato.rubric.validate_rubric_object) in the SEPARATE rubric lane.
        # Accepted here so a conversation-test / assert document may reference
        # one; inside the deterministic assert.v1 lane its evaluator routes it
        # out as a deterministic INCONCLUSIVE pointing to the rubric lane, never
        # a guess and never a model call.
        return


def validate_assertions_doc(doc: Any) -> Tuple[int, List[Dict[str, Any]]]:
    """Validate a parsed assertions document (as returned by
    :func:`parse_assertions_yaml`). Returns ``(version, assertions)``.
    Raises ``ValueError`` on anything malformed: not a mapping, missing
    ``version``/``assertions``, an unsupported version, a duplicate or
    missing ``id``, an unknown ``kind``, or a kind missing its required
    fields (including an invalid regex). Nothing here evaluates an
    assertion; this is pure structural validation, run before any context
    is touched."""
    if not isinstance(doc, dict):
        raise ValueError(
            "assertions document must be a mapping with 'version' and "
            "'assertions' at the top level"
        )
    if "version" not in doc:
        raise ValueError("assertions document is missing required 'version'")
    if "assertions" not in doc:
        raise ValueError("assertions document is missing required 'assertions'")

    version = doc["version"]
    if not isinstance(version, int) or isinstance(version, bool):
        raise ValueError(f"'version' must be an integer, got {version!r}")
    if version != SUPPORTED_DOC_VERSION:
        raise ValueError(
            f"unsupported assertions schema version {version!r}; hotato "
            f"assert supports version {SUPPORTED_DOC_VERSION}"
        )

    # Optional top-level gating policy. Validated HERE, before any assertion
    # is evaluated, so a bad value is the same up-front usage error (exit 2) a
    # bad kind/regex is -- never a partial result set. Absent = the default
    # "report" policy is applied by run_assertions.
    if "inconclusive_policy" in doc:
        _validate_inconclusive_policy(
            doc["inconclusive_policy"], "assertions document"
        )

    items = doc["assertions"]
    if not isinstance(items, list) or not items:
        raise ValueError("'assertions' must be a non-empty list")

    seen_ids: set = set()
    out: List[Dict[str, Any]] = []
    for idx, item in enumerate(items):
        if not isinstance(item, dict):
            raise ValueError(f"assertions[{idx}] must be a mapping")
        aid = item.get("id")
        if not aid or not isinstance(aid, str):
            raise ValueError(f"assertions[{idx}] is missing a string 'id'")
        if aid in seen_ids:
            raise ValueError(f"duplicate assertion id {aid!r}")
        seen_ids.add(aid)
        kind = item.get("kind")
        if kind not in ALL_KINDS:
            raise ValueError(
                f"assertion {aid!r}: 'kind' must be one of {ALL_KINDS}, got {kind!r}"
            )
        _validate_kind_fields(aid, kind, item)
        # An OPTIONAL report-dimension TAG, validated up front so it can never
        # propagate an out-of-vocabulary value onto a result (which the
        # assert.v1 schema constrains to the same enum). Absent = untagged,
        # exactly as before this existed.
        dim = item.get("dimension")
        if dim is not None and dim not in RESULT_DIMENSIONS:
            raise ValueError(
                f"assertion {aid!r}: 'dimension' (optional) must be one of "
                f"{RESULT_DIMENSIONS}, got {dim!r}"
            )
        out.append(item)

    return version, out


# =========================================================================
# PII detectors: regex + checksum, deterministic, zero variance
# =========================================================================

_SSN_RE = re.compile(r"\b(?!000|666|9\d\d)\d{3}[-\s]?(?!00)\d{2}[-\s]?(?!0000)\d{4}\b")
_EMAIL_RE = re.compile(r"\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}\b")
_PHONE_RE = re.compile(
    r"(?<!\d)(?:\+?1[-.\s]?)?\(?\d{3}\)?[-.\s]\d{3}[-.\s]\d{4}(?!\d)"
)
_CARD_CANDIDATE_RE = re.compile(r"(?<!\d)(?:\d[ -]?){13,19}(?!\d)")


def _luhn_valid(digits: str) -> bool:
    total = 0
    parity = len(digits) % 2
    for i, ch in enumerate(digits):
        d = int(ch)
        if i % 2 == parity:
            d *= 2
            if d > 9:
                d -= 9
        total += d
    return total % 10 == 0


def _detect_ssn(text: str) -> List[Tuple[int, int]]:
    return [(m.start(), m.end()) for m in _SSN_RE.finditer(text)]


def _detect_email(text: str) -> List[Tuple[int, int]]:
    return [(m.start(), m.end()) for m in _EMAIL_RE.finditer(text)]


def _detect_phone(text: str) -> List[Tuple[int, int]]:
    return [(m.start(), m.end()) for m in _PHONE_RE.finditer(text)]


def _detect_card_luhn(text: str) -> List[Tuple[int, int]]:
    hits = []
    for m in _CARD_CANDIDATE_RE.finditer(text):
        raw = m.group(0)
        digits = re.sub(r"[ -]", "", raw)
        if 13 <= len(digits) <= 19 and _luhn_valid(digits):
            hits.append((m.start(), m.end()))
    return hits


DETECTORS = {
    "ssn": _detect_ssn,
    "card_luhn": _detect_card_luhn,
    "email": _detect_email,
    "phone": _detect_phone,
}


def _redact_spans_in_text(text: str, spans: List[Tuple[int, int]]) -> str:
    if not spans:
        return text
    merged: List[Tuple[int, int]] = []
    for s, e in sorted(spans):
        if merged and s <= merged[-1][1]:
            merged[-1] = (merged[-1][0], max(merged[-1][1], e))
        else:
            merged.append((s, e))
    out = []
    prev = 0
    for s, e in merged:
        out.append(text[prev:s])
        out.append("[REDACTED]")
        prev = e
    out.append(text[prev:])
    return "".join(out)


# =========================================================================
# Policy rule pack: a named, versioned, offline banned/required-disclosure
# regex pack. Byte-replayable: same pack + same transcript -> same result.
# =========================================================================

DEFAULT_POLICY_PACK: Dict[str, Any] = {
    "name": "default",
    "version": 1,
    "rules": [
        {
            "id": "no-profanity",
            "type": "banned",
            "regex": r"\b(damn|hell|shit|fuck)\b",
        },
        {
            "id": "no-guarantee-language",
            "type": "banned",
            "regex": r"\b(guarantee(d)?|i promise|100% certain)\b",
        },
        {
            "id": "recording-disclosure",
            "type": "required_disclosure",
            "regex": r"recorded for quality",
            "role": "agent",
        },
    ],
}


def _validate_policy_pack(pack: Any, source: str) -> None:
    if not isinstance(pack, dict) or not isinstance(pack.get("rules"), list):
        raise ValueError(
            f"{source!r}: a policy pack must be a mapping with a 'rules' list"
        )
    for i, r in enumerate(pack["rules"]):
        if not isinstance(r, dict) or not all(k in r for k in ("id", "type", "regex")):
            raise ValueError(
                f"{source!r}: rules[{i}] must have 'id', 'type', 'regex'"
            )
        if r["type"] not in ("banned", "required_disclosure"):
            raise ValueError(
                f"{source!r}: rules[{i}]['type'] must be 'banned' or "
                "'required_disclosure'"
            )
        try:
            re.compile(r["regex"])
        except re.error as exc:
            raise ValueError(
                f"{source!r}: rules[{i}]['regex'] is invalid: {exc}"
            ) from exc


def load_policy_pack(
    name: Optional[str] = None, path: Optional[str] = None
) -> Dict[str, Any]:
    """Load a named, versioned, offline policy pack: the bundled
    ``"default"`` pack, or a custom pack from a JSON file at ``path`` (same
    shape as :data:`DEFAULT_POLICY_PACK`). Exactly one of ``name``/``path``
    is meaningful; ``path`` wins if both are given. Raises ``ValueError`` for
    an unknown built-in name or a malformed custom pack file."""
    if path is not None:
        with _open_regular(path, "r", encoding="utf-8") as fh:
            try:
                pack = json.load(fh)
            except json.JSONDecodeError as exc:
                raise ValueError(
                    f"{path!r} is not a valid policy pack JSON: {exc}"
                ) from exc
        _validate_policy_pack(pack, path)
        return pack
    name = name or "default"
    if name == "default":
        return DEFAULT_POLICY_PACK
    raise ValueError(
        f"unknown built-in policy pack {name!r}; pass 'pack_path' for a "
        "custom pack, or use 'default'"
    )


# =========================================================================
# Evaluators: one per kind. Each returns a partial result dict (id/kind
# always set by the caller too, deterministic always True).
# =========================================================================

def _base_result(a: Dict[str, Any]) -> Dict[str, Any]:
    result = {"id": a["id"], "kind": a["kind"], "deterministic": True}
    # Propagate an OPTIONAL ``dimension`` TAG verbatim onto the result, so the
    # report can group results into the per-dimension scorecard without
    # inventing a scorer. Additive: an assertion with no dimension yields a
    # result with no dimension (byte-identical to before this existed), never a
    # fabricated or defaulted one.
    dim = a.get("dimension")
    if dim is not None:
        result["dimension"] = dim
    return result


def _eval_phrase(a: Dict[str, Any], ctx: Context) -> Dict[str, Any]:
    result = _base_result(a)
    if ctx.transcript is None:
        result["status"] = "INCONCLUSIVE"
        result["reason"] = "no transcript was provided in the context"
        return result

    role = a.get("role")
    position = a.get("position", "any")
    absent = bool(a.get("absent", False))
    case_sensitive = bool(a.get("case_sensitive", False))
    flags = 0 if case_sensitive else re.IGNORECASE
    rx = re.compile(a["regex"], flags)
    turns = [t for t in ctx.transcript if role is None or t.get("role") == role]

    if absent:
        hit = next((t for t in turns if rx.search(t.get("text") or "")), None)
        if hit is None:
            result["status"] = "PASS"
        else:
            result["status"] = "FAIL"
            result["reason"] = (
                f"regex {a['regex']!r} matched but must be absent "
                f"(role={role or 'any'})"
            )
        return result

    if not turns:
        result["status"] = "FAIL"
        result["reason"] = f"no turns found for role={role or 'any'}"
        return result

    if position == "any":
        matched = any(rx.search(t.get("text") or "") for t in turns)
    else:
        target = turns[0] if position == "first" else turns[-1]
        matched = bool(rx.search(target.get("text") or ""))
    result["status"] = "PASS" if matched else "FAIL"
    if not matched:
        result["reason"] = (
            f"regex {a['regex']!r} did not match (role={role or 'any'}, "
            f"position={position})"
        )
    return result


def _eval_pii(a: Dict[str, Any], ctx: Context) -> Dict[str, Any]:
    result = _base_result(a)
    if ctx.transcript is None:
        result["status"] = "INCONCLUSIVE"
        result["reason"] = "no transcript was provided in the context"
        return result

    detector_names = a["detectors"]
    hits: List[Dict[str, Any]] = []
    redacted_turns: List[Dict[str, Any]] = []
    for turn_idx, turn in enumerate(ctx.transcript):
        text = turn.get("text") or ""
        turn_spans: List[Tuple[int, int]] = []
        for name in detector_names:
            for s, e in DETECTORS[name](text):
                hits.append({"detector": name, "turn": turn_idx, "role": turn.get("role")})
                turn_spans.append((s, e))
        redacted_turns.append(
            {
                "role": turn.get("role"),
                "text": _redact_spans_in_text(text, turn_spans),
                "start": turn.get("start"),
                "end": turn.get("end"),
            }
        )

    result["redacted_transcript"] = redacted_turns
    if hits:
        result["status"] = "FAIL"
        result["hits"] = hits
        detectors_hit = sorted({h["detector"] for h in hits})
        result["reason"] = (
            f"{len(hits)} PII hit(s) found ({', '.join(detectors_hit)}); see "
            "redacted_transcript"
        )
    else:
        result["status"] = "PASS"
    return result


def _eval_policy(a: Dict[str, Any], ctx: Context) -> Dict[str, Any]:
    result = _base_result(a)
    if ctx.transcript is None:
        result["status"] = "INCONCLUSIVE"
        result["reason"] = "no transcript was provided in the context"
        return result

    pack = load_policy_pack(a.get("pack"), a.get("pack_path"))
    rule_ids = a.get("rule_ids")
    rules = pack["rules"]
    if rule_ids:
        rules = [r for r in rules if r["id"] in rule_ids]

    matched: List[Dict[str, Any]] = []
    for r in rules:
        rx = re.compile(r["regex"], re.IGNORECASE)
        role = r.get("role")
        turns = [t for t in ctx.transcript if role is None or t.get("role") == role]
        hit_turn = next(
            (i for i, t in enumerate(turns) if rx.search(t.get("text") or "")), None
        )
        if r["type"] == "banned" and hit_turn is not None:
            matched.append({"rule": r["id"], "type": "banned", "turn": hit_turn})
        elif r["type"] == "required_disclosure" and hit_turn is None:
            matched.append(
                {"rule": r["id"], "type": "required_disclosure_missing", "turn": None}
            )

    result["pack"] = {"name": pack.get("name"), "version": pack.get("version")}
    if matched:
        result["status"] = "FAIL"
        result["matched_rules"] = matched
        result["reason"] = (
            f"{len(matched)} policy rule(s) violated: "
            f"{[m['rule'] for m in matched]}"
        )
    else:
        result["status"] = "PASS"
    return result


def _synthesize_span_id(idx: int) -> str:
    """A stable synthetic span id: the span's zero-based position in the
    full ``ctx.spans`` list (``hotato.voice_trace.v1`` spans carry no id of
    their own). Stable across every assertion that reads the same trace."""
    return f"s_{idx}"


def _tool_call_entries(ctx: Context) -> List[Dict[str, Any]]:
    entries = []
    for idx, s in enumerate(ctx.spans or []):
        if s.get("type") != "tool_call":
            continue
        attrs = s.get("attributes") or {}
        args = s.get("arguments")
        if args is None:
            args = attrs.get("arguments") or {}
        entries.append(
            {
                "span_id": _synthesize_span_id(idx),
                "name": s.get("name"),
                "arguments": args,
                "order": idx,
            }
        )
    return entries


def _is_subset(small: Dict[str, Any], big: Dict[str, Any]) -> bool:
    return all(k in big and big[k] == v for k, v in small.items())


def _count_in_bounds(n: int, spec: Any) -> bool:
    if isinstance(spec, int):
        return n == spec
    lo = spec.get("min")
    hi = spec.get("max")
    if lo is not None and n < lo:
        return False
    if hi is not None and n > hi:
        return False
    return True


def _sorted_span_ids(span_ids: Sequence[str]) -> List[str]:
    return sorted(set(span_ids), key=lambda sid: int(sid.split("_")[1]))


def _eval_tool_call(a: Dict[str, Any], ctx: Context) -> Dict[str, Any]:
    result = _base_result(a)
    if ctx.spans is None:
        result["status"] = "INCONCLUSIVE"
        result["reason"] = (
            "no trace was provided in the context; tool_call assertions "
            "read voice_trace.v1 spans, never transcript text"
        )
        return result

    entries = _tool_call_entries(ctx)
    failures: List[str] = []
    span_ids: List[str] = []

    name = a.get("name")
    if name is not None:
        by_name = [e for e in entries if e.get("name") == name]
        args_subset = a.get("args_subset")
        qualifying = (
            [e for e in by_name if _is_subset(args_subset, e.get("arguments") or {})]
            if args_subset
            else by_name
        )
        span_ids.extend(e["span_id"] for e in qualifying)

        count_spec = a.get("count")
        if count_spec is None:
            if not qualifying:
                if by_name and args_subset:
                    failures.append(
                        f"tool {name!r} was called but never with arguments "
                        f"matching {args_subset!r}"
                    )
                else:
                    failures.append(f"tool {name!r} was never called")
        else:
            n = len(qualifying)
            if not _count_in_bounds(n, count_spec):
                failures.append(
                    f"tool {name!r} was called {n} time(s); expected {count_spec!r}"
                )

    require_order = a.get("require_order")
    if require_order:
        last_pos = -1
        for tool_name in require_order:
            match = next(
                (e for e in entries if e["name"] == tool_name and e["order"] > last_pos),
                None,
            )
            if match is None:
                failures.append(
                    f"required order {require_order!r} broken: {tool_name!r} "
                    "did not appear (in order) after the previously required tool"
                )
                break
            span_ids.append(match["span_id"])
            last_pos = match["order"]

    never_before = a.get("never_before")
    if never_before:
        tool_x = never_before["tool"]
        tool_y = never_before["until"]
        first_y_order = next(
            (e["order"] for e in entries if e["name"] == tool_y), None
        )
        offenders = [
            e
            for e in entries
            if e["name"] == tool_x and (first_y_order is None or e["order"] < first_y_order)
        ]
        if offenders:
            if first_y_order is not None:
                failures.append(f"{tool_x!r} appeared before {tool_y!r}")
            else:
                failures.append(f"{tool_x!r} appeared but {tool_y!r} never did")
            span_ids.extend(o["span_id"] for o in offenders)

    result["status"] = "FAIL" if failures else "PASS"
    if span_ids:
        result["span_ids"] = _sorted_span_ids(span_ids)
    if failures:
        result["reason"] = "; ".join(failures)
    return result


def _get_path(obj: Any, path: str) -> Tuple[Any, bool]:
    cur = obj
    for part in path.split("."):
        if isinstance(cur, dict) and part in cur:
            cur = cur[part]
        else:
            return None, False
    return cur, True


def _field_present_in_timing(timing: Any, path: str) -> bool:
    if isinstance(timing, list):
        for ev in timing:
            val, found = _get_path(ev, path)
            if found and val is not None:
                return True
        return False
    val, found = _get_path(timing, path)
    return found and val is not None


def _eval_outcome_predicate(p: Dict[str, Any], ctx: Context) -> Optional[bool]:
    """Returns ``True``/``False``, or ``None`` when the context this
    predicate needs was never supplied (propagates to INCONCLUSIVE)."""
    if "tool_called" in p:
        if ctx.spans is None:
            return None
        name = p["tool_called"]
        return any(
            s.get("type") == "tool_call" and s.get("name") == name for s in ctx.spans
        )
    if "phrase" in p:
        if ctx.transcript is None:
            return None
        rx = re.compile(p["phrase"], re.IGNORECASE)
        role = p.get("role")
        for turn in ctx.transcript:
            if role is not None and turn.get("role") != role:
                continue
            if rx.search(turn.get("text") or ""):
                return True
        return False
    if "field_present" in p:
        if ctx.timing is None:
            return None
        return _field_present_in_timing(ctx.timing, p["field_present"])
    return None  # unreachable given prior validation; defensive only


def _eval_outcome(a: Dict[str, Any], ctx: Context) -> Dict[str, Any]:
    result = _base_result(a)
    mode = "all_of" if a.get("all_of") is not None else "any_of"
    preds = a.get(mode)
    values = [_eval_outcome_predicate(p, ctx) for p in preds]

    if any(v is None for v in values):
        idxs = [i for i, v in enumerate(values) if v is None]
        result["status"] = "INCONCLUSIVE"
        result["met"] = sum(1 for v in values if v is True)
        result["of"] = len(values)
        result["reason"] = (
            f"predicate(s) at index {idxs} could not be evaluated: required "
            "context (transcript/spans/timing) was not provided"
        )
        return result

    met = sum(1 for v in values if v)
    of = len(values)
    passed = (met == of) if mode == "all_of" else (met > 0)
    result["status"] = "PASS" if passed else "FAIL"
    result["met"] = met
    result["of"] = of
    if not passed:
        result["reason"] = f"{mode}: {met}/{of} predicate(s) met"
    return result


# =========================================================================
# Phase-1 expanded deterministic kinds. Each reads the AUTHENTICATED trace
# spans (voice_trace.v1), the post-call state adapter, or numeric timing --
# NEVER the agent's spoken transcript claim (for the Authority 1 & 2 kinds
# this is structural: their code below simply has no ``ctx.transcript`` read
# and no model call). Every one returns ``deterministic: true``, and reports
# INCONCLUSIVE -- never a guessed PASS/FAIL -- when its required input is
# absent.
# =========================================================================

def _span_field(s: Dict[str, Any], *keys: str) -> Any:
    """First non-``None`` value among a span's top-level ``keys`` or the same
    keys inside its ``attributes`` dict (voice_trace.v1 spans carry payload at
    either level; an ingested OTel export flattens into ``attributes``)."""
    attrs = s.get("attributes") or {}
    for k in keys:
        if s.get(k) is not None:
            return s[k]
        if attrs.get(k) is not None:
            return attrs[k]
    return None


def _typed_spans(ctx: Context, span_type: str, name: Optional[str] = None):
    """``(index, span)`` for every span of ``span_type`` (optionally also
    matching a ``name``), preserving trace order."""
    out = []
    for idx, s in enumerate(ctx.spans or []):
        if s.get("type") != span_type:
            continue
        if name is not None and s.get("name") != name:
            continue
        out.append((idx, s))
    return out


def _span_errored(s: Dict[str, Any]) -> bool:
    """A tool span 'errored' iff it carries a truthy ``error``, a ``status`` of
    ``"error"``, or an explicit ``ok: false`` (top-level or in attributes)."""
    if _span_field(s, "error") not in (None, "", False):
        return True
    if _span_field(s, "status") == "error":
        return True
    ok = _span_field(s, "ok")
    return ok is False


def _eval_tool_result(a: Dict[str, Any], ctx: Context) -> Dict[str, Any]:
    result = _base_result(a)
    if ctx.spans is None:
        result["status"] = "INCONCLUSIVE"
        result["reason"] = (
            "no trace was provided; tool_result reads voice_trace.v1 tool "
            "spans (Authority 1), never the agent's spoken claim"
        )
        return result
    name = a["name"]
    subset = a.get("result_subset") or {}
    called = False
    matched_ids: List[str] = []
    for idx, s in _typed_spans(ctx, "tool_call", name):
        called = True
        res = _span_field(s, "result")
        if not isinstance(res, dict):
            continue
        if _is_subset(subset, res):
            matched_ids.append(_synthesize_span_id(idx))
    if matched_ids:
        result["status"] = "PASS"
        result["span_ids"] = _sorted_span_ids(matched_ids)
    else:
        result["status"] = "FAIL"
        result["reason"] = (
            f"tool {name!r} produced no result span in the trace"
            if not called
            else f"tool {name!r} was called but no result matched {subset!r}"
        )
    return result


def _eval_tool_error(a: Dict[str, Any], ctx: Context) -> Dict[str, Any]:
    result = _base_result(a)
    if ctx.spans is None:
        result["status"] = "INCONCLUSIVE"
        result["reason"] = (
            "no trace was provided; tool_error reads voice_trace.v1 tool "
            "spans (Authority 1), never the agent's spoken claim"
        )
        return result
    name = a["name"]
    absent = bool(a.get("absent", False))
    matcher = a.get("error_matches")
    rx = re.compile(matcher, re.IGNORECASE) if matcher else None
    errored_ids: List[str] = []
    for idx, s in _typed_spans(ctx, "tool_call", name):
        if not _span_errored(s):
            continue
        if rx is not None:
            msg = _span_field(s, "error", "error_message", "message")
            if not (isinstance(msg, str) and rx.search(msg)):
                continue
        errored_ids.append(_synthesize_span_id(idx))
    hit = bool(errored_ids)
    passed = (not hit) if absent else hit
    result["status"] = "PASS" if passed else "FAIL"
    if errored_ids:
        result["span_ids"] = _sorted_span_ids(errored_ids)
    if not passed:
        result["reason"] = (
            f"tool {name!r} errored but must not have"
            if absent
            else f"tool {name!r} did not error (no matching error span in the trace)"
        )
    return result


def _eval_state(a: Dict[str, Any], ctx: Context) -> Dict[str, Any]:
    result = _base_result(a)
    if ctx.state_adapter is None:
        result["status"] = "INCONCLUSIVE"
        result["reason"] = (
            "no state adapter was provided; state reads a post-call system of "
            "record (Authority 2), never the agent's spoken claim"
        )
        return result
    resource = a["resource"]
    filters = a.get("filters") or {}
    expect = a["expect"]
    try:
        rec = ctx.state_adapter.query(resource, **filters)
    except Exception as exc:  # an adapter failure withholds a verdict, never guesses
        result["status"] = "INCONCLUSIVE"
        result["reason"] = f"state adapter query for {resource!r} failed: {exc}"
        return result
    if rec is None:
        # The adapter IS queryable (input present); the record genuinely does
        # not exist -> a grounded FAIL, not a guess. (Contrast a MISSING
        # adapter above, which is absent input -> INCONCLUSIVE.)
        result["status"] = "FAIL"
        result["reason"] = f"no {resource!r} record matched filters {filters!r}"
        return result
    mismatched = []
    for k in expect:
        val, found = _get_path(rec, k)
        if not found or val != expect[k]:
            mismatched.append(k)
    if not mismatched:
        result["status"] = "PASS"
    else:
        result["status"] = "FAIL"
        result["reason"] = (
            f"{resource!r} record present but field(s) {sorted(mismatched)} did "
            "not match the expected post-call state"
        )
    return result


def _eval_state_change(a: Dict[str, Any], ctx: Context) -> Dict[str, Any]:
    result = _base_result(a)
    if ctx.state_adapter is None:
        result["status"] = "INCONCLUSIVE"
        result["reason"] = (
            "no state adapter was provided; state_change reads before/after "
            "post-call state (Authority 2), never the agent's spoken claim"
        )
        return result
    resource = a["resource"]
    filters = a.get("filters") or {}
    field = a["field"]
    try:
        before = ctx.state_adapter.query(resource, when="before", **filters)
        after = ctx.state_adapter.query(resource, when="after", **filters)
    except Exception as exc:
        result["status"] = "INCONCLUSIVE"
        result["reason"] = f"state adapter query for {resource!r} failed: {exc}"
        return result
    if before is None or after is None:
        # A delta needs both snapshots; a missing one is absent INPUT.
        missing = [w for w, r in (("before", before), ("after", after)) if r is None]
        result["status"] = "INCONCLUSIVE"
        result["reason"] = (
            f"cannot measure a delta on {resource!r}: {missing} snapshot(s) absent"
        )
        return result
    bval, _bf = _get_path(before, field)
    aval, _af = _get_path(after, field)
    failures: List[str] = []
    if "from" in a and bval != a["from"]:
        failures.append(f"before {field!r} was {bval!r}, expected {a['from']!r}")
    if "to" in a and aval != a["to"]:
        failures.append(f"after {field!r} was {aval!r}, expected {a['to']!r}")
    if a.get("changed") and bval == aval:
        failures.append(f"{field!r} did not change (stayed {bval!r})")
    if not failures:
        result["status"] = "PASS"
        result["delta"] = {"field": field, "before": bval, "after": aval}
    else:
        result["status"] = "FAIL"
        result["reason"] = "; ".join(failures)
    return result


def _eval_handoff(a: Dict[str, Any], ctx: Context) -> Dict[str, Any]:
    result = _base_result(a)
    if ctx.spans is None:
        result["status"] = "INCONCLUSIVE"
        result["reason"] = "no trace was provided; handoff reads voice_trace.v1 spans"
        return result
    absent = bool(a.get("absent", False))
    want_to = a.get("to")
    ids: List[str] = []
    for idx, s in _typed_spans(ctx, "handoff"):
        if want_to is not None and _span_field(s, "to", "target", "name") != want_to:
            continue
        ids.append(_synthesize_span_id(idx))
    hit = bool(ids)
    passed = (not hit) if absent else hit
    result["status"] = "PASS" if passed else "FAIL"
    if ids:
        result["span_ids"] = _sorted_span_ids(ids)
    if not passed:
        target = f" to {want_to!r}" if want_to is not None else ""
        result["reason"] = (
            f"a handoff{target} occurred but must not have"
            if absent
            else f"no handoff{target} span occurred in the trace"
        )
    return result


def _eval_dtmf(a: Dict[str, Any], ctx: Context) -> Dict[str, Any]:
    result = _base_result(a)
    if ctx.spans is None:
        result["status"] = "INCONCLUSIVE"
        result["reason"] = "no trace was provided; dtmf reads voice_trace.v1 spans"
        return result
    expected = a["digits"]
    absent = bool(a.get("absent", False))
    seen_parts = []
    ids: List[str] = []
    for idx, s in _typed_spans(ctx, "dtmf"):
        digits = _span_field(s, "digits", "digit")
        if digits is not None:
            seen_parts.append(str(digits))
            ids.append(_synthesize_span_id(idx))
    seen = "".join(seen_parts)
    present = expected in seen
    passed = (not present) if absent else present
    result["status"] = "PASS" if passed else "FAIL"
    if ids:
        result["span_ids"] = _sorted_span_ids(ids)
    if not passed:
        result["reason"] = (
            f"DTMF digits {expected!r} were present but must be absent"
            if absent
            else f"expected DTMF digits {expected!r} not found in the trace "
                 f"(saw {seen!r})"
        )
    return result


_TERMINATION_TYPES = ("termination", "call_ended", "call_terminated", "hangup")


def _eval_termination(a: Dict[str, Any], ctx: Context) -> Dict[str, Any]:
    result = _base_result(a)
    if ctx.spans is None:
        result["status"] = "INCONCLUSIVE"
        result["reason"] = (
            "no trace was provided; termination reads voice_trace.v1 spans"
        )
        return result
    absent = bool(a.get("absent", False))
    want_reason = a.get("reason")
    want_by = a.get("by")
    ids: List[str] = []
    for idx, s in enumerate(ctx.spans or []):
        if s.get("type") not in _TERMINATION_TYPES:
            continue
        if want_reason is not None and _span_field(s, "reason") != want_reason:
            continue
        if want_by is not None and _span_field(s, "by", "terminated_by") != want_by:
            continue
        ids.append(_synthesize_span_id(idx))
    hit = bool(ids)
    passed = (not hit) if absent else hit
    result["status"] = "PASS" if passed else "FAIL"
    if ids:
        result["span_ids"] = _sorted_span_ids(ids)
    if not passed:
        result["reason"] = (
            "the call terminated as described but must not have"
            if absent
            else "no matching termination span occurred in the trace"
        )
    return result


def _first_numeric_from_timing(timing: Any, path: str) -> Optional[float]:
    def _num(v: Any) -> Optional[float]:
        return v if (isinstance(v, (int, float)) and not isinstance(v, bool)) else None

    if isinstance(timing, list):
        for ev in timing:
            val, found = _get_path(ev, path)
            if found and _num(val) is not None:
                return _num(val)
        return None
    val, found = _get_path(timing, path)
    return _num(val) if found else None


def _eval_latency(a: Dict[str, Any], ctx: Context) -> Dict[str, Any]:
    result = _base_result(a)
    if "field" in a:
        # Timing source: a numeric field (in its own unit) <= 'max'.
        if ctx.timing is None:
            result["status"] = "INCONCLUSIVE"
            result["reason"] = "no timing context was provided for a latency field"
            return result
        measured = _first_numeric_from_timing(ctx.timing, a["field"])
        if measured is None:
            result["status"] = "INCONCLUSIVE"
            result["reason"] = f"no numeric {a['field']!r} present in the timing context"
            return result
        threshold = a["max"]
        result["measured"] = measured
        result["status"] = "PASS" if measured <= threshold else "FAIL"
        if measured > threshold:
            result["reason"] = f"{a['field']} {measured} exceeds max {threshold}"
        return result
    # Trace source: the slowest matching span's latency_ms <= 'max_ms'.
    if ctx.spans is None:
        result["status"] = "INCONCLUSIVE"
        result["reason"] = "no trace was provided; latency reads span latency_ms"
        return result
    if "tool" in a:
        spans = _typed_spans(ctx, "tool_call", a["tool"])
        what = f"tool {a['tool']!r}"
    else:
        spans = _typed_spans(ctx, a["span_type"])
        what = f"span_type {a['span_type']!r}"
    measured_pairs = [
        (_synthesize_span_id(idx), _span_field(s, "latency_ms"))
        for idx, s in spans
    ]
    measured_pairs = [
        (sid, v) for sid, v in measured_pairs
        if isinstance(v, (int, float)) and not isinstance(v, bool)
    ]
    if not measured_pairs:
        result["status"] = "INCONCLUSIVE"
        result["reason"] = f"no latency_ms measurement found for {what} in the trace"
        return result
    worst_sid, worst = max(measured_pairs, key=lambda p: p[1])
    threshold = a["max_ms"]
    result["measured_ms"] = worst
    result["span_ids"] = _sorted_span_ids([sid for sid, _ in measured_pairs])
    result["status"] = "PASS" if worst <= threshold else "FAIL"
    if worst > threshold:
        result["reason"] = (
            f"{what} latency {worst}ms exceeds max_ms {threshold} (span {worst_sid})"
        )
    return result


def _eval_timing_contract(a: Dict[str, Any], ctx: Context) -> Dict[str, Any]:
    result = _base_result(a)
    bundle = a["bundle"]
    if not os.path.exists(bundle):
        result["status"] = "INCONCLUSIVE"
        result["reason"] = f"timing-contract bundle {bundle!r} not found"
        return result
    # Deferred import: assert_ <- report <- contract is a real module cycle at
    # import time (see load_spans_file); by the time an assertion runs, every
    # module has finished loading, so importing contract here is safe. This is
    # the design's "REUSE contract verify" -- the exact same re-scoring path.
    from . import contract as _contract
    try:
        verdict = _contract.verify_contracts(bundle)
    except ValueError as exc:
        result["status"] = "INCONCLUSIVE"
        result["reason"] = f"could not verify timing contract {bundle!r}: {exc}"
        return result
    per = verdict.get("results") or []
    if not per:
        result["status"] = "INCONCLUSIVE"
        result["reason"] = f"no contracts found under {bundle!r}"
        return result
    failing = [r.get("id") for r in per if not r.get("passed")]
    result["contracts"] = {"total": len(per), "passed": len(per) - len(failing)}
    if not failing:
        result["status"] = "PASS"
    else:
        result["status"] = "FAIL"
        result["reason"] = f"timing contract(s) did not pass: {failing}"
    return result


def _tool_arg_values(ctx: Context) -> Dict[str, Any]:
    """Every tool_call span's arguments flattened into ``{arg_name: value}`` --
    the AUTHENTICATED entity values the agent actually passed to its tools,
    read from the trace, never from the transcript. Later spans win a name
    clash (the last value the agent committed)."""
    out: Dict[str, Any] = {}
    for _idx, s in _typed_spans(ctx, "tool_call"):
        args = _span_field(s, "arguments")
        if isinstance(args, dict):
            out.update(args)
    return out


def _eval_entity_accuracy(a: Dict[str, Any], ctx: Context) -> Dict[str, Any]:
    result = _base_result(a)
    if ctx.spans is None:
        result["status"] = "INCONCLUSIVE"
        result["reason"] = (
            "no trace was provided; entity_accuracy matches the authenticated "
            "tool arguments against the reference, never the spoken transcript"
        )
        return result
    reference = a["reference"]
    require = a.get("require", "all")
    case_sensitive = bool(a.get("case_sensitive", False))
    observed = _tool_arg_values(ctx)

    def _norm(x: Any) -> str:
        s = str(x)
        return s if case_sensitive else s.lower()

    mismatched: List[str] = []
    for key, expected in reference.items():
        got = observed.get(key)
        if got is None or _norm(got) != _norm(expected):
            mismatched.append(key)
    correct = len(reference) - len(mismatched)
    passed = (not mismatched) if require == "all" else (correct > 0)
    result["met"] = correct
    result["of"] = len(reference)
    result["status"] = "PASS" if passed else "FAIL"
    if not passed:
        # Report which ENTITY keys were wrong/missing, never the raw values.
        result["reason"] = (
            f"{require}: {correct}/{len(reference)} reference entit(y/ies) matched "
            f"the tool arguments; incorrect/absent: {sorted(mismatched)}"
        )
    return result


def _sequence_step_matches(step: Dict[str, Any], s: Dict[str, Any]) -> bool:
    if "tool" in step:
        return s.get("type") == "tool_call" and s.get("name") == step["tool"]
    return s.get("type") == step["span_type"]


def _eval_sequence(a: Dict[str, Any], ctx: Context) -> Dict[str, Any]:
    result = _base_result(a)
    if ctx.spans is None:
        result["status"] = "INCONCLUSIVE"
        result["reason"] = "no trace was provided; sequence reads voice_trace.v1 spans"
        return result
    steps = a["steps"]
    last = -1
    ids: List[str] = []
    for i, step in enumerate(steps):
        match_idx = next(
            (idx for idx, s in enumerate(ctx.spans)
             if idx > last and _sequence_step_matches(step, s)),
            None,
        )
        if match_idx is None:
            result["status"] = "FAIL"
            result["reason"] = (
                f"sequence broke at step {i} ({step}): no matching span occurred "
                "after the previous step"
            )
            if ids:
                result["span_ids"] = _sorted_span_ids(ids)
            return result
        ids.append(_synthesize_span_id(match_idx))
        last = match_idx
    result["status"] = "PASS"
    result["span_ids"] = _sorted_span_ids(ids)
    return result


def _eval_count(a: Dict[str, Any], ctx: Context) -> Dict[str, Any]:
    result = _base_result(a)
    spec = a["count"]
    if "phrase" in a:
        if ctx.transcript is None:
            result["status"] = "INCONCLUSIVE"
            result["reason"] = "no transcript was provided for a phrase count"
            return result
        rx = re.compile(a["phrase"], re.IGNORECASE)
        role = a.get("role")
        n = sum(
            1 for t in ctx.transcript
            if (role is None or t.get("role") == role) and rx.search(t.get("text") or "")
        )
    else:
        if ctx.spans is None:
            result["status"] = "INCONCLUSIVE"
            result["reason"] = "no trace was provided; count reads voice_trace.v1 spans"
            return result
        if "tool" in a:
            n = len(_typed_spans(ctx, "tool_call", a["tool"]))
        else:
            n = len(_typed_spans(ctx, a["span_type"]))
    result["observed"] = n
    result["status"] = "PASS" if _count_in_bounds(n, spec) else "FAIL"
    if result["status"] == "FAIL":
        result["reason"] = f"observed {n} occurrence(s); expected {spec!r}"
    return result


def _eval_rubric_stub(a: Dict[str, Any], ctx: Context) -> Dict[str, Any]:
    """The model-judged ``human_rubric``/``judge_rubric`` kinds do NOT run in the
    deterministic ``assert.v1`` lane -- they run in the SEPARATE rubric lane
    (``hotato.rubric`` / ``hotato rubric run`` / a conversation-test's
    ``assertions.rubric``), which is where the real local-model judge lives and
    emits ``deterministic:false`` ``rubric.v1`` results with full provenance.

    ``assert.v1`` is the deterministic wall: its results are structurally
    ``deterministic:true`` (schema ``const``), so a model-backed verdict
    physically cannot be an ``assert.v1`` result. When a rubric kind is
    referenced inside a raw ``assert.v1`` document this evaluator therefore
    routes it out honestly -- a deterministic INCONCLUSIVE that points to the
    rubric lane, WITHOUT ever calling a model (so ``summary.judge`` stays the
    ``{pass:0, fail:0}`` quarantine and the deterministic guarantee is
    untouched). The rubric ENGINE is built; it just does not run here."""
    result = _base_result(a)
    result["status"] = "INCONCLUSIVE"
    result["reason"] = (
        "rubric kinds run in the model-judged rubric lane (hotato rubric run / "
        "a conversation-test's assertions.rubric), not the deterministic "
        "assert.v1 lane"
    )
    return result


_EVALUATORS = {
    "phrase": _eval_phrase,
    "pii": _eval_pii,
    "policy": _eval_policy,
    "tool_call": _eval_tool_call,
    "outcome": _eval_outcome,
    "tool_result": _eval_tool_result,
    "tool_error": _eval_tool_error,
    "state": _eval_state,
    "state_change": _eval_state_change,
    "handoff": _eval_handoff,
    "dtmf": _eval_dtmf,
    "termination": _eval_termination,
    "latency": _eval_latency,
    "timing_contract": _eval_timing_contract,
    "entity_accuracy": _eval_entity_accuracy,
    "sequence": _eval_sequence,
    "count": _eval_count,
    "human_rubric": _eval_rubric_stub,
    "judge_rubric": _eval_rubric_stub,
}


def evaluate_assertion(a: Dict[str, Any], ctx: Context) -> Dict[str, Any]:
    """Evaluate one already-validated assertion dict against ``ctx``.
    Returns one ``results[]`` entry of the ``assert.v1`` envelope:
    ``{id, kind, status, deterministic: true, ...kind-specific fields}``.
    Every kind here is deterministic by construction (no model call), so
    ``deterministic`` is always ``true`` regardless of ``status`` --
    including on an ``INCONCLUSIVE`` result, which reflects absent INPUT,
    never a non-deterministic judgment."""
    return _EVALUATORS[a["kind"]](a, ctx)


def envelope_from_results(
    results: List[Dict[str, Any]],
    inconclusive_policy: str = DEFAULT_INCONCLUSIVE_POLICY,
) -> Dict[str, Any]:
    """Build the ``assert.v1`` envelope from a list of already-evaluated
    results. ``exit_code`` depends on ``inconclusive_policy`` -- how an
    ``INCONCLUSIVE`` result (absent required INPUT, never a failure of
    judgment) gates the run:

    * ``"report"`` (default): ``exit_code`` is 1 if any result is ``FAIL``,
      else 0 -- an ``INCONCLUSIVE`` result never forces a non-zero exit. This
      is the historical behavior, unchanged, so a suite that never sets a
      policy gates exactly as it did before this field existed.
    * ``"fail"``: an ``INCONCLUSIVE`` result gates like a ``FAIL`` --
      ``exit_code`` is 1 if any result is ``FAIL`` OR ``INCONCLUSIVE``, else 0.
    * ``"refuse"``: an ``INCONCLUSIVE`` result refuses a verdict outright --
      ``exit_code`` is 2 if any result is ``INCONCLUSIVE`` (this exit-2
      refusal takes PRECEDENCE over a ``FAIL``: "I will not return a verdict
      when required input is missing"), else 1 if any result is ``FAIL``,
      else 0.

    The envelope always carries the ``inconclusive_policy`` actually applied,
    and ``summary.note`` states that policy plus the counts. ``summary``
    always splits deterministic from judge counts and never emits a merged
    score (``summary.judge`` stays the ``{"pass": 0, "fail": 0}`` quarantine
    -- a judge kind is a separate capability, not built here)."""
    _validate_inconclusive_policy(inconclusive_policy, "envelope_from_results")
    counts = {"pass": 0, "fail": 0, "inconclusive": 0}
    for r in results:
        key = r["status"].lower()
        counts[key] = counts.get(key, 0) + 1
    fail = counts["fail"]
    inconclusive = counts["inconclusive"]
    if inconclusive_policy == "refuse":
        # exit-2 refusal takes precedence over a FAIL: a run missing required
        # input withholds its verdict rather than reporting a partial one.
        exit_code = 2 if inconclusive > 0 else (1 if fail > 0 else 0)
    elif inconclusive_policy == "fail":
        exit_code = 1 if (fail > 0 or inconclusive > 0) else 0
    else:  # "report": historical behavior, INCONCLUSIVE never gates
        exit_code = 1 if fail > 0 else 0
    return {
        "schema": SCHEMA,
        "exit_code": exit_code,
        "inconclusive_policy": inconclusive_policy,
        "results": results,
        "summary": {
            "deterministic": {
                "pass": counts["pass"],
                "fail": fail,
                "inconclusive": inconclusive,
            },
            "judge": {"pass": 0, "fail": 0},
            "note": (
                f"inconclusive_policy={inconclusive_policy}: "
                f"{counts['pass']} pass, {fail} fail, {inconclusive} "
                f"inconclusive across {len(results)} deterministic "
                "assertion(s); 0 judge-scored assertions (a judge kind is a "
                "separate, quarantined capability, not built here)"
            ),
        },
    }


def _resolve_inconclusive_policy(
    doc: Any, override: Optional[str]
) -> str:
    """Resolve the gating policy for a run: an explicit caller/CLI
    ``override`` wins; else the document's own optional top-level
    ``inconclusive_policy`` key (already value-validated by
    :func:`validate_assertions_doc`); else the default ``"report"``. An
    explicit override is validated here so a bad caller argument is the same
    ``ValueError`` a bad document key is."""
    if override is not None:
        return _validate_inconclusive_policy(override, "inconclusive_policy argument")
    if isinstance(doc, dict) and "inconclusive_policy" in doc:
        return _validate_inconclusive_policy(
            doc["inconclusive_policy"], "assertions document"
        )
    return DEFAULT_INCONCLUSIVE_POLICY


def run_assertions(
    doc: Any, ctx: Context, inconclusive_policy: Optional[str] = None
) -> Dict[str, Any]:
    """Validate a parsed assertions document and evaluate every assertion in
    it against ``ctx``, returning the ``assert.v1`` envelope. Raises
    ``ValueError`` for a malformed document -- validation runs before any
    assertion is evaluated, so a bad file never produces a partial result
    set (the caller's exit-2 usage-error path, see :mod:`hotato.errors`).

    ``inconclusive_policy`` (how an ``INCONCLUSIVE`` result gates the exit
    code, see :func:`envelope_from_results`) resolves as: an explicit
    caller/CLI argument overrides the document's own optional top-level
    ``inconclusive_policy`` key; absent both, the default ``"report"`` (the
    historical, backward-compatible behavior)."""
    _version, assertions = validate_assertions_doc(doc)
    policy = _resolve_inconclusive_policy(doc, inconclusive_policy)
    results = [evaluate_assertion(a, ctx) for a in assertions]
    return envelope_from_results(results, inconclusive_policy=policy)


def run_assertions_from_yaml(
    text: str, ctx: Context, inconclusive_policy: Optional[str] = None
) -> Dict[str, Any]:
    """Convenience: parse an ``assertions.yaml`` TEXT and evaluate it in one
    call. See :func:`parse_assertions_yaml` and :func:`run_assertions`
    (including how ``inconclusive_policy`` is resolved)."""
    return run_assertions(parse_assertions_yaml(text), ctx, inconclusive_policy)


def run_assertions_from_file(
    path: str, ctx: Context, inconclusive_policy: Optional[str] = None
) -> Dict[str, Any]:
    """Convenience: read ``path`` (an ``assertions.yaml`` file, guarded by
    :func:`hotato.errors.open_regular`) and evaluate it in one call."""
    with _open_regular(path, "r", encoding="utf-8") as fh:
        text = fh.read()
    return run_assertions_from_yaml(text, ctx, inconclusive_policy)


# =========================================================================
# ``assert run --format text``: a per-kind grouped report. Deterministic
# pass/fail/inconclusive counts and the (always-zero, in this build) judge
# count are printed as two SEPARATE lines -- never combined into one number,
# the same structural honesty invariant ``envelope_from_results`` enforces
# on the JSON envelope.
# =========================================================================

def render_run_text(env: Dict[str, Any]) -> str:
    """A human-readable ``hotato assert run`` report: results grouped by
    ``kind`` (in :data:`KINDS` order), each with its own PASS/FAIL/
    INCONCLUSIVE tally and one line per result; then the envelope's overall
    deterministic tally and the judge tally, printed SEPARATELY -- by
    construction, never a merged score (see the module docstring)."""
    by_kind: Dict[str, List[Dict[str, Any]]] = {}
    for r in env["results"]:
        by_kind.setdefault(r["kind"], []).append(r)

    lines = [f"hotato assert ({env['schema']}) -- exit_code={env['exit_code']}"]
    # Surface a non-default gating policy so a reader can see WHY an
    # inconclusive-only run exited non-zero; the default "report" run's text
    # output stays byte-identical to before this field existed.
    policy = env.get("inconclusive_policy", DEFAULT_INCONCLUSIVE_POLICY)
    if policy != DEFAULT_INCONCLUSIVE_POLICY:
        lines.append(f"inconclusive_policy: {policy}")
    for kind in ALL_KINDS:
        results = by_kind.get(kind)
        if not results:
            continue
        tally = {"PASS": 0, "FAIL": 0, "INCONCLUSIVE": 0}
        for r in results:
            tally[r["status"]] = tally.get(r["status"], 0) + 1
        lines.append(
            f"[{kind}] {tally['PASS']} pass, {tally['FAIL']} fail, "
            f"{tally['INCONCLUSIVE']} inconclusive"
        )
        for r in results:
            reason = f" -- {r['reason']}" if r.get("reason") else ""
            lines.append(f"  {r['status']:<12} {r['id']}{reason}")

    det = env["summary"]["deterministic"]
    judge = env["summary"]["judge"]
    lines.append(
        f"deterministic: {det['pass']} pass, {det['fail']} fail, "
        f"{det['inconclusive']} inconclusive"
    )
    lines.append(
        f"judge: {judge['pass']} pass, {judge['fail']} fail (no judge kind "
        "is built in this release -- see the module docstring)"
    )
    return "\n".join(lines) + "\n"


# =========================================================================
# ``assert init --from-trace``: infer a starter assertions.yaml from a
# hotato.voice_trace.v1 trace's tool_call spans (+ optional timing from a
# freshly scored recording). A STARTER the user edits -- every inferred
# assertion is grounded in something directly observable here (a tool name
# actually seen in the trace, an order actually observed, a verdict field
# actually produced); this never claims to have inferred the RIGHT
# assertions for the call, and never writes an empty/fabricated stub.
# =========================================================================

# Tool names come from third-party trace input. Only a name shaped like a
# normal identifier (what every real tool/function name in the wild looks
# like: snake_case, dotted, hyphenated) is rendered as a bare YAML/flow
# token; render_assertions_yaml is a small, purpose-built emitter with NO
# escaping support (see its docstring), so a name outside this pattern is
# reported back to the caller as skipped rather than risking a corrupt
# (or, worse, silently-misparsed) generated file.
# The bare-token predicate (regex + reserved-scalar tuple) is the shared
# hotato.errors.is_safe_bare_token (finding #7), imported above as
# _is_safe_bare_token, so this emitter and conversation_test's starter emitter
# use one definition and can never drift.


def _tool_call_names_seen(spans: Sequence[Dict[str, Any]]) -> Tuple[List[str], List[str]]:
    """Distinct ``tool_call`` span names, in first-seen order, split into
    ``(safe, unsafe)``: ``safe`` names get an inferred assertion; ``unsafe``
    names (a character this generator will not safely emit unquoted) are
    reported back, never silently dropped, but no assertion is generated for
    one here."""
    safe: List[str] = []
    unsafe: List[str] = []
    for s in spans:
        if s.get("type") != "tool_call":
            continue
        name = s.get("name")
        if not name or name in safe or name in unsafe:
            continue
        if _is_safe_bare_token(name):
            safe.append(name)
        else:
            unsafe.append(name)
    return safe, unsafe


def build_init_stub(
    spans: Optional[Sequence[Dict[str, Any]]], *,
    timing: Any = None,
    source_trace: Optional[str] = None,
) -> Dict[str, Any]:
    """Infer a starter assertions document from a trace's ``tool_call``
    spans and optional ``timing`` (an ``envelope.v1`` ``events`` list or a
    single event dict, e.g. from scoring a recording with ``--stereo``):

    * one ``tool_call`` "was it called" assertion per distinct (safe-to-
      render) tool name seen in the trace;
    * one ``tool_call`` ``require_order`` assertion, additionally, when 2+
      distinct tool names were observed (in first-seen order);
    * one ``outcome`` ``field_present`` starter pointed at this run's own
      ``verdict.did_yield`` field, ONLY when ``timing`` was supplied.

    Returns ``{"doc": <validated-shape assertions doc>, "yaml": <rendered
    text>, "tool_names": [...], "skipped_tool_names": [...],
    "used_timing": bool}``. Raises ``ValueError`` when nothing could be
    inferred (no tool_call spans with a renderable name, and no timing) --
    a starter file is never written empty or fabricated; the message
    suggests ``--stereo`` or hand-authoring instead."""
    tool_names, skipped = _tool_call_names_seen(spans or [])

    assertions: List[Dict[str, Any]] = []
    for name in tool_names:
        assertions.append({"id": f"{name}-called", "kind": "tool_call", "name": name})
    if len(tool_names) >= 2:
        assertions.append({
            "id": "tool-call-order",
            "kind": "tool_call",
            "require_order": list(tool_names),
        })
    used_timing = timing is not None
    if used_timing:
        assertions.append({
            "id": "produced-a-verdict",
            "kind": "outcome",
            "all_of": [{"field_present": "verdict.did_yield"}],
        })

    if not assertions:
        raise ValueError(
            "nothing to infer a starter assertions file from: no tool_call "
            "spans with a renderable name were found in "
            f"{source_trace!r} and no timing was supplied. Pass --stereo "
            "to score the recording and seed a timing-based starter, or "
            "write assertions.yaml by hand (see docs/ASSERTIONS.md for the "
            "shape)."
        )

    doc = {"version": SUPPORTED_DOC_VERSION, "assertions": assertions}
    header = _init_stub_header(
        source_trace=source_trace, tool_names=tool_names, skipped=skipped,
        used_timing=used_timing,
    )
    return {
        "doc": doc,
        "yaml": header + render_assertions_yaml(doc),
        "tool_names": tool_names,
        "skipped_tool_names": skipped,
        "used_timing": used_timing,
    }


def _init_stub_header(
    *, source_trace: Optional[str], tool_names: List[str], skipped: List[str],
    used_timing: bool,
) -> str:
    lines = [
        "# assertions.yaml -- STARTER generated by `hotato assert init`",
        f"# from --from-trace {source_trace!r}." if source_trace else "#",
        "# Every check below is inferred from what was actually observed;",
        "# edit ids, add phrase/pii/policy checks, or remove what does not",
        "# apply. Run it, all on one line, e.g.:",
        "#   hotato assert run --assertions assertions.yaml --trace voice_trace.jsonl",
        "# (add --transcript FILE and/or --stereo WAV for phrase and timing checks)",
    ]
    if tool_names:
        lines.append(f"# tool_call spans seen (in order): {', '.join(tool_names)}")
    if skipped:
        lines.append(
            "# skipped (unsafe characters for this generator; add by hand "
            f"if needed): {', '.join(skipped)}"
        )
    if used_timing:
        lines.append(
            "# timing starter seeded from the --stereo recording's own "
            "scored verdict."
        )
    # Left commented (a starter, not an imposed policy): uncomment to make a
    # missing-input INCONCLUSIVE gate CI rather than silently stay green.
    lines.append(
        "# inconclusive_policy: fail  # CI/compliance suites should set fail "
        "or refuse"
    )
    lines.append("")
    return "\n".join(lines) + "\n"


# --- a small, purpose-built YAML emitter (the inverse of the subset above) --
#
# NOT a general-purpose YAML dumper: every scalar value it ever renders is
# generator-controlled (a tool name already filtered to a bare-safe token by
# _is_safe_bare_token, or one of this module's own fixed literal id/key
# strings), so no quoting/escaping corner case is reachable. This mirrors
# parse_assertions_yaml's block-sequence-of-mappings + flow-list/dict shape
# exactly (see PLAN_EXAMPLE in tests/test_assert.py), so round-tripping
# render_assertions_yaml -> parse_assertions_yaml -> validate_assertions_doc
# reproduces the exact input document.

def _yaml_flow_scalar(value: Any) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if value is None:
        return "null"
    if isinstance(value, (int, float)):
        return str(value)
    return str(value)


def _yaml_flow_value(value: Any) -> str:
    if isinstance(value, dict):
        inner = ", ".join(f"{k}: {_yaml_flow_value(v)}" for k, v in value.items())
        return "{" + inner + "}"
    if isinstance(value, list):
        return "[" + ", ".join(_yaml_flow_value(v) for v in value) + "]"
    return _yaml_flow_scalar(value)


def render_assertions_yaml(doc: Dict[str, Any]) -> str:
    """Render an already-validated-shape assertions document (``{"version":
    int, "assertions": [{"id": ..., "kind": ..., ...}, ...]}``, e.g. from
    :func:`build_init_stub`) as YAML text in exactly the block-sequence-of-
    mappings-plus-flow-collections subset :func:`parse_assertions_yaml`
    reads back. See the module note above for why this is safe without a
    general escaping scheme."""
    lines = [f"version: {doc['version']}", "assertions:"]
    for item in doc["assertions"]:
        first = True
        for key, value in item.items():
            prefix = "  - " if first else "    "
            first = False
            lines.append(f"{prefix}{key}: {_yaml_flow_value(value)}")
    return "\n".join(lines) + "\n"
