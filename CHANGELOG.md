# Changelog

All notable changes to Hotato are recorded here.

The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and
the project aims to follow [Semantic Versioning](https://semver.org/spec/v2.0.0.html).
Every entry reports millisecond measurement error and a confusion matrix, by
design. See `docs/BENCHMARK.md`.

## [Unreleased]

Nothing yet.

## [0.1.0] - 2026-07-05

Initial release, published on PyPI. Offline, MIT, zero-install turn-taking eval
for voice agents. It scores one narrow thing well and is honest about the rest.

### Added

- **Core scorer**: one recording or the bundled battery, returning a single
  machine-readable envelope (`schema_version` "1"). Three objective timing signals
  measured from audio energy over aligned caller/agent channels: `did_yield`,
  `seconds_to_yield` (time to yield), `talk_over_sec`. Delegated unchanged to the
  vendored MIT `barge_scoring` engine (`src/hotato/_engine`), kept byte-identical
  to upstream by `sync_engine.py` (a CI drift gate).
- **Signal bus**: a namespaced `signals` block. `barge_in` mirrors the three
  originals byte-for-byte; `latency` adds `response_gap_sec` and
  `premature_start_sec`, pure endpointing timing on the same two VAD tracks.
- **Fix map**: every failing event carries a concrete fix. `fix_class: "config"`
  is a stack-specific knob with direction and honest trade-off.
  `fix_class: "engagement-control"` is a both-axes discrimination failure a single
  sensitivity dial cannot solve, pointed high-level and numbers-free at an
  engagement-control / addressee-detection layer. Plus a suite-level funnel that
  fires only when both axes fail at once.
- **CLI** (`hotato`): `run` (suite or single recording), `capture` and `setup` for
  stack adapters (Vapi, Twilio, LiveKit, Pipecat; Retell status stated honestly),
  `--format json`, non-zero exit for CI, and `--dump-frames` for per-frame,
  re-derivable evidence behind every number.
- **One-tool MCP server** (`hotato-mcp`): exposes exactly one tool,
  `voice_eval_run`, returning the identical JSON envelope, so an AI agent can run
  the eval mid-task.
- **Bundled synthetic battery**: an eight-scenario `barge-in` self-test
  (interruptions, backchannels, telephony 8 kHz, double-talk, echo bleed, rapid
  turn-taking) with exact rendered ground truth. A floor and a regression guard.
- **Honest scope, everywhere**: a `limits` block in every result and in the MCP
  schema. Reproducible timing against published thresholds, an explicit ceiling,
  energy-is-not-intent, and the best input (two-channel) stated up front. Offline
  by design.
- **JSON Schema** (`src/hotato/schema/envelope.v1.json`), `METHODOLOGY.md`,
  `CONTRIBUTING.md`, `docs/CORPUS-GOVERNANCE.md`, `llms.txt`, and a stdlib-only
  core with zero required third-party dependencies.
- **`hotato doctor`**: the 5-minute path in one command. Scores your recording
  (or the bundled self-test when none is given), writes the self-contained visual
  HTML report, and opens it in a browser (best effort; `--no-open` prints the
  path). A convenience wrapper over the existing scorer and report; nothing new
  is claimed.
- **`hotato report --format {html,md} [--base BASE.json]`**: a self-contained
  visual report (inline CSS and SVG, zero external requests, opens offline). Per
  event it draws a to-scale caller/agent timeline from the real frame data with
  the overlap shaded, onset and yield markers, measured talk-over, expected vs
  actual, and the exact `ScoreConfig` thresholds used. An analytics block
  computed from the same measurements: a time-to-yield distribution strip, a
  talk-over histogram, and failure clustering by fix class. A collapsible
  per-event frame inspector puts the full frame dump behind every timeline.
  `--base` renders per-scenario talk-over and time-to-yield deltas against a
  previous envelope. Ships print CSS, so print-to-PDF from any browser.
  `--format md` renders the same content as Markdown tables.
- **`hotato team DIR [--order mtime|name] [--html team.html] [--out agg.json]`**:
  aggregates a directory of run envelopes into one trend view. Reports runs,
  mean/median/p90 talk-over and time-to-yield pooled across all events, pass rate
  per run over time, the most common failure class, and a pass-rate trend line in
  the HTML page. Fewer than 2 runs is stated plainly, never padded into a trend.
- **`hotato export ... --out DIR`**: research-grade output. Writes `events.csv`
  (one row per event, every measured signal plus verdict), `frames.csv` (one row
  per VAD frame, the evidence behind every number), and `envelope.json`. Column
  meanings are documented in comment lines at the top of each CSV. Stdlib only,
  offline.
- **`hotato benchmark`** (`src/hotato/stackbench.py`): comparable stack runs.
  Scores your captured battery per stack and compares result files side by side
  with `hotato benchmark compare`. Measurements only; no vendor numbers, no
  leaderboard, no ranking.
- **Pytest plugin**, auto-registered on install (`pytest11` entry point): a
  `hotato_score` fixture that scores a recording or suite inside any test, and an
  opt-in `--hotato-suite` session gate that runs the battery after the tests and
  fails the session on a regression. `--hotato-suite-scenarios` and
  `--hotato-suite-audio` point the gate at your own labelled sets.
- **MCP: optional `report_path`** on the one `voice_eval_run` tool. When set, the
  server also writes the self-contained HTML report there and the returned
  envelope carries `report_path` (absolute). Additive; the envelope is otherwise
  unchanged.
- **Tiered corpus suites** (`corpus/suites/`): `silver`, `silver-defects`,
  `gold`, and `gold-defects`, 112 scenarios total, all synthetic shaped noise
  rendered deterministically from each scenario's own labelled timings (seed
  `sha256(id)`). The defect suites exist to fail: every scenario in them fails on
  its labelled axis, so `exit_code 1` there proves the scorer catches what it
  claims to catch. Built and byte-verified by `corpus/suites/build_suites.py`
  (`--check` regenerates to a temp dir and byte-compares); `manifest.json` is the
  machine-readable inventory. Run any suite via
  `hotato run --suite barge-in --scenarios DIR --audio DIR`.
