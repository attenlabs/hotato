# Benchmark methodology

`hotato` ships a reproducible measurement-error harness: `src/hotato/benchmark.py`.
Given a set of labelled dual-channel recordings, it reports how far the scorer's
measured event times land from the rendered or labelled ground truth, and how its
yield/hold decisions line up with the human labels. That is the whole output.

```bash
PYTHONPATH=src python3 -m hotato.benchmark
```

This runs on every synthetic fixture in the checkout (the bundled battery, the
`examples/` reference set, and the deliberately-bad `funnel-demo` set), prints a
markdown table, and writes `benchmark-report/measurement-error.json` and
`benchmark-report/measurement-error.md`.

The harness is a standalone measurement tool you run against fixtures. The `hotato`
CLI scores a single call or the battery.

---

## What it measures

For every `(recording, label)` pair it reports two things.

### 1. Per-signal measurement error, in milliseconds

For each timing signal it computes `|measured - rendered|` in ms, where `rendered`
is the exact value the synthetic fixture was rendered from (its `reference_render`
block) or the value a contributor labelled by hand:

| signal | measured | rendered/true reference |
|---|---|---|
| **caller onset** | onset the VAD detects (the scorer is given no onset label) | start of the first caller segment |
| **time to yield** | seconds from caller onset to the agent going quiet | the agent's in-progress turn end minus the caller onset |
| **response gap** | endpointing dead-air from the caller's turn end to the agent's next onset | the fixture's rendered response gap |

Errors are reported as a distribution: median, mean, worst case, best case, and n.
A signal is scored where a rendered or true reference exists and the scorer
produced a value; a missing reference yields a `-`. (The echo-of-agent fixture has
no independent caller speech, so it has no onset or yield error: a gap.)

Onset is measured in **detect mode** (the scorer gets no onset hint), so it
tests the onset detector directly. Yield, talk-over, response gap, and `did_yield`
are measured in **label mode** (the scorer is given the human `caller_onset_sec`,
exactly as the shipped battery runs), so those are the numbers a user
sees.

### 2. A `did_yield` confusion matrix

Against the `should_yield` / `should_not_yield` label (each scenario's
`expected.yield`), it reports the four cells:

|  | measured **did_yield** | measured **held floor** |
|---|---|---|
| **should_yield** | correct yield | **missed yield** |
| **should_not_yield** | **false yield** | correct hold |

The two off-diagonal cells (missed yields, and false yields) are the failures an
operator feels, so the report surfaces them directly.

---

## Why milliseconds and a matrix

The report is a per-signal error distribution plus a four-cell confusion matrix.
The cells stay separate because a missed yield and a false yield are different
failures with different fixes; averaging them into one number would hide which
one you have. This is the rule the corpus governance doc enforces
(`docs/CORPUS-GOVERNANCE.md`, "Validity metrics"), applied to the tooling.

The reported error is what the default shipped config measures, so it is
the number you get. On the synthetic fixtures the yield error equals the exposed VAD
hangover and the onset/gap error is one frame hop, both documented `ScoreConfig`
parameters. Set the hangover to zero
(`caller_vad.hangover_sec = agent_vad.hangover_sec = 0`) and every signal collapses
to within one hop of the rendered ground truth; the test suite asserts exactly
this, so the claim is checkable.

The scorer works on speech energy over time, so the harness reports timing and
decisions.

---

## Quantization: a reported time is not infinitely precise

Every timing signal the scorer reports is quantized to the frame hop
(`ScoreConfig.hop_ms`, default `10.0` ms) plus, for yield/talk-over, the VAD
hangover (`caller_vad.hangover_sec` / `agent_vad.hangover_sec`, default `0.15`
s): the measured value can land up to one hop off the true underlying event
purely from where that event falls inside a 10 ms frame, before hangover is
even counted. This sub-frame-phase rounding is deterministic, driven purely
by where the event lands inside the frame, and it is the same one-hop
collapse the section above already pins down (hangover zero -> every signal
within one hop of ground truth).

The consequence for a `--max-time-to-yield` (or any other) policy bound: a
physically identical yield event, shifted by a few milliseconds of sub-frame
phase with nothing else about the audio changed, can cross an exact bound
purely from quantization. Reproduced case: a 250 ms yield event evaluated
against a 400 ms bound flips PASS/FAIL as the event's sub-frame phase is swept
through 3, 6, 12, and 16 ms offsets, with the underlying event unchanged --
the measured value moves by exactly one hop (10 ms) at each transition, and
no further. Phase alone can flip a bound set within one hop of the true value
either way on that recording.

**Read policy bounds accordingly: a margin of less than one hop (10 ms
default) from the true value sits inside the scorer's quantization noise.**
This is a disclosure of an existing property. `hotato` surfaces the margin
plainly (see `docs/FIX-PLANS.md`'s no-single-threshold rule, which the same
logic extends to quantization); the fix is to know the margin exists and set
bounds at least one hop away from a value you need to hold.

---

## Reproducing it

```bash
# render the synthetic fixtures deterministically (sha256-seeded; byte-identical
# for a fixed hotato version -- verified in CI on Linux x86_64, Python 3.10,
# 3.11, and 3.12; also now checked, not yet green, on macOS and Windows -- see
# .github/workflows/tests.yml), then run the harness over them
python3 examples/render_examples.py
PYTHONPATH=src python3 -m hotato.benchmark
```

The report has no wall-clock timestamp and the render is deterministic, so two runs
on the same code produce byte-identical artifacts. The JSON carries a `config`
snapshot of every threshold that produced the numbers, so a reader can re-derive
any value. `tests/test_benchmark.py` pins the whole thing: it asserts the harness
runs, the confusion matrix matches the known rendered behaviour, and every ms-error
is within its known, config-derived tolerance.

The synthetic fixtures are a floor: deterministic rendered audio with exact known
timings. They prove the scorer behaves as specified and guard against regressions.
See `examples/README.md` and `docs/CORPUS-GOVERNANCE.md`.

---

## Extending to your own recordings (bring your own labelled data)

The synthetic floor tells you the scorer does what the spec says. A labelled
recording from a live phone line tells you what it measures under production
conditions. The harness runs on your own labelled data with no code change:

```bash
PYTHONPATH=src python3 -m hotato.benchmark \
  --scenarios path/to/your/scenarios \
  --audio     path/to/your/audio \
  --name      my-corpus \
  --out       my-benchmark-report
```

- `--scenarios` is a directory of scenario JSON labels, same shape as
  `src/hotato/data/scenarios/*.json`. Each needs an `id`, an `expected.yield`
  label, and whatever reference timings you can defend: a `reference_render` with
  segment timings (as the synthetic fixtures carry), or at minimum a hand-labelled
  `caller_onset_sec` and the true event times you want error scored against. Supply
  a reference only where you have ground truth; the harness reports error for those
  signals and leaves the rest blank.
- `--audio` is a directory of dual-channel `<id>.example.wav` recordings (caller on
  channel 0, agent on channel 1). Record two physically separated channels whenever
  you can: that is what makes overlap ground-truthable.

When `--scenarios` is given, only that set is scored, so you get a clean report for
your corpus alone.

The path to a number from your own recordings is manual and consented: bring
your own labelled audio. Contributing that audio is governed by
`docs/CORPUS-GOVERNANCE.md` (consent, PII, data-handling) and the pipeline in
`corpus/` (a labelling schema and a validator). Synthetic fixtures keep their
synthetic label.
