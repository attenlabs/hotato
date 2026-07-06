# Submitting a recording

One consented, de-identified, human-labeled real call makes this eval more
credible. This is the whole path: record, label, validate, submit, and what
happens at intake.

The scorer measures speech energy over time. Your human label supplies the
meaning: which onset was a real bid for the floor, and whether a well-behaved
agent yields there. Energy is not intent, so the label is the ground truth,
and the label is what you are contributing.

## 1. Record dual-channel

Caller on one channel, agent on the other, separated at capture: the two legs
of a SIP bridge, or two streams that never mix. Real separation makes overlap
a fact of the recording, exact to the sample. Save a WAV at the call's native
sample rate (8000 Hz telephony, 16000 Hz wideband).

Consent, PII, and PHI rules are normative in
[`CORPUS-GOVERNANCE.md`](CORPUS-GOVERNANCE.md): documented consent from every
audible party, identifiers redacted with same-duration tone or silence so the
timing survives, no PHI ever. Read it before you record; it includes a
reusable release paragraph.

## 2. Label it

The label is a JSON file next to your WAV: the bundled scenario shape plus
provenance and attestation. Spec of record:
[`corpus/label.schema.json`](../corpus/label.schema.json). Worked example:
[`corpus/examples/sample-contribution.json`](../corpus/examples/sample-contribution.json).

Minimal example:

```json
{
  "id": "call-021-address-change-interrupt",
  "title": "Caller interrupts the shipping recap to change the address",
  "category": "should_yield",
  "source_type": "real-call",
  "audio": "call-021-address-change-interrupt.wav",
  "channels": { "caller_channel": 0, "agent_channel": 1 },
  "sample_rate": 8000,
  "duration_sec": 9.2,
  "caller_onset_sec": 3.15,
  "expected": {
    "yield": true,
    "max_time_to_yield_sec": 0.7,
    "max_talk_over_sec": 0.8
  },
  "license": "MIT",
  "attestation": {
    "contributor": "Your Name <you@example.com>",
    "consent_on_file": true,
    "pii_removed": true,
    "no_phi": true,
    "right_to_release_mit": true
  }
}
```

For a hold case (the caller backchannels "mm-hm" and a good agent keeps
talking): set `category` to `should_not_yield`, `expected.yield` to `false`,
and both `max_*` bounds to `null`.

Label only what you can defend by hand. `caller_onset_sec` is required;
`reference_render` segment timings are welcome wherever you have them, and
the harness reports error for exactly the signals you supply.

## 3. Validate locally

```bash
python3 corpus/validate.py your_label.json
```

The WAV resolves next to the label via its `audio` field. Pass the audio path
as a second argument if it lives elsewhere:

```bash
python3 corpus/validate.py your_label.json your_recording.wav
```

PASS (exit code 0) means the pair conforms: fields well-typed, category and
expected bounds consistent, timings in range, attestation affirmed, and the
audio a readable WAV with two or more channels that matches the label. A
human still reviews consent and PII before anything merges.

## 4. Submit

Two doors, one intake:

- **Issue form.** Open
  ["Corpus submission: labeled recording"](https://github.com/attenlabs/hotato/issues/new?template=corpus_submission.yml).
  Attach the WAV as a .zip (GitHub accepts .zip attachments; a raw .wav
  upload is rejected) or link it, and paste your label JSON in the
  description.
- **Pull request.** Add the label and WAV under [`corpus/`](../corpus/), for
  example `corpus/call-021-address-change-interrupt.json` plus its `.wav`.
  State the source type in the PR body and confirm the attestation, per
  [`CONTRIBUTING.md`](../CONTRIBUTING.md).

## What maintainers do at intake

1. **Validate.** Re-run `corpus/validate.py` on the pair and check the label
   against [`corpus/label.schema.json`](../corpus/label.schema.json).
2. **Dedupe.** Hash the audio and compare call details against existing
   clips. The corpus stays small and high-integrity; every clip earns its
   place.
3. **Normalize.** Slug the `id`, align filenames, confirm the channel map and
   declared sample rate. Timing is preserved exactly; audio is never edited
   in a way that shifts a label.
4. **Add to a suite.** Register the clip so the benchmark harness scores it
   and reports millisecond error distributions and the yield confusion matrix
   ([`BENCHMARK.md`](BENCHMARK.md)).
5. **Credit.** `attestation.contributor` is the credit of record, and
   contributors are named in the changelog when their clip lands.

Removal requests are honored in the next release, per
[`CORPUS-GOVERNANCE.md`](CORPUS-GOVERNANCE.md).
