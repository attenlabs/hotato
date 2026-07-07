# corpus/ the turn-taking corpus (suites, tooling, schema)

This directory carries two things: the **tiered synthetic scenario suites** that
stress the scorer across 100+ labeled conditions today, and the pipeline for a
small, high-integrity corpus of **real** dual-channel voice-agent calls with
human-labelled turn-taking ground truth, the part of Hotato that grows slowly
and compounds.

The synthetic suites prove the scorer does what the spec says, across noise
floors, sample rates, gain extremes, echo, and edge timings, with every verdict
regression-tested. Real recordings prove it measures what happens on an actual
phone line. Real recordings are what make the scorer credible, and they are the
hardest thing here to fake, which is exactly why they are worth doing right.

## What's here

| file | what it is |
|---|---|
| `suites/` | Four tiered synthetic suites, 112 scenarios: `silver`, `silver-defects`, `gold`, `gold-defects`. See the next section. |
| `suites/manifest.json` | Machine-readable index: every suite, its scenario count, expected pass/fail split, and dimension coverage. |
| `suites/build_suites.py` | Deterministic builder. Regenerates every label and WAV byte-identically; `--check` proves it. |
| `classes/` | Four additional, standalone scenario classes built the same deterministic way: `mid-utterance-pause`, `backchannel-multilingual`, `noise-hold`, `telephony-degraded`. See `classes/README.md`. |
| `label.schema.json` | JSON Schema (draft-07) for a contribution: the bundled scenario shape extended with provenance, consent, PII, and attestation fields. |
| `validate.py` | Standalone, stdlib-only validator for one `(recording, label)` pair. Runnable: `python3 corpus/validate.py <label.json> [audio.wav]`. |
| `examples/sample-contribution.json` | A schema example. Its `source_type` is `synthetic` and it says so everywhere: it exercises the validator. |
| `examples/sample-contribution.example.wav` | The synthetic audio for that example (a deterministic render reused from the bundled `01-hard-interruption` fixture). |

## The tiered suites

Every scenario is synthetic and says so in its own JSON (`source_type:
"synthetic"`). The audio is deterministic shaped noise rendered from the exact
`reference_render` segment timings in the label, seeded by `sha256(id)`, so the
timings are the ground truth, two renders are byte-identical on any machine,
and no accuracy percentage is claimed anywhere. Where a scenario hardens the
conditions, the label states the exact physical parameter used: a noise floor
raised to a stated amplitude, a channel scaled by a stated gain, an echo at a
stated delay. Nothing is dressed up as a recording of a real place.

| suite | scenarios | what it holds |
|---|---|---|
| `silver` | 40 | Clean conditions at 16 kHz. Interruptions across onsets, durations, and yield speeds; one-word interrupts; single, repeated, dense, and near-miss backchannels; graceful double-talk; rapid multi-turn and re-interrupt; prompt-response latency; stutter shaped onsets. Every reference render passes. |
| `silver-defects` | 16 | The same clean conditions with deliberately bad agent renders: missed interrupts, slow yields, talk-over past bound, false barges on backchannels, stubborn double-talk, sluggish and premature responses. Every scenario fails on its labeled axis. |
| `gold` | 40 | Hard conditions: noise floor sweeps, 8 kHz telephony replicas, quiet and hot channels, echo bleed at varied delay and gain, edge timings, heavy overlap, combined conditions, and a one minute endurance case. Every reference render passes. |
| `gold-defects` | 16 | Defects under hard conditions, plus two labeled capture-defect cases (a caller below the absolute gate, a noise floor that saturates the energy VAD) that document the measurement ceiling honestly. Every scenario fails on its labeled axis. |

Run any suite with the standard CLI:

```bash
hotato run --suite barge-in \
  --scenarios corpus/suites/silver/scenarios \
  --audio corpus/suites/silver/audio
```

`silver` and `gold` exit 0. The defect suites exit 1 by design: they are what a
regression looks like, and they keep the failure detection itself under test.
Each scenario JSON carries `reference_verdict` (`pass` or `fail`) and, for
defects, `failure_axis` (`barge_in` or `latency`), so a runner can assert every
verdict individually. Latency scenarios carry their exposed bounds in
`latency_bounds` and their rendered ground truth in `reference_render`.

Rebuild or verify the whole tree:

```bash
python3 corpus/suites/build_suites.py          # write labels + render audio
python3 corpus/suites/build_suites.py --check  # regenerate to a temp dir, byte-compare
```

`tests/test_corpus_suites.py` holds the regression gate: manifest vs disk,
schema shape, every labeled verdict through `run_suite`, latency measurements
against rendered ground truth, and the byte-identical regenerate.

The suites are the runnable floor. The ceiling comes from real recordings, and
that is what the rest of this directory is for: contribute one below.

## Contribute a call (the short version)

1. **Record dual-channel.** Caller on one channel, agent on the other, physically
   separated at capture: the two legs of a SIP bridge, or two streams that never
   mix. Overlap is ground-truthable when the channels are real. Prefer two channels
   every time.
2. **Label it.** Copy the shape of `examples/sample-contribution.json` (and the
   bundled `src/hotato/data/scenarios/*.json`). Set `source_type` honestly
   (`real-call` or `role-played`), give the `caller_onset_sec`, the `expected`
   yield/hold bounds, and whatever `reference_render` timings you can defend by
   hand. Supply a reference only where you have ground truth.
3. **De-identify and get consent** (see below, this is non-negotiable).
4. **Validate.** `python3 corpus/validate.py your-label.json your-audio.wav`. It
   must PASS before you open a PR. A pass means the pair conforms; a human still
   reviews consent and PII before anything merges.
5. **Open a PR** with the contributor attestation filled in.

The validator checks structure, category/expected consistency, timings in range,
the attestation booleans, and that the audio is a readable WAV with two or more
channels. It checks conformance only; validity is reported separately as
millisecond measurement error and a confusion matrix (`docs/BENCHMARK.md`).
Consent and PII are on you and on the reviewer.

## Consent and PII: read the governing document

The rules for consent, PII removal, PHI, data-handling, and how validity is
reported are normative and live in one place:

> **[`docs/CORPUS-GOVERNANCE.md`](../docs/CORPUS-GOVERNANCE.md)**

That document is the source of truth: the reusable consent/release paragraph, the
PII strip list, the redaction rules (replace with same-duration tone so the labels
stay aligned), the no-PHI line, and the four-part contributor attestation. It is
kept in one place so the two can never drift.

In one breath: every audible party consents in writing, every identifier is
stripped, no PHI ever, and a clip enters the corpus only if it can be released
under MIT with consent. The `attestation` block in your label is where you affirm
all of that, and the validator rejects a label that leaves it unchecked.

## Synthetic stays synthetic

The bundled and example fixtures are synthetic: deterministic rendered audio. They
are a runnable floor and a regression guard. A synthetic clip in this directory
carries `source_type: "synthetic"`, says so in its title and provenance, and the
validator prints a note every time it sees one.

When real recordings arrive, they live beside their labels here with `source_type`
set to `real-call` or `role-played` and a complete consent trail, and the
synthetic examples keep their honest label next to them.

## Submit end to end

The complete walkthrough, from recording through the issue form or PR to
maintainer intake, is [`docs/SUBMITTING.md`](../docs/SUBMITTING.md).