- **GitHub PR check** (`.github/workflows/hotato.yml` plus
  `scripts/pr_comment.py`): scores the suite on every pull request, posts one
  sticky comment (results table, pass/fail line, regressions with deltas against
  the base branch when scorable), and fails the job on a regression. See
  `docs/CI.md`.
- **Documented distribution statistics** (`src/hotato/_stats.py`): mean, median,
  and p90 used by `report` and `team` have one stdlib implementation with the
  definition in the docstring. p90 is linear interpolation between closest ranks.
  Rates are reported as fractions, never a percentage. Empty input returns null,
  never a fabricated number.
- **New docs pages**: `docs/REPORTS.md` (doctor, report, team, export),
  `docs/PYTEST.md` (the plugin and the gate), `docs/SUITES.md` (the tiered
  corpus suites), and `docs/SUBMITTING.md` (the corpus submission walkthrough).
- **Reproducible benchmark harness** (`src/hotato/benchmark.py`, run it with
  `python3 -m hotato.benchmark`). Takes labelled dual-channel recordings and
  reports per-signal measurement error in milliseconds (onset, time-to-yield,
  response-gap) plus a `did_yield` confusion matrix against the
  should_yield / should_not_yield labels. It keeps the missed-yield and
  false-yield cells separate so neither hides behind an average. Runs on the
  bundled and example synthetic fixtures out of the box and writes a JSON and
  markdown report. It is a standalone measurement tool you point at fixtures, not
  a CLI command.
- **`docs/BENCHMARK.md`**: the methodology. What is measured, why
  milliseconds-and-a-matrix is the honest metric, how to reproduce it, and the
  bring-your-own-labelled-recordings path to real validity.
- **Corpus pipeline** (`corpus/`): the tooling for the real-recording corpus. A
  labelling schema (`corpus/label.schema.json`) that extends the scenario shape
  with provenance, consent, PII, and attestation fields. A standalone stdlib
  validator (`corpus/validate.py`) that checks a `(recording, label)` pair
  conforms (two-channel WAV, required fields, timings in range, attestation). And
  a `corpus/README.md` that defers to `docs/CORPUS-GOVERNANCE.md` for consent and
  PII. Ships one clearly-labelled synthetic example and no real audio.
- **Community scaffolding**: this changelog, GitHub issue templates (bug report
  and feature request), and a pull-request template whose checklist carries the
  project's honesty rules (no accuracy %, no fabricated numbers, energy is not
  intent, do not edit the vendored `_engine`, keep the drift gate green).

### Note

- The bundled and example fixtures remain synthetic and labelled as such. This
  release ships no fabricated recording, real-model number, benchmark result,
  leaderboard, or star count. The synthetic fixtures are a floor and a regression
  guard; real validity comes from contributed, consented, human-labelled calls.

[Unreleased]: https://github.com/attenlabs/hotato/compare/v0.1.0...HEAD
[0.1.0]: https://github.com/attenlabs/hotato/releases/tag/v0.1.0
