# observe: LLM/voice observability, derived on your machine

`hotato observe` reads the observability an LLM or voice agent already emits
as OpenTelemetry spans, on your machine, from files you already have. One
command group, four subcommands, all built on the same
`hotato.voice_trace.v1` spans [`docs/TRACE.md`](TRACE.md) /
[`docs/OTEL.md`](OTEL.md) describe:

```bash
hotato observe capture --out voice_trace.jsonl -- python agent.py
hotato observe cost voice_trace.jsonl --prices starter
hotato observe percentiles traces/ --html latency.html
hotato observe report traces/ --out observe.html --prices starter
```

Every number is derived locally: token counts read from your spans, latency
from their timestamps, USD from a price table you keep. hotato opens no
socket, runs no listener, and makes no network call of its own.

## capture: a local file sink, no account

`hotato observe capture -- <command...>` runs the child process with a local
file sink wired through its environment, then ingests whatever the child
wrote:

- `HOTATO_OTEL_FILE` -- the file path hotato names for the spans.
- `OTEL_EXPORTER_OTLP_ENDPOINT` / `OTEL_EXPORTER_OTLP_TRACES_ENDPOINT` -- a
  `file://` URL pointing at that same path (not a host:port, so there is
  nothing for an exporter to dial out to).
- `OTEL_EXPORTER_OTLP_PROTOCOL=http/json` -- the plain-text OTLP encoding.

A cooperating process writes its spans to that file: either as a standard
OTel JSON export, or as hotato's own OTel bridge JSONL written directly to
`$HOTATO_OTEL_FILE` (the shape in [`docs/OTEL.md`](OTEL.md)). On the child's
exit, hotato ingests the file through the same `trace ingest` path into
`--out` and prints a one-screen summary: span count, per-hop latency, and
token totals. If the child wrote no spans, capture refuses (exit 2) and
leaves nothing behind. The child's own network is the child's business;
hotato adds none.

```bash
hotato observe capture --out run.jsonl -- python agent.py
# observe capture: 7 spans -> run.jsonl
#   child exit: 0
#   per-hop latency:
#     STT (speech to text)      : 550.0 ms
#     LLM (first token)         : 250.0 ms
#     Tool call                 : 320.0 ms
#   tokens:
#     input 1200  output 340  cached not captured  reasoning not captured
```

## cost: tokens are facts, USD is a local estimate

`hotato observe cost <voice_trace.jsonl>` sums each span's LLM token usage
per model and in total. Token attributes resolve by first-match alias:

| category  | attributes (first match wins)                                       |
|-----------|---------------------------------------------------------------------|
| input     | `gen_ai.usage.input_tokens`, `gen_ai.usage.prompt_tokens`, `input_tokens`, `prompt_tokens` |
| output    | `gen_ai.usage.output_tokens`, `gen_ai.usage.completion_tokens`, `output_tokens`, `completion_tokens` |
| cached    | `gen_ai.usage.cached_tokens`, `gen_ai.usage.cache_read_input_tokens`, `cached_tokens` |
| reasoning | `gen_ai.usage.reasoning_tokens`, `reasoning_tokens`                  |

The model is `gen_ai.response.model` (else `gen_ai.request.model`,
`gen_ai.model`, `llm.model_name`, `model`).

Tokens are facts read from the spans. A category no span reported reads
**not captured** (null, plus a count of the spans that lacked it), never 0.

`--prices FILE` adds an estimated USD cost from a **local** per-model $/1M
table (`--prices starter` reads the bundled `src/hotato/data/prices.yaml`).
Copy that file, set the rates from your own agreements, and pass it. USD is
labeled "estimated from `<table>`": the tokens are measured, the dollars are
your arithmetic over your table. A model with no row in the table is
**unpriced** (its cost reads null), never a guessed rate; a category with no
rate in a row is simply not billed.

```yaml
# your-rates.yaml (USD per 1,000,000 tokens)
table: my-contract
per_tokens: 1000000
models:
  gpt-4o:
    input: 2.5
    output: 10.0
    cached: 1.25
```

## percentiles: nearest-rank over a folder

`hotato observe percentiles DIR` reports p50 / p90 / p99 of each per-hop
latency (STT, LLM, tool, TTS, transport) and of end-to-end latency over every
readable voice trace in `DIR`, by the nearest-rank method
(`hotato._stats.nearest_rank`): with the measured values sorted ascending and
n of them, `rank = ceil(q * n)` and the percentile is the value at that rank.
Every percentile is therefore an observed measurement, re-derivable by hand.

A trace that did not capture a hop is **excluded** from that hop's
percentiles, and the excluded count is shown, so an uncaptured hop is never
counted as 0. `--format json` and `--html PATH` render the same numbers as a
machine envelope and a self-contained panel.

## report: one self-contained HTML page

`hotato observe report DIR --out observe.html` writes one self-contained HTML
page (inline CSS and SVG, no external request) summarizing trace and span
counts, per-hop latency and its percentiles, per-model token totals and an
estimated USD line (with `--prices`), and links to the slowest traces. It
opens offline and embeds every value it shows.

## Determinism

The same inputs render byte-identical text, JSON, and HTML. No artifact
embeds a wall clock. `hotato observe` reads files and writes files; it holds
no state and reaches nowhere.
