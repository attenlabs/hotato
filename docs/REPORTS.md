# Reports: doctor, report, team, export

Four surfaces, one scorer: reproducible timing measurements read straight
from the envelope, with the method exposed at every layer.

## `hotato doctor`: the 5-minute path

One command: pass a recording and it scores that; run it bare and it runs
the bundled self-test battery. Either way it writes a self-contained HTML
report and opens it in your browser -- on a headless box, it prints the
path instead.

```bash
uvx hotato doctor --stereo call.wav     # score your call, open the report
uvx hotato doctor                       # self-test fallback, same flow
uvx hotato doctor --demo --no-open --out report.html
```

Wraps the scorer and report, runs offline end to end. Exit codes match
`run`: `0` all pass, `1` a regression (`--no-fail` forces `0`), `2`
usage/IO error or a not-scorable recording -- meaning it lacks a moment to
measure (the caller channel is silent, or the agent wasn't talking when
the caller started), reported plainly instead of a verdict.

**Your own recording gets its audio embedded in the report by default.**
Scoring a call with `--stereo` / `--caller`+`--agent` writes the scored
audio into the HTML file as a base64 data URI, so hearing the moment next
to its timeline works offline (the bundled self-test fallback stays
unembedded). Sharing, mailing, or posting the resulting `report.html`
shares the raw recording -- treat it with the same care.

## `hotato report`: the visual report

One self-contained file -- inline CSS, inline SVG, zero external requests
-- that opens by double-click and survives being mailed around.

```bash
uvx hotato report --stereo call.wav --out report.html
uvx hotato report --suite barge-in --out selftest.html
uvx hotato report --stereo call.wav --format md --out report.md
uvx hotato report --stereo call.wav --embed-audio --out report.html  # opt-in: embed the audio too
```

`--embed-audio` embeds the exact scored audio (base64, under a size cap)
so the report is a fully self-contained, hearable artifact. Same caution:
a report built with `--embed-audio` (or `hotato doctor` on your own
recording, which sets it by default) carries the call audio inside the
HTML -- give it the same care as the raw recording before posting it
publicly or attaching it to a public issue/PR.

Per event it draws a to-scale caller/agent activity timeline from the
frame data -- overlap shaded, caller-onset and yield markers, measured
talk-over seconds, expected vs. measured, and a PASS or FAIL chip.

Once the page has at least three event cards, an analytics rollup follows,
computed from the same measurements (fewer events skips it):

| Chart | Shows |
|---|---|
| Time-to-yield distribution | One dot per measured yield, with mean, median, and p90 (definitions in `METHODOLOGY.md`) |
| Talk-over histogram | Per-event seconds, bucketed on a fixed grid |
| Failure clustering by fix class | A batch of failures reads as "these five share one config setting" instead of five separate mysteries |

Every timeline carries a collapsible **frame inspector**: the full frame
dump behind that event as a table (`t_sec`, per-channel dBFS, active
flags, thresholds) -- any pixel on the page can be re-derived by hand.
The `ScoreConfig` thresholds sit in one collapsed "Thresholds used" panel
at the end, reproducible without stamping the parameter table above every
render.

### Voice-trace context with `--trace`

Pass `--trace voice_trace.jsonl` (written by `hotato trace ingest`;
format: [`TRACE.md`](TRACE.md)) to attach a voice trace as **context**
next to the timing:

```bash
hotato trace ingest --otel spans.otel.jsonl --out voice_trace.jsonl
hotato report --stereo call.wav --trace voice_trace.jsonl --out report.html
```

The report gains one collapsed "Trace (context, not a score)" section:
the trace's discrete voice-pipeline events -- TTS cancel/stop, ASR
partials, tool calls -- as a mono span table. It stays scoped to context:
`did_yield`, `talk_over_sec`, `seconds_to_yield`, and the PASS/FAIL
verdict come from the scorer alone; the trace folds into the envelope as
an additive `trace_context` key. Without `--trace`, the report is
byte-identical to one built before the flag existed. Redaction carries
through: a span ingested without `--include-text` shows `[redacted]`
instead of its text, so a shared report carries only what the trace
already chose to keep. Same `trace=` parameter on `build_report_html` /
`build_report_md`.

### Reliability in the scorecard (pass@1 / pass@k / pass^k)

