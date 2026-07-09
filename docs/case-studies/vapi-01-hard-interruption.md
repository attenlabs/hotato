# Vapi, 01-hard-interruption: a real scored moment (not a fix study)

Status: real BEFORE measurement only. This recording does NOT anchor a before/after
fix study, because at the interruption it PASSES. There is no failure here to fix
and therefore no honest "after" to record. It is kept as a real correct-behavior
baseline and as the worked example of how a moment is located and scored.

This page reports exactly what Hotato measured on the real audio. Every number
below is from the linked commands' JSON output. Nothing is reconstructed.

## Provenance

- File: `~/Projects/hotato-recordings/data/01-hard-interruption.wav`
- Duration: 32.0 s, two-channel (stereo)
- Channel map: caller = channel 0 (L), agent = channel 1 (R), verified consistent
  head-window vs full-call
- Stack: Vapi
- Assistant: `hotato-probe` (`37995a11-3b7e-41e7-87b5-198f08b6161a`),
  call_id `019f3905-f5cb-7991-a4b3-e45b8606b631`
- Model: openai/gpt-4o; voice: vapi/Elliot
- Interruption settings: Vapi defaults (`stopSpeakingPlan` and `startSpeakingPlan`
  unset)
- Captured: 2026-07-06 (MBP lane), recording dual-channel via
  `artifactPlan.recordingEnabled=true`
- sha256: `a763be90faf4fbebff4e5228f42eb58dba23d0bee2c6bc03b4a153c994816bfe`
- Scenario: caller asks about hours; agent answers in a long paragraph; caller
  cuts in loudly mid-paragraph. Expected behavior at the cut-in: yield.
- Audio is held privately (not yet cleared for public corpus).

## Step 1, locate the moment (real scan output)

```bash
hotato scan --stereo ~/Projects/hotato-recordings/data/01-hard-interruption.wav
```

```
hotato scan: 01-hard-interruption.wav  (32.0s, 4 candidate moments)
  [ 1] t=2.07s   agent_stop_no_caller         trailing silence=3.82s  no caller energy within 0.50s
  [ 2] t=6.76s   agent_stop_no_caller         trailing silence=0.82s  no caller energy within 0.50s
  [ 3] t=21.41s  overlap_while_agent_talking  overlap=0.35s  agent went silent after 0.35s
  [ 4] t=13.62s  agent_stop_no_caller         trailing silence=0.21s  no caller energy within 0.50s
```

Candidate 3 at t=21.41s is the interruption: the caller became active while the
agent was talking. The other three are the agent going quiet between sentences with
no caller energy nearby, which are not barge-in events. The real onset to score is
21.41 s.

Note on auto-onset: running `hotato run` without `--onset` auto-detects 3.27 s,
which is the caller's first energy (the initial question), not the interruption. At
3.27 s the agent is not yet talking, so a should-yield verdict is not scorable there
(`scorable: false`, exit code 2). The onset must be the interruption at 21.41 s.

## Step 2, score the moment (real run output)

```bash
hotato run --stereo ~/Projects/hotato-recordings/data/01-hard-interruption.wav \
    --onset 21.41 --expect yield --format json
```

Real verdict (from the JSON):

| field | value |
|-------|-------|
| `expected_yield` | true (label: yield) |
| `agent_talking_at_onset` | true |
| `did_yield` | true |
| `seconds_to_yield` | 0.35 |
| `talk_over_sec` | 0.35 |
| `passed` | true |
| exit code | 0 |

Secondary signals from the same run:

- Latency: `response_gap_sec` 1.49, `premature_start_sec` 0.0
- Echo: `coherence` 0.057, `echo_suspected` false
- Resume: `resumed` true, `resume_gap_sec` 3.2, `restart_suspected` true

Running the same command with `--stack vapi` produces the identical verdict
(`did_yield` true, `talk_over_sec` 0.35, `passed` true) and `fix: null`, because a
passing event has no fix to propose.

## What this measurement says

At the loud mid-paragraph interruption, the Vapi default configuration did the
right thing: the agent yielded 0.35 s after the caller took the floor, with 0.35 s
of talk-over. On the yield label this is a pass. This matches the corpus note for
this call ("agent yielded to interruption") and the corpus summary, which lists a
loud hard interrupt under behavior that already works.

One soft signal is worth recording without overstating it: `restart_suspected` is
true, with a resume 3.2 s after the yield. That is consistent with the agent
restarting its answer rather than continuing, which is a separate quality question
from barge-in timing. It does not change the barge-in verdict and is not scored as
a failure here.

## Why this is not a before/after fix study

A before/after study requires a moment that FAILS its label on the current config,
a named change, and a re-recording that PASSES. This moment already passes. There
is nothing to fix, so there is no honest "after" to capture. Publishing an "after"
for this recording would mean inventing a fix that "worked," which the standard
forbids.

This recording is still useful. It is a real correct-behavior baseline and can
serve as an opposite-risk or did-not-regress anchor in a study built around a
genuine failure elsewhere in the corpus.

## What Hotato did not prove

- It did not prove any failure on this recording. The measured result is a pass.
- It did not prove intent. The scorer read energy-based voice activity on two
  channels. The label "yield" was supplied by a human. No speech-to-text, no
  diarization, no speaker id, no emotion or intent detection.
- It did not prove anything about Vapi's accuracy. A 0.35 s yield on this one clip
  under default settings is a timing measurement on this clip, not a statement
  about the stack's detector quality in general.
- It did not measure a rate. One clip is a single sample, not a frequency.
- The `restart_suspected` signal is a soft heuristic on the agent's own track, not
  a proof that the agent restarted its answer.

## Related real failures in the same corpus (candidates, not scored studies)

The human-observed failures in this Vapi corpus are real but are harder to score as
single-onset barge-in verdicts on the energy backend:

- `02-one-word-stop.wav`: a firm one-word "Stop." that was under-yielded (the agent
  kept talking). The utterance is short and did not surface as an
  `overlap_while_agent_talking` candidate on scan, so a clean should-yield onset is
  not directly located by energy.
- `03-backchannel-single.wav` and `04-backchannel-repeated.wav`: soft backchannels
  ("mhm", "right") that a human observed the agent over-yielding to. At the one
  energy-detectable overlap onset in `03` (t=22.74 s), scored with `--expect hold`,
  the agent actually holds (`did_yield` false, `passed` true); the observed
  abandonment is a later self-stop with no caller energy nearby, which is not a
  barge-in verdict at a caller onset.

These are documented so the operator can decide which real failure to build a
publishable before/after around. See `README.md`, status and gaps, for the exact
missing pieces per study.
