# CI: gate a PR on turn-taking

hotato turns a call recording into reproducible turn-taking timing, so a pull
request can carry a running score and fail when your agent gets worse at
yielding. The workflow is offline, dependency light, and posts one comment that
updates itself on every push.

## Drop it in

Copy [`.github/workflows/hotato.yml`](../.github/workflows/hotato.yml) into your
repository at the same path. That is the whole setup. On the next pull request it
will:

- install the package with `pip install .`
- score the bundled barge-in suite to a JSON envelope
- render a Markdown summary with [`scripts/pr_comment.py`](../scripts/pr_comment.py)
- post or update one sticky comment (found by a hidden marker, so it never spams)
- fail the job on any regression

The sticky comment shows a pass/fail line, a per scenario table (expect, yielded,
time to yield, talk over, result), and a short regressions section.

The job needs `pull-requests: write` to post the comment. The workflow already
requests it; if your org restricts the default `GITHUB_TOKEN`, allow that scope.

## Point it at your own recordings

The bundled suite is a self-test: it scores frozen synthetic fixtures to prove
the harness works, not to judge your agent. To gate on your agent, replace one
step, `Score turn-taking (head)`, with your own capture and score:

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

If the base branch is scorable, the workflow scores the same suite there in an
isolated venv and passes it as a baseline. Any scenario that started overlapping
more (talk over up) or yielding slower (time to yield up) is listed under
Regressions with the delta. This step is best effort: if it cannot run, the
comment falls back to the current pass/fail table and the gate still holds.

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
