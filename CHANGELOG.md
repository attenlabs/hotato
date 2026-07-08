# Changelog

All notable changes to Hotato are recorded here.

The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and
the project aims to follow [Semantic Versioning](https://semver.org/spec/v2.0.0.html).
Every entry reports millisecond measurement error and a confusion matrix, by
design. See `docs/BENCHMARK.md`.

## [Unreleased]

### Fixed
- **Softer unsourced wording in `docs/WHY.md` and `README.md`**: "about one in
  five reported barge-in bugs" is now "in our observed reports, many alleged
  barge-in bugs," and "the two highest-frequency complaints" is now "two
  common complaints"; no percentage or frequency ranking is claimed. Same
  treatment for "four timing failures dominate real transcripts" in
  `docs/WHY.md`, now "show up again and again."

### Added
- **`hotato analyze <folder>`, the zero-config drop-a-folder flagship**: point
  it at a folder of real dual-channel call recordings and it walks EVERY
  recording label-free with the existing whole-call scanner (`hotato scan`),
  aggregates the candidate turn-taking moments across all calls, and ranks them
  by the scanner's own salience (overlap seconds / gap seconds / echo coherence)
  so the worst moments float to the top. No scenarios, no labels, no onset, no
  flags required. It writes ONE self-contained, offline HTML dashboard reusing
  the `report.py` house style and its timeline SVG renderer: each top moment
  shows the call file, the timestamp, the candidate kind, the measured number,
  and a to-scale caller/agent timeline. For the top moments (`--audio-top`,
  default 8) the REAL audio around the moment is embedded inline as a base64 WAV
  data URI (nothing uploaded, zero external requests) with the **hear-the-bug
  player**: press play and a playhead sweeps that moment's timeline in sync with
  `audio.currentTime` (via `requestAnimationFrame`), landing on the measured
  overlap or gap; reduced-motion safe (the playhead rides `timeupdate` instead
  of the animation loop). `--format json` emits the ranked candidates plus their
  metadata for an agent to drive. Framed honestly throughout as MEASURED
  candidate timing moments you review and label with `hotato fixture create` —
  never a pass/fail, a failure count, an intent claim, or an accuracy number.
  Non-dual-channel or unreadable files are reported cleanly as skipped with
  their reason, never a crash. A bare `hotato <folder>` (a directory as the
  first argument) routes to `analyze`. Exit codes: 0 ran (candidate moments
  listed, possibly zero), 2 usage/IO error. Docs: `docs/ANALYZE.md`. Reuses
  `scan.py`, `report.py`, and the stdlib WAV reader; adds `scan.activity_tracks`
  (the exact per-frame tracks the scanner walks, for drawing the per-moment
  timelines).
- **`hotato ingest`, the composable passive on-ramp**: wire a webhook to invoke
  `hotato ingest` once and every completed call is scanned for candidate
  turn-taking moments automatically, so you never have to remember to run a CLI
  after a bad call. It composes existing primitives and adds only a per-stack
  webhook parser: it parses the platform payload for the call id / recording
  locator (`message.call.id` for Vapi, top-level `event` + `call.call_id` for
  Retell, `RecordingSid` for Twilio's form-encoded `recordingStatusCallback`,
  `egressInfo.fileResults[].location` for LiveKit egress, and a user-supplied
  `recording_path` / `recording_url` for Pipecat; field paths verified against
  live vendor docs on 2026-07-07, parsed defensively where unconfirmable), reuses
  the same per-stack fetch as `hotato capture` for the dual-channel recording,
  then runs `hotato scan` for candidates. Ingest is discovery, never a pass/fail
  and never an intent claim: exit 0 means it ran (candidates possibly zero), exit
  2 means a parse/fetch/IO error or not-scorable input. It never auto-labels,
  auto-fixtures, or auto-tunes; you promote a candidate with `hotato fixture
  create`. It is not a daemon (you own the trigger), the only network is the same
  recording fetch `capture` does, and a webhook payload is treated strictly as
  untrusted data and never executed. `--out` writes an HTML candidate report; new
  guide in `docs/INGEST.md` with the webhook-to-ingest recipe per stack.
- **CI sdist guard**: a new `sdist-guard` job in
  `.github/workflows/tests.yml`, separate from the `pytest` job, builds the
  sdist, extracts it to a clean directory, installs only that extracted tree
  into a fresh venv, and runs the full pytest suite from inside it. Fails the
  build on any collection error. This is the failure mode that shipped
  unguarded in 0.2.3 and 0.3.0: a green wheel masked a broken sdist.
- **MCP registry manifest and docs**: `server.json` at the repo root (name
  `io.github.attenlabs/hotato`, matching the `mcp-name:` marker already in
  `README.md`), pointing at the `hotato[mcp]` PyPI extra and the
  `hotato-mcp` console script. `.github/workflows/release.yml` wires a
  `publish-mcp-registry` job (`mcp-publisher publish`) for the eventual
  first publish, hard-gated (`if: false`, plus workflow_dispatch-only) until
  an operator explicitly lifts it; nothing publishes today. New
  `docs/MCP.md`: copy-paste `mcpServers` configs for Claude Desktop, Cursor,
  and Codex CLI, and the `uvx --from "hotato[mcp]" hotato-mcp` footgun
  (`uvx hotato-mcp` alone fails) called out explicitly. `mcp_server.py`'s
  tool description now states the envelope schema URL and the correct
  zero-install command. `ci/github_action.yml` now says plainly it is
  hotato's own dev CI, not a copy-paste template (that footgun pointed a
  copying agent at the wrong workflow); the real drop-in stays
  `.github/workflows/hotato.yml`. `llms.txt` is reconciled to every shipped
  0.3.1 command (`capture`, `setup`, `ingest`, `describe`, and the rest were
  missing) plus both schema URLs and the MCP one-liner; new `llms-full.txt`
  concatenates README + every `docs/*.md` + `METHODOLOGY.md` + the envelope
  schema with file-boundary headers, built deterministically by
  `scripts/build_llms_full.py`. New `CITATION.cff`.

