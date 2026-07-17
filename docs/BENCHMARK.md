# Benchmark methodology

`hotato` ships a reproducible measurement-error harness,
`src/hotato/benchmark.py`. Given labelled dual-channel recordings, it
reports how far the scorer's measured event times land from the rendered
or hand-labelled ground truth, and how its yield/hold decisions line up
with the human labels. That is the whole output.

```bash
PYTHONPATH=src python3 -m hotato.benchmark
```

This runs on every synthetic fixture in the checkout (the bundled battery,
the `examples/` reference set, and the deliberately-bad `funnel-demo` set),
prints a markdown table, and writes `benchmark-report/measurement-error.json`
and `benchmark-report/measurement-error.md`.

The harness is a standalone measurement tool you run against fixtures; the
`hotato` CLI scores a single call or the battery.

**Scope.** This benchmark measures the timing signals: per-signal
measurement error against rendered or hand-labelled ground truth, and the
yield/hold decision. Say-do verification is scored on its own deterministic
lane, evaluated against recorded trace spans; its methodology is the
"Say-do verification methodology" section of
[`METHODOLOGY.md`](../METHODOLOGY.md), with
[`examples/reference-agent`](../examples/reference-agent) as the runnable
worked example.

---

## What it measures

For every `(recording, label)` pair it reports two things.

### 1. Per-signal measurement error, in milliseconds

For each timing signal it computes `|measured - rendered|` in ms, where
`rendered` is either the exact value the fixture was rendered from (its
`reference_render` block) or a contributor's hand label:

- **caller onset**
  - Measured: onset the VAD detects (the scorer is given no onset label).
  - Rendered: start of the first caller segment.
- **time to yield**
  - Measured: seconds from caller onset to the agent going quiet.
  - Rendered: the agent's in-progress turn end minus the caller onset.
- **response gap**
  - Measured: endpointing dead-air from the caller's turn end to the
    agent's next onset.
  - Rendered: the fixture's rendered response gap.

Errors are reported as a distribution: median, mean, worst case, best case,
and n. A signal scores only where a reference exists and the scorer
produced a value; missing either yields a `-`. (The echo-of-agent fixture
has no independent caller speech, so its onset/yield error is a gap by
construction.)

Onset is measured in **detect mode** (the scorer gets no onset hint), so it
tests the onset detector directly. Yield, talk-over, response gap, and
`did_yield` are measured in **label mode** (the scorer is given the human
`caller_onset_sec`, as the shipped battery runs) -- those are the numbers a
user sees.

### 2. A `did_yield` confusion matrix

Against the `should_yield` / `should_not_yield` label (each scenario's
`expected.yield`), it reports the four cells:

|  | measured **did_yield** | measured **held floor** |
|---|---|---|
| **should_yield** | correct yield | **missed yield** |
| **should_not_yield** | **false yield** | correct hold |

The two off-diagonal cells -- missed yields and false yields -- are the
failures an operator feels, so the report surfaces them directly.

---

## Why milliseconds and a matrix

