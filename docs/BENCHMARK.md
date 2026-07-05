# Benchmark methodology

`hotato` ships a reproducible measurement-error harness: `src/hotato/benchmark.py`.
It exists to **produce honest numbers, not to assert them**. Given a set of
labelled dual-channel recordings, it reports how far the scorer's measured event
times land from the rendered/labelled ground truth, and how its yield/hold
decisions line up with the human labels. That is the whole output. There is no
accuracy percentage, and there is no single "score."

```bash
PYTHONPATH=src python3 -m hotato.benchmark
```

This runs on every synthetic fixture in the checkout (the bundled battery + the
`examples/` reference set + the deliberately-bad `funnel-demo` set), prints a
markdown table, and writes `benchmark-report/measurement-error.json` and
`benchmark-report/measurement-error.md`.

The harness is deliberately **not** wired into the `hotato` CLI or the packaged
entry points. It is a standalone measurement tool you run against fixtures; the
CLI scores a single call or the battery.

---

## What it measures

For every `(recording, label)` pair it reports two things and only two things.

### 1. Per-signal measurement error, in milliseconds

For each timing signal it computes `|measured − rendered|` in ms, where
`rendered` is the exact value the synthetic fixture was rendered from (its
`reference_render` block) or the value a contributor labelled by hand:

| signal | measured | rendered/true reference |
|---|---|---|
| **caller onset** | onset the VAD detects (the scorer is given **no** onset label) | start of the first caller segment |
| **time to yield** | seconds from caller onset to the agent going quiet | the agent's in-progress turn end minus the caller onset |
| **response gap** | endpointing dead-air from the caller's turn end to the agent's next onset | the fixture's rendered response gap |

Errors are reported as a **distribution** — median, mean, worst case, best case,
and n — never a single flattering statistic. A signal is only scored where a
rendered/true reference genuinely exists **and** the scorer produced a value; a
missing reference yields a `-`, never a fabricated number. (For example, the
echo-of-agent fixture has no independent caller speech, so it has no onset or
yield error at all — an honest gap, not a zero.)

Onset is measured in **detect mode** (the scorer gets no onset hint), so it is a
real test of the onset detector. Yield, talk-over, response gap, and `did_yield`
are measured in **label mode** — the scorer is given the human `caller_onset_sec`,
exactly as the shipped battery runs — so those are the numbers a user actually
sees.

### 2. A `did_yield` confusion matrix

Against the `should_yield` / `should_not_yield` label (each scenario's
`expected.yield`), it reports the four cells:

|  | measured **did_yield** | measured **held floor** |
|---|---|---|
| **should_yield** | correct yield | **missed yield** |
| **should_not_yield** | **false yield** | correct hold |

The two off-diagonal cells — missed yields (talked over a real interruption) and
false yields (stopped for a backchannel) — are the failures an operator actually
feels, so the report surfaces them directly instead of averaging them away.

---

## Why milliseconds and a matrix, not "% accurate"

Because a single accuracy percentage hides the exact trade-off that matters.

A scorer can look "95% accurate" while quietly failing the rare, expensive
missed-yield case. Collapsing the millisecond errors and the four confusion cells
into one number throws away the shape of the error and the asymmetry between the
two failure modes. So we refuse to. The distribution and the matrix **are** the
report. This is the same rule the corpus governance doc enforces
(`docs/CORPUS-GOVERNANCE.md`, "Validity metrics"), applied to the tooling.

The reported error is what the **default shipped config** measures, so it is the
number a real user gets, not a tuned-for-the-demo best case. On the synthetic
fixtures the yield error equals the exposed VAD hangover and the onset/gap error
is one frame hop — both are documented `ScoreConfig` parameters, not an accuracy
ceiling. Neutralise the hangover (`caller_vad.hangover_sec = agent_vad.hangover_sec = 0`)
and every signal collapses to within one hop of the rendered ground truth; the
test suite asserts exactly this, so the claim is checkable, not rhetorical.

Energy is not intent. The scorer sees speech-level energy over time — never
speaker identity, diarization, transcription, or emotion — so there is nothing
honest to report about those and the harness reports nothing about them.

---

## Reproducing it

```bash
# render the synthetic fixtures deterministically (sha256-seeded; byte-identical
# on any machine), then run the harness over them
python3 examples/render_examples.py
PYTHONPATH=src python3 -m hotato.benchmark
```

The report has no wall-clock timestamp and the render is deterministic, so two
runs on the same code produce byte-identical artifacts. The JSON carries a
`config` snapshot of every threshold that produced the numbers, so a reader can
re-derive any value. `tests/test_benchmark.py` pins the whole thing: it asserts
the harness runs, the confusion matrix matches the known rendered behaviour, and
every ms-error is within its known, config-derived tolerance.

The synthetic fixtures are a **floor**: deterministic rendered audio with exact
known timings. They prove the scorer behaves as specified and guard against
regressions. They are not recorded speech and are not a production-validity
claim. See `examples/README.md` and `docs/CORPUS-GOVERNANCE.md`.

---

## Extending to real recordings (bring your own labelled data)

The synthetic floor tells you the scorer does what the spec says. Only real
recordings tell you it measures what happens on an actual phone line. The harness
runs on your own labelled data with no code change:

```bash
PYTHONPATH=src python3 -m hotato.benchmark \
  --scenarios path/to/your/scenarios \
  --audio     path/to/your/audio \
  --name      my-corpus \
  --out       my-benchmark-report
```

- `--scenarios` is a directory of scenario JSON labels, same shape as
  `src/hotato/data/scenarios/*.json`. Each needs an `id`, an `expected.yield`
  label, and whatever reference timings you can defend — a `reference_render`
  with segment timings (as the synthetic fixtures carry), or at minimum a
  hand-labelled `caller_onset_sec` and the true event times you want error scored
  against. Supply a reference only where you actually have ground truth; the
  harness reports error for exactly those signals and leaves the rest blank.
- `--audio` is a directory of dual-channel `<id>.example.wav` recordings (caller
  on channel 0, agent on channel 1). Record two physically separated channels
  whenever you can — that is what makes overlap ground-truthable.

When `--scenarios` is given, only that set is scored, so you get a clean report
for your corpus alone.

**This is the only honest path to a real-model number, and it is a manual,
consented one.** The harness will never invent a "real" recording or a real-model
result. Contributing real audio is governed by `docs/CORPUS-GOVERNANCE.md`
(consent, PII, data-handling) and the pipeline in `corpus/` (a labelling schema
and a validator). Synthetic fixtures stay labelled synthetic; they are never
passed off as real.
