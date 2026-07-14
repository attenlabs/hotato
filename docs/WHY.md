# Why Hotato

## Voice agents fail in ways your tests do not catch

Your test suite checks what the agent says. Production calls fail on *when* it
speaks. Four timing failures show up again and again in real transcripts, and
all four are invisible to text-level tests. Three are interruption patterns
(missed interruption, false stop, slow yield); the fourth is an endpointing
gap:

1. **Missed interruption.** The caller says "stop, take that off" and the agent
   keeps talking. `did_yield` is false where it must be true. The caller
   repeats themselves, louder, then hangs up.
2. **False stop.** The caller says "mhm" -- a backchannel, an acknowledgement,
   not a request to take over. The agent stops mid-sentence anyway. `did_yield`
   is true where it must be false. The call stalls, restarts, stalls again.
3. **Slow yield.** The caller interrupts and the agent does stop, eventually.
   Every second of `talk_over_sec` before it stops is both speaking at once,
   and the caller hears all of it.
4. **Endpointing misses.** Endpointing is detecting that the caller finished
   speaking. When it misses, the caller gets dead air (`response_gap_sec`), or
   the agent starts before they are done (`premature_start_sec`). Both read as
   a broken conversation partner. Hotato measures how long the silence lasted,
   never what it meant (thinking, distracted, or gone quiet).

Each of these lives entirely in the audio timing. A transcript diff, an LLM
judge on text, and a unit test on the agent's reply all score such a call as
perfect: the transcript reads clean, the caller called back anyway. That gap is
what these four patterns hide.

You label the expected behavior: `yield` (the agent should stop for the caller)
or `hold` (it should keep speaking through a backchannel/noise/acknowledgement).
Hotato measures whether the timing matched that label -- intent is always yours
to call.

## Is this even a turn-taking bug?

In our observed reports, many alleged barge-in bugs turn out not to be
turn-taking bugs at all. Each below produces a symptom that reads exactly like a
missed interruption or false stop, but the fix lives in a different layer, and
no VAD threshold will touch it:

- **STT hallucination.** The transcript has words the caller never said, or
  drops words that mattered, so the agent responds to something unsaid. Reach
  for: an STT/ASR word-error-rate check against the raw audio.
- **Client-side audio buffering.** The caller's device or browser queues
  outgoing audio before it reaches the agent, so "the agent talked over me" is
  audio arriving late -- a transport ordering artifact. Reach for: client/WebRTC
  jitter-buffer and network-latency instrumentation.
- **LLM verbosity or tool-selection.** The agent "kept talking through the
  interruption" because it was mid-tool-call or committed to finishing a long
  generation before re-checking for a stop signal. Reach for: response-length
  and tool-call latency tracing in your agent framework.
- **Safety false-refusal.** The agent stops abruptly because a moderation layer
  cut it off, upstream of any barge-in detection. Timing-wise this is
  indistinguishable from a false stop on "mhm". Reach for: your
  safety/moderation logs.
- **Wrong-language STT.** The caller speaks a language or accent the STT covers
  poorly, recognition comes back empty or garbled, and the response looks
  unrelated. That reads as a missed interruption; it is a language-coverage
  gap. Reach for: per-locale STT accuracy tooling. (Hotato's own detector works
  from energy alone, so it measures the same regardless of language: see
  `corpus/classes/README.md`.)

This is both a scope guard and a shortcut. If your bug matches one of these
five, tuning Hotato will not surface it -- and you save the day you would have
spent staring at `turn_end_silence_sec`. If it matches none of them:
agent-talks-over-caller and false-stop-on-backchannel are exactly what Hotato
measures, with the funnel (no single config value fixes both directions at
once) proven on recorded calls, not synthetic fixtures
(`corpus/vapi-defaults/README.md`).

## What makes Hotato different

- **It scores recordings you already have.** A dual-channel WAV from your stack
  is the whole input -- no test harness, synthetic caller, or re-architecture.
- **It runs offline.** Scoring is local and deterministic; audio, transcript,
  and result all stay on your machine.
- **It emits machine-readable timing.** One JSON envelope per run, the same
  shape from the CLI, the MCP tool, and the pytest fixture.
- **It fails CI.** Exit code 1 on a regression, 0 on pass, 2 when a recording
  isn't scorable yet (the caller channel is silent, or the agent wasn't talking
  when the caller started) -- so the verdict waits for a recording that can
  answer the question. A turn-taking regression blocks a merge like a failing
  unit test.
- **It routes every failure to a fix class.** When the failure maps cleanly to
  stack config, `config` names the setting family and the direction to
  investigate. `engagement-control` names the failure as a classification
  problem -- telling "mhm" apart from "stop" -- so you spend time on the fix
  that works instead of retuning a setting that cannot win.

## What Hotato measures (and what it leaves to other tools)

Hotato measures energy over time on two channels -- that is the whole method.
Transcription, emotion, and intent detection sit outside it; a diarizer, where
used, assigns anonymous SPEAKER_00/01 labels and never identifies who a person
is. A single-channel (mono) recording is scorable via the opt-in,
quality-gated `[diarize]` front-end (`hotato run --mono call.wav --diarize`),
labeled indicative below the confidence bar, with dual-channel as the reference
standard. The method and its ceiling are stated in
[METHODOLOGY.md](../METHODOLOGY.md) and in the `limits` block of every result.

## Reproducible timing measurements, with the method exposed

A single blended percentage tells you nothing about which call failed, on which
axis, or what to change, so Hotato reports three direct measurements per event:

- `did_yield`: did the agent stop talking after the caller started
- `seconds_to_yield`: seconds between the caller starting and the agent stopping
- `talk_over_sec`: seconds the caller and the agent spoke at the same time

Every number is reproducible from the frame dump by hand, every threshold is
exposed, and a failure points at its fix. That is what you debug with.

Ready to pin a failure? The loop from one bad call to a CI gate, with `hotato
fixture create`, `compare`, and `plan`: [BAD-CALL-TO-CI.md](BAD-CALL-TO-CI.md).
