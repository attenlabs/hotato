# Recapture: proving the CURRENT agent, not just the frozen recording

`hotato contract verify` and a promoted CI fixture both re-score the SAME
audio they were created from. That audio never changes, so those checks
fail only on changed evidence, policy, or scorer -- never because your
deployed agent changed. Proving the CURRENT agent still avoids a bug takes
a NEW recording of the same caller stimulus, scored the same way: this
page is the manual walkthrough. See the two-lane table in
[`docs/CONTRACTS.md`](CONTRACTS.md#two-lanes-what-verify-proves-depends-on-which-recording-you-feed-it)
for the guarantee each lane gives.

Reproducing a caller's stimulus against a live agent is a human task:
placing the call, running a scripted test caller, knowing what the caller
said. Hotato's job starts once you hand it the new recording.

## When you need this

- After a prompt, model, or config change you believe fixes a known bug,
  and you want proof beyond "the frozen contract still fails the same way
  it always did" (it will -- the recording never changes).
- On a schedule (weekly, before a release), to catch silent regressions
  the frozen-evidence CI gate structurally cannot see.
- Before telling a customer, a teammate, or a PR reviewer "this is fixed."

If your stack can create a staging clone (`vapi`, `retell`), `hotato fix
trial` (with `hotato apply --clone --yes`) automates the before/after half
of this -- source vs. clone, re-captured -- and folds in the
neighbouring-cases check. Use this manual walkthrough when that path
isn't available, or the recapture is on a live production agent you can't
clone.

## Step 1: read the original contract's stimulus and policy

```bash
hotato contract inspect contracts/refund-cutoff-001.hotato
```

Read from the printed fields (or `contract.json` directly with `--format
json`):

- `label.expected_behavior` (`yield` or `hold`) -- what the agent should do.
- `policy.pass_conditions` (`max_talk_over_sec`, `max_time_to_yield_sec`) --
  the SAME thresholds the fresh recapture must be scored against, for an
  apples-to-apples comparison.
- `source.category` / `source/call_metadata.json` (if
  `--include-identifiers` was used at creation) -- context recorded about
  the caller scenario. Hotato stores timing evidence, not a transcript or
  script; the caller's exact words come from your own call notes, scripted
  test caller, or a human re-running the scenario.

## Step 2: reproduce the caller stimulus against the CURRENT agent

Human-assisted is fine and expected: place the call (or run the scripted
test caller) against the agent as deployed TODAY, using the same scenario
that produced the original recording.

## Step 3: capture dual-channel audio

Record caller and agent on separate channels (two-channel WAV, or two
aligned mono files) -- the same input every other Hotato command takes. A
mixed mono recording scores unreliably; see
[`docs/CONTRACTS.md`](CONTRACTS.md#the-opt-in-diarized-mono-path) for the
quality-gated diarized-mono fallback when separated channels aren't
available.

## Step 4: create a NEW contract from the fresh recording, same policy

```bash
hotato contract create --stereo recapture-2026-07-10.wav --onset 41.90 \
    --expect yield --id refund-cutoff-001-recapture-2026-07-10 \
    --out contracts \
    --max-talk-over 0.6 --max-time-to-yield 1.0
```

Use the SAME `--expect` and `--max-talk-over` / `--max-time-to-yield`
values Step 1 read from the original policy. `contract create` scores
immediately and refuses a not-scorable recording (exit 2) rather than
writing a meaningless bundle -- a refusal here is itself information: the
recapture didn't produce a scorable moment, not that the agent passed.

## Step 5: verify the fresh contract

```bash
hotato contract verify contracts/refund-cutoff-001-recapture-2026-07-10.hotato
```

A pass here is the claim the frozen-recording gate can't make: the CURRENT
agent, on a fresh recording of the same stimulus, still meets the same
labelled policy. Keep both contracts -- the original (the historical record
of the bug) and the recapture (today's evidence) -- they answer different
questions.

## How Hotato tells a recapture from a re-score

Every run envelope Step 3's capture produces (and every `hotato contract
create`) carries an `audio_provenance` block per event: a streamed sha256
of the raw file bytes and of the decoded PCM samples, plus sample rate and
frame count -- the mechanical proof that Step 2/3 happened: a NEW
recording, not the old one replayed through a looser threshold.

`hotato fix trial` checks this block rather than trusting it: for every
guarded fixture it validates the block, recomputes the digests from the
audio on disk, and compares decoded PCM before vs. after, so a re-score
can't dress up as a fresh capture. An `improved` verdict is reachable only
on verifiable evidence; a merely-asserted or unrecomputable identity
downgrades to `inconclusive`, never `improved`. See
[`docs/FIX-TRIAL.md`](FIX-TRIAL.md#fresh-capture-provenance-guard-a-re-score-is-never-a-fix)
for the full guard, table included.

`hotato contract verify` on a frozen bundle skips this guard by design,
per the two-lane table above: it re-scores the SAME recording on purpose
(a CI regression gate on labelled evidence, not a fix claim), so identical
audio identity there is expected, not a red flag. The guard applies only
where a "fix" is being claimed: `fix trial`'s before/after.

## Claim language: what each kind of evidence lets you accurately say

The same word ("verified", "fixed", "passed") means something different
depending on which of these five you're holding. Match what you say to
what you have:

**Historical contract only**
- How you get it: `hotato contract create` ran once; you're reading
  `contract.json` / `hotato contract inspect`, no `verify` run since.
- Accurate to say: "On \[created_at], a human labeled this call and hotato
  measured \[timing] against that label; this is the frozen record of that
  one measurement."
- Overclaim to avoid: "This proves our agent behaves correctly" -- nothing
  has been re-checked since capture; it speaks to that one recorded moment,
  not to now.

**Contract plus unchanged historical audio**
- How you get it: `hotato contract verify` re-scored the SAME
  `audio/event.wav` the contract was created from.
- Accurate to say: "`hotato contract verify` re-measured stored evidence
  and it still meets its policy." (This is the literal caveat `contract
  verify` prints -- see below.)
- Overclaim to avoid: "The deployed agent no longer has this bug." A pass
  here fails only if the evidence, policy, or scorer changed -- never
  because the deployed agent changed. See the two-lane table in
  [`docs/CONTRACTS.md`](CONTRACTS.md#two-lanes-what-verify-proves-depends-on-which-recording-you-feed-it).

**Separately captured current-agent take**
- How you get it: Steps 1-5 above, standalone (no paired before/after).
- Accurate to say: "The CURRENT agent, on a fresh recording of the same
  stimulus captured \[date], still meets the labeled policy. One data
  point."
- Overclaim to avoid: "This proves the fix caused the improvement"
  (coincidence, not causation) or "this guarantees the next call passes
  too" (one recapture is one data point, not a rate -- see Limits below).

**Fresh take plus opposite-risk cases**
- How you get it: `hotato fix trial` `improved` -- the provenance guard
  held on every guarded fixture (target and hold) and the hold/opposite-
  risk fixture still passed.
- Accurate to say: "This proves the specific fresh capture scored above, at
  the revision it was captured from, including that a paired
  hold/opposite-risk case did not flip." (The literal caution `fix trial`
  prints -- see below.)
- Overclaim to avoid: "This fix is now permanently verified" or "it will
  keep holding after future deploys." A later deploy is a new revision this
  report says nothing about, and nothing here re-runs itself.

**Production rerun after deploy**
- How you get it: you (there is no automatic trigger) recaptured again
  against the LIVE deployed agent post-deploy, per this page, on your own
  schedule.
- Accurate to say: "As of \[date], a fresh production capture reverified
  the same labeled stimulus against the live agent." Still one data point
  per run.
- Overclaim to avoid: "The fix is confirmed working in production, no
  further checks needed." This doesn't run in CI by itself (see Limits
  below); each rerun is one more independent data point, not a standing
  guarantee that survives the NEXT deploy.

Two of these statements are wired straight into the command output, not
just guidance here -- a reader never has to take the caution on faith:

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

- **Not a controlled experiment.** A pass coincides with the change; it
  does not prove causation. See
  [`docs/VALIDATION.md`](VALIDATION.md) and
  [`docs/THREAT-MODEL.md`](THREAT-MODEL.md).
- **The stimulus match is only as good as your reproduction.** A scripted
  test caller reproduces wording and timing more faithfully than an ad hoc
  call; matching the fresh call to the original scenario is your judgment
  call, like the original label.
- **One recapture is one data point.** A single fresh pass doesn't
  establish a rate; run it more than once, or fold it into a battery-scale
  check (`hotato verify --before --after`,
  [`docs/FIX-LOOP.md`](FIX-LOOP.md)) for a distribution instead of a
  single yes/no.
- **This doesn't run in CI by itself.** Unlike `contract verify` on the
  frozen recording, rerunning against production has no automatic trigger
  -- wiring one (a scheduled synthetic-caller job, a pre-release checklist
  item) is your call.

## What this does not stop

This is an offline tool: a user who controls every input can shape what it
measures. Nothing on this page, and nothing `hotato fix trial`'s guard
recomputes, overrides that. Specifically:

- **A fresh recording of a fabricated stimulus still passes.** If the "same
  scenario" you reproduced in Step 2 doesn't match the original bug, the
  audio identity check has nothing to say about it -- it verifies the bytes
  are freshly captured, never that the scenario is the one you claim.
- **Repacking a `.hotato` contract with a loosened policy still verifies.**
  `MANIFEST.sha256.json` is integrity (the archive agrees with itself), not
  authenticity (who approved the policy inside it). Only a trusted
  signature over the manifest closes that gap, and none is implemented
  today; treat a bundle's origin with the same care as any file that
  arrived from outside your own pipeline.
- **A resample, re-encode, or gain change of the SAME call still reads as a
  distinct capture,** because the guard's freshness check is decoded-PCM
  difference, and those transforms change the decoded samples of an
  otherwise-identical call. A known residual, not a claim broken.

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
