"""``hotato trace ingest/attach/export``: the voice-trace observability bridge.

A voice trace is a JSONL timeline of discrete voice-pipeline events (audio
activity, TTS cancel/stop, ASR partials, tool calls, ...) that supplements a
failure contract's frame-level timing evidence with the WHY layer a caller
and agent audio track alone cannot show: "the agent talked over the caller"
becomes "evidence suggests TTS cancellation lagged: cancel requested at
42.40s, audio stopped at 43.60s". This module never claims that evidence
proves root cause; it renders findings alongside an explicit "does not prove
root cause" note and an "Unknowns" line whenever a client-side playout trace
was not attached (see ``_findings_lines``).

Three commands, per ``docs/TRACE.md`` / ``docs/OTEL.md``:

* ``trace ingest --otel FILE --out voice_trace.jsonl`` parses an OTel-flavored
  source into ``hotato.voice_trace.v1`` and writes it as JSONL (one meta line,
  then one line per span -- the same convention ``evidence/frames.jsonl``
  uses). Two input shapes are recognized: a standard OTel JSON export (a
  single document with a top-level ``resourceSpans`` array; best-effort
  span/span-event flattening, NOT full OTel wire-protocol coverage) and
  hotato's own documented per-line bridge shape (``{"type": ..., "start_sec"/
  "end_sec"/"time_sec": ..., ...}``), the format ``trace export`` writes back
  out.
* ``trace attach <bundle> --trace voice_trace.jsonl`` copies the trace into
  ``<bundle>/traces/voice_trace.jsonl`` and re-renders
  ``evidence/timeline.html`` with the trace's events drawn as an additional,
  scale-aligned row below the existing caller/agent timeline. It rebuilds the
  audio timeline from the bundle's OWN already-computed
  ``evidence/frames.jsonl`` and ``contract.json`` -- it never re-runs the VAD
  or diarizer, so attaching a trace never needs the diarization extra
  installed and never re-scores the audio.
* ``trace export <bundle> --format otel --out FILE`` writes the bundle's
  attached trace back out as hotato's OTel-flavored bridge JSONL (the exact
  shape ``trace ingest`` reads back in, so ``ingest`` -> ``attach`` ->
  ``export`` -> ``ingest`` round-trips the same spans).

Redaction by default: ``call_id`` and ``deployment.agent_id`` are dropped
(``None``) unless ``--include-identifiers`` was passed at ingest time; an
``asr_partial`` span's transcript text is dropped (``text_redacted: true``,
no ``text`` key) unless ``--include-text`` was passed. Hotato does not prove
authorization, identity, compliance, or policy safety; a voice trace adds
timing correlation, never intent.
"""

from __future__ import annotations

from .errors import open_regular as _open_regular

import json
import os
import tempfile
from datetime import datetime, timezone
from typing import Optional

from ._engine.score import ScoreConfig
from . import contract as _contract
from . import report as _report

__all__ = [
    "SCHEMA",
    "TRACE_REL_PATH",
    "ingest_otel",
    "render_ingest_text",
    "ingest_result_json",
    "attach_trace",
    "render_attach_text",
    "attach_result_json",
    "export_trace",
    "render_export_text",
    "export_result_json",
    "load_voice_trace_jsonl",
]

SCHEMA = "hotato.voice_trace.v1"
CREATED_BY_INGEST = "hotato trace ingest"
CREATED_BY_ATTACH = "hotato trace attach"
# Derived from contract.py's own _REL["traces_dir"] rather than a second
# hardcoded "traces" literal, so the two modules can never drift apart.
TRACE_REL_PATH = _contract._REL["traces_dir"] + "/voice_trace.jsonl"

# Documented, commonly-emitted span types (the set is OPEN: an unrecognized
# type is passed through as-is rather than dropped or rejected).
CANONICAL_SPAN_TYPES = (
    "caller_audio_active", "agent_audio_active", "tts_cancel_requested",
    "tts_audio_stopped", "asr_partial", "tool_call", "llm_first_token",
    "handoff",
)