## [0.3.1] - 2026-07-07

### Fixed
- **Self-consistent source distribution**: the sdist now ships the small,
  non-audio test dependencies (scenario labels, manifests, the corpus
  validator, and the deterministic builders under `corpus/` and `examples/`),
  so an extracted sdist collects and runs the full test suite instead of
  hitting collection errors. The heavy real and rendered audio stays pruned;
  suite and class audio is regenerated deterministically by
  `tests/conftest.py` (seed = sha256(id)) when absent, and the tests that
  depend on genuinely-absent heavy real audio skip cleanly rather than error.
- **Honest fix-plan wording in `README.md` and `docs/WHY.md`**: reworded the
  claim that every failing event returns a fix that "names the exact setting to
  change in your stack." A `plan()` may correctly refuse to tune a single
  threshold, report insufficient coverage, or emit a checklist, so the copy now
  says a fix class is always returned and, when the failure maps cleanly to
  stack config, the setting family and direction to investigate.

### Added
- **`hotato scan` self-truncation candidate**: a new candidate kind,
  `agent_stop_no_caller`, surfaces the agent going from active to quiet with
  zero caller energy anywhere nearby, a drop nothing on the caller channel
  explains (not a barge-in, not a caller-driven handoff). Additive; existing
  candidate kinds and scoring are unchanged.
- **Response-gap percentiles and a latency SLA gate**: `hotato team` and
  `hotato export` now also report p95 (alongside the existing mean/median/p90)
  for talk-over and time-to-yield, and pool `response_gap_sec` (dead air
  before the agent speaks) into the same distribution shape. `--max-response-gap
  SECONDS` on both commands gates the pooled p95 and fails (exit 1) exactly
  when it is exceeded, the same pass/fail contract as `--max-talk-over` /
  `--max-time-to-yield`. A plain export's `envelope.json` stays byte-identical
  to before; the new numbers live only in the printed summary and the
  returned manifest.
