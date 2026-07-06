# From a bad call to a CI gate

One bad call moment becomes a permanent, offline regression test in five
steps. Everything below runs locally; no audio leaves your machine.

## The label comes from you

Hotato does not infer intent. You label the expected behavior for the event:
yield means the agent should stop for the caller. hold means the agent should
keep speaking through a backchannel/noise/acknowledgement. Hotato then
measures whether the timing matched that label.

That is the whole contract. "mhm" and "stop" can carry identical speech
energy; no timing measurement can tell them apart. What Hotato can measure,
reproducibly, is whether the agent did what your label says it should have
done, and how many seconds it took.

Input requirement, stated once: Hotato's main scorer requires separated
caller and agent tracks: either one two-channel WAV or two aligned mono WAVs.
A single mixed mono call is not enough to attribute talk-over reliably.

## Step 1: turn the moment into a fixture

You have a recording where the agent talked over a caller at 42.18 seconds.
Label it:

```bash
hotato fixture create --stereo bad-call.wav \
    --id refund-interruption-001 \
    --onset 42.18 --expect yield \
    --max-talk-over 0.6 --max-time-to-yield 1.0 \
    --out tests/hotato
```

This writes `tests/hotato/scenarios/refund-interruption-001.json` (the label,
with provenance) and `tests/hotato/audio/refund-interruption-001.example.wav`
(a two-channel clip around the event, onset re-based to the clip). The
fixture is scored immediately on creation; an input that cannot be judged is
refused with the reason and exit code 2, never written silently.

Do not know the onset? List the candidate moments first:

```bash
hotato scan --stereo bad-call.wav
```

`scan` reports timing facts only (overlap onsets, agent starts during caller
speech, long response gaps). You pick the moment and supply the label.

## Step 2: run it

```bash
hotato run --scenarios tests/hotato/scenarios --audio tests/hotato/audio
```

The bad take fails, by design: that is the regression you are pinning. Exit
codes are the contract: 0 all pass, 1 a regression, 2 unusable input.

## Step 3: fix your agent, then compare

Change the setting, re-capture the same scenario, and let the numbers speak:

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

If the moment shifted between takes, pass `--before-onset` and
`--after-onset` separately. `--out report.html` writes the shareable HTML
report with the before take as the base. A side that cannot be judged renders
NOT SCORABLE and exits 2; no verdict is invented.

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

`hotato run` exits 1 on any regression, so the job fails when the timing
regresses. Already running pytest? `pytest --hotato-suite
--hotato-suite-scenarios tests/hotato/scenarios --hotato-suite-audio
tests/hotato/audio` adds the same gate to the run you have (see
[PYTEST.md](PYTEST.md)). The richer PR check with a sticky results comment is
in [CI.md](CI.md).

## Step 5: when it fails, plan the fix

```bash
hotato run --scenarios tests/hotato/scenarios --audio tests/hotato/audio \
    --format json > result.json
hotato plan result.json --stack vapi --assistant-id <id>
```

`plan` is read-only and guarded: it proposes at most one bounded step on one
setting, refuses to tune a single threshold when the battery fails on both
axes at once, and downgrades to a checklist when the evidence cannot isolate
the layer. It never applies anything (`platform_mutation.performed` is always
false). Details: [FIX-PLANS.md](FIX-PLANS.md).

## Use Hotato when

- You have (or can capture) separated caller/agent audio for the call.
- You can point at a moment and label what should have happened: yield or
  hold.
- You want a local, deterministic regression test that fails CI when the
  turn-taking timing regresses.

## When not to use Hotato

Hotato measures turn-taking timing from separated audio tracks. It is the
wrong tool for:

- Transcript quality or wording checks. Nothing here transcribes.
- Task success or goal completion. Hotato does not know what the call was
  for.
- Sentiment, tone, or emotion. Out of scope, permanently.
- Compliance or script adherence review.
- Tool-call or API-behavior testing.
- Mixed mono recordings. One summed channel cannot attribute talk-over to a
  speaker.
- Live, in-call decisions. Hotato scores recordings after the fact; it does
  not predict at runtime.
- Unlabeled whole-call analysis. `hotato scan` surfaces candidate moments,
  but every verdict needs your yield or hold label; Hotato never invents one.
