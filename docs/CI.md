# CI: gate a PR on turn-taking

hotato scores turn-taking timing from call recordings, so a pull request
carries a running score and fails when the agent gets slower to stop
talking for an interrupting caller. The workflow runs offline with zero
extra dependencies.

## One command to the same record CI renders

`hotato start --demo` runs the whole loop offline and leaves the canonical
share-safe Failure Record under `hotato-failure-record/` --
`failure-record.{json,md,html,svg}`, the same primitive this Action renders
per non-passing unit (the `records` output below). The demo prints its
evidence-specific headline, the Markdown and SVG share paths, and the
one-command verifier:

```
Conversation failed: Agent did not yield; measured talk-over was 2.66 s.

  Share in a PR:      hotato-failure-record/failure-record.md
  Share as an image:  hotato-failure-record/failure-record.svg
  Verify the record:  uvx --from hotato==1.14.0 hotato record verify hotato-failure-record/failure-record.json
```

Preview it locally, then scaffold the durable gate into your own repository
in one command:

```bash
hotato init starter --stack generic --out .
# stack-tuned instead:  --stack vapi, retell, twilio, livekit, pipecat
```

That writes a CI workflow verifying `contracts/` and re-scoring `fixtures/`
on every push and pull request (a no-op until your first one lands), plus a
stack-tuned `hotato.yaml`, `contracts/`, and `fixtures/`.

## The root Action: run a committed suite from any repository

The repository root ships a composite GitHub Action: a repository with no
hotato source can run a committed suite, conversation test, or contract
verification and gate on hotato's exit status. The default run is
offline -- it runs the pinned Action revision itself off PYTHONPATH (no
pip, no package index), installs no model, no ASR, no Node tool, calls no
external judge, and reads no secret.

Composite Action since v1.4.0. Adopt the current release (v1.14.0), pinned
by its full commit SHA; resolve the tag to its SHA first:

```bash
git ls-remote https://github.com/attenlabs/hotato refs/tags/v1.14.0
```

Then commit this workflow (replace the `attenlabs/hotato` pin with the SHA
the command printed):

```yaml
name: conversation QA

on:
  pull_request:

permissions:
  contents: read

jobs:
  conversation-qa:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@11bd71901bbe5b1630ceea73d27597364c9af683 # v4.2.2
      - id: hotato
        # Pin by full commit SHA (immutable); the comment names the release.
        uses: attenlabs/hotato@<full-commit-sha>  # v1.14.0
        with:
          suite: tests/voice/qa.suite.json
          agent: support-agent
      # Artifact upload is an explicit consumer step, pinned by full commit
      # SHA; the Action itself never uploads, comments, or notifies.
      - if: always()
        uses: actions/upload-artifact@834a144ee995460fba8ed112a2fc961b36a5ec5a # v4.3.6
        with:
          name: hotato-conversation-qa
          path: ${{ steps.hotato.outputs.output }}
          if-no-files-found: error
```

`permissions: contents: read` is sufficient. The Action never posts a
comment, uploads an artifact, or sends a notification -- retention and
publication stay under your workflow's control.

### Inputs

Exactly one of `suite`, `test`, `contracts` selects what runs (a
workspace-relative committed path); `agent` names the agent under test.
Everything else is optional:

| Input | Meaning |
|---|---|
| `release` | Defaults to the commit SHA |
| `output` | Defaults to `.hotato/results` |
| `parallel` | Suite worker cap; never changes result bytes |
| `transcript` / `trace` / `state` | Evidence files for a test run |
| `gate-advisory` | Test runs only; passes `--gate-judge` so the model-judged rubric lane gates |
| `hotato-version` | Which install to use -- see below |

`hotato-version` pins the install to one of three forms:

