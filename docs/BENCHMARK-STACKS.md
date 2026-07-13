# Benchmarking voice stacks with hotato

`hotato benchmark` scores the recordings you captured by running one fixed
scenario set through YOUR configured voice stack. Every stack is scored on the
same scenarios, the same labels, and the same thresholds, so the result files
are directly comparable.

hotato measures timing on the recordings it is given. It ships no vendor
numbers, no leaderboard, and no accuracy percentage. Results depend on your
stack configuration and your captures; a benchmark result describes YOUR
setup, on the day you captured it.

## 1. Pick the scenario set

The bundled 8-scenario barge-in battery is the default. Any scenarios dir
works the same way, for example the larger tiered suites:

```
corpus/suites/gold/scenarios
corpus/suites/silver/scenarios
```

Each scenario JSON carries the caller transcript, the caller onset timing,
and the pass thresholds. The corpus suites also ship each scenario's caller
stimulus as `audio/<id>.caller.wav`.

## 2. Capture the battery through your stack

For every scenario, drive the caller side through your stack and record the
call dual-channel (caller on channel 0, agent on channel 1):

- Corpus suites: play the shipped `<id>.caller.wav` stimulus into your stack.
  For the bundled battery, reproduce the caller side from each scenario's
  transcript and `caller_onset_sec`.
- `hotato setup --stack livekit` or `--stack pipecat` prints the exact
  dual-channel recording scaffold for your infra.
- `hotato capture --stack vapi --call-id <id>` or
  `hotato capture --stack twilio --recording-sid RE...` pulls the finished
  dual-channel recording for you.

Name each recording by its scenario id, one directory per stack:

```
captures/livekit/01-hard-interruption.wav
captures/livekit/02-backchannel-mhm.wav
...
captures/vapi/01-hard-interruption.wav
```

## 3. Run the benchmark

```bash
hotato benchmark --stack livekit --recordings captures/livekit --out livekit.json
hotato benchmark --stack vapi    --recordings captures/vapi    --out vapi.json
```

Each result JSON records the stack, the scenario set, every measured signal
(the same events `hotato run` produces), the exact scoring thresholds, and a
provenance block: who ran it, on which files, with each file's mtime. The
result timestamp comes from the input files, not the wall clock, so the same
inputs reproduce the same result.

Scenarios without a matching recording are listed under `not_captured`. They
are never scored and never counted as failures. The exit code is 0 when the
run completes; add `--fail-on-regression` to exit 1 when a scored event fails
its scenario thresholds.

## 4. Compare stacks

```bash
hotato benchmark compare livekit.json vapi.json
```

Side by side, per scenario: yielded, talk-over seconds, and time-to-yield
seconds for each input, with signed deltas against the first file, plus
summary medians. If the files cover different scenario sets, the intersection
is compared and everything else is listed as skipped, with the files it was
missing from.

## What the numbers are

Reproducible timing measurements of the recordings you provided, scored by
the same engine as `hotato run`, with every threshold exposed in the result.
They tell you how your configuration of a stack handled the battery you
captured.
