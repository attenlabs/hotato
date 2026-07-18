# Why is my voice agent's perceived latency worse than its dashboard?

Because a dashboard averages each pipeline component on its own while the
caller feels the sum of every hop on one specific turn: hotato renders a
per-hop latency waterfall (STT, LLM, tool call, TTS, transport) derived from
the trace spans of the exact call being scored, next to that call's timing
verdict, so the slow hop on the turn that went wrong is a row in a table
instead of a hunch.

## From a trace to the waterfall

Two commands. First normalize a trace into `hotato.voice_trace.v1`; the input
is a standard OTel JSON export or hotato's documented bridge JSONL (both
shapes: [`docs/OTEL.md`](OTEL.md)), and the repo ships a worked example at
`tests/data/otel/demo-trace.otel.jsonl`:

```console
$ hotato trace ingest --otel tests/data/otel/demo-trace.otel.jsonl --out voice_trace.jsonl
ingested voice trace: voice_trace.jsonl
  format:  otel-jsonl-bridge
  spans:   6
  stack:   vapi
  types:   agent_audio_active, asr_partial, caller_audio_active, tool_call, tts_audio_stopped, tts_cancel_requested
```

Then render the report for the recording with the trace alongside (the
two-channel example recording here ships in the package):

```console
$ hotato report --stereo src/hotato/data/audio/01-hard-interruption.example.wav \
    --trace voice_trace.jsonl --format md --out report.md --no-fail
wrote markdown report (1 events) to report.md
```

`--format md` writes Markdown; the default self-contained HTML report carries
the same block.

## Read the waterfall

The report's forensic section is derived entirely from evidence already in the
report: the timing verdict, the attached voice trace's span timestamps, and
the evaluated assertions. This is the block the commands above produce,
verbatim:

```markdown
### Per-hop latency waterfall

| hop | latency | derived from |
| --- | --- | --- |
| STT (speech to text) | 550 ms | asr_partial recognition window (first start to last end) |
| LLM (first token) | not captured | no llm_first_token span in the trace |
| Tool call | 320 ms | sum of 1 tool_call latency_ms |
| TTS (speech out) | 300 ms | tts_audio_stopped minus tts_cancel_requested (cancellation lag) |
| Transport (backend HTTP) | not captured | no http_exchange spans in the trace |
```

Each row names the spans it was derived from, so every number is traceable to
a timestamp you supplied. A hop with no spans reads `not captured`, never
estimated: the example trace carries no `llm_first_token` and no
`http_exchange` spans, so those rows say so. A pipeline that emits all five
span kinds gets all five hops measured.

The trace stays context, not a score. The report states it in place: the
spans are rendered alongside the timing measurement, and `did_yield`,
talk-over, time to yield, and the PASS/FAIL verdict are unaffected by
anything in the trace.

## Pin it to a failure

The same trace file attaches to a pinned failure so the waterfall's source
spans travel with the evidence:

```bash
hotato trace attach contracts/demo-missed-interruption.hotato --trace voice_trace.jsonl
```

That writes the trace into the contract bundle and re-renders its evidence
timeline with an aligned trace row. The full trace path (ingest, attach,
export, redaction defaults) is [`docs/TRACE.md`](TRACE.md); the report
surfaces in depth are [`docs/REPORTS.md`](REPORTS.md).