The report pairs a per-signal error distribution with a four-cell confusion
matrix and keeps the cells separate: a missed yield and a false yield are
different failures with different fixes, so averaging them into one number
would hide which one you have. `docs/CORPUS-GOVERNANCE.md` ("Validity
metrics") enforces the same rule for corpora; the benchmark applies it to
the tooling itself.

The reported error is what the default shipped config measures, so it is
the number you get. On the synthetic fixtures, the yield error equals the
exposed VAD hangover, and the onset/gap error is one frame hop -- both
documented `ScoreConfig` parameters. Set the hangover to zero
(`caller_vad.hangover_sec = agent_vad.hangover_sec = 0`) and every signal
collapses to within one hop of ground truth; the test suite asserts this,
so the claim is checkable against the code.

The scorer reads speech energy over time, so that is what the harness
reports: timing and decisions.

---

## Quantization: every reported time has a resolution floor

Every timing signal the scorer reports is quantized to the frame hop
(`ScoreConfig.hop_ms`, default `10.0` ms) plus, for yield/talk-over, the VAD
hangover (`caller_vad.hangover_sec` / `agent_vad.hangover_sec`, default
`0.15` s): the measured value can land up to one hop off the true event,
purely from where that event falls inside a 10 ms frame, before hangover is
even counted. This sub-frame-phase rounding is deterministic -- the same
one-hop collapse the section above already pins down (hangover zero ->
every signal within one hop of ground truth).

Against a label placed at the raw end of speech energy, the measured end of
an overlap or yield sits late by at most `hangover_sec` plus one hop
(0.16 s at defaults) and the measured start sits early by at most one frame
(0.02 s at defaults). The bias is deterministic and one sided. Setting
`caller_vad.hangover_sec` and `agent_vad.hangover_sec` to 0 removes the
hangover term and leaves frame quantization; on recorded speech a zero
hangover can fragment one utterance at intra word dips, which lowers
measured overlap. Measured case: a two channel recording of two human
speakers with a hand labeled 0.420 s overlap at the raw speech edges
measures `talk_over_sec = 0.590` at defaults and `0.420` at hangover zero,
the bound holding exactly (end +0.155 s, start -0.005 s).

The consequence for a `--max-time-to-yield` (or any) policy bound: a
physically identical yield event, shifted by a few milliseconds of
sub-frame phase with nothing else changed, can cross an exact bound purely
from quantization. Reproduced case: a 250 ms yield event against a 400 ms
bound flips PASS/FAIL as the event's sub-frame phase sweeps through 3, 6,
12, and 16 ms offsets, the underlying event unchanged -- the measured value
moves by exactly one hop (10 ms) at each transition, no further. Phase
alone can flip a bound set within one hop of the true value either way, on
that recording.

**Read policy bounds accordingly: a margin under one hop (10 ms default)
from the true value sits inside the scorer's quantization noise.** `hotato`
surfaces that margin plainly (see `docs/FIX-PLANS.md`'s no-single-threshold
rule, extended here to quantization); set bounds at least one hop from any
value you need to hold.

---

## Noise floor and the verdict cliff

Below a measurable per-channel SNR the verdict flips rather than degrades.
The mechanism is one line of the energy VAD: the speech threshold is capped
at the channel's loudest frame minus `dyn_margin_db` (22 dB), so once the
noise floor climbs inside that margin every frame reads active, agent
activity never ends, and a correct yield scores as a false 3.0 s talk-over.

Measured on the reference fixture (`01-hard-interruption`, seeded noise
added to both channels): with uniform noise the yield verdict flips between
19 and 18 dB per-channel SNR; with babble-shaped noise, between 21 and
20 dB. The hardest shipped pass tier (the gold noise family) bottoms out at
a 23.8 dB noise floor, about 5 dB above the cliff.

The opt-in scorability gate covers the band below: `hotato run
--snr-gate-db` (bare flag = 22.0, which equals `dyn_margin_db`, the
geometric constant of the cliff, not a tuned number) estimates each
channel's stationary SNR deterministically and refuses to score
(not-scorable, exit 2, reason `low-snr`) when either estimate falls below
the floor, instead of emitting the false talk-over. A gated run carries the
per-channel `snr_estimate` block on the event.

The estimate certifies a stationary noise floor. Strongly non-stationary
noise (babble) can flip a verdict while estimating above the floor: the
babble flip sits 2 dB above the uniform flip on the curve above. Read a
gated pass as "the stationary floor clears the margin", and treat heavy
competing speech as its own capture problem.

---

## Reproducing it

```bash
# render the synthetic fixtures deterministically (sha256-seeded; byte-identical
# for a fixed hotato version -- verified in CI on Linux x86_64, Python 3.10,
# 3.11, and 3.12 -- see .github/workflows/tests.yml), then run the harness over them
python3 examples/render_examples.py
PYTHONPATH=src python3 -m hotato.benchmark
```

The report carries no wall-clock timestamp, and the render is
deterministic, so two runs on the same code produce byte-identical
artifacts. The JSON carries a `config` snapshot of every threshold behind
the numbers, so a reader can re-derive any value. `tests/test_benchmark.py`
pins all of it: the harness runs, the confusion matrix matches known
rendered behaviour, and every ms-error sits within its known,
config-derived tolerance.

The synthetic fixtures are a floor: deterministic rendered audio with exact
known timings, showing the scorer behaves as specified and guarding
against regressions. See `examples/README.md` and `docs/CORPUS-GOVERNANCE.md`.

---

## Extending to your own recordings (bring your own labelled data)

The synthetic floor shows the scorer does what the spec says. A labelled
recording from a live phone line shows what it measures under production
conditions. The harness runs on your own labelled data with no code
change:

```bash
PYTHONPATH=src python3 -m hotato.benchmark \
  --scenarios path/to/your/scenarios \
  --audio     path/to/your/audio \
  --name      my-corpus \
  --out       my-benchmark-report
```

- `--scenarios` is a directory of scenario JSON labels, same shape as
  `src/hotato/data/scenarios/*.json`. Each needs an `id`, an `expected.yield`
  label, and whatever reference timings you can defend: a `reference_render`
  with segment timings (as the synthetic fixtures carry), or at minimum a
  hand-labelled `caller_onset_sec` plus the true event times to score error
  against. Supply a reference only where you have ground truth -- the
  harness reports error for those signals and leaves the rest blank.
- `--audio` is a directory of dual-channel `<id>.example.wav` recordings (caller on
  channel 0, agent on channel 1). Record two physically separated channels whenever
  you can: that is what makes overlap ground-truthable.

When `--scenarios` is given, only that set is scored -- a clean report for
your corpus alone.

The path to a number from your own recordings is manual and consented:
bring your own labelled audio, governed by `docs/CORPUS-GOVERNANCE.md`
(consent, PII, data-handling) and the pipeline in `corpus/` (a labelling
schema and a validator). Synthetic fixtures keep their synthetic label.
