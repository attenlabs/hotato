# OTel ingest: two source shapes

`hotato trace ingest --otel FILE` recognizes two input shapes. Both convert
into the same `hotato.voice_trace.v1` spans; which one it used is recorded
in `source.format` (`"otel-json"` or `"otel-jsonl-bridge"`).

This is a bridge, not an OTel collector: it reads a file you already have
(an exported trace, or a small script's own event log), offline, once. It
is not a running OTel receiver, does not speak the OTLP wire protocol, and
does not claim full OpenTelemetry semantic-convention coverage -- only
`name`, `startTimeUnixNano`/`endTimeUnixNano`, `attributes`, and span
`events` are read from a standard export.

## 1. Standard OTel JSON export (`otel-json`)

A single JSON document with a top-level `resourceSpans` array (the shape a
real OTel exporter/collector writes):

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

- Resource attributes are flattened into a plain dict; `service.name`
  (or a custom `stack` attribute) becomes `deployment.stack`, `git_sha` /
  `config_hash` attributes (if present) fill the matching deployment
  fields. Only the FIRST `resourceSpans` entry's resource is used for
  deployment metadata; every entry's spans are still walked.
- Timestamps convert to seconds relative to the EARLIEST timestamp anywhere
  in the file (matching the audio-relative-seconds convention every other
  hotato timestamp uses) -- not wall-clock time.
- A span's own `name` maps to a hotato span `type` via a small documented
  table (`caller_audio_active`, `agent_audio_active`,
  `tts.cancel_requested` -> `tts_cancel_requested`,
  `tts.audio_stopped` -> `tts_audio_stopped`, `asr.partial` -> `asr_partial`,
  `llm.first_token` -> `llm_first_token`); an unmapped name passes through
  unchanged.
- Span `events` (OTel's own point-in-time markers nested inside a span --
  the natural place a real pipeline would put a `tts.cancel_requested`
  marker inside a broader `tts_playback` span) are flattened into their own
  point events the same way top-level spans are.
- A `tool_call`-mapped span's `tool.name` / `gen_ai.tool.name` attribute
  becomes the span's `name` field; a `latency_ms` attribute is used if
  present, otherwise computed from the span's own start/end.
- An `asr_partial`-mapped span's `text` / `asr.transcript.partial` attribute
  is captured but immediately redacted (dropped, `text_redacted: true`)
  unless `--include-text` was passed.

## 2. Hotato's OTel bridge JSONL (`otel-jsonl-bridge`)

The simpler, documented shape for a script or test fixture that does not
have a real OTel exporter: one JSON object per line (or one bare JSON array
of the same objects). Each line is either a span or a meta/resource line.

A span line uses `type` directly (never `name` -- `name` on a span line is
reserved for `tool_call`'s own tool name, not the span kind):

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
uses this shape.

## Which one to use

If you already run an OTel collector or SDK and can dump a trace export,
point `--otel` at that file directly -- the standard-export path is
best-effort but reads it. If you are wiring a quick script (a webhook
handler, a log-line scraper) that does not go through OTel at all, write the
bridge JSONL directly; it is the same information with none of the nesting.
