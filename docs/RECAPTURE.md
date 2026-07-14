# Recapture: proving the CURRENT agent, not just the frozen recording

`hotato contract verify` and a promoted fixture in CI both re-score the SAME
audio the fixture or contract was created from. That audio never changes, so
those checks can only fail if the recorded evidence, the policy, or the
scorer changes -- never because your deployed agent got worse. Proving the
CURRENT agent still avoids a bug requires generating a NEW recording of the
same caller stimulus and scoring that. This page is the manual walkthrough
for doing that. See the two-lane table in
[`docs/CONTRACTS.md`](CONTRACTS.md#two-lanes-what-verify-proves-depends-on-which-recording-you-feed-it)
for the guarantee each lane gives.

There is no `hotato recapture` command. Reproducing a caller's stimulus
against a live agent is not something Hotato can automate: it does not place
calls, drive a caller script, or know what a caller said. This is a human
task; Hotato's job starts again once you have a new recording.

## When you need this

- After a prompt, model, or config change you believe fixes a known bug, and
  you want proof beyond "the frozen contract still fails the same way it
  always did" (it will -- the recording never changes).
- On a schedule (weekly, before a release) to catch silent regressions the
  frozen-evidence CI gate structurally cannot see.
- Before telling a customer, a teammate, or a PR reviewer "this is fixed."

If your stack can create a staging clone (`vapi`, `retell`) and you already
have `hotato apply --clone --yes`, `hotato fix trial` automates the
before/after half of this (source vs. clone, re-captured) and folds in the
neighbouring-cases check. Use this manual walkthrough when that path is not
available, or when the recapture is on a live production agent you cannot
clone.

## Step 1: read the original contract's stimulus and policy

```bash
hotato contract inspect contracts/refund-cutoff-001.hotato
```

Note, from the printed fields (or `contract.json` directly with
`--format json`):

- `label.expected_behavior` (`yield` or `hold`) -- what the agent should do.
- `policy.pass_conditions` (`max_talk_over_sec`, `max_time_to_yield_sec`) --
  the SAME thresholds the fresh recapture must be scored against, or the
  comparison is not apples-to-apples.
- `source.category` / `source/call_metadata.json` (if `--include-identifiers`
  was used at creation) -- whatever context was recorded about the caller
  scenario. Hotato does not store a transcript or a script; if you need the
  exact words a caller used, that has to come from your own call notes,
  scripted test caller, or a human re-running the same scenario.

## Step 2: reproduce the caller stimulus against the CURRENT agent

Human-assisted is fine and expected: place the call (or run the scripted
test caller) against the agent as it is deployed TODAY, using the same
scenario that produced the original recording.

## Step 3: capture dual-channel audio

Record caller and agent on separate channels (two-channel WAV, or two
aligned mono files) -- the same input requirement every other Hotato command
has. A mixed mono recording cannot be scored reliably; see
[`docs/CONTRACTS.md`](CONTRACTS.md#the-opt-in-diarized-mono-path) for the
quality-gated diarized-mono fallback if separated channels are not
available.

## Step 4: create a NEW contract from the fresh recording, same policy

```bash
hotato contract create --stereo recapture-2026-07-10.wav --onset 41.90 \
    --expect yield --id refund-cutoff-001-recapture-2026-07-10 \
    --out contracts \
    --max-talk-over 0.6 --max-time-to-yield 1.0
```

Use the SAME `--expect` and the SAME `--max-talk-over` / `--max-time-to-yield`
values Step 1 read from the original contract's policy. `contract create`
scores immediately and refuses a not-scorable recording (exit 2) rather than
writing a meaningless bundle -- so a refusal here is itself information (the
recapture didn't produce a scorable moment, not that the agent passed).

## Step 5: verify the fresh contract

```bash
hotato contract verify contracts/refund-cutoff-001-recapture-2026-07-10.hotato
```

A pass here is the claim the frozen-recording gate cannot make: the CURRENT
agent, on a fresh recording of the same stimulus, still meets the same
labelled policy. Keep both contracts -- the original (the historical record
of the bug) and the recapture (today's evidence) -- they answer different
questions and neither substitutes for the other.

## How Hotato tells a recapture from a re-score

Every run envelope Step 3's capture produces (and every `hotato contract
create`) carries an `audio_provenance` block per event: a streamed sha256 of
the raw file bytes AND a streamed sha256 of the decoded PCM samples, plus
sample rate and frame count. This is the mechanical proof that Step 2/3
happened -- a NEW recording, not the old one replayed through a
looser threshold.

`hotato fix trial` enforces this automatically, and does not trust the string:
for every guarded fixture (the fail->pass targets AND the still-passing holds)
it VALIDATES the block (well-formed hex, plausible metadata, a top-level digest
consistent with the per-side digests), RECOMPUTES the raw and decoded-PCM
sha256 from the audio when it is present next to the envelope (a mismatch is
`refused`), and compares DECODED PCM before vs. after so a header-only edit or a
trailing-byte append cannot dress a re-score up as a fresh capture (identical
decoded audio is `refused`). It also refuses an incomplete after set (a dropped
target or hold), and downgrades to `inconclusive` -- never `improved` -- when
the identity is merely asserted: malformed, missing, or well-formed but not
recomputable because the audio was not present. An `improved` verdict is never
reachable on unverifiable evidence. See
[`docs/FIX-TRIAL.md`](FIX-TRIAL.md#fresh-capture-provenance-guard-a-re-score-is-never-a-fix)
for the full guard.

`hotato contract verify` on a frozen bundle does NOT run this guard -- by
design, per the two-lane table above, it re-scores the SAME recording on
purpose (a CI regression gate on labelled evidence, not a fix claim), so
identical audio identity there is expected, not a red flag. The guard exists
specifically where a "fix" is being claimed: `fix trial`'s before/after.

## Claim language: what each kind of evidence lets you honestly say

The same word ("verified", "fixed", "passed") means something different
depending on which of these five you are holding. Match what you say to what
you have:

**Historical contract only**
- How you get it: `hotato contract create` ran once; you are reading
  `contract.json` / `hotato contract inspect`, no `verify` run since.
- Accurate to say: "On \[created_at], a human labeled this call and hotato
  measured \[timing] against that label; this is the frozen record of that
  one measurement."
- Inaccurate (common overclaim): "This proves our agent behaves correctly"
  -- nothing has been re-checked since capture; it speaks to that one
  recorded moment, not to now.

**Contract plus unchanged historical audio**
- How you get it: `hotato contract verify` re-scored the SAME
  `audio/event.wav` the contract was created from.
- Accurate to say: "`hotato contract verify` re-measured stored evidence
  and it still meets its policy." (This is the literal caveat `contract
  verify` now prints -- see below.)
- Inaccurate (common overclaim): "The deployed agent no longer has this
  bug." Per the two-lane table in
  [`docs/CONTRACTS.md`](CONTRACTS.md#two-lanes-what-verify-proves-depends-on-which-recording-you-feed-it),
  a pass here can only fail if the evidence, policy, or scorer changed,
  never because the deployed agent changed.

**Separately captured current-agent take**
- How you get it: Steps 1 to 5 above: a fresh recording of the same
  stimulus, a new contract created from it, verified once, standalone (no
  paired before/after).
- Accurate to say: "The CURRENT agent, on a fresh recording of the same
  stimulus captured \[date], still meets the labeled policy. One data
  point."
- Inaccurate (common overclaim): "This proves the fix caused the
  improvement" (coincidence, not causation) or "this guarantees the next
  call passes too" (one recapture is one data point, not a rate -- see
  Limits below).

**Fresh take plus opposite-risk cases**
- How you get it: `hotato fix trial` `improved`: paired before/after with
  verified (recomputed, freshly distinct decoded-PCM) `audio_provenance` on
  every guarded target AND hold fixture (the provenance guard held) AND the
  hold/opposite-risk fixture still passed.
- Accurate to say: "This proves the specific fresh capture scored above, at
  the revision it was captured from, including that a paired
  hold/opposite-risk case did not flip." (The literal caution `fix trial`
  prints -- see below.)
- Inaccurate (common overclaim): "This fix is now permanently verified" or
  "it will keep holding after future deploys." A later deploy is a new
  revision this report says nothing about, and nothing here re-runs itself.

**Production rerun after deploy**
- How you get it: you (there is no automatic trigger) recaptured again
  against the LIVE deployed agent post-deploy, per this page, on your own
  schedule.
- Accurate to say: "As of \[date], a fresh production capture reverified
  the same labeled stimulus against the live agent." Still one data point
  per run.
- Inaccurate (common overclaim): "The fix is confirmed working in
  production, no further checks needed." This does not run in CI by itself
  (see Limits below); each rerun is one more independent data point, not a
  standing guarantee that survives the NEXT deploy.

Two of these statements are not just guidance in this doc -- they are wired
into the command output, so a reader never has to take the caution on
faith:

- **`hotato contract verify`** (the stored-evidence row above) prints, in
  every text, HTML, and rollup render: *"This result re-measures stored
  evidence. It does not test the current agent."*
- **`hotato fix trial`**, wherever its audio-provenance section renders (an
  `improved` verdict, or a `refused`/`inconclusive` one the guard downgraded)
  prints: *"Provenance caution: this proves the specific fresh capture
  scored above, at the revision it was captured from. It does not certify a
  later deploy or every future call, and it does not re-run itself; recapture
  again after the next change."*

## Limits, stated plainly

- **This is not a controlled experiment.** A pass after a change coincides
  with the change; it does not prove causation. See
  [`docs/VALIDATION.md`](VALIDATION.md) and
  [`docs/THREAT-MODEL.md`](THREAT-MODEL.md).
- **The stimulus match is only as good as your reproduction of it.** A
  scripted test caller reproduces wording and timing more faithfully than an
  ad hoc human call; Hotato has no way to verify the fresh call
  matches the original scenario -- that judgment is yours, same as the
  original label.
- **One recapture is one data point.** A single fresh pass does not establish
  a rate; run it more than once, or fold it into a battery-scale check
  (`hotato verify --before --after`, [`docs/FIX-LOOP.md`](FIX-LOOP.md)) if
  you need a distribution rather than a single yes/no.
- **This does not run in CI by itself.** Unlike `contract verify` on the
  frozen recording, there is no automatic trigger for "go re-run the call
  against production" -- wiring that is a decision you make (a scheduled
  synthetic-caller job, a manual pre-release checklist item), not something
  Hotato ships turned on.

## What this does not stop

This is an offline tool: a user who controls every input can always lie to
themselves. Nothing on this page, and nothing `hotato fix trial`'s guard
recomputes, changes that. Specifically:

- **A fresh recording of a fabricated stimulus still passes.** If
  the "same scenario" you reproduced in Step 2 does not match the
  original bug, the audio identity check has nothing to say about it -- it
  verifies the bytes are freshly captured, never that the scenario is the
  one you claim.
- **Repacking a `.hotato` contract with a loosened policy still verifies.**
  `MANIFEST.sha256.json` is integrity (the archive agrees with itself), not
  authenticity (who approved the policy inside it). No signature is
  implemented yet.
- **A resample, re-encode, or gain change of the SAME call still reads as a
  distinct capture,** because the guard's freshness check is decoded-PCM
  difference, and those transforms change the decoded samples of a call that
  is otherwise identical. This is a known residual, not a claim broken.

See [`docs/FIX-TRIAL.md`](FIX-TRIAL.md#what-this-does-not-stop) for the same
note at length, in the context of the automated guard it applies to.

## Read more

- The two-lane distinction this page exists to close:
  [`docs/CONTRACTS.md`](CONTRACTS.md#two-lanes-what-verify-proves-depends-on-which-recording-you-feed-it)
- The automated before/after path for clone-appliable stacks:
  [`docs/FIX-TRIAL.md`](FIX-TRIAL.md) · [`docs/APPLY.md`](APPLY.md)
- Battery-scale before/after proof, coincidence not causation:
  [`docs/FIX-LOOP.md`](FIX-LOOP.md)
- What a contract does and does not prove:
  [`docs/CONTRACTS.md`](CONTRACTS.md#what-a-contract-does-not-prove)
