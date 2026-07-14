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

Every accepted deletion is evaluated through Hotato's own scripted simulator
and deterministic assertion engine. A candidate is retained only when the
same assertion still contains the source-selected failure branch, and
therefore keeps the same failure fingerprint.

The v1 boundary is narrow:

- input is one local `hotato.scenario` plus one local
  `hotato.conversation-test`;
- the target is one assertion in `assertions.deterministic`;
- the scripted simulator supplies the transcript, trace, mock tools, and mock
  state;
- transformations only delete units named by `hotato.reducers.v1`;
- model-judged targets, provider sessions, captured recordings, external
  timing bundles, DTMF targets, and custom policy-pack paths are refused.

The scripted proof lane covers 15 deterministic assertion kinds through 48
closed, schema-coupled failure branches: `phrase`, `pii`, `policy`,
`tool_call`, `outcome`, `tool_result`, `tool_error`, `state`, `state_change`,
`handoff`, `termination`, `latency`, `entity_accuracy`, `sequence`, and
`count`.

The compiler does not invoke a voice agent, provider, model, TTS service, STT
service, command oracle, or network endpoint: its result describes the
scripted fixture under the recorded Hotato evaluator source identity, not
the behavior of a deployed agent.

## Run the example

The repository example: three caller turns, two mock tools, two state
records. The selected assertion fails because order `A-1` remains `pending`
instead of `posted`.

```bash
sh examples/counterexample/run.sh
```

The script compiles a private capsule in a new temporary directory, verifies
the proof, reproduces the reduced fixture, checks predicate semantics, and
exports a non-runnable share-safe projection, leaving both directories in
place and printing their paths.

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
temporary directory and promotes it with a no-replace primitive: Linux,
macOS, and Windows have explicit implementations, and an unsupported
platform refuses rather than falling back to an overwriting move.

## Source and target selection

### One base scenario

The compiler evaluates the scenario document passed to `--scenario` directly,
without expanding `variation_matrix` or selecting a matrix row. `capsule.json`
records:

```json
{
  "scenario_selection": {
    "mode": "base-scenario",
    "variation_matrix_applied": false
  }
}
```

`--seed` selects the scripted replay seed, defaulting to `scenario.seed` or
`0` when the scenario has none. A seed does not select a variation-matrix
combination.

To minimize one generated variation, materialize it as its own complete
scenario document, then pass the concrete file to `compile`. Leaving a matrix
in the source only preserves its bytes in source provenance; the reducer can
delete the matrix when it has no effect on the base-scenario failure.

### One deterministic assertion

`--target` must match exactly one assertion ID in `assertions.deterministic`.
Compilation confirms twice that the selected assertion fails with identical
result, content, and trace identities, and refuses a passing, inconclusive,
advisory, missing, duplicated, or unsupported target.

### Exact failure branch identity

A deterministic `FAIL` can contain more than one reason: the oracle maps them
into a closed set of payload-free failure atoms, sorts and de-duplicates them
canonically, and selects the first as the preservation anchor. The private
capsule records the selected atom and the complete ordered source atom set;
a candidate is `PRESERVED` only when that atom is still present in its own
atom set.

An atom carries only the discriminator needed to keep failure branches apart:
for example, an assertion index, detector, policy rule/type, state field, or
entity key. Example: a wrong tool argument gives `tool-argument-value-mismatch`
(plus its key); delete the value and the branch becomes
`tool-argument-field-missing`; delete the call and it becomes `tool-missing`.
Both are `DRIFTED`, though the assertion still reports `FAIL`. The same
missing-versus-value care extends to other kinds:

| Atom type | Distinguishes |
| --- | --- |
| results / termination | missing vs. wrong value |
| required-order / sequence | present-but-out-of-order vs. absent step |
| latency | declared mock measurement vs. simulator default |

Payload values, transcript text, and broad diagnostic counts are never part
of an atom.

