# Validation plan

This tracks the evidence Hotato is committing to produce, and the gap on each. It
is a checklist, not a claim: an unchecked box is an honest "not yet."

The three validation jobs Hotato reports on (timing reproducibility, candidate
discovery, fixture pass-fail agreement) are defined in
[VALIDATION.md](../VALIDATION.md). This plan is about assembling the public
artifacts that let an outside reader check those jobs on real calls.

## Launch bar

The minimum evidence pack for launch:

| Artifact | Target | Status |
|---|---|---|
| External / semi-external case studies | 3 | in progress (The Table dogfood first) |
| Consented real-call fixtures | 5 | in progress |
| Before/after verify (real fix, real re-record) | 1 | blocked on a genuine failing moment plus its fixed re-recording |
| Public PR adding a promoted fixture | 1 | not started |
| Bundled demo, runnable offline | ships in 0.5.0 | done (`hotato demo`) |
| Real provider-default battery | ships in 0.5.0 | done (`corpus/vapi-defaults/`) |
| Not-scorable gallery | ships in 0.5.0 | done ([TRUST-GALLERY.md](../TRUST-GALLERY.md)) |
| Determinism check | ships in 0.5.0 | done ([VALIDATION.md](../VALIDATION.md) Job 1) |

## Case study 1: The Table (dogfood, before/after)

- **Goal:** a real failing turn-taking moment from a live The Table session, a
  named change, and a re-recorded take that passes, with an opposite-risk fixture
  that still holds.
- **Gap:** needs real multi-party turn-taking recordings. Note that
  `voice-ab/*.wav` are TTS voice samples, not sessions, so they cannot anchor a
  before/after. Coordinate capture with the The Table lane.
- **Owner:** dogfood lane. Consent is internal.

## Case studies 2 and 3: external testers

- **Goal:** two external or semi-external teams, each contributing one anonymized
  fixture and a short case study using the template.
- **Gap:** external tester sourcing (operator). Each tester needs a consented,
  dual-channel recording and permission to publish the measurements (audio may
  stay private).
- **Standard:** every study uses [case-study-TEMPLATE.md](case-study-TEMPLATE.md),
  including the mandatory "what Hotato did not prove" section.

## Consented fixtures (target 5)

- **Goal:** 5 real, labelled, dual-channel fixtures, each with consent and
  provenance recorded, promotable into a CI battery.
- **Standard:** a fixture that already passes is kept as a baseline / opposite-risk
  anchor, not dressed up as a fix. Mono or single-channel recordings are refused
  per the [trust matrix](../TRUST-MATRIX.md) unless diarized, in which case the
  verdict is marked `indicative_only`.

## Before/after verify (target 1)

- **Goal:** one end-to-end `hotato verify` proof: previously-failing fixtures now
  pass, hold fixtures still pass, reported as coincidence not causation.
- **Gap:** requires a real failing moment AND its fixed re-recording on the same
  scenario. This is the hardest artifact and the most valuable; it cannot be
  fabricated, because `compare`/`verify` refuse an unjudgeable side and a
  low-`n` battery-scale claim.

## Public PR (target 1)

- **Goal:** one open pull request that adds a promoted fixture to a repository,
  via `hotato pr create`, so the fixture-to-CI path is visible end to end.
- **Gap:** depends on at least one consented, publishable fixture landing first.

## What "done" means

Done is not "the numbers look good." Done is: an outside reader can run the
bundled demo, read the not-scorable gallery, diff two runs to confirm
determinism, and follow one before/after case study from failing recording to
passing re-record to gated CI fixture, with every number backed by a repro
command. When all four are true and the launch-bar table is fully checked, the
evidence pack stands on its own.
