# Voice traces

A voice trace is a timeline of discrete voice-pipeline events (caller/agent
audio activity, TTS cancel/stop, ASR partials, tool calls, ...) that
supplements a failure contract's frame-level timing evidence with the WHY
layer a caller/agent audio track alone cannot show: "the agent talked over
the caller" becomes "evidence suggests TTS cancellation lagged: cancel
requested at 42.40s, audio stopped at 43.60s".

Hotato does not prove root cause from a trace. It reports coincidence: a
pattern of events that lined up with the measured timing. See
[`docs/OTEL.md`](OTEL.md) for the ingest source formats.

Three commands:

```bash
hotato trace ingest --otel traces.jsonl --out voice_trace.jsonl
hotato trace attach contracts/refund-cutoff-001.hotato --trace voice_trace.jsonl
hotato trace export contracts/refund-cutoff-001.hotato --format otel --out otel.jsonl
```

## Schema

`voice_trace.jsonl` is JSONL (one meta line, then one line per span -- the
same convention `evidence/frames.jsonl` uses), validating against
`hotato.voice_trace.v1` (`src/hotato/schema/voice_trace.v1.json`) once
reassembled by `hotato.trace.load_voice_trace_jsonl`:

```json
{
  "schema": "hotato.voice_trace.v1",
  "call_id": null,
  "deployment": {"stack": "vapi", "agent_id": null, "git_sha": "deadbeef", "config_hash": "sha256:..."},
  "spans": [
    {"type": "agent_audio_active", "start_sec": 0.0, "end_sec": 2.9},
    {"type": "tts_cancel_requested", "time_sec": 2.6},
    {"type": "tts_audio_stopped", "time_sec": 2.9},
    {"type": "asr_partial", "start_sec": 2.4, "end_sec": 2.95, "text_redacted": true},
    {"type": "tool_call", "start_sec": 1.1, "end_sec": 1.42, "name": "lookup_order", "latency_ms": 320}
  ],
  "source": {"format": "otel-jsonl-bridge", "input_span_count": 6}
}
```

A span carries an open `type` string; the common ones are
`caller_audio_active`, `agent_audio_active`, `tts_cancel_requested`,
`tts_audio_stopped`, `asr_partial`, `tool_call`, `llm_first_token`,
`handoff`. An unrecognized type is passed through unchanged, never dropped
(additive, forward-compatible with a pipeline that emits a span type this
release does not name). Exactly one time shape applies per span: an
interval span carries `start_sec`/`end_sec`, a point event carries
`time_sec`.

## Redaction by default

`call_id` and `deployment.agent_id` are dropped (`null`) unless
`--include-identifiers` is passed at ingest time. An `asr_partial` span's
transcript text is dropped (`text_redacted: true`, no `text` key) unless
`--include-text` is passed. `deployment.stack` / `git_sha` / `config_hash`
are not treated as identifiers and are kept by default.

## Ingest

```bash
hotato trace ingest --otel traces.jsonl --out voice_trace.jsonl
hotato trace ingest --otel export.json --out voice_trace.jsonl \
    --stack vapi --include-identifiers --include-text
```

`--otel FILE` accepts either a standard OTel JSON export (a document with a
top-level `resourceSpans` array) or hotato's own documented OTel bridge
JSONL (see [`docs/OTEL.md`](OTEL.md)). `--stack` / `--call-id` /
`--agent-id` / `--git-sha` / `--config-hash` override or fill in whatever
the source's own resource attributes carried. Refused (exit 2, nothing
written) for an unreadable file, an empty file, or a source with zero
spans.

## Attach

```bash
hotato trace attach contracts/refund-cutoff-001.hotato --trace voice_trace.jsonl
```

Copies the trace into `<bundle>/traces/voice_trace.jsonl` and re-renders
`evidence/timeline.html` with the trace's events drawn as an additional row,
aligned to the SAME [0, duration] scale as the existing caller/agent
timeline. This reads the bundle's OWN `evidence/frames.jsonl` and
`contract.json` back in -- it never re-runs the VAD or the diarizer, so
attaching a trace never needs the diarization extra installed and never
re-scores the audio. On a diarized-mono bundle (no frame-level evidence),
the base timeline states that instead of fabricating one, and the
trace row still renders on its own scale.

`contract.json` records the attachment (additive, schema-safe: `trace:
{attached, path, span_count, attached_at, source_format}`). Refused (exit 2)
for a missing bundle, a trace file that does not validate as a
`hotato.voice_trace.v1` JSONL, or an already-attached trace without
`--force`.

### Report wording

When a `tts_cancel_requested` / `tts_audio_stopped` pair is present, the
timeline states the measured delta plainly:

> Evidence suggests TTS cancellation delay: cancel requested at 2.60s, audio
> stopped at 2.90s (delta 0.30s).
> Hotato does not prove root cause.
> Unknowns: no client-side playout trace was attached.

The last line is always present in this release: a client-side audio
playout trace (the point where the CALLER'S device stopped
rendering audio, as opposed to when the server issued the stop) is not a
span type this release collects, so the gap is always named rather
than silently omitted.

## Export

```bash
hotato trace export contracts/refund-cutoff-001.hotato --format otel --out otel.jsonl
```

Writes the bundle's attached trace back out as hotato's OTel bridge JSONL --
the exact shape `trace ingest` reads, so `ingest -> attach -> export ->
ingest` round-trips the identical spans. `--format` is claimed by the export
format on this subcommand (only `otel` is supported today); pass `--json`
for the machine result summary instead of the default text line. Refused
(exit 2) when the bundle has no attached trace, or `--out` exists without
`--force`.

## What a voice trace does not prove

Hotato does not prove authorization, identity, compliance, or policy
safety. A voice trace adds timing correlation to a failure contract's
existing timing measurement; it never adds intent, never a root-cause
verdict, and never a claim that a client-side playout event happened at a
particular moment unless one was attached.

## Read more

- Source formats and the OTel bridge JSONL shape:
  [`docs/OTEL.md`](OTEL.md)
- Failure contracts a trace attaches to: [`docs/CONTRACTS.md`](CONTRACTS.md)
