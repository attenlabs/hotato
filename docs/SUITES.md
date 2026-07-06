# Corpus suites: tiered, deterministic, honest about what they prove

`corpus/suites/` ships four labelled scenario suites, 112 scenarios total,
every one synthetic shaped noise rendered deterministically from its own
labelled timings (seed `sha256(scenario_id)`). The timings are the ground
truth. Synthetic audio is the floor: these suites prove the scorer runs end to
end and catch regressions; validity on your system comes from your own labelled
calls (`docs/SUBMITTING.md`).

## The four suites

| Suite | Scenarios | Conditions | Expected exit |
|---------------------|-----------|-----------------------------------------------------------------------|---------------|
| `silver` | 40 | clean, 16 kHz, default noise floor | `0` |
| `silver-defects` | 16 | clean conditions, deliberate defect renders | `1` |
| `gold` | 40 | hard conditions: noise floors, 8 kHz, gain extremes, echo, edge timings, endurance | `0` |
| `gold-defects` | 16 | hard-condition defect renders, plus two labelled capture-defect cases | `1` |

The scenario families span the behaviours that matter on a real call: hard
interruptions (onset, speed, duration, resume), backchannels, short
acknowledgements like "mhm" that the agent should talk through (varied in
position, density, repeats, and length), double-talk, one-word interruptions,
stutter onsets, multi-turn exchanges, resume-then-reinterrupt, and latency
prompts.
`corpus/suites/manifest.json` is the machine-readable inventory: per suite the
family and category breakdown, sample rates, and the expected exit code.

## Defect suites fail by design

Every scenario in `silver-defects` and `gold-defects` is rendered to fail on
its labelled axis: an agent that keeps talking through a real interruption, or
stops for a backchannel, or misses its latency budget. A defect suite that
exits `1` is the scorer catching what it claims to catch; a defect suite that
exits `0` would be a bug. This is the negative control the positive suites
need to mean anything.

## Run a suite

Any suite is just a scenarios directory plus an audio directory:

```bash
hotato run --suite barge-in \
  --scenarios corpus/suites/gold/scenarios \
  --audio corpus/suites/gold/audio
```

The same directories plug into every other surface: `hotato report ... --out
report.html` for the visual report, `hotato export ... --out research/` for
CSVs, and `pytest --hotato-suite --hotato-suite-scenarios ... --hotato-suite-audio ...`
for the session gate (`docs/PYTEST.md`).

## Deterministic builder

The suites are generated, not hand-edited, and the generator is the proof:

```bash
python3 corpus/suites/build_suites.py           # rebuild in place
python3 corpus/suites/build_suites.py --check   # regenerate to a temp dir, byte-compare
```

`--check` regenerating byte-identical output is the reproducibility guarantee:
the audio on disk is exactly what the labelled timings say it is, on any
machine. CI can run it as a drift gate.

## Against a live stack

To run scenario audio through a real voice stack and score what comes back,
see [`docs/BENCHMARK-STACKS.md`](BENCHMARK-STACKS.md). Measurement-error
methodology for the scorer itself is in [`docs/BENCHMARK.md`](BENCHMARK.md).

## Contribute scenarios

New synthetic families are welcome through the normal PR path
(`CONTRIBUTING.md`). The highest-value contribution stays a real, consented,
labelled call: the walkthrough is [`docs/SUBMITTING.md`](SUBMITTING.md) and the
[corpus-submission issue form](https://github.com/attenlabs/hotato/issues/new?template=corpus_submission.yml)
opens the intake.
