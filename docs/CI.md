# CI: gate a PR on turn-taking

hotato scores turn-taking timing from call recordings, so a pull request can
carry a running score and fail when the agent gets slower to stop talking for
an interrupting caller. The workflow runs offline with zero extra dependencies.

## The root Action: run a committed suite from any repository

The repository root ships a composite GitHub Action, so a repository with no
hotato source can run a committed suite, conversation test, or contract
verification and gate on hotato's exit status. The default run is offline: it
runs the pinned Action revision itself off PYTHONPATH (no pip, no package index), installs no
model, no ASR, no Node tool, and calls no external judge. No secret is read
or needed.

The Action is available as a composite Action from release v1.4.0 onward. Adopt
the current release (v1.5.3) and pin the revision you adopt by its full commit
SHA; resolve a tag to its SHA first:

```bash
git ls-remote https://github.com/attenlabs/hotato refs/tags/v1.5.3
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
        uses: attenlabs/hotato@<full-commit-sha>  # v1.5.3
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
comment, never uploads an artifact, and never sends a notification; retention
and publication stay under the consumer workflow's control, as above.

### Inputs

Exactly one of `suite`, `test`, `contracts` selects what runs; each is a
workspace-relative committed path. `agent` names the agent under test for
suite and test runs. Everything else is optional: `release` (defaults to the
commit SHA), `output` (defaults to `.hotato/results`), `parallel` (suite
worker cap; never changes result bytes), `transcript` / `trace` / `state`
(evidence files for a test run), `gate-advisory` (test runs only; passes
hotato's own `--gate-judge` so the model-judged rubric lane gates), and
`hotato-version`.

`hotato-version` controls the install and refuses anything unpinned:

- `action` (default): install the pinned Action revision itself, with
  `--no-deps` and no package-index egress. The code that runs is exactly the
  revision your workflow pinned.
- an exact version such as `1.5.3`: `pip install --no-deps hotato==1.5.3`.
- `preinstalled`: skip installation (hotato is already on the runner).

Ranges and `latest` are refused. Suite policy lives in the committed suite
file; the Action never overrides a suite's `inconclusive_policy`.

### Outputs and the exit contract

The step exit IS hotato's exit code, so the job gates without any extra step:

| Exit | Meaning |
|---|---|
| 0 | every deterministic check passed under the committed policy |
| 1 | a deterministic check or a `success.required` condition failed |
| 2 | the verdict was withheld (refuse policy), or a usage/validation error |

The machine JSON stays primary. Outputs: `output` (results directory),
`suite-result` (the machine result JSON path), `summary` (the rendered
Markdown), `records` (Failure Record directory when produced, else empty),
`exit-code` (as a string), `status` (`pass`, `fail`, `inconclusive`, or
`error`, read from the machine result; presentation only), and
`hotato-version` (the executed package version).

The five-lane job summary (Outcome, Policy, Conversation, Speech,
Reliability) is appended to the job page on pass AND on failure, with the
exact reproduce command and the evaluated check ids. A lane with no evaluated
check renders NOT_RUN; a lane whose checks lack required evidence renders
INCONCLUSIVE, never PASS. The model-judged rubric lane reports in its own
advisory section with `gate enabled: true|false` and never changes the exit
unless the run opted in with `gate-advisory: true`; when no local judge is
reachable it reports ERROR, never a fabricated verdict.

The conformance fixture for all of this is
[`tests/fixtures/action-consumer/`](../tests/fixtures/action-consumer/), and
`.github/workflows/tests.yml` runs the same consumer shape against the local
checkout on every pull request (job `action-smoke`).

## Drop it in

Copy [`.github/workflows/hotato.yml`](../.github/workflows/hotato.yml) into your
repository at the same path. That is the whole setup. On the next pull request it
will:

- install the package with `pip install .`
- score the bundled barge-in suite to a JSON envelope
- render a Markdown summary with [`scripts/pr_comment.py`](../scripts/pr_comment.py)
- post or update one sticky comment (found by a hidden marker, so it stays a single comment across runs)
- fail the job on any regression

The sticky comment shows a pass/fail line, a per scenario table (expect, yielded,
time to yield, talk over, result), and a short regressions section.

The job needs `pull-requests: write` to post the comment. The workflow already
requests it; if your org restricts the default `GITHUB_TOKEN`, allow that scope.

## Point it at your own recordings

The bundled suite is a self-test: it scores frozen synthetic fixtures, proving
the harness itself works. The strongest gate for your agent is a suite of
your OWN bad moments, pinned as fixtures with `hotato fixture create` and run
with `hotato run --scenarios DIR --audio DIR`; the full loop from one bad call
to this gate is [BAD-CALL-TO-CI.md](BAD-CALL-TO-CI.md). Alternatively, replace
one step, `Score turn-taking (head)`, with your own capture and score:

1. play each corpus `*.caller.wav` into your agent
2. record your agent's reply
3. score the pair, caller on channel 0 and your agent on channel 1:

```bash
hotato run --stereo your_call.wav --expect yield --format json --no-fail > head.json
```

The envelope shape and exit codes are identical, so the render, comment, and gate
steps stay exactly as they are. Keep `--no-fail` on the score step so the comment
still posts on a regression; the true pass/fail lives in the envelope's
`exit_code`, which the `Fail on regression` step reads.

## Deltas against the base branch

When the workflow can install and score the base branch, it runs the same suite
there in an isolated venv and uses it as the baseline. Any scenario where the
overlap grew (talk-over up) or the agent stopped later (time to yield up) is
listed under Regressions with the delta. This step is best effort: if it cannot
run, the comment falls back to the current pass/fail table and the gate still
holds.

## Render a comment yourself

`scripts/pr_comment.py` is stdlib only and reads the same JSON the CLI emits, so
you can preview the comment locally:

```bash
hotato run --suite barge-in --format json | python3 scripts/pr_comment.py
```

Add `--base base.json` to include deltas against a saved baseline run.

## The other ready-made gate: pytest

If your CI already runs pytest, one flag adds the same regression gate to that
run instead: `pytest --hotato-suite` scores the battery after your tests and
fails the session on a regression. Point it at your own labelled sets with
`--hotato-suite-scenarios` and `--hotato-suite-audio`. Details:
[`PYTEST.md`](PYTEST.md). For richer artifacts on a gate failure,
`hotato report` renders the visual report and `hotato team` tracks the trend
across runs ([`REPORTS.md`](REPORTS.md)).