| Value | Effect |
|---|---|
| `action` (default) | Installs the pinned Action revision itself, `--no-deps`, no package-index egress -- exactly the revision your workflow pinned |
| an exact version, e.g. `1.14.0` | `pip install --no-deps hotato==1.14.0` |
| `preinstalled` | Skips installation (hotato is already on the runner) |

A range or `latest` is refused, so the pin always names one exact
revision. Suite policy lives in the committed suite file -- the Action
never overrides a suite's `inconclusive_policy`.

### Outputs and the exit contract

The step exit IS hotato's exit code, so the job gates without any extra
step:

| Exit | Meaning |
|---|---|
| 0 | every deterministic check passed under the committed policy |
| 1 | a deterministic check or a `success.required` condition failed |
| 2 | the verdict was withheld (refuse policy), or a usage/validation error |

The machine JSON stays primary:

| Output | Meaning |
|---|---|
| `output` | Results directory |
| `suite-result` | The machine result JSON path |
| `summary` | The rendered Markdown |
| `records` | Failure Record directory when produced, else empty |
| `exit-code` | As a string |
| `status` | `pass`, `fail`, `inconclusive`, or `error`, read from the machine result -- presentation only |
| `hotato-version` | The executed package version |

The five-lane summary (Outcome, Policy, Conversation, Speech, Reliability)
appends to the job page on pass AND failure, with the reproduce command
and evaluated check ids. A lane with no evaluated check renders NOT_RUN; a
lane whose checks lack required evidence renders INCONCLUSIVE, never
PASS. The model-judged rubric lane reports in its own advisory section
with `gate enabled: true|false` and changes the exit only when
`gate-advisory: true` was set; with no local judge reachable it reports
ERROR instead of guessing.

The conformance fixture:
[`tests/fixtures/action-consumer/`](../tests/fixtures/action-consumer/);
`.github/workflows/tests.yml` runs the same consumer shape against the
local checkout on every pull request (job `action-smoke`).

## Drop it in

Copy [`.github/workflows/hotato.yml`](../.github/workflows/hotato.yml) into
your repository at the same path. That is the whole setup. On the next
pull request it will:

- install the package with `pip install .`
- score the bundled barge-in suite to a JSON envelope
- render a Markdown summary with [`scripts/pr_comment.py`](../scripts/pr_comment.py)
- post or update one sticky comment (found by a hidden marker, so it stays
  a single comment across runs)
- fail the job on any regression

The sticky comment shows a pass/fail line, a per scenario table (expect,
yielded, time to yield, talk over, result), and a short regressions
section.

The job needs `pull-requests: write` to post the comment -- the workflow
already requests it; if your org restricts the default `GITHUB_TOKEN`,
grant that scope.

## Point it at your own recordings

The bundled suite is a self-test: it scores frozen synthetic fixtures,
proving the harness works. The strongest gate for your agent is a suite of
your OWN bad moments, pinned with `hotato fixture create` and run with
`hotato run --scenarios DIR --audio DIR`; full loop:
[BAD-CALL-TO-CI.md](BAD-CALL-TO-CI.md). Or replace one step, `Score
turn-taking (head)`, with your own capture and score:

1. play each corpus `*.caller.wav` into your agent
2. record your agent's reply
3. score the pair, caller on channel 0 and your agent on channel 1:

```bash
hotato run --stereo your_call.wav --expect yield --format json --no-fail > head.json
```

The envelope shape and exit codes are identical, so render, comment, and
gate steps stay exactly as they are. Keep `--no-fail` on the score step so
the comment still posts on a regression -- the true pass/fail lives in the
envelope's `exit_code`, which the `Fail on regression` step reads.

## Deltas against the base branch

When the workflow can install and score the base branch, it runs the same
suite there in an isolated venv as the baseline. Any scenario where
overlap grew (talk-over up) or the agent stopped later (time to yield up)
lists under Regressions with the delta. Best effort: when it can't run,
the comment falls back to the current pass/fail table and the gate still
holds.

## Gate timing drift against a saved baseline

