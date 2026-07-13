# `hotato run --mono call.wav --diarize`: score a single-channel recording

Hotato's gold reference is a **two-channel** recording -- the caller on one
channel, the agent on the other, each channel one party, no separation needed.
That path, and every published/golden number, is unchanged. A **mono** (single,
mixed channel) recording is the coverage wall: by default it is rejected as not
scorable, because with both voices summed into one waveform the scorer cannot
attribute energy to a speaker.

The opt-in `[diarize]` front-end widens that coverage. It runs an off-the-shelf
**speaker diarizer** over the mono to recover *who was active when*, reconstructs
two caller/agent tracks, and feeds the **existing** scorer -- so a mono call
becomes scorable. It is **quality-gated** and labeled by tier: above the
confidence bar the verdict is a `diarized-mono` verdict; below it, the
verdict is labeled indicative only and no SLA gate fires; a non-separable file is
refused. A diarized-mono verdict is **never** equivalent to a true dual-channel
measurement for sub-second talk-over, and the gate enforces that per
file.

Diarization, not source separation: Hotato scores *timing* (who was active when,
and overlap), which a diarizer's turn timestamps reconstruct directly. It does
**not** reconstruct isolated waveforms, and does **not** do speaker
IDENTIFICATION -- a diarizer assigns anonymous `SPEAKER_00` / `SPEAKER_01`; it
never says who a person is.

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

With the extra (or the token/model) absent, `--diarize` errors cleanly and exits
`2`; it **never** falls back to scoring raw mono.

## Backends and extras

A pluggable backend seam (mirroring the neural-VAD seam). Pick with
`--diarizer`; install only the extra you select. The default backend is chosen by
the downstream benchmark, not pre-assumed -- `pyannote` is the accessible local
default but is **not** best on telephone, so a user who needs best-in-class picks
`sortformer` (local) or `pyannoteai` (hosted).

| `--diarizer` | extra | where it runs | notes |
|---|---|---|---|
| `pyannote` (default) | `[diarize]` | local, CPU-viable, offline | richest confidence signals (posterior + embedding margin); gated HF weights |
| `sortformer` | `[diarize-sortformer]` | local, GPU-leaning | best self-hostable on 2-speaker telephone; EEND (no embedding margin) |
| `pyannoteai` | `[diarize-hosted]` | HOSTED (audio leaves the machine) | best absolute accuracy; requires `--egress-opt-in` |

```toml
diarize            = ["pyannote.audio>=4.0", "torch>=2.8", "torchaudio>=2.8", "numpy>=1.21"]  # default; needs system ffmpeg
diarize-sortformer = ["nemo-toolkit[asr]>=2.7", "torch>=2.8", "numpy>=1.21"]                   # best self-hostable, GPU
diarize-hosted     = ["pyannoteai-sdk>=0.3"]                                                   # hosted, egress opt-in
```

The `[diarize]` path raises the effective Python floor to **>=3.10** (pyannote
4.x); the stdlib core stays >=3.9 -- this only constrains the optional path.

### Model licenses (log per FTO note)

Using off-the-shelf diarizers is integration, orthogonal to any Hotato IP claim,
but the dependency licenses are logged here and carried in the score envelope's
`diarization.licenses` block:

- `pyannote-audio` -- **MIT** (code)
- `speaker-diarization-community-1` weights -- **CC-BY-4.0** (attribution required)
- `segmentation-3.0` weights -- **MIT**; `wespeaker` embedding -- **CC-BY-4.0**
- `torch` -- **BSD-3-Clause**; `torchaudio` -- **BSD-2-Clause**
- `nemo-toolkit` -- **Apache-2.0**
- Sortformer **streaming v2** -- **CC-BY-4.0** (the offline v1 is **CC-BY-NC** ->
  non-commercial, and is never shipped)
- pyannoteAI hosted -- proprietary terms (verify before enabling egress)

If your licensing posture wants a permissive weights license, prefer
`speaker-diarization-3.1` (MIT weights) over community-1 (CC-BY-4.0) via
`HOTATO_DIARIZE_MODEL`.

## The confidence gate (the honesty core)

Aggregate diarization error is a corpus statistic; the gate is **per file, at
runtime, with no ground truth**. Six signals feed a `separation_confidence` in
`[0, 1]` and one of three tiers:

