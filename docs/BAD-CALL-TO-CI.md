# From a bad call to a CI gate

Turn one bad call into a permanent, offline regression test. Five steps,
audio included, all local.

## Start from your own provider call

Have a call id instead of a WAV? Pull, review, label, and open the pull
request in three commands:

```bash
hotato investigate --stack vapi --call-id <id>
hotato investigate label .hotato/investigate-state.json#<n> --expect yield --reviewer you
hotato pr create --fixtures contracts/<contract-id>.hotato --repo you/your-repo --title "Add hotato contract <contract-id>"
```

`investigate` prints a ranked candidate for each timing moment with the exact
label command. `investigate label` writes a signed contract bundle to
`contracts/<contract-id>.hotato/` and prints the exact `pr create` command
for it. `pr create` accepts that bundle directly: it stages the bundle
byte-identical under `tests/hotato/contracts/` and opens the pull request
(dry run by default; `--yes` runs git and gh). CI then gates on
`hotato contract verify tests/hotato/contracts/`, which exits non-zero when
a contract regresses.

Prefer a plain scenario + audio fixture over a contract bundle? The same
labeled moment lands as one through the alternate sequence, and the
`hotato run` CI gate below applies to it:

```bash
hotato fixture promote .hotato/investigate-state.json#<n> --expect yield --out tests/hotato
hotato pr create --fixtures tests/hotato --repo you/your-repo --title "Add turn-taking regression fixtures"
```

## The label comes from you

Yield means the agent should stop for the caller. Hold means it should keep
talking through a backchannel, noise, or acknowledgement. Hotato measures
whether the timing matched your label.

`"mhm"` and `"stop"` can carry identical speech energy -- no timing
measurement tells them apart. So Hotato measures the one thing timing can
settle: did the agent do what your label says, and how fast.

The main scorer needs separated tracks: one two-channel WAV, or two aligned
mono WAVs, enough to attribute talk-over reliably.

## Step 1: turn the moment into a fixture

Say the agent talked over a caller at 42.18 seconds. Label it:

```bash
hotato fixture create --stereo bad-call.wav \
    --id refund-interruption-001 \
    --onset 42.18 --expect yield \
    --max-talk-over 0.6 --max-time-to-yield 1.0 \
    --out tests/hotato
```

Writes the labeled scenario with provenance
(`tests/hotato/scenarios/refund-interruption-001.json`) and a two-channel
audio clip (`tests/hotato/audio/refund-interruption-001.example.wav`),
scored on creation. An unjudgeable input is refused with the reason, exit
code 2.

Don't know the onset? List the candidates first:

```bash
hotato scan --stereo bad-call.wav
```

`scan` reports timing facts only -- overlap, an agent starting over caller
speech, long gaps, dead air, or suspected TTS echo. You pick the moment and
supply the label.

## Step 2: run it

```bash
hotato run --scenarios tests/hotato/scenarios --audio tests/hotato/audio
```

The bad take fails, by design: that's the regression you're pinning. Exit
codes are the contract: 0 all pass, 1 a regression, 2 unusable input.

## Step 3: fix your agent, then compare

Change the setting, re-capture the same scenario, let the numbers speak:

```bash
hotato compare --before bad-call.wav --after new-take.wav \
    --onset 42.18 --expect yield
```

```
hotato compare: bad-call.wav -> new-take.wav
  verdict:           FAIL -> PASS
  did_yield:         false -> true
  seconds_to_yield:  - -> 0.42s
  talk_over_sec:     2.65s -> 0.42s  improved -2.23s

result: fixed
```

Moment shifted between takes? Pass `--before-onset` and `--after-onset`
separately. `--out report.html` writes a shareable HTML report. An
unjudgeable side renders NOT SCORABLE and exits 2.

## Step 4: gate CI on it

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

`hotato run` exits 1 on a regression, failing the job. Already running
pytest? `pytest --hotato-suite --hotato-suite-scenarios
tests/hotato/scenarios --hotato-suite-audio tests/hotato/audio` adds the
same gate (see [PYTEST.md](PYTEST.md)). The richer PR check with a sticky
results comment is in [CI.md](CI.md).

## Step 5: when it fails, plan the fix

```bash
hotato run --scenarios tests/hotato/scenarios --audio tests/hotato/audio \
    --format json > result.json
hotato plan result.json --stack vapi --assistant-id <id>
```

`plan` is read-only and guarded: one bounded step on one setting at most,
never a single-threshold fix when the battery fails on both axes at once,
and a checklist when the evidence can't isolate the layer. It only proposes
-- `platform_mutation.performed` is always false. Details:
[FIX-PLANS.md](FIX-PLANS.md).

## Use Hotato when

- You have (or can capture) separated caller/agent audio.
- You can point at a moment and label it: yield or hold.
- You want a local, deterministic CI regression test on turn-taking timing.

## What Hotato measures

Turn-taking timing from separated audio tracks: talk-over, response gaps,
and yield/hold timing against your label. Not transcript wording, call
outcome, sentiment, compliance, or tool-call behavior. Not live, in-call
decisions either -- Hotato scores recordings after the fact. And `hotato
scan` surfaces candidates only; every verdict still needs your yield or
hold label.
