# `hotato trust --stereo call.wav`: is this recording even scorable?

The input-health check, or "trust doctor": inspect ONE recording and report
whether the audio is good enough to score, BEFORE you scan or run it. A bad
export (a mono file, a silent channel, a swapped channel map, a hot capture) is
caught up front, so it never turns into a confident-looking but meaningless
turn-taking verdict downstream.

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

Exit code `0` means eligible for scan; exit code `2` means NOT SCORABLE (or a usage
error / unreadable file), so `trust` composes straight into a shell gate:

```bash
hotato trust --stereo call.wav && hotato scan --stereo call.wav
```

## What it checks

By convention the caller is on channel 0 and the agent on channel 1 (override
with `--caller-channel` / `--agent-channel`). `trust` reports INPUT health only:

- **per-channel activity**: how much speech each channel carries and when each
  first speaks;
- **possible channel swap**: a heuristic flag: if the channel mapped as the
  caller holds the floor far longer than the channel mapped as the agent (the
  reverse of the usual pattern, where an assistant answers in paragraphs), the
  caller/agent channels may be reversed;
- **sample rate and duration**: the basic recording facts;
- **clipping**: per-channel peak level (dBFS) and the fraction of samples at
  full scale, so a too-hot capture is visible;
- **leading silence**: dead air before the first speech on either channel;
- **crosstalk risk**: cross-channel echo coherence: is the caller channel
  carrying a delayed copy of the agent's own audio (echo bleed / missing echo
  cancellation)?
- **cross-channel leakage** (`crosstalk_risk.leakage_db`): the level, in dB below
  the source, of a consistent attenuated delayed COPY of one channel found on the
  other. The whole-clip coherence above is a single best-lag cosine over the
  entire envelope, so unrelated activity elsewhere in the call dilutes it: bleed
  loud enough to corrupt a downstream timing verdict can sit under the coherence
  bar and go unflagged. This number is measured differently -- from the per-frame
  level ratio of the copy, which a real leak holds constant across every frame the
  source speaks -- so it catches that regime. At or above `-40 dB` (calibrated to
  the level at which symmetric bleed was red-teamed into flipping a verdict) the
  copy is loud enough to be counted as the other party's activity, so it is
  flagged and the recommendation is downgraded off `eligible for scan`;
- **low signal level**: when even the loudest channel peaks below `-30 dBFS`, the
  capture is quiet enough that turn timing can be under-measured downstream; a
  warning, never a not-scorable condition;
- **scorability**: the three things a real score needs: separated tracks, enough
  caller activity, and enough agent activity;
- **recommendation**: `eligible for scan`; `scan with caution` (scorable, but a loud
  cross-channel leak may corrupt the timing a scan produces); or `NOT SCORABLE`
  with the specific reason AND the next step to fix it.

## Scope: one question, and it stops there

`trust` answers exactly one question -- is this audio good enough to score? --
and stops: no `yield` / `hold`, no `pass` / `fail`, no `did_yield`, no
talk-over number, no intent label, no turn-taking verdict.
A recording that is eligible for scan may still contain agent bugs; finding those is
what [`hotato scan`](../src/hotato/scan.py) and `hotato run` are for.

## Not scorable

Three input defects make a recording unscorable. Each is reported with `scorable:
false`, a plain reason, and the next step, and exits `2`:

- **mono**: a single channel cannot separate the caller from the agent on its
  own. Next step: export a dual-channel recording (the gold reference), OR score
  it via the opt-in diarization front-end (see "Mono via `--diarize`" below).
- **identical channels**: a mono recording duplicated into two channels: two
  channels, but not separated. Same fix as mono.
- **a silent required channel**: for example `caller channel has no detected
  speech`. Next step: verify channel mapping or export dual-channel again.

Clipping, high leading silence, crosstalk risk, cross-channel leakage, a very low
signal level, and a possible channel swap are **warnings**: surfaced as signal,
without changing scorability by themselves. A loud cross-channel leak
additionally downgrades the recommendation to `scan with caution` -- the tracks
stay separated and scorable, so the scorability gate and exit code hold
steady; only the human-facing recommendation records that a scan's timing may
be wrong. `trust` adds signals and discloses limits -- the not-scorable
boundary, and what passes, stay fixed.

## Mono via `--diarize`: is this mono file confidently separable?

By default a mono file is not scorable (above). With the opt-in `[diarize]`
front-end, `hotato trust --stereo call.wav --diarize` reports whether the mono is
confidently SEPARABLE into caller/agent -- still WITHOUT emitting any turn-taking
verdict. It runs the selected diarizer (`--diarizer pyannote|sortformer|pyannoteai`,
default local `pyannote`), then reports a `scorability.separation` sub-block and a
confidence **tier**:

- **high**: confidently separable -- score it with `hotato run --mono call.wav
  --diarize` for a real (diarized-mono) verdict. Exit `0`.
- **low**: separable but only indicative -- e.g. voices close, overlap elevated,
  or the caller/agent mapping balanced. `hotato run --mono ... --diarize` will
  still score it, but the verdict is stamped `indicative_only` and no SLA gate
  fires. Exit `0`.
- **refuse**: not confidently separable (not two clean parties, a near-silent
  speaker, extreme overlap, voices too similar). Not scorable, exit `2`; next
  step: record a dual-channel call.

The six signals behind the tier (speaker count, per-speaker activity, mean
segmentation posterior, embedding cluster-separation margin, overlap ratio,
segment churn) are in the `separation.signals` block. A missing extra / token /
model raises a clean error (exit 2) instead of a raw-mono guess. The
dual-channel path stays the gold reference; a diarized-mono verdict stays
distinct from it. See `docs/DIARIZE.md` for the full front-end.

## JSON for agents

`--format json` emits one machine-parseable report. Branch on `scorable` (and, on
a defect, read `not_scorable_reason` and `next_step`); `exit_code` mirrors the
process exit.

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

Everything runs offline and reuses hotato's existing primitives: the hardened
WAV reader, the reference framing, the energy VAD, and the cross-channel echo
coherence, all on your machine, reporting input-health signals only -- never
an accuracy percentage.