_NOT_PROVED_TRACE = (
    "Hotato does not prove root cause. A voice trace adds timing "
    "correlation only."
)


def _now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _mkparents(path: str) -> None:
    d = os.path.dirname(path)
    if d:
        os.makedirs(d, exist_ok=True)


def _atomic_write(path: str, text: str) -> None:
    """Same tmp-file-then-rename shape ``contract.pack_contract`` uses for a
    standalone output file: a crash mid-write never truncates a previously
    good file at ``path``."""
    _mkparents(path)
    d = os.path.dirname(os.path.abspath(path)) or "."
    fd, tmp = tempfile.mkstemp(dir=d, prefix=".hotato-trace-tmp-", suffix=".part")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(text)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


# --- voice_trace.jsonl read/write (meta line + one span per line) ---------

def _dump_voice_trace_jsonl(vt: dict) -> str:
    meta = {k: v for k, v in vt.items() if k != "spans"}
    meta["_meta"] = True
    lines = [json.dumps(meta, sort_keys=True)]
    for s in vt.get("spans") or []:
        lines.append(json.dumps(s, sort_keys=True))
    return "\n".join(lines) + "\n"


def load_voice_trace_jsonl(path: str) -> dict:
    """Read a ``voice_trace.jsonl`` (meta line + one span per line, the shape
    :func:`ingest_otel` writes) back into the full ``hotato.voice_trace.v1``
    object shape (``{"schema", ..., "spans": [...]}``)."""
    try:
        with _open_regular(path, "r", encoding="utf-8") as fh:
            raw_lines = [ln for ln in (l.strip() for l in fh) if ln]
    except OSError as exc:
        raise ValueError(f"{path!r} is not a readable voice trace: {exc}") from exc
    if not raw_lines:
        raise ValueError(f"{path!r} is empty; not a hotato voice trace")
    try:
        meta = json.loads(raw_lines[0])
    except json.JSONDecodeError as exc:
        raise ValueError(f"{path!r} is not a readable voice trace: {exc}") from exc
    if not meta.get("_meta") or meta.get("schema") != SCHEMA:
        raise ValueError(
            f"{path!r} is not a {SCHEMA} voice trace (missing/mismatched "
            "meta line)"
        )
    meta.pop("_meta", None)
    spans = []
    for ln in raw_lines[1:]:
        try:
            spans.append(json.loads(ln))
        except json.JSONDecodeError as exc:
            raise ValueError(f"{path!r} has a corrupt span line: {exc}") from exc
    meta["spans"] = spans
    return meta


# --- ingest: OTel-flavored source -> hotato.voice_trace.v1 ----------------

def _flatten_otel_attributes(attrs) -> dict:
    """OTel's list-of-{key,value} attribute shape -> a plain dict. Accepts a
    plain dict too (already flattened), so a hand-written bridge fixture
    does not need to use the verbose OTel shape."""
    if isinstance(attrs, dict):
        return dict(attrs)
    out = {}
    for a in attrs or []:
        key = a.get("key")
        if key is None:
            continue
        val = a.get("value") or {}
        for vk in ("stringValue", "intValue", "doubleValue", "boolValue"):
            if vk in val:
                out[key] = val[vk]
                break
        else:
            out[key] = val
    return out


# OTel standard-export span/event `name` -> hotato span `type`. A name not
# listed here is passed through UNCHANGED (open set; never dropped).
_OTEL_NAME_MAP = {
    "caller_audio_active": "caller_audio_active",
    "agent_audio_active": "agent_audio_active",
    "tts.cancel_requested": "tts_cancel_requested",
    "tts_cancel_requested": "tts_cancel_requested",
    "tts.audio_stopped": "tts_audio_stopped",
    "tts_audio_stopped": "tts_audio_stopped",
    "asr.partial": "asr_partial",
    "asr_partial": "asr_partial",
    "llm.first_token": "llm_first_token",
    "llm_first_token": "llm_first_token",
}