When a report carries an `assert.v1` envelope with dimension-tagged
results, the "Deterministic" shelf renders as a per-dimension
**scorecard** (outcome / policy / conversation / speech / reliability).
**Reliability** is pass^k's home (definitions: [`SIMULATE.md`](SIMULATE.md)),
rendering the repetition data you thread in:

```python
from hotato import report, simulate

summary = simulate.run_matrix(scenario, conversation_test=ct)   # a matrix aggregate
html, _ = report.build_report_html(stereo="call.wav",
                                   assertions=env, reliability=summary)
```

`reliability=` accepts a `simulate.run_matrix` summary, a bare
`simulate.reliability()` dict, or a `{"aggregate": <reliability dict>, "origin":
...}` wrapper. The dimension shows each number labeled, tabular mono:
**pass@1**, **pass@k**, **pass^k**, `n`, `k`, `passes`, a **Wilson 95% CI**
on pass@1, a **per-variation-cell** breakdown when the summary carries
one, and a **SIMULATOR_INVALID** bucket for broken fixtures, excluded
from `n`. pass^k stays its own number, its own lane -- no `overall_score`
field to blend into. Runs from simulation are labeled
**origin=simulated**, scoped apart from production reliability.

`hotato test run --repetitions N` (`N > 1`) computes this aggregate over
the N deterministic runs and threads it into `report.{html,md}`
automatically. With no repetition data, the dimension shows the
empty-state -- "not measured: no repeated runs in this report" --
byte-identical to a report built without the parameter.

### Regression deltas with `--base`

Save an envelope, then compare a later run against it:

```bash
hotato run --suite barge-in --format json > base.json
hotato report --suite barge-in --base base.json --out report.html
```

The report renders per-scenario talk-over and time-to-yield deltas, worse
and better marks clearly flagged. The same `--base` flag drives
`scripts/pr_comment.py`'s CI comment (`docs/CI.md`).

### PDF

The page ships print CSS: print it from any browser and the interactive
parts collapse into a clean paper layout -- print-to-PDF is the PDF
export.

## `hotato team`: the trend view

Aggregates a directory of run envelopes into one trend.

```bash
hotato run --suite barge-in --format json > runs/001.json
# ... more runs over days or branches ...
hotato team runs/ --html team.html --out agg.json
```

Reports: run count; mean/median/p90 talk-over and time-to-yield, pooled
across all events; mean/median/p90/p95 response gap (dead air before the
agent speaks), pooled the same way; pass rate per run; the most common
failure class; and a pass-rate trend line in the HTML page. `--order
mtime` (default) orders by file time; `--order name` uses the filename,
so a numeric prefix acts as an explicit index.

`--max-response-gap SECONDS` turns the pooled p95 response gap into a
latency SLA: the run exits `1` exactly when p95 exceeds the bound, the
same pass/fail contract as a talk-over or time-to-yield regression
(`--no-fail` always exits `0`). Percentile definitions: `METHODOLOGY.md`;
pooling shape: `dist_summary` in `src/hotato/_stats.py`.

Fewer than two runs is stated plainly, and exits `0`; a trend line renders
once there are enough points to mean something.

## `hotato export`: research-grade CSVs

Scores a recording (or the bundled battery) exactly like `hotato run`, and
writes three files into a directory:

```bash
uvx hotato export --stereo call.wav --out research/
uvx hotato export --suite barge-in --out research/
```

| File | Contents |
|---|---|
| `events.csv` | One row per scored event, every measured signal plus verdict |
| `frames.csv` | One row per VAD frame, the evidence behind every number |
| `envelope.json` | The standard machine envelope, unchanged |

Column meanings live in comment lines at the top of each CSV, so the
files are self-describing in a notebook or stats package months later. An
empty cell means "not derivable", distinct from a zero. Stdlib only,
offline.

`export` also prints mean/median/p90/p95 response gap pooled across the
exported events, and accepts the same `--max-response-gap SECONDS`
latency SLA gate as `team` (exit `1` when pooled p95 exceeds the bound).
A plain export with no `--max-response-gap` writes a byte-identical
`envelope.json` -- pooled numbers and the gate live only in the printed
summary and returned manifest, never in the CSVs or envelope file.
