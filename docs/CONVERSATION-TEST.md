# Conversation tests (`hotato test run`): one file, one call, a per-dimension scorecard

A **conversation test** (`hotato.conversation-test.v1`) is one file defining
one testable conversation: agent, simulated caller, environment, two
SEPARATE assertion lanes, and an explicit success condition. `hotato test
run` evaluates a supplied call against it and produces a **conversation
artifact** (`hotato.conversation.v1`, evidence bound by sha256) plus a
**per-dimension scorecard**. Success is a boolean
over a small closed vocabulary of named conditions; every dimension counts
on its own, including in `--format json`.

This is the single documented end-to-end conversation-QA workflow:

```
hotato scenario init   ->   hotato test run   ->   hotato conversation verify
   (author a file)          (evaluate a call)        (digest-check the artifact)
```

## 1. Author a conversation test

```bash
hotato scenario init refund-flow --agent support-v3 --out refund.yaml
```

The starter carries two lanes, tagged checks across the five report
dimensions, and a boolean `success`:

```yaml
kind: hotato.conversation-test
version: 1
id: refund-flow
agent: support-v3
caller:
  persona: a customer whose order arrived damaged and wants a refund
  goal: get a refund for order A-1001
  facts: {order_id: A-1001}
assertions:
  # DETERMINISTIC lane: pure, offline, no model. Each result is TAGGED with one
  # of the five report dimensions (a grouping key, never a weight).
  deterministic:
    - id: disclosure-said
      kind: phrase
      regex: "recorded for quality"
      role: agent
      dimension: policy
    - id: refund-tool-called
      kind: tool_call
      name: issue_refund
      dimension: outcome
    - id: lookup-then-refund
      kind: sequence
      steps: [{tool: lookup_order}, {tool: issue_refund}]
      dimension: conversation
    - id: refund-latency
      kind: latency
      tool: issue_refund
      max_ms: 1500
      dimension: speech
  # RUBRIC (model-judged) lane lands in Phase 3. In Phase 1 each is INCONCLUSIVE
  # (no model runs); it never counts toward the deterministic result or exit code.
  rubric:
    - id: was-empathetic
      kind: judge_rubric
      dimension: conversation
# inconclusive_policy: fail  # CI/compliance suites should set fail or refuse
success:
  # Success is the conjunction of these named conditions -- never a score.
  required: [all_deterministic_assertions_pass, no_rubric_failure]
  report_dimensions: [outcome, policy, conversation, speech, reliability]
```

Validate one file or a whole directory (exit 2 on any malformed file):

```bash
hotato scenario validate refund.yaml
hotato scenario validate ./scenarios --format json
```

The closed vocabularies are enforced structurally
(`hotato.conversation_test`): `success.required` is drawn from
`all_deterministic_assertions_pass`, `no_deterministic_fail`,
`no_rubric_failure`, `no_inconclusive`; `dimension` is one of `outcome`,
`policy`, `conversation`, `speech`, `reliability`; an `overall_score` key
anywhere is rejected.

## 2. Evaluate a call

`hotato test run` takes the file plus whatever evidence you have: a scored
recording (`--audio`), an ingested trace (`--trace`, see
[`docs/TRACE.md`](TRACE.md)), a transcript (`--transcript`), a post-call
state sandbox (`--state`, Authority 2). Each supplied piece feeds the
assertions; each absent one leaves the checks that need it `INCONCLUSIVE`.

```bash
hotato test run refund.yaml --agent support-v3 \
    --audio call.wav \
    --trace voice_trace.jsonl \
    --transcript call.transcript.json \
    --out ./conv-artifact --format html
```

Flow: load and validate the file -> build the evaluation context from the
supplied transcript / trace / state / (scored) timing -> evaluate the
DETERMINISTIC lane (rubric quarantined -> `INCONCLUSIVE`) -> evaluate
`success.required` over the results -> bind evidence into a
`hotato.conversation.v1` artifact -> render the unified report +
per-dimension scorecard. Summary printed to stdout:

```
hotato test run: refund-flow (agent support-v3) -- exit_code=0
inconclusive_policy: report
success: PASS  (required: all_deterministic_assertions_pass, no_rubric_failure)
  [ok] all_deterministic_assertions_pass
  [ok] no_rubric_failure
per-dimension (grouped view; never blended):
  outcome       1 pass / 0 fail / 0 inconclusive
  policy        1 pass / 0 fail / 0 inconclusive
  conversation  1 pass / 0 fail / 0 inconclusive
  speech        1 pass / 0 fail / 0 inconclusive
  reliability   0 pass / 0 fail / 0 inconclusive
reliability over 1 repeated run(s) of the deterministic lane on the same supplied real recording; pass^k == pass@1 because the deterministic replay is byte-identical (zero run-to-run variance)
rubric lane: 1 assertion(s) INCONCLUSIVE (quarantined, Phase 3 -- no model ran)
deterministic: 4 pass, 0 fail, 0 inconclusive
judge: 0 pass, 0 fail (no judge/rubric kind is built in this release)
```