| signal | flags low quality when |
|---|---|
| speaker count == 2 | != 2: not two clean parties (1 = couldn't separate; 3+ = extra voices / mis-cluster) -> refuse |
| both speakers >= 0.30s activity | a near-silent "speaker" is a spurious split of one party -> refuse |
| mean segmentation posterior | near-chance: the model is unsure who is speaking (uncalibrated; a relative signal) |
| embedding cluster margin (pyannote only) | small: two voices too similar to attribute confidently |
| overlap ratio in a sane band | extreme: heavy crosstalk / collapsed turns -> talk-over unreliable |
| segment churn (short turns/sec) | high: a jittery, unstable timeline -> noisy timing |

**Tiers:**

- **high** -- score normally; the verdict is always tagged
  `source: "diarized-mono"` and `confidence_tier: "high"` (never presented as
  dual-channel).
- **low** -- score, but the envelope carries `indicative_only: true`: the verdict
  is "indicative only, reconstructed from single-channel diarization." **No
  pass/fail SLA gate** (`--max-talk-over` / `--max-time-to-yield`) fires on a low
  tier.
- **refuse** -- `scorable: false`, a reason naming the failed signal, exit `2`
  (exactly like today's mono rejection).

The thresholds are provisional and **uncalibrated** -- they are pinned by the
downstream verdict-agreement benchmark, not asserted as accuracy -- and are
exposed as constants in `hotato/diarize.py`.

## Caller vs agent assignment (never a silent guess)

A diarizer returns anonymous `SPEAKER_00` / `SPEAKER_01`; Hotato needs
caller/agent. The mapping is proposed, stated as an assumption, and overridable:

- **default proposal** -- reuse the floor-dominance heuristic (`trust`'s
  possible-swap band): an agent usually holds the floor longer, so the
  higher-talk-time speaker is proposed as the agent. Ambiguous (balanced floor
  time) mappings are broken by who-speaks-first and flagged `balanced: true`,
  which downgrades the verdict to indicative rather than a coin-flip.
- **override** -- `--caller-speaker SPEAKER_00 --agent-speaker SPEAKER_01`; when
  both are given no heuristic runs.

The chosen mapping and its basis are emitted in
`diarization.speaker_map: {caller, agent, basis, balanced, confidence}`.

## Echo / crosstalk is N/A on this path

The two reconstructed tracks are slices of **one physical microphone**, so
`signals.echo` / crosstalk coherence carries no echo information (it is trivially
high in overlap). On the diarized-mono path the echo block is marked
`applicable: false` and the `--echo-gate` can never fire -- it is meaningful only
for two physically separate channels.

## Limits (what stays indicative or refused)

- **very similar voices** (same gender/pitch, or one person on both ends) -> low
  embedding margin -> low/refuse.
- **heavy crosstalk / echo bleed** -> overlap balloons -> low/refuse.
- **>2 speakers** (a supervisor, hold music with vocals) -> refuse.
- **deep sustained overlap** and **sub-second boundary precision** are inherently
  weaker than dual-channel and are stamped, never hidden.
- **balanced speaker map** -> mapping uncertain -> indicative until confirmed.

A de-risk spike (with a *perfect* diarizer) confirmed the masked-reconstruction
path systematically inflates sub-second talk-over by ~0.1-0.36s and can bridge a
short backchannel gap -- an error intrinsic to single-channel masking that no
diarizer quality removes. That is precisely why elevated-overlap and short-yield
cases land in the fragile `low` zone (indicative, no SLA gate). Direct
diarization-timeline injection (skipping the reconstruction re-VAD) is the
recommended follow-up; under either approach the error budget is governed
by the gate above.

## JSON shape (agents)

A scored diarized-mono envelope is the SAME envelope as any single run, plus a
`diarization` provenance block and, when the tier is not high, `indicative_only:
true` on the event:

```json
{
  "mode": "single",
  "diarization": {
    "source": "diarized-mono",
    "backend": "pyannote",
    "model": "pyannote/speaker-diarization-community-1",
    "num_speakers": 2,
    "speaker_map": {"caller": "SPEAKER_00", "agent": "SPEAKER_01", "basis": "floor-dominance", "balanced": false},
    "separation_confidence": 0.86,
    "confidence_tier": "high",
    "overlap_ratio": 0.14,
    "licenses": {"pyannote-audio": "MIT (code)", "...": "..."}
  },
  "events": [{
    "verdict": {"did_yield": true, "seconds_to_yield": 0.5, "talk_over_sec": 0.5, "reasons": []},
    "diarization": { "...": "same block" },
    "scorability": {"separation": {"confidence_tier": "high", "separation_confidence": 0.86, "signals": {"...": "..."}}},
    "signals": {"echo": {"applicable": false, "reason": "single physical channel ..."}}
  }]
}
```

Branch on `diarization.confidence_tier`; treat any event carrying
`indicative_only: true` as indicative and never as a confident dual-channel
verdict. A refused file is `scorable: false` with `not_scorable_reason` and exit
`2`.
