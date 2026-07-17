# `hotato run --mono call.wav --diarize`: score a single-channel recording

Hotato's gold reference is **two-channel** audio: caller on one channel,
agent on the other, no separation needed. That path, and every published
number, stays unchanged. A **mono** (single, mixed) recording fails by
default: both voices sum into one waveform, so the scorer cannot attribute
energy to a speaker and rejects it as not scorable.

The opt-in `[diarize]` front-end widens that coverage: it runs an
off-the-shelf **speaker diarizer** over the mono to recover *who was active
when*, reconstructs caller/agent tracks, and hands them to the **existing**
scorer -- so a mono call becomes scorable. The result is **quality-gated**
and tiered per file: above the confidence bar it is a `diarized-mono`
verdict; below it, indicative only with no SLA gate; non-separable, it is
refused.

**Turn-timing reconstruction, from anonymous labels.** Hotato scores
*timing* -- who was active when, and overlap -- and a diarizer's turn
timestamps reconstruct that directly: anonymous `SPEAKER_00` / `SPEAKER_01`
labels per turn, handed to the scorer as boundaries. The audio stays mixed;
the labels carry only channel timing, never a name, a voice-print match, or
an identity.

The companion opt-in extra is [`docs/TRANSCRIBE.md`](TRANSCRIBE.md): it
attaches plain-text, timestamped words next to the score, computed strictly
after scoring and never fed back into it.

## Quickstart

```bash
# Install the default (local, offline) diarizer extra, plus a Hugging Face token
# with the gated model conditions accepted (one-time download, then offline):
pip install 'hotato[diarize]'
export HUGGINGFACE_TOKEN=hf_...            # accept the model card conditions first

# Check first whether the mono is confidently separable (no scoring):
hotato trust --stereo call.wav --diarize            # -> high / low / refuse tier

# Score the mono:
hotato run --mono call.wav --diarize --format json  # diarized-mono verdict
```

Without the extra (or the token/model), `--diarize` exits `2` with a clean
error -- it always requires the diarizer to run before scoring.

## Backends and extras

A pluggable backend seam, mirroring the neural-VAD seam: pick with
`--diarizer`, install only the extra you select. `pyannote` is the
accessible local default, chosen from the downstream benchmark; for
best-in-class telephone-audio accuracy, pick `sortformer` (local) or
`pyannoteai` (hosted).

- **`pyannote`** (default)
  - Extra: `[diarize]`
  - Where it runs: local, CPU-viable, offline
  - Notes: richest confidence signals (posterior + embedding margin); gated
    HF weights
- **`sortformer`**
  - Extra: `[diarize-sortformer]`
  - Where it runs: local, GPU-leaning
  - Notes: best self-hostable on 2-speaker telephone; EEND (no embedding
    margin)
- **`pyannoteai`**
  - Extra: `[diarize-hosted]`
  - Where it runs: HOSTED (audio leaves the machine)
  - Notes: best absolute accuracy; requires `--egress-opt-in`

```toml
# default; needs system ffmpeg
diarize = ["pyannote.audio>=4.0", "torch>=2.8", "torchaudio>=2.8", "numpy>=1.21"]

# best self-hostable, GPU
diarize-sortformer = ["nemo-toolkit[asr]>=2.7", "torch>=2.8", "numpy>=1.21"]

# hosted, egress opt-in
diarize-hosted = ["pyannoteai-sdk>=0.3"]
```

The `[diarize]` path raises the effective Python floor to **>=3.10**
(pyannote 4.x); the stdlib core stays >=3.9 -- this only constrains the
optional path.

### Model licenses (log per FTO note)

Off-the-shelf diarizers are an integration, orthogonal to any Hotato IP
claim; licenses are logged here and carried in the score envelope's
`diarization.licenses` block:

- `pyannote-audio`: **MIT** (code)
- `speaker-diarization-community-1` weights: **CC-BY-4.0** (attribution required)
- `segmentation-3.0` weights: **MIT**; `wespeaker` embedding: **CC-BY-4.0**
- `torch`: **BSD-3-Clause**; `torchaudio`: **BSD-2-Clause**
- `nemo-toolkit`: **Apache-2.0**
- Sortformer **streaming v2**: **CC-BY-4.0** (the offline v1 is **CC-BY-NC**,
  non-commercial, kept out of the shipped extras)
- pyannoteAI hosted: proprietary terms (verify before enabling egress)

For a permissive weights license, prefer `speaker-diarization-3.1` (MIT
weights) over community-1 (CC-BY-4.0) via `HOTATO_DIARIZE_MODEL`.

## The confidence gate (the core safeguard)

Aggregate diarization error is a corpus statistic; the gate is **per file,
at runtime, with no ground truth**. Six signals feed a
`separation_confidence` in `[0, 1]` and one of three tiers:

