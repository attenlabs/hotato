# Counterexamples: reduce one scripted failure to a verified repro

`hotato counterexample` takes one failing deterministic scripted scenario,
deletes inputs that are unnecessary to that exact failure, and writes a local
`.hotato-repro` directory.

```text
scenario + conversation test + target assertion
                    |
                    v
       hotato counterexample compile
                    |
                    v
  private runnable capsule + deletion certificate
```

Every accepted deletion is evaluated through Hotato's existing scripted
simulator and deterministic assertion engine. A candidate is retained only
when the same assertion fails through the same typed witness and failure
fingerprint.

The v1 boundary is narrow:

- input is one local `hotato.scenario` plus one local
  `hotato.conversation-test`;
- the target is one assertion in `assertions.deterministic`;
- the scripted simulator supplies the transcript, trace, mock tools, and mock
  state;
- transformations only delete units named by `hotato.reducers.v1`;
- model-judged targets, provider sessions, captured recordings, external
  timing bundles, and custom policy-pack paths are refused.

The compiler does not invoke a voice agent, provider, model, TTS service, STT
service, command oracle, or network endpoint. Its result describes the
scripted fixture under the recorded Hotato evaluator source identity. It does
not establish the behavior of a deployed agent.

## Run the example

The repository example starts with three caller turns, two mock tools, and two
state records. The selected assertion fails because order `A-1` remains
`pending` instead of `posted`.

```bash
sh examples/counterexample/run.sh
```

The script compiles a private capsule in a new temporary directory, verifies
the proof, reproduces the reduced fixture, checks predicate semantics, and
exports a non-runnable share-safe projection. It leaves both directories in
place and prints their paths.

Run the steps directly:

```bash
hotato counterexample compile \
  --scenario examples/counterexample/refund-not-posted.scenario.json \
  --test examples/counterexample/refund-not-posted.test.json \
  --target refund-posted \
  --workspace examples/counterexample \
  --out /tmp/refund-not-posted.hotato-repro

hotato counterexample verify /tmp/refund-not-posted.hotato-repro
hotato counterexample reproduce /tmp/refund-not-posted.hotato-repro
```

`--out` must name a path that does not exist. Compilation builds a sibling
temporary directory and promotes it with an operating-system no-replace
primitive. Linux, macOS, and Windows have explicit implementations; an
unsupported platform refuses instead of falling back to an overwriting move.

## Source and target selection

### One base scenario

The compiler evaluates the scenario document passed to `--scenario` directly.
It does not expand `variation_matrix` and does not select a matrix row.
`capsule.json` records:

```json
{
  "scenario_selection": {
    "mode": "base-scenario",
    "variation_matrix_applied": false
  }
}
```

`--seed` selects the scripted replay seed. Its default is `scenario.seed`, or
`0` when the scenario has no seed. A seed does not select a variation-matrix
combination.

To minimize one generated variation, first materialize that variation as its
own complete scenario document, then pass the concrete file to `compile`.
Leaving a matrix in the source only preserves its bytes in source provenance;
the reducer can delete the matrix when it has no effect on the base-scenario
failure.

### One deterministic assertion

`--target` must match exactly one assertion ID in
`assertions.deterministic`. Compilation first confirms twice that the selected
assertion fails with identical result, content, and trace identities. It
refuses a passing, inconclusive, advisory, missing, duplicated, or unsupported
target.

The emitted `input/conversation-test.json` is a canonical projection containing
only the selected deterministic assertion. Other source assertions and the
rubric lane are outside the preservation oracle. Their removal is normalization
and is not evidence that those checks passed.

### Workspace boundary

`--workspace` is the directory both input files must resolve within. Input
files and path components must be regular local paths without symlink escape.
When omitted, the workspace is the common parent of the scenario and test
files. Set it explicitly in automation so the filesystem boundary is visible
in review.

## What the compiler deletes

`hotato.reducers.v1` is a closed, versioned, deletion-only algebra over the
scripted scenario. It can attempt to remove:

- variation-matrix, facts, and environment entries;
- caller behavior and interruption declarations;
- caller turns while retaining at least one turn;
- mock tool calls and optional tool fields;
- handoff and termination records;
- mock-state resources, rows, snapshots, and nested leaves;
- optional additive scenario fields.

It cannot add content, replace a scalar, reorder a list, rewrite speech, or
change the target assertion. `certificate.json` records the digest-bound
delete-only transform for every accepted step. `reduction.jsonl` records every
candidate attempt.

## The 1-minimal claim

A capsule with:

```json
{"minimality": {"status": "one_minimal"}}
```

supports this exact statement:

> The fixture is 1-minimal under `hotato.reducers.v1` with the recorded
> observation-scope freezes.

After hierarchical reduction, the compiler repeatedly tries every remaining
single-unit deletion. It reaches `one_minimal` only when none of those
deletions preserves the selected failure fingerprint. `verify` recomputes the
single-unit check instead of trusting `minimality.json`.