def _parse_otel_standard_json(doc: dict) -> tuple:
    """Best-effort flatten of a standard OTel JSON export (a single document
    with top-level ``resourceSpans``). Returns ``(raw_spans, resource)``
    where each raw span is ``{"type", "start_sec"|None, "end_sec"|None,
    "time_sec"|None, "attributes"}`` and ``resource`` is the flattened
    resource-attribute dict from the FIRST resourceSpans entry (real
    deployments carry one resource per export; a multi-resource export's
    later resources are still walked for spans, just not used for
    deployment metadata).

    This is NOT full OTel coverage: only ``name``, ``startTimeUnixNano``,
    ``endTimeUnixNano``, ``attributes``, and span ``events`` are read. Times
    are converted to seconds relative to the EARLIEST timestamp anywhere in
    the export, matching the audio-relative-seconds convention every other
    hotato timestamp uses."""
    resource_spans = doc.get("resourceSpans") or []
    if not resource_spans:
        raise ValueError(
            "OTel JSON export has no resourceSpans; nothing to ingest"
        )
    all_nanos = []
    raw_entries = []  # (name, start_nano, end_nano, attrs)
    resource = {}
    for i, rs in enumerate(resource_spans):
        res_attrs = _flatten_otel_attributes(
            (rs.get("resource") or {}).get("attributes")
        )
        if i == 0:
            resource = res_attrs
        for scope_spans in rs.get("scopeSpans") or []:
            for span in scope_spans.get("spans") or []:
                name = span.get("name") or "span"
                start_nano = span.get("startTimeUnixNano")
                end_nano = span.get("endTimeUnixNano")
                attrs = _flatten_otel_attributes(span.get("attributes"))
                for n in (start_nano, end_nano):
                    if n is not None:
                        all_nanos.append(int(n))
                raw_entries.append((name, start_nano, end_nano, attrs))
                for ev in span.get("events") or []:
                    ev_name = ev.get("name") or name
                    ev_nano = ev.get("timeUnixNano")
                    ev_attrs = _flatten_otel_attributes(ev.get("attributes"))
                    if ev_nano is not None:
                        all_nanos.append(int(ev_nano))
                    raw_entries.append((ev_name, ev_nano, None, ev_attrs))
    if not all_nanos:
        raise ValueError(
            "OTel JSON export has no timestamped spans or span events; "
            "nothing to ingest"
        )
    t0 = min(all_nanos)

    def _sec(nano):
        return None if nano is None else (int(nano) - t0) / 1e9

    out = []
    for name, start_nano, end_nano, attrs in raw_entries:
        mapped = _OTEL_NAME_MAP.get(name, name)
        start_sec = _sec(start_nano)
        end_sec = _sec(end_nano) if end_nano is not None else None
        time_sec = start_sec if (end_sec is None) else None
        span = {"type": mapped}
        if end_sec is not None:
            span["start_sec"] = start_sec
            span["end_sec"] = end_sec
        else:
            span["time_sec"] = time_sec
        if mapped == "tool_call":
            tool_name = attrs.get("tool.name") or attrs.get("gen_ai.tool.name")
            if tool_name is not None:
                span["name"] = tool_name
            if "latency_ms" in attrs:
                span["latency_ms"] = attrs["latency_ms"]
            elif start_sec is not None and end_sec is not None:
                span["latency_ms"] = round((end_sec - start_sec) * 1000, 3)
        if mapped == "asr_partial":
            text = attrs.get("text") or attrs.get("asr.transcript.partial")
            if text is not None:
                span["_raw_text"] = text  # consumed + dropped by _redact_spans
        if attrs:
            span["attributes"] = attrs
        out.append(span)
    return out, resource


