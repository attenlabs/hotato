# Case studies: the honesty standard

A Hotato case study is a before/after record of one turn-taking failure on one
real voice stack. It exists to answer a single narrow question: did a specific,
named configuration change move a specific, measured timing failure from fail to
pass, without breaking the opposite behavior. Nothing more is claimed.

These are not testimonials and not benchmarks. Every number in a case study is a
reproducible Hotato measurement over real call audio you can re-run. If a step
cannot be run for real, the case study is not published. There are no illustrative
numbers, no reconstructed transcripts, and no "after" that was not actually
re-recorded.

## What every case study must contain

A case study is complete only when all six sections below are filled with real,
re-runnable measurements. A missing section means the study stays in draft.

1. Real dual-channel recording. The scorer requires separated caller and agent
   tracks: one two-channel WAV, or two aligned mono WAVs. A single mixed mono
   call cannot attribute talk-over reliably and does not qualify. The recording
   provenance (stack, assistant/agent id, model, voice, the exact interruption
   settings in force, capture date, sha256) is stated up front.
2. A real configuration change. One named field, with its old value and its new
   value, applied in the real stack. Threshold-only bandaids that trade one
   failure for the opposite failure are called out, not hidden.
3. Before AND after, both measured by Hotato. The same recording label, the same
   onset, the same config for the scorer, run once against the pre-change audio
   and once against the post-change re-recording. Both raw JSON outputs are
   linked.
4. An opposite-risk fixture that did not regress. If the change made the agent
   yield faster, a hold fixture (a backchannel the agent should talk through)
   must be shown still passing on the new config. If the change made the agent
   hold the floor, a yield fixture must be shown still passing. A fix that only
   moves the failure to the other side is a regression, and the study must say so.
5. The "What Hotato did not prove" section (below). Mandatory. Not optional.
6. Exit codes and commands. Every claim is backed by the literal command that
   produced it and that command's exit code, so a reader can reproduce it.

## The template

Copy this structure verbatim for each study. Fill only with measured values.

### Provenance

Stack, assistant/agent id, model, voice, the exact interruption settings, capture
date, file, duration, channel map, sha256. State whether the audio is public,
consented, or held privately.

### Before: command and result

The literal `hotato scan` used to locate the moment, then the literal `hotato run`
used to score it, then the real JSON verdict. Report `did_yield`, `talk_over_sec`,
`seconds_to_yield` (or `null`), the verdict `passed`, and the process exit code.
State the label (`--expect yield` or `--expect hold`) and why that is the correct
expected behavior for this moment. Quote no numbers that are not in the JSON.

### Change: what changed and what did NOT change

The one field, old value to new value, and where it was applied (a Vapi/Retell
REST merge-patch, or a LiveKit/Pipecat source kwarg). State explicitly what was
left untouched. If the plan called for a structural fix and only a threshold was
moved, say so and do not call the study complete.

### After: command and result

The same label and onset scored against the newly captured post-change recording.
Report the same fields as Before. The after audio must be a real re-recording of
the same scenario on the new config, not the same file re-scored.

### Opposite-risk check

The command and JSON for a fixture on the opposite axis, run on the new config,
showing it still passes. Name the fixture and its label. If it regressed, the
study reports a regression and the change is rejected.

### What Hotato did not prove

State the limits plainly for this specific study. At minimum:

- Coincidence, not causation. Hotato measured that the timing changed between two
  recordings. It did not prove the named field was the sole cause. Other run-to-run
  variance (model sampling, network, ASR endpointing) is not controlled by Hotato.
- No intent, no words. The scorer reads energy-based voice activity on two
  channels. It cannot tell "mhm" from "stop"; the human supplied the label. No
  speech-to-text, no diarization, no speaker id, no emotion or intent detection.
- No vendor accuracy claim. A pass or fail here is a timing measurement on this
  recording under this config. It is not a statement about the stack's internal
  detector quality or its accuracy in general.
- Single sample. One before and one after recording is an existence proof, not a
  rate. It does not establish how often the failure or the fix occurs across calls.
- Stereo ceiling. Numbers are the deterministic energy backend over separated
  channels. A short or quiet utterance that does not cross the energy threshold,
  or a self-stop with no caller energy nearby, may not be scorable as a barge-in
  verdict at a single onset even when a human clearly heard a failure.

## Common failure modes that disqualify a study

- Scoring the same file twice and labelling the second run "after".
- An "after" recording made on a different scenario or a different prompt.
- Reporting a pass without the opposite-risk fixture.
- A threshold moved far enough to pass the target fixture while silently
  regressing a hold fixture that is then omitted.
- Any number not present in a linked JSON output.

## Status and gaps: what each of the three studies still needs

As of this writing none of the three target studies (Vapi, Retell, LiveKit) is
complete. Each needs specific real data before it can be published. This section
is the operator's checklist.

### Vapi

- Have: 10 real dual-channel Vapi recordings at
  `~/Projects/hotato-recordings/data/` (assistant `hotato-probe`, model
  openai/gpt-4o, voice vapi/Elliot, Vapi default interruption settings,
  captured 2026-07-06, sha256-verified). Provenance is solid.
- Gap 1, a real before-FAILURE at a scorable onset. `01-hard-interruption.wav`
  was expected to be the talk-over failure. It is not: at its real interruption
  onset it PASSES (see `vapi-01-hard-interruption.md`). The human-observed
  failures in this corpus (a firm one-word "Stop." that was under-yielded in
  `02`, soft backchannels that were over-yielded in `03`/`04`) live in moments
  that are short, quiet, or are self-stops with no caller energy, so they do not
  cleanly reproduce as a single-onset barge-in verdict on the energy backend. A
  publishable Vapi before needs either a recording that fails its label at an
  energy-detectable onset, or a documented neural-backend/manual-onset scoring of
  one of the existing failures.
- Gap 2, the "after" re-recording. The tuned-assistant A/B pass (the MBP
  `TUNED-ARM.md` arm) is not on this box. The study needs the post-fix Vapi
  re-recording of the same scenario on the changed config.
- Gap 3, the named change. The exact `stopSpeakingPlan`/`startSpeakingPlan` field,
  old value to new value, that the tuned arm applied.
- Gap 4, the opposite-risk hold fixture scored on the tuned config, shown not to
  regress.

### Retell

- Have: nothing real yet.
- Gaps: a Retell account; a real dual-channel before recording of a labelled
  turn-taking failure; the named Retell config change (old to new); a post-change
  after re-recording of the same scenario; an opposite-risk fixture scored on the
  new config. All five are required before drafting.

### LiveKit (or Pipecat)

- Have: nothing real yet.
- Gaps: a LiveKit or Pipecat agent and account; a real dual-channel before
  recording of a labelled failure; the named source-level change (for example an
  `InterruptionOptions` kwarg, old value to new value) applied in the agent
  source; a post-change after re-recording of the same scenario; an opposite-risk
  fixture scored on the new build. All five are required before drafting.

Until a study has every section filled with real measurements it stays a draft
and is not linked from the site or the corpus.
