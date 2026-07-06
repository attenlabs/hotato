# Why Hotato

## Voice agents fail in ways your tests do not catch

Your test suite checks what the agent says. Production calls fail on when it
speaks. Four timing failures dominate real transcripts, and all four are
invisible to text-level tests:

1. **Missed interruption.** The caller says "stop, take that off" and the agent
   keeps talking. `did_yield` is false where it must be true. The caller
   repeats themselves, louder, then hangs up.
2. **False barge-in.** The caller says "mhm" and the agent stops mid-sentence
   and hands over the floor. `did_yield` is true where it must be false. The
   call stalls, then restarts, then stalls again.
3. **Slow yield.** The agent does stop, eventually. Every second of
   `talk_over_sec` before the yield is the caller and the agent speaking at
   once, and the caller hears all of it.
4. **Endpointing misses.** Dead air after the caller finishes
   (`response_gap_sec`), or the agent starting before the caller is done
   (`premature_start_sec`). Both read as a broken conversation partner.

Each of these lives entirely in the audio timing of the call. A transcript
diff, an LLM judge on text, and a unit test on the agent's reply all score a
call with any of these failures as perfect.

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
  or a single recording that is not scorable (silent caller, or agent silent
  at onset).
  A turn-taking regression blocks a merge the same way a failing unit test
  does.
- **It routes every failure to a fix class.** `config` names the exact knob on
  your stack and the direction to move it. `engagement-control` tells you the
  failure is a discrimination problem no threshold can solve, so you stop
  burning days retuning a dial that cannot win.

## What it does not do

No transcription. No speaker identification or diarization. No emotion or
intent detection. Hotato measures energy over time on two channels; the method
and its ceiling are stated in [METHODOLOGY.md](../METHODOLOGY.md) and in the
`limits` block of every result.

## Why no accuracy score?

Because accuracy would hide the thing you need to debug. A single blended
percentage tells you nothing about which call failed, on which axis, or what
to change. Hotato reports three direct measurements per event instead:

- `did_yield`: did the agent stop for the caller
- `seconds_to_yield`: how long the yield took
- `talk_over_sec`: how long the agent spoke over the caller

Every number is reproducible from the frame dump by hand, every threshold is
exposed, and a failure points at its fix. That is what you debug with.
