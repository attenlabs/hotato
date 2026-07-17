# Assertions (`assert.v1`): deterministic, typed, each scored on its own lane

`hotato.assert_` checks a call's transcript, ingested trace, and timing
against a fixed set of typed assertions, each kind scored on its own lane by
construction. Every kind is a regex, checksum, or span/dict lookup --
deterministic end to end, no model call in the loop.

```python
from hotato import assert_ as A

ctx = A.build_context(
    transcript_path="transcript.json",   # or transcript=[...] turns already in hand
    trace_path="voice_trace.jsonl",      # or spans=[...] already in hand
    timing=envelope,                     # optional: a run's envelope.v1 events
)
env = A.run_assertions_from_file("assertions.yaml", ctx)
print(env["exit_code"])   # 0 pass, 1 fail
```

Python API: [`docs/API.md`](API.md) covers the scoring envelope this
complements; the schema is `src/hotato/schema/assert.v1.json`.

## The core deterministic kinds

Every kind is deterministic, so every result carries `deterministic: true`,
including `INCONCLUSIVE`. The five below are the original core; the full
vocabulary is under [the whole `kind` vocabulary](#the-whole-kind-vocabulary).

| Kind | Checks | Reads |
| --- | --- | --- |
| `phrase` | a regex is present (or, in `absent` mode, never present), with an optional `role` filter and `position` (`first`/`last`/`any`) | transcript text |
| `pii` | deterministic detectors (`ssn`, `card_luhn` with full Luhn validation, `email`, `phone`) find nothing, `mode: must_not_leak` | transcript text |
| `policy` | a named, versioned, offline rule pack's banned-language and required-disclosure rules | transcript text |
| `tool_call` | a tool was (or was not) called, with an optional argument subset, a count bound, a required order across tools, or a "never before" ordering constraint | ingested `voice_trace.v1` spans only |
| `outcome` | task success as `all_of`/`any_of` a list of the sub-predicates above (`tool_called`, `phrase`, `field_present`), reported as a `met`/`of` fraction | whichever context each sub-predicate needs |

`tool_call` checks only the ingested trace (`hotato trace ingest`,
[`docs/TRACE.md`](TRACE.md)) -- that's the evidence a tool ran; an agent's own
words claiming it ran don't count. `pii` surfaces only a `[REDACTED]`
transcript artifact plus hit metadata (detector name, turn index, role) --
never the matched text.

### The whole `kind` vocabulary

The full `assert.v1` `kind` vocabulary is **18 deterministic kinds**, all
`deterministic: true`, all model-free. Beyond the five core kinds:
`tool_result` and `tool_error` (a tool's returned value or raised error, from
the trace), `http_result` (a recorded HTTP exchange span's method, URL,
status, and response subset, from the trace), `state` and `state_change` (a
state adapter's snapshot or
transition -- see [STATE-ADAPTERS.md](STATE-ADAPTERS.md)), `handoff`, `dtmf`,
`termination`, `latency`, `timing_contract`, `entity_accuracy`, `sequence`, and
`count`. Two more, `human_rubric` and `judge_rubric`, belong to the SEPARATE
model-judged rubric lane ([RUBRIC.md](RUBRIC.md)); inside a raw `assert.v1`
document they resolve to a deterministic `INCONCLUSIVE`, so no model runs here
and the guarantee holds.

### `http_result`: a recorded HTTP exchange, checked from the trace

`http_result` reads `http_exchange` spans from the ingested
`hotato.voice_trace.v1` trace -- a recorded request/response report carrying
`method`, `url`, `status_code`, and `response`. Evaluation is a pure span
lookup: hotato never performs the request, so a rerun is deterministic and
offline, and it shares the Authority-1 wall with `tool_result` /
`tool_error` -- an agent's spoken claim about a request can never satisfy it.

```yaml
version: 1
assertions:
  - id: refund-posted
    kind: http_result
    method: POST                      # matched case-insensitively
    url_matches: "/v1/refunds$"       # a regex searched against the span's url
    status: 201                       # one status, or a list: [200, 201]
    response_subset: {status: ok}     # optional: fields the response must carry
```

The span's `method` and `url` select the exchange; `status` and
`response_subset` then judge it. `PASS` carries the grounding `span_ids`;
`FAIL` distinguishes "no exchange matched the method and URL" from "the
exchange matched but its status/response did not"; a context with no trace at
all reports `INCONCLUSIVE`, never a guess. A malformed assertion (missing
`method`, an invalid `url_matches` regex, a `status` outside 100-599) is a
usage error caught up front, before any assertion runs.

## Context: transcript, trace, timing

`build_context` assembles the three inputs an assertion run needs, each built
from hotato's existing primitives:

- **transcript**: `hotato.transcribe` (the opt-in `[transcribe]` extra,
  faster-whisper) produces one, or pass `--transcript FILE` / `transcript_path=`
  with a JSON file -- a plain array of `{role, text, start, end}` turns, or
  the `{"segments": [...]}` shape `hotato.transcribe` and the MCP surface
  write.
- **trace**: `hotato trace ingest --otel FILE --out voice_trace.jsonl`
  ([`docs/TRACE.md`](TRACE.md), [`docs/OTEL.md`](OTEL.md)) produces the
  `hotato.voice_trace.v1` spans `tool_call` reads (`name`, `arguments`, and --
  when the source trace carries them -- `result`/`error`) and `http_result`
  reads (`method`, `url`, `status_code`, `response`).
- **timing**: a scoring run's own envelope (`hotato run --format json`,
  [`docs/API.md`](API.md)) passed straight through as read-only context for
  `outcome`'s `field_present` sub-predicate. Nothing here recomputes it.

