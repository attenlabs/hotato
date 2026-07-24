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

## Quick start from your platform (Vapi, Retell, Bland, Synthflow, Millis)

You never touch a WAV: one command pulls your recent calls straight from
the platform's own API, checks every recording, and writes the health
report led by the Voice Stability Score.

```bash
export VAPI_API_KEY=YOUR_KEY          # or: hotato connect vapi
hotato vapi health
```

```
hotato vapi health: pulled 38 of 38 listed calls (0 skipped, last 7d) -> hotato-output/vapi-calls
hotato scan: vapi-calls  (38 recordings: 38 analyzed, 0 refused)
  Voice Stability Score: 74/100  (38 dual-channel calls; policy 9412d82c1328)
  health: 28 of 38 dual-channel calls had no critical incidents (74%)
  ...
```

The entries share one implementation and the exact same flags:

```bash
hotato vapi health                    # VAPI_API_KEY / hotato connect vapi
hotato retell health --call-id ID     # RETELL_API_KEY / hotato connect retell
hotato bland health                   # BLAND_API_KEY / hotato connect bland
hotato synthflow health               # SYNTHFLOW_API_KEY / hotato connect synthflow
hotato millis health                  # MILLIS_API_KEY / hotato connect millis
```

| Flag | Meaning |
| :-- | :-- |
| `--last WINDOW` | how far back to pull (e.g. `7d`, `12h`, `2w`; default `7d`) |
| `--limit N` | maximum calls to pull (default 100) |
| `--dir PATH` | download directory (default `hotato-output/<stack>-calls`) |
| `--output PATH` | also write the HTML health report to PATH (the content-addressed report under `hotato-output/` is written either way) |
| `--call-id ID` | check this call id (repeatable; skips the list step). Required for Retell, which has no verified list-recent-calls endpoint -- hotato never guesses one |
| `--api-key KEY` | vendor API key (else the `hotato connect` store, else the stack's env var) |
| `--format text\|json` | output format (default text) |

Credentials resolve exactly as `hotato pull` does -- an explicit flag,
then the `hotato connect` store, then the stack's env var (`VAPI_API_KEY`
/ `RETELL_API_KEY` / `BLAND_API_KEY` / `SYNTHFLOW_API_KEY` /
`MILLIS_API_KEY`) -- and a missing key is one actionable line. Vapi and
Retell fetch the separated two-channel recording; Bland, Synthflow, and
Millis export one mixed channel, so each of their calls runs the
measured-confidence mono path below (silence timing measured from the
mixed channel; talk-over attribution comes from a two-channel recording
-- the scope line states this once per run). Mono calls report into the
best-effort mono observations block with their own counts and never
enter the Voice Stability denominator, so the mono stacks' reports carry
observations without a stability score
([EVIDENCE-CONTRACT.md](EVIDENCE-CONTRACT.md) states the whole tier
policy). The analysis runs on this machine; recordings download straight
from the platform and go nowhere else.

A window with no calls, a pull in which every recording failed to fetch,
or a pulled set with zero analyzable calls refuses with the reason (exit
2) -- no score is reported over zero calls.

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
the CI gate stays deterministic and dual-channel. The full four-tier
policy behind this split -- dual-channel deterministic, mono with
provider metadata, raw mixed mono, refused -- is stated once in
[EVIDENCE-CONTRACT.md](EVIDENCE-CONTRACT.md).

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

## The persisted envelope

Every autopsy also writes `hotato-output/autopsy-<id>.json` next to the
HTML: the machine-readable result envelope -- the source path, the mode,
and the incidents with their onset, kind, and (on the stereo path) the
underlying scan candidate kind. Like the report it is content-addressed
and deterministic: the same recording writes the same bytes on every run.
The envelope carries the measured facts only; est. cost figures live on
the rendered surfaces (they exist only under `--cost-config`), never in
the stored envelope. This is the offline store `hotato pin` resolves an
`apx-...#N` ref from without re-running the analysis.

## `hotato scan <directory>`: the folder health report

Point `hotato scan` at a folder and it runs the autopsy engine over every
recording in it -- stereo through the deterministic scanner, mono
best-effort, exactly the rules above; an unreadable file is listed as
refused with the reason, never skipped silently:

```bash
hotato scan ./calls
```

```
hotato scan: calls  (5 recordings: 4 analyzed, 1 refused)
  Voice Stability Score: 25/100  (4 dual-channel calls; policy 9412d82c1328)
  SMALL SAMPLE: 4 dual-channel calls, under the 20-call bar
  health: 1 of 4 dual-channel calls had no critical incidents (25%)
  ...
  evidence coverage (measured from this run):
    dual-channel timing: 4 calls -- deterministic two-channel timing walk
    refused: 1 file -- unreadable as call audio; every file listed with its reason, never scored
  ...
  worst calls (critical count, then worst measured magnitude):
     1. late-followup.wav  apx-6836885bd877  1 critical, 0 warning  worst 5.14s gap
  ...
  report:   hotato-output/scan-scn-46a5af300d6b.html
  envelope: hotato-output/scan-scn-46a5af300d6b.json
```

