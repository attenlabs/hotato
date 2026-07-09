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
  => safe to scan
```

Exit code `0` means safe to scan; exit code `2` means NOT SCORABLE (or a usage
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
- **scorability**: the three things a real score needs: separated tracks, enough
  caller activity, and enough agent activity;
- **recommendation**: `safe to scan`, or `NOT SCORABLE` with the specific reason
  AND the next step to fix it.

## What it is NOT

`trust` never labels intent and never emits a turn-taking verdict. There is no
`yield` / `hold`, no `pass` / `fail`, no `did_yield`, no talk-over number. It
answers exactly one question (is this audio good enough to score?) and stops.
A recording that is safe to scan may still contain agent bugs; finding those is
what [`hotato scan`](../src/hotato/scan.py) and `hotato run` are for.

## Not scorable

Three input defects make a recording unscorable. Each is reported with `scorable:
false`, a plain reason, and the next step, and exits `2`:

- **mono**: a single channel cannot separate the caller from the agent. Next
  step: export a dual-channel recording with the caller on one channel and the
  agent on the other.
- **identical channels**: a mono recording duplicated into two channels: two
  channels, but not separated. Same fix as mono.
- **a silent required channel**: for example `caller channel has no detected
  speech`. Next step: verify channel mapping or export dual-channel again.

Clipping, high leading silence, crosstalk risk, and a possible channel swap are
**warnings**: they are surfaced but do not, by themselves, make a recording
unscorable.

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
      "caller": {"peak": 0.7906, "peak_dbfs": -2.1, "clipped_fraction": 0.0, "clipped": false},
      "agent": {"peak": 0.9016, "peak_dbfs": -0.9, "clipped_fraction": 0.0, "clipped": false}
    },
    "leading_silence_sec": 0.45
  },
  "channels": {
    "caller": {"channel": 0, "active_sec": 3.32, "first_speech_sec": 3.27, "has_speech": true, "enough_activity": true},
    "agent": {"channel": 1, "active_sec": 23.3, "first_speech_sec": 0.45, "has_speech": true, "enough_activity": true},
    "possible_swap": false,
    "swap_reason": null
  },
  "crosstalk_risk": {"coherence": 0.057, "lag_sec": 0.39, "suspected": false},
  "scorability": {"separated_tracks": true, "enough_caller_activity": true, "enough_agent_activity": true},
  "warnings": [],
  "scorable": true,
  "recommendation": "safe to scan",
  "not_scorable_reason": null,
  "next_step": null,
  "exit_code": 0
}
```

Everything runs offline and reuses hotato's existing primitives: the hardened
WAV reader, the reference framing, the energy VAD, and the cross-channel echo
coherence. No audio leaves the machine, and no accuracy percentage appears
anywhere.
