# `hotato trust --stereo call.wav`: is this recording even scorable?

The input-health check: inspect one recording and learn whether the audio is
good enough to score, before you scan or run it. `trust` catches a bad
export -- mono file, silent channel, swapped channel map, hot capture -- up
front, before it becomes a confident-looking but meaningless verdict
downstream. What each input tier supports downstream is the four-tier
policy in [EVIDENCE-CONTRACT.md](EVIDENCE-CONTRACT.md).

```bash
hotato trust --stereo your-call.wav
```

```text
hotato trust: your-call.wav
  recording: 32.0s, 44100 Hz, 2 channels
  caller (ch0): 3.32s speech, first at 3.27s, peak -2.1 dBFS
  agent  (ch1): 23.30s speech, first at 0.45s, peak -0.9 dBFS
  leading silence: 0.45s
  crosstalk: coherence 0.057 (low) at 0.39s lag
  scorability: separated tracks yes, caller activity yes, agent activity yes
  => eligible for scan
```

Exit `0` means eligible for scan; exit `2` means NOT SCORABLE (or a usage
error, or an unreadable file). `trust` composes straight into a shell gate:

```bash
hotato trust --stereo call.wav && hotato scan --stereo call.wav
```

## What it checks

By convention the caller is channel 0 and the agent channel 1 (override with
`--caller-channel` / `--agent-channel`). `trust` reports INPUT health only:

