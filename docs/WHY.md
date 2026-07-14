# Why Hotato

Four timing failures break a voice-agent call while every text-level test
still passes.

## Voice agents fail in ways your tests do not catch

Your test suite checks what the agent says. Production calls fail on *when*
it speaks:

1. **Missed interruption.** The caller says "stop, take that off"; the
   agent keeps talking. `did_yield` is false where it should be true --
   they repeat themselves, louder, then hang up.
2. **False stop.** The caller says "mhm" -- a backchannel, not a takeover
   request -- and the agent stops mid-sentence anyway. `did_yield` is true
   where it should be false.
3. **Slow yield.** The agent does stop, eventually. Every second of
   `talk_over_sec` before it does, the caller hears both voices at once.
4. **Endpointing misses.** The agent misjudges when the caller finished
   talking: dead air (`response_gap_sec`), or a start before they are done
   (`premature_start_sec`). Hotato measures how long the silence lasted,
   never what it meant.

Each lives in audio timing, invisible to a transcript diff, an LLM judge, or
a unit test on the reply -- all three score the call clean, and the caller
calls back anyway.

You label the expected behavior -- `yield` (stop for the caller) or `hold`
(keep speaking through a backchannel or noise). Hotato measures whether the
timing matched that label; intent is always yours to call.

## Rule out five look-alikes first

These produce the same symptom as a turn-taking bug, but no VAD threshold
touches them:

- **STT hallucination** -- the transcript has words the caller never said.
  Check ASR word-error-rate.
- **Client-side audio buffering** -- the caller's device queues audio
  before it reaches the agent, so "talked over me" is a transport delay,
  not a turn-taking one. Check WebRTC jitter-buffer/network latency.
- **LLM verbosity or tool-selection** -- the agent is mid-tool-call and
  doesn't re-check for a stop signal. Check response-length and tool-call
  latency.
- **Safety false-refusal** -- a moderation layer cut the agent off. Check
  your safety/moderation logs.
- **Wrong-language STT** -- the caller's language or accent isn't covered
  well. Check per-locale STT accuracy -- Hotato's own detector runs on
  audio energy alone, so it scores the same regardless of language
  (`corpus/classes/README.md`).

Match one of these and Hotato will not surface it -- a day saved. Match
none, and agent-talks-over-caller / false-stop-on-backchannel is exactly
what Hotato measures: no single threshold fixes both directions, shown on
recorded calls, not synthetic fixtures (`corpus/vapi-defaults/README.md`).

## What makes Hotato different

- **Scores recordings you already have.** A dual-channel WAV from your
  stack is the whole input -- no test harness, synthetic caller, or
  re-architecture.
- **Runs offline.** Scoring is local and deterministic; audio, transcript,
  and result stay on your machine.
- **Emits machine-readable timing.** One JSON envelope, the same shape from
  the CLI, the MCP tool, and the pytest fixture.
- **Fails CI.** Exit code 1 on a regression, 0 on pass, 2 when a recording
  isn't scorable yet -- a turn-taking regression blocks a merge like a
  failing unit test.
- **Routes every failure to a fix class.** `config` names the setting to
  change; `engagement-control` names it as a classification problem
  (telling "mhm" from "stop") so you don't retune a setting that can't win.

## What Hotato measures, and where it stops

Hotato measures energy over time on two channels -- that is the whole
method. Transcription, emotion, and intent sit outside it. A diarizer,
where used, assigns anonymous SPEAKER_00/01 labels, never who a person is.
A single-channel (mono) recording scores via the opt-in,
quality-gated `[diarize]` front-end (`hotato run --mono call.wav
--diarize`), labeled indicative below the confidence bar, with dual-channel
as the reference standard. Method and ceiling: [METHODOLOGY.md](../METHODOLOGY.md)
and the `limits` block of every result.

## Three numbers, reproducible by hand

A single blended percentage tells you nothing about which call failed or
what to change. Hotato reports three measurements per event:

- `did_yield`: did the agent stop talking after the caller started
- `seconds_to_yield`: seconds between the caller starting and the agent stopping
- `talk_over_sec`: seconds the caller and the agent spoke at the same time

Every number is reproducible from the frame dump by hand, every threshold
exposed -- that is what you debug with.

Ready to pin a failure? The loop from one bad call to a CI gate, with `hotato
fixture create`, `compare`, and `plan`: [BAD-CALL-TO-CI.md](BAD-CALL-TO-CI.md).
