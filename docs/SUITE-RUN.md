# `hotato suite run`: execute a suite of conversation-tests

`hotato suite run` loads a `hotato.suite` file (a named set of
conversation-test refs, each scored on its own), resolves each ref relative
to the suite file, executes every test, and emits a per-dimension +
reliability report, also recording the run into the fleet registry so
`hotato serve` can browse it.

A conversation-test is one file defining one testable conversation; see
[CONVERSATION-TEST.md](CONVERSATION-TEST.md). A suite (JSON or YAML) groups
those files under a `suite_id`, a `purpose`, an `inconclusive_policy`, and a
`required_for_release` flag.

## How a test runs

- A test that declares a `scenario` runs **offline** through the
  deterministic scripted-caller simulator: its variation matrix expands
  into concrete runs, each rendered, validated, and scored against the
  test's deterministic lane. Authority-1 tool spans and the Authority-2
  mock-state sandbox come from the scenario's `agent_mock`, fully simulated
  and offline (Authority 2 is grounded state; see
  [STATE-ADAPTERS.md](STATE-ADAPTERS.md)).
- A test with no scenario is evaluated once against an empty context: every
  input-dependent check reports INCONCLUSIVE, never a guessed pass/fail.

## Every dimension scored on its own

The report carries per-dimension counts (`outcome`, `policy`,
`conversation`, `speech`, `reliability`) plus a reliability aggregate:
`pass@1`, `pass@k`, `pass^k`. Per-dimension counts group results across
tests; reliability stands as its own dimension over every valid simulated
run. Each dimension keeps its own count and verdict, exposed as-is,
including in `--format json`; the schemas reject an `overall_score` key.

A `SIMULATOR_INVALID` run flags a broken fixture, bucketed separately and
reported by test id and run id, kept out of every dimension and the
reliability aggregate.

## The suite's policy is authoritative

The suite's `inconclusive_policy` is the effective policy for every test it
names, regardless of a test's own policy. `inconclusive_policy: fail` makes
an INCONCLUSIVE (absent required input) FAIL the gate; `refuse` withholds
the verdict (exit `2`, precedence).

The suite exit code is the **worst** test outcome under that policy
(`refuse` > `fail` > `pass`), raised to at least `1` by any
`SIMULATOR_INVALID` run.

## Run it

```bash
hotato suite run examples/reference-agent/suite.json \
    --agent reference-agent-v1 \
    --release reference-agent-v1 \
    --out ./suite-out
```

`--agent ID` (required) is recorded on every conversation and the release.
`--release ID` names the release runs are recorded under (default
`<agent>@<suite_id>`) -- keep it stable per release for
[`hotato release compare`](RELEASE-COMPARE.md). `--out DIR`
writes the per-test simulated conversation artifacts plus
`suite-report.md`, `suite-report.html`, and `suite-run.json`. `--parallel N`
caps worker threads per scenario's matrix; the worker count never changes
the byte-identical result. `--junit PATH` also writes a JUnit XML report
for a CI test widget (one `<testsuite>` per dimension, one `<testcase>`
per test; a FAILed dimension is a `<failure>` with the measured reason, an
INCONCLUSIVE dimension or a `SIMULATOR_INVALID` run is an `<error>`, never
a silent pass); see [CI.md](CI.md).

Runs are recorded into the fleet registry (`--registry`, default
`~/.hotato/fleet`; `--workspace`/`-w`, default `default`) as Release / Suite
/ Scenario / Run / Conversation / Evaluation rows, so `hotato serve -w
<workspace>` renders them (see [WORKSPACE.md](WORKSPACE.md));
`--no-registry` reports only.

## Worked example: the reference agent

`examples/reference-agent/` ships a full suite: 25 realistic voice-agent
jobs x 5 caller behaviours x 3 audio environments = 375 offline simulated
runs, scored across the outcome / policy / conversation / speech
dimensions. Its `suite.json` has `inconclusive_policy: fail` and
`required_for_release: true`.

One command regenerates the files, runs the 375-run suite offline, and
records a browsable workspace:

```bash
cd examples/reference-agent
make reference
# then browse:
hotato serve --workspace reference --registry ./.workspace
```

The summary printed to stdout (counts here are schematic):

```
hotato suite run: reference-agent-suite (...) -- agent reference-agent-v1, release reference-agent-v1 -- exit_code=0
inconclusive_policy: fail  required_for_release: True
tests: 25  (25 pass, 0 fail, 0 refuse)
runs: 375 (375 valid, 0 SIMULATOR_INVALID -- broken fixtures, never an agent PASS/FAIL), origin=simulated
per-dimension (grouped across tests; never blended):
  outcome       ... pass / ... fail / ... inconclusive
  policy        ... pass / ... fail / ... inconclusive
  conversation  ... pass / ... fail / ... inconclusive
  speech        ... pass / ... fail / ... inconclusive
  reliability   ... pass / ... fail / ... inconclusive
reliability [origin=simulated]: pass@1=... pass@k=... pass^k=... (n=...)
per-test:
  [pass   ] refund-damaged-order            runs=15  ...
  ...
```

Origin is always labelled `simulated`, keeping a simulator's replay
reliability distinct from production reliability.

## Exit codes

- **`0`**: every test in the suite passed under its `inconclusive_policy`
  (no run `SIMULATOR_INVALID`).
- **`1`**: at least one test FAILed (a `success.required` condition
  failed, a deterministic assertion FAILed, or, under
  `inconclusive_policy fail`, an INCONCLUSIVE gated), or a run was
  `SIMULATOR_INVALID`.
- **`2`**: under `inconclusive_policy refuse` a scored INCONCLUSIVE
  withheld the verdict (precedence over a FAIL); or a usage error /
  unusable input: a malformed suite / conversation-test / scenario file, or
  an unresolvable test/scenario ref.

## See also

- [CONVERSATION-TEST.md](CONVERSATION-TEST.md): the conversation-test file each suite ref points at.
- [STATE-ADAPTERS.md](STATE-ADAPTERS.md): Authority 2 state grounding for `state` / `state_change` checks.
- [RELEASE-COMPARE.md](RELEASE-COMPARE.md): diff two releases recorded by `suite run`.
- [WORKSPACE.md](WORKSPACE.md): browse the recorded runs with `hotato serve`.
- [SUITES.md](SUITES.md): the `corpus/suites/` scenario-audio suites for the `hotato run` scoring path (a distinct mechanism).