`--format`:

| format | stdout | writes into `--out` |
| --- | --- | --- |
| `text` (default) | the per-dimension summary | the conversation artifact |
| `html` / `md` | the summary (+ where files landed) | the artifact **and** `report.{html,md}` (needs `--audio`) |
| `json` | the full machine result | the conversation artifact |

### Exit code

The exit code honors the file's `inconclusive_policy` exactly as
`assert run` does, rising to non-zero when a `success.required` condition
fails:

| `inconclusive_policy` | an `INCONCLUSIVE` (missing-input) result | a `FAIL` |
| --- | --- | --- |
| `report` (default) | does not gate (exit 0) | exit 1 |
| `fail` | gates like a FAIL (exit 1) | exit 1 |
| `refuse` | withholds the verdict (exit 2, precedence) | exit 1 |

A `success.required` failure makes an otherwise-passing run non-zero; a
refuse (exit 2) is never downgraded.

### Reliability and repetitions

`--repetitions N` runs the deterministic lane N times and reports per-run
results, run count, and a reliability aggregate: **pass@1** (single-run
pass rate), **pass@k** (>=1 of k passed), **pass^k** (all k passed), plus a
Wilson 95% CI. Every run scores the same recording, so the deterministic
lane has zero variance and `pass^k == pass@1`. With `N > 1` the aggregate
feeds the report's Reliability dimension (`--format html/md`); with none,
it shows the empty-state ("not measured: no repeated runs in this
report"). pass^k stays its own number, separate from every other
dimension and from `overall_score`.

## 3. Verify the artifact

The conversation artifact directory binds each child by sha256:

```
conv-artifact/
  conversation.json      # the manifest (origin real|simulated + bound digests)
  audio/call.wav         # the recording (single dual-channel form)
  transcript.json
  trace.jsonl
  timing.json            # the scored envelope
  assertions.json        # the evaluated assert.v1 envelope
  report.html            # the unified report + per-dimension scorecard
```

`hotato conversation verify` re-hashes every bound child and REFUSES
(exit 2) on any digest mismatch or missing file:

```bash
hotato conversation verify ./conv-artifact
# conversation refund-flow: VERIFIED
#   verified: assertions, audio, timing, trace, transcript
#   all 5 bound artifact(s) re-hashed to their recorded digest
```

`origin.kind` is `real` by default (a supplied recording is evaluated
as-is) and `simulated` only when the test file carries a `simulator`
block, which must declare its `model_id` / `scenario_id` / `seed`;
synthetic and live origins are never conflated.

## The deterministic/judge split (structural)

* **Each dimension scored on its own.** Success is a boolean conjunction
  of named conditions; the scorecard groups results by dimension, each
  with its own counts.
* **Two separate lanes.** Deterministic checks (regex / checksum / span /
  state lookup, no model) run; the model-judged rubric lane stays
  quarantined until Phase 3: `INCONCLUSIVE`, out of the deterministic
  summary and exit code.
* **Grounded in authority.** `tool_result` / `tool_error` (Authority 1)
  read the ingested trace spans; `state` / `state_change` (Authority 2)
  query a post-call state adapter; the trace and the adapter are the
  evidence a tool ran or a state changed, deterministic and model-free.
* **Missing input is `INCONCLUSIVE`,** never a guessed pass or fail.

## The bundled end-to-end example

`tests/data/conversation/` ships one worked call: a conversation-test file,
a transcript, and a `voice_trace.v1` trace, evaluated against the bundled
recording `01-hard-interruption.example.wav`.
`tests/test_test_run_cli.py`
(`test_bundled_call_one_file_end_to_end_scorecard_and_artifact`) drives it:
one call evaluated for outcome / policy / timing / transcript-facts /
tool-behaviour from one file, producing an artifact that `conversation
verify` passes and a five-dimension scorecard.

## See also

* [`docs/ASSERTIONS.md`](ASSERTIONS.md): deterministic assertion kinds.
* [`docs/TRACE.md`](TRACE.md): ingesting a `voice_trace.v1` trace.
* [`docs/REPORTS.md`](REPORTS.md): the unified report and scorecard.
