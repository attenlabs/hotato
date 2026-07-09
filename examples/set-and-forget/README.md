# Example: set-and-forget monitoring (cron + CI)

A concrete, runnable pairing for the passive workflow in
[`docs/SET-AND-FORGET.md`](../../docs/SET-AND-FORGET.md): sweep on a nightly
cron, gate a pull request on whatever you have promoted so far. No audio
files live in this directory. Everything below runs on the bundled demo
calls (`--demo`) with zero credentials, so you can try the whole loop before
pointing it at your own stack.

## 0. Try it now, no stack connected

```bash
hotato sweep --demo --format json > hotato-sweep.json
hotato fixture promote hotato-sweep.json#2 --expect yield \
    --id demo-missed-interruption --out tests/hotato
hotato run --scenarios tests/hotato/scenarios --audio tests/hotato/audio
# exit code 1: a real missed-interruption moment from the demo battery is now pinned
```

Swap `--demo` for `--stack vapi` (or any connected stack) once you have run
`hotato connect vapi`; the rest of this example is otherwise identical.

## 1. Nightly cron: sweep, don't watch

[`sweep-nightly.sh`](sweep-nightly.sh) pulls the last day of calls, writes a
dated HTML dashboard, and writes the JSON candidate list `fixture promote`
reads. Run it once yourself:

```bash
DEMO=1 ./sweep-nightly.sh                       # bundled demo calls, no setup
HOTATO_STACK=vapi HOTATO_SINCE=1d ./sweep-nightly.sh   # your connected stack
```

Crontab entry, 03:00 daily, at the repo root:

```
0 3 * * * cd /path/to/repo && HOTATO_STACK=vapi HOTATO_SINCE=1d examples/set-and-forget/sweep-nightly.sh >> reports/sweep.log 2>&1
```

Nothing here writes a fixture or fails a build by itself. `sweep` only ever
reports candidates. Read `reports/sweep-<date>.html` in the morning, and
promote the ones that are real:

```bash
hotato fixture promote "reports/sweep-2026-07-08.json#3" --expect yield \
    --id refund-cutoff-002 --out tests/hotato
```

## 2. CI: gate on what you have promoted

Once you have promoted at least one fixture into `tests/hotato/`, gate every
pull request on it. This reuses the exact pattern in
[`docs/CI.md`](../../docs/CI.md) / [`.github/workflows/hotato.yml`](../../.github/workflows/hotato.yml),
pointed at your own fixtures instead of the bundled self-test suite:

```yaml
# .github/workflows/hotato-fixtures.yml
name: hotato fixture regressions
on: [pull_request]
jobs:
  turn-taking:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - uses: actions/setup-python@v5
        with: { python-version: "3.11" }
      - run: python -m pip install .
      - run: |
          hotato run --scenarios tests/hotato/scenarios \
              --audio tests/hotato/audio --format json --no-fail > result.json
      - run: python -c "import json,sys; sys.exit(json.load(open('result.json'))['exit_code'])"
```

Every fixture promoted from a nightly sweep now fails the build if the same
moment ever comes back, whether it resurfaces in production first or a
reviewer reproduces it locally.

## The whole loop, end to end

```
cron: sweep-nightly.sh --since 1d     (candidates, never a verdict)
  -> you read reports/sweep-*.html    (decide: real bug, or noise)
  -> fixture promote                  (freeze the real ones)
  -> git commit tests/hotato/         (the fixture ships with the fix)
  -> CI: hotato run on every PR       (fails if the bug ever comes back)
```