The fingerprint binds the test and assertion identity, assertion bytes,
kind, dimension, deterministic authority, required `FAIL` status, and
selected source atom -- not every diagnostic field in the assertion result,
so irrelevant evidence stays reducible without the preserved failure
collapsing into a different branch.

The emitted `input/conversation-test.json` is a canonical projection
containing only the selected deterministic assertion: other source
assertions and the rubric lane sit outside the preservation oracle, and
their removal is normalization, not evidence those checks passed.

### Workspace boundary

`--workspace` is the directory both input files must resolve within, as
regular local paths without symlink escape. Omitted, it defaults to the
common parent of the scenario and test files; set it explicitly in
automation so the filesystem boundary stays visible in review.

## What the compiler deletes

`hotato.reducers.v1` is a closed, versioned, deletion-only algebra over the
scripted scenario, able to attempt removing:

- variation-matrix, facts, and environment entries;
- caller behavior and interruption declarations;
- caller turns while retaining at least one turn;
- mock tool calls, optional tool fields, and nested argument/result entries;
- handoff and termination records;
- mock-state resources, rows, snapshots, and nested leaves;
- optional additive scenario fields.

It cannot add content, replace a scalar, reorder a list, rewrite speech, or
change the target assertion. `certificate.json` records the digest-bound
delete-only transform for every accepted step; `reduction.jsonl` records
every candidate attempt.

## The 1-minimal claim

A capsule with:

```json
{"minimality": {"status": "one_minimal"}}
```

supports this exact statement:

> The fixture is 1-minimal under `hotato.reducers.v1` with the recorded
> observation-scope freezes.

After hierarchical reduction, the compiler repeatedly tries every remaining
single-unit deletion, reaching `one_minimal` only when none of those
deletions preserves the selected source failure branch. `verify` recomputes
the single-unit check rather than trusting `minimality.json`.

This is a local claim over the named deletion algebra -- not a global minimum,
a root-cause determination, or a semantic-equivalence claim. A remaining
deletion can make the target `PASS`, change the selected failure branch,
invalidate the scenario, or make required evidence unavailable; each such
outcome means that deletion did not preserve the recorded exact failure
identity, and is recorded as such in `minimality.json`.

## Budgets

`--budget` limits uncached candidate evaluations during reduction:

```bash
hotato counterexample compile ... --budget 512
```

| | |
| --- | --- |
| default | `512` |
| minimum | `1` |
| hard maximum | `100000` |

Cache hits do not consume the budget, nor do the two source and two final
qualification executions (reported separately).

When the budget ends before the final deletion pass completes, the compiler
still emits a capsule if the reduced fixture preserves the exact failure on
both final executions: status `budget_exhausted`, claim limited to failure
preservation, no 1-minimal claim.

Compilation exits `0` for `one_minimal` and `1` for `budget_exhausted`,
writing a complete capsule either way; automation can gate on the process
status alone, without parsing a nested field. `verify` can check the
integrity and preserved-failure evidence of a `budget_exhausted` capsule, but
does not upgrade it to `one_minimal`.

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

`MANIFEST.sha256.json` binds every other file; the capsule ID binds the
proof-relevant documents and their digests too. Strict verification rejects
missing, changed, extra, symlinked, or unsafe members, and independently
re-renders `report.md`, `report.html`, `card.svg`, `reproduce.sh`, and
`predicate.sh` before accepting their bytes.

The Markdown, HTML, and SVG reports summarize identifiers, proof status, and
reduction counts -- bound, reproducible projections of the capsule that do
not replace verification.

### Resource and local-filesystem boundary

Verification refuses a capsule before replay when it exceeds any of these
limits:

| Limit | Cap |
| --- | --- |
| files | 1,024 |
| directories | 4,096 |
| directory levels | 64 |
| one member | 64 MiB |
| all members combined | 256 MiB |

Each source JSON file is limited to 16 MiB and 96 JSON levels. The scripted
scenario also caps caller turns and mock tools at 10,000 each.

Counterexample compilation and replay add a narrower proof-lane budget:

