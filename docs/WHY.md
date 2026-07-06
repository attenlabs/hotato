# Why Hotato

## Voice agents fail in ways your tests do not catch

Your test suite checks what the agent says. Production calls fail on when it
speaks. Four timing failures dominate real transcripts, and all four are
invisible to text-level tests:

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
   (`premature_start_sec`). Both read as a broken conversation partner.

Each of these lives entirely in the audio timing of the call. A transcript
diff, an LLM judge on text, and a unit test on the agent's reply all score a
call with any of these failures as perfect.

Hotato does not infer intent. You label the expected behavior for the event:
yield means the agent should stop for the caller. hold means the agent should
keep speaking through a backchannel/noise/acknowledgement. Hotato then
measures whether the timing matched that label.

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
- **It routes every failure to a fix class.** `config` names the exact setting
  on your stack and the direction to move it. `engagement-control` tells you
  no threshold value can fix this failure, because telling "mhm" apart from
  "stop" is a classification problem, so you stop burning days retuning a
  setting that cannot win.

## What it does not do

No transcription. No speaker identification or diarization. No emotion or
intent detection. Hotato measures energy over time on two channels; the method
and its ceiling are stated in [METHODOLOGY.md](../METHODOLOGY.md) and in the
`limits` block of every result.

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