| signal | flags low quality when |
|---|---|
| speaker count == 2 | != 2: not two clean parties (1 = couldn't separate; 3+ = extra voices / mis-cluster) -> refuse |
| both speakers >= 0.30s activity | a near-silent "speaker" is a spurious split of one party -> refuse |
| mean segmentation posterior | near-chance: the model is unsure who is speaking (uncalibrated; a relative signal) |
| embedding cluster margin (pyannote only) | small: two voices too similar to attribute confidently |
| overlap ratio in a sane band | extreme: heavy crosstalk / collapsed turns -> talk-over unreliable |
| segment churn (short turns/sec) | high: a jittery, unstable timeline -> noisy timing |

**Tiers:**

- **high** -- scores normally, tagged `source: "diarized-mono"` and
  `confidence_tier: "high"`, distinct at every step from a dual-channel
  verdict.
- **low** -- scores with `indicative_only: true`, reading "indicative only,
  reconstructed from single-channel diarization." The pass/fail SLA gate
  (`--max-talk-over` / `--max-time-to-yield`) stays off at this tier.
- **refuse** -- `scorable: false`, a reason naming the failed signal, exit
  `2` (same as today's mono rejection).

The thresholds are provisional and **uncalibrated**, pinned by the
downstream verdict-agreement benchmark rather than asserted as accuracy, and
exposed as constants in `hotato/diarize.py`.

## Caller vs agent assignment (proposed, stated, overridable)

A diarizer returns anonymous `SPEAKER_00` / `SPEAKER_01`; Hotato needs
caller/agent. The mapping is proposed, stated as an assumption, and
overridable:

- **default proposal** -- the floor-dominance heuristic (`trust`'s
  possible-swap band): an agent usually holds the floor longer, so the
  higher-talk-time speaker is proposed as agent. A balanced (ambiguous)
  floor time is broken by who-speaks-first and flagged `balanced: true`,
  downgrading the verdict to indicative until confirmed.
- **override** -- `--caller-speaker SPEAKER_00 --agent-speaker SPEAKER_01`;
  when both are given, no heuristic runs.

The chosen mapping and its basis are emitted in
`diarization.speaker_map: {caller, agent, basis, balanced, confidence}`.

## Echo / crosstalk is N/A on this path

The two reconstructed tracks are slices of **one physical microphone**, so
`signals.echo` / crosstalk coherence carries no real signal (it reads
trivially high on overlap) and is marked `applicable: false` on this path --
`--echo-gate` only means something across two physically separate channels.

## Limits (what stays indicative or refused)

- **very similar voices** (same gender/pitch, or one person on both ends) →
  low embedding margin → low/refuse.
- **heavy crosstalk / echo bleed** → overlap balloons → low/refuse.
- **>2 speakers** (a supervisor, hold music with vocals) → refuse.
- **deep sustained overlap** and **sub-second boundary precision** run
  weaker than dual-channel and are stamped as such, never hidden.
- **balanced speaker map** → mapping uncertain → indicative until
  confirmed.

A de-risk spike, with a *perfect* diarizer, confirmed the
masked-reconstruction path inflates sub-second talk-over by ~0.1-0.36s and
can bridge a short backchannel gap -- an error intrinsic to single-channel
masking that no diarizer quality removes. That is why elevated-overlap and
short-yield cases land in the fragile `low` zone. The recommended follow-up
is direct diarization-timeline injection, skipping the reconstruction
re-VAD; either way, the gate above governs the error budget.

## JSON shape (agents)

A scored diarized-mono envelope is the SAME envelope as any single run, plus
a `diarization` provenance block and, when the tier is not high,
`indicative_only: true` on the event:

```json
{
  "mode": "single",
  "diarization": {
    "source": "diarized-mono",
    "backend": "pyannote",
    "model": "pyannote/speaker-diarization-community-1",
    "num_speakers": 2,
    "speaker_map": {
      "caller": "SPEAKER_00",
      "agent": "SPEAKER_01",
      "basis": "floor-dominance",
      "balanced": false
    },
    "separation_confidence": 0.86,
    "confidence_tier": "high",
    "overlap_ratio": 0.14,
    "licenses": {"pyannote-audio": "MIT (code)", "...": "..."}
  },
  "events": [{
    "verdict": {
      "did_yield": true,
      "seconds_to_yield": 0.5,
      "talk_over_sec": 0.5,
      "reasons": []
    },
    "diarization": { "...": "same block" },
    "scorability": {
      "separation": {
        "confidence_tier": "high",
        "separation_confidence": 0.86,
        "signals": {"...": "..."}
      }
    },
    "signals": {
      "echo": {"applicable": false, "reason": "single physical channel ..."}
    }
  }]
}
```

Branch on `diarization.confidence_tier`: an event carrying
`indicative_only: true` stays indicative-only, distinct from a confident
dual-channel verdict. A refused file is `scorable: false` with
`not_scorable_reason` and exit `2`.
