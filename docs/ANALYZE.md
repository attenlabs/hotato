# `hotato analyze <folder>`: drop a folder, hear the bug

Point Hotato at a folder of dual-channel call recordings and it finds the
worst turn-taking moments across all of them, ranks them, and lets you hear
each one.

```bash
hotato analyze ./recordings
```

This writes one self-contained, offline HTML dashboard (`hotato-analyze.html`
by default) and opens it. A bare folder as the first argument routes to the
same command: `hotato ./recordings`.

## What it does

For every `.wav` under the folder (walked recursively, in sorted order),
`analyze`:

1. runs the same whole-call scanner as [`hotato scan`](../src/hotato/scan.py):
   it walks the caller and agent VAD activity tracks across the entire call,
   label-free, and emits candidate timing moments
   (`overlap_while_agent_talking`, `agent_start_during_caller`,
   `long_response_gap`, `agent_stop_no_caller`, `echo_correlated_activity`),
   each with a timestamp and a measured number;
2. aggregates the candidates across every call and ranks them by the
   scanner's own salience (overlap seconds / gap seconds / echo coherence),
   so the worst moments float to the top.

Then it emits three things.

### 1. A ranked dashboard

One card per top moment, in the [`hotato report`](REPORTS.md) house style: the
call file, the timestamp, the candidate kind, the measured number, and a
to-scale caller/agent timeline of that exact moment (the same SVG renderer the
report uses: activity spans, the shaded talk-over band, the onset marker, and
a yield marker where the scanner measured the agent going silent).

### 2. The hear-the-bug player

For the top `--audio-top` moments (default 8), the audio around the moment is
embedded inline as a base64 WAV data URI, so the page is fully self-contained
with zero external requests. Press play and a **playhead** sweeps that
moment's timeline in lockstep with `audio.currentTime` (via
`requestAnimationFrame`): you hear the agent talk over the caller, or the
dead-air gap, land exactly where the chart marks it. The playhead tracks
playback under `prefers-reduced-motion: reduce` too, riding `timeupdate`
instead of the smooth animation loop.

Only the top moments carry audio; the rest show the timeline. That, plus
`--pre` / `--post` (seconds of audio kept before/after each moment) and a
total-page audio budget, keeps the page a manageable size.

### 3. JSON for agents

```bash
hotato analyze ./recordings --format json
```

Prints the ranked candidates plus their metadata (source file, timestamp,
kind, salience, measured durations, and the audio/timeline window) to stdout,
capped by `--top`. Pass `--out FILE` to also write the full ranked JSON.

## Framing

Each result is a **measured candidate timing moment**: a timestamp and a
number, not a verdict on intent. Energy sounds the same whether a caller said
"mhm" or "stop", so you decide the expected behavior and label the moments
that matter:

```bash
hotato fixture create --stereo <call>.wav --onset <t> \
    --expect yield|hold --id found-moment-001 --out tests/hotato
```

Non-dual-channel or otherwise unreadable files are reported cleanly in a
"Skipped files" section with their reason (a mono mix cannot attribute
talk-over), and the run keeps going through the rest of the folder.

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

- **0**: ran -- candidate moments listed across the folder, possibly zero.
- **2**: usage error (the path is not a folder) or an IO error reading it.

Everything, audio included, stays on your machine.