def _parse_bridge_jsonl(text: str) -> tuple:
    """hotato's own documented bridge shape: one JSON object per line, each
    either a span (``{"type": ..., ...}``) or a meta line (``{"call_id":
    ..., "deployment": {...}}``, no ``"type"`` key). A bare JSON array of the
    same span objects is also accepted (some exporters write one array
    instead of newline-delimited records)."""
    text = text.strip()
    if not text:
        raise ValueError("the input file is empty; nothing to ingest")
    try:
        whole = json.loads(text)
    except json.JSONDecodeError:
        whole = None
    if isinstance(whole, list):
        records = whole
    elif isinstance(whole, dict) and "resourceSpans" not in whole:
        records = [whole]
    else:
        records = []
        for i, ln in enumerate(text.splitlines()):
            ln = ln.strip()
            if not ln:
                continue
            try:
                records.append(json.loads(ln))
            except json.JSONDecodeError as exc:
                raise ValueError(
                    f"line {i + 1} of the OTel bridge JSONL is not valid "
                    f"JSON: {exc}"
                ) from exc
    resource = {}
    call_id = None
    spans = []
    for rec in records:
        if "type" not in rec and "name" not in rec:
            # meta/resource line
            resource.update(rec.get("deployment") or {})
            if rec.get("call_id"):
                call_id = rec["call_id"]
            continue
        span = dict(rec)
        if "type" not in span:
            # A record with no "type" but a "name" is treated as naming the
            # span KIND via "name" (mirroring real OTel span.name); this is
            # the ONLY case "name" is consumed as the type -- a record that
            # already has "type" keeps its own "name" untouched (tool_call's
            # tool name, for example).
            span["type"] = span.pop("name", None)
        text_val = span.pop("text", None)
        if text_val is not None:
            span["_raw_text"] = text_val
        spans.append(span)
    if not spans:
        raise ValueError(
            "no spans found in the OTel bridge JSONL (every line looked "
            "like a meta/resource line); see docs/OTEL.md for the shape"
        )
    return spans, resource, call_id


def _redact_spans(spans: list, *, include_text: bool) -> list:
    out = []
    for s in spans:
        s = dict(s)
        raw_text = s.pop("_raw_text", None)
        if s.get("type") == "asr_partial" or raw_text is not None:
            if include_text and raw_text is not None:
                s["text"] = raw_text
                s["text_redacted"] = False
            else:
                s.pop("text", None)
                s["text_redacted"] = True
        out.append(s)
    return out


def _sort_key(span: dict):
    t = span.get("start_sec")
    if t is None:
        t = span.get("time_sec")
    return (t if t is not None else float("inf"),)


def ingest_otel(
    otel_path: str,
    *,
    out_path: str,
    call_id: Optional[str] = None,
    stack: Optional[str] = None,
    agent_id: Optional[str] = None,
    git_sha: Optional[str] = None,
    config_hash: Optional[str] = None,
    include_identifiers: bool = False,
    include_text: bool = False,
    force: bool = False,
) -> dict:
    """Parse ``otel_path`` (a standard OTel JSON export, or hotato's OTel
    bridge JSONL) into ``hotato.voice_trace.v1`` and write it to ``out_path``
    as JSONL. Raises ``ValueError`` (CLI exit 2) for an unreadable file, an
    empty export, or a source with no spans; nothing is written in that
    case."""
    if os.path.exists(out_path) and not force:
        raise ValueError(
            f"{out_path!r} already exists; pass --force to overwrite it, or "
            "choose a new --out"
        )
    try:
        with _open_regular(otel_path, "r", encoding="utf-8") as fh:
            text = fh.read()
    except OSError as exc:
        raise ValueError(f"{otel_path!r} is not readable: {exc}") from exc

    doc = None
    try:
        candidate = json.loads(text)
        if isinstance(candidate, dict) and "resourceSpans" in candidate:
            doc = candidate
    except json.JSONDecodeError:
        pass

    if doc is not None:
        raw_spans, resource = _parse_otel_standard_json(doc)
        src_call_id = None
        fmt = "otel-json"
    else:
        raw_spans, resource, src_call_id = _parse_bridge_jsonl(text)
        fmt = "otel-jsonl-bridge"

    spans = _redact_spans(raw_spans, include_text=include_text)
    spans.sort(key=_sort_key)

    resolved_call_id = call_id or src_call_id
    vt = {
        "schema": SCHEMA,
        "created_at": _now_iso(),
        "created_by": CREATED_BY_INGEST,
        "call_id": resolved_call_id if include_identifiers else None,
        "deployment": {
            "stack": stack or resource.get("stack") or resource.get("service.name"),
            "agent_id": (
                (agent_id or resource.get("agent_id")) if include_identifiers else None
            ),
            "git_sha": git_sha or resource.get("git_sha"),
            "config_hash": config_hash or resource.get("config_hash"),
        },
        "spans": spans,
        "source": {"format": fmt, "input_span_count": len(spans)},
    }
    _atomic_write(out_path, _dump_voice_trace_jsonl(vt))
    return {"path": out_path, "voice_trace": vt, "count": len(spans)}


