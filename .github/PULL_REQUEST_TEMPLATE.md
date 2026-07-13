<!--
Thanks for contributing to hotato. We review for correctness first, and hold the
line on the claims checklist below. A change that's technically fine but slips in
an accuracy claim gets sent back regardless. Keep the diff small and focused.
-->

## What this changes

<!-- One or two sentences: what does this PR do, and why? -->

## Type of change

- [ ] Bug fix (a number was wrong / a crash / wrong behavior)
- [ ] New synthetic scenario or example fixture
- [ ] New recorded / role-played corpus recording (see the corpus section below)
- [ ] Scorer tuning, a new measured signal, or a new deterministic check
- [ ] Docs
- [ ] Tooling / CI / community scaffolding

## Claims checklist (non-negotiable, these bind code and copy)

- [ ] **No accuracy percentage, no blended score.** No `accuracy %`, no invented
 benchmark result, no leaderboard, no `overall_score`. Validity is millisecond
 measurement error, per-dimension counts, and a confusion matrix
 ([`docs/BENCHMARK.md`](docs/BENCHMARK.md)).
- [ ] **No fabricated numbers.** Every number is a reproducible measurement from a
 recorded or clearly-labeled-synthetic input, or a documented threshold.
- [ ] **Energy is not intent.** No speaker-identity, emotion, or intent claim, in
 code, comments, tests, or docs. Transcription and diarization stay opt-in context
 layers that never feed the reference score.
- [ ] **Two lanes stay separate.** Deterministic checks stay apart from the
 model-judged rubric; a rubric verdict stays advisory.
- [ ] **Synthetic stays labeled.** No synthetic clip is presented as a recorded
 call. `source_type` matches the audio.
- [ ] **The vendored `_engine` is untouched.** Scoring stays byte-identical to
 upstream; `python sync_engine.py --check` still passes (the drift gate stays green).
- [ ] **The engagement-control pointer stays vendor-neutral.** The output names the
 kind of fix, never a product or vendor, with no internals and no numbers.
 Audio-only is its weaker modality and a local offline model is a known gap.
- [ ] **MIT, forever.** The open core is not relicensed.

## Checks

- [ ] `python -m pytest -q` passes locally.
- [ ] `python scripts/copy_lint.py` passes (plain, declarative copy; no overclaims).
- [ ] If I added a fixture, I added or extended a test that loads it through the
 public scorer and asserts its `expected` bounds.
- [ ] If I touched measurement, the benchmark harness still produces sane ms
 errors: `python3 -m hotato.benchmark`.

## Corpus contributions only (delete if not applicable)

- [ ] Audio is **dual-channel** (caller and agent on separate channels).
- [ ] `source_type` is set to match the audio (`real-call` / `role-played`).
- [ ] Consent is on file for **every** audible party (see
 [`docs/CORPUS-GOVERNANCE.md`](docs/CORPUS-GOVERNANCE.md)); the reusable release
 paragraph was used.
- [ ] PII is stripped per the policy; **no PHI**; redaction preserved timing.
- [ ] The label passes `python3 corpus/validate.py <label.json> <audio.wav>`.
- [ ] The `attestation` block is complete and affirmed.

## Anything reviewers should know

<!-- Trade-offs, follow-ups, things you're unsure about. -->
