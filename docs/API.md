# Python API reference

Every function the CLI, the pytest plugin, and the MCP server call is a plain
Python function you can call directly. The core is stdlib only, offline, and
deterministic: the same audio and config always produce the same envelope.
Import and score:

```python
from hotato.core import run_single, run_suite

env = run_single(stereo="call.wav", expect="yield")
env = run_suite()  # bundled 8-scenario battery
```

The top-level package re-exports the essentials:
`from hotato import run_single, run_suite, LIMITS, SUITE_ID, __version__`.

All scoring functions take keyword arguments only.

## The envelope

`run_single` and `run_suite` return the same machine-readable dict
(JSON Schema: `src/hotato/schema/envelope.v1.json`):

```python
{
  "tool": "hotato",
  "schema_version": "1",
  "mode": "single" | "suite",
  "stack": "generic",            # normalized stack label
  "offline": True,
  "engine": {"name", "version", "upstream"},
  "limits": {...},               # scope and ceiling, hotato.core.LIMITS
  "summary": {"events", "passed", "failed", "regression"},
                                 # plus additive "not_scorable" (count) when
                                 # at least one event could not be judged
  "events": [...],               # one dict per scored event, below
  "fix_map": [...],              # one entry per failing event with a fix
  "funnel": {...} | None,        # systemic pointer, fires only when both axes fail
  "exit_code": 0 | 1,            # 1 when any scorable event failed; the CLI
                                 # process exits 2 for a single recording that
                                 # is not scorable (see Exit codes below)
  "suite": "barge-in",           # run_suite only
}
```

Each event:

```python
{
  "event_id": str,               # file basename or scenario id
  "scenario_id": str | None,
  "title": str | None,
  "category": str | None,        # e.g. "should_yield"
  "expected_yield": bool,
  "verdict": {
    "passed": bool,
    "did_yield": bool,
    "seconds_to_yield": float | None,
    "talk_over_sec": float,
    "reasons": [str],            # failure reasons, empty on pass
  },
  "measurements": {
    "caller_onset_sec": float,
    "agent_talking_at_onset": bool,
    "hop_sec": float,
    "notes": str,
  },
  "signals": {                   # namespaced signal bus, additive
    "barge_in": {"did_yield", "time_to_yield_sec", "talk_over_sec"},
    "latency": {"response_gap_sec", "premature_start_sec"},
    "echo": {                    # every event; cross-channel coherence,
                                 # deterministic, computed in hotato's own
                                 # layer (hotato/echo.py), never _engine
      "coherence", "lag_sec", "echo_suspected",
    },
    "resume": {                  # only on events where the agent yielded
      "resumed", "resume_gap_sec", "restart_suspected",
    },
  },
  "fix": None | {                # set on failing events
    "fix_class": "config" | "engagement-control",
    "title": str, "detail": str,
    "knob": str | None, "pointer": str | None,
  },
}
```

An event that lacks the input to be judged (a silent caller channel with no
onset label, or a should-yield expectation with the agent silent at onset)
additionally carries `"scorable": False` and a plain `"not_scorable_reason"`.
It reports as an input problem: it sits outside `passed`/`failed` and outside
the fix router and the funnel, keeping envelopes for valid recordings
byte-identical.

## hotato.core

### run_single

```python
run_single(
    *,
    stereo: str | None = None,        # two-channel WAV path
    caller: str | None = None,        # mono WAV path (with agent=)
    agent: str | None = None,         # mono WAV path (with caller=)
    caller_channel: int = 0,
    agent_channel: int = 1,
    onset_sec: float | None = None,   # caller onset hint, seconds from start
    expect: str = "yield",            # "yield" or "hold" (backchannel)
    stack: str | None = None,         # livekit | pipecat | vapi | generic
    max_talk_over_sec: float | None = None,
    max_time_to_yield_sec: float | None = None,
    echo_gate: bool = False,          # hold an echo-suspected yield out of
                                      # the verdict (scorable: false) instead
                                      # of counting it as a clean pass
    cfg: ScoreConfig | None = None,
) -> dict
```

Scores one recording and returns the envelope. Provide either `stereo` or
both `caller` and `agent`. `expect="hold"` means the caller's speech is a
backchannel, a short acknowledgement like "mhm", and a correct agent keeps
talking through it. The two `max_*` thresholds tighten the pass criteria.
`echo_gate` is opt-in and off by default; `signals.echo` is always computed
and reported either way. A malformed or truncated WAV raises a clean
`ValueError` carrying the ffmpeg export line to fix it.

### run_suite