The HEALTH headline is a measured share -- **dual-channel calls** with
zero critical incidents over dual-channel calls analyzed. The **Voice
Stability Score** is that same share, times 100: `round(share x 100)`,
nothing else (the machine field is `critical_free_call_rate`). The share
line prints directly beneath the score as its formula, the eligible
sample size and the analysis-policy sha print beside the score, a
`SMALL SAMPLE` label renders under 20 dual-channel calls, and the HTML
report carries a one-line "How this is calculated" note pointing at the
share line. A mono call **never enters the denominator**: mono-analyzed
calls report into the *Best-effort mono observations* block with their
own counts (measured silence timing from one mixed channel; talk-over
and barge-in attribution comes from a two-channel recording). With zero
dual-channel calls no score renders and the report states why. There is
deliberately **no blended quality score anywhere**: one blended number
hides exactly the distinction the tool exists to draw (see
[METHODOLOGY.md](../METHODOLOGY.md)), so the branded number restates the
measured share -- no weights, no other arithmetic. The share sits beside
the **evidence coverage** block (per-lane measured counts from what the
run actually had -- dual-channel timing, mono best-effort, refused with
reasons; a lane whose evidence was absent from the run never renders as
assessed), a per-category breakdown (counts plus the worst measured
magnitude in each category), and the worst-calls ranking, each call
linking to its own per-call autopsy report, generated alongside with its
envelope -- so `hotato pin` works straight from a folder scan.

The scan is deterministic end to end: the same directory with the same
flags produces byte-identical CLI text and a byte-identical HTML report,
and the outputs are content-addressed (`scn-` + 12 hex over the sorted
file-content manifest plus the analysis flags). The summary envelope
(`scan-<id>.json`) stores the aggregate with a `recorded_at` provenance
stamp written once, when that content is first seen; a re-run of
unchanged content resolves to the same file and leaves it untouched.

**Trend.** When prior summary envelopes for the same directory sit in the
output dir, the report renders a run-over-run strip from them -- each
prior run's share and critical count under its stored provenance
timestamp, with the current run beside them. The page stays
byte-identical given the same directory and the same prior-run store.

**Recurrence states.** An incident kind present in the current run that
also appears in stored prior runs of the same directory prints a
recurrence line, in the CLI text and in the report, each line carrying a
measured state:

```
RECURRING: DEAD AIR in 7 of 38 calls this run (23 in the stored window). Also present in 3 prior run(s): 2026-07-08T09:00:00Z, 2026-07-15T09:00:00Z, 2026-07-22T09:00:00Z.
```

The states, all derived from stored facts: `observed` (1-2 calls carry
the kind across this run plus the stored prior runs), `RECURRING` (3+),
`RECURRING, LOW SAMPLE` (3+ but the eligible dual-channel sample is
under 20 calls), and `ELEVATED` (this run and the most recent comparable
prior run -- same analysis policy, same evidence lanes, 20+ eligible
dual-channel calls in both -- have Wilson 95% intervals on the kind's
per-call rate that do not overlap, this run higher). Every count and
date is a measured aggregate or a stored envelope's `recorded_at`
provenance -- never extrapolation -- so the same directory and the same
prior-run store always print the same lines. The platform health
commands run the same aggregate over their download directory, so
re-running `hotato vapi health` week over week builds the store the
recurrence lines read from.

`--cost-config` renders est. cost totals across the analyzed calls, from
your own per-incident figures, exactly as above. The single-recording
mode is unchanged: `hotato scan --stereo call.wav` lists one recording's
candidate turn-taking moments, byte-for-byte as before.

## `hotato pin <autopsy-ref>`: incident to contract

`hotato pin` turns one autopsy incident into a portable `.hotato` failure
contract through the existing contract machinery
(`hotato contract create` on the recording at the incident's onset -- no
separate minting logic):

```bash
hotato pin apx-cc33f46fad58        # the call's top critical incident
hotato pin apx-cc33f46fad58#1      # one specific incident
```

Resolution is offline, from the persisted envelope under
`./hotato-output/` (or `--from DIR`). Before delegating, pin re-hashes
the source recording: the CURRENT bytes must still hash to the pinned
autopsy id, so a file that changed on disk refuses rather than binding a
different call.

The incident kind maps to the contract's expect decision: BARGE-IN and
TALK-OVER -- the floor-holding events the yield/hold contract vocabulary
expresses -- default to `--expect yield` (the caller held the floor), and
`--expect hold` records the human's call that the agent was right to keep
talking. A mono-derived incident refuses (contracts require the
two-channel deterministic path); DEAD AIR and LATENCY SPIKE are
silence-timing measurements with no caller onset to pin, and ECHO
SUSPECTED is a caveat, so those refuse with the reason too. Every refusal
-- malformed ref, unknown id, rank out of range, missing or changed
source, mono -- exits 2 and leaves no artifact.

Success prints the bundle dir and the CI step:

```bash
hotato prove --contracts contracts
```

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

An incident worth keeping fixed graduates in one step:

```bash
hotato pin apx-cc33f46fad58#1                # incident -> portable contract
hotato prove --contracts contracts           # the CI gate
```

The labeled-review path does the same through `hotato investigate`:

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
