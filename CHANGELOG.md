# Changelog

All notable changes to Hotato are recorded here.

The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and
the project aims to follow [Semantic Versioning](https://semver.org/spec/v2.0.0.html).
Every entry reports millisecond measurement error and a confusion matrix, by
design. See `docs/BENCHMARK.md`.

## [Unreleased]

Nothing yet.

## [0.2.2] - 2026-07-06

### Fixed
- Report not-scorable rendering: the HTML and Markdown reports now render a not-scorable event with a NOT SCORABLE chip (or verdict cell) and its reason, never PASS or FAIL. The overall verdict is REGRESSION when any scorable event failed, else NOT SCORABLE when any input could not be judged, else ALL PASS. Not-scorable events are excluded from the failure clusters and from the time-to-yield and talk-over distributions, and are listed in their own "Not scorable inputs" section with id and reason (never under "Failures and fixes"). The summary counts line in both formats gains `not_scorable=N` when N > 0, mirroring the CLI text. Reports for fully-scorable inputs are byte-identical to 0.2.1.

### Changed
- License metadata modernization: `pyproject.toml` now uses the SPDX string form `license = "MIT"` with `license-files = ["LICENSE"]`, replacing the deprecated license table and the OSI license classifier. Built wheels carry `License-Expression: MIT` and builds emit no license deprecation warnings. Building from source now needs setuptools 77 or newer.
- Package docstring: the fix-pointer wording is softened. Discrimination failures are not solvable by one timing threshold; where your stack provides an interruption/backchannel classifier, use it, otherwise a learned engagement-control / addressee-detection layer is needed.

## [0.2.1] - 2026-07-06

### Fixed
- Sdist completeness: `MANIFEST.in` now ships everything the packaged tests need (corpus suites and manifest, `corpus/validate.py`, `sync_engine.py`, `scripts/pr_comment.py`, golden files, docs and assets, `SECURITY.md`, adapters, examples, ci, `.github`). Rendered suite audio is pruned from the sdist; it regenerates deterministically at test session start. `python -m pytest -q` now runs fully green inside an extracted sdist.
- Not-scorable rendering: the text output renders a not-scorable event as `[NOT SCORABLE] <id>` with its reason on the next line, never as `[FAIL]`, and the summary line gains `not_scorable=N` when any event is not scorable. Applies to `run`, `doctor`, `demo`, and `capture` text output. JSON envelopes are unchanged.
- Capture exit mapping: `hotato capture` now returns `process_exit_code(env)`, so a single capture whose every event is not scorable exits 2 (unusable input) instead of 0. When the process exit code differs from the envelope `exit_code`, the trailing text line prints `process_exit_code=N`; fully-scorable runs keep the exact `exit_code=N` line.
- Defensive Vapi recording parsing: the stereo URL chain also accepts `artifact.recording.stereoRecordingUrl` and `artifact.recording.stereo.url` (when `stereo` is an object), between the current field and the deprecated legacy fields.

### Changed
- Retell copy: the first-run guide and `capture --help` now show the real self-serve path, `hotato capture --stack retell --call-id <id>` with `RETELL_API_KEY`, replacing the stale "no self-serve stereo export yet" line.
- `SECURITY.md` states the network posture precisely: network access is used only when you explicitly run hosted-stack capture commands; core scoring, reports, exports, benchmarks, and demos run offline.
- `adapters/README.md` describes separated caller/agent channels as the required input for attributable overlap measurement, with speech activity still measured by the configured VAD thresholds and frame-inspectable via `--dump-frames`.

## [0.2.0] - 2026-07-06

### Added
- `hotato demo`: packaged intentionally bad agent battery with the visual report; `--fail` returns the real regression code.
- `python -m hotato` entry point.
- Retell capture is a real multichannel fetch (prefers the scrubbed recording); Vapi capture reads the current `artifact.recording.stereoUrl` with legacy fallbacks; Twilio capture requests dual-channel media explicitly and handles the mono case cleanly; `--allow-mono` degraded opt-in.
- Not scorable semantics: a silent caller or an agent that was not talking at onset is reported as not scorable with a plain reason, never as a normal verdict; single-recording runs exit 2.
- Report audio embedding (`--embed-audio`) with a size guard; the demo report on hotato.dev carries playable synthetic audio.
- Neural cross-check verified against the real Silero model; measured properties documented.
- `docs/ADAPTER-STATUS.md` with per-stack API basis and last-verified dates; `SECURITY.md`; `docs/WHY.md`.

### Changed
- `hotato run` default output is human-readable text; CI examples use `--format json` explicitly.
- README rebuilt demo-first with a real screenshot and the live CI badge.
- Fix-map knob text refreshed to current LiveKit `TurnHandlingOptions` and Pipecat user-turn strategy APIs.

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

[Unreleased]: https://github.com/attenlabs/hotato/compare/v0.2.1...HEAD
[0.2.1]: https://github.com/attenlabs/hotato/compare/v0.2.0...v0.2.1
[0.2.0]: https://github.com/attenlabs/hotato/compare/v0.1.0...v0.2.0
[0.1.0]: https://github.com/attenlabs/hotato/releases/tag/v0.1.0
