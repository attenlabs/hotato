# Case study: <stack>, <short-moment-id>

> Copy this file to `case-study-<stack>-<id>.md` and fill every field. If a field
> does not apply, write why, do not delete it. Every number must come from a
> command's output; nothing is reconstructed. If the moment already passes, this
> is a baseline, not a fix study: leave **After** empty and say so.

## Provenance

- **Stack:** the voice stack the recording came from, and its config summary (for
  a provider-default run, say so: one assistant, one config, one date, one scripted
  caller, out-of-the-box interruption settings).
- **Recording type:** <dual-channel stereo / diarized mono / other>, sample rate,
  channel map (caller channel, agent channel), sha256.
- **Call count:** <n> (one clip is a single sample, never a rate).
- **Date:** <capture date>.
- **Consent:** <who consented, on what basis; is the audio cleared for the public
  corpus, or held privately with only measurements reported?>.

## What failed

Plain-language description of the moment and the label. Expected behavior at the
onset: `yield` (agent should stop for the caller) or `hold` (agent should keep
the floor through a backchannel). One or two sentences. No stack blame.

## What Hotato measured

The real verdict from the run, as a table. Copy from the JSON output.

| field | value |
|---|---|
| `expected_yield` / label | <yield/hold> |
| `agent_talking_at_onset` | <true/false> |
| `did_yield` | <true/false> |
| `seconds_to_yield` | <s> |
| `talk_over_sec` | <s> |
| `passed` | <true/false> |
| exit code | <0/1/2> |

Secondary signals worth recording without overstating (latency, echo coherence,
resume): <...>.

## What changed

The single named change (a config knob and direction, or an engagement-control
layer). No before/after without a real, named change. If nothing changed because
the moment already passes, write "no change; baseline only."

## Before

The failing measurement on the original config: `did_yield`, `talk_over_sec`,
`seconds_to_yield`, verdict. This is the pre-change take.

## After

The measurement on the re-recorded take after the change, same label, same bounds:
`did_yield`, `talk_over_sec`, `seconds_to_yield`, verdict, and the `compare`
result word (`fixed` / `improved` / `regressed` / `worse` / `unchanged` /
`still_pass` / `not_scorable`). **Leave empty for a baseline-only study and say
why** (a moment that already passes has no honest after).

## Opposite-risk fixture

The fixture that must still hold after the change, so the fix is not a one-axis
bandaid. Example: a `hold` backchannel fixture that must not start false-yielding
after you lower the interruption threshold to fix a missed barge-in. Report its
verdict before and after.

## What Hotato did not prove

Mandatory. At minimum:

- It did not prove intent. The scorer read voice activity on separated channels;
  the label was supplied by a human. No speech-to-text, no speaker id, no emotion
  or intent detection (unless the diarized-mono front-end was used, in which case
  say so and note the verdict is `indicative_only` at the `low` tier).
- It did not prove a rate. <n> clips is a sample, not a frequency.
- It did not prove anything about the stack's detector quality in general. This
  is a timing measurement on this audio under this config, not a vendor benchmark.
- <any soft heuristic used here (resume/restart, echo) is a heuristic, not proof>.

## Repro command

The exact command(s), copy-paste runnable, that regenerate every number above.

```bash
hotato run --stereo <path> --onset <t> --expect <yield|hold> --format json
# before/after:
hotato compare --before <before.wav> --after <after.wav> --onset <t> --expect <yield|hold>
```