```python
run_suite(
    *,
    suite: str = "barge-in",          # the only suite id (SUITE_ID)
    stack: str | None = None,
    scenarios_dir: str | None = None, # your scenario JSON labels
    audio_dir: str | None = None,     # your recordings, <scenario-id><suffix>
    suffix: str = ".example.wav",
    caller_channel: int = 0,
    agent_channel: int = 1,
    echo_gate: bool = False,          # see run_single
    cfg: ScoreConfig | None = None,
) -> dict
```

Runs a labelled battery and returns the envelope with a `suite` key. The
bundled 8-scenario battery ships inside the package, zero external files;
point `scenarios_dir` and `audio_dir` at your own labelled set instead (for
example `corpus/suites/gold/scenarios` and `.../audio`). Suite audio must be
two-channel.

### dump_frames_for_input

```python
dump_frames_for_input(
    *,
    stereo: str | None = None,
    caller: str | None = None,
    agent: str | None = None,
    caller_channel: int = 0,
    agent_channel: int = 1,
    onset_sec: float | None = None,
    cfg: ScoreConfig | None = None,
) -> dict
```

The per-frame evidence behind every reported number: each channel's dBFS, VAD
activity, threshold, and noise floor, plus a self-describing `config` block.
Every reported signal is re-derivable by hand from this dump.

### LIMITS and SUITE_ID

`hotato.core.LIMITS` is the scope dict embedded in every envelope: method,
ceiling, best input, and boundaries. `SUITE_ID` is `"barge-in"`.

### ScoreConfig

Every threshold is an exposed parameter:

```python
from hotato._engine.score import ScoreConfig
from hotato._engine.vad import VADParams

cfg = ScoreConfig(
    frame_ms=20.0, hop_ms=10.0,
    yield_hangover_sec=0.20,       # agent quiet this long = yielded
    max_search_sec=3.0,            # yield search window after onset
    caller_proximity_sec=0.5,
    turn_end_silence_sec=0.20,
    premature_tolerance_sec=0.05,
    onset_min_run_sec=0.05,
    agent_onset_lookback_sec=0.10,
    caller_vad=VADParams(),        # rel_db=15.0, abs_gate_db=-60.0,
    agent_vad=VADParams(),         # hangover_sec=0.15, noise_percentile=0.10,
)                                  # dyn_margin_db=22.0, backend="energy"
```

`VADParams.backend` is `"energy"` (the deterministic reference behind every
published number) or `"neural"` (an optional Silero VAD cross-check via
`pip install 'hotato[neural]'`; without the extra it raises a clean
`BackendUnavailable`, surfacing the missing dependency immediately).

## hotato.report

Self-contained visual reports scored from the same measurements. All three
functions accept the full scoring parameter set (`stereo`, `caller`, `agent`,
`suite`, `scenarios_dir`, `audio_dir`, `suffix`, `caller_channel`,
`agent_channel`, `onset_sec`, `expect`, `stack`, `max_talk_over_sec`,
`max_time_to_yield_sec`, `cfg`) as keyword arguments.

```python
build_report_html(*, base: dict | None = None,
                  base_label: str | None = None, **kwargs) -> (str, dict)
build_report_md(*, base: dict | None = None,
                base_label: str | None = None, **kwargs) -> (str, dict)
write_report(path: str, fmt: str = "html", **kwargs) -> dict
```

`build_report_html` scores the input and returns `(html, envelope)`: one
self-contained file with inline CSS and SVG, per-event timelines, analytics,
a frame inspector, and print CSS for PDF. `build_report_md` mirrors it as
Markdown tables. `write_report` builds in `fmt` (`"html"` or `"md"`), writes
to `path`, and returns the envelope. Pass `base` (a previous envelope dict,
for example loaded from `hotato run --format json` output) to render
per-scenario regression deltas; `base_label` names it on the page.

```python
from hotato.report import write_report

env = write_report("report.html", suite="barge-in", stack="livekit")
```

## hotato.aggregate

Team mode: many run envelopes, one trend view.

```python
load_run_dir(dirpath: str, order: str = "mtime") -> dict
    # {"runs": [{"file", "path", "mtime", "env"}], "skipped": [...], "order"}
    # order: "mtime" (oldest first) or "name" (numeric prefix = explicit index)

aggregate_runs(runs: list, order: str = "mtime",
               skipped: list | None = None) -> dict
    # team envelope: kind "team-aggregate", runs, events_total,
    # talk_over_sec / seconds_to_yield distribution summaries (mean/median/p90),
    # pass_rate {latest, first, mean, direction}, pass_rate_over_time,
    # failure_classes, most_common_failure_class, skipped, exit_code 0.
    # Raises ValueError with fewer than 2 runs; each trend point corresponds
    # to one input run.

build_team_section_html(agg: dict) -> str   # embeddable section
build_team_page_html(agg: dict) -> str      # full self-contained page
```