def render_ingest_text(result: dict) -> str:
    vt = result["voice_trace"]
    lines = [
        f"ingested voice trace: {result['path']}",
        f"  format:  {vt['source']['format']}",
        f"  spans:   {result['count']}",
        f"  stack:   {vt['deployment'].get('stack') or 'unknown'}",
    ]
    types = sorted({s.get("type") for s in vt["spans"]})
    lines.append(f"  types:   {', '.join(types) if types else '(none)'}")
    lines.append("next:")
    lines.append(f"  hotato trace attach BUNDLE.hotato --trace {result['path']}")
    return "\n".join(lines)


def ingest_result_json(result: dict) -> dict:
    return {
        "tool": "hotato", "kind": "trace-ingest", "schema_version": "1",
        "path": result["path"], "count": result["count"],
        "voice_trace": result["voice_trace"],
    }


# --- attach: write into a bundle + re-render the evidence timeline --------

def _load_bundle_frames(bundle_dir: str) -> tuple:
    """Read the bundle's OWN ``evidence/frames.jsonl`` (written at `contract
    create` time) back into ``(frames, hop_sec)`` -- never re-runs the VAD or
    diarizer. ``frames`` is ``[]`` and ``hop_sec`` is ``None`` when the
    contract's frame-level evidence was never available (the diarized-mono
    path)."""
    path = os.path.join(bundle_dir, "evidence", "frames.jsonl")
    try:
        with _open_regular(path, "r", encoding="utf-8") as fh:
            lines = [ln for ln in (l.strip() for l in fh) if ln]
    except OSError as exc:
        raise ValueError(f"{bundle_dir!r}: unreadable evidence/frames.jsonl: {exc}") from exc
    if not lines:
        raise ValueError(f"{bundle_dir!r}: evidence/frames.jsonl is empty")
    meta = json.loads(lines[0])
    if meta.get("available") is False:
        return [], None
    hop_sec = meta.get("hop_sec")
    frames = [json.loads(ln) for ln in lines[1:]]
    return frames, hop_sec


def _event_like_from_contract(contract: dict) -> dict:
    """A minimal event dict shaped like the scorer's own event object, built
    straight from the ALREADY-COMPUTED numbers in ``contract.json`` (never a
    fresh score) -- just enough for :func:`hotato.report._event_model` to
    rebuild the same timeline model ``contract create`` originally drew."""
    m = contract["measurement"]
    return {
        "scorable": bool(m.get("scorable")),
        "expected_yield": contract["label"]["expected_behavior"] == "yield",
        "verdict": {
            "did_yield": m.get("did_yield"),
            "seconds_to_yield": m.get("seconds_to_yield"),
            "talk_over_sec": m.get("talk_over_sec"),
            "passed": m.get("passed"),
        },
        "measurements": {"caller_onset_sec": contract["event"].get("onset_sec")},
        "signals": {},
    }


_TRACE_ROW_H = 46
_TRACE_LABEL_Y = 16


