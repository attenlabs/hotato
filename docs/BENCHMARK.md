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
no independent caller speech, so it has no onset or yield error: an honest gap.)

Onset is measured in **detect mode** (the scorer gets no onset hint), so it is a
real test of the onset detector. Yield, talk-over, response gap, and `did_yield`
are measured in **label mode** (the scorer is given the human `caller_onset_sec`,
exactly as the shipped battery runs), so those are the numbers a user actually
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
one you have. The distribution and the matrix are the report. This is the rule the corpus governance doc enforces
(`docs/CORPUS-GOVERNANCE.md`, "Validity metrics"), applied to the tooling.

The reported error is what the default shipped config measures, so it is the number
a real user gets. On the synthetic fixtures the yield error equals the exposed VAD
hangover and the onset/gap error is one frame hop, both documented `ScoreConfig`
parameters. Set the hangover to zero
(`caller_vad.hangover_sec = agent_vad.hangover_sec = 0`) and every signal collapses
to within one hop of the rendered ground truth; the test suite asserts exactly
this, so the claim is checkable.

The scorer works on speech energy over time, so the harness reports timing and
decisions.

---

## Reproducing it

```bash
# render the synthetic fixtures deterministically (sha256-seeded; byte-identical
# on any machine), then run the harness over them
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

## Extending to real recordings (bring your own labelled data)

The synthetic floor tells you the scorer does what the spec says. Real recordings
tell you it measures what happens on an actual phone line. The harness runs on your
own labelled data with no code change:

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

The honest path to a real-model number is manual and consented: bring your own
labelled audio. Contributing real audio is governed by `docs/CORPUS-GOVERNANCE.md`
(consent, PII, data-handling) and the pipeline in `corpus/` (a labelling schema and
a validator). Synthetic fixtures keep their synthetic label.
