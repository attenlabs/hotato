# `hotato run --stereo call.wav --transcribe`: read a transcript next to the score

Hotato's gold reference is a deterministic **energy VAD** over the audio
waveform: `did_yield`, `talk_over_sec`, and `seconds_to_yield` come from frame
energy, nothing else, and every published/golden number is computed exactly
that way. The opt-in `[transcribe]` extra runs an off-the-shelf speech-to-text
model over the same recording and hands back plain text with per-segment
timestamps, so a human (or an agent) reading a report can see WHAT was said
next to WHEN the timing engine says it was said.

That is the entire feature. A transcript is **context**, attached to the
output strictly after scoring is final. It is not an input to the score, it
does not refine or cross-check the timing measurement, and it carries no
accuracy claim.

## What this is not

- **Not a change to the timing score.** `did_yield` / `talk_over_sec` /
  `seconds_to_yield` / every other verdict and measurement field is computed
  identically whether or not `--transcribe` is passed. Adding `--transcribe`
  to a run produces byte-identical timing numbers to the same run without it
  -- this is pinned by a test (`tests/test_transcribe.py`), not just asserted
  in prose.
- **Not a QA or semantic judgment.** Hotato does not grade what was said,
  whether the agent understood the caller, or content quality; see "Not
  transcript scoring" in the README. No WER, accuracy, or quality number is
  claimed for the ASR model, here or anywhere in hotato.
- **Not speaker identification.** The transcript is plain text with
  timestamps; it does not attribute a segment to caller vs. agent. (Anonymous
  speaker separation is a separate, unrelated extra -- see
  [`docs/DIARIZE.md`](DIARIZE.md).)

## Quickstart

```bash
# Install the opt-in extra (local, offline; no gated token, unlike [diarize]):
pip install 'hotato[transcribe]'

# Score a call and attach a transcript next to the verdict:
hotato run --stereo call.wav --transcribe --format json
```

With the extra absent, `--transcribe` errors cleanly and exits `2`; it
**never** falls back to skipping the transcript silently.

## Usage

```bash
hotato run --stereo call.wav --transcribe                                   # base.en, device auto-detected
hotato run --stereo call.wav --transcribe --transcribe-model small.en \
  --transcribe-device cpu                                                    # pick a model + device
hotato run --mono call.wav --diarize --transcribe                            # also works on the diarized-mono path
```

`--transcribe` needs a **single** audio file to run ASR over: `--stereo`, or
`--mono` when scoring through the opt-in `--diarize` front-end (it transcribes
the same mono file the diarizer separated). Two separate `--caller`/`--agent`
files are not supported and raise a clean usage error naming `--stereo`
instead of guessing which channel to transcribe or silently dropping one.

The bundled self-test battery (`--suite` / `--scenarios`+`--audio`) does not
support `--transcribe`; combining them is a clean usage error, not a silent
no-op, so a CI script never mistakes "flag ignored" for "flag applied."

## Models and device

- Default model: `base.en` (English-only, the fastest of the useful sizes).
  Override with `--transcribe-model NAME` -- any name or local path
  `faster-whisper`/CTranslate2 accepts.
- Default device: `auto` -- picks `cuda` if CTranslate2 reports a CUDA
  device, else `cpu`. `compute_type` defaults to `float16` on GPU and `int8`
  on CPU, CTranslate2's documented fast/accurate defaults for each device,
  not an accuracy claim; both are overridable.
- Every choice used is stamped in the output
  (`transcript.model` / `transcript.device` / `transcript.compute_type` /
  `transcript.language`), so a report is reproducible.

## Backend and extra

| extra | brings in | where it runs |
|---|---|---|
| `[transcribe]` | `faster-whisper` (a CTranslate2 re-implementation of OpenAI's Whisper) | local, CPU-viable, GPU-optional, fully offline once the model is cached |

```toml
transcribe = ["faster-whisper>=1.0"]
```

Unlike `[diarize]`, this extra does not raise the core's Python floor: the
core stays `>=3.9`, and `faster-whisper`/CTranslate2 support that same range.
There is also no gated model card to accept (see the egress note below).

### Model licenses (log per FTO note)

Using an off-the-shelf ASR model is integration, orthogonal to any Hotato IP
claim, but the dependency licenses are logged here:

- `faster-whisper` -- **MIT** (code)
- `CTranslate2` -- **MIT** (the inference runtime `faster-whisper` runs on)
- Whisper model weights -- **MIT** (OpenAI)

## The honesty invariant (never touches the score)

A transcript is computed strictly **after** the score is final, over the
same audio the score already used, and is attached as a new top-level key on
the envelope -- nothing already there is read or rewritten:

- `events`, `verdict`, `measurements`, and `signals` are the exact same
  values with or without `--transcribe`.
- A transcript word boundary is not a voice-activity boundary; ASR word
  timestamps are themselves a model estimate, not ground truth, and are
  never substituted for the energy VAD's frame-level activity.
- A missing/broken `[transcribe]` extra raises a clean, actionable error
  (never a bare `ImportError`, never a silent skip, never a fallback to a
  different backend) -- exactly like the `[neural]` and `[diarize]` seams.
- `hotato.transcribe` is wired nowhere near the scorer: nothing in the
  vendored `_engine` imports it, and nothing in `_engine` is imported by it.

## JSON shape (agents)

A `--transcribe` run is the SAME envelope as any single run, plus one
additive top-level `transcript` block. `events[0].verdict` is identical to a
run without `--transcribe`:

```json
{
  "mode": "single",
  "events": [{
    "verdict": {"did_yield": true, "seconds_to_yield": 0.42, "talk_over_sec": 0.18, "reasons": []}
  }],
  "transcript": {
    "text": "I need to check on my refund -- hold on, let me pull that up for you.",
    "segments": [
      {"start": 0.10, "end": 2.30, "text": "I need to check on my refund"},
      {"start": 2.60, "end": 4.95, "text": "hold on, let me pull that up for you."}
    ],
    "model": "base.en",
    "device": "cpu",
    "compute_type": "int8",
    "language": "en"
  }
}
```

Branch on the presence of `transcript`; it is only ever present when
`--transcribe` was passed and never changes the meaning of anything under
`events`. On the diarized-mono path, `transcript` still attaches even when
the diarization confidence gate refused or downgraded the verdict -- ASR does
not depend on diarization succeeding.

## Egress

Fully local at inference time: once the named model is cached, transcribing
opens no socket. The **first** run of a model you have not used before
downloads its weights from its public host (the same one-time fetch as
installing any pip package with model weights); every run after that is
offline. See [`docs/EGRESS.md`](EGRESS.md) for the full per-command network
table.
