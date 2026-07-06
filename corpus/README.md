# corpus/ the real turn-taking corpus (tooling and schema)

This directory is the pipeline for the slow-compounding part of Hotato: a small,
high-integrity corpus of **real** dual-channel voice-agent calls with
human-labelled turn-taking ground truth.

The synthetic fixtures prove the scorer does what the spec says. Real recordings
prove it measures what happens on an actual phone line. The second kind moves the
needle, and it is the hardest to fake, which is exactly why it is worth doing
right.

Right now this directory ships the tooling (a labelling schema and a validator)
plus one clearly-labelled synthetic example so the mechanics are runnable. Real
audio comes from contributors, under rules that hold.

## What's here

| file | what it is |
|---|---|
| `label.schema.json` | JSON Schema (draft-07) for a contribution: the bundled scenario shape extended with provenance, consent, PII, and attestation fields. |
| `validate.py` | Standalone, stdlib-only validator for one `(recording, label)` pair. Runnable: `python3 corpus/validate.py <label.json> [audio.wav]`. |
| `examples/sample-contribution.json` | A schema example. Its `source_type` is `synthetic` and it says so everywhere: it exercises the validator. |
| `examples/sample-contribution.example.wav` | The synthetic audio for that example (a deterministic render reused from the bundled `01-hard-interruption` fixture). |

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