def _svg_trace_row(spans: list, *, duration: float) -> str:
    """A single to-scale SVG row of trace event markers, sharing hotato's
    warm-charcoal palette and the SAME [0, duration] scale
    ``hotato.report._svg_timeline`` uses for the timeline above it, so the
    two rows line up by x-position even though they are separate SVG
    elements. Point events (tts_cancel_requested, tts_audio_stopped,
    asr_partial, tool_call, ...) draw a tick + label; interval events
    (caller_audio_active, agent_audio_active present in the trace itself)
    draw a thin bar. A span outside [0, duration] is clamped to the nearest
    edge rather than silently dropped."""
    esc = _report._esc
    C = _report._C
    gut, rpad, pw = _report._GUT, _report._RPAD, _report._PW
    w = gut + pw + rpad
    dur = duration or 1.0

    def X(t: float) -> float:
        t = max(0.0, min(float(t), dur))
        return gut + t * pw / dur

    p = [f'<svg class="tr-svg" viewBox="0 0 {w} {_TRACE_ROW_H}" width="{w}" '
         f'height="{_TRACE_ROW_H}" role="img" '
         f'aria-label="Voice trace events aligned to the same timeline" '
         f'font-family="ui-monospace, SFMono-Regular, Menlo, monospace">']
    p.append(f'<line x1="{gut}" y1="{_TRACE_ROW_H - 14}" x2="{gut + pw}" '
             f'y2="{_TRACE_ROW_H - 14}" stroke="{C["line"]}" stroke-width="1" />')
    p.append(f'<text x="{gut - 12}" y="{_TRACE_ROW_H - 10}" fill="{C["cream"]}" '
             f'font-size="12" text-anchor="end">Trace</text>')
    for s in spans:
        typ = s.get("type", "event")
        if "start_sec" in s and s.get("start_sec") is not None and s.get("end_sec") is not None:
            x1, x2 = X(s["start_sec"]), X(s["end_sec"])
            xw = max(1.5, x2 - x1)
            p.append(f'<rect x="{x1:.1f}" y="{_TRACE_ROW_H - 22}" width="{xw:.1f}" '
                     f'height="8" rx="3" fill="{C["agent"]}" fill-opacity="0.55" />')
            label = typ
        else:
            t = s.get("time_sec")
            if t is None:
                continue
            x = X(t)
            p.append(f'<line x1="{x:.1f}" y1="4" x2="{x:.1f}" y2="{_TRACE_ROW_H - 14}" '
                     f'stroke="{C["ember"]}" stroke-width="1.4" stroke-dasharray="2 2" />')
            if typ == "tool_call" and s.get("name"):
                label = f'tool:{s["name"]}'
            elif typ == "asr_partial":
                label = "asr partial"
            else:
                label = typ
        p.append(f'<text x="{X(s.get("start_sec", s.get("time_sec", 0))):.1f}" '
                 f'y="{_TRACE_LABEL_Y}" fill="{C["muted"]}" font-size="9" '
                 f'text-anchor="start">{esc(label)}</text>')
    p.append("</svg>")
    return "".join(p)


def _findings_lines(spans: list) -> list:
    """The literal wording pattern the operator's canon specifies: an
    'evidence suggests' line when a TTS-cancel/stop pair is present, always
    followed by the root-cause disclaimer, always followed by an explicit
    Unknowns line whenever no client-side playout trace was attached (this
    release never collects one, so the line is always honestly present)."""
    lines = []
    cancel = next((s for s in spans if s.get("type") == "tts_cancel_requested"), None)
    stopped = next((s for s in spans if s.get("type") == "tts_audio_stopped"), None)
    if cancel and stopped:
        t1 = cancel.get("time_sec", cancel.get("start_sec"))
        t2 = stopped.get("time_sec", stopped.get("start_sec"))
        if t1 is not None and t2 is not None:
            delta = t2 - t1
            lines.append(
                f"Evidence suggests TTS cancellation delay: cancel requested "
                f"at {t1:.2f}s, audio stopped at {t2:.2f}s (delta {delta:.2f}s)."
            )
    lines.append(_NOT_PROVED_TRACE)
    has_client_playout = any(
        s.get("type") == "client_audio_playout" for s in spans
    )
    if not has_client_playout:
        lines.append(
            "Unknowns: no client-side playout trace was attached."
        )
    return lines


