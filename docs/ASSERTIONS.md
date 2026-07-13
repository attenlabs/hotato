# Assertions (`assert.v1`): deterministic, typed, each scored on its own lane

`hotato.assert_` evaluates a small, fixed set of assertions against a call's
transcript, ingested trace, and timing -- each kind scored on its own lane, by
construction. Every kind here is regex, checksum, or span/dict lookup. None of
them is a model call.

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

Every kind is deterministic, which is why every result carries
`deterministic: true` -- including a result whose status is `INCONCLUSIVE`.
The five below are the original core; the full vocabulary is listed under
[the whole `kind` vocabulary](#the-whole-kind-vocabulary).

| kind | checks | reads |
| --- | --- | --- |
| `phrase` | a regex is present (or, in `absent` mode, never present), with an optional `role` filter and `position` (`first`/`last`/`any`) | transcript text |
| `pii` | deterministic detectors (`ssn`, `card_luhn` with full Luhn validation, `email`, `phone`) find nothing, `mode: must_not_leak` | transcript text |
| `policy` | a named, versioned, offline rule pack's banned-language and required-disclosure rules | transcript text |
| `tool_call` | a tool was (or was not) called, with an optional argument subset, a count bound, a required order across tools, or a "never before" ordering constraint | ingested `voice_trace.v1` spans only |
| `outcome` | task success as `all_of`/`any_of` a list of the sub-predicates above (`tool_called`, `phrase`, `field_present`), reported as a `met`/`of` fraction | whichever context each sub-predicate needs |

`tool_call` checks the ingested trace (`hotato trace ingest`, `docs/TRACE.md`)
alone -- that's the evidence a tool ran; an agent's own words claiming it ran
don't move the needle. `pii` surfaces only a `[REDACTED]` transcript artifact
plus hit metadata (detector name, transcript turn index, role) -- never the
matched text itself.

### The whole `kind` vocabulary

The full
`assert.v1` `kind` vocabulary is **17 deterministic kinds** -- all
`deterministic: true`, all model-free. Alongside the five core kinds:
`tool_result` and `tool_error` (a tool's returned value or raised error, read
from the trace), `state` and `state_change` (a state adapter's snapshot or
transition -- see [STATE-ADAPTERS.md](STATE-ADAPTERS.md)), `handoff`, `dtmf`,
`termination`, `latency`, `timing_contract`, `entity_accuracy`, `sequence`,
and `count`. Two further NAMED kinds, `human_rubric` and `judge_rubric`, belong
to the SEPARATE model-judged rubric lane ([RUBRIC.md](RUBRIC.md)); inside a raw
`assert.v1` document they resolve to a deterministic `INCONCLUSIVE` that points
at that lane, so no model runs here and the deterministic guarantee holds.

## Context: transcript, trace, timing

`build_context` assembles the three inputs an assertion run is evaluated
against, using hotato's existing primitives to produce each one:

- **transcript**: `hotato.transcribe` (the opt-in `[transcribe]` extra,
  faster-whisper) produces one, or pass `--transcript FILE` / `transcript_path=`
  with a JSON file you already have -- a plain array of `{role, text, start,
  end}` turns, or the `{"segments": [...]}` shape `hotato.transcribe` and the
  MCP surface already write. Either way, `assert` works out of the box.
- **trace**: `hotato trace ingest --otel FILE --out voice_trace.jsonl`
  (`docs/TRACE.md`, `docs/OTEL.md`) produces the `hotato.voice_trace.v1` spans
  `tool_call` reads (`name`, `arguments`, and -- when the source trace carries
  them -- `result`/`error`).
- **timing**: a scoring run's own envelope (`hotato run --format json`,
  `docs/API.md`) passed straight through as read-only context for `outcome`'s
  `field_present` sub-predicate. Nothing here recomputes it.

A piece of context that was never supplied stays `None` -- distinct from `[]`
or `{}`, a value that WAS supplied and happens to be empty. An assertion whose
required input is simply absent reports `INCONCLUSIVE`. `tool_call` with
`spans=[]` (a trace WAS ingested, it just has zero spans) is a `FAIL` --
distinct from `tool_call` with no trace at all, which is `INCONCLUSIVE`.

## `assertions.yaml`

A small, dependency-free YAML subset (block mappings/sequences, flow
`[...]`/`{...}`, quoted or bare scalars, `#` comments) -- or a document that is
already valid JSON, accepted directly. Hotato parses this subset itself, so
the core stays zero third-party dependency either way.

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

Every assertion needs a unique `id` and a recognized `kind` (the core table
above, or any of the additional deterministic kinds listed with it); the
kind-specific fields are validated (bad regex, unknown detector, missing
required field, an unsupported `version`) up front, before anything is
evaluated -- a malformed file is caught whole, before partial results exist.

## The deterministic/judge split

This is the entire point of the module, and it is structural, not a
convention someone can quietly break:

- Every result carries `kind` and `deterministic: true` -- true on every
  deterministic kind and every status, including `INCONCLUSIVE`, which is
  itself a deterministic read of missing required input.
