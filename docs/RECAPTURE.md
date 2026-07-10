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
scenario that produced the original recording. Nothing about this step is
automated by Hotato.

## Step 3: capture dual-channel audio

Record caller and agent on separate channels (two-channel WAV, or two
aligned mono files) -- the same input requirement every other Hotato command
has. A mixed mono recording cannot be scored reliably; see
[`docs/CONTRACTS.md`](CONTRACTS.md#the-opt-in-diarized-mono-path) for the
quality-gated diarized-mono fallback if separated channels are genuinely not
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

## Limits, stated plainly

- **This is not a controlled experiment.** A pass after a change coincides
  with the change; it does not prove causation. See
  [`docs/VALIDATION.md`](VALIDATION.md) and
  [`docs/THREAT-MODEL.md`](THREAT-MODEL.md).
- **The stimulus match is only as good as your reproduction of it.** A
  scripted test caller reproduces wording and timing more faithfully than an
  ad hoc human call; Hotato has no way to verify the fresh call actually
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

## Read more

- The two-lane distinction this page exists to close:
  [`docs/CONTRACTS.md`](CONTRACTS.md#two-lanes-what-verify-proves-depends-on-which-recording-you-feed-it)
- The automated before/after path for clone-appliable stacks:
  [`docs/FIX-TRIAL.md`](FIX-TRIAL.md) · [`docs/APPLY.md`](APPLY.md)
- Battery-scale before/after proof, coincidence not causation:
  [`docs/FIX-LOOP.md`](FIX-LOOP.md)
- What a contract does and does not prove:
  [`docs/CONTRACTS.md`](CONTRACTS.md#what-a-contract-does-not-prove)
