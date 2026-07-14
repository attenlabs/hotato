# OTel ingest: two source shapes

`hotato trace ingest --otel FILE` turns an OTel trace into hotato's own
`hotato.voice_trace.v1` spans -- reading a file you already have (an
exported trace, or a script's own event log) offline, once, and
translating `name`, `startTimeUnixNano`/`endTimeUnixNano`, `attributes`,
and span `events` into hotato's span shape. It recognizes two input
shapes; `source.format` records which one it used (`"otel-json"` or
`"otel-jsonl-bridge"`).

## 1. Standard OTel JSON export (`otel-json`)

A single JSON document with a top-level `resourceSpans` array (the shape a
standard OTel exporter/collector writes):

```json
{
  "resourceSpans": [{
    "resource": {"attributes": [
      {"key": "service.name", "value": {"stringValue": "vapi"}},
      {"key": "git_sha", "value": {"stringValue": "cafebabe"}}
    ]},
    "scopeSpans": [{"spans": [
      {
        "name": "agent_audio_active",
        "startTimeUnixNano": "1000000000",
        "endTimeUnixNano": "4400000000"
      },
      {
        "name": "tts.cancel_requested",
        "startTimeUnixNano": "2600000000",
        "events": []
      }
    ]}]
  }]
}
```

- Resource attributes flatten into a plain dict: `service.name` (or a custom
  `stack` attribute) becomes `deployment.stack`; `git_sha` / `config_hash`,
  when present, fill the matching deployment fields. Only the FIRST
  `resourceSpans` entry supplies deployment metadata -- every entry's spans
  are still walked.
- Timestamps convert to seconds relative to the EARLIEST timestamp in the
  file, matching the audio-relative-seconds convention every other hotato
  timestamp uses.
- A span's own `name` maps to a hotato span `type` via a small documented
  table (`caller_audio_active`, `agent_audio_active`,
  `tts.cancel_requested` -> `tts_cancel_requested`,
  `tts.audio_stopped` -> `tts_audio_stopped`, `asr.partial` -> `asr_partial`,
  `llm.first_token` -> `llm_first_token`); an unmapped name passes through
  unchanged.
- Span `events` -- OTel's point-in-time markers nested inside a span, e.g.
  a `tts.cancel_requested` marker inside a broader `tts_playback` span --
  flatten into their own point events the same way top-level spans do.
- A `tool_call`-mapped span takes its `name` field from the `tool.name` /
  `gen_ai.tool.name` attribute; a `latency_ms` attribute is used when
  present, otherwise computed from the span's own start/end.
- An `asr_partial`-mapped span's `text` / `asr.transcript.partial` attribute
  is captured and redacted by default (`text_redacted: true`); pass
  `--include-text` to keep it in the output.

## 2. Hotato's OTel bridge JSONL (`otel-jsonl-bridge`)

The shape for a script or test fixture that skips a full OTel exporter: one
JSON object per line (or one bare JSON array of them). Each line is a span
or a meta/resource line. A span line uses `type` directly; `name` is
reserved for `tool_call`'s own tool name, distinct from the span kind:

```
{"type": "caller_audio_active", "start_sec": 2.40, "end_sec": 4.10}
{"type": "agent_audio_active", "start_sec": 0.00, "end_sec": 2.90}
{"type": "tts_cancel_requested", "time_sec": 2.60}
{"type": "tts_audio_stopped", "time_sec": 2.90}
{"type": "asr_partial", "start_sec": 2.40, "end_sec": 2.95, "text": "wait, I need a refund"}
{"type": "tool_call", "start_sec": 1.10, "end_sec": 1.42, "name": "lookup_order", "latency_ms": 320}
```

An optional meta/resource line (no `type` key) attaches deployment
metadata and a call id:

```
{"call_id": "demo-call-001", "deployment": {"stack": "vapi", "agent_id": "agent-demo-1", "git_sha": "deadbeefcafe", "config_hash": "sha256:0f1e2d3c"}}
```

`--call-id` / `--stack` / `--agent-id` / `--git-sha` / `--config-hash` on
`trace ingest` override whatever a meta line (or a standard export's
resource) provided. `hotato trace export` writes exactly this bridge
shape, so `ingest -> attach -> export -> ingest` round-trips the same
spans; the shipped test fixture (`tests/data/otel/demo-trace.otel.jsonl`)
uses it.

## Which one to use

Already running an OTel collector or SDK? Point `--otel` at that trace
export directly -- the standard-export path reads it, best-effort. Wiring a
quick script (a webhook handler, a log-line scraper) outside a full OTel
pipeline? Write the bridge JSONL directly instead -- same information,
already flat.