```python
from hotato.aggregate import load_run_dir, aggregate_runs, build_team_page_html

loaded = load_run_dir("runs/")
agg = aggregate_runs(loaded["runs"], order=loaded["order"],
                     skipped=loaded["skipped"])
html = build_team_page_html(agg)
```

## hotato.export

Research-grade flat files from the same scorer.

```python
run_export(
    *,
    out_dir: str,
    # plus the full scoring parameter set: stereo, caller, agent,
    # caller_channel, agent_channel, onset_sec, expect, stack, suite,
    # scenarios_dir, audio_dir, suffix, max_talk_over_sec,
    # max_time_to_yield_sec, cfg
) -> dict   # {"env", "events_rows", "frames_rows", "paths"}
```

Writes `events.csv` (one row per event, columns in
`hotato.export.EVENT_COLUMNS`), `frames.csv` (one row per VAD frame, columns
in `FRAME_COLUMNS`), and `envelope.json` into `out_dir` (created if missing).
`#` comment lines at the top of each CSV document the column meanings. An
empty cell marks a value not derivable from that input; every filled cell is
a direct measurement.

## hotato.stackbench

Identical scenarios, your stack, comparable result files: every number is a
measurement of the recordings you provide.

### run_stackbench

```python
run_stackbench(
    *,
    stack: str,                       # one of BENCH_STACKS:
                                      # vapi | twilio | livekit | pipecat | generic
    recordings_dir: str,              # <scenario-id><suffix> WAVs
    scenarios_dir: str | None = None, # default: bundled battery
    suffix: str | None = None,        # default: auto-detected
                                      # (.wav, .stereo.wav, .example.wav)
    caller_channel: int = 0,
    agent_channel: int = 1,
    cfg: ScoreConfig | None = None,
) -> dict
```

Returns a result dict (`kind: "stack-benchmark"`) with the envelope fields
plus `config`, `scenarios {total, captured, not_captured}`, and `provenance`.
Scoring is `run_suite`, unchanged. Scenarios with no matching recording are
listed under `not_captured`, left out of both the scoring and the failure
counts. The timestamp derives from input file mtimes, so the same inputs
always reproduce the same result file.

### load_result, compare_results, render_comparison_md

```python
load_result(path: str) -> dict
    # loads and validates one result JSON; anything else is a clean ValueError

compare_results(inputs: Sequence[tuple[str, dict]]) -> dict
    # inputs: (path, loaded_result) pairs, at least two.
    # Compares the intersection of scenarios scored in EVERY input;
    # the rest is listed under "skipped". Deltas are signed differences
    # against the FIRST input. Returns kind "stack-benchmark-comparison"
    # with inputs, compared, skipped, per_scenario, medians.

render_comparison_md(cmp_env: dict) -> str
    # the comparison as Markdown tables
```

```python
from hotato.stackbench import load_result, compare_results, render_comparison_md

cmp = compare_results([(p, load_result(p)) for p in ("a.json", "b.json")])
print(render_comparison_md(cmp))
```

## Pytest fixture

Installs automatically via the `pytest11` entry point (or load it explicitly
with `-p hotato.pytest_plugin`). Sits inert until your test calls it.

```python
def test_call_yields(hotato_score):
    env = hotato_score(stereo="call.wav", expect="yield")
    assert env["summary"]["regression"] is False
    assert env["events"][0]["verdict"]["seconds_to_yield"] < 1.0
```

`hotato_score(**kwargs)` takes the same keyword arguments as `run_single`;
pass `suite="barge-in"` (plus `run_suite` keywords) to score a battery
instead. It returns the envelope, so you write your own assertions against
it.

Session gate flags: `pytest --hotato-suite` runs the battery after your tests
and fails the session (exit 1) on a regression; `--hotato-suite-scenarios DIR`
and `--hotato-suite-audio DIR` point it at your own labelled set. Detail:
`docs/PYTEST.md`.

## `hotato.counterexample`

The counterexample API reduces one failing deterministic scripted scenario,
then emits a content-addressed replay capsule and a replayable deletion proof.
It never loads a provider adapter, model, network client, or subprocess.

```python
from hotato.counterexample import (
    compile_counterexample,
    verify_counterexample,
    reproduce_counterexample,
    inspect_counterexample,
    export_counterexample,
    predicate_counterexample,
)

compiled = compile_counterexample(
    "refund.scenario.json",
    "refund.test.json",
    target="refund-posted",
    out_dir="refund-posted.hotato-repro",
    workspace=".",
    budget=512,
)
assert compiled["exit_code"] in (0, 1)
```

