# Interrupt mid tool-call: the duplicate-side-effect fixture bundle

A caller interrupts while a tool call is in flight. The in-flight result gets
cancelled, discarded, or orphaned, and the side effect double-fires (two
reservations, two deposits, two database rows) or is discarded while the
backend committed anyway. This class shows up on independent stacks:

- [livekit/agents#3702](https://github.com/livekit/agents/issues/3702): an
  interrupted tool result leads to duplicate reservations and database records.
- [pipecat-ai/pipecat#4936](https://github.com/pipecat-ai/pipecat/issues/4936):
  a timed-out tool continues unresolved, its result discarded while the side
  effect can still land, so a retry can double-fire.

It lands on hotato's say-do wedge: what the agent **said** happened versus what
the **trace** and the **system of record** hold. The check measures timing and
say-do, not intent. Each variant is a self-contained directory you run
verbatim: a transcript, an ingested `voice_trace.v1` trace, a `mock`-adapter
state sandbox, and a `hotato.conversation-test`. All three share one scenario
(booking a four-top under "Rivera" with a twenty dollar deposit) so the only
thing that moves between them is the failure.

## The three variants

| Variant | Trace | System of record | Verdict | Caught by |
| --- | --- | --- | --- | --- |
| `clean-cancel` | one `book_table` span; barge-in fires `tts_cancel_requested` mid-call | one hold, deposit 20 | **PASS** (exit 0) | every assertion |
| `double-fire` | two `book_table` spans (`tool_timeout` -> `tool_retry`), same idempotency key | two holds, deposit 40 | **FAIL** (exit 1) | `booked-exactly-once` (tool_call count) **and** `reservation-committed-once` (state) |
| `zombie` | one `book_table` span that `tool_timeout`s unresolved; agent says it failed | one hold, deposit 20 | **FAIL** (exit 1) | `said-failed-but-record-landed` (state) |

- **`clean-cancel`.** The barge-in lands while the `book_table` span is open:
  `tts_cancel_requested` fires, exactly one `tool_call` span carries the
  logical operation, and the sandbox holds exactly one committed row. The
  `booked-exactly-once` count bound and the `reservation-committed-once` state
  check both pass. This is the reference the two defects are measured against.
- **`double-fire`.** The first attempt times out on the client, the retry
  re-fires the same idempotency key, and the backend commits twice. The trace
  carries two `book_table` spans, so `booked-exactly-once` (`count: {max: 1}`)
  fails on the span evidence; the sandbox carries a doubled deposit and two
  holds, so `reservation-committed-once` fails on the record. Two authorities,
  two grounded reasons -- the agent's "you're all set, one table" counts for
  neither.
- **`zombie`.** The tool span times out unresolved, the agent tells the caller
  the booking did not complete and nothing was charged, and the backend
  committed anyway. The transcript-only `agent-reported-failure` check passes
  (the agent did say it) -- and `said-failed-but-record-landed` reads the
  system of record and fails: said it failed, the record shows an active hold.

## Run it

```bash
cd examples/interrupted-tool-call

# clean-cancel -> exit 0, every assertion PASS
hotato test run clean-cancel/test.json --agent reservation-agent-v1 \
    --trace clean-cancel/voice_trace.jsonl \
    --transcript clean-cancel/transcript.json \
    --state clean-cancel/sandbox.json

# double-fire -> exit 1, tool_call count + state both FAIL
hotato test run double-fire/test.json --agent reservation-agent-v1 \
    --trace double-fire/voice_trace.jsonl \
    --transcript double-fire/transcript.json \
    --state double-fire/sandbox.json

# zombie -> exit 1, state FAIL (said failed, record landed)
hotato test run zombie/test.json --agent reservation-agent-v1 \
    --trace zombie/voice_trace.jsonl \
    --transcript zombie/transcript.json \
    --state zombie/sandbox.json
```

Everything is a committed file and the replay is byte-identical: no random, no
wall clock, no network.
`tests/test_examples_interrupted_tool_call.py` drives all three and pins the
exit codes and the named assertions.

## Make it your own

The pair of defects is a template for any stack. Swap in your own trace export
(`hotato trace ingest`, [`docs/TRACE.md`](../../docs/TRACE.md)) and your own
state adapter -- point `--state` at your REST or SQL system of record
([`docs/STATE-ADAPTERS.md`](../../docs/STATE-ADAPTERS.md)) instead of the mock
sandbox -- and keep the assertions: a `count: {max: 1}` bound on the
side-effecting tool and a `state` check that the record holds exactly what the
agent claimed.

## See also

- [`docs/ASSERTIONS.md`](../../docs/ASSERTIONS.md): the `tool_call`, `count`,
  `phrase`, and `state` kinds.
- [`docs/STATE-ADAPTERS.md`](../../docs/STATE-ADAPTERS.md): grounding a `state`
  assertion in a system of record.
- [`docs/CONVERSATION-TEST.md`](../../docs/CONVERSATION-TEST.md): `hotato test
  run` end to end.
- [`examples/reference-agent`](../reference-agent): the offline suite this
  bundle follows.
