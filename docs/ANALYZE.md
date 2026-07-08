# `hotato analyze <folder>` — drop a folder, hear the bug

Zero-config discovery over a whole folder of real dual-channel call recordings.
No scenarios, no labels, no onset, no flags required: point it at the folder and
it does the rest.

```bash
hotato analyze ./recordings
```

That writes one self-contained, offline HTML dashboard (`hotato-analyze.html` by
default) and opens it. `hotato ./recordings` — a bare folder as the first
argument — routes to the same command.

## What it does

For every `.wav` under the folder (walked recursively, in sorted order):

1. runs the same whole-call scanner as [`hotato scan`](../src/hotato/scan.py):
   it walks the caller and agent VAD activity tracks across the ENTIRE call,
   label-free, and emits candidate timing moments —
   `overlap_while_agent_talking`, `agent_start_during_caller`,
   `long_response_gap`, `agent_stop_no_caller`, `echo_correlated_activity` —
   each with a timestamp and a measured number;
2. aggregates the candidates across ALL calls and ranks them by the scanner's
   own salience (overlap seconds / gap seconds / echo coherence) so the worst
   moments float to the top.

Then it emits three things.

### 1. A ranked dashboard

One card per top moment, in the [`hotato report`](REPORTS.md) house style: the
call file, the timestamp, the candidate kind, the measured number, and a
to-scale caller/agent timeline of that exact moment (the same SVG renderer the
report uses — activity spans, the shaded talk-over band, the onset marker, and
a yield marker where the scanner measured the agent going silent).

### 2. The hear-the-bug player

For the top `--audio-top` moments (default 8) the REAL audio around the moment
is embedded inline as a base64 WAV data URI — nothing is uploaded, the page has
zero external requests. Press play and a **playhead** sweeps that moment's
timeline in lockstep with `audio.currentTime` (via `requestAnimationFrame`), so
you HEAR the agent talk over the caller, or the dead-air gap, land exactly where
the chart marks it. Reduced-motion safe: with `prefers-reduced-motion: reduce`
the playhead still tracks playback (it rides `timeupdate` instead of the smooth
animation loop).

Only the top moments carry audio; the rest show the timeline only. That, plus
`--pre` / `--post` (the seconds of audio kept before/after each moment) and a
total-page audio budget, keeps the page a reasonable size.

### 3. JSON for agents

```bash
hotato analyze ./recordings --format json
```

prints the ranked candidates plus their metadata (source file, timestamp, kind,
salience, measured durations, and the audio/timeline window) to stdout, capped
by `--top`. Pass `--out FILE` to also write the full ranked JSON.

## Honest framing

These are **measured candidate timing moments**, not verdicts and not intent.
Energy is not intent: the scanner cannot know whether a caller sound was "mhm"
or "stop", so nothing here is a pass/fail, a failure count, or an accuracy
number. You decide the expected behavior and label the moments that matter:

```bash
hotato fixture create --stereo <call>.wav --onset <t> \
    --expect yield|hold --id found-moment-001 --out tests/hotato
```

Non-dual-channel or otherwise unreadable files are reported cleanly in a
"Skipped files" section with their reason (a mono mix cannot attribute
talk-over); a bad file never crashes the run.

## Flags

| flag | default | meaning |
| --- | --- | --- |
| `FOLDER` | (required) | directory of dual-channel WAVs, walked recursively |
| `--top` | 25 | ranked moments shown in the dashboard / stdout JSON (0 = all) |
| `--audio-top` | 8 | top moments that get the embedded hear-the-bug player |
| `--pre` | 2.0 | seconds of audio/timeline kept before each moment |
| `--post` | 4.0 | seconds of audio/timeline kept after each moment |
| `--min-gap` | 2.0 | minimum response gap (seconds) to surface as a candidate |
| `--caller-channel` / `--agent-channel` | 0 / 1 | channel assignment |
| `--format` | `html` | `html` (dashboard) or `json` (ranked candidates) |
| `--out` | `hotato-analyze.html` | where to write the dashboard, or the JSON |
| `--no-open` | off | do not launch a browser for the dashboard |

## Exit codes

- **0** — ran (candidate moments listed across the folder, possibly zero; never
  a pass/fail and never a verdict).
- **2** — usage error (the path is not a folder) or an IO error reading it.

Everything runs offline; no audio leaves the machine.