| Item | Cap |
| --- | --- |
| selected assertion (canonical JSON) | 256 KiB |
| candidate scenario (canonical JSON) | 2 MiB |
| rendered transcript text (UTF-8) | 256 KiB |
| one deterministic assertion result (canonical JSON) | 2 MiB |
| `hits` / `matched_rules` evidence rows | 10,000 |
| deletion steps per accepted proof chain | 512 |
| deletion operations per step | 10,000 |
| remaining units in a completed minimality proof | 512 |
| proof-lane regex length | 1,024 UTF-8 bytes |

Each proof-lane regex also belongs to a closed, fixed-width replay subset;
groups, alternation, backreferences, and variable quantifiers are refused.
The assertion and regex byte bounds run before the general assertion
validator can invoke Python's regex parser.

The selected structured failure branch is still the preservation identity;
these limits just bound the work needed to establish it. A candidate that
crosses an execution limit is `UNRESOLVED` with `resource_limit_exceeded`,
never treated as preserved or absent. These counterexample compiler/replay
limits do not narrow the regex or result surface of Hotato's deterministic
evaluator outside the proof lane.

The capsule directory must stay unchanged for the duration of `verify`,
`reproduce`, `inspect`, or `export`: persistent mutation, a root
replacement, and symlink or special-file substitution are all detected. A
privileged local process able to swap bytes between reads and restore them
before the final manifest pass sits outside this version's proof snapshot --
copy an untrusted capsule into a private, non-writable workspace before
verification.

### What the journal proves

`certificate.json` and the `PRESERVED` rows in `reduction.jsonl` carry the
accepted delete-only chain; strict verification reconstructs those transforms
and reruns the final single-unit deletion inventory for a `one_minimal`
claim. `ABSENT`, `DRIFTED`, and `UNRESOLVED` journal rows are diagnostic reduction
history only; their candidate payloads sit outside the accepted proof chain,
unreplayed by `verify`.

## Verify, reproduce, and inspect

These commands answer different questions.

| Command | Question | Evaluator rule | Work performed |
|---|---|---|---|
| `verify` | Is this the same intact proof produced by the recorded evaluator source? | Recorded package version and evaluator source digest must match the installed implementation. | Replays source and final cases twice, reconstructs every accepted deletion, checks the selected source failure branch and expected result, re-renders derived artifacts, and recomputes claimed single-unit minimality. |
| `reproduce` | Does the reduced fixture still produce the selected source failure branch under the installed evaluator? | Evaluator drift is allowed and reported. | Checks capsule integrity and the delete-only chain, then runs the reduced fixture twice. It does not reassert historical intermediate verdicts or renew the original minimality proof under changed code. |
| `inspect` | What does the intact capsule claim? | No evaluator execution. | Checks the closed member inventory, source/oracle/artifact bindings, canonical human files, manifest, and capsule schema, then prints target, reduction, minimality, preservation, and profile metadata. |

Use `verify` for proof audit and artifact integrity against the recorded
Hotato package version and evaluator source closure. The evaluator digest
does not bind the Python interpreter build, OS, CPU, or native libraries;
strict replay still compares the recorded result, content, and trace hashes,
so a runtime difference that changes evaluator behavior fails verification.
Use `reproduce` to evaluate the frozen reduced fixture after Hotato
evaluator or simulator code changes.

`reproduce` returning `0` means the failure reproduced -- the inverse of a
conventional quality gate. Use `predicate` or the generated `predicate.sh`
when the desired shell result is “failure present means bad.”

## Private and share-safe forms

Compilation writes `private-runnable-v1`: complete source and reduced
scenario/test inputs, including scripted speech and mock tool/state
payloads. Treat the directory as sensitive even when its origin is simulated.
The compiler applies restrictive POSIX modes where supported, but permissions
do not establish publication rights or de-identification.

Create a payload-free projection only after the private capsule verifies:

```bash
hotato counterexample export /tmp/refund-not-posted.hotato-repro \
  --profile share-safe-v1 \
  --out /tmp/refund-not-posted.share-safe
```