| Signal | What it means |
|---|---|
| per-channel activity | how much speech each channel carries, and when each first speaks |
| possible channel swap | a heuristic flag -- if the channel mapped as caller holds the floor far longer than the one mapped as agent (the reverse of the usual pattern, since an assistant answers in paragraphs), the channels may be reversed |
| sample rate and duration | the basic recording facts |
| clipping | per-channel peak level (dBFS) and the fraction of samples at full scale, so a too-hot capture is visible |
| leading silence | dead air before the first speech on either channel |
| crosstalk risk | cross-channel echo coherence -- is the caller channel carrying a delayed copy of the agent's own audio (echo bleed, missing echo cancellation)? |
| cross-channel leakage (`crosstalk_risk.leakage_db`) | how many dB below the source a consistent, delayed COPY of one channel shows up on the other. Whole-clip coherence (above) is one best-lag cosine over the entire envelope, diluted by unrelated activity elsewhere in the call, so bleed loud enough to corrupt a timing verdict can sit under that bar and go unflagged. Leakage measures differently: the per-frame level ratio of the copy, constant across every frame the source speaks, so it catches that regime. At or above `-40 dB` (the level at which red-teaming flipped a verdict with symmetric bleed) the copy counts as the other party's activity: flagged, and the recommendation drops off `eligible for scan` |
| low signal level | when even the loudest channel peaks below `-30 dBFS`, turn timing can be under-measured downstream -- a warning, never a not-scorable condition |
| scorability | the three things a score needs -- separated tracks, enough caller activity, enough agent activity |
| recommendation | `eligible for scan`; `scan with caution` (scorable, but a loud cross-channel leak may corrupt the scan's timing); or `NOT SCORABLE`, with the specific reason and the next step |

## Scope: one question, and it stops there

`trust` answers one question -- is this audio good enough to score? -- and
stops there: no `yield`/`hold`, no `pass`/`fail`, no `did_yield`, no
talk-over number, no intent label, no turn-taking verdict. Eligible for scan
means the input is clean, not that the agent behaved well; finding agent
bugs is [`hotato scan`](../src/hotato/scan.py)'s and `hotato run`'s job.

## Not scorable

Three input defects make a recording unscorable. Each is reported with
`scorable: false`, a plain reason, and the next step -- and exits `2`:

- **mono**: a single channel can't separate caller from agent. Next step:
  export a dual-channel recording (the gold reference), or score it via the
  opt-in diarization front-end ("Mono via `--diarize`" below);
- **identical channels**: a mono recording duplicated into two channels --
  two channels, but not separated. Same fix as mono;
- **a silent required channel**: for example `caller channel has no
  detected speech`. Next step: verify channel mapping or export dual-channel
  again.

Clipping, leading silence, crosstalk risk, cross-channel leakage, low signal
level, and a possible channel swap are **warnings**: they surface as
signal, not as a scorability change -- except a loud leak, which alone also
downgrades the recommendation to `scan with caution` (above). `trust` adds
signal and discloses limits; the not-scorable boundary stays fixed.

## Mono via `--diarize`: is this mono file confidently separable?

A mono file is not scorable by default (above). The opt-in `[diarize]`
front-end changes the question: `hotato trust --stereo call.wav --diarize`
reports whether the mono is confidently SEPARABLE into caller/agent -- still
no turn-taking verdict. It runs the selected diarizer (`--diarizer
pyannote|sortformer|pyannoteai`, default local `pyannote`), then reports a
`scorability.separation` sub-block and a confidence **tier**:

- **high**: confidently separable -- score it with `hotato run --mono
  call.wav --diarize` for a diarized-mono verdict. Exit `0`.
- **low**: separable but only indicative -- voices close, overlap elevated,
  or the mapping balanced. `hotato run --mono ... --diarize` still scores
  it, but the verdict is stamped `indicative_only` and no SLA gate fires.
  Exit `0`.
- **refuse**: not confidently separable (not two clean parties, a
  near-silent speaker, extreme overlap, voices too similar). Not scorable,
  exit `2`; next step: record dual-channel.

The six signals behind the tier -- speaker count, per-speaker activity, mean
segmentation posterior, embedding cluster-separation margin, overlap ratio,
segment churn -- are in the `separation.signals` block. A missing extra,
token, or model raises a clean error (exit 2), never a raw-mono guess.
Dual-channel stays the gold reference; a diarized-mono verdict stays
distinct from it. Full front-end: `docs/DIARIZE.md`.

## JSON for agents

`--format json` emits one machine-parseable report. Branch on `scorable`; on
a defect, read `not_scorable_reason` and `next_step`. `exit_code` mirrors
the process exit.

```bash
hotato trust --stereo call.wav --format json
```

```json
{
  "tool": "hotato",
  "kind": "input-health",
  "schema_version": "1",
  "source": "call.wav",
  "recording": {
    "sample_rate": 44100,
    "duration_sec": 32.0,
    "channels": 2,
    "clipping": {
      "caller": {
        "peak": 0.7906, "peak_dbfs": -2.1,
        "clipped_fraction": 0.0, "clipped": false
      },
      "agent": {
        "peak": 0.9016, "peak_dbfs": -0.9,
        "clipped_fraction": 0.0, "clipped": false
      }
    },
    "leading_silence_sec": 0.45
  },
  "channels": {
    "caller": {
      "channel": 0, "active_sec": 3.32, "first_speech_sec": 3.27,
      "has_speech": true, "enough_activity": true
    },
    "agent": {
      "channel": 1, "active_sec": 23.3, "first_speech_sec": 0.45,
      "has_speech": true, "enough_activity": true
    },
    "possible_swap": false,
    "swap_reason": null
  },
  "crosstalk_risk": {
    "coherence": 0.057, "lag_sec": 0.39, "suspected": false,
    "leakage_db": null, "leakage_direction": null
  },
  "scorability": {
    "separated_tracks": true, "enough_caller_activity": true,
    "enough_agent_activity": true
  },
  "warnings": [],
  "scorable": true,
  "recommendation": "eligible for scan",
  "not_scorable_reason": null,
  "next_step": null,
  "exit_code": 0
}
```

Everything runs offline, on your machine, reusing hotato's existing
primitives -- the hardened WAV reader, reference framing, energy VAD, and
cross-channel echo coherence. It reports input-health signals only, never an
accuracy percentage.
