# One pull-request comment per CI run: `hotato pr comment`

When hotato gates a pull request, the review page should show what broke
without anyone opening the job log. `hotato pr comment` turns the machine
result of a run into the Markdown body of ONE pull-request comment, and the
root Action can post it, update it in place on every push, and write the same
Markdown to the job summary.

The comment carries:

- the overall verdict (`PASS`, `FAIL`, `INCONCLUSIVE`, or `ERROR`) and the
  count of cases per status;
- a table of every case that did not pass: its status, the case, what failed,
  the measured evidence, and links to its files;
- for a contract run, the caught moment with its ASCII barge-in timeline;
- a comparison with the base branch's result when you supply one: fixed,
  newly failing, still failing, and now inconclusive cases;
- the paths of each case's Failure Record and SVG evidence card inside the
  results artifact, and a link to the workflow run that holds it.

## Render it

Any machine result works: a suite run (`hotato suite run --format json`), a
conversation test run (`hotato test run --format json`), or a contract
verification (`hotato contract verify --format json`).

```bash
hotato contract verify contracts --format json > results/contract-verify.json
hotato record render results/contract-verify.json --all --out results/records
hotato pr comment results/contract-verify.json \
  --base base/contract-verify.json --base-label main \
  --records-index results/records/index.json \
  --cards results/cards --artifact-root results \
  --out results/pr-report.md
```

| Flag | Meaning |
|---|---|
| `--base FILE` | The base branch's result of the same kind. A missing or unusable file is a line in the comment, never an error |
| `--base-label TEXT` | How the comment names the base, for example `main` |
| `--records-index FILE` | The `index.json` that `hotato record render --all` wrote; each case links its Failure Record |
| `--cards DIR` | Render one `hotato card` SVG per failing contract (or failing say-do test run) into DIR |
| `--artifact-root DIR` | Show file paths relative to DIR, the directory your workflow uploads |
| `--run-url URL` / `--artifact-url URL` | The link the files are reached through (the artifact link wins when both are given) |
| `--reproduce COMMAND` | The command that produced the result, shown as the local reproduce line |
| `--max-rows N` | Rows per table (default 25); the rest are counted |
| `--format json` | The body plus the verdict and the card paths |

The command is offline and deterministic: the same files and flags render the
same bytes. The output holds no timestamp and no absolute path. It never posts
anything. Exit 0 once rendered (the verdict it reports never changes the exit
code), 2 for an unreadable result or a result of another kind.

## Post it from the root Action

Set `comment-pr: true` and grant `pull-requests: write`. Pass the base
branch's result as `baseline` to get the comparison:

```yaml
name: conversation QA

on:
  pull_request:

permissions:
  contents: read
  pull-requests: write

jobs:
  conversation-qa:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@11bd71901bbe5b1630ceea73d27597364c9af683 # v4.2.2
      # The base branch's suite, scored the same way, for the comparison.
      # Best effort: a red base never fails this job.
      - uses: actions/checkout@11bd71901bbe5b1630ceea73d27597364c9af683 # v4.2.2
        with:
          ref: ${{ github.base_ref }}
          path: .hotato-base-src
      - continue-on-error: true
        uses: attenlabs/hotato@<full-commit-sha>  # v1.22.0
        with:
          suite: .hotato-base-src/tests/voice/qa.suite.json
          agent: support-agent
          output: .hotato/base
          render-records: false
      - id: hotato
        uses: attenlabs/hotato@<full-commit-sha>  # v1.22.0
        with:
          suite: tests/voice/qa.suite.json
          agent: support-agent
          baseline: .hotato/base/suite-run.json
          comment-pr: true
      - if: always()
        uses: actions/upload-artifact@834a144ee995460fba8ed112a2fc961b36a5ec5a # v4.3.6
        with:
          name: hotato-conversation-qa
          path: ${{ steps.hotato.outputs.output }}
          if-no-files-found: error
```

The result file in an output directory is `suite-run.json`,
`test-run.json`, or `contract-verify.json`. Any earlier result of the same
kind works as `baseline`, for example one downloaded from the base branch's
last run. Without `baseline` the comment says there was nothing to compare
against.

What the Action does on every run, with or without `comment-pr`:

- renders the report to `<output>/pr-report.md` (the `pr-report` output),
  with the Failure Records and evidence cards under `<output>/records/` and
  `<output>/cards/`, so an uploaded `output` directory carries every file the
  comment names;
- leaves the gate alone: the step exit is still hotato's exit code, and a
  rendering problem is a warning, never a different exit.

What `comment-pr: true` adds, in a separate step that exits 0 on every path:

- finds the one comment carrying the hidden marker `<!-- hotato-pr-report -->`
  (or the older `<!-- hotato-contract-verify -->` marker) and updates it in
  place; it creates the comment only when none exists, and skips the write
  when the body has not changed;
- appends the same Markdown to the job summary;
- on a pull request from a fork, where GitHub hands the workflow a read-only
  token, skips the API and leaves the report on the job summary with a
  one-line note. A token without write access (HTTP 401, 403, or 404) gets
  the same fallback.

Only this step reads the token. The default (`comment-pr: false`) reads no
token and posts nothing. On an exact older `hotato-version` pin that has no
`hotato pr comment`, a contract run posts the contract-verify block instead,
and suite and test runs post nothing.

## Reading the comparison

Cases are matched by id: the test id for a suite run, the test id and check
id for a test run, the contract id for a contract run (a contract's embedded
assertions are a separate row, never blended into its timing verdict).

| Change | This run | Base |
|---|---|---|
| fixed | PASS | FAIL, TAMPERED, ERROR, or an inconclusive status |
| newly failing | FAIL, TAMPERED, or ERROR | PASS, or not on the base |
| still failing | FAIL, TAMPERED, or ERROR | FAIL, TAMPERED, or ERROR |
| now inconclusive | INCONCLUSIVE, REFUSED, or NOT_SCORABLE | anything else |

An inconclusive case lacks the evidence its check needs. It is never counted
as a pass or a fail. Cases on the base that this run does not have are
counted on one line.

## Safety of the rendered text

Ids and reasons come from result files, which a pull request can change. The
renderer escapes table pipes, HTML brackets, and backticks, and neutralises
`@` so a value cannot mention a user. Tables are capped at `--max-rows`, and
the body stays under GitHub's comment size limit.

Related: [CI.md](CI.md) (the root Action), [CARDS.md](CARDS.md)
(evidence cards), [REPORTS.md](REPORTS.md) (Failure Records and reports).
