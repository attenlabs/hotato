# Changelog

All notable changes to Hotato are recorded here.

The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and
the project aims to follow [Semantic Versioning](https://semver.org/spec/v2.0.0.html).
The one thing you will not find in any entry: an accuracy percentage. Hotato
reports millisecond measurement error and a confusion matrix, and it does that on
purpose. See `docs/BENCHMARK.md`.

## [Unreleased]

### Added

- **Reproducible benchmark harness** (`src/hotato/benchmark.py`, run it with
  `python3 -m hotato.benchmark`). Takes labelled dual-channel recordings and
  reports per-signal **measurement error in milliseconds** (onset, time-to-yield,
  response-gap) plus a `did_yield` **confusion matrix** against the
  should_yield / should_not_yield labels. It never aggregates into a single
  accuracy figure, because that would hide the missed-yield / false-yield
  trade-off. Runs on the bundled + example synthetic fixtures out of the box and
  writes a JSON + markdown report. Not wired into the CLI; it is a measurement
  tool you point at fixtures.
- **`docs/BENCHMARK.md`** — the methodology: exactly what is measured, why
  milliseconds-and-a-matrix is the honest metric and a percentage is not, how to
  reproduce it, and the bring-your-own-labelled-recordings path to real validity.
- **Corpus pipeline** (`corpus/`) — the tooling for the real-recording corpus:
  a labelling schema (`corpus/label.schema.json`) extending the scenario shape
  with provenance / consent / PII / attestation fields, a standalone stdlib
  validator (`corpus/validate.py`) that checks a `(recording, label)` pair
  conforms (two-channel WAV, required fields, timings in range, attestation), and
  a `corpus/README.md` call for contributions that defers to
  `docs/CORPUS-GOVERNANCE.md` for consent and PII. Ships one clearly-labelled
  synthetic example and **no real audio**.
- **Community scaffolding** — this changelog, GitHub issue templates (bug report
  and feature request), and a pull-request template whose checklist carries the
  project's honesty rules (no accuracy %, no fabricated numbers, energy ≠ intent,
  do not edit the vendored `_engine`, keep the drift gate green).

### Note

- The bundled and example fixtures remain **synthetic** and labelled as such.
  Nothing in this release fabricates a real recording, a real-model number, a
  benchmark result, a leaderboard, or a star count. The synthetic fixtures are a
  floor and a regression guard; real validity comes from contributed, consented,
  human-labelled calls.

## [0.1.0] - 2026-07-04

Initial build. Offline, MIT, zero-install turn-taking eval for voice agents. It
scores one narrow thing well and is honest about the rest.

### Added

- **Core scorer** — one recording or the bundled battery, returning a single
  machine-readable envelope (`schema_version` "1"). Three objective timing
  signals measured from audio energy over aligned caller/agent channels:
  `did_yield`, `seconds_to_yield` (time to yield), `talk_over_sec`. Delegated
  unchanged to the vendored MIT `barge_scoring` engine (`src/hotato/_engine`),
  kept byte-identical to upstream by `sync_engine.py` (a CI drift gate).
- **Signal bus** — a namespaced `signals` block: `barge_in` (mirrors the three
  originals byte-for-byte) and `latency` (`response_gap_sec`,
  `premature_start_sec`), pure endpointing timing on the same two VAD tracks. No
  new model, no new accuracy claim.
- **Fix map** — every failing event carries a concrete fix: `fix_class: "config"`
  (a stack-specific knob with direction and honest trade-off) or
  `fix_class: "engagement-control"` (a both-axes discrimination failure a single
  sensitivity dial cannot solve, pointed high-level, numbers-free, at an
  engagement-control / addressee-detection layer). Plus a suite-level funnel that
  fires only when both axes fail at once.
- **CLI** (`hotato`) — `run` (suite or single recording), `capture` and `setup`
  for stack adapters (Vapi, Twilio, LiveKit, Pipecat; Retell status is stated
  honestly), `--format json`, non-zero exit for CI, and `--dump-frames` for
  per-frame, re-derivable evidence behind every number.
- **One-tool MCP server** (`hotato-mcp`) — exposes exactly one tool,
  `voice_eval_run`, returning the identical JSON envelope, so an AI agent can run
  the eval mid-task.
- **Bundled synthetic battery** — an eight-scenario `barge-in` self-test
  (interruptions, backchannels, telephony 8 kHz, double-talk, echo bleed, rapid
  turn-taking) with exact rendered ground truth. A floor and a regression guard,
  not production audio.
- **Honest limits, everywhere** — a `limits` block in every result and in the MCP
  schema: no accuracy percentage, an explicit ceiling, energy-is-not-intent, and
  the stated best input (two-channel). Offline by design: no network egress of
  user audio.
- **JSON Schema** (`src/hotato/schema/envelope.v1.json`), `METHODOLOGY.md`,
  `CONTRIBUTING.md`, `docs/CORPUS-GOVERNANCE.md`, `llms.txt`, and a stdlib-only
  core with zero required third-party dependencies.

[Unreleased]: https://github.com/attenlabs/hotato/compare/v0.1.0...HEAD
[0.1.0]: https://github.com/attenlabs/hotato/releases/tag/v0.1.0
