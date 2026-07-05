<!--
Thanks for contributing to Hotato. We review for correctness first and honesty
always. A change that's technically fine but slips in an accuracy claim gets sent
back regardless of how good the rest is. Keep the diff small and focused.
-->

## What this changes

<!-- One or two sentences. What does this PR do, and why? -->

## Type of change

- [ ] Bug fix (a number was wrong / a crash / wrong behaviour)
- [ ] New synthetic scenario or example fixture
- [ ] New real / role-played corpus recording (see the corpus section below)
- [ ] Scorer tuning or a new measured signal
- [ ] Docs
- [ ] Tooling / CI / community scaffolding

## Honesty checklist (non-negotiable — these bind code and copy)

- [ ] **No accuracy percentage** anywhere — no `accuracy %`, no invented
      benchmark result, no leaderboard, no fake stars/traction. Validity is
      millisecond measurement error and a confusion matrix (`docs/BENCHMARK.md`).
- [ ] **No fabricated numbers.** Every number is a reproducible measurement from
      real or clearly-labelled-synthetic audio, or a documented threshold.
- [ ] **Energy is not intent.** No speaker ID, diarization, transcription, or
      emotion claims — in code, comments, tests, or docs.
- [ ] **Synthetic stays synthetic.** No synthetic clip is presented as a real
      recording. `source_type` is honest.
- [ ] **The vendored `_engine` is untouched.** Scoring stays byte-identical to
      upstream; new behaviour lives in the Hotato layer around it. `python
      sync_engine.py --check` still passes (the drift gate stays green).
- [ ] **The engagement-control pointer stays vendor-neutral** — the tool's output names the kind of fix, never a product or vendor, no
      internals, no numbers, audio-only is its weaker modality and a local offline
      model is a known gap. Say so plainly if it comes up.
- [ ] **MIT, forever.** The open core is not relicensed.

## Tests

- [ ] `python -m pytest` passes locally.
- [ ] If I added a fixture, I added or extended a test that loads it through the
      public scorer and asserts its `expected` bounds.
- [ ] If I touched measurement, I checked the benchmark harness still produces
      sane ms-errors: `python3 -m hotato.benchmark`.

## Corpus contributions only (delete if not applicable)

- [ ] Audio is **dual-channel** (caller and agent on separate channels).
- [ ] `source_type` is set honestly (`real-call` / `role-played`).
- [ ] Consent is on file for **every** audible party (see
      `docs/CORPUS-GOVERNANCE.md`); the reusable release paragraph was used.
- [ ] PII is stripped per the policy; **no PHI**; redaction preserved timing.
- [ ] The label passes `python3 corpus/validate.py <label.json> <audio.wav>`.
- [ ] The `attestation` block is complete and true.

## Anything reviewers should know

<!-- Trade-offs, follow-ups, things you're unsure about. -->
