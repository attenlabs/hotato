# `hotato suite run`: execute a suite of conversation-tests

`hotato suite run` loads a `hotato.suite` file (a NAMED SET of
conversation-test refs, each scored on its own), resolves each ref relative to the suite
file, executes every test, and emits a per-dimension + reliability report. It
also records the run into the fleet registry so `hotato serve` can browse it.

A conversation-test is one file that defines one testable conversation; see
[CONVERSATION-TEST.md](CONVERSATION-TEST.md). A suite groups those files under a
`suite_id`, a `purpose`, an `inconclusive_policy`, and a
`required_for_release` flag. The suite file is JSON or YAML.

## How a test runs

- A test that declares a `scenario` runs **offline** through the deterministic
  scripted-caller simulator. The scenario's variation matrix expands into
  concrete runs, each rendered, validated, and scored against the test's
  DETERMINISTIC lane. Authority-1 tool spans and the Authority-2 mock-state
  sandbox come from the scenario's `agent_mock`, fully simulated and offline.
  (Authority 2 is grounded state; see [STATE-ADAPTERS.md](STATE-ADAPTERS.md).)
- A test with no scenario is evaluated once against an empty context: every
  input-dependent check is INCONCLUSIVE, never a guessed pass/fail.

## Every dimension scored on its own

The report carries per-dimension counts (`outcome`, `policy`, `conversation`,
`speech`, `reliability`) plus a reliability aggregate -- `pass@1`, `pass@k`,
`pass^k`. Per-dimension counts group results across tests; reliability stands
as its own dimension over every valid simulated run. Each dimension keeps its
own count and its own verdict, exposed as-is, including in `--format json`;
the schemas reject an `overall_score` key.

A `SIMULATOR_INVALID` run flags a broken fixture. It is bucketed separately
and reported by test id and run id, kept out of every dimension and the
reliability aggregate.

## The suite's policy is authoritative

The SUITE's `inconclusive_policy` is the effective policy for every test it
names, keeping a required CI/compliance suite's gate consistent regardless of
a test's own policy. So a suite with `inconclusive_policy: fail` makes an
INCONCLUSIVE (absent required input) FAIL the gate; `refuse` withholds the
verdict (exit `2`, precedence).

The suite exit code is the **worst** test outcome under that policy
(`refuse` > `fail` > `pass`), raised to at least `1` by any `SIMULATOR_INVALID`
run.

## Run it

```bash
hotato suite run examples/reference-agent/suite.json \
    --agent reference-agent-v1 \
    --release reference-agent-v1 \
    --out ./suite-out
```

`--agent ID` (required) is recorded on every conversation and the release.
`--release ID` names the release the runs are recorded under (default
`<agent>@<suite_id>`); use a stable id per release so
[`hotato release compare`](RELEASE-COMPARE.md) can diff two of them. `--out DIR`
writes the per-test simulated conversation artifacts plus `suite-report.md`,
`suite-report.html`, and `suite-run.json`. `--parallel N` caps the worker
threads for each scenario's matrix; the worker count never changes the
byte-identical result.

The runs are recorded into the fleet registry (`--registry`, default
`~/.hotato/fleet`; `--workspace`/`-w`, default `default`) as
Release / Suite / Scenario / Run / Conversation / Evaluation rows, so
`hotato serve -w <workspace>` renders them (see [WORKSPACE.md](WORKSPACE.md)).
Pass `--no-registry` to report only.

## Worked example: the reference agent

`examples/reference-agent/` ships a full suite: 25 realistic voice-agent jobs
x 5 caller behaviours x 3 audio environments = 375 offline simulated runs,
scored across the outcome / policy / conversation / speech dimensions. Its
`suite.json` has `inconclusive_policy: fail` and `required_for_release: true`.

The one-command path regenerates the files, runs the 375-run suite offline, and
records a browsable workspace:

```bash
cd examples/reference-agent
make reference
# then browse:
hotato serve --workspace reference --registry ./.workspace
```

The summary printed to stdout follows this shape (counts here are schematic,
illustrating the report's shape):

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

| code | meaning |
| --- | --- |
| `0` | every test in the suite passed under the suite's `inconclusive_policy` (and no run was `SIMULATOR_INVALID`) |
| `1` | at least one test FAILed (a `success.required` condition failed, a deterministic assertion FAILed, or -- under `inconclusive_policy fail` -- an INCONCLUSIVE gated), or a run was `SIMULATOR_INVALID` (a broken fixture, kept separate from any agent PASS/FAIL) |
| `2` | under `inconclusive_policy refuse` a scored INCONCLUSIVE withheld the verdict (takes precedence over a FAIL); OR a usage error / unusable input: a malformed suite / conversation-test / scenario file, or an unresolvable test/scenario ref |

## See also

- [CONVERSATION-TEST.md](CONVERSATION-TEST.md) -- the conversation-test file each suite ref points at.
- [STATE-ADAPTERS.md](STATE-ADAPTERS.md) -- Authority 2 state grounding for `state` / `state_change` checks.
- [RELEASE-COMPARE.md](RELEASE-COMPARE.md) -- diff two releases recorded by `suite run`.
- [WORKSPACE.md](WORKSPACE.md) -- browse the recorded runs with `hotato serve`.
- [SUITES.md](SUITES.md) -- the `corpus/suites/` scenario-audio suites for the `hotato run` scoring path (a distinct mechanism).