This is a local claim over the named deletion algebra. It is not a global
minimum, a root-cause determination, or a semantic-equivalence claim. A
remaining deletion can make the target `PASS`, change the typed failure
witness, invalidate the scenario, or make required evidence unavailable. All
of those outcomes mean that deletion did not preserve the recorded exact
failure identity; the individual outcome remains recorded in
`minimality.json`.

## Budgets

`--budget` limits uncached candidate evaluations during reduction:

```bash
hotato counterexample compile ... --budget 512
```

- default: `512`;
- minimum: `1`;
- hard maximum: `100000`;
- cache hits do not consume the budget;
- the two source and two final qualification executions are reported
  separately and do not consume the candidate budget.

When the budget ends before the final deletion pass completes, the compiler
still emits a capsule if the reduced fixture preserves the exact failure on
both final executions. Its status is `budget_exhausted`, and its permitted
claim is limited to failure preservation. It carries no 1-minimal claim.

Compilation exits `0` for `one_minimal` and `1` for `budget_exhausted`, while
writing a complete capsule in either case. Automation can require a completed
proof from the process status without parsing a nested field. `verify` can
verify the integrity and preserved-failure evidence of a
`budget_exhausted` capsule, but it does not upgrade that capsule to
`one_minimal`.

## Capsule contents

A private runnable capsule contains:

```text
case.hotato-repro/
  capsule.json
  oracle.json
  certificate.json
  minimality.json
  reduction.jsonl
  source/
    scenario.json
    scenario.original
    conversation-test.json
    conversation-test.original
  input/
    scenario.json
    conversation-test.json
  expected/assertion-result.json
  report.md
  report.html
  card.svg
  reproduce.sh
  predicate.sh
  MANIFEST.sha256.json
```

`MANIFEST.sha256.json` binds every other file. The capsule ID also binds the
proof-relevant documents and their digests. Strict verification rejects
missing, changed, extra, symlinked, or unsafe members and independently
re-renders `report.md`, `report.html`, `card.svg`, `reproduce.sh`, and
`predicate.sh` before accepting their bytes.

The Markdown, HTML, and SVG reports summarize identifiers, proof status, and
reduction counts. They are bound, reproducible projections of the capsule;
they do not replace verification.

### Resource and local-filesystem boundary

Verification refuses a capsule before replay when it exceeds any of these
limits:

- 1,024 files;
- 4,096 directories;
- 64 directory levels;
- 64 MiB for one member;
- 256 MiB across all members.

Each source JSON file is limited to 16 MiB and 96 JSON levels. The scripted
scenario also caps caller turns and mock tools at 10,000 each.

The capsule directory must remain unchanged for the duration of `verify`,
`reproduce`, `inspect`, or `export`. Persistent mutation, a root replacement,
and symlink or special-file substitution are detected. A privileged local
process that can swap bytes between individual reads and restore them before
the final manifest pass is outside this version's proof snapshot. Copy an
untrusted capsule into a private, non-writable workspace before verification.

### What the journal proves

`certificate.json` and the `PRESERVED` rows in `reduction.jsonl` carry the
accepted delete-only chain. Strict verification reconstructs those transforms
and reruns the final single-unit deletion inventory for a `one_minimal` claim.
`ABSENT`, `DRIFTED`, and `UNRESOLVED` journal rows are diagnostic reduction
history. Their candidate payloads are not part of the accepted proof chain and
are not independently replayed by `verify`.

## Verify, reproduce, and inspect

These commands answer different questions.

| Command | Question | Evaluator rule | Work performed |
|---|---|---|---|
| `verify` | Is this the same intact proof produced by the recorded evaluator source? | Recorded package version and evaluator source digest must match the installed implementation. | Replays source and final cases twice, reconstructs every accepted deletion, checks the exact failure identity and expected result, re-renders derived artifacts, and recomputes claimed single-unit minimality. |
| `reproduce` | Does the reduced fixture still produce the exact typed failure under the installed evaluator? | Evaluator drift is allowed and reported. | Checks capsule integrity and the delete-only chain, then runs the reduced fixture twice. It does not reassert historical intermediate verdicts or renew the original minimality proof under changed code. |
| `inspect` | What does the intact capsule claim? | No evaluator execution. | Checks the manifest and capsule schema, then prints target, reduction, minimality, preservation, and profile metadata. |

Use `verify` for proof audit and artifact integrity with the recorded Hotato
package version and evaluator source closure. The evaluator digest does not
bind the Python interpreter build, operating system, CPU, or native libraries.
Strict replay still compares the recorded result, content, and trace hashes,
so a runtime difference that changes evaluator behavior fails verification.
Use `reproduce` when evaluating the frozen reduced fixture after Hotato
evaluator or simulator code changes.

`reproduce` returning `0` means the failure was reproduced. That success code
answers the reproduction question; it is the inverse of a conventional
quality gate. Use `predicate` or the generated `predicate.sh` when the desired
shell result is “failure present means bad.”

## Private and share-safe forms

