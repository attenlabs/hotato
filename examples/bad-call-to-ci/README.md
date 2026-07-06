# Example: from a bad call to a CI gate

A runnable walkthrough of the regression loop using only what ships in the
package. No audio files live in this directory: the packaged demo battery
provides the bad-agent recordings, so every command below works right after
`pip install hotato` (or with `uvx hotato ...`), fully offline.

The full guide, including the label semantics and when not to use Hotato, is
[docs/BAD-CALL-TO-CI.md](../../docs/BAD-CALL-TO-CI.md).

## 0. Get a bad call to work with

The packaged demo battery contains a deliberately bad agent. Export its
audio path once:

```bash
python3 -c "from importlib import resources; print(resources.files('hotato').joinpath('data', 'demo', 'failing', 'audio', 'fd-01-missed-interruption.example.wav'))"
BAD=$(python3 -c "from importlib import resources; print(resources.files('hotato').joinpath('data', 'demo', 'failing', 'audio', 'fd-01-missed-interruption.example.wav'))")
```

In `fd-01` the caller takes the floor at 2.00s and the agent never stops.

## 1. Turn the moment into a fixture

```bash
hotato fixture create --stereo "$BAD" \
    --id missed-interruption-001 \
    --onset 2.00 --expect yield --max-talk-over 0.8 \
    --out tests/hotato
```

Unsure where the moment is on your own call? `hotato scan --stereo call.wav`
lists candidate moments as timing facts; you pick one and label it.

## 2. Run it (it fails, by design)

```bash
hotato run --scenarios tests/hotato/scenarios --audio tests/hotato/audio
# exit code 1: the regression is pinned
```

## 3. Fix the agent, then compare before/after

The bundled `01-hard-interruption` fixture is a well-behaved take of the same
kind of moment (caller onset 2.40s), so it stands in for the after take:

```bash
GOOD=$(python3 -c "from importlib import resources; print(resources.files('hotato').joinpath('data', 'audio', '01-hard-interruption.example.wav'))")
hotato compare --before "$BAD" --after "$GOOD" \
    --before-onset 2.00 --after-onset 2.40 --expect yield
# verdict: FAIL -> PASS, result: fixed
```

## 4. Gate CI

```yaml
# .github/workflows/turn-taking.yml
name: turn-taking
on: [pull_request]
jobs:
  hotato:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - uses: actions/setup-python@v5
        with: { python-version: "3.12" }
      - run: pip install hotato
      - run: hotato run --scenarios tests/hotato/scenarios --audio tests/hotato/audio --format json
```

## 5. Plan the fix when it fails

```bash
hotato run --scenarios tests/hotato/scenarios --audio tests/hotato/audio \
    --format json > result.json
hotato plan result.json
```

The plan is read-only (`platform_mutation.performed` is always false): at
most one bounded step on one setting, a refusal when no single threshold can
win, a checklist when the evidence cannot isolate the layer.
