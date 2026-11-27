# Custom metrics example

Three checks written as plain Python functions in
[`hotato_metrics/voice_checks.py`](hotato_metrics/voice_checks.py), run next
to a built-in `pii` assertion:

| Metric | Checks |
|---|---|
| `greets_with_name` | the agent's first turn names the business |
| `max_agent_turn_words` | no agent turn is longer than `max_words` words |
| `claim_backed_by_tool` | when the agent says "booked", the trace has a `book_appointment` tool call |

```bash
cd examples/custom-metrics
hotato assert metrics
hotato assert run --assertions assertions.yaml \
  --transcript call.transcript.json --trace voice_trace.jsonl   # exit 0
hotato assert run --assertions assertions.yaml \
  --transcript call.transcript.json                             # exit 2: the tool check needs the trace
```

The full guide is [docs/CUSTOM-METRICS.md](../../docs/CUSTOM-METRICS.md).