`hotato baseline check` turns those deltas into a hard gate. Commit a
tolerance file naming how much each timing dimension may rise, save a
baseline run envelope, and the check exits 1 the moment a candidate run
drifts beyond a tolerance:

```yaml
# tolerances.yaml -- how much increase each dimension may absorb
response_gap_sec: "+10%"     # percent of the baseline mean
seconds_to_yield: "+0.05"    # absolute seconds
```

```bash
hotato run --suite barge-in --format json --no-fail > baseline.json   # once, on main
hotato run --suite barge-in --format json --no-fail > candidate.json  # per PR
hotato baseline check tolerances.yaml baseline.json candidate.json --junit drift.xml
```

Dimensions: `seconds_to_yield`, `talk_over_sec`, `response_gap_sec`,
`premature_start_sec`; each side's value is the pooled mean across its
scorable events. Every dimension is lower-is-better timing, so the gate is
one-sided: an improvement always passes, and a dimension with no
measurements on a side refuses (exit 2) rather than passing silently.
`--format json` emits the machine envelope with the per-dimension deltas;
`--junit drift.xml` feeds the same CI test widgets as `hotato contract
verify --junit`. Exit codes: 0 within tolerance, 1 drift beyond tolerance,
2 usage error.

## Render a comment yourself

`scripts/pr_comment.py` is stdlib only and reads the same JSON the CLI
emits, so you can preview the comment locally:

```bash
hotato run --suite barge-in --format json | python3 scripts/pr_comment.py
```

Add `--base base.json` to include deltas against a saved baseline run.

## The other ready-made gate: pytest

If your CI already runs pytest, one flag adds the same regression gate:
`pytest --hotato-suite` scores the battery after your tests and fails the
session on a regression. Point it at your own labelled sets with
`--hotato-suite-scenarios` and `--hotato-suite-audio`. Details:
[`PYTEST.md`](PYTEST.md). For richer artifacts on a gate failure, `hotato
report` renders the visual report and `hotato team` tracks the trend
([`REPORTS.md`](REPORTS.md)).

## GitLab CI, Jenkins, Azure Pipelines, CircleCI: `hotato init ci`

The gate is exit-code driven, so it runs in any CI. `hotato init ci`
writes the one canonical config the chosen system reads:

```bash
hotato init ci --system gitlab     # .gitlab-ci.yml
hotato init ci --system jenkins    # Jenkinsfile
hotato init ci --system azure      # azure-pipelines.yml
hotato init ci --system circleci   # .circleci/config.yml
```

Every generated config does the same four things: install the pinned
hotato release (the version that generated the file), verify `contracts/`
with `hotato contract verify`, re-score `fixtures/` with `hotato run`, and
publish the JSON reports plus the JUnit file. A regression exits non-zero
and fails the pipeline; an empty `contracts/` or `fixtures/` directory is
a normal starting state, and each gate stays a no-op until the first one
lands.

- **GitLab CI**: one `hotato` job on `python:3.12`;
  `artifacts.reports.junit` feeds the merge request test widget, and the
  JSON reports upload with `when: always`.
- **Jenkins**: a declarative pipeline on a `python:3.12` docker agent
  (swap in `agent any` on a controller with Python 3.10+); `junit` and
  `archiveArtifacts` publish in the `post { always }` block.
- **Azure Pipelines**: `UsePythonVersion@0` plus script steps;
  `PublishTestResults@2` feeds the Tests tab and `PublishBuildArtifacts@1`
  keeps the JSON reports, both on `always()`.
- **CircleCI**: a `cimg/python:3.12` job wired into a workflow;
  `store_test_results` feeds the Tests tab and `store_artifacts` keeps the
  JSON reports.

`--out DIR` writes into another directory (default `.`, the repo root
where each system looks for its config); `--force` overwrites an existing
file. The pinned version is the hotato that generated the file; bump it in
one place when you upgrade.
