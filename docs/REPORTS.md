# Reports: doctor, report, team, export

Four surfaces over the same scorer. Every number in every one of them is a
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
`1` a regression (`--no-fail` forces `0`), `2` usage or IO error, or a
recording that is not scorable. Not scorable means the recording cannot answer
the question (the caller channel is silent, or the agent was not talking when
the caller started), so no verdict is given.

**A real recording gets its audio embedded in the report by default.**
Scoring a call with `--stereo` / `--caller`+`--agent` writes the exact scored
audio into the HTML file as a base64 data URI, so hearing the moment next to
its timeline works offline. The bundled self-test fallback stays unembedded.
If you plan to share, mail, or post the resulting `report.html`, treat it the
same as sharing the raw recording -- because it contains the raw recording.

## `hotato report`: the visual report

One self-contained file: inline CSS, inline SVG, zero external requests. It
opens offline by double-click and survives being mailed around.

```bash
uvx hotato report --stereo call.wav --out report.html
uvx hotato report --suite barge-in --out selftest.html
uvx hotato report --stereo call.wav --format md --out report.md
uvx hotato report --stereo call.wav --embed-audio --out report.html  # opt-in: embed the audio too
```

`--embed-audio` embeds the exact scored audio (base64, under a size cap) so
the report is a fully self-contained, hearable artifact -- and, for the same
reason, a shareable-HTML caution applies: a report built with `--embed-audio`
(or `hotato doctor` on a real recording, which sets it by default) carries
the call audio inside the HTML file. Do not post it somewhere public, or
attach it to a public issue/PR, without the same care you would give the raw
recording.

Per event it draws a to-scale caller/agent activity timeline from the
frame data: the overlap shaded, the caller-onset and yield markers, the
measured talk-over seconds, expected vs actual, and a PASS or FAIL chip.

After the per-event cards, once the page has at least three of them, sits an
analytics rollup computed from the same measurements (a page with fewer
events skips it -- there is nothing left for a rollup to say):

- a **time-to-yield distribution** strip, one dot per measured yield, with
  mean, median, and p90 (definitions in `METHODOLOGY.md`);
- a **talk-over histogram**, per-event seconds bucketed on a fixed grid;
- **failure clustering by fix class**, so a batch of failures reads as "these
  five share one config setting" instead of five separate mysteries.

Every timeline carries a collapsible **frame inspector**: the full frame dump
behind that event as a table (`t_sec`, per-channel dBFS, active flags,
thresholds), so any pixel on the page can be re-derived by hand. The exact
`ScoreConfig` thresholds used sit in one collapsed "Thresholds used" panel at
the end of the page, so the run is reproducible without stamping the
parameter table above every render.

### Voice-trace context with `--trace`

Pass `--trace voice_trace.jsonl` (a `hotato.voice_trace.v1` file, written by
`hotato trace ingest`) to attach a voice trace as **context** next to the
timing:

```bash
hotato trace ingest --otel spans.otel.jsonl --out voice_trace.jsonl
hotato report --stereo call.wav --trace voice_trace.jsonl --out report.html
```

The report grows one collapsed, clearly-labelled "Trace (context, not a
score)" section: the trace's discrete voice-pipeline events -- TTS
cancel/stop, ASR partials, tool calls -- as a mono span table (type, name,
start, end, detail). Exactly like `--base` and an attached `assert.v1`
envelope, the report never **evaluates** or scores a trace; it renders the
already-produced artifact as data. The section is context only: it never
touches `did_yield`, `talk_over_sec`, `seconds_to_yield`, or the PASS/FAIL
verdict, and it is folded into the machine envelope as an additive top-level
`trace_context` key. A report built without `--trace` is byte-identical to one
built before the flag existed.

Redaction is respected: a span carrying `text_redacted: true` (e.g. an
`asr_partial` ingested without `--include-text`) shows a `[redacted]`
placeholder, never its text -- so a report shared outside the fleet leaks no
spoken content the trace itself already withheld. The same `trace=` parameter
is on `build_report_html` / `build_report_md`; see `docs/TRACE.md` for the
trace format and `hotato trace ingest`.

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

Aggregates a directory of run envelopes into one trend.

```bash
hotato run --suite barge-in --format json > runs/001.json
# ... more runs over days or branches ...
hotato team runs/ --html team.html --out agg.json
```

It reports: number of runs, mean/median/p90 talk-over and time-to-yield pooled
across all events, mean/median/p90/p95 response gap (dead air before the agent
speaks) pooled the same way, pass rate per run over time, the most common
failure class, and a pass-rate trend line in the HTML page. `--order mtime`
(default) orders runs by file time; `--order name` uses the filename, so a
numeric prefix is an explicit index.

`--max-response-gap SECONDS` turns the pooled p95 response gap into a latency
SLA: the run exits `1` exactly when p95 exceeds the bound, the same
pass/fail contract as a talk-over or time-to-yield regression (`--no-fail`
always exits `0`). Percentile definitions: `METHODOLOGY.md`; the pooling
shape is `dist_summary` in `src/hotato/_stats.py`.

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

`export` also prints mean/median/p90/p95 response gap pooled across the
exported events, and accepts the same `--max-response-gap SECONDS` latency
SLA gate as `team` (exit `1` when the pooled p95 exceeds the bound). A plain
export with no `--max-response-gap` writes a byte-identical `envelope.json`;
the pooled numbers and the gate live only in the printed summary and the
returned manifest, never in the CSVs or the envelope file.