Context you never supply stays `None`, distinct from a supplied `[]` or `{}`
that happens to be empty. An assertion whose required input is absent reports
`INCONCLUSIVE`. `tool_call` with `spans=[]` (a trace was ingested with zero
spans) is a `FAIL`, distinct from `tool_call` with no trace at all, which
reports `INCONCLUSIVE`.

## `assertions.yaml`

A small, dependency-free YAML subset (block mappings/sequences, flow
`[...]`/`{...}`, quoted or bare scalars, `#` comments) -- or valid JSON,
accepted directly. Hotato parses this subset itself, so the core stays zero
third-party dependency either way.

```yaml
version: 1
assertions:
  - id: refund-confirmed
    kind: outcome
    all_of: [{tool_called: issue_refund}, {phrase: "confirmation number", role: agent}]
  - id: tool-order
    kind: tool_call
    require_order: [verify_identity, lookup_account, issue_refund]
    never_before: {tool: issue_refund, until: verify_identity}
  - id: disclosure
    kind: phrase
    regex: "recorded for quality"
    role: agent
    position: first
  - id: no-ssn-leak
    kind: pii
    detectors: [ssn, card_luhn]
    mode: must_not_leak
```

Every assertion needs a unique `id` and a recognized `kind`. Kind-specific
fields are validated (bad regex, unknown detector, missing required field,
unsupported `version`) up front -- a malformed file is caught whole, before
partial results exist.

## The deterministic/judge split

This is the entire point of the module: structural, not a convention someone
can quietly break.

- Every result carries `kind` and `deterministic: true` -- true on every
  deterministic kind and every status, including `INCONCLUSIVE`, itself a
  deterministic read of missing required input.
- The envelope's `summary` **splits** `deterministic` (`{pass, fail,
  inconclusive}`) from `judge` (`{pass, fail}`), each in its own count -- the
  schema (`src/hotato/schema/assert.v1.json`) enforces this with
  `"overall_score": false` and a `not: {required: [overall_score]}` on the
  summary object.
- `judge` -- an LLM-scored rubric kind -- stays structurally quarantined
  from the deterministic count, so a model-scored result can never blend
  in. `summary.judge` reports `{"pass": 0, "fail": 0}`; `summary.note`
  states how many judge-scored assertions ran.
- Same inputs, same file, same result, every time: `run_assertions` is
  byte-stable across repeated calls on identical input -- no wall-clock
  timestamp or random id in the mix.

The report (below) renders this as two visually separate shelves, not one
number -- visible on the page, not just in the JSON.

## The report: two shelves, each counted on its own

`hotato.report.build_report_html` / `build_report_md` accept an optional
`assertions=` parameter: an already-evaluated `assert.v1` envelope (build one
with `run_assertions` / `run_assertions_from_file` / `run_assertions_from_yaml`
above). Like `base` (a previous run envelope) and `transcript` (an
already-produced ASR artifact), the report purely renders whatever result
it's handed.

```python
from hotato import assert_ as A, report