Compilation writes `private-runnable-v1`. It contains complete source and
reduced scenario/test inputs, including scripted speech and mock tool/state
payloads. Treat the directory as sensitive even when its origin is simulated.
The compiler applies restrictive POSIX modes where supported, but permissions
do not establish publication rights or de-identification.

Create a content-free projection only after the private capsule verifies:

```bash
hotato counterexample export /tmp/refund-not-posted.hotato-repro \
  --profile share-safe-v1 \
  --out /tmp/refund-not-posted.share-safe
```

The exported directory contains the capsule projection, Markdown/HTML/SVG
reports, README, and manifest. It omits runnable inputs, scenario and assertion
bodies, transcript or audio content, tool payloads, state values, credentials,
provider identifiers, and absolute paths. SHA-256 values remain correlators.

The projection cannot reproduce the failure. Keep the private capsule when a
reviewer, CI job, or coding agent must execute the fixture.

## Exit codes

### `compile`

| Exit | Meaning |
|---:|---|
| `0` | An atomic private capsule was written and earned `one_minimal`. |
| `1` | An atomic private capsule was written with the exact failure preserved, but the candidate budget ended before one-minimality was proved. |
| `2` | Refused: input, target, workspace, output, replay, or proof safety requirements were not met. No completed destination is left. |

### `verify`

| Exit | Meaning |
|---:|---|
| `0` | Integrity, provenance, source/final replay, accepted deletion chain, exact failure identity, and the stated minimality level verified. |
| `1` | The capsule is intact, but the target failure or claimed single-unit minimality no longer reproduces. |
| `2` | No proof verdict: malformed, tampered, unsafe, evaluator-incompatible, unsupported, or inconclusive capsule. |

### `reproduce`

| Exit | Meaning |
|---:|---|
| `0` | The exact typed failure reproduced twice under the installed evaluator. |
| `1` | The capsule is intact, but that exact failure is absent. |
| `2` | No reproduction verdict: malformed, tampered, unsafe, unsupported, disagreeing, or inconclusive capsule. |

`inspect` and `export` return `0` on success and `2` on refusal.

### `predicate`

`hotato counterexample predicate DIR` and `DIR/predicate.sh` map reproduction
to `git bisect run` semantics:

| Exit | Meaning |
|---:|---|
| `1` | Bad: the exact target failure is present. |
| `0` | Good: the exact target failure is absent. |
| `125` | Skip: the fixture is unusable, unsafe, unsupported, disagreeing, or inconclusive at this revision. |

## CI and evaluator bisect boundaries

### Recorded-evaluator proof audit

Use `verify` when CI installs the Hotato evaluator recorded in the capsule:

```bash
hotato counterexample verify tests/repros/refund-not-posted.hotato-repro
```

This detects capsule mutation and evaluator-source drift. A changed evaluator
digest is a refusal (`2`), because a different implementation cannot silently
validate the historical proof. Interpreter and platform identity are not part
of that digest; the replayed result, content, and trace hashes remain the
behavioral check across runtimes.

### Current-evaluator regression

Use the generated predicate when a reproduced failure should fail the job:

```bash
tests/repros/refund-not-posted.hotato-repro/predicate.sh
```

The predicate executes the frozen reduced mock scenario. It does not call an
application's current voice agent, prompt, tools, provider, or telephony path.
V1 therefore gates Hotato evaluator/simulator behavior over the capsule; it is
not an end-to-end agent release gate.

### `git bisect run`

```bash
git bisect start BAD_REVISION GOOD_REVISION
git bisect run /absolute/path/to/refund-not-posted.hotato-repro/predicate.sh
```

This can locate the revision where Hotato evaluator or scripted-simulator
behavior begins reproducing the recorded failure. The capsule retains its own
source and reduced inputs, so changes to external scenario/test files are not
part of the predicate. A revision that cannot load or evaluate the capsule is
skipped with `125`.

Bisecting agent implementations, hosted models, provider configurations, or
prompt revisions requires a separately defined adapter that executes those
revisions and emits the evidence consumed by an assertion. That adapter is
outside counterexample v1.

## Refusals worth designing around

The compiler fails closed when:

- the target is in the rubric lane or does not fail twice identically;
- required evidence is missing and the target is inconclusive;
- a `timing_contract` depends on an external bundle;
- a latency assertion reads an external timing field rather than a scripted
  tool span;
- a policy assertion uses an external `pack_path`;
- an input escapes the workspace or traverses a symlink;
- the output path already exists;
- source or final replays disagree;
- a capsule member, manifest, certificate, or evaluator provenance is invalid.

Refusal is evidence that the compiler cannot make its promised statement for
that input. It is never converted into a passing or failing agent verdict.

## Machine output

Add `--format json` to `compile`, `verify`, `reproduce`, `inspect`, or `export`.
The JSON carries the command kind, exit code, capsule/failure identities, and
the command-specific proof or reduction fields. `predicate` communicates only
through its process exit.

See the complete runnable fixture in
[`examples/counterexample/`](../examples/counterexample/).
