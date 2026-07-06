# Reports: doctor, report, team, export

Four surfaces over the same scorer. Every number in every one of them is a real
measurement from the envelope; nothing is recomputed, restyled into a
percentage, or fabricated. There is no accuracy percentage anywhere.

## `hotato doctor`: the 5-minute path

One command. If you pass a recording it scores that; otherwise it runs the
bundled self-test battery. Either way it writes the self-contained HTML report
and tries to open it in your browser (on a headless box it prints the path).

```bash
uvx hotato doctor --stereo call.wav     # score your call, open the report
uvx hotato doctor                       # self-test fallback, same flow
uvx hotato doctor --demo --no-open --out report.html
```

It is a convenience wrapper over the existing scorer and report. Nothing new is
claimed, and everything runs offline. Exit codes match `run`: `0` all pass,
`1` a regression (`--no-fail` forces `0`), `2` usage or IO error.

## `hotato report`: the visual report

One self-contained file: inline CSS, inline SVG, zero external requests. It
opens offline by double-click and survives being mailed around.

```bash
uvx hotato report --stereo call.wav --out report.html
uvx hotato report --suite barge-in --out selftest.html
uvx hotato report --stereo call.wav --format md --out report.md
```

Per event it draws a to-scale caller/agent activity timeline from the real
frame data: the overlap shaded, the caller-onset and yield markers, the
measured talk-over seconds, expected vs actual, a PASS or FAIL chip, and the
exact `ScoreConfig` thresholds used.

Above the per-event cards sits an analytics block computed from the same
measurements:

- a **time-to-yield distribution** strip, one dot per measured yield, with
  mean, median, and p90 (definitions in `METHODOLOGY.md`);
- a **talk-over histogram**, real per-event seconds bucketed on a fixed grid;
- **failure clustering by fix class**, so a batch of failures reads as "these
  five are one config knob" instead of five separate mysteries.

Every timeline carries a collapsible **frame inspector**: the full frame dump
behind that event as a table (`t_sec`, per-channel dBFS, active flags,
thresholds), so any pixel on the page can be re-derived by hand.

### Regression deltas with `--base`

Save an envelope, then compare a later run against it:

```bash
hotato run --suite barge-in --format json > base.json
hotato report --suite barge-in --base base.json --out report.html
```

The report renders per-scenario talk-over and time-to-yield deltas with clear
worse and better marks. The same `--base` flag works on
`scripts/pr_comment.py` for the CI comment (`docs/CI.md`).

### PDF

The page ships print CSS. Print it from any browser and the interactive parts
collapse into a clean paper layout, so print-to-PDF is the PDF export.

## `hotato team`: the trend view

Aggregates a directory of run envelopes into one honest trend.

```bash
hotato run --suite barge-in --format json > runs/001.json
# ... more runs over days or branches ...
hotato team runs/ --html team.html --out agg.json
```

It reports: number of runs, mean/median/p90 talk-over and time-to-yield pooled
across all events, pass rate per run over time, the most common failure class,
and a pass-rate trend line in the HTML page. `--order mtime` (default) orders
runs by file time; `--order name` uses the filename, so a numeric prefix is an
explicit index.

Fewer than two runs is stated plainly and exits `0`. It is never padded into a
trend, because a trend of one point is a fabrication.

## `hotato export`: research-grade CSVs

Scores a recording (or the bundled battery) exactly like `hotato run` and
writes three files into a directory:

```bash
uvx hotato export --stereo call.wav --out research/
uvx hotato export --suite barge-in --out research/
```

- `events.csv`: one row per scored event, every measured signal plus verdict.
- `frames.csv`: one row per VAD frame, the evidence behind every number.
- `envelope.json`: the standard machine envelope, unchanged.

Column meanings are documented in comment lines at the top of each CSV, so the
files are self-describing when they land in a notebook or a stats package
months later. An empty cell means "not derivable", never zero. Stdlib only,
offline.