- **Echo detector**: a deterministic cross-channel coherence signal
  (`signals.echo`: `coherence`, `lag_sec`, `echo_suspected`) on every scored
  event, flagging when the caller channel looks like a lag-shifted copy of
  the agent's own audio (leaked TTS), so a stop the agent makes because it
  heard itself is no longer indistinguishable from a clean yield. New `scan`
  candidate kind `echo_correlated_activity`; a loud WARNING in `hotato
  diagnose` and the single-run text output for every echo-suspected yield;
  opt-in `--echo-gate` on `hotato run` holds a bleed-induced yield out of the
  verdict (`scorable: false`) instead of counting it as a pass. Default run
  behavior, and every existing golden verdict, is unchanged; `--echo-gate` is
  off by default.
- **Resume/restart detector**: on events where the agent yielded, an additive
  `signals.resume` block (`resumed`, `resume_gap_sec`, `restart_suspected`)
  measures from the agent's own VAD track whether it came back after the
  yield, how fast, and whether the post-resume run is long enough to look
  like a restart-from-the-top rather than a short continuation. Whether the
  resumed words repeat the earlier ones is out of scope (a transcript
  question, not a timing one).
- **Four corpus scenario classes** under `corpus/classes/` (deterministic,
  additive, same seeded-render/`--check` contract as `corpus/suites/`):
  `mid-utterance-pause`, `backchannel-multilingual`, `noise-hold`, and
  `telephony-degraded`. Detail: `corpus/classes/README.md`.
- **"Is this even a turn-taking bug?" triage** in `docs/WHY.md` and `README.md`:
  names the failure modes commonly conflated with turn-taking bugs (STT
  hallucination, client-side audio buffering, LLM verbosity/tool-selection,
  safety false-refusal, wrong-language STT) and which tool class to reach for
  instead, alongside a plain statement of the flagship case Hotato covers
  (agent-talks-over-caller and false-stop-on-backchannel).
- **Report accessibility**: the generated HTML report carries aria-labels on
  its inline SVGs and audio elements, a `main` landmark, and measured titles,
  so the artifact is navigable by assistive tech. Visual output is unchanged.
- **Startup lazy-imports**: `importlib.resources`, the report renderer, and the
  numpy accelerator used only inside `scan` are deferred to first use, so plain
  CLI paths import less at startup. Behavior and output are unchanged.

All of the above are additive optional fields or opt-in flags: `did_yield`
and `passed` for every existing fixture are unchanged, and the vendored
`src/hotato/_engine` is untouched (verify with `python3 sync_engine.py
--check`).

## [0.3.0] - 2026-07-06

### Added
- **`hotato fixture create`**: the missing piece of the regression loop. One
  bad call moment (a recording, an `--onset`, and YOUR `--expect yield|hold`
  label) becomes a permanent fixture: `scenarios/<id>.json` in the existing
  scenario schema shape (with provenance: source file, original onset,
  `created_by`) plus a two-channel `audio/<id>.example.wav`. Clips around the
  event by default (`--pre` 2.0 s / `--post` 6.0 s) and re-bases the onset to
  the clip; `--no-clip` keeps the full recording. The created fixture is
  validated by scoring it through the suite runner immediately; a not-scorable
  input is refused with the honest reason (exit 2) and partial outputs are
  removed unless `--force`. Overwrite refused without `--force`. Round-trips
  through `hotato run --scenarios DIR --audio DIR` as written.
