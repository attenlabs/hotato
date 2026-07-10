# Why Hotato

## Voice agents fail in ways your tests do not catch

Your test suite checks what the agent says. Production calls fail on when it
speaks. Four timing failures show up again and again in real transcripts, and
all four are invisible to text-level tests. Three are interruption patterns
(missed interruption, false stop, slow yield); the fourth is an endpointing
gap:

1. **Missed interruption.** The caller says "stop, take that off" and the agent
   keeps talking. `did_yield` is false where it must be true. The caller
   repeats themselves, louder, then hangs up.
2. **False stop.** The caller says "mhm", a backchannel: a short acknowledgement,
   not a request to take over. The agent stops mid-sentence anyway. `did_yield`
   is true where it must be false. The call stalls, then restarts, then stalls
   again.
3. **Slow yield.** The caller interrupts and the agent does stop, eventually.
   Every second of `talk_over_sec` before it stops is the caller and the agent
   speaking at once, and the caller hears all of it.
4. **Endpointing misses.** Endpointing is detecting that the caller finished
   speaking. When it misses, the caller gets dead air after they finish
   (`response_gap_sec`), or the agent starts before they are done
   (`premature_start_sec`). Both read as a broken conversation partner. The
   silence ambiguity underneath (thinking, distracted, or gone quiet) is not
   something Hotato resolves either: it measures how long the silence lasted,
   never what it meant.

Each of these lives entirely in the audio timing of the call. A transcript
diff, an LLM judge on text, and a unit test on the agent's reply all score a
call with any of these failures as perfect. The transcript reads clean; the
caller called back anyway. That gap, between a passing text-level check and a
caller who came back unhappy, is exactly what these four patterns hide.

Hotato does not infer intent. You label the expected behavior for the event:
yield means the agent should stop for the caller. hold means the agent should
keep speaking through a backchannel/noise/acknowledgement. Hotato then
measures whether the timing matched that label.

## Is this even a turn-taking bug?

In our observed reports, many alleged barge-in bugs turn out not to be
turn-taking bugs at all. Each of these produces a symptom that reads exactly like a
missed interruption or a false stop, but the fix lives in a different layer,
and no VAD threshold will ever touch it:

- **STT hallucination.** The transcript has words the caller never said, or
  drops words that mattered, so the agent responds to something that was not
  actually said. That looks like an interruption the agent ignored. Reach
  for: an STT/ASR word-error-rate check against the raw audio, not a
  turn-taking scorer.
- **Client-side audio buffering.** The caller's own device or browser queues
  outgoing audio before it reaches the agent, so "the agent talked over me"
  is audio arriving late, not the agent failing to yield. Reach for:
  client/WebRTC jitter-buffer and network-latency instrumentation, not an
  interruption-sensitivity setting.
- **LLM verbosity or tool-selection.** The agent "kept talking through the
  interruption" because it was mid-tool-call or committed to finishing a long
  generation before it re-checked for a stop signal. Reach for: response-length
  and tool-call latency tracing in your agent framework, not a VAD setting.
- **Safety false-refusal.** The agent stops abruptly mid-sentence because a
  moderation or safety layer cut it off, not because it heard a barge-in or a
  backchannel. Timing-wise this is indistinguishable from a false stop on
  "mhm". Reach for: your safety/moderation logs, not an engagement-control
  layer.
- **Wrong-language STT.** The caller is speaking a language or accent the STT
  covers poorly, recognition comes back empty or garbled, and the agent's
  response looks unrelated or missing entirely. That reads as a missed
  interruption; it is a language-coverage gap. Reach for: per-locale STT
  accuracy tooling. (Hotato's own detector is energy-based and does not
  detect language either, by design: see `corpus/classes/README.md`.)

This is both an honesty guard and a shortcut. If your bug matches one of
these five, Hotato will not find it no matter how you tune it, because it is
not a timing bug; you will save the day you would have spent staring at
`turn_end_silence_sec`. If it matches none of them: two common complaints are
agent-talks-over-caller and false-stop-on-backchannel, and that is exactly what
Hotato measures, with the funnel (no single config value fixes both directions
at once) proven on real recorded calls, not synthetic fixtures
(`corpus/vapi-defaults/README.md`).

## What makes Hotato different

- **It scores recordings you already have.** A dual-channel WAV from your
  stack is the whole input. No test harness, no synthetic caller, no
  re-architecture.
- **It runs offline.** Scoring is local and deterministic; no audio,
  transcript, or result leaves your machine.
- **It emits machine-readable timing.** One JSON envelope per run, the same
  shape from the CLI, the MCP tool, and the pytest fixture, so an agent or a
  CI job consumes one schema.
- **It fails CI.** Exit code 1 on a regression, 0 on pass, 2 on a usage error
  or a recording that is not scorable: the recording cannot answer the
  question (the caller channel is silent, or the agent was not talking when
  the caller started), so no verdict is given. A turn-taking regression blocks
  a merge the same way a failing unit test does.
- **It routes every failure to a fix class.** When the failure maps cleanly to
  stack config, `config` names the setting family on your stack and the
  direction to investigate. `engagement-control` tells you
  no threshold value can fix this failure, because telling "mhm" apart from
  "stop" is a classification problem, so you stop burning days retuning a
  setting that cannot win.

## What it does not do

No transcription. No speaker identification (a diarizer assigns anonymous
SPEAKER_00/01; it never says who a person is). No emotion or intent detection.
Hotato measures energy over time on two channels; a single-channel (mono)
recording is scorable via the opt-in, quality-gated `[diarize]` front-end
(`hotato run --mono call.wav --diarize`), labeled indicative below the
confidence bar and never equivalent to a true dual-channel measurement. The
method and its ceiling are stated in [METHODOLOGY.md](../METHODOLOGY.md) and in
the `limits` block of every result.

## Why no accuracy score?

Because accuracy would hide the thing you need to debug. A single blended
percentage tells you nothing about which call failed, on which axis, or what
to change. Hotato reports three direct measurements per event instead:

- `did_yield`: did the agent stop talking after the caller started
- `seconds_to_yield`: seconds between the caller starting and the agent stopping
- `talk_over_sec`: seconds the caller and the agent spoke at the same time

Every number is reproducible from the frame dump by hand, every threshold is
exposed, and a failure points at its fix. That is what you debug with.

Ready to pin a real failure? The loop from one bad call to a CI gate, with
`hotato fixture create`, `compare`, and `plan`:
[BAD-CALL-TO-CI.md](BAD-CALL-TO-CI.md).