def _render_timeline_with_trace_html(
    *, contract_id: str, expect: str, model: Optional[dict], spans: list,
) -> str:
    esc = _report._esc
    s = _report._s
    if model is not None and model["has_frames"]:
        base_svg = _report._svg_timeline(model)
        duration = model["duration"]
        stats = [
            ("caller onset", s(model["onset"])),
            ("time to yield", s(model["seconds_to_yield"])),
            ("talk-over", s(model["talk_over_sec"])),
            ("response gap", s(model["response_gap_sec"])),
        ]
    else:
        base_svg = (
            '<div class="note">no frame-level timeline for this contract '
            "(diarized-mono path); trace events are drawn on their own "
            "timeline below.</div>"
        )
        max_t = 1.0
        for sp in spans:
            for k in ("start_sec", "end_sec", "time_sec"):
                v = sp.get(k)
                if v is not None:
                    max_t = max(max_t, float(v))
        duration = max_t + 1.0
        stats = []
    stat_html = "".join(
        f'<div class="stat"><span class="k">{esc(k)}</span>'
        f'<span class="v">{esc(v)}</span></div>' for k, v in stats
    )
    trace_svg = _svg_trace_row(spans, duration=duration)
    findings = _findings_lines(spans)
    findings_html = "".join(f'<p class="finding">{esc(f)}</p>' for f in findings)
    return (
        "<!doctype html>\n<html lang=\"en\"><head><meta charset=\"utf-8\">"
        "<meta name=\"viewport\" content=\"width=device-width, initial-scale=1\">"
        f"<title>hotato contract {esc(contract_id)}: timeline evidence "
        "(trace-attached)</title>"
        f"<style>{_contract._TIMELINE_CSS}"
        ".finding{color:#ead9a6;font-size:12.5px;margin:2px 0}"
        "</style></head><body><div class=\"wrap\">"
        f"<h1>{esc(contract_id)}: timeline evidence</h1>"
        f'<p class="sub">expect {esc(expect)} &middot; frame-level evidence '
        "plus an attached voice trace.</p>"
        f'<div class="tl">{base_svg}{trace_svg}</div>'
        f'<div class="stats">{stat_html}</div>'
        f'<div style="margin-top:10px">{findings_html}</div>'
        f'<p class="note">{esc(_contract._NOT_PROVED)}</p>'
        "</div></body></html>\n"
    )


def attach_trace(bundle_dir: str, trace_path: str, *, force: bool = False) -> dict:
    """Copy ``trace_path`` (a ``hotato.voice_trace.v1`` JSONL, written by
    :func:`ingest_otel`) into ``<bundle>/traces/voice_trace.jsonl`` and
    re-render ``evidence/timeline.html`` with the trace's events drawn as a
    scale-aligned row. Rebuilds the timeline from the bundle's OWN
    ``evidence/frames.jsonl`` and ``contract.json`` -- never re-scores the
    audio. Raises ``ValueError`` (CLI exit 2) for a missing bundle, an
    unreadable/mismatched trace file, or an already-attached trace without
    ``--force``."""
    contract = _contract._load_contract(bundle_dir)
    dest = os.path.join(bundle_dir, TRACE_REL_PATH)
    already = os.path.isfile(dest) and os.path.getsize(dest) > 0
    if already and not force:
        raise ValueError(
            f"{bundle_dir!r} already has an attached trace at "
            f"{TRACE_REL_PATH!r}; pass --force to replace it"
        )
    vt = load_voice_trace_jsonl(trace_path)
    spans = vt.get("spans") or []

    frames, hop_sec = _load_bundle_frames(bundle_dir)
    model = None
    if frames:
        event_like = _event_like_from_contract(contract)
        model = _report._event_model(event_like, frames, hop_sec, ScoreConfig())

    timeline_html = _render_timeline_with_trace_html(
        contract_id=contract["id"], expect=contract["label"]["expected_behavior"],
        model=model, spans=spans,
    )
    _atomic_write(os.path.join(bundle_dir, "evidence", "timeline.html"), timeline_html)
    _atomic_write(dest, _dump_voice_trace_jsonl(vt))

    gitkeep = os.path.join(bundle_dir, "traces", ".gitkeep")
    if os.path.isfile(gitkeep):
        try:
            os.remove(gitkeep)
        except OSError:
            pass

    contract["trace"] = {
        "attached": True,
        "path": TRACE_REL_PATH,
        "span_count": len(spans),
        "attached_at": _now_iso(),
        "source_format": vt.get("source", {}).get("format"),
    }
    _atomic_write(
        os.path.join(bundle_dir, "contract.json"),
        json.dumps(contract, indent=2, sort_keys=True) + "\n",
    )
    return {
        "dir": bundle_dir, "id": contract["id"], "trace_path": dest,
        "span_count": len(spans),
        "timeline_path": os.path.join(bundle_dir, "evidence", "timeline.html"),
    }