- The envelope's `summary` **splits** `deterministic` (`{pass, fail,
  inconclusive}`) from `judge` (`{pass, fail}`), each kept in its own count --
  the schema (`src/hotato/schema/assert.v1.json`) enforces this with
  `"overall_score": false` and a `not: {required: [overall_score]}` on the
  summary object.
- `judge` -- an LLM-scored rubric kind -- is a separate, quarantined
  capability reserved for a future release. `summary.judge` is always
  `{"pass": 0, "fail": 0}`, and `summary.note` states plainly how many
  judge-scored assertions ran (zero, in this build).
- Same inputs, same assertions file, same result, every time: `run_assertions`
  is byte-stable across repeated calls on identical input -- every field in
  the envelope is reproducible, with no wall-clock timestamp or random id in
  the mix.

The report (below) renders this as two visually separate shelves
instead of one number, so it is visible on the page, not just in the JSON.

## The report: two shelves, each counted on its own

`hotato.report.build_report_html` / `build_report_md` accept an optional
`assertions=` parameter: an already-evaluated `assert.v1` envelope (build one
with `run_assertions` / `run_assertions_from_file` / `run_assertions_from_yaml`
above). Exactly like `base` (a previous run envelope) and `transcript` (an
already-produced ASR artifact), the report purely renders an assertion result
that was handed to it.

```python
from hotato import assert_ as A, report

env = A.run_assertions_from_file("assertions.yaml", ctx)
html, _ = report.build_report_html(suite="barge-in", assertions=env)
```

When present, it adds one "Assertions" section with:

- **The headline.** Two counts side by side, each scored on its own:
  `N deterministic pass / M fail  K judge-scored (advisory)`.
- **Deterministic** (audio / timing / transcript / trace derived): one
  PER-DIMENSION TYPED card per result -- a kind tag, the `deterministic` flag
  stated plainly, the PASS/FAIL/INCONCLUSIVE chip, and whatever kind-specific
  fields that particular result carries (a `pii` card's hit detail
  and redacted transcript, a `policy` card's matched rules and pack name, a
  `tool_call` card's grounding span ids, an `outcome` card's met/of fraction).
- **Model-assisted (advisory, quarantined)**: reserved for a future release;
  empty in this build, with a note explaining why.

`assertions=None` (the default) is byte-identical to a report built before
this parameter existed.

## `inconclusive_policy`: making missing input gate CI

By default an `INCONCLUSIVE` result -- a check whose required input was simply
absent -- leaves the run's exit code unaffected. That is the default for an
exploratory run, but it means a suite whose transcript or trace never arrived
stays silently green. `inconclusive_policy` lets a suite opt into gating on
that:

| value | how `INCONCLUSIVE` gates | `exit_code` |
| --- | --- | --- |
| `report` (default) | reports missing input and leaves the gate unchanged | `1` if any `FAIL`, else `0` |
| `fail` | gates exactly like a `FAIL` | `1` if any `FAIL` **or** `INCONCLUSIVE`, else `0` |
| `refuse` | refuses to return a verdict at all | `2` if any `INCONCLUSIVE`, else `1` if any `FAIL`, else `0` |

**`refuse` precedence.** Under `refuse`, an `INCONCLUSIVE` result exits `2`
*even if another assertion also `FAIL`ed* -- the exit-2 refusal, withholding a
verdict when required input is missing, takes precedence over the FAIL. A run
that cannot fully see its inputs withholds its verdict.

The **default is `report`**, so a suite that sets nothing gates exactly as it
did before this field existed -- fully backward-compatible. **CI and
compliance suites should set `fail` or `refuse`**, so a missing transcript or
trace fails loudly.

Set it as an optional top-level key in the assertions document:

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

or from Python (an explicit argument overrides the document's key; absent
both, the default `report` applies):

```python
env = A.run_assertions_from_file("assertions.yaml", ctx, inconclusive_policy="fail")
```

A bad value (anything but `report`/`fail`/`refuse`), whether in the document
or passed explicitly, is a usage error (`ValueError`, exit `2`) raised during
validation, before any assertion is evaluated. The envelope always carries the
`inconclusive_policy` applied and states it, with the counts, in
`summary.note`.

## Mapping to a CI gate

Same exit-code convention as every other hotato command: `0` every assertion
passed (under the default `report` policy an `INCONCLUSIVE` result reports
missing input and leaves the exit code at `0`; under `fail` it gates like a
`FAIL`; under `refuse` it exits `2`), `1` at least one assertion's
deterministic status is `FAIL` (or, under `fail`, `INCONCLUSIVE`), `2` a
refusal under `refuse` (an `INCONCLUSIVE` result, taking precedence over a
`FAIL`) or a malformed assertions file / bad input, raised before any
assertion is evaluated.

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

A gate on `assert.v1` and a gate on the timing scorer's own `exit_code`
(`hotato run` / `hotato verify`, `docs/CI.md`) are two different, composable
guarantees: one gates turn-taking timing, the other gates transcript/trace
content. Run both; neither one's exit code substitutes for the other's.
