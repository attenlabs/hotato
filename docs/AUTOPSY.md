# `hotato autopsy <recording>`: one call in, the incidents out

Drop one call recording in with zero config and get the incident list:
barge-in, talk-over, dead air, latency spikes, each with a timestamp and
the measured magnitude, plus one self-contained HTML report.

```bash
hotato autopsy call.wav
```

```
hotato autopsy: call.wav  (12.0s, 2 channels, stereo)
  [CRITICAL] BARGE-IN       t=2.99s  overlap=1.96s  agent did not go silent within 3.0s
      the caller took the floor and the agent kept talking over them for 1.96 s without going quiet within 3.0 s
  [WARNING]  DEAD AIR       t=6.65s  trailing silence=1.94s  no caller energy within 0.50s
      the agent went quiet for 1.94 s with no caller energy nearby
  2 incidents: 1 critical, 1 warning
  report: hotato-output/autopsy-apx-cc33f46fad58.html
  pin: apx-cc33f46fad58  (incidents apx-cc33f46fad58#1..#2)
```

WAV reads natively; mp3/m4a convert through `ffmpeg` when it is on PATH
(when it is not, the one-line message names the install command).
Everything runs offline; no audio leaves the machine. `hotato autopsy`
with no recording prints the quick start on the bundled rendered example.

## Stereo: the deterministic path

A two-channel recording (caller on one channel, agent on the other) runs
the existing whole-call scanner ([`hotato scan`](../src/hotato/scan.py))
unchanged: the same walk, the same measured numbers, byte-for-byte. The
same file produces byte-identical CLI text and a byte-identical report on
every run -- the report is even named by content (see the autopsy id
below), so the path is stable too.

The scanner's candidate kinds map to the incident vocabulary:

| Incident | From | CRITICAL when |
| :-- | :-- | :-- |
| `BARGE-IN` | the caller became active while the agent was talking (`overlap_while_agent_talking`) | the agent kept talking over the caller past the 1.0 s prompt-yield ceiling, or never went quiet in the search window |
| `TALK-OVER` | the agent started a fresh utterance over the caller (`agent_start_during_caller`) | the overlap exceeds the same 1.0 s ceiling |
| `DEAD AIR` | a response gap of 5 s or more (`long_response_gap`), or the agent stopping with no caller energy nearby (`agent_stop_no_caller`) | the gap is 5 s or more |
| `LATENCY SPIKE` | a response gap between 2 s and 5 s (`long_response_gap`) | never (a warning) |
| `ECHO SUSPECTED` | the caller channel tracks the agent's own audio (`echo_correlated_activity`) | never (a caveat) |

Each incident block carries the severity, the timestamp, the measured
magnitudes (`candidate_detail`), and the scanner's plain-English sentence
(`candidate_plain_english`) -- the same measured numbers, restated once.

## Mono: best-effort, confidence-scored

A one-channel (mixed) recording is analyzed best-effort with the same
energy VAD. One mixed channel measures silence timing -- dead air and
latency gaps -- and every mono finding carries a **measured confidence**
with its derivation printed beside it: how far the gap's mean energy sits
below the speech-activity threshold the VAD measured for this recording
(a 20 dB margin or more scores 1.00). Talk-over and barge-in attribution
comes from a two-channel recording, where the caller and the agent are
physically separated; that functional scope is stated once, on one line,
in the output. A mono gap says everything stopped, not who stopped --
nothing is guessed and no confidence is invented.

The stricter commands keep their bar: `run`, `scan`, `trust`, and the
contract path still refuse mono as NOT SCORABLE. Autopsy is discovery;
the CI gate stays deterministic and dual-channel.

An unreadable input -- a text file, a truncated header, a non-audio blob
-- is refused with the reason (exit 2), never scored.

## The report

One self-contained HTML file under `./hotato-output/`, in the
[`hotato report`](REPORTS.md) house style: the per-channel energy
waveform with one labeled marker per incident, then a card per incident
with the same measured numbers the CLI printed. Zero external requests
and zero scripts; the page renders the same bytes for the same recording
on every run.

## The autopsy id

`apx-` + the first 12 hex chars of the sha256 of the input file's bytes:
content-derived, so the same recording gets the same id on any machine,
whatever the file is named (an mp3 hashes its own bytes, so the id is
independent of the local ffmpeg build). Incidents are addressed as
`<autopsy-id>#<rank>` -- the `pin:` line prints both -- and the report is
`hotato-output/autopsy-<id>.html`.

## est. cost, only from your figures

Cost lines render only when you supply your own per-incident figures;
hotato ships no default dollar amount:

```bash
hotato autopsy call.wav --cost-config costs.json
```

```json
{"currency": "USD",
 "per_incident": {"dead-air": 3.0, "barge-in": 2.0,
                  "talk-over": 2.0, "latency-spike": 1.0}}
```

Each priced incident gets an `est. cost` line naming the kind the figure
came from, and the summary totals them. With no config, no figure
appears anywhere.

## The bundled examples

Three deterministic rendered example calls live in `examples/autopsy/`
(seeded renderer, seed = `sha256(id)`, byte-identical on any machine) --
rendered demonstrations of the failure patterns, one each:

```bash
hotato autopsy examples/autopsy/audio/autopsy-01-barge-in-say-do.example.wav
hotato autopsy examples/autopsy/audio/autopsy-02-latency-dead-air.example.wav
hotato autopsy examples/autopsy/audio/autopsy-03-talk-over.example.wav
```

## From an incident to a regression gate

An incident worth keeping fixed graduates through the existing path:

```bash
hotato investigate call.wav                  # ranked candidates + the label command
hotato investigate label .hotato/investigate-state.json#1 --expect yield
hotato contract verify contracts/ --junit hotato.xml   # the CI gate
```

## Scope and method

Deterministic energy measurement over time: per-frame RMS, a transparent
activity threshold, and the timing walk between the tracks. Timing and
floor-holding, not intent or transcription -- the scanner cannot know
whether a caller sound was "mhm" or "stop", and no accuracy percentage
appears anywhere. See [METHODOLOGY.md](../METHODOLOGY.md).