The exported directory has one closed inventory: `capsule.json`, `report.md`,
`report.html`, `card.svg`, `README.md`, and `MANIFEST.sha256.json`. Extra,
missing, renamed, symlinked, or special-file members are refused even when a
manifest declares them. `inspect` re-renders every human-facing file and
requires byte-for-byte equality with its canonical rendering, so rebinding
modified report text into a new manifest does not make the projection valid.

Those canonical renderer bytes are part of the capsule v1 exchange contract.
A compatible implementation must retain the v1 renderer; changing its output
requires a versioned renderer/profile or a capsule format version bump.

The projection omits runnable inputs, scenario and assertion bodies,
transcript or audio content, tool payloads, state values, credentials,
provider identifiers, absolute paths, source-derived reducer paths, and
per-candidate digests: private single-unit rows become an aggregate count by
outcome instead.

The share target and canonical reports expose the selected failure code
(such as `tool-argument-value-mismatch` or `state-field-value-mismatch`), so
the failure category stays readable without a payload. Atom discriminators
(a field, key, rule, detector, or index) stay private.
`failure_atom_digest` binds the complete selected atom without publishing
those values.

`share-safe-v1` is an engineering access boundary, not an anonymity claim:
capsule, source, assertion, evaluator, fingerprint, and failure-atom digests
remain correlators, and low-entropy or externally known source material can
be tested against them. Keep a projection under the same review,
artifact-retention, and disclosure controls as engineering metadata.

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
| `0` | The selected source failure branch reproduced twice under the installed evaluator. |
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

This detects capsule mutation and evaluator-source drift: a changed evaluator
digest is a refusal (`2`), since a different implementation cannot silently
validate the historical proof (what the digest binds is above).

### Current-evaluator regression

Use the generated predicate when a reproduced failure should fail the job:

```bash
tests/repros/refund-not-posted.hotato-repro/predicate.sh
```

The predicate executes the frozen reduced mock scenario; it does not call an
application's current voice agent, prompt, tools, provider, or telephony
path. V1 gates Hotato evaluator/simulator behavior over the capsule, not an
end-to-end agent release gate.

### `git bisect run`

```bash
git bisect start BAD_REVISION GOOD_REVISION
git bisect run /absolute/path/to/refund-not-posted.hotato-repro/predicate.sh
```

This locates the revision where Hotato evaluator or scripted-simulator
behavior begins reproducing the recorded failure. The capsule retains its own
source and reduced inputs, so external scenario/test file changes play no
part in the predicate. A revision that cannot load or evaluate the capsule
is skipped with `125` -- as with `git bisect run` generally, skipped
revisions near the transition can leave a candidate range rather than one
first bad revision.

Bisecting agent implementations, hosted models, provider configurations, or
prompt revisions needs a separately defined adapter, outside counterexample
v1, that runs those revisions and emits the evidence an assertion consumes.

## Refusals worth designing around

The compiler fails closed when:

- the target is in the rubric lane or does not fail twice identically;
- required evidence is missing and the target is inconclusive;
- a `timing_contract` depends on an external bundle;
- a DTMF assertion requests trace evidence the scripted simulator cannot emit;
- an outcome predicate reads an external timing field;
- a latency assertion reads an external timing field or a span type other than
  a scripted `tool_call`;
- a keyed tool argument/result target uses an empty key, or an entity reference
  uses an empty key or a null expected value;
- a policy assertion uses an external `pack_path`;
- an input escapes the workspace or traverses a symlink;
- the output path already exists;
- source or final replays disagree;
- a capsule member, manifest, certificate, or evaluator provenance is invalid.

Refusal is evidence the compiler cannot make its promised statement for that
input; it never becomes a passing or failing agent verdict.

## Machine output

Add `--format json` to `compile`, `verify`, `reproduce`, `inspect`, or `export`.
The JSON carries the command kind, exit code, capsule/failure identities, and
the command-specific proof or reduction fields. `predicate` communicates only
through its process exit.

See the complete runnable fixture in
[`examples/counterexample/`](../examples/counterexample/).