env = A.run_assertions_from_file("assertions.yaml", ctx)
html, _ = report.build_report_html(suite="barge-in", assertions=env)
```

When present, it adds one "Assertions" section:

- **The headline.** Two counts side by side, each scored on its own:
  `N deterministic pass / M fail  K judge-scored (advisory)`.
- **Deterministic** (audio / timing / transcript / trace derived): one
  PER-DIMENSION TYPED card per result -- a kind tag, the `deterministic` flag,
  the PASS/FAIL/INCONCLUSIVE chip, and that result's kind-specific fields (a
  `pii` card's hit detail and redacted transcript, a `policy` card's matched
  rules and pack name, a `tool_call` card's grounding span ids, an `outcome`
  card's met/of fraction).
- **Model-assisted (advisory, quarantined)**: stays empty here by design,
  with a note pointing at the model-judged rubric lane
  ([RUBRIC.md](RUBRIC.md)) where that scoring runs.

`assertions=None` (the default) is byte-identical to a report built before this
parameter existed.

## `inconclusive_policy`: making missing input gate CI

By default an `INCONCLUSIVE` result -- a check whose required input was
absent -- leaves the exit code unaffected, the right default for an
exploratory run. `inconclusive_policy` lets a suite gate on that instead, so a
transcript or trace that never arrived fails loudly instead of leaving the
suite silently green:

| value | how `INCONCLUSIVE` gates | `exit_code` |
| --- | --- | --- |
| `report` (default) | reports missing input and leaves the gate unchanged | `1` if any `FAIL`, else `0` |
| `fail` | gates exactly like a `FAIL` | `1` if any `FAIL` **or** `INCONCLUSIVE`, else `0` |
| `refuse` | refuses to return a verdict at all | `2` if any `INCONCLUSIVE`, else `1` if any `FAIL`, else `0` |

**`refuse` precedence.** Under `refuse`, an `INCONCLUSIVE` result exits `2`
*even if another assertion also `FAIL`ed* -- the exit-2 refusal takes precedence
over the FAIL. A run that cannot fully see its inputs withholds its verdict.

The **default is `report`**, so a suite that sets nothing gates exactly as
before this field existed -- fully backward-compatible. **CI and compliance
suites should set `fail` or `refuse`**, so a missing transcript or trace fails
loudly.

Set it as an optional top-level key:

```yaml
version: 1
inconclusive_policy: fail   # or: refuse | report (default)
assertions:
  - id: disclosure
    kind: phrase
    regex: "recorded for quality"
    role: agent
```

or override the document's key from the CLI (the flag wins):

```bash
hotato assert run --assertions assertions.yaml --transcript call.json \
    --inconclusive-policy refuse
```

or from Python (an explicit argument overrides the document's key; absent both,
`report` applies):

```python
env = A.run_assertions_from_file("assertions.yaml", ctx, inconclusive_policy="fail")
```

A bad value (anything but `report`/`fail`/`refuse`) -- in the document or
passed explicitly -- is a usage error (`ValueError`, exit `2`), raised during
validation before any assertion runs. The envelope always carries the applied
`inconclusive_policy`, stated with the counts in `summary.note`.

## Mapping to a CI gate

Same exit-code convention as every hotato command:

| Exit | Meaning |
| ---: | --- |
| `0` | every assertion passed (under `report`, an `INCONCLUSIVE` reports missing input and leaves the exit at `0`; under `fail` it gates like `FAIL`; under `refuse` it exits `2`) |
| `1` | at least one deterministic status is `FAIL` (or, under `fail`, `INCONCLUSIVE`) |
| `2` | a refusal under `refuse` (an `INCONCLUSIVE`, taking precedence over a `FAIL`), or a malformed file / bad input, raised before any assertion runs |

```bash
python3 - <<'PY'
from hotato import assert_ as A
import sys

ctx = A.build_context(transcript_path="transcript.json", trace_path="voice_trace.jsonl")
env = A.run_assertions_from_file("assertions.yaml", ctx)
print(env["summary"]["note"])
sys.exit(env["exit_code"])
PY
```

A gate on `assert.v1` and a gate on the timing scorer's `exit_code`
(`hotato run` / `hotato verify`, [`docs/CI.md`](CI.md)) are two different,
composable guarantees: one gates turn-taking timing, the other gates
transcript/trace content. Run both; neither exit code substitutes for the
other's.