def render_attach_text(result: dict) -> str:
    return (
        f"attached voice trace to {result['id']}: {result['span_count']} spans\n"
        f"  trace:    {result['trace_path']}\n"
        f"  timeline: {result['timeline_path']} (re-rendered)"
    )


def attach_result_json(result: dict) -> dict:
    return {
        "tool": "hotato", "kind": "trace-attach", "schema_version": "1",
        "dir": result["dir"], "id": result["id"],
        "span_count": result["span_count"], "trace_path": result["trace_path"],
        "timeline_path": result["timeline_path"],
    }


# --- export: attached trace -> OTel-flavored bridge JSONL -----------------

def export_trace(bundle_dir: str, *, out_path: str, fmt: str = "otel",
                 force: bool = False) -> dict:
    """Write the bundle's attached voice trace back out as hotato's
    OTel-flavored bridge JSONL (the shape :func:`ingest_otel` reads). Raises
    ``ValueError`` (CLI exit 2) when no trace is attached, ``fmt`` is not
    ``"otel"``, or ``out_path`` exists without ``--force``."""
    if fmt != "otel":
        raise ValueError(f"unsupported --format {fmt!r}; only 'otel' is supported")
    trace_path = os.path.join(bundle_dir, TRACE_REL_PATH)
    if not os.path.isfile(trace_path) or os.path.getsize(trace_path) == 0:
        raise ValueError(
            f"{bundle_dir!r} has no attached trace ({TRACE_REL_PATH!r} is "
            "missing or empty); run `hotato trace attach` first"
        )
    if os.path.exists(out_path) and not force:
        raise ValueError(
            f"{out_path!r} already exists; pass --force to overwrite it, or "
            "choose a new --out"
        )
    vt = load_voice_trace_jsonl(trace_path)
    lines = [json.dumps(
        {"call_id": vt.get("call_id"), "deployment": vt.get("deployment")},
        sort_keys=True,
    )]
    for s in vt.get("spans") or []:
        span_out = {"type": s.get("type")}
        for k in ("start_sec", "end_sec", "time_sec", "name", "latency_ms",
                  "text_redacted", "text", "attributes"):
            if k in s:
                span_out[k] = s[k]
        lines.append(json.dumps(span_out, sort_keys=True))
    text = "\n".join(lines) + "\n"
    _atomic_write(out_path, text)
    return {"path": out_path, "bundle_dir": bundle_dir, "count": len(vt.get("spans") or [])}


def render_export_text(result: dict) -> str:
    return (
        f"exported {result['count']} spans: {result['bundle_dir']} -> "
        f"{result['path']}"
    )


def export_result_json(result: dict) -> dict:
    return {
        "tool": "hotato", "kind": "trace-export", "schema_version": "1",
        "path": result["path"], "bundle_dir": result["bundle_dir"],
        "count": result["count"],
    }
