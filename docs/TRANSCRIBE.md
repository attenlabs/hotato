# `hotato run --stereo call.wav --transcribe`: read a transcript next to the score

Hotato's gold reference is a deterministic **energy VAD** over the audio
waveform: `did_yield`, `talk_over_sec`, and `seconds_to_yield` come from frame
energy, nothing else, and every published/golden number is computed that way.
The opt-in `[transcribe]` extra runs an off-the-shelf speech-to-text model over
the same recording and hands back plain text with per-segment timestamps, so a
human (or an agent) reading a report can see WHAT was said next to WHEN the
timing engine says it was said.

That is the entire feature. A transcript is attached as **context** beside the
score, computed strictly after scoring is final. The timing score stays grounded
in the audio: the transcript rides alongside it, never feeding back into it, and
carries no accuracy claim of its own.

## What stays unchanged

- **The timing score stays grounded in the audio.** `did_yield` /
  `talk_over_sec` / `seconds_to_yield` / every other field is computed
  identically with or without `--transcribe` -- adding it produces byte-identical
  timing numbers, pinned by a test (`tests/test_transcribe.py`), not just prose.
- **Plain text, not a quality judgment.** The transcript hands back what was
  said, not a grade on whether it was said well. What's reported for the ASR
  model is the plain text and its timestamps -- no WER, accuracy, or quality
  number, here or anywhere in hotato.
- **Timestamps only, no speaker attribution.** Plain text with timestamps, no
  caller/agent attribution -- anonymous speaker separation lives in a separate,
  unrelated extra ([`docs/DIARIZE.md`](DIARIZE.md)).

## Quickstart

```bash
# Install the opt-in extra (local, offline; no gated token, unlike [diarize]):
pip install 'hotato[transcribe]'

# Score a call and attach a transcript next to the verdict:
hotato run --stereo call.wav --transcribe --format json
```

With the extra absent, `--transcribe` always fails loud: a clean error, exit
`2`, never a silent skip of the transcript.

## Usage

```bash
hotato run --stereo call.wav --transcribe                                   # base.en, device auto-detected
hotato run --stereo call.wav --transcribe --transcribe-model small.en \
  --transcribe-device cpu                                                    # pick a model + device
hotato run --mono call.wav --diarize --transcribe                            # also works on the diarized-mono path
```

`--transcribe` needs a **single** audio file: `--stereo`, or `--mono` when
scoring through the opt-in `--diarize` front-end (it transcribes the same mono
file the diarizer separated). Two separate `--caller`/`--agent` files raise a
clean usage error naming `--stereo`, rather than guessing which channel to
transcribe or silently dropping one. Combining `--transcribe` with the bundled
self-test battery (`--suite` / `--scenarios`+`--audio`) also raises a clean
usage error, not a silent no-op, so CI never mistakes "flag ignored" for "flag
applied."

## Models and device

- Default model: `base.en` (English-only, the fastest useful size). Override
  with `--transcribe-model NAME` -- any name or local path
  `faster-whisper`/CTranslate2 accepts.
- Default device: `auto` -- `cuda` if CTranslate2 reports a CUDA device, else
  `cpu`. `compute_type` defaults to `float16` on GPU and `int8` on CPU
  (CTranslate2's documented fast defaults per device, not an accuracy claim);
  both overridable.
- Every choice used is stamped in the output (`transcript.model` / `.device` /
  `.compute_type` / `.language`), so a report is reproducible.

## Backend and extra

| extra | brings in | where it runs |
|---|---|---|
| `[transcribe]` | `faster-whisper` (a CTranslate2 re-implementation of OpenAI's Whisper) | local, CPU-viable, GPU-optional, fully offline once the model is cached |

```toml
transcribe = ["faster-whisper>=1.0"]
```

This extra keeps the core's Python floor at `>=3.9` (unlike `[diarize]`), since
`faster-whisper`/CTranslate2 support that range. There's no model card to accept
here either.

### Model licenses (log per FTO note)

Using an off-the-shelf ASR model is integration, orthogonal to any Hotato IP
claim, but the dependency licenses are logged here:

- `faster-whisper` -- **MIT** (code)
- `CTranslate2` -- **MIT** (the inference runtime `faster-whisper` runs on)
- Whisper model weights -- **MIT** (OpenAI)

## The invariant: context beside the score, grounded in the audio

A transcript is computed strictly **after** the score is final, over the same
audio the score already used, and attached on a new top-level key of the
envelope -- nothing already there is read or rewritten:

- `events`, `verdict`, `measurements`, and `signals` are the exact same values
  with or without `--transcribe`.
- The energy VAD's frame-level activity stays the ground truth for timing; a
  transcript word boundary is a separate thing -- ASR word timestamps are
  themselves a model estimate, read alongside it, never substituted in.
- A missing/broken `[transcribe]` extra raises a clean, actionable error --
  exactly like the `[neural]` and `[diarize]` seams: never a bare `ImportError`,
  a silent skip, or a fallback to a different backend.
- `hotato.transcribe` and the vendored `_engine` import nothing from each other,
  keeping the transcript path fully separate from the scorer.

## JSON shape (agents)

A `--transcribe` run is the SAME envelope as any single run, plus one additive
top-level `transcript` block. `events[0].verdict` is identical to a run without
it:

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

Branch on the presence of `transcript`: present only when `--transcribe` was
passed, additive only -- the meaning of everything under `events` stays the
same. On the diarized-mono path, `transcript` still attaches even when the
diarization confidence gate refused or downgraded the verdict; ASR runs
independent of whether diarization succeeds.

## Egress

Fully local at inference time: once the named model is cached, transcribing
opens no socket. The **first** run of a model you have not used before downloads
its weights from its public host (the same one-time fetch as installing any pip
package with model weights); every run after that is offline. See
[`docs/EGRESS.md`](EGRESS.md) for the full per-command network table.