`compile_counterexample` returns exit `0` only after the final unit-deletion
pass earns `one_minimal`. Exit `1` means the exact failure was preserved and a
runnable capsule was written, while the candidate-evaluation budget ended
before the proof completed. A refusal raises `CounterexampleRefusal` and leaves
no destination directory. Shared input parsing can raise `ValueError` for
malformed JSON/YAML-subset or schema-invalid documents and `OSError` for input
I/O failures. The CLI maps these public-API failures to its structured handled-
error and exit-code contract.

```python
verify_counterexample("refund-posted.hotato-repro")
reproduce_counterexample("refund-posted.hotato-repro")
inspect_counterexample("refund-posted.hotato-repro")
export_counterexample(
    "refund-posted.hotato-repro",
    out_dir="refund-posted.share",
)
predicate_counterexample("refund-posted.hotato-repro")
```

- `verify_counterexample` requires the recorded package version and evaluator
  source digest, then independently replays the source, accepted delete-only
  chain, final case, derived artifacts, and any completed one-minimal claim.
  The digest identifies shipped evaluator source; interpreter and platform
  identity are outside it, while replay hashes detect behavior changes.
- `reproduce_counterexample` permits evaluator drift and checks the reduced
  case twice for the source-selected structured failure branch. It is for Hotato
  evaluator/scenario regressions; it does not execute a deployed voice agent.
- `inspect_counterexample` verifies the closed member inventory, bound source,
  oracle and artifacts, and canonical human files without executing the scenario.
- `export_counterexample` first verifies the private capsule, then writes a
  non-runnable projection with content-bearing inputs omitted. Hashes remain
  correlators and may still be sensitive.
- `predicate_counterexample` returns `1` when the failure remains, `0` when it
  is absent, and `125` when the result cannot be used by `git bisect run`.

The v1 source is the base scripted scenario at the selected seed. A
`variation_matrix` is recorded as unapplied and may be removed during
reduction; compile a concrete scenario when a specific expanded run matters.
The exact scope, artifacts, commands, and claim ceiling are in
[`COUNTEREXAMPLES.md`](COUNTEREXAMPLES.md).

## MCP tool

`hotato-mcp` (or `python -m hotato.mcp_server`) speaks MCP over stdio. Its
scoring tool, `voice_eval_run`, returns the identical envelope the CLI emits;
three counterexample tools (`counterexample_compile`, `counterexample_verify`,
`counterexample_reproduce`) compile and check offline regression capsules; and
the fleet tools read, verify, and propose over a local fleet workspace
(`fleet_status`, `candidate_list`, `contract_list`, `trial_explain`,
`artifact_verify`, `experiment_propose`, `experiment_run`, `clone_cleanup`),
documented in [`MCP.md`](MCP.md). Install:
`uvx --from "hotato[mcp]" hotato-mcp`.

The parameters below are `voice_eval_run`'s, all optional:

| Parameter | Type | Default | Meaning |
| --- | --- | --- | --- |
| `stereo` | str | None | two-channel WAV path |
| `caller` | str | None | mono caller WAV (with `agent`) |
| `agent` | str | None | mono agent WAV (with `caller`) |
| `suite` | str | None | `"barge-in"` to run the bundled battery |
| `stack` | str | `"generic"` | livekit, pipecat, vapi, or generic |
| `expect` | str | `"yield"` | `"yield"` or `"hold"` |
| `onset_sec` | float | None | caller onset hint |
| `caller_channel` | int | 0 | caller channel index |
| `agent_channel` | int | 1 | agent channel index |
| `max_talk_over_sec` | float | None | pass threshold |
| `max_time_to_yield_sec` | float | None | pass threshold |
| `report_path` | str | None | also write the HTML report here; the envelope then carries `report_path` (absolute) |

## Exit codes and errors

Envelopes carry `exit_code`: 0 all scorable events passed, 1 regression.
`hotato.core.process_exit_code(env)` maps the finished envelope to the CLI
process exit: a single-recording run whose event isn't scorable (silent
caller with no onset label, or agent silent at onset; the reason lands in
`not_scorable_reason`) exits 2, because that is an input problem, not an
agent verdict. Suite runs count such events in `summary.not_scorable` and
keep their 0/1 semantics. Malformed input (bad WAV, out-of-range channel,
negative onset, unknown suite or stack) raises `ValueError`, which the CLI
surfaces as exit code 2 -- only a file the scorer can fully read gets scored.