- **`hotato compare`**: the shareable before/after on one fixed moment. Scores
  both takes with the identical expectation, bounds, and reference config and
  reports the per-signal movement plus one machine-stable result word:
  `fixed`, `regressed`, `improved`, `worse`, `unchanged`, `still_pass`, or
  `not_scorable`. Marks come from real measurements only; a not-scorable side
  renders NOT SCORABLE (exit 2), never an invented verdict.
  `--before-onset` / `--after-onset` handle the moment shifting between takes;
  `--out report.html` writes the HTML report with the before take as the base
  comparison; `--fail-on-worse` exits 1 on `regressed`/`worse`.
- **`hotato scan`**: candidate extraction across a WHOLE recording, as timing
  facts only. Walks the two VAD activity tracks (windowed pass, so long files
  never load fully into memory) and surfaces `overlap_while_agent_talking`
  (with overlap length and whether/when the agent went silent),
  `agent_start_during_caller`, and `long_response_gap` candidates, sorted by
  salience with `--top` (default 20). No intent claims anywhere: the output
  header states that you decide the expected behavior and label the moment
  with `hotato fixture create`.
- **`hotato diagnose` / `hotato inspect` / `hotato plan`**: the guarded,
  read-only fix ladder (landed on main unreleased; first release here).
  Diagnose explains a finished run per failing event plus a battery-level
  decision; inspect reads and normalizes the live turn-taking config (GET or
  static parse, never executed); plan combines them into a
  `hotato.fixplan.v1` proposal: at most one bounded step on one setting,
  refusal on the threshold funnel, checklist on ambiguous evidence,
  `insufficient_coverage` without a passing opposite-risk fixture, and
  `production_apply` pinned to false.
- `hotato plan` also accepts the result JSON as a positional argument
  (`hotato plan result.json`), and every plan now carries `kind: "fix-plan"`,
  a `platform_mutation` block (`performed` always false), the measured
  `evidence`, `risks`, `next_commands` (including the `hotato compare`
  verification step), and not-scorable events as `input_issues`. New Twilio
  rule: `--stack twilio` never yields agent-config advice; the plan points at
  channel assignment and the upstream voice-agent stack.
- `docs/BAD-CALL-TO-CI.md`: the five-step loop from one bad call to a CI
  gate, with the label semantics up top and explicit use/do-not-use lists;
  runnable walkthrough in `examples/bad-call-to-ci/`.

### Changed
- Label wording, stated wherever the backchannel story is told (README,
  METHODOLOGY, WHY, and the `run`/`doctor`/`demo`/`fixture create` help):
  Hotato does not infer intent. You label the expected behavior for the
  event: yield means the agent should stop for the caller. hold means the
  agent should keep speaking through a backchannel/noise/acknowledgement.
  Hotato then measures whether the timing matched that label.
- README positioning line sharpened: Hotato turns bad voice-agent call
  moments into offline regression tests.
- Input wording unified: Hotato's main scorer requires separated caller and
  agent tracks; a single mixed mono call is not enough to attribute talk-over
  reliably.

## [0.2.3] - 2026-07-06

### Changed
- Documentation clarity pass across README, docs, and the PyPI page. Every
  turn-taking term (talk-over, barge-in, yield, backchannel, endpointing, not
  scorable) is defined in plain language at first use, and fix descriptions
  name the concrete setting to change. No code or scoring behaviour changed.

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

[Unreleased]: https://github.com/attenlabs/hotato/compare/v0.3.1...HEAD
[0.3.1]: https://github.com/attenlabs/hotato/compare/v0.3.0...v0.3.1
[0.3.0]: https://github.com/attenlabs/hotato/compare/v0.2.3...v0.3.0
[0.2.3]: https://github.com/attenlabs/hotato/compare/v0.2.2...v0.2.3
[0.2.2]: https://github.com/attenlabs/hotato/compare/v0.2.1...v0.2.2
[0.2.1]: https://github.com/attenlabs/hotato/compare/v0.2.0...v0.2.1
[0.2.0]: https://github.com/attenlabs/hotato/compare/v0.1.0...v0.2.0
[0.1.0]: https://github.com/attenlabs/hotato/releases/tag/v0.1.0
